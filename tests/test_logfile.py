import time

from canopener.can.frame import CanFrame
from canopener.can.logfile import LogReplayBackend, parse_line, read_log, read_log_with_markers, write_log


def test_parse_candump_log_format():
    f = parse_line("(1700000000.123456) can0 18FECA00#0000FFFF00000000")
    assert f.can_id == 0x18FECA00 and f.extended and f.timestamp == 1700000000.123456
    assert f.data == bytes.fromhex("0000FFFF00000000")


def test_parse_standard_id_and_rtr():
    f = parse_line("(1.0) can0 123#DEAD")
    assert f.can_id == 0x123 and not f.extended and f.data == b"\xde\xad"
    assert parse_line("(1.0) can0 123#R") is None


def test_parse_screen_formats():
    f = parse_line("  can0  18FEEE00   [8]  47 43 D0 26 FF FF FF FF", 5)
    assert f.can_id == 0x18FEEE00 and f.dlc == 8 and f.timestamp == 0.005
    f = parse_line(" (1700000000.5)  can0  0CF00400   [3]  FF FF 7D")
    assert f.timestamp == 1700000000.5 and f.data == b"\xff\xff\x7d"
    assert parse_line("garbage line") is None


def test_write_read_round_trip(tmp_path):
    frames = [CanFrame(0x18FECA00, b"\x00\xff\x00\x00\x00\x00\xff\xff", True, 1.5),
              CanFrame(0x123, b"\x01", False, 2.0),
              CanFrame(0x20, b"", True, 2.1, error="bus-off")]
    path = tmp_path / "x.log"
    assert write_log(str(path), frames) == 2
    back = read_log(str(path))
    assert [(f.can_id, f.data, f.extended, f.timestamp) for f in back] == \
        [(f.can_id, f.data, f.extended, f.timestamp) for f in frames[:2]]


def test_marker_comment_lines_anchored_to_frames(tmp_path):
    # A field logger's .log: epoch-stamped frames plus wall-clock ISO comment lines.
    # END ISO (33.134745) anchors to the last frame; the marker (23.658860) is 9.48s before it.
    path = tmp_path / "field.log"
    path.write_text(
        "# === PRE-ROLL: last 10s before log start ===\n"
        "# === LIVE CAPTURE START: 2026-05-08T17:34:45.381308 ===\n"
        "(1778286885.381308) can0 18FEEE00#47FFFFFFFFFFFFFF\n"
        "# === MARKER 2026-05-08T17:35:23.658860 engine stop ===\n"
        "(1778286923.658860) can0 0CF00400#FFFF7D000000FFFF\n"
        "# === LIVE CAPTURE END: 2026-05-08T17:35:33.134745 (47.8s, 2 frames) ===\n"
        "(1778286933.134745) can0 18FEF100#00000000FFFFFFFF\n")
    frames, markers = read_log_with_markers(str(path))
    assert len(frames) == 3
    assert len(markers) == 1
    t, label = markers[0]
    assert label == "engine stop"
    assert abs(t - 1778286923.658860) < 0.01      # lands on the real frame epoch, tz-independent
    assert read_log(str(path)) == frames          # read_log still returns just the frames


def test_no_markers_without_comment_lines(tmp_path):
    path = tmp_path / "plain.log"
    write_log(str(path), [CanFrame(0x18FEEE00, b"\x01", True, 100.0)])
    frames, markers = read_log_with_markers(str(path))
    assert len(frames) == 1 and markers == []


def test_replay_backend_keeps_timing(tmp_path):
    path = tmp_path / "r.log"
    write_log(str(path), [CanFrame(0x18FEEE00, b"\x01", True, 100.0 + i * 0.05) for i in range(5)])
    b = LogReplayBackend(str(path), speed=1.0)
    b.open()
    t0 = time.time()
    got = []
    while len(got) < 5:
        fr = b.recv()
        if fr:
            got.append(fr)
    assert 0.15 < time.time() - t0 < 1.0
    assert b.recv() is None and b.finished
