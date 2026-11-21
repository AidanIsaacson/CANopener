"""The Zapi console link, for both front-ends: listen to the console port, show what goes by.

Groundwork for a mode that will one day *be* the console. Today it only listens: one tap per
direction (the console's transmit wire and the controller's, docs/SERIAL-SNIFFING.md), ports and
framing from settings (``zapi.console_port``, ``zapi.controller_port``, ``zapi.framing``).

``Console`` opens the taps (a ``serialline.port.Sniffer``), joins chunks into messages as they
come (the same rules as ``core.serial_analysis.split_messages``), keeps the recent ones with what
``protocol.describe`` makes of them, and runs the whole analysis on what it has heard on demand.
While it listens, ``sniffer`` is what a capture records (the front-ends hand it to the Log tool
through their context's ``serial``), so a recording has CAN, meters and the console side by side.

Nothing is sent: the taps are opened listen-only. No Qt, no Pygame.
"""

import threading
import time
from collections import Counter, deque

from ...core import serial_analysis
from ...serialline import port as sp
from ...serialline.framing import Framing
from ...serialline.port import Chunk
from . import protocol

LINES = (("console", "zapi.console_port", "the console's transmit wire"),
         ("controller", "zapi.controller_port", "the controller's transmit wire"))
KEEP_MESSAGES = 400             # shown on screen
KEEP_CHUNKS = 50_000            # kept for "Analyze" (a few minutes of a busy console)
IDLE, LISTENING, FAILED = "idle", "listening", "failed"


class Shown:
    """One message on screen: the message and what we make of it."""

    __slots__ = ("message", "text")

    def __init__(self, message: serial_analysis.Message, text: str):
        self.message, self.text = message, text

    @property
    def hex(self) -> str:
        return self.message.data.hex(" ").upper()


class Console:
    def __init__(self, settings, opener=None):
        """``opener(lines, framing)`` -> Sniffer; tests pass one that needs no ports."""
        self.settings = settings
        self._opener = opener or (lambda lines, framing: sp.Sniffer.open(lines, framing))
        self.sniffer: sp.Sniffer | None = None
        self.state = IDLE
        self.error = ""
        self.messages: deque = deque(maxlen=KEEP_MESSAGES)
        self.chunks: deque = deque(maxlen=KEEP_CHUNKS)
        self.bytes = Counter()
        self.count = 0                  # messages since listening started
        self._lock = threading.Lock()
        self._open: dict = {}
        self._last_line = None
        self.gap = serial_analysis.DEFAULT_GAP

    # --- settings ---
    def ports(self) -> list[tuple[str, str]]:
        """(line, port) for the lines that have a port set."""
        return [(line, self.settings.get(key) or "") for line, key, _ in LINES if self.settings.get(key)]

    @property
    def framing(self) -> Framing:
        try:
            return Framing.parse(self.settings.get("zapi.framing") or "9600 8N1")
        except ValueError:
            return Framing()

    # --- listening ---
    @property
    def listening(self) -> bool:
        return self.state == LISTENING

    def start(self) -> str:
        """Open the taps and listen. Returns "" or why it couldn't."""
        if self.listening:
            return ""
        lines = self.ports()
        if not lines:
            self.state, self.error = FAILED, "No port set: pick the console's and the controller's taps first"
            return self.error
        try:
            framing = self.framing
            self.sniffer = self._opener(lines, framing)
        except (sp.SerialError, ValueError) as e:
            self.state, self.error = FAILED, str(e)
            return self.error
        self.gap = serial_analysis.default_gap(framing)
        with self._lock:
            self.messages.clear()
            self.chunks.clear()
            self.bytes.clear()
            self.count = 0
            self._open, self._last_line = {}, None
        self.sniffer.add_listener(self._chunk)
        self.sniffer.start()
        self.state, self.error = LISTENING, ""
        return ""

    def stop(self):
        s, self.sniffer = self.sniffer, None
        if s is not None:
            s.remove_listener(self._chunk)
            s.close()
        self.poll(force=True)
        if self.state == LISTENING:
            self.state = IDLE

    def _chunk(self, c: Chunk):
        with self._lock:
            self.chunks.append(c)
            self.bytes[c.line] += len(c.data)
            m = self._open.get(c.line)
            if m is not None and (c.t - m.t_end > self.gap or self._last_line != c.line):
                self._close(c.line)
                m = None
            if m is None:
                self._open[c.line] = serial_analysis.Message(c.line, c.t, c.t, c.data)
            else:
                m.data += c.data
                m.t_end = c.t
                m.chunks += 1
            self._last_line = c.line

    def _close(self, line: str):
        m = self._open.pop(line)
        self.messages.append(Shown(m, protocol.describe(m)))
        self.count += 1

    def poll(self, now: float | None = None, force: bool = False):
        """Finish messages that have gone quiet (call from the screen's tick). Also checks the
        taps: an unplugged adapter turns the console FAILED with the reason."""
        now = time.time() if now is None else now
        with self._lock:
            for line, m in list(self._open.items()):
                if force or now - m.t_end > self.gap:
                    self._close(line)
        if self.sniffer is not None and self.sniffer.errors():
            self.error = "; ".join(self.sniffer.errors())
            self.state = FAILED

    # --- for the screens ---
    def recent(self, n: int = 50) -> list[Shown]:
        with self._lock:
            return list(self.messages)[-n:]

    def status(self) -> str:
        if self.state == FAILED:
            return self.error or "Failed"
        if not self.listening:
            return "Not listening" if self.ports() else "No ports set"
        parts = [f"{line} {self.bytes.get(line, 0):,} B" for line, _ in self.ports()]
        return f"Listening at {self.framing}: " + ", ".join(parts) + f", {self.count} messages"

    def analyze(self) -> serial_analysis.Report:
        """The whole analysis over what's been heard since listening started."""
        with self._lock:
            chunks = list(self.chunks)
        meta = [{"line": line, "port": port, "framing": str(self.framing)} for line, port in self.ports()]
        return serial_analysis.analyze(chunks, meta, gap=self.gap)
