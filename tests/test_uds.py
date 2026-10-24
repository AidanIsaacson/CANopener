"""UDS (ISO 14229) protocol layer and client over the OBD simulator."""

import pytest

from canopener import uds
from canopener.core import connection
from canopener.core.uds import UdsClient


def test_request_builders():
    assert uds.read_data_by_identifier(0xF190) == bytes([0x22, 0xF1, 0x90])
    assert uds.read_dtc_by_status_mask(0xFF) == bytes([0x19, 0x02, 0xFF])
    assert uds.tester_present() == bytes([0x3E, 0x00])
    assert uds.diagnostic_session(0x03) == bytes([0x10, 0x03])
    assert uds.build_request(0x31, bytes([0x01, 0x12, 0x34])) == bytes([0x31, 0x01, 0x12, 0x34])


def test_dtc_and_status_decode():
    assert uds.decode_uds_dtc(0x45, 0x61, 0x00) == "C0561-00"
    assert uds.decode_uds_dtc(0x07, 0x30, 0x00) == "P0730-00"
    assert uds.status_flags(0x08) == ["confirmedDTC"]
    assert set(uds.status_flags(0x09)) == {"testFailed", "confirmedDTC"}


def test_negative_response_raises_with_nrc():
    with pytest.raises(uds.UdsError) as e:
        uds.parse_response(0x22, bytes([0x7F, 0x22, 0x31]))
    assert e.value.nrc == 0x31 and "out of range" in str(e.value)


def test_positive_response_payload():
    assert uds.parse_response(0x22, bytes([0x62, 0xF1, 0x90, 0x41, 0x42])) == bytes([0xF1, 0x90, 0x41, 0x42])


def test_client_reads_dtcs_and_did_from_sim():
    conn = connection.open_connection(connection.OBD_SIM)
    try:
        c = UdsClient(conn.session)
        assert c.available
        codes = {code for code, _status, _flags in c.read_dtcs()}
        assert codes == {"C0561-00", "P0730-00"}
        vin = c.read_data_by_identifier(0xF190)
        assert vin.decode("ascii") == "1HGCM82633A004352"
        c.close()
    finally:
        conn.close()


def test_client_refuses_in_listen_only():
    conn = connection.open_connection(connection.OBD_SIM, listen_only=True)
    try:
        c = UdsClient(conn.session)
        assert not c.available
        with pytest.raises(uds.UdsError):
            c.read_dtcs()
        c.close()
    finally:
        conn.close()


def test_uds_layering_is_pure():
    import pathlib
    root = pathlib.Path(uds.__file__).parent
    src = "\n".join(p.read_text() for p in root.glob("*.py"))
    for banned in ("import canopener.core", "from ..core", "from ..gui", "from ..sim"):
        assert banned not in src, f"uds/ should not reference app layers: {banned}"


def test_clear_reset_and_multi_frame_write():
    assert uds.clear_diagnostic_information() == bytes([0x14, 0xFF, 0xFF, 0xFF])
    assert uds.ecu_reset(uds.RESET_SOFT) == bytes([0x11, 0x03])
    conn = connection.open_connection(connection.OBD_SIM)
    try:
        c = UdsClient(conn.session)
        c.clear_dtcs()
        assert c.read_dtcs() == [] and conn.simulator.uds_dtcs == []
        c.ecu_reset()
        assert conn.simulator.resets == 1
        # 3 + 17 bytes: goes out as First Frame + Consecutive Frames, paced by the module's flow control.
        c.request(uds.build_request(0x2E, bytes([0xF1, 0x90]) + b"2T1BURHE0JC123456"))
        assert c.read_data_by_identifier(0xF190) == b"2T1BURHE0JC123456"
        c.close()
    finally:
        conn.close()
