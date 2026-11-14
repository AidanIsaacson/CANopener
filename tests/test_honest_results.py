"""Results that say what really happened: a frame the adapter couldn't send isn't logged as
traffic, a capture in which nothing answered says so, and a module that answers OBD-II as a
formality gets a warning beside its 'no codes'."""

from canopener.can.frame import CanFrame
from canopener.core import capture_report, connection, scan
from canopener.core.capture import Capture
from canopener.core.obd import THIN_NOTE, ObdClient


def _refuse_requests(backend, monkeypatch, message="CAN ERROR"):
    """Make the simulated adapter answer every request to the vehicle with ``message``."""
    real = backend.command
    sent = []

    def command(cmd, *a, **k):
        if not cmd.upper().startswith(("AT", "ST")):
            sent.append(cmd)
            return [message]
        return real(cmd, *a, **k)
    monkeypatch.setattr(backend, "command", command)
    return sent


def test_a_request_the_adapter_could_not_send_is_not_logged_and_stops_the_scan(monkeypatch):
    conn = connection.open_connection(connection.ELM_SIM, "sim:j1939")
    try:
        s = conn.session
        sent = _refuse_requests(s.backend, monkeypatch)
        before = s.tx_count
        assert not s.request(0xFECA)
        assert "CAN ERROR" in s.health.tx_error and "acknowledged" in s.health.tx_error
        assert s.tx_count == before                             # not counted, not logged as sent
        sent.clear()
        result = scan.run_preset(s, "active", sleep=lambda _t: None)
        assert len(sent) == 1                                   # the first failed: the rest weren't tried
        assert result.tx_error and "CAN ERROR" in result.tx_error
    finally:
        conn.close()


def test_own_requests_are_told_from_what_the_vehicle_said():
    own = capture_report.own_request
    assert own(0x18EAFFF9, True) and own(0x7DF, False) and own(0x7E0, False) and own(0x18DB33F1, True)
    assert not own(0x18FECA00, True) and not own(0x7E8, False) and not own(0x18DAF110, True)


def _analysis(frames):
    a = capture_report._Analysis(Capture(), "metric")
    a.t0 = frames[0].timestamp if frames else 0.0
    for fr in frames:
        a.feed(fr)
    return a


def test_summary_says_when_nothing_answered():
    frames = [CanFrame(0x18EAFFF9, bytes.fromhex("CAFE00"), True, 100.0 + i) for i in range(3)]
    a = _analysis(frames)
    text = capture_report.summary_text(a)
    assert "!! Nothing answered: all 3 frames in this log are this tool's own requests." in text
    assert "none heard" in text
    bus = capture_report.bus_summary(a, [])
    assert bus["heard_frames"] == 0 and bus["quiet"][0].startswith("Nothing answered")
    assert capture_report.nothing_heard_lines(0, 0, [(1.0, "OBD adapter: CAN ERROR")]) == [
        "Nothing was recorded from the bus: no frames at all.",
        "The adapter reported trouble 1 time(s); first: OBD adapter: CAN ERROR."]
    assert capture_report.nothing_heard_lines(5, 2, []) == []


def test_summary_names_a_module_that_answered_with_no_codes_and_flags_a_thin_one():
    frames = [CanFrame(0x7DF, bytes.fromhex("0201005555555555"), False, 100.0),
              CanFrame(0x7E8, bytes.fromhex("0641000000000000"), False, 100.4),
              CanFrame(0x7DF, bytes.fromhex("0103555555555555"), False, 101.0),
              CanFrame(0x7E8, bytes.fromhex("0243000000000000"), False, 101.4)]
    a = _analysis(frames)
    text = capture_report.summary_text(a)
    assert a.heard == 2 and "Nothing answered" not in text
    assert "answered: no codes" in text
    assert "lists no live data at all" in text and capture_report.bus_summary(a, [])["thin"]
    normal = _analysis([CanFrame(0x7E8, bytes.fromhex("064100BE3FA81300"), False, 100.0)])
    assert not normal.thin


def test_thin_module_is_recognised_and_a_normal_car_is_not():
    conn = connection.open_connection(connection.ELM_SIM, "sim:thin-kline")
    try:
        c = ObdClient(conn.session)
        by_ecu = c.read_dtcs_by_ecu()
        assert list(by_ecu) == [0x7E8] and not any(by_ecu[0x7E8].values())    # the shell: no codes shown
        assert c.thin_ecus() == [0x7E8] and "deep probe" in THIN_NOTE
        c.close()
    finally:
        conn.close()
    conn = connection.open_connection(connection.ELM_SIM, "sim:iso9141")
    try:
        c = ObdClient(conn.session)
        assert c.thin_ecus() == []
        c.close()
    finally:
        conn.close()


def test_installed_plugins_are_left_out_of_the_tests(monkeypatch):
    from canopener import plugins
    assert plugins._targets() == []                         # conftest sets CANOPENER_NO_INSTALLED_PLUGINS
    monkeypatch.setenv("CANOPENER_PLUGINS", "demo_plugin")
    assert plugins._targets() == [("demo_plugin", "demo_plugin")]
