"""What can this OBD adapter actually do? A capability report for an ELM327-style adapter.

Sellers' listings say a lot; this asks the adapter. The quick probe only sends adapter commands
(AT / ST settings it accepts or refuses, restored afterwards) plus, if a vehicle answered, one
standard read (vehicle speed, Mode 01 PID 0D) to see whether the adapter takes the 'expect one
answer' digit. Two optional parts talk to the vehicle more:

* ``protocols=True`` tries each OBD-II protocol in turn with the standard 'supported PIDs' request
  (a read). The K-line ones wake the car's computer first (a few seconds each).
* ``listen`` seconds of passive monitoring (ATMA) on the current protocol, or on J1939 (protocol A,
  250k) with ``j1939=True``: how many frames and IDs it hears. Nothing is transmitted (the adapter
  monitors silently where it supports ATCSM1).

Front-ends: ``canopener-cli bluetooth probe``, and Test in the Pair / Manage dialog (quick probe).
"""

from dataclasses import dataclass

from .elm327 import CAN_PROTOCOLS, PROTOCOLS, ElmBackend, classify, parse_frame_line

YES, NO, UNKNOWN = "yes", "no", "?"


@dataclass
class ProbeItem:
    section: str
    name: str
    status: str             # yes / no / ? (couldn't tell) / or a value ("ELM327 v2.2")
    detail: str = ""


# (command, what it means) for adapter commands that change nothing lasting. Each is restored
# by the commands after it or by re-applying the backend's own settings at the end.
COMMANDS = [
    ("ATCSM1", "Silent monitoring (listen without acknowledging frames)"),
    ("ATAR", "Automatic receive filter"),
    ("ATCRA7E8", "Receive filter by ID (UDS to other modules)"),
    ("ATCP18", "29-bit priority byte"),
    ("ATST32", "Response timeout setting"),
    ("ATPBC001", "User CAN protocols B / C (other bitrates)"),
    ("ATJE", "J1939 data format commands"),
    ("ATJHF0", "J1939 header display setting"),
]
STN_COMMANDS = [
    ("STDI", "STN device ID"),
    ("STSN", "STN serial number"),
    ("STPRS", "STN protocol report"),
]


def _status(lines: list[str]) -> str:
    if not lines:
        return UNKNOWN
    return NO if classify(lines) == "?" else YES


def probe(b: ElmBackend, protocols: bool = False, listen: float = 0.0, j1939: bool = False,
          progress=None) -> list[ProbeItem]:
    """Probe an opened ``ElmBackend``. ``progress(text)`` is called before each slow step."""
    say = progress or (lambda _t: None)
    out: list[ProbeItem] = []
    add = out.append
    add(ProbeItem("Adapter", "Chip / version", b.version or UNKNOWN))
    add(ProbeItem("Adapter", "Maker's description", b.device or "(none)"))
    add(ProbeItem("Adapter", "STN (OBDLink-style) commands", YES if b.stn else NO,
                  b.stn or "STI wasn't understood"))
    add(ProbeItem("Adapter", "Battery voltage at the OBD port", b.read_voltage() or UNKNOWN))
    add(ProbeItem("Vehicle", "Protocol found", b.protocol_name or NO,
                  "" if b.protocol else "nothing answered the automatic search (key on? plugged in?)"))

    say("Checking which adapter commands it understands")
    for cmd, what in COMMANDS:
        add(ProbeItem("Commands", what, _status(b.command(cmd)), cmd))
    if b.stn:
        for cmd, what in STN_COMMANDS:
            lines = b.command(cmd)
            st = _status(lines)
            add(ProbeItem("Commands", what, " ".join(lines) if st == YES else st, cmd))
    # The J1939 protocol: accepted as a protocol choice? (Selecting it sends nothing.)
    j = b.command("ATSPA")
    add(ProbeItem("Commands", "J1939 protocol (ATSP A, 250k)", _status(j), "ATSPA"))
    _restore(b)

    if b.protocol in CAN_PROTOCOLS:
        say("One standard read, to check the 'expect one answer' digit")
        from .frame import CanFrame
        req = 0x18DA10F1 if b.extended else 0x7E0
        try:
            b.send(CanFrame(req, bytes([0x02, 0x01, 0x0D]), extended=b.extended))
            add(ProbeItem("Commands", "'Expect one answer' digit (faster live data)",
                          YES if b.fast_single else NO, f"last round trip {b.last_round_trip * 1000:.0f} ms"))
        except OSError as e:
            add(ProbeItem("Commands", "'Expect one answer' digit (faster live data)", UNKNOWN, str(e)))

    if protocols:
        for num in "123456789":
            say(f"Trying {PROTOCOLS[num]}")
            b.command(f"ATSP{num}")
            lines = b.command("0100", 15.0)
            msg = classify(lines)
            answered = any(_is_hex(line) for line in lines)
            add(ProbeItem("Protocols", PROTOCOLS[num], YES if answered else NO,
                          msg or ("answered" if answered else "no answer")))
        _restore(b)

    if listen > 0:
        if j1939:
            b.command("ATSPA")
        say(f"Listening for {listen:g} s")
        b.command("ATCSM1")
        lines = b.monitor("ATMA", listen)
        ext = True if j1939 else b.extended
        frames = [f for f in (parse_frame_line(line, ext) for line in lines) if f]
        ids = {f[0] for f in frames}
        trouble = classify(lines)
        add(ProbeItem("Listening", "J1939 (250k)" if j1939 else (b.protocol_name or "current protocol"),
                      f"{len(frames)} frames, {len(ids)} IDs",
                      (f"adapter said {trouble}; " if trouble else "")
                      + ", ".join(f"{i:X}" for i in sorted(ids)[:12]) + (" ..." if len(ids) > 12 else "")))
        _restore(b)
    return out


def _is_hex(line: str) -> bool:
    s = line.replace(" ", "")
    return len(s) >= 6 and all(c in "0123456789ABCDEFabcdef" for c in s)


def _restore(b: ElmBackend):
    """Put the adapter back the way the backend set it up."""
    for cmd in ("ATH1", "ATS0", "ATAR", "ATST32", f"ATSP{b.protocol_choice}"):
        b.command(cmd)
    b._header, b._filter = None, ""
    if b.protocol_choice == "0" and b.protocol:
        b.command(f"ATSP{b.protocol}")     # the protocol it found: no new search needed


def report_lines(items: list[ProbeItem]) -> list[str]:
    """The report as text, one section after another."""
    lines, section = [], None
    for it in items:
        if it.section != section:
            section = it.section
            lines += ([""] if lines else []) + [section]
        lines.append(f"  {it.name:56} {it.status}" + (f"   ({it.detail})" if it.detail else ""))
    return lines
