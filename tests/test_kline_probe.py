"""The K-line deep probe (can.kline_probe) against the simulated adapter: read-only, every reply kept."""

import pytest

from canopener.can import elm327, kline_probe
from canopener.sim.elm import SimulatedCar

WRITES = ("04", "08", "14", "11", "2E", "2F", "30", "31", "3B", "27", "34", "36")


def _probe(vehicle):
    car = SimulatedCar(vehicle=vehicle)
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket))
    b.open()
    try:
        start = len(car.adapter.commands)
        report = kline_probe.run(b, settle=0)
        return report, car.adapter.commands[start:], b.protocol
    finally:
        b.close()
        car.stop()


def test_message_body_checks_the_check_byte_and_kwp_headers():
    assert kline_probe.message_body("486B1041000000000004", False) == bytes.fromhex("410000000000")
    assert kline_probe.message_body("486B1041000000000005", False) is None           # wrong check byte
    assert kline_probe.message_body("82F1105800DB", True) == b"\x58\x00"
    assert kline_probe.message_body("80F110025800DB", True) == b"\x58\x00"            # separate length byte
    assert kline_probe.message_body("NO DATA", False) is None


def test_probe_only_reads_and_keeps_every_reply():
    report, sent, protocol = _probe("iso9141")
    requests = [c for c in sent if not c.startswith("AT")]
    assert requests and not [c for c in requests if c[:2] in WRITES]
    assert "0101" in requests and "03" in requests
    text = "\n".join(report.lines())
    assert "486B1041018207E50477" in text                       # the raw line, as the adapter printed it
    assert "Lamp status (0101): lamp ON, 2 code(s) counted" in report.findings
    assert "Generic stored codes: P0133, P0420, P0730" in report.findings
    assert any(f.startswith("Standard request answered on: ISO 9141-2") for f in report.findings)
    assert "BUS INIT: ...ERROR" in text                         # the failed KWP wake-ups show
    assert report.ecu == 0x10 and not report.error
    assert protocol == "3"                                      # set up again: the car answers as before
    assert report.summary()[-1].endswith("answered.")


def test_probe_finds_kwp_services_and_retries_in_the_default_session():
    report, sent, protocol = _probe("kwp")
    assert "ATSH8110F1" in sent and "ATSH8110FC" in sent and "ATFI" in sent
    by_cmd = {}
    for s in report.steps:
        by_cmd.setdefault(s.command, []).append(s)
    assert by_cmd["3E"][0].positive
    first, again = by_cmd["1A80"][0], by_cmd["1A80"][1]
    assert "not supported in this diagnostic session" in first.result and again.positive
    assert sent.index("1081") > sent.index("1A80")              # only after a read was refused
    assert any("KWP2000 (module 0x10, tester 0xF1, fast init) answered" in f and "1A80" in f for f in report.findings)
    assert any(f.startswith("Codes by 1800FF00") for f in report.findings)
    assert sent[-1] != "ATIIA10" and "ATIIA33" in sent and protocol == "5"


def test_probe_refuses_can_and_j1939_connections():
    car = SimulatedCar()
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket))
    b.open()
    try:
        assert "K-line" in kline_probe.can_probe(b)
        with pytest.raises(ValueError):
            kline_probe.run(b, settle=0)
    finally:
        b.close()
        car.stop()
    assert kline_probe.can_probe(object())


def test_trace_stamps_the_send_and_the_reply_separately(tmp_path, monkeypatch):
    path = tmp_path / "trace.txt"
    monkeypatch.setenv(elm327.TRACE_ENV, str(path))
    car = SimulatedCar(vehicle="iso9141")
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket))
    b.open()
    try:
        assert b.command("ATFI", keep_progress=True) == ["BUS INIT: ...ERROR"]
        assert b.command("ATFI") == []
    finally:
        b.close()
        car.stop()
    lines = path.read_text().splitlines()
    assert any(" > ATFI" in line for line in lines) and any("< BUS INIT: ...ERROR" in line for line in lines)
