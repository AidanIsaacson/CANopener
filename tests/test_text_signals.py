"""Text signals ("type": "text"): ASCII display messages in a profile, through every view."""

import pytest

from canopener.can.frame import CanFrame
from canopener.core import live, profiles, units
from canopener.j1939 import signals
from canopener.j1939.signals import decode_text

DOC = {"name": "Text test", "protocol": "canopen", "signals": [
    {"name": "Display text", "id": "0x370", "start": 0, "length": 8, "type": "text", "group": "Display"},
    {"name": "Battery", "id": "0x388", "start": 1, "length": 1, "unit": "%"}]}


def test_decode_text_trims_marks_odd_bytes_and_treats_blank_as_nothing():
    assert decode_text(b" 19%    ", 0, 8) == " 19%"
    assert decode_text(b"LOW BTRY", 0, 8) == "LOW BTRY"
    assert decode_text(b"M   185 ", 0, 8) == "M   185"
    assert decode_text(b"AB\x01C\x00\x00\x00\x00", 0, 8) == "AB.C"
    assert decode_text(b"        ", 0, 8) is None
    assert decode_text(b"\x00" * 8, 0, 8) is None
    assert decode_text(b"ABC", 0, 8) is None            # frame too short


def test_profile_parses_text_and_rejects_nonsense():
    p = profiles.parse_profile(DOC, "t")
    sig = p.signals[0]
    assert sig.is_text and sig.unit == "" and sig.length == 8
    assert sig.text("LOW BTRY") == "LOW BTRY" and not sig.out_of_range("LOW BTRY")
    buf = bytearray(8)
    sig.encode_into(buf, "HI")
    assert bytes(buf) == b"HI      " and sig.decode(buf) == "HI"
    for bad in ({"bits": 4}, {"scale": 0.1}, {"signed": True}, {"enum": {"0": "x"}}, {"length": 9},
                {"start": 4, "length": 8}, {"order": "big", "length": 2}):
        with pytest.raises(ValueError):
            profiles.parse_signal({"name": "x", "id": "0x370", "start": 0, "length": 8, "type": "text", **bad})
    with pytest.raises(ValueError):
        profiles.parse_signal({"name": "x", "id": "0x370", "start": 0, "type": "words"})
    assert not profiles.parse_signal({"name": "n", "id": "0x370", "start": 0, "type": "number"}).is_text


def test_units_pass_text_through():
    assert units.convert("LOW BTRY", "", units.IMPERIAL) == ("LOW BTRY", "")
    assert units.fmt("LOW BTRY", "", 1, units.IMPERIAL) == "LOW BTRY"


class FakeSession:
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.latest, self.latest_std = {}, {}


def test_live_row_keeps_last_message_through_blanks_and_recent_ones():
    p = profiles.parse_profile(DOC, "t")
    profiles.apply(p)
    try:
        model, s = live.LiveData(), FakeSession()
        seq = [b" 19%    ", b"        ", b"M   185 ", b"        ", b"LOW BTRY", b" 19%    ", b"        "]
        for i, d in enumerate(seq):
            s.latest_std[0x370] = (float(i + 1), d)
            s.latest_std[0x388] = (float(i + 1), bytes([0, 19, 0, 0, 0, 0, 0, 0]))
            model.sample(s)
        row = next(r for r in model.rows.values() if r.sig.name == "Display text")
        assert row.value == " 19%" and list(row.recent) == ["M   185", "LOW BTRY", " 19%"]
        assert row.lo is None and not row.points                 # no min / max / graph for text
        num = next(r for r in model.rows.values() if r.sig.name == "Battery")
        assert num.value == 19 and len(num.points) == 7
        model.reset_minmax()
        assert list(row.recent) == [" 19%"]
    finally:
        profiles.apply(None)


def test_capture_timeline_notes_each_message_once(tmp_path):
    from canopener.core import capture as cap
    from canopener.core import capture_report
    p = profiles.parse_profile(DOC, "t")
    profiles.apply(p)
    try:
        c = cap.Capture()
        from canopener.core.measure import MeasurementHub
        c.start(None, str(tmp_path), {}, measurements=MeasurementHub())
        frames = []
        for i, d in enumerate([b"XXXXXXXX", b"        ", b" 19%    ", b"LOW BTRY", b" 19%    ", b"LOW BTRY"]):
            frames.append(CanFrame(0x370, d, extended=False, timestamp=c.started + i))
        with open(c.log_path, "w") as f:
            from canopener.can.logfile import format_line
            f.writelines(format_line(fr) + "\n" for fr in frames)
        c.stop()
        a = capture_report.analyze(c)
        texts = [e for _, e in a.events if e.startswith("Display text")]
        assert texts == ['Display text: "XXXXXXXX"', 'Display text: " 19%"', 'Display text: "LOW BTRY"']
        capture_report.write_live_csv(a, str(tmp_path / "live.csv"))
        assert "LOW BTRY" in (tmp_path / "live.csv").read_text()
    finally:
        profiles.apply(None)


def test_profile_simulator_sends_text():
    import random

    from canopener.sim.profile_sim import _Value
    sig = profiles.parse_profile(DOC, "t").signals[0]
    g = _Value(sig, random.Random(1))
    vals = {g(t) for t in range(0, 30, 3)}
    assert vals and all(isinstance(v, str) and len(v) <= 8 for v in vals)
    buf = bytearray(8)
    sig.encode_into(buf, g(0))
    assert sig.decode(buf) in vals


def test_signals_without_type_are_unchanged():
    assert not any(s.is_text for s in signals.SIGNALS)


def _sim_zapi(seconds=3.5):
    """The Zapi profile on the profile simulator, sampled into a LiveData model."""
    import time

    from canopener.core.connection import PROFILE_SIM, open_connection
    p = profiles.find("zapi-combiacx")
    profiles.apply(p)
    conn = open_connection(PROFILE_SIM, profile=p, sim_seed=3)
    model = live.LiveData()
    end = time.time() + seconds
    while time.time() < end:
        model.sample(conn.session)
        time.sleep(0.1)
    return conn, model


def test_desktop_live_tab_shows_text_without_a_plot_box(monkeypatch):
    qt = pytest.importorskip("canopener.gui.qt")
    app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])
    from canopener.gui.tab_live import C_PLOT, C_VALUE, LiveTab
    conn, model = _sim_zapi()
    try:
        class Ctx(qt.QtCore.QObject):
            session_changed = qt.Signal(object)
            units_changed = qt.Signal(object)
            profile_changed = qt.Signal()
            show_signal = qt.Signal(object)
            status_message = qt.Signal(str)
            units, capture = "metric", None
        ctx = Ctx()
        ctx.session = conn.session
        tab = LiveTab(ctx, model=model)
        tab._rebuild_table()
        i = next(i for i, k in enumerate(tab.order) if tab.rows[k].sig.name == "Display text")
        cell = tab.table.item(i, C_VALUE)
        assert cell.text() in {"SIM", "TEXT", "12345678", "LOW BAT"} and "Recent messages" in cell.toolTip()
        assert not tab.table.item(i, C_PLOT).flags() & qt.Qt.ItemFlag.ItemIsUserCheckable
        app.processEvents()
    finally:
        conn.close()
        profiles.apply(None)


def test_handheld_tile_shows_text_and_other_recent_messages():
    from canopener.device.screens.live import fmt_value, trace_points
    conn, model = _sim_zapi(7)
    try:
        row = next(r for r in model.rows.values() if r.sig.name == "Display text")
        value, unit, others, bad = fmt_value(model, row, units.IMPERIAL)
        assert value == row.value and unit == "" and not bad and len(row.recent) >= 2 and others
        assert trace_points(row, units.IMPERIAL) == []
    finally:
        conn.close()
        profiles.apply(None)
