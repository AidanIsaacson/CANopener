"""One place that turns "what the user picked" into a running Session.

Front-ends (desktop GUI, CLI, a future on-device UI) all go through ``open_connection`` so
they get the same behaviour for real hardware, the simulator and log replay.
"""

from dataclasses import dataclass

from ..can.backends import SocketCanBackend, VirtualBackend, VirtualBus
from ..can.logfile import LogReplayBackend, read_log
from .session import Session

SOCKETCAN = "socketcan"
SIMULATOR = "sim"
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
        if self.kind == REPLAY:
            return f"Replay: {self.session.backend.channel}"
        return f"SocketCAN {self.session.backend.channel}"


def open_connection(kind: str, channel: str = "can0", *, listen_only: bool = False,
                    replay_speed: float = 1.0, replay_loop: bool = False, sim_seed=None) -> Connection:
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


def load_log_session(path: str) -> tuple[Session, int]:
    """Decode a whole log file offline into a backend-less Session."""
    session = Session(None)
    frames = read_log(path)
    for fr in frames:
        session.feed(fr)
    return session, len(frames)
