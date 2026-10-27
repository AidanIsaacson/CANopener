"""Connection status for the indicators, and the signal finder used while placing probes.

Two quick, read-only checks the front-ends show as lamps:

* ``adapter_status``: is the CAN adapter plugged into this computer (does its interface exist)?
* ``bus_status``: is the connected adapter hearing good traffic from the machine?

``SignalFinder`` is a background loop for back-probing a harness: it keeps listening, cycling
through the candidate bitrates, until it hears steady good traffic, then reports the bitrate it
locked on. It never stops on a failed attempt (no traffic, error frames, adapter unplugged for a
moment); it reports and tries again. Every attempt is **listen-only**, so nothing is transmitted
onto the machine's bus while the probes are being moved.
"""

import threading
import time
from dataclasses import dataclass

from ..can import pythoncan
from . import interfaces
from .connection import REPLAY, SOCKETCAN

OK, WAIT, BAD, OFF, SIM = "ok", "wait", "bad", "off", "sim"      # lamp colours: green / amber / red / grey / blue
IDLE = "idle"           # quiet but fine: a gatewayed OBD-II port with nobody asking (indigo, no chime)
QUIET_SECS = 2.0            # no good frame for this long = no traffic


@dataclass(frozen=True)
class Status:
    level: str              # OK / WAIT / BAD / OFF / SIM
    text: str               # a few words for the lamp
    detail: str = ""        # a sentence for the tooltip / panel


def adapter_status(kind: str, name: str, found: list | None = None, usb: list | None = None) -> Status:
    """Is the adapter for this connection plugged in? ``found`` / ``usb`` are the current
    ``interfaces.list_interfaces()`` / ``usb_adapters()``, looked up if not given."""
    if kind == REPLAY:
        return Status(SIM, "Log file", "Replaying a log file: no adapter needed.")
    if kind != SOCKETCAN:
        return Status(SIM, "Simulator", "Using a simulated bus: no adapter needed.")
    if pythoncan.is_spec(name):
        if not pythoncan.available():
            return Status(BAD, "python-can missing", pythoncan.INSTALL_HINT)
        return Status(OK, f"Adapter {name}", f"{name} is opened through python-can when you connect; "
                      "if it isn't plugged in, Connect says so.")
    found = interfaces.list_interfaces(details=False) if found is None else found
    match = next((i for i in found if i.name == name), None)
    if match is not None:
        return Status(OK, f"Adapter {name}", f"{name} is present ({match.state}).")
    others = [i.name for i in found if not i.virtual]
    if others:
        return Status(WAIT, "Adapter: other name", f"{name} isn't there, but {', '.join(others)} is. "
                      "Pick it in Connection setup.")
    usb = interfaces.usb_adapters() if usb is None else usb
    if usb:
        return Status(WAIT, "Adapter not set up", "\n".join(a.hint() for a in usb))
    return Status(BAD, "No adapter", "No CAN adapter found. Is it plugged into this computer?")


def bus_status(session, now: float | None = None, quiet: float = QUIET_SECS, last_answer: float = 0.0,
               polling: bool = False) -> Status:
    """Is the connected adapter hearing the machine? ``session`` may be None (not connected).

    For an OBD-II car pass ``last_answer`` (when a module last answered a request, 0 = never) and
    ``polling`` (the app is asking right now). Behind a security gateway the diagnostic port is
    silent unless a tester asks, so a quiet bus after modules have answered is normal (IDLE), not
    a lost connection. Silence while polling still is one."""
    if session is None:
        return Status(OFF, "Bus: not connected", "Not connected to a bus.")
    if session.offline:
        return Status(OFF, "Bus: log file", "Looking at a saved log, not a live bus.")
    now = time.time() if now is None else now
    h = session.health
    if h.link_error:
        return Status(BAD, "Bus: link error", f"The adapter reported: {h.link_error}. Unplugged?")
    if session.last_rx_t and now - session.last_rx_t <= quiet:
        return Status(OK, "Bus: traffic", "Good CAN traffic from the machine.")
    if h.last_error and now - h.last_error_t <= quiet:
        return Status(BAD, "Bus: errors only", f"Only error frames ({h.last_error}): wrong bitrate, or "
                      "CAN-H / CAN-L swapped or on the wrong wires?")
    if last_answer and not polling:
        return Status(IDLE, "Bus: quiet (gateway?)",
                      "Modules answered, and the port is quiet between requests: this car's diagnostic port "
                      "only carries traffic when asked (a security gateway). Stream or Check codes to talk to it.")
    if last_answer and polling:
        return Status(WAIT, "Bus: no answer", "Asking, but the modules stopped answering. Key off? Probe moved?")
    return Status(WAIT, "Bus: no traffic", "Connected but hearing nothing. Key on? Probes on CAN-H and CAN-L?")


def judge(good: int, errors: int, min_frames: int) -> bool:
    """Does a listening window count as good traffic? Enough valid frames, and errors don't dominate."""
    return good >= min_frames and errors * 2 <= good


def bitrate_order(preferred: int | None, candidates=interfaces.BITRATES) -> list[int]:
    """Candidates with the preferred one (last good / user's choice) tried first."""
    rest = [b for b in candidates if b != preferred]
    return ([preferred] if preferred else []) + rest


@dataclass(frozen=True)
class FinderEvent:
    state: str              # see SignalFinder: "searching", "no_adapter", "error", "heard", "locked", "failed"
    text: str
    bitrate: int | None = None
    good: int = 0
    errors: int = 0


class SignalFinder:
    """Listen until steady good traffic is heard, then lock on that bitrate.

    Events (passed to ``on_event``, from the finder's thread when started with ``start()``):

    * ``searching``  listened at a bitrate and heard nothing good; moving on
    * ``no_adapter`` the interface isn't there (unplugged?); checking again every second
    * ``error``      couldn't configure the interface this time; retrying
    * ``heard``      good frames at a bitrate; confirming they keep coming
    * ``locked``     steady good traffic; ``bitrate`` is the answer and the finder has finished
    * ``failed``     gave up: the interface exists but can't be configured (permissions, or the
                     adapter can't do listen-only). The only way it stops on its own besides locking.

    After locking, the interface is left up at the found bitrate, in listen-only unless
    ``listen_only=False`` (then it is brought up once more in normal mode, like Connect does).
    """

    def __init__(self, name: str, bitrates: list[int], on_event, *, listen_only: bool = True,
                 listen_secs: float = 1.0, confirm_secs: float = 1.0, min_frames: int = 5,
                 max_failures: int = 3, runner=None, bring_up=None, open_backend=None,
                 list_ifaces=None, hint=None):
        if not bitrates:
            raise ValueError("no bitrates to try")
        self.name = name
        self.bitrates = list(bitrates)
        self.on_event = on_event
        self.listen_only = listen_only
        self.listen_secs = listen_secs
        self.confirm_secs = confirm_secs
        self.min_frames = min_frames
        self.max_failures = max_failures
        self.runner = runner or interfaces.run_privileged
        self.bring_up = bring_up or interfaces.bring_up
        if open_backend is None:
            from ..can.backends import SocketCanBackend
            open_backend = SocketCanBackend
        self.open_backend = open_backend
        self.list_ifaces = list_ifaces or (lambda: interfaces.list_interfaces(details=False))
        self.hint = hint or (lambda: interfaces.no_adapter_hint().split("\n")[0])
        self.result: int | None = None     # the locked bitrate (None for vcan, or not locked)
        self.state = ""                      # the last event's state ("locked" once it has)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def virtual(self) -> bool:
        return self.name.startswith("vcan")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="signal-finder", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 3.0):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=wait)
        self._thread = None

    def _emit(self, state: str, text: str, bitrate=None, good=0, errors=0):
        self.state = state
        try:
            self.on_event(FinderEvent(state, text, bitrate, good, errors))
        except Exception:          # a front-end bug must not kill the loop
            pass

    def _listen(self, secs: float, enough: int | None) -> tuple[int, int]:
        backend = self.open_backend(self.name)
        backend.open()
        try:
            return interfaces.count_traffic(backend, secs, enough=enough, stop=self._stop.is_set)
        finally:
            backend.close()

    def run(self):
        """The loop (``start()`` runs it on a thread; tests call it directly)."""
        configured = None           # bitrate the interface is currently set to (listen-only)
        failures = 0
        i = 0
        while not self._stop.is_set():
            if not any(f.name == self.name for f in self.list_ifaces()):
                configured = None
                self._emit("no_adapter", f"{self.name} not found. {self.hint()}")
                self._stop.wait(1.0)
                continue
            br = None if self.virtual else self.bitrates[i % len(self.bitrates)]
            label = f"{br // 1000}k" if br else self.name
            if br is not None and configured != br:
                ok, msg = self.runner(interfaces.setup_steps(self.name, br, listen_only=True))
                if not ok:
                    failures += 1
                    if failures >= self.max_failures:
                        self._emit("failed", f"Couldn't set {self.name} to {label} listen-only: {msg}")
                        return
                    self._emit("error", f"Couldn't set {label} ({msg}); retrying")
                    self._stop.wait(1.0)
                    continue
                configured, failures = br, 0
            try:
                # A quick look first (ends as soon as frames arrive), then a full window to confirm
                # the traffic is steady and not a probe brushing the wire for a moment.
                good, errors = self._listen(self.listen_secs, self.min_frames)
                if judge(good, errors, self.min_frames) and not self._stop.is_set():
                    self._emit("heard", f"Hearing traffic at {label}; checking it's steady", br, good, errors)
                    good, errors = self._listen(self.confirm_secs, None)
                    if judge(good, errors, self.min_frames):
                        self._lock(br, good, errors)
                        return
            except OSError as e:     # unplugged mid-listen, interface went away: just go round again
                configured = None
                self._emit("error", f"{self.name}: {e}; retrying")
                self._stop.wait(0.5)
                continue
            if self._stop.is_set():
                return
            what = "nothing heard" if not good and not errors else \
                "errors only (wrong bitrate or wiring?)" if not good else f"only {good} frame(s), {errors} error(s)"
            self._emit("searching", f"{label}: {what}", br, good, errors)
            if not good or errors * 2 > good:
                i += 1              # some clean frames = right bitrate, flaky contact: stay on it

    def _lock(self, br, good, errors):
        self.result = br
        note = ""
        if br is not None and not self.listen_only:
            ok, msg = self.bring_up(self.name, br, False)
            note = "" if ok else f" (left in listen-only: couldn't switch to normal mode: {msg})"
        label = f"{br // 1000}k" if br else self.name
        self._emit("locked", f"Locked on {label}: {good} good frame(s) in {self.confirm_secs:.0f} s{note}",
                   br, good, errors)
