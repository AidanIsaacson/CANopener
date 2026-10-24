"""OBD-II protocol layer: PID formulas, readiness, freeze frame, Mode 06/08/09, ISO-TP addressing."""

from canopener import obd


def test_single_and_multi_value_pids():
    r = obd.decode(bytes([0x41, 0x0C, 0x1A, 0xF8]))
    assert r.name == "Engine speed" and r.value == 1726.0 and r.unit == "rpm"
    # O2 B1S2: voltage A/200 and short-term trim (B-128)*100/128.
    r = obd.decode(bytes([0x41, 0x15, 0x5A, 0x90]))
    assert r.values == [("O2 B1S2: voltage", 0.45, "V"), ("O2 B1S2: short-term trim", 12.5, "%")]
    # Trim byte 0xFF = sensor not used for trim: only the voltage.
    assert len(obd.decode(bytes([0x41, 0x15, 0x5A, 0xFF])).values) == 1
    # Exhaust gas temp bank 1: only the sensors flagged supported (bit 0 and 2).
    r = obd.decode(bytes([0x41, 0x78, 0b101]) + (4400).to_bytes(2, "big") + b"\x00\x00"
                   + (5000).to_bytes(2, "big") + b"\x00\x00")
    assert [(n.split(": ")[1], v) for n, v, _ in r.values] == [("sensor 1", 400.0), ("sensor 3", 460.0)]
    # Evap vapor pressure is signed: 0xFF38 = -200 -> -50 Pa.
    assert obd.decode(bytes([0x41, 0x32, 0xFF, 0x38])).value == -50.0
    # Text PIDs.
    assert obd.decode(bytes([0x41, 0x03, 0x02, 0x00])).text == "system 1: Closed loop (O2 sensor feedback)"
    assert obd.decode(bytes([0x41, 0x51, 0x01])).text == "Gasoline"
    assert obd.decode(bytes([0x41, 0x1C, 0x01])).text == "OBD-II as defined by the CARB"
    # Unknown PID: raw bytes, not invented.
    assert obd.decode(bytes([0x41, 0xC3, 0x12, 0x34])).values == [("PID 0xC3", "12 34", "raw")]


def test_readiness_spark_and_compression():
    # MIL on, 2 codes; spark; misfire+fuel+components supported, fuel incomplete;
    # catalyst, evap, O2, O2 heater supported; evap incomplete.
    s = obd.decode_monitor_status(bytes([0x82, 0b0010_0111, 0b0110_0101, 0b0000_0100]))
    assert s.mil and s.dtc_count == 2 and not s.compression
    assert {m.name for m in s.incomplete} == {"Fuel system", "Evaporative system"}
    assert not s.ready
    by = {m.name: m.state for m in s.monitors}
    assert by["Catalyst"] == "complete" and by["Heated catalyst"] == "not supported"
    d = obd.decode_monitor_status(bytes([0x00, 0b0000_1111, 0b1100_0000, 0x00]))
    assert d.compression and d.ready
    assert [m.name for m in d.monitors if m.supported][-2:] == ["PM filter", "EGR / VVT system"]


def test_freeze_frame_skips_the_frame_number():
    r = obd.decode(bytes([0x42, 0x05, 0x00, 0x7B]))          # coolant 123-40 = 83 C, frame 0
    assert r.mode == 2 and r.frame == 0 and r.value == 83.0
    r = obd.decode(bytes([0x42, 0x02, 0x00]) + obd.encode_dtc("P0171"))
    assert r.text == "P0171"


def test_mode06_results_and_support():
    rec = bytes([0x21, 0x81, 0x0B]) + (450).to_bytes(2, "big") + (200).to_bytes(2, "big") + (800).to_bytes(2, "big")
    r = obd.decode(bytes([0x46]) + rec + bytes([0x21, 0x82, 0x24, 0, 9, 0, 0, 0, 5]))
    assert len(r.results) == 2
    a, b = r.results
    assert a.monitor == "Catalyst B1" and a.unit == "V" and (a.value, a.minimum, a.maximum) == (0.45, 0.2, 0.8)
    assert a.passed and not b.passed and "manufacturer" in a.test
    r = obd.decode(bytes([0x46, 0x00, 0x80, 0x00, 0x00, 0x01]))
    assert r.supported == [0x01, 0x20]


def test_mode08_and_mode09():
    assert obd.decode(bytes([0x48, 0x00, 0x80, 0, 0, 0])).supported == [0x01]
    assert obd.decode(bytes([0x48, 0x01])).name == "Evaporative system leak test"
    r = obd.decode(bytes([0x49, 0x04, 0x02]) + b"CAL-ONE".ljust(16, b"\x00") + b"CAL-TWO".ljust(16, b"\x00"))
    assert r.text == "CAL-ONE, CAL-TWO"
    r = obd.decode(bytes([0x49, 0x0A, 0x01]) + b"ECM\x00-EngineControl".ljust(20, b"\x00"))
    assert r.text == "ECM-EngineControl"
    counters = b"".join(n.to_bytes(2, "big") for n in range(1, 21))
    r = obd.decode(bytes([0x49, 0x08, 0x14]) + counters)
    assert r.values[1] == ("Ignition cycles", 2.0, "") and len(r.values) == 20


def test_negative_and_pending():
    r = obd.decode(bytes([0x7F, 0x04, 0x22]))
    assert not r.ok and r.error == "conditions not correct" and not r.pending
    assert obd.decode(bytes([0x7F, 0x04, 0x78])).pending


def test_isotp_segment_and_flow_control():
    assert obd.segment(b"\x22\xF1\x90") == [b"\x03\x22\xF1\x90\x55\x55\x55\x55"]
    frames = obd.segment(bytes(range(20)))
    assert frames[0][:2] == b"\x10\x14" and [f[0] for f in frames[1:]] == [0x21, 0x22]
    reasm = obd.IsoTpReassembler()
    out = None
    for f in frames:
        out = reasm.feed(0x7E0, f) or out
    assert out == (0x7E0, bytes(range(20)))
    assert obd.parse_flow_control(b"\x30\x08\x0A") == (0, 8, 0.01)
    assert obd.parse_flow_control(b"\x02\x01") is None


def test_addressing():
    a, b = obd.ELEVEN_BIT, obd.TWENTY_NINE_BIT
    assert a.is_response(0x7E9, False) and not a.is_response(0x7E9, True) and a.request_id(0x7E9) == 0x7E1
    assert b.is_response(0x18DAF110, True) and not b.is_response(0x18DAF110, False)
    assert not b.is_response(0x18DA10F1, True)                 # that's a request, not a reply
    assert b.request_id(0x18DAF118) == 0x18DA18F1 and b.label(0x18DAF118) == "18DAF118"
    assert obd.request_frame(0x02, 0x05, extra=b"\x00") == b"\x03\x02\x05\x00\x55\x55\x55\x55"
