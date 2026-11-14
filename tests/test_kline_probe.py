"""The K-line deep probe (can.kline_probe) against the simulated adapter: read-only, every reply kept."""

import pytest

from canopener.can import elm327, kline_probe
from canopener.sim.elm import SimulatedCar

WRITES = ("04", "08", "14", "11", "2E", "2F", "30", "31", "3B", "27", "34", "36")


def _probe(vehicle, full=True):
    car = SimulatedCar(vehicle=vehicle)
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket))
    b.open()
    try:
        start = len(car.adapter.commands)
        report = kline_probe.run(b, settle=0, full=full)
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
    # service 21 refused only the block number, so every block and identification option was tried
    assert {f"21{n:02X}" for n in range(256)} <= set(sent) and {f"1A{n:02X}" for n in range(0x80, 0xA0)} <= set(sent)
    assert any(f.startswith("Data blocks that answer (21 xx, module 0x10, tester 0xF1, fast init): 10 (3 bytes)")
               for f in report.findings)
    text = "\n".join(report.lines())
    assert "> 2110" in text and "61 10 0C 80 50" in text and "> 2111" not in text    # refusals are only counted
    assert "every data block: 256 tried" in text and "x255" in text
    # service 22 exists: every page sampled, the described pages in full; page 0x30 answered its sample
    # but is only reported, never swept (a sweep of an unknown page stopped a real engine)
    # ... asked in the shape that got an answer (number + 04 01), found by trying a few numbers first
    assert "22110104" not in sent and any("reacts to the shape '22 1101 0401'" in f for f in report.findings)
    assert "2230000401" in sent and "2230FF0401" not in sent and "2211FF0401" in sent and "2214FF0401" not in sent
    assert any("not swept" in f and f.endswith(": 30") for f in report.findings)
    where = "module 0x10, tester 0xF1, fast init"
    assert f"Common identifiers that answer (22 11 xx, {where}): 1 (1 data bytes): 01" in report.findings
    assert any("22 12 xx" in f and f.endswith("none") for f in report.findings)


def test_scan_is_skipped_without_the_data_block_service():
    report, sent, _ = _probe("iso9141")
    assert "2110" not in sent and not any("Data blocks" in f for f in report.findings)


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


def test_quick_run_tries_the_shapes_but_leaves_the_full_scans_out():
    report, sent, _ = _probe("kwp", full=False)
    assert "2211010401" in sent and "21FF" not in sent and "2211FF0401" not in sent
    assert any(f.startswith("Quick run") for f in report.findings)
    assert any("reacts to the shape '22 1101 0401'" in f and "62 11 01 50" in f for f in report.findings)


def test_full_scan_is_left_out_when_the_engine_is_running(monkeypatch):
    from canopener.sim import elm as sim_elm
    monkeypatch.setattr(sim_elm, "VOLTAGE", "14.3V")
    report, sent, _ = _probe("kwp", full=True)
    assert "21FF0401" not in sent and "2211FF0401" not in sent and "2211010401" in sent
    assert any("engine looks to be running (14.3V" in f for f in report.findings)


REAL_REPORT = """== What it found ==
- Service 21 reacts to the shape '21 01 0401' (module 0x10, tester 0xFC, fast init): answered: 61 01 00 18 00 00
- Service 22 reacts to the shape '22 0000 0401' (module 0x10, tester 0xFC, fast init): refused: general reject
- Service 22 reacts to the shape '22 1101 0401' (module 0x10, tester 0xFC, fast init): answered: 62 11 01 8A
- Data blocks that answer (21 xx, module 0x10, tester 0xFC, fast init): 00 (4 bytes), 01 (4 bytes)
- Identification options that answer (1A xx, module 0x10, tester 0xFC, fast init): 80 (1 bytes)
- Common identifiers that answer (22 11 xx, module 0x10, tester 0xFC, fast init): 3 (1, 4 data bytes): 00 01 EB
- Common identifiers that answer (22 12 xx, module 0x10, tester 0xFC, fast init): none
- Common identifiers that answer (22 14 xx, module 0x10, tester 0xFC, fast init): 2 (1 data bytes): 00 01
- The scan of 2214xx0401 stopped at 2214460401: the module went quiet (no data)
"""


def test_plan_from_a_report_keeps_the_described_pages_only():
    plan = kline_probe.plan_from_report(REAL_REPORT)
    assert (plan.ecu, plan.tester, plan.fast) == (0x10, 0xFC, True)
    assert plan.commands == ["21000401", "21010401", "2211000401", "2211010401", "2211EB0401"]   # never page 14
    assert kline_probe.plan_from_report("== What it found ==\n- Generic stored codes: none\n") is None


def test_record_values_reads_the_plan_twice_and_sets_the_adapter_up_again():
    car = SimulatedCar(vehicle="kwp")
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket))
    b.open()
    try:
        plan = kline_probe.Plan(0x10, 0xF1, True, ["21100401", "2211010401", "2212000401"])
        start = len(car.adapter.commands)
        rows, summary = kline_probe.record_values(b, plan, settle=0)
        sent = car.adapter.commands[start:]
        assert sent.count("2211010401") == 2 and "ATSH8110F1" in sent and "ATFI" in sent
        assert [r.split(",")[2:] for r in rows[:4]] == [
            ["ATRV", "12.6V", "battery"], ["21100401", "0C8050", "ok"], ["2211010401", "50", "ok"],
            ["2212000401", "", "refused: request out of range (7F 22 31)"]]
        assert summary[0].startswith("4 values read in 2 pass(es) of 3 requests")
        assert b.protocol == "5"
    finally:
        b.close()
        car.stop()
