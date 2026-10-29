"""OBD-II client driving the OBD simulator (engine + transmission ECUs) over the virtual bus."""

import pytest

from canopener import obd
from canopener.can.backends import VirtualBackend, VirtualBus
from canopener.core import connection
from canopener.core.obd import ObdClient
from canopener.core.session import Session
from canopener.sim.obd import ObdSimulator

ECM, TCM = 0x7E8, 0x7E9


@pytest.fixture
def car():
    conn = connection.open_connection(connection.OBD_SIM)
    c = ObdClient(conn.session)
    yield c, conn.simulator
    c.close()
    conn.close()


def test_reads_vin_dtcs_and_live(car):
    c, _ = car
    assert c.available
    assert c.read_vin() == "1HGCM82633A004352"          # multi-frame, exercises ISO-TP flow control
    assert c.addressing is obd.ELEVEN_BIT
    by_ecu = c.read_dtcs_by_ecu()
    assert by_ecu[ECM] == {"stored": ["P0133", "P0420"], "pending": ["P0171"], "permanent": ["P0420"]}
    assert by_ecu[TCM]["stored"] == ["P0730"]
    assert c.read_dtcs()["stored"] == ["P0133", "P0420", "P0730"]
    live = {r.name: (r.value, r.unit) for r in c.read_live()}
    assert live["Engine speed"][1] == "rpm"
    assert live["Control module voltage"] == (14.1, "V")
    assert "Vehicle speed" in live


def test_supported_pid_walk_spans_ranges(car):
    c, _ = car
    by_ecu = c.supported_by_ecu()
    assert {0x05, 0x0C, 0x0D, 0x11} <= set(by_ecu[ECM])
    assert {0x42, 0x5E, 0xA6} <= set(by_ecu[ECM])         # reached via the continuation bits
    assert by_ecu[TCM] == [0x01]
    live = c.live_pids()
    assert (ECM, 0x05) == live[0] and (ECM, 0x01) not in live and (ECM, 0x15) in live


def test_readiness_freeze_frame_and_test_results(car):
    c, _ = car
    ready = c.read_readiness()
    ecm = ready[ECM]
    assert ecm.mil and ecm.dtc_count == 2 and [m.name for m in ecm.incomplete] == ["Evaporative system"]
    assert ready[TCM].ready
    ff = c.read_freeze_frame()[ECM]
    assert ff[0].text == "P0133" and ff[0].frame == 0
    assert {r.name: r.value for r in ff[1:]}["Engine speed"] == 2150.0
    results = c.read_test_results()
    cat = [t for ecu, t in results if t.monitor == "Catalyst B1"]
    assert len(results) == 4 and len(cat) == 1 and not cat[0].passed and cat[0].value == 0.859375
    assert all(t.passed for _, t in results if t.monitor != "Catalyst B1")


def test_vehicle_info_names_each_ecu(car):
    c, _ = car
    info = c.read_info_by_ecu()
    assert ("VIN", "1HGCM82633A004352") in info[ECM]
    assert ("Calibration ID", "SIMTCM-02B") in info[TCM]
    assert dict(info[ECM])["In-use performance (spark): Ignition cycles"] == "450"
    assert c.ecu_label(ECM) == "ECM (7E8)" and c.ecu_label(TCM) == "TCM (7E9)"


def test_clear_codes_waits_through_response_pending(car):
    c, sim = car
    assert c.clear_dtcs() == {ECM: (True, "cleared"), TCM: (True, "cleared")}
    by_ecu = c.read_dtcs_by_ecu()
    assert by_ecu[ECM] == {"stored": [], "pending": [], "permanent": ["P0420"]}   # permanent stays
    assert by_ecu[TCM]["stored"] == []
    ecm = c.read_readiness()[ECM]
    assert not ecm.mil and ecm.dtc_count == 0 and len(ecm.incomplete) == 8
    assert c.read_freeze_frame() == {} and c.read_test_results() == []
    assert sim.cleared


def test_mode08_evap_test(car):
    c, sim = car
    assert c.control_tests() == {ECM: [0x01]}
    assert c.run_control_test(0x01) == {ECM: (True, "started")}
    assert sim.evap_test_running
    assert c.run_control_test(0x01) == {ECM: (False, "conditions not correct")}


def test_29_bit_addressing_is_detected():
    bus = VirtualBus()
    sim = ObdSimulator(VirtualBackend(bus, "car"), addressing="29")
    sim.start()
    session = Session(VirtualBackend(bus, "tool"))
    session.start()
    c = ObdClient(session)
    try:
        assert c.read_vin() == "1HGCM82633A004352"
        assert c.addressing is obd.TWENTY_NINE_BIT
        by_ecu = c.read_dtcs_by_ecu()
        assert by_ecu[0x18DAF110]["stored"] == ["P0133", "P0420"] and by_ecu[0x18DAF118]["stored"] == ["P0730"]
        assert c.read_pid(0x18DAF110, 0x42).value == 14.1          # physical request on 29-bit
    finally:
        c.close()
        session.stop()
        sim.stop()


def test_listen_only_blocks_requests():
    conn = connection.open_connection(connection.OBD_SIM)
    conn.session.listen_only = True
    c = ObdClient(conn.session, timeout=0.2)
    try:
        assert not c.available
        assert c.query(0x01, 0x0C) is None               # send refused, nothing comes back
        assert c.clear_dtcs() == {}
        assert not conn.simulator.cleared
    finally:
        c.close()
        conn.close()


def test_seeded_sim_keeps_answering_run_time():
    # sim_seed is the sim's start time, so "time since start" is about the epoch: PID 1F (run time,
    # 2 bytes) used to overflow, killing the sim's rx thread so it never answered again.
    conn = connection.open_connection(connection.OBD_SIM, sim_seed=1)
    c = ObdClient(conn.session)
    try:
        assert c.read_pid(ECM, 0x1F).value == 65535         # saturates, like a real ECU
        assert c.read_vin() == "1HGCM82633A004352"          # and it's still answering
    finally:
        c.close()
        conn.close()
