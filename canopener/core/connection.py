"""One place that turns "what the user picked" into a running Session.

Front-ends (desktop GUI, CLI, a future on-device UI) all go through ``open_connection`` so
they get the same behaviour for real hardware, the simulator and log replay.
"""

from dataclasses import dataclass

from ..can.backends import SocketCanBackend, VirtualBackend, VirtualBus
from ..can.logfile import LogReplayBackend, read_log_with_markers
from .session import Session

SOCKETCAN = "socketcan"
SIMULATOR = "sim"
PROFILE_SIM = "profile-sim"
OBD_SIM = "obd-sim"
REPLAY = "replay"


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
        if self.kind == REPLAY:
            return f"Replay: {self.session.backend.channel}"
        return f"SocketCAN {self.session.backend.channel}"


def open_connection(kind: str, channel: str = "can0", *, listen_only: bool = False,
                    replay_speed: float = 1.0, replay_loop: bool = False, sim_seed=None,
                    profile=None) -> Connection:
    """Open a bus and start a session. Raises OSError / BackendError on failure."""
    sim = None
    if kind == SOCKETCAN:
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
    elif kind == REPLAY:
        backend = LogReplayBackend(channel, speed=replay_speed, loop=replay_loop)
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
