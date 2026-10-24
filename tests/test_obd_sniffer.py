"""Passive OBD-II decoding: the sniffer, the OBD-II live model, and code -> sensor links."""

from canopener import obd
from canopener.core import connection
from canopener.core.live import ObdLiveData
from canopener.core.obd import ObdClient


def test_sniffer_follows_a_session_from_frames():
    conn = connection.open_connection(connection.OBD_SIM)
    c = ObdClient(conn.session)
    try:
        c.read_dtcs_by_ecu()
        c.read_readiness()
        c.read_freeze_frame()
        c.read_pid(0x7E8, 0x0C)
        c.clear_dtcs()
        c.run_control_test(0x01)
        with conn.session.lock:
            frames = list(conn.session.raw)
    finally:
        c.close()
        conn.close()
    sn = obd.ObdSniffer()
    for fr in frames:                       # exactly what a recorded log replays
        sn.feed(fr.can_id, fr.extended, bytes(fr.data), fr.timestamp)
    assert sn.names == {0x7E8: "ECM", 0x7E9: "TCM"} and sn.label(0x7E9) == "TCM (7E9)"
    assert sn.dtcs[0x7E8] == {"stored": [], "pending": [], "permanent": ["P0420"]}   # cleared at the end
    assert sn.readiness[0x7E8].mil and sn.values[(0x7E8, 0x0C, "Engine speed")].unit == "rpm"
    assert 0x7E8 not in sn.freeze                                                    # cleared with the codes
    text = [e for _, e in sn.events]
    assert "ECM (7E8): stored code(s) P0133, P0420" in text and "Clear codes (Mode 04) sent" in text
    assert "ECM (7E8): codes cleared" in text and "TCM (7E9): codes cleared" in text
    assert any("Evaporative system leak test" in e for e in text)
    assert sn.modules() == {0x7E8, 0x7E9}


def test_live_model_from_polling():
    conn = connection.open_connection(connection.OBD_SIM)
    c = ObdClient(conn.session)
    m = ObdLiveData()
    try:
        for ecu, pid in c.live_pids():
            c.read_pid(ecu, pid)
        assert m.sample(conn.session)                       # new rows
        names = [m.rows[k].sig.name for k in m.order]
        assert "Engine speed" in names and "Monitor status since codes cleared" not in names
        key = m.find_pids([0x0C])[0]
        row = m.rows[key]
        assert m.source_label(row.sa) == "ECM (7E8)" and m.ids(row) == ("0C", "") and row.sig.group == "Engine"
        fuel = m.rows[m.find_pids([0x03])[0]]               # a text PID: an index into its labels
        assert fuel.sig.text(fuel.value).startswith("system 1: Closed loop")
        c.read_pid(0x7E8, 0x0C)
        assert not m.sample(conn.session) and len(row.points) == 2
        assert (0x7E8, 0x0C) in m.polled()
        assert m.matches(key, "ecm") and m.matches(key, "0c") and not m.matches(key, "tcm")
    finally:
        c.close()
        conn.close()


def test_codes_link_to_their_sensors():
    assert obd.pids_for_dtc("P0133") == [0x14, 0x06]
    assert obd.pids_for_dtc("P0430")[:2] == [0x18, 0x19]
    assert obd.pids_for_dtc("P0118") == [0x05]
    assert obd.pids_for_dtc("P0730") == [] and obd.pids_for_dtc("P1133") == [] and obd.pids_for_dtc("U0100") == []
    assert all(p in obd.PIDS for lo in (0x0100, 0x0420, 0x2195) for p in obd.pids_for_dtc(f"P{lo:04X}"))
