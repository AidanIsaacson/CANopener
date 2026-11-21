"""``serial.log``: the bytes a capture heard on its serial lines, in arrival order.

Plain text, one chunk per line, like candump's format so the two read alike::

    # CANopener serial log 1
    # line {"line": "A", "port": "/dev/ttyUSB0", "framing": "9600 8N1", "label": "console"}
    (1700000000.123456) A 02 41 0D
    (1700000000.131002) B 06

The time is when the chunk arrived (wall clock, the same clock as ``capture.log``), the name is
the line it came in on, then the bytes in hex. The ``# line`` comments say what each line was.
A chunk is whatever the adapter handed over in one read: one message can arrive as several
chunks, so ``core/serial_analysis.py`` joins them back up by the gaps between them.

Rows are flushed about once a second while recording, so a crash loses at most that much.
Standard library only.
"""

import json
import os
import threading
import time

from .port import Chunk

HEADER = "# CANopener serial log 1"
LINE_PREFIX = "# line "


def format_chunk(c: Chunk) -> str:
    return f"({c.t:.6f}) {c.line} {c.data.hex(' ').upper()}"


def parse_chunk(text: str) -> Chunk | None:
    """One log row back into a Chunk; None for comments and anything unparsable."""
    if not text.startswith("("):
        return None
    try:
        stamp, rest = text[1:].split(")", 1)
        line, _, hexed = rest.strip().partition(" ")
        return Chunk(float(stamp), line, bytes.fromhex(hexed))
    except ValueError:
        return None


class SerialLog:
    """Writes every chunk a sniffer hears to ``path`` while a capture records."""

    def __init__(self, path: str, sniffer, flush_every: float = 1.0):
        self.path = path
        self.sniffer = sniffer
        self.chunks = 0
        self.bytes = 0
        self._lock = threading.Lock()
        self._flush_every = flush_every
        self._last_flush = time.monotonic()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "a")
        if new:
            self._f.write(HEADER + "\n")
            for d in sniffer.lines():
                self._f.write(LINE_PREFIX + json.dumps(d) + "\n")
            self._f.flush()
        sniffer.add_listener(self.write)

    def write(self, c: Chunk):
        with self._lock:
            if self._f is None:
                return
            self._f.write(format_chunk(c) + "\n")
            self.chunks += 1
            self.bytes += len(c.data)
            now = time.monotonic()
            if now - self._last_flush >= self._flush_every:
                self._f.flush()
                self._last_flush = now

    def close(self) -> int:
        self.sniffer.remove_listener(self.write)
        with self._lock:
            if self._f is not None:
                self._f.close()
                self._f = None
        return self.bytes


def read_serial_log(path: str) -> tuple[list[dict], list[Chunk]]:
    """(the lines described in the header, every chunk in file order)."""
    lines, chunks = [], []
    with open(path, errors="replace") as f:
        for row in f:
            row = row.rstrip("\n")
            if row.startswith(LINE_PREFIX):
                try:
                    lines.append(json.loads(row[len(LINE_PREFIX):]))
                except ValueError:
                    pass
                continue
            c = parse_chunk(row)
            if c is not None:
                chunks.append(c)
    return lines, chunks


def trim_serial_log(path: str, lo: float, hi: float) -> tuple[int, int]:
    """Keep only the chunks from ``lo`` to ``hi`` (comments stay). (bytes kept, bytes dropped)."""
    tmp = path + ".trim"
    kept = dropped = 0
    with open(path, errors="replace") as fin, open(tmp, "w") as fout:
        for row in fin:
            c = parse_chunk(row.rstrip("\n"))
            if c is None or lo <= c.t <= hi:
                fout.write(row)
                kept += len(c.data) if c else 0
            else:
                dropped += len(c.data)
    os.replace(tmp, path)
    return kept, dropped
