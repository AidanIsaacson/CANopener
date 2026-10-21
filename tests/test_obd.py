"""OBD-II (J1979) protocol layer: ISO-TP reassembly, PID decode, DTC decode, VIN."""

from canopener import obd
from canopener.obd.isotp import IsoTpReassembler, flow_control_frame, request_frame


def test_request_frame_single():
    # Mode 01 PID 0x0C (engine RPM): len=2, 01 0C, padded with 0x55.
    assert request_frame(0x01, 0x0C) == bytes.fromhex("02010C5555555555")
    # Mode 03 (stored DTCs) takes no PID.
    assert request_frame(0x03) == bytes.fromhex("0103555555555555")


def test_single_frame_pid_decode():
    r = IsoTpReassembler()
    # 0x7E8: SF len 4, 41 0C 1A F8  -> RPM = (0x1AF8)/4 = 1726
    done = r.feed(0x7E8, bytes.fromhex("0441 0C 1A F8".replace(" ", "")))
    assert done == (0x7E8, bytes.fromhex("410C1AF8"))
    resp = obd.decode(done[1])
    assert resp.mode == obd.MODE_CURRENT_DATA and resp.pid == 0x0C
    assert resp.name == "Engine speed" and resp.unit == "rpm" and resp.value == 1726.0


def test_pid_temperature_and_speed():
    assert obd.decode(bytes.fromhex("41055B")).value == 51      # coolant 0x5B-40 = 51 °C
    assert obd.decode(bytes.fromhex("410D46")).value == 70      # speed 0x46 = 70 km/h


def test_multiframe_reassembly_and_flow_control():
    r = IsoTpReassembler()
    # First frame: total length 0x014 = 20 bytes, first 6 payload bytes.
    assert r.feed(0x7E8, bytes.fromhex("1014 49 02 01 31 32 33")) is None
    assert 0x7E8 in r.needs_flow_control                        # tester must now clear-to-send
    assert flow_control_frame()[:3] == bytes.fromhex("300000")
    # Consecutive frames carry the rest (seq 1, then 2).
    assert r.feed(0x7E8, bytes.fromhex("21 34 35 36 37 38 39 41")) is None
    done = r.feed(0x7E8, bytes.fromhex("22 42 43 44 45 46 47 00"))
    assert done is not None
    resp = obd.decode(done[1])
    assert resp.mode == obd.MODE_VEHICLE_INFO and resp.name == "VIN"
    assert resp.text == "123456789ABCDEFG"                      # 16 ASCII bytes after the count


def test_consecutive_out_of_order_is_dropped():
    r = IsoTpReassembler()
    r.feed(0x7E8, bytes.fromhex("1014 49 02 01 31 32 33"))
    assert r.feed(0x7E8, bytes.fromhex("22 34 35 36 37 38 39 41")) is None   # wrong seq (want 1)
    assert 0x7E8 not in r.pending


def test_dtc_round_trip_and_list():
    assert obd.decode_dtc(0x01, 0x33) == "P0133"
    assert obd.decode_dtc(0xC1, 0x23) == "U0123"
    assert obd.encode_dtc("P0133") == bytes([0x01, 0x33])
    # Mode 03 response: count=2, then P0133 and P0420 (exact ISO-TP payload, no padding).
    resp = obd.decode(bytes.fromhex("430201330420"))
    assert resp.mode == obd.MODE_STORED_DTCS and resp.dtcs == ["P0133", "P0420"]


def test_negative_response():
    resp = obd.decode(bytes.fromhex("7F 01 12"))
    assert not resp.ok and "sub-function" in resp.error


def test_supported_pids_bitmask():
    # PID 0x00 response with only the top bit set -> PID 0x01 supported.
    resp = obd.decode(bytes.fromhex("4100 80000000"))
    assert resp.supported == [0x01]


def test_obd_layering_is_pure():
    # Mirror of the j1939/can purity rule: obd/ must not import app layers.
    import pathlib
    root = pathlib.Path(obd.__file__).parent
    src = "\n".join(p.read_text() for p in root.glob("*.py"))
    for banned in ("import canopener.core", "from ..core", "from ..gui", "from ..sim",
                   "import canopener.gui"):
        assert banned not in src, f"obd/ should not reference app layers: {banned}"
