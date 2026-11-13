from canopener.j1939 import fields
from canopener.j1939.addresses import J1939Name, sa_name
from canopener.j1939.constants import DM1, PGN_REQUEST, pgn_label
from canopener.j1939.dtc import Dtc, Lamps, decode_dm, decode_dm4, decode_dm5, decode_dtc, encode_dm
from canopener.j1939.ids import build_id, parse_id
from canopener.j1939.info import describe
from canopener.j1939.signals import SIGNALS, SIGNALS_BY_PGN
from canopener.j1939.spn import spn_name


def test_parse_pdu2_broadcast():
    j = parse_id(0x18FECA00)
    assert (j.priority, j.pgn, j.sa, j.da) == (6, DM1, 0x00, 0xFF)


def test_parse_pdu1_destination():
    j = parse_id(0x18EA00F9)          # request from the tool to the engine
    assert (j.pgn, j.sa, j.da) == (PGN_REQUEST, 0xF9, 0x00)


def test_build_id_round_trip():
    assert build_id(PGN_REQUEST, 0xF9, 0x00, 6) == 0x18EA00F9
    assert build_id(DM1, 0x00) == 0x18FECA00
    assert build_id(0xF004, 0x00, priority=3) == 0x0CF00400
    for cid in (0x18EA00F9, 0x1CECFF00, 0x0CF00400, 0x18FEF127):
        j = parse_id(cid)
        assert build_id(j.pgn, j.sa, j.da, j.priority) == cid


def test_decode_dtc_spn_bits_and_oc():
    d = Dtc(520205, 11, 5)
    assert decode_dtc(d.raw()) == d
    d = decode_dtc(bytes([0x64, 0x00, 0x12, 0x06]))     # SPN 100, FMI 18, OC 6
    assert (d.spn, d.fmi, d.oc, d.cm) == (100, 18, 6, 0)


def test_decode_dtc_no_fault_and_unavailable_oc():
    assert decode_dtc(b"\x00\x00\x00\x00") is None
    assert decode_dtc(b"\xff\xff\xff\xff") is None
    assert decode_dtc(bytes([0x6E, 0x00, 0x00, 0x7F])).oc is None


def test_decode_dtc_old_conversion_method_keeps_alternate():
    d = decode_dtc(bytes([0x00, 0x0D, 0x83, 0x81]))
    assert d.cm == 1 and d.alt_spn is not None


def test_lamps():
    lp = Lamps.from_bytes(0b01000100, 0b00111111)    # MIL on (slow flash), warn on (no flash)
    assert lp.mil.on and lp.mil.flashing
    assert lp.warn.on and not lp.warn.flashing
    assert not lp.stop.on and not lp.protect.on
    assert str(lp) == "MIL:ON (slow flash) Stop:off Warn:ON Protect:off"


def test_dm1_single_and_multi():
    lamps, dtcs = decode_dm(encode_dm(0x04, 0xFF, []))
    assert dtcs == [] and lamps.warn.on
    payload = encode_dm(0x04, 0xFF, [Dtc(3719, 16, 3), Dtc(100, 18, 7)])
    assert len(payload) == 10
    assert [(d.spn, d.fmi, d.oc) for d in decode_dm(payload)[1]] == [(3719, 16, 3), (100, 18, 7)]


def test_dm4_freeze_frame():
    body = Dtc(100, 18, 6).raw() + bytes([0x00]) + fields.encode(101, 1, 2) + fields.encode(850, 2, 0.125) \
        + fields.encode(18, 1) + fields.encode(87, 1, 1, -40) + fields.encode(0.0, 2, 1 / 256) + b"\x3c\x00"
    (ff,) = decode_dm4(bytes([len(body)]) + body)
    assert ff.dtc.spn == 100 and ff.rpm == 850 and ff.coolant_c == 87 and ff.boost_kpa == 100
    assert ff.extra == b"\x3c\x00"


def test_dm5():
    r = decode_dm5(bytes([1, 4, 5, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]))
    assert (r.active, r.previously_active) == (1, 4)
    assert "Not intended" in r.obd_text


def test_fields_not_available():
    assert fields.u8(b"\xfe", 0) is None
    assert fields.u16(b"\xff\xff", 0) is None
    assert fields.u16(b"\x40\x1a", 0, 0.125) == 0x1A40 * 0.125
    assert fields.read(b"\xff", 0, 1, na_check=False) == 255
    assert fields.u8(b"", 0) is None


def test_signal_encode_decode_round_trip():
    for sig in SIGNALS:
        buf = bytearray(b"\xff" * 8)
        if sig.is_text:
            sig.encode_into(buf, "D1")
            assert sig.decode(buf) == "D1", sig.name
            continue
        raw = 100 if sig.width_bits >= 8 else 1         # a switch or state field can't hold 100
        value = sig.offset + sig.scale * raw
        sig.encode_into(buf, value)
        assert abs(sig.decode(buf) - value) < 1e-6, sig.name


def test_signals_do_not_overlap_within_a_pgn():
    for pgn, sigs in SIGNALS_BY_PGN.items():
        used = set()
        for s in sigs:
            first = s.start * 8 + s.bit
            span = set(range(first, first + s.width_bits))
            assert not used & span, f"{s.name} overlaps in {pgn:04X}"
            used |= span


def test_name_round_trip():
    n = J1939Name.build(identity=123456, manufacturer=99, function=0, industry_group=3,
                        arbitrary_address_capable=True)
    m = J1939Name.from_bytes(n.to_bytes())
    assert (m.identity, m.manufacturer, m.function, m.industry_group, m.arbitrary_address_capable) == \
        (123456, 99, 0, 3, True)
    assert "Engine" in m.function_text and m.industry_text == "Construction"


def test_info_decoding():
    assert describe(0xFEDA, b"\x02EMR4 1.0*BOOT 2*") == "EMR4 1.0 | BOOT 2"
    assert describe(0xFEEB, b"DEUTZ*TCD*123*U1*").startswith("Make: DEUTZ, Model: TCD")
    assert describe(0xFEE5, fields.encode(100, 4, 0.05) + fields.encode(5000, 4, 1000)).startswith("100.00 h")


def test_names():
    assert spn_name(100) == "Engine oil pressure"
    assert "Manufacturer-specific" in spn_name(520205)
    assert sa_name(0x00) == "00 Engine #1"
    # Codes a Kubota-engined forklift set with its MAF/IAT sensor unplugged.
    assert spn_name(132) == "Intake air mass flow rate (MAF)"
    assert spn_name(171) == "Ambient air temperature"
    assert sa_name(0x47) == "47 Chassis controller #1" and sa_name(0x54) == "54 Cab display #2"
    assert pgn_label(0xFF10) == "Proprietary B"


def test_standard_signal_table_is_consistent():
    """Every built-in signal fits in 8 bytes, has a named SPN, a labelled PGN, and its own place."""
    from canopener.j1939.constants import PGN_LABEL
    from canopener.j1939.spn import SPN_NAMES
    seen = set()
    for sig in SIGNALS:
        assert sig.start * 8 + sig.bit + sig.width_bits <= 64, sig.name
        assert sig.spn in SPN_NAMES, sig.name
        assert sig.pgn in PGN_LABEL, sig.name
        assert sig.location not in seen, sig.name
        seen.add(sig.location)
        if sig.enum:
            assert all(raw < (1 << sig.width_bits) for raw, _ in sig.enum), sig.name
    assert len(SIGNALS) > 140


def test_bit_fields_and_states_decode_from_a_real_looking_frame():
    from canopener.j1939.signals import SIGNALS_BY_PGN
    by = {s.name: s for s in SIGNALS_BY_PGN[0xFEF1]}             # CCVS
    d = bytes([0b11110111, 0x00, 0x40, 0b00010101, 0xFF, 80, 0b11100101, 0xFF])
    assert by["Parking brake switch"].text(by["Parking brake switch"].decode(d)) == "On"
    assert by["Vehicle speed"].decode(d) == 64.0
    assert by["Cruise control active"].text(by["Cruise control active"].decode(d)) == "On"
    assert by["Brake switch"].text(by["Brake switch"].decode(d)) == "Pressed"
    assert by["Clutch switch"].text(by["Clutch switch"].decode(d)) == "Released"
    assert by["Cruise control set speed"].decode(d) == 80
    assert by["PTO governor state"].text(by["PTO governor state"].decode(d)) == "Set"
    etc2 = {s.name: s for s in SIGNALS_BY_PGN[0xF005]}
    d = bytes([0x7E, 0xE8, 0x03, 0x7F, ord("D"), ord("2"), ord("D"), ord(" ")])
    assert etc2["Transmission selected gear"].decode(d) == 1 and etc2["Transmission current gear"].decode(d) == 2
    assert etc2["Transmission actual gear ratio"].decode(d) == 1.0
    assert etc2["Transmission requested range"].decode(d) == "D2" and etc2["Transmission current range"].decode(d) == "D"
    na = bytes([0xFF] * 8)                                      # nothing fitted: every field reads "not available"
    assert all(s.decode(na) is None for sigs in SIGNALS_BY_PGN.values() for s in sigs)


def test_later_diagnostic_messages():
    from canopener.j1939 import constants as k
    from canopener.j1939.dtc import Dtc, decode_dm19, decode_dm21, decode_dm29, decode_dm31, encode_dm
    from canopener.j1939.info import describe
    cal = (0x1A2B3C4D).to_bytes(4, "little") + b"CAL-ID-0001".ljust(16, b"\x00")
    got = decode_dm19(cal + (0xFFFFFFFF).to_bytes(4, "little") + b"\xff" * 16)
    assert [(c.cal_id, c.cvn) for c in got] == [("CAL-ID-0001", 0x1A2B3C4D)]
    assert describe(k.DM19, cal) == "CAL-ID-0001  CVN 1A2B3C4D"
    h = decode_dm21(bytes([0x10, 0x00, 0xE8, 0x03, 0xFF, 0xFF, 0x3C, 0x00]))
    assert (h.km_mil_on, h.km_since_clear, h.minutes_mil_on, h.minutes_since_clear) == (16, 1000, None, 60)
    assert "since codes cleared 1,000 km / 60 min" in describe(k.DM21, bytes([0x10, 0, 0xE8, 3, 0xFF, 0xFF, 0x3C, 0]))
    c = decode_dm29(bytes([1, 2, 3, 0, 0xFF, 0xFF, 0xFF, 0xFF]))
    assert (c.pending, c.all_pending, c.mil_on, c.previously_mil_on, c.permanent) == (1, 2, 3, 0, None)
    one = Dtc(3936, 16, 5)
    pairs = decode_dm31(one.raw() + bytes([0x40, 0xFF]))
    assert pairs[0][0].spn == 3936 and pairs[0][0].fmi == 16 and pairs[0][1].any_on
    lamps, codes = __import__("canopener.j1939.dtc", fromlist=["decode_dm"]).decode_dm(encode_dm(0, 0xFF, [one]))
    assert k.DTC_PGNS[k.DM28] == "Permanent" and k.DTC_PGNS[k.DM27] == "All pending" and codes[0].spn == 3936
