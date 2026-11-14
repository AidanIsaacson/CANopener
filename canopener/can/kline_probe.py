"""What does this K-line computer actually answer? A read-only deep probe through an ELM327-style
adapter, for a vehicle or machine whose generic OBD-II answers are thin (it says it supports no
live data, or shows no codes while its lamp is on).

Some engine computers on ISO 9141-2 / KWP2000 answer SAE J1979 only as far as the law asked and
keep their real diagnostics behind the KWP2000 services (ISO 14230-3) on the same wire. This asks
everything standard that only reads, and writes down every reply exactly as the adapter printed it:

1. **Generic OBD-II, asked anyway**: live values, lamp status, freeze frame, codes and vehicle info,
   whatever the 'supported' answer said.
2. **Each K-line wake-up** (ISO 9141-2, KWP2000 5-baud, KWP2000 fast) with the standard request.
3. **KWP2000 addressed to the module itself** (the address that answered OBD-II, from tester
   addresses 0xF1 and 0xFC): tester present, read identification, read codes by status, read a
   data block. If one is refused for the session, the default diagnostic session is started
   (``10 81``, what the module is in anyway) and it is asked again.
4. **Where the module has the data-block read** (service ``21``: it answered, or refused only the
   block number), **every block number** (``21 00`` - ``21 FF``) and **every identification option**
   (``1A 80`` - ``1A 9F``), plus whether three more read services exist. Reads by definition
   (ISO 14230-3 ReadDataByLocalIdentifier / ReadEcuIdentification); the transcript lists the ones
   that answered and counts the refusals.
   First a few numbers are tried in other request shapes (more bytes after the number), in case
   the plain shape is what's refused. The full scans only run with ``full=True``.
5. **Where it has the common-identifier read** (service ``22``, two-byte numbers): every page sampled
   (``22 xx 00`` / ``22 xx 01``), then a full scan of ``CID_PAGES`` only, and only with the engine off
   (see ``CID_PAGES`` for why: a sweep of another page stopped an engine).

Nothing here clears, writes, resets or runs a test, but see ``CID_PAGES``: the full scan (``full=True``)
of a maker's identifiers is not risk-free and is kept to engine-off. No maker-specific identifiers are requested:
what a maker's blocks mean belongs in a profile or the plugin, worked out from our own captures.

The transcript is the point: ``Report.lines()`` goes into the capture (``kline_probe.txt``) so it
uploads with it. ``run`` holds the adapter for its whole length (a minute or two) and sets it up
again from scratch at the end.
"""


import time
from dataclasses import dataclass, field

from ..obd.dtc import decode_dtc
from .elm327 import NONCAN_PROTOCOLS, PROTOCOLS, ElmBackend, classify

REPORT_FILE = "kline_probe.txt"
WARNING = ("Asks the engine computer read-only questions over the K-line (about 80), including ones it says it "
           "doesn't support, and tries each wake-up (ISO 9141-2, KWP2000). It doesn't clear codes, change "
           "settings or run tests. Live data pauses while it runs (about 2 minutes). Key on, engine running or not.")
WARNING_FULL = ("ENGINE OFF, key on, machine parked and safe to lose power. This also asks for every data-block "
                "number and a few thousand identifier numbers. They are reads, but a maker's identifiers aren't "
                "guaranteed harmless: on one machine a scan like this made the engine computer restart and the "
                "engine stop. It won't run if the engine looks to be running. Up to 8 minutes.")
REQUEST_TIMEOUT = 12.0          # a first request includes the wake-up (5-baud: 2-3 s, retried by some adapters)
TESTERS = (0xF1, 0xFC)          # tester addresses tried: the standard one, and one some makers' tools use
DEFAULT_ECU = 0x10              # the engine, when nothing answered OBD-II to tell us
SETTLE = 5.5                    # seconds of silence before another wake-up: a module stays in its
#                                 session for up to 5 s after the last request and ignores a wake-up till then

# Generic OBD-II reads (SAE J1979), asked whether or not the module lists them as supported.
GENERIC = [
    ("0100", "supported live values 01-20"),
    ("0101", "lamp status and code count"),
    ("0103", "fuel system status"),
    ("0104", "engine load"),
    ("0105", "coolant temperature"),
    ("010C", "engine speed"),
    ("010D", "vehicle speed"),
    ("0111", "throttle position"),
    ("011C", "which OBD standard it follows"),
    ("0120", "supported live values 21-40"),
    ("0140", "supported live values 41-60"),
    ("020000", "freeze frame: supported values"),
    ("020200", "freeze frame: the code that stored it"),
    ("03", "stored codes"),
    ("07", "pending codes"),
    ("0600", "on-board test results: supported"),
    ("0900", "vehicle info: supported"),
    ("0902", "VIN"),
    ("0904", "calibration ID"),
]

# KWP2000 (ISO 14230-3) reads, in the order asked. (request, what it is, ask again after 10 81?)
KWP_READS = [
    ("3E", "tester present", False),
    ("1A80", "read identification (table)", True),
    ("1A90", "read identification (VIN)", True),
    ("1A91", "read identification (hardware number)", True),
    ("1A94", "read identification (software number)", True),
    ("1800FF00", "read codes by status (all groups)", True),
    ("1802FF00", "read codes by status (2-byte codes, all groups)", True),
    ("1802FFFF", "read codes by status (2-byte codes, group FFFF)", True),
    ("13", "read codes (older service)", True),
    ("2101", "read data block 01", True),
    ("2100", "read data block 00", True),
    ("0100", "OBD-II supported live values, addressed", False),
    ("03", "OBD-II stored codes, addressed", False),
]
# The scan, where service 21 exists: (service, the numbers to try, what one is)
SCANS = [(0x21, range(0x00, 0x100), "data block"), (0x1A, range(0x80, 0xA0), "identification option")]
# Do these read services exist at all? (Any refusal but 'service not supported' says yes.)
EXISTS = [("1200", "read freeze frame data"), ("17FF00", "read status of codes"), ("220000", "read data by common ID")]
# Common identifiers (service 22, two bytes) are too many to try them all (65536, hours on a K-line):
# every page is sampled at xx 00 / xx 01, and only these pages are scanned in full: the ones public
# notes on K-line command sets of this kind describe as values. **Other pages are never swept**, even
# when their sample answers: on the first real machine a sweep of page 14 (which answered almost every
# number in a row, like a window onto memory or hardware registers, not a list of values) ended with
# the engine computer going silent at 22 14 44 and the engine stopping. A "read" of a maker's
# identifier is not guaranteed to be free of side effects.
CID_PAGES = (0x11, 0x12, 0x13)
RUNNING_VOLTS = 13.2            # at or above this at the OBD port the engine is taken to be running (charging)
# Request shapes: a module that refuses every number of a read the same way may want more bytes
# after the number (ISO 14230-3 allows a transmission mode and a count there). A few numbers are
# tried with each ending, and the bare freeze-frame read in a few lengths: an answer that differs
# from the plain refusal shows the shape. (service, numbers, endings)
# 04 01 = "one answer, now". (A bare 04 is left out: the first real machine answered it 256 times over.)
SHAPES = [(0x21, ("01", "80", "81"), ("0401", "01", "0101")),
          (0x22, ("0000", "1101", "1201", "1301"), ("0401", "01", "0101"))]
FREEZE_SHAPES = ("12", "1201", "120000", "120001", "12000000", "1200000401")
SCAN_GIVE_UP = 3                # this many requests in a row with no reply: the session is gone
DEFAULT_SESSION = ("1081", "start the default diagnostic session")

NRC = {0x10: "general reject", 0x11: "service not supported", 0x12: "sub-function not supported / bad format",
       0x21: "busy, repeat", 0x22: "conditions not correct", 0x23: "routine not complete",
       0x31: "request out of range", 0x33: "security access denied", 0x78: "response pending",
       0x80: "not supported in this diagnostic session"}
INIT_OK, INIT_FAILED = "woke up", "no wake-up"


@dataclass
class Step:
    section: str
    command: str
    what: str
    lines: list
    seconds: float
    result: str = ""            # how it read: "answered: 41 00 ...", "refused: ...", "no data" ...
    data: list = field(default_factory=list)   # the message bodies (bytes), headers and check byte off
    positive: bool = False


@dataclass
class Report:
    adapter: str = ""
    started: float = 0.0
    connected_protocol: str = ""
    ecu: int = DEFAULT_ECU
    steps: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    error: str = ""

    def lines(self) -> list[str]:
        """The whole transcript, for a file."""
        out = ["K-line deep probe (read-only): every command sent and every line the adapter printed.",
               f"Adapter: {self.adapter}",
               f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
               f"Connected protocol: {PROTOCOLS.get(self.connected_protocol, self.connected_protocol or 'none')}",
               f"Module address probed: 0x{self.ecu:02X}", "", "== What it found =="]
        out += [f"- {f}" for f in self.findings] or ["- (nothing)"]
        if self.error:
            out += ["", f"!! The probe stopped early: {self.error}"]
        section = None
        for s in self.steps:
            if s.section != section:
                section = s.section
                out += ["", f"== {section} =="]
            out.append(f"> {s.command:<10} {s.what}   [{s.seconds:.2f} s]  => {s.result}")
            out += [f"    < {line}" for line in s.lines] or ["    < (nothing)"]
        return out

    def summary(self) -> list[str]:
        """A screenful: the findings, then how many requests got an answer."""
        asked = [s for s in self.steps if not s.command.startswith("AT")]
        got = sum(1 for s in asked if s.positive)
        out = list(self.findings) or ["Nothing new answered."]
        out += ["", f"{len(asked)} requests sent, {got} answered."]
        if self.error:
            out += ["", f"Stopped early: {self.error}"]
        return out


def can_probe(b) -> str:
    """'' if this connection can be probed, else why not."""
    if not isinstance(b, ElmBackend):
        return "The deep probe needs a Bluetooth OBD adapter (ELM327-style)."
    if b.j1939:
        return "The deep probe is for K-line vehicles: this connection is J1939."
    if b.protocol and b.protocol not in NONCAN_PROTOCOLS:
        return f"This vehicle answered on {b.protocol_name}: the deep probe is for K-line ones (ISO 9141-2, KWP2000)."
    return ""


def engine_running(b) -> bool:
    """Is the battery being charged (the adapter's voltage reading)? Unknown counts as no."""
    try:
        return float((b.read_voltage() or "").upper().rstrip("V")) >= RUNNING_VOLTS
    except (ValueError, OSError):
        return False


def message_body(line: str, kwp: bool) -> bytes | None:
    """One reply line with headers on -> the message without header or check byte. None if it isn't
    a message (or its check byte is wrong). KWP headers are 3 bytes, or 4 with a separate length."""
    s = line.replace(" ", "").upper()
    if len(s) < 10 or len(s) % 2 or any(c not in "0123456789ABCDEF" for c in s):
        return None
    raw = bytes.fromhex(s)
    if sum(raw[:-1]) & 0xFF != raw[-1]:
        return None
    head = 4 if kwp and raw[0] & 0x80 and not raw[0] & 0x3F else 3
    return raw[head:-1] or None


def _hex(data: bytes) -> str:
    return " ".join(f"{x:02X}" for x in data)


def _read(request: bytes, bodies: list, lines: list) -> tuple[str, bool]:
    """How one request's reply reads, and whether it's a positive answer."""
    sid = request[0]
    positive = [d for d in bodies if d[0] == sid + 0x40]
    if positive:
        return "answered: " + "  |  ".join(_hex(d) for d in positive), True
    negative = [d for d in bodies if d[0] == 0x7F and len(d) >= 3]
    if negative:
        code = negative[-1][2]
        return f"refused: {NRC.get(code, 'code')} (7F {negative[-1][1]:02X} {code:02X})", False
    if bodies:
        return "answered something else: " + "  |  ".join(_hex(d) for d in bodies), False
    msg = classify(lines)
    if msg:
        return msg.lower(), False
    text = " ".join(lines).upper()
    if "ERROR" in text:
        return "wake-up failed", False
    return ("unreadable: " + " / ".join(lines)) if lines else "no answer", False


class _Probe:
    def __init__(self, b: ElmBackend, progress, settle: float = SETTLE, full: bool = False):
        self.b = b
        self.full = full
        self.settle = settle
        self.say = progress or (lambda _t: None)
        self.report = Report(adapter=b.stn or b.version or "ELM327", started=time.time(),
                             connected_protocol=b.protocol)
        self.section = ""
        self.kwp = False

    # --- one exchange ---
    def at(self, cmd: str, what: str = "", timeout: float = 3.0) -> Step:
        t0 = time.time()
        lines = self.b.command(cmd, timeout, keep_progress=True)
        ok = bool(lines) and classify(lines) != "?"
        step = Step(self.section, cmd, what or "adapter setting", lines, time.time() - t0,
                    " ".join(lines) if lines else "no answer", positive=ok)
        self.report.steps.append(step)
        return step

    def ask(self, cmd: str, what: str, keep: bool = True) -> Step:
        t0 = time.time()
        lines = self.b.command(cmd, REQUEST_TIMEOUT, keep_progress=True)
        bodies = [d for d in (message_body(line, self.kwp) for line in lines) if d]
        result, positive = _read(bytes.fromhex(cmd), bodies, lines)
        step = Step(self.section, cmd, what, lines, time.time() - t0, result, bodies, positive)
        if keep:
            self.report.steps.append(step)
        return step

    def close_protocol(self):
        self.at("ATPC", f"close the protocol, then {self.settle:g} s of silence")
        if self.settle > 0:
            time.sleep(self.settle)

    def find(self, text: str):
        self.report.findings.append(text)

    # --- the parts ---
    def generic(self):
        name = PROTOCOLS.get(self.b.protocol, "the connected protocol")
        self.section = f"Generic OBD-II, asked anyway ({name})"
        self.kwp = self.b.protocol in ("4", "5")
        self.say("Generic OBD-II requests")
        answered = []
        for cmd, what in GENERIC:
            step = self.ask(cmd, what)
            if step.positive:
                answered.append(step)
                self._note_generic(step)
        hidden = [s for s in answered if s.command not in ("0100", "03")]
        if hidden:
            self.find("Generic OBD-II answers beyond the supported list and stored codes: "
                      + ", ".join(f"{s.command} ({s.what})" for s in hidden))
        else:
            self.find("Generic OBD-II: nothing answered beyond the supported list and the stored-code read.")

    def _note_generic(self, step: Step):
        d = step.data[0]
        if step.command == "0101" and len(d) >= 3:
            self.find(f"Lamp status (0101): lamp {'ON' if d[2] & 0x80 else 'off'}, {d[2] & 0x7F} code(s) counted")
        if step.command in ("03", "07"):
            codes = []
            for body in step.data:
                pairs = [body[i:i + 2] for i in range(1, len(body) - 1, 2)]
                codes += [decode_dtc(p[0], p[1]) for p in pairs if p != b"\x00\x00"]
            kind = "stored" if step.command == "03" else "pending"
            self.find(f"Generic {kind} codes: {', '.join(codes) if codes else 'none'}")

    def find_ecu(self):
        """The address that answers OBD-II: the third header byte of a reply to 0100."""
        for s in self.report.steps:
            if s.command == "0100" and s.positive:
                for line in s.lines:
                    raw = line.replace(" ", "")
                    if message_body(line, self.kwp) and len(raw) >= 6:
                        self.report.ecu = int(raw[4:6], 16)
                        return

    def wakeups(self) -> list[str]:
        """Each K-line protocol with the standard request. Returns the ones that answered."""
        self.section = "Each K-line wake-up, standard request"
        works = []
        for proto in ("3", "4", "5"):
            self.say(f"Trying {PROTOCOLS[proto]}")
            self.close_protocol()
            self.at(f"ATSP{proto}", PROTOCOLS[proto])
            self.kwp = proto in ("4", "5")
            if self.ask("0100", "supported live values 01-20").positive:
                works.append(proto)
                self.at("ATKW", "the key bytes it sent")
        self.find("Standard request answered on: " + (", ".join(PROTOCOLS[p] for p in works) or "none"))
        return works

    def addressed(self):
        """KWP2000 with the module's own address in the header: fast init, then 5-baud init."""
        ecu = self.report.ecu
        self.kwp = True
        for proto, init, how in (("5", "ATFI", "fast init"), ("4", "ATSI", "5-baud init")):
            for tester in TESTERS:
                self.section = f"KWP2000 to module 0x{ecu:02X} from tester 0x{tester:02X}, {how}"
                self.say(self.section)
                self.close_protocol()
                self.at(f"ATSP{proto}", PROTOCOLS[proto])
                self.at(f"ATSH81{ecu:02X}{tester:02X}", "header: to the module, from the tester")
                if proto == "4":
                    self.at(f"ATIIA{ecu:02X}", "wake-up address")
                woke = self.at(init, how, REQUEST_TIMEOUT)
                woke.result = INIT_OK if woke.positive and "ERROR" not in " ".join(woke.lines).upper() \
                    else INIT_FAILED
                if woke.result != INIT_OK and classify(woke.lines) != "?":
                    continue                        # nothing there: its requests would each time out
                self.at("ATKW", "the key bytes it sent")
                self._kwp_reads(f"module 0x{ecu:02X}, tester 0x{tester:02X}, {how}")
        self.at("ATPC", "close the protocol")
        self.at("ATIIA33", "wake-up address back to the OBD one")

    def _kwp_reads(self, where: str):
        steps = {}
        for cmd, what, _again in KWP_READS:
            steps[cmd] = self.ask(cmd, what)
            if steps[cmd].result in ("wake-up failed", "unable to connect"):
                self.find(f"KWP2000 ({where}): no wake-up")
                return
        refused = [(c, w) for c, w, again in KWP_READS if again and not steps[c].positive and steps[c].data]
        if refused and self.ask(*DEFAULT_SESSION).positive:
            for cmd, what in refused:
                steps[cmd] = self.ask(cmd, what + " (in the default session)")
        got = [c for c, s in steps.items() if s.positive]
        heard = any(s.data for s in steps.values())
        if got:
            self.find(f"KWP2000 ({where}) answered: " + ", ".join(got))
        elif heard:
            self.find(f"KWP2000 ({where}): the module replied, but refused every read")
        else:
            self.find(f"KWP2000 ({where}): no reply")
        for cmd in ("1800FF00", "1802FF00", "1802FFFF", "13"):
            s = steps.get(cmd)
            if s is not None and s.positive:
                self.find(f"Codes by {cmd} ({where}): {_hex(s.data[0])}")
        blocks = [steps[c] for c in ("2101", "2100") if c in steps]
        if any(s.positive or (s.data and s.data[0][0] == 0x7F and s.data[0][2] != 0x11) for s in blocks):
            self.scan(where)

    def scan(self, where: str):
        """Every data-block number and identification option, then the common-identifier pages,
        in the session that's open. Only the ones that answered are listed one by one; refusals
        are counted by their code."""
        self.section = f"Scan of the read services ({where})"
        exists = set()
        for cmd, what in EXISTS:
            step = self.ask(cmd, what)
            if step.positive or (step.data and step.data[0][0] == 0x7F and step.data[0][2] != 0x11):
                exists.add(cmd[:2])
                self.find(f"Service {cmd[:2]} ({what}) exists ({where}): {step.result}")
        ending = self.shapes(where)
        if not self.full:
            self.find("Quick run: the full scans of block and identifier numbers were left out.")
            return
        for sid, numbers, what in SCANS:
            self.say(f"Scanning every {what} ({sid:02X} {numbers[0]:02X}-{numbers[-1]:02X})")
            end = ending.get(sid, "")
            answered = self._sweep([f"{sid:02X}{n:02X}{end}" for n in numbers], what, f"{sid:02X}xx{end}")
            if answered is None:
                return
            self.find(f"{what.capitalize()}s that answer ({sid:02X} xx, {where}): "
                      + (", ".join(f"{s.command[2:4]} ({len(s.data[0]) - 2} bytes)" for s in answered) or "none"))
        if "22" not in exists:
            return
        self.say("Sampling every common-identifier page (22 xx 00 / 01)")
        end = ending.get(0x22, "")
        sample = self._sweep([f"22{hi:02X}{lo:02X}{end}" for hi in range(256) for lo in (0, 1)],
                             "common identifier (sample)", "22xx00/01")
        if sample is None:
            return
        others = sorted({int(s.command[2:4], 16) for s in sample} - set(CID_PAGES))
        if others:
            self.find("Common-identifier pages that answer but are not swept (" + where + "): "
                      + " ".join(f"{hi:02X}" for hi in others))
        for hi in CID_PAGES:
            self.say(f"Scanning common identifiers 22 {hi:02X} 00-FF")
            answered = self._sweep([f"22{hi:02X}{lo:02X}{end}" for lo in range(256)], "common identifier",
                                   f"22{hi:02X}xx{end}")
            if answered is None:
                return
            sizes = sorted({len(s.data[0]) - 3 for s in answered})
            self.find(f"Common identifiers that answer (22 {hi:02X} xx, {where}): "
                      + (f"{len(answered)} ({', '.join(str(n) for n in sizes)} data bytes): "
                         + " ".join(s.command[4:6] for s in answered) if answered else "none"))

    def shapes(self, where: str) -> dict:
        """Try the request shapes (see SHAPES). Returns {service: the ending that got an answer}."""
        self.say("Trying request shapes")
        plain = {}                      # service -> how the bare request was refused
        ending = {}
        for sid, numbers, endings in SHAPES:
            for number in numbers:
                for end in ("",) + endings:
                    step = self.ask(f"{sid:02X}{number}{end}", "shape: number" + (f" + {end}" if end else " alone"))
                    code = step.data[0][2] if step.data and step.data[0][0] == 0x7F and len(step.data[0]) >= 3 else None
                    if not end:
                        plain[(sid, number)] = code
                    elif step.positive or (step.data and code != plain.get((sid, number))):
                        self.find(f"Service {sid:02X} reacts to the shape '{sid:02X} {number} {end}' ({where}): "
                                  f"{step.result}")
                        if step.positive:
                            ending.setdefault(sid, end)
        for cmd in FREEZE_SHAPES:
            step = self.ask(cmd, "shape: freeze frame read")
            if step.positive or (step.data and step.data[0][0] == 0x7F and step.data[0][2] != 0x12):
                self.find(f"Service 12 reacts to the shape '{cmd}' ({where}): {step.result}")
        if not ending:
            self.find(f"No request shape changed the refusals ({where}).")
        return ending

    def _sweep(self, commands: list, what: str, label: str) -> list | None:
        """Ask each command; keep the steps that answered, count the refusals. None if the module
        went quiet (the session is gone: the scan stops)."""
        answered, refused, silent, tried = [], {}, 0, 0
        for cmd in commands:
            tried += 1
            step = self.ask(cmd, f"read {what} {cmd[2:]}", keep=False)
            if step.positive:
                self.report.steps.append(step)
                answered.append(step)
            else:
                refused[step.result] = refused.get(step.result, 0) + 1
            silent = 0 if step.data else silent + 1
            if silent >= SCAN_GIVE_UP:
                self.find(f"The scan of {label} stopped at {cmd}: the module went quiet ({step.result})")
                break
        counts = "; ".join(f"{k} x{v}" for k, v in sorted(refused.items(), key=lambda kv: -kv[1]))
        self.report.steps.append(Step(self.section, label, f"every {what}: {tried} tried", [], 0.0,
                                      f"{len(answered)} answered" + (f"; {counts}" if counts else "")))
        return None if silent >= SCAN_GIVE_UP else answered


def run(b: ElmBackend, progress=None, settle: float | None = None, full: bool = False) -> Report:
    """Probe an opened K-line ``ElmBackend`` (see the module text). Holds the adapter throughout
    and sets it up again at the end. ``settle`` is the silence left before each new wake-up; ``full`` adds the
    scans of every block / identifier number (minutes more).
    Raises ValueError if the connection can't be probed."""
    why = can_probe(b)
    if why:
        raise ValueError(why)
    p = _Probe(b, progress, SETTLE if settle is None else settle, full)
    with b._lock:
        try:
            if full and engine_running(b):
                p.full = False
                p.find(f"The full scan was left out: the engine looks to be running ({b.voltage} at the OBD "
                       "port). It only runs with the engine off and the key on.")
            if b.protocol:
                p.generic()
                p.find_ecu()
            else:
                p.find("No vehicle had answered when the probe started: only the wake-ups were tried.")
            p.wakeups()
            p.addressed()
        except OSError as e:
            p.report.error = str(e) or "connection to the adapter lost"
        finally:
            if b.is_open:
                p.say("Setting the adapter up again")
                try:
                    b._init()
                except OSError as e:
                    p.report.error = p.report.error or f"couldn't set the adapter up again: {e}"
    return p.report
