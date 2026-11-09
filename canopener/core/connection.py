"""One place that turns "what the user picked" into a running Session.

Front-ends (desktop GUI, CLI, a future on-device UI) all go through ``open_connection`` so
they get the same behaviour for real hardware, the simulator and log replay.
"""

from dataclasses import dataclass

from ..can import elm327, pythoncan
from ..can.backends import SocketCanBackend, VirtualBackend, VirtualBus
from ..can.logfile import LogReplayBackend, read_log_with_markers
from .session import Session

SOCKETCAN = "socketcan"
SIMULATOR = "sim"
PROFILE_SIM = "profile-sim"
OBD_SIM = "obd-sim"
CANOPEN_SIM = "canopen-sim"
REPLAY = "replay"
ELM = "elm"                 # a Bluetooth (or Wi-Fi / USB) ELM327-style OBD adapter: can.elm327
ELM_SIM = "elm-sim"         # the simulated adapter plugged into the OBD-II simulator
# The cars the Bluetooth OBD simulator can play (settings: elm.sim_vehicle; sim.elm.VEHICLES)
SIM_CARS = [("CAN, 11-bit (most cars)", "11"), ("CAN, 29-bit", "29"), ("J1850 VPW (older GM)", "vpw"),
            ("J1850 PWM (older Ford)", "pwm"), ("ISO 9141-2 (older Chrysler, Asian, European)", "iso9141"),
            ("KWP2000, slow start", "kwp-slow"), ("KWP2000, fast start", "kwp")]


@dataclass
class Connection:
    kind: str
    session: Session
    simulator: object = None

    def close(self):
        self.session.stop()
        if self.simulator is not None:
            self.simulator.stop()

    def describe(self) -> str:
        if self.kind == SIMULATOR:
            return "Simulator (virtual bus)"
        if self.kind == PROFILE_SIM:
            return f"Profile simulator: {self.simulator.profile.name}"
        if self.kind == OBD_SIM:
            return "OBD-II simulator (virtual car)"
        if self.kind == CANOPEN_SIM:
            return "CANopen simulator (virtual battery truck)"
        if self.kind == REPLAY:
            return f"Replay: {self.session.backend.channel}"
        if self.kind == ELM_SIM:
            what = getattr(self.simulator, "vehicle", "11")
            return ("Bluetooth OBD simulator (virtual adapter and "
                    + ("J1939 machine)" if what == "j1939" else "car)" if what in ("11", "29") else f"{what} car)"))
        if self.kind == ELM:
            return self.session.backend.describe()
        if self.session.backend.kind == pythoncan.PythonCanBackend.kind:
            return self.session.backend.describe()
        return f"SocketCAN {self.session.backend.channel}"


def open_connection(kind: str, channel: str = "can0", *, listen_only: bool = False,
                    replay_speed: float = 1.0, replay_loop: bool = False, sim_seed=None,
                    profile=None, bitrate: int | None = 250000, elm_protocol: str = "0",
                    elm_bus: str = "") -> Connection:
    """Open a bus and start a session. Raises OSError / BackendError on failure.

    For SOCKETCAN, ``channel`` is a Linux interface (can0, already up at its bitrate) or an
    ``interface:channel`` adapter name for python-can (``can.pythoncan``), which ``bitrate``
    configures. For ELM it's the adapter: a Bluetooth address, ``tcp:host:port`` or a serial
    device (``can.elm327.parse_channel``), and ``elm_protocol`` its ATSP protocol ("0" = search,
    "A" = J1939). A car only works by asking, so ``listen_only`` applies to J1939 only. ELM_SIM's
    channel picks the simulated vehicle: "sim", "sim:29", "sim:vpw", "sim:iso9141", "sim:j1939" ...
    (``sim.elm.VEHICLES``)."""
    sim = None
    if kind == SOCKETCAN and pythoncan.is_spec(channel):
        backend = pythoncan.PythonCanBackend(channel, bitrate, listen_only)
    elif kind == SOCKETCAN:
        backend = SocketCanBackend(channel)
    elif kind == SIMULATOR:
        from ..sim import MachineSimulator  # imported lazily: only needed for the simulator
        bus = VirtualBus()
        sim = MachineSimulator(VirtualBackend(bus, "sim-ecus"), seed=sim_seed)
        sim.start()
        backend = VirtualBackend(bus, "sim")
    elif kind == PROFILE_SIM:
        from ..sim.profile_sim import ProfileSimulator
        if profile is None:
            raise ValueError("pick a machine profile to simulate first")
        bus = VirtualBus()
        sim = ProfileSimulator(VirtualBackend(bus, "sim-ecus"), profile, seed=sim_seed)
        sim.start()
        backend = VirtualBackend(bus, "sim")
    elif kind == OBD_SIM:
        from ..sim.obd import ObdSimulator
        bus = VirtualBus()
        sim = ObdSimulator(VirtualBackend(bus, "obd-ecu"), seed=sim_seed)
        sim.start()
        backend = VirtualBackend(bus, "sim")
    elif kind == CANOPEN_SIM:
        from ..sim.canopen import CanopenSimulator
        bus = VirtualBus()
        sim = CanopenSimulator(VirtualBackend(bus, "canopen-nodes"), seed=sim_seed)
        sim.start()
        backend = VirtualBackend(bus, "sim")
    elif kind == REPLAY:
        backend = LogReplayBackend(channel, speed=replay_speed, loop=replay_loop)
    elif kind == ELM:
        bus = None
        if elm_bus:
            from .adapter_buses import find
            bus = find(elm_bus)
            if bus is None:
                raise elm327.ElmError(f"No adapter bus {elm_bus!r}: is the plugin that adds it installed?")
            elm_protocol = bus.protocol or "0"
        backend = elm327.ElmBackend(channel, protocol=elm_protocol, bus=bus)
        listen_only = listen_only and elm_protocol.upper() == elm327.J1939_PROTOCOL   # a car only works by asking
    elif kind == ELM_SIM:
        from ..sim.elm import SimulatedCar
        vehicle = channel.partition(":")[2] if ":" in (channel or "") else "11"
        if vehicle == "j1939" and elm_protocol == "0":
            elm_protocol = elm327.J1939_PROTOCOL        # the automatic search never picks J1939
        sim = SimulatedCar(seed=sim_seed, vehicle=vehicle)
        sim.start()
        backend = elm327.ElmBackend("simulated", protocol=elm_protocol,
                                    stream=elm327.SocketStream(sim.adapter.host_socket))
        listen_only = listen_only and elm_protocol.upper() == elm327.J1939_PROTOCOL
    else:
        raise ValueError(f"unknown connection kind {kind!r}")
    session = Session(backend)
    session.listen_only = listen_only or not backend.can_transmit
    try:
        session.start()
    except Exception:
        if sim:
            sim.stop()
        raise
    return Connection(kind, session, sim)


def load_log_session(path: str) -> tuple[Session, int, list[tuple[float, str]]]:
    """Decode a whole log file offline into a backend-less Session.

    Returns the session, the frame count, and any ``# === MARKER ... ===`` comment lines a
    field logger wrote into the log, as ``(epoch, label)`` aligned to the frame timestamps.
    """
    session = Session(None)
    frames, markers = read_log_with_markers(path)
    for fr in frames:
        session.feed(fr)
    return session, len(frames), markers
