"""The generic confirm-first transmit framework: gating, logging, standard frame builders."""

import pytest

from canopener.core import connection, transmit
from canopener.j1939.constants import GLOBAL_SA, PGN_REQUEST
from canopener.j1939.ids import parse_id


def test_standard_actions_registered():
    transmit.register_standard_actions()
    transmit.register_standard_actions()          # idempotent
    names = [a.name for a in transmit.actions()]
    assert any("J1939 Request" in n for n in names)
    assert any("Raw CAN frame" in n for n in names)


def test_j1939_request_builder():
    fr = transmit.j1939_request_frame(0xFEEB, 0x00)
    jid = parse_id(fr.can_id)
    assert jid.pgn == PGN_REQUEST and jid.da == 0x00
    assert fr.data == bytes([0xEB, 0xFE, 0x00])    # requested PGN, little-endian
    assert parse_id(transmit.j1939_request_frame(0xFEEB).can_id).da == GLOBAL_SA  # default: broadcast


def test_raw_frame_validation():
    assert transmit.raw_frame(0x7DF, b"\x02\x01\x0c", extended=False).can_id == 0x7DF
    with pytest.raises(ValueError):
        transmit.raw_frame(0x800, b"", extended=False)      # 11-bit id too big
    with pytest.raises(ValueError):
        transmit.raw_frame(0x100, b"\x00" * 9)              # too many data bytes


def test_transmitter_is_off_by_default_and_logs():
    conn = connection.open_connection(connection.SIMULATOR)
    try:
        tx = transmit.Transmitter(conn.session)
        assert not tx.enabled and not tx.can_send
        with pytest.raises(transmit.TransmitDisabled):
            tx.send(transmit.j1939_request_frame(0xFEEB))
        assert tx.log == []                                 # nothing sent, nothing logged
        tx.enabled = True
        assert tx.can_send
        assert tx.send(transmit.j1939_request_frame(0xFEEB), "req")
        assert len(tx.log) == 1 and tx.log[0].ok and tx.log[0].label == "req"
    finally:
        conn.close()


def test_listen_only_refuses_even_when_enabled():
    conn = connection.open_connection(connection.SIMULATOR, listen_only=True)
    try:
        tx = transmit.Transmitter(conn.session)
        tx.enabled = True
        assert not tx.can_send                              # listen-only wins
        assert tx.send(transmit.j1939_request_frame(0xFEEB)) is False
        assert tx.log[-1].ok is False                       # attempt recorded as failed
    finally:
        conn.close()


def test_plugin_can_register_an_action():
    before = len(transmit.actions())
    transmit.register_action(transmit.TransmitAction(
        "Test action", "desc", [("x", "X", "1")], lambda v: [transmit.raw_frame(0x123, b"\x01", False)]))
    assert len(transmit.actions()) == before + 1
