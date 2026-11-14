"""A KWP2000 (ISO 14230) diagnostic session with one module on the K-line, through an ELM327-style
adapter: the module's own address in the header, the standard wake-up, then requests one at a time.

Generic OBD-II on a K-line car goes through ``ElmBackend.send`` (functional addressing, answers
rebuilt in CAN form). Some modules keep more behind a session addressed to them alone, sometimes
from a tester address of the maker's choosing: ``can.kline_probe`` finds whether one exists and
what it answers. This is the piece that then uses it: ``open`` wakes the module, ``request`` sends
one service request and returns the positive answer's bytes, ``close`` hands the adapter back to
ordinary OBD-II. The adapter keeps the session alive with the tester-present message (``3E``) set
here as its wake-up message.

Only the transport is here. Which requests a maker's module understands, and what the answers
mean, belongs in a profile or a plugin; a request that changes the vehicle must be confirmed by
the caller first, like every other transmit.
"""

import threading
import time

from .elm327 import NONCAN_PROTOCOLS, ElmBackend, classify
from .kline_probe import NRC, SETTLE, message_body

REQUEST_TIMEOUT = 6.0
WAKE_TIMEOUT = 12.0
NO_REPLY_LIMIT = 3              # this many requests in a row without a reply: the session is gone


class KwpError(OSError):
    """The session couldn't be opened, or it was lost."""


class KwpNegative(Exception):
    """The module refused a request (7F service code)."""

    def __init__(self, service: int, code: int):
        self.service, self.code = service, code
        super().__init__(f"refused: {NRC.get(code, 'code')} (7F {service:02X} {code:02X})")


class KwpSession:
    """``fast`` picks the fast wake-up (ISO 14230 fast init) over the 5-baud one."""

    def __init__(self, backend: ElmBackend, ecu: int, tester: int = 0xF1, fast: bool = True,
                 settle: float | None = None):
        if not isinstance(backend, ElmBackend) or backend.j1939:
            raise KwpError("A KWP2000 session needs a Bluetooth OBD adapter (ELM327-style) on a K-line vehicle.")
        if backend.protocol and backend.protocol not in NONCAN_PROTOCOLS:
            raise KwpError(f"This vehicle answered on {backend.protocol_name}, not on the K-line.")
        self.backend, self.ecu, self.tester, self.fast = backend, ecu, tester, fast
        self.settle = SETTLE if settle is None else settle
        self.is_open = False
        self.key_bytes = ""
        self.requests = 0
        self.no_reply = 0               # requests in a row that got no reply
        self.last_error = ""
        self._lock = threading.Lock()   # one request at a time (the adapter's own lock is per command)

    def _at(self, cmd: str, timeout: float = 3.0) -> list[str]:
        lines = self.backend.command(cmd, timeout, keep_progress=True)
        if classify(lines) == "?":
            raise KwpError(f"The adapter doesn't understand {cmd}.")
        return lines

    def open(self):
        """Wake the module. Raises KwpError if it doesn't answer the wake-up."""
        b = self.backend
        with self._lock, b._lock:
            self._at("ATPC")                        # leave the OBD-II conversation
            if self.settle > 0:
                time.sleep(self.settle)             # a module ignores a wake-up until its last session timed out
            self._at("ATSP5" if self.fast else "ATSP4")
            head = f"{self.ecu:02X}{self.tester:02X}"
            self._at(f"ATSH81{head}")
            if not self.fast:
                self._at(f"ATIIA{self.ecu:02X}")
            self._at(f"ATWM81{head}3E")             # keep-alive: tester present, to this module
            lines = self._at("ATFI" if self.fast else "ATSI", WAKE_TIMEOUT)
            text = " ".join(lines).upper()
            if "OK" not in text or "ERROR" in text:
                self.last_error = " ".join(lines) or "no answer"
                self._restore()
                raise KwpError(f"The module at 0x{self.ecu:02X} didn't wake up ({self.last_error}). Key on?")
            self.key_bytes = " ".join(self._at("ATKW"))
            self.is_open, self.no_reply = True, 0

    def request(self, payload: bytes, timeout: float = REQUEST_TIMEOUT) -> bytes:
        """One request -> the positive answer (service + 0x40 first). Raises KwpNegative if the
        module refused, KwpError if nothing came back or the session is closed."""
        if not self.is_open:
            raise KwpError("The KWP2000 session isn't open.")
        with self._lock:
            try:
                lines = self.backend.command(bytes(payload).hex().upper(), timeout, keep_progress=True)
            except OSError as e:
                self.is_open = False
                raise KwpError(str(e) or "connection to the adapter lost") from e
            self.requests += 1
            bodies = [d for d in (message_body(line, True) for line in lines) if d]
            for d in bodies:
                if d[0] == payload[0] + 0x40:
                    self.no_reply = 0
                    return d
            for d in reversed(bodies):
                if d[0] == 0x7F and len(d) >= 3 and d[2] != 0x78:
                    self.no_reply = 0
                    raise KwpNegative(d[1], d[2])
            self.no_reply += 1
            self.last_error = classify(lines) or " / ".join(lines) or "no answer"
            if self.no_reply >= NO_REPLY_LIMIT:
                self.is_open = False
            raise KwpError(f"No reply to {bytes(payload).hex().upper()} ({self.last_error}).")

    def _restore(self):
        try:
            self.backend._init()
        except OSError as e:
            self.last_error = str(e)

    def close(self):
        """Hand the adapter back to ordinary OBD-II (it is set up again from scratch)."""
        was, self.is_open = self.is_open, False
        b = self.backend
        if b.is_open and (was or self.requests):
            with self._lock, b._lock:
                self._restore()
