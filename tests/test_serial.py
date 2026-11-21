"""Serial lines: framing, the reader threads, serial.log, captures and the protocol analysis.

The protocol in these tests is made up (a header byte, a command, a length, data, a two's-
complement sum): it exercises the analysis, it isn't any real controller's.
"""

import os
import pathlib
import queue
import time
import zipfile

import pytest

from canopener import cli
from canopener.core import capture_readme, logic, serial_analysis
from canopener.core.capture import SERIAL_LOG, Capture
from canopener.serialline import framing as fr
from canopener.serialline import log as slog
from canopener.serialline import port as sp
from canopener.serialline.port import Chunk

T0 = 1_700_000_000.0


# ----------------------------------------------------------------------
# helpers: a made-up request / response protocol
# ----------------------------------------------------------------------
def _frame(cmd: int, data: bytes = b"") -> bytes:
    body = bytes([0x02, cmd, len(data)]) + data
    return body + bytes([-sum(body) & 0xFF])


def _conversation(n: int = 30, t0: float = T0, value_at=None) -> list[Chunk]:
    """The console asks 0x10 (status) and 0x20 (a menu line); the controller answers. The status
    reply's byte 3 holds a value (``value_at(t)``); replies arrive split over two chunks."""
    out, t = [], t0
    for i in range(n):
        if i % 2 == 0:
            out.append(Chunk(t, "console", _frame(0x10)))
            v = value_at(t) if value_at else 40
            reply = _frame(0x90, bytes([v, i & 0xFF]))
        else:
            out.append(Chunk(t, "console", _frame(0x20, bytes([i % 3]))))
            reply = _frame(0xA0, b"BATTERY " + str(i % 3).encode())
        out.append(Chunk(t + 0.020, "controller", reply[:3]))
        out.append(Chunk(t + 0.022, "controller", reply[3:]))
        t += 0.1
    return out


class FakePort:
    """pyserial's read / in_waiting / write / close, fed from a queue."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.written = b""
        self.closed = False

    @property
    def in_waiting(self):
        return 0

    def read(self, n):
        try:
            return self.q.get(timeout=0.02)
        except queue.Empty:
            return b""

    def write(self, data):
        self.written += data
        return len(data)

    def close(self):
        self.closed = True


def _wait(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ----------------------------------------------------------------------
# framing
# ----------------------------------------------------------------------
def test_framing_parses_the_usual_short_forms():
    assert fr.Framing.parse("9600 8N1") == fr.Framing(9600, 8, "N", 1)
    assert fr.Framing.parse("4800,7e2") == fr.Framing(4800, 7, "E", 2)
    assert fr.Framing.parse("19200") == fr.Framing(19200)
    assert str(fr.Framing.parse("1200/8O1.5")) == "1200 8O1.5"
    assert str(fr.Framing(9600)) == "9600 8N1"
    assert fr.Framing(9600, 7, "E", 1).bits_per_char == 10
    assert fr.Framing(1000).char_time == pytest.approx(0.010)
    for bad in ("", "fast", "9600 9N1", "9600 8X1", "9600 8N3", "-5"):
        with pytest.raises(ValueError):
            fr.Framing.parse(bad)


def test_baud_comes_from_the_narrowest_pulses_and_snaps_to_a_standard_rate():
    bit = 1 / 9600
    widths = [bit * k * (1 + 0.01 * ((i % 5) - 2)) for i, k in enumerate([1, 2, 1, 3, 1, 1, 4, 2, 1, 5] * 10)]
    rate, std = fr.guess_baud(widths + [bit / 20])          # one glitch, far narrower, is ignored
    assert std == 9600 and rate == pytest.approx(9600, rel=0.03)
    assert fr.guess_baud([1e-3, 1.37e-3, 2.71e-3, 3.3e-3, 4.9e-3] * 5) == (None, None)    # no common bit
    assert fr.guess_baud([bit] * 3) == (None, None)        # too few pulses
    assert fr.snap_baud(9300) == 9600 and fr.snap_baud(7000) is None


def test_pulse_widths_read_a_sigrok_capture(tmp_path):
    # D3 toggles every 4 samples at 1 MHz on bit 1 of each sample
    data = bytes(((i // 4) % 2) << 1 for i in range(400))
    meta = ("[global]\nsigrok version=0.5.2\n[device 1]\ncapturefile=logic-1\ntotal probes=2\n"
            "samplerate=1 MHz\nunitsize=1\nprobe1=D2\nprobe2=D3\n")
    path = tmp_path / "x.sr"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("metadata", meta)
        z.writestr("logic-1-1", data)
    widths = logic.pulse_widths(str(path), "D3")
    assert widths and all(w == pytest.approx(4e-6) for w in widths)
    with pytest.raises(ValueError):
        logic.pulse_widths(str(path), "D9")


# ----------------------------------------------------------------------
# taps and the log
# ----------------------------------------------------------------------
def test_tap_stamps_chunks_and_writing_needs_permission():
    port = FakePort()
    got = []
    tap = sp.Tap("A", port, on_chunk=got.append)
    tap.start()
    port.q.put(b"\x02\x10")
    assert _wait(lambda: got)
    assert got[0].line == "A" and got[0].data == b"\x02\x10" and abs(got[0].t - time.time()) < 2
    with pytest.raises(sp.SerialError):
        tap.write(b"\x01")
    tap.close()
    assert port.closed and not tap.running
    w = sp.Tap("B", FakePort(), allow_write=True)
    assert w.write(b"\x01\x02") == 2 and w.port.written == b"\x01\x02"


def test_a_failing_port_stops_its_tap_with_the_error():
    class Broken(FakePort):
        def read(self, n):
            raise OSError("device unplugged")
    s = sp.Sniffer([sp.Tap("A", Broken(), device="/dev/ttyUSB0")])
    s.start()
    assert _wait(lambda: s.errors())
    assert "unplugged" in s.errors()[0] and "/dev/ttyUSB0" in s.errors()[0]
    s.close()


def test_pyserial_loopback_round_trip():
    pytest.importorskip("serial")
    tap = sp.Tap.open("A", "loop://", fr.Framing(9600), allow_write=True)
    got = []
    tap.on_chunk = got.append
    tap.start()
    tap.write(b"\x02\x10\x00\xEE")
    assert _wait(lambda: b"".join(c.data for c in got) == b"\x02\x10\x00\xEE")
    tap.close()
    with pytest.raises(sp.SerialError):
        sp.open_port("/dev/does-not-exist-canopener", fr.Framing(9600))


def test_serial_log_round_trip_and_trim(tmp_path):
    path = str(tmp_path / SERIAL_LOG)
    s = sp.Sniffer([sp.Tap("console", FakePort(), device="/dev/ttyUSB0", framing=fr.Framing(4800), label="tx")])
    log = slog.SerialLog(path, s)
    chunks = _conversation(4)
    for c in chunks:
        s._chunk(c)
    assert log.close() == sum(len(c.data) for c in chunks)
    lines, back = slog.read_serial_log(path)
    assert lines == [{"line": "console", "port": "/dev/ttyUSB0", "framing": "4800 8N1", "label": "tx"}]
    assert [(round(c.t, 6), c.line, c.data) for c in back] == [(round(c.t, 6), c.line, c.data) for c in chunks]
    assert slog.parse_chunk("# comment") is None and slog.parse_chunk("(x) A 01") is None
    kept, dropped = slog.trim_serial_log(path, T0 + 0.15, T0 + 1)
    _, left = slog.read_serial_log(path)
    assert all(c.t >= T0 + 0.15 for c in left) and kept + dropped == log.bytes and dropped > 0


# ----------------------------------------------------------------------
# the analysis
# ----------------------------------------------------------------------
def test_chunks_join_into_messages_by_gaps_and_turns():
    msgs = serial_analysis.split_messages(_conversation(4))
    assert [m.line for m in msgs] == ["console", "controller"] * 4
    assert msgs[1].data == _frame(0x90, bytes([40, 0])) and msgs[1].chunks == 2
    # without turns, a reply that comes too soon doesn't end anything; a long gap still does
    one = [Chunk(T0, "A", b"\x01"), Chunk(T0 + 0.001, "A", b"\x02"), Chunk(T0 + 0.5, "A", b"\x03")]
    assert [m.data for m in serial_analysis.split_messages(one)] == [b"\x01\x02", b"\x03"]


def test_crc_catalogue_matches_the_standard_check_values():
    check = {"CRC-8": 0xF4, "CRC-8/MAXIM": 0xA1, "CRC-8/SAE-J1850": 0x4B, "CRC-16/ARC": 0xBB3D,
             "CRC-16/MODBUS": 0x4B37, "CRC-16/CCITT-FALSE": 0x29B1, "CRC-16/XMODEM": 0x31C3,
             "CRC-16/KERMIT": 0x2189}
    for name, want in check.items():
        assert serial_analysis.CHECKSUMS[name][1](b"123456789") == want, name


def test_the_checksum_is_found():
    msgs = serial_analysis.split_messages(_conversation(30))
    fits = serial_analysis.find_checksums(msgs)
    assert fits and fits[0].name.startswith("sum8, two's complement") and fits[0].skip == 0
    assert fits[0].share == 1.0 and set(fits[0].lines) == {"console", "controller"}
    # a CRC-16/MODBUS at the end, little-endian, is told apart from the rest
    crc = serial_analysis.CHECKSUMS["CRC-16/MODBUS"][1]
    modbus = [serial_analysis.Message("A", T0 + i, T0 + i, (b := bytes([1, 3, i, 7, i * 3 & 0xFF]))
                                      + crc(b).to_bytes(2, "little")) for i in range(10)]
    best = serial_analysis.find_checksums(modbus)[0]
    assert (best.name, best.order) == ("CRC-16/MODBUS", "little-endian")
    assert serial_analysis.find_checksums(modbus[:3]) == []    # too few to say


def test_exchanges_text_and_byte_behaviour():
    r = serial_analysis.analyze(_conversation(30))
    assert r.master == "console"
    top = {e.ask: e for e in r.exchanges}
    assert top[_frame(0x10)].count == 15 and 15 < top[_frame(0x10)].latency * 1000 < 30
    assert "BATTERY 1" in r.text and not r.hint
    status = next(k for k in r.kinds if k.key == ("controller", 6, "02"))
    roles = {i: how for i, how, _ in status.positions}
    assert roles[0] == "constant" and roles[1] == "constant" and roles[4] == "counter"
    text = "\n".join(r.lines())
    assert "Line console asks most often" in text and "sum8, two's complement" in text
    assert r.to_dict()["master"] == "console"


def test_text_with_parity_shows_up_when_the_top_bit_is_cleared():
    noisy = bytes(b | 0x80 if i % 2 else b for i, b in enumerate(b"LIFT LOCKED"))
    msgs = [serial_analysis.Message("A", T0 + i, T0 + i, noisy) for i in range(5)]
    assert not serial_analysis.text_runs(msgs)
    assert serial_analysis.text_runs(msgs, mask7=True)["LIFT LOCKED"] == 5
    r = serial_analysis.analyze([Chunk(m.t, m.line, m.data) for m in msgs])
    assert "7 data bits plus parity" in "\n".join(r.lines())


def test_wrong_baud_looks_like_junk():
    junk = [Chunk(T0 + i * 0.1, "A", bytes([0x00, 0xFF, 0x80, 0xF0, 0xFE, 0x00, 0xFF, 0xE0])) for i in range(8)]
    assert "wrong baud rate" in serial_analysis.framing_hint(junk)
    assert serial_analysis.framing_hint(_conversation(10)) == ""


def test_phases_show_the_byte_that_follows_what_was_done():
    chunks = _conversation(40, value_at=lambda t: 40 if t < T0 + 2 else 90)
    r = serial_analysis.analyze(chunks, markers=[(T0 + 2, "1 - throttle up")])
    assert [pk.phase.label for pk in r.phases] == ["Before the first timestamp", "1 - throttle up"]
    moved = r.phases[1].moved
    assert (("controller", 6, "02"), 3, 40, 90) in moved
    assert "byte 3: 28 -> 5A" in "\n".join(r.lines())


def test_an_empty_recording_still_reports():
    r = serial_analysis.analyze([])
    assert r.messages == [] and "None of the usual" in "\n".join(r.lines())


# ----------------------------------------------------------------------
# captures and the command line
# ----------------------------------------------------------------------
def _record(tmp_path, chunks) -> Capture:
    ports = {"console": FakePort(), "controller": FakePort()}
    s = sp.Sniffer([sp.Tap(n, p, device=f"/dev/fake-{n}", framing=fr.Framing(9600)) for n, p in ports.items()])
    c = Capture("Console sniff", plan=["connect", "tester"])
    c.start(None, root=str(tmp_path), serial=s)
    c.started = T0 - 1                       # the recording's clock: the made-up chunks start at T0
    for ch in chunks:
        s._chunk(ch)
    c.tag(0, T0 + 0.05)
    c.tag(1, T0 + 1.0)
    c.stop()
    c.stopped = T0 + 5
    s.close()
    return c


def test_a_capture_records_serial_lines_and_explains_them(tmp_path):
    chunks = _conversation(20)
    c = _record(tmp_path, chunks)
    assert c.serial_bytes == sum(len(x.data) for x in chunks)
    assert c.meta["serial"]["lines"][0] == {"line": "console", "port": "/dev/fake-console", "framing": "9600 8N1"}
    folder = c.save()
    files = set(os.listdir(folder))
    assert {SERIAL_LOG, serial_analysis.SERIAL_ANALYSIS_TXT, "summary.txt", "README.md"} <= files
    summary = pathlib.Path(folder, "summary.txt").read_text()
    assert "-- Serial lines" in summary and "sum8, two's complement" in summary
    assert "Nothing was recorded from the bus" not in summary
    readme = pathlib.Path(folder, "README.md").read_text()
    assert "`serial.log`" in readme and "serial bytes" in readme and "Serial line console" in readme
    assert "Nothing answered" not in readme
    back = Capture.load(folder)
    assert back.serial_bytes == c.serial_bytes
    r = serial_analysis.analyze_capture(folder)
    assert len(r.phases) >= 2 and r.master == "console"
    assert capture_readme.FILES[SERIAL_LOG]


def test_trimming_a_capture_trims_its_serial_log(tmp_path):
    c = _record(tmp_path, _conversation(40))
    c.save()
    before = c.serial_bytes
    c.trim(before=0.0, after=0.5, keep_full=True)
    assert 0 < c.serial_bytes < before
    assert os.path.exists(os.path.join(c.folder, "serial_full.log"))


def test_nothing_to_record_is_refused():
    with pytest.raises(ValueError):
        Capture().start(None)


def test_cli_analyze_and_a_short_sniff(tmp_path, capsys):
    c = _record(tmp_path, _conversation(10))
    folder = c.save()
    out_json = tmp_path / "r.json"
    assert cli.main(["serial", "analyze", folder, "--json", str(out_json)]) == 0
    out = capsys.readouterr().out
    assert "Message kinds" in out and out_json.exists()
    assert cli.main(["serial", "analyze"]) == 2
    assert cli.main(["serial", "sniff"]) == 2
    assert cli._serial_lines(["console=/dev/ttyUSB0", "/dev/ttyUSB1", "loop://"]) == [
        ("console", "/dev/ttyUSB0"), ("B", "/dev/ttyUSB1"), ("C", "loop://")]
    if sp.available():
        assert cli.main(["serial", "sniff", "--line", "A=loop://", "--seconds", "0.2",
                         "--dir", str(tmp_path / "sniffs")]) == 0
        assert "bytes saved" in capsys.readouterr().err


def test_serialline_stays_below_the_app_layers():
    root = pathlib.Path(sp.__file__).parent
    src = "\n".join(p.read_text() for p in root.glob("*.py"))
    for banned in ("from ..core", "from ..gui", "from ..sim", "from ..device", "import canopener.core"):
        assert banned not in src, f"serialline/ should not reference app layers: {banned}"
    assert "\nimport serial" not in src     # pyserial only inside functions, when a port is used
