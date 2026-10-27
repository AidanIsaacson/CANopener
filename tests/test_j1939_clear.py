"""J1939 code clearing (DM11 / DM3) against the simulated machine."""

import time

from canopener.core import j1939_clear as jc
from canopener.core import scan
from canopener.core.connection import SIMULATOR, open_connection


def _codes(session, preset):
    return {(r.sa, r.spn, r.fmi) for r in scan.run_preset(session, preset, retry_wait=0.5).rows}


def test_clear_active_everywhere_then_stored_on_one_module(sim_session):
    session, sim = sim_session
    time.sleep(0.5)
    assert (0x00, 3719, 16) in _codes(session, "active")
    r = jc.clear_codes(session, jc.ACTIVE)
    assert r.sent == [jc.DM11] and r.answers == [] and r.ok      # a global clear isn't answered
    assert "sent to every module" in r.lines()[0]
    time.sleep(1.2)                                               # next DM1 broadcast
    assert (0x00, 3719, 16) not in _codes(session, "active")
    stored = _codes(session, "stored")
    assert (0x27, 522001, 5) in stored and (0x00, 97, 3) in stored
    r = jc.clear_codes(session, jc.STORED, da=0x27)               # just the machine controller
    assert [(a.sa, a.control) for a in r.answers] == [(0x27, 0)] and r.ok
    assert "27 Management computer #1: DM3 ACK" in r.lines()[0]
    stored = _codes(session, "stored")
    assert (0x27, 522001, 5) not in stored and (0x00, 97, 3) in stored   # the engine kept its history


def test_both_to_one_module_is_acknowledged_twice(sim_session):
    session, sim = sim_session
    r = jc.clear_codes(session, jc.BOTH, da=0x00)
    assert r.sent == [jc.DM11, jc.DM3] and sorted(a.pgn for a in r.answers) == sorted([jc.DM11, jc.DM3])
    assert not sim.stored and not sim.active_engine


def test_nack_and_no_answer_are_reported(sim_session):
    session, sim = sim_session
    sim.engine.commands[jc.DM3] = lambda: False                  # an ECU that refuses
    r = jc.clear_codes(session, jc.STORED, da=0x00)
    assert not r.ok and "NACK" in r.lines()[0]
    r = jc.clear_codes(session, jc.STORED, da=0x42, wait=0.3)    # nobody at 0x42
    assert r.answers == [] and "no answer to DM3" in r.lines()[0]


def test_never_sent_in_listen_only():
    conn = open_connection(SIMULATOR, listen_only=True, sim_seed=1)
    try:
        r = jc.clear_codes(conn.session, jc.BOTH)
        assert r.refused and not r.sent and not r.ok and conn.session.tx_count == 0
        assert conn.simulator.stored                               # nothing cleared
    finally:
        conn.close()


def test_warning_says_what_and_where():
    w = jc.warning(jc.STORED, 0x00)
    assert w.startswith("Clear stored / previously active codes (DM3) on 00 Engine #1?")
    assert "every module" in jc.warning(jc.ACTIVE)
