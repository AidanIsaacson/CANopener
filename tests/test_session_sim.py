"""End-to-end: the Session and scan engine against the simulated machine."""

import time

from canopener.can.logfile import read_log
from canopener.core import report, scan
from canopener.core.connection import load_log_session
from canopener.j1939.constants import DM2, PGN_COMPONENT_ID, PGN_VIN


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_broadcast_dm1_and_live_data(sim_session):
    session, _ = sim_session
    assert wait_for(lambda: session.latest_for(0xFECA) and session.latest_for(0xF004))
    lamps = session.current_lamps()
    assert lamps[0x00].warn.on and not lamps[0x27].warn.on


def test_full_scan(sim_session):
    session, _ = sim_session
    wait_for(lambda: len(session.known_ecus()) >= 2)
    res = scan.run_preset(session, "full", sleep=time.sleep)
    codes = {(r.status, r.sa, r.spn, r.fmi) for r in res.rows}
    assert ("Active", 0x00, 3719, 16) in codes
    assert {("Stored", 0x00, 168, 4), ("Stored", 0x00, 520205, 11), ("Stored", 0x27, 522001, 5)} <= codes
    assert res.responders(PGN_COMPONENT_ID) == [0x00, 0x27]
    assert res.freeze_frames()[0x00][0][0].dtc.spn == 100
    vin_nacks = res.nacks(PGN_VIN)                     # nobody knows the VIN: directed retry is NACKed
    assert PGN_VIN in res.retried and {a.sa for a in vin_nacks} == {0x00, 0x27}
    text = report.build_report(res, session, full=True)
    assert "DPF soot load" in text and "Bus survey" in text and "Engine hours" in text


def test_directed_request_uses_rts_cts(sim_session):
    session, _ = sim_session
    t0 = time.time()
    assert session.request(DM2, 0x00)
    assert wait_for(lambda: (DM2, 0x00) in session.snapshot_since(t0))
    # we must have sent CTS + EOM ACK (TP.CM to the engine)
    sent = [f for f in list(session.raw) if f.tx and (f.can_id >> 8) & 0xFFFF == 0xEC00]
    assert [f.data[0] for f in sent] == [0x11, 0x13]


def test_listen_only_sends_nothing(sim_session):
    session, _ = sim_session
    session.listen_only = True
    res = scan.run_scan(session, [DM2], listen=0.3)
    assert session.tx_count == 0
    assert res.rows == [] and res.listen_only


def test_intermittent_code_lands_in_history(sim_session):
    session, sim = sim_session
    sim.model.t0 -= 600          # pretend the engine is hot: idle oil pressure sags below the limit
    assert wait_for(lambda: (0xFECA, 0x00, 100, 18) in session.dtc_hist, timeout=4)


def test_recording_then_offline_decode(sim_session, tmp_path):
    session, _ = sim_session
    path = tmp_path / "cap.log"
    session.start_recording(str(path))
    scan.run_preset(session, "stored")
    session.stop_recording()
    frames = read_log(str(path))
    assert any(f.can_id == 0x18EAFFF9 for f in frames)      # our request was logged too
    offline, n, _markers = load_log_session(str(path))
    assert n == len(frames)
    res = scan.offline_result(offline)
    assert ("Stored", 0x00, 168, 4) in {(r.status, r.sa, r.spn, r.fmi) for r in res.rows}
    assert offline.tx_count == 0                             # decoding never transmits
