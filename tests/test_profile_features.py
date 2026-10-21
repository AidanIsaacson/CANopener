"""Bit fields, signed values, enums, ranges, multi-SA signals, detection, units, plugins."""

import json
import sys
import textwrap

import pytest

from canopener.core import profiles, units
from canopener.j1939 import fields
from canopener.j1939.signals import SIGNALS, Signal, all_signals, set_profile_signals


def test_read_bits_little_endian_and_spanning_bytes():
    d = bytes([0b10110100, 0b00000011])
    assert fields.read_bits(d, 0, 2, 1) == 1
    assert fields.read_bits(d, 0, 3, 1) == 0
    assert fields.read_bits(d, 0, 4, 4, na_check=False) == 0b1011
    # 12-bit value starting at bit 4 of byte 0: 0x03B
    assert fields.read_bits(d, 0, 4, 12, na_check=False) == 0x03B
    assert fields.read_bits(b"\x01", 0, 4, 12) is None            # runs past the end


def test_read_bits_not_available_and_signed():
    assert fields.read_bits(b"\x03", 0, 0, 2) is None             # 2-bit "11" = not available
    assert fields.read_bits(b"\x01", 0, 0, 1) == 1                # a 1-bit flag has no "n/a" value
    assert fields.read_bits(b"\x03", 0, 0, 2, na_check=False) == 3
    assert fields.read_bits(b"\xfb", 0, 0, 8) is None             # 8-bit error range
    assert fields.read_bits(b"\xff\x7f", 0, 0, 16, signed=True) == 32767
    assert fields.read_bits(b"\x00\x80", 0, 0, 16, signed=True) == -32768
    assert fields.read_bits(b"\xfe", 0, 0, 8, signed=True, scale=0.5) == -1.0


def test_encode_bits_round_trip_preserves_neighbours():
    buf = bytearray(b"\xff\xff")
    fields.encode_bits(buf, 0, 4, 12, 0x123)
    assert fields.read_bits(buf, 0, 4, 12) == 0x123
    assert buf[0] & 0x0F == 0x0F                                   # low nibble untouched
    fields.encode_bits(buf, 1, 7, 1, 0)
    assert buf[1] >> 7 == 0
    fields.encode_bits(buf, 0, 0, 8, -2, signed=True)
    assert fields.read_bits(buf, 0, 0, 8, signed=True) == -2


def test_signal_enum_range_and_sources():
    s = Signal("Seat", 0xFF10, 4, 1, 1, 0, "", bit=4, bits=1, enum=((0, "Empty"), (1, "Occupied")),
               sa=(0x27, 0xE4), low=None, high=None)
    assert s.decode(bytes([0, 0, 0, 0, 0x10])) == 1 and s.text(1) == "Occupied" and s.text(None) is None
    assert s.accepts(0xE4) and not s.accepts(0x00)
    r = Signal("Temp", 0xFEEE, 0, 1, 1, -40, "°C", low=-20, high=110)
    assert r.out_of_range(120) and r.out_of_range(-30) and not r.out_of_range(80) and not r.out_of_range(None)
    signed = Signal("Torque", 0xFF20, 0, 2, 0.5, 0, "Nm", signed=True)
    assert signed.decode(b"\x00\xff") == -128.0


def test_profile_signal_replaces_builtin_at_same_location():
    builtin = next(s for s in SIGNALS if s.spn == 190)
    renamed = Signal("Engine RPM (OEM name)", builtin.pgn, builtin.start, builtin.length, builtin.scale,
                     builtin.offset, builtin.unit, group="Engine (OEM)")
    set_profile_signals([renamed])
    try:
        names = [s.name for s in all_signals() if s.pgn == builtin.pgn and s.start == builtin.start]
        assert names == ["Engine RPM (OEM name)"]
    finally:
        set_profile_signals([])
    assert builtin in all_signals()


def test_parse_profile_new_keys():
    p = profiles.parse_profile({
        "name": "T", "detect": {"addresses": ["0x00", 39, "0xE4"], "min_match": 2},
        "signals": [
            {"name": "Flag", "pgn": "0xF004", "start": 0, "bit": 2, "bits": 2, "enum": {"0": "off", "1": "on"},
             "group": "G", "sa": [0, "0x27"]},
            {"name": "Volts", "pgn": "0xFF00", "start": 1, "length": 2, "unit": "Volts", "low": 9, "high": 15,
             "signed": True},
        ]})
    flag, volts = p.signals
    assert (flag.bit, flag.bits, flag.length, flag.sa, flag.decimals) == (2, 2, 1, (0, 0x27), 0)
    assert flag.enum == ((0, "off"), (1, "on")) and flag.group == "G"
    assert volts.unit == "V" and (volts.low, volts.high, volts.signed) == (9, 15, True)
    assert p.detect_addresses == frozenset({0, 0x27, 0xE4}) and p.detect_min == 2
    with pytest.raises(profiles.ProfileError, match="Flag2"):
        profiles.parse_profile({"name": "x", "signals": [{"name": "Flag2", "pgn": 1, "start": 0, "bit": 9,
                                                           "bits": 1}]})


def test_detect_ranks_specific_profiles():
    a = profiles.Profile("a", "Small", detect_addresses=frozenset({0, 3}), detect_min=2)
    b = profiles.Profile("b", "Big", detect_addresses=frozenset({0, 3, 39, 228}), detect_min=3)
    c = profiles.Profile("c", "Other", detect_addresses=frozenset({0x80, 0x81}), detect_min=2)
    assert [m.profile.id for m in profiles.detect({0, 3, 39, 228, 23}, [a, b, c])] == ["b", "a"]
    assert [m.profile.id for m in profiles.detect({0, 3}, [a, b, c])] == ["a"]
    assert profiles.detect({0}, [a, b, c]) == []
    assert "3 of 4" in profiles.detect({0, 3, 39}, [b])[0].describe()


def test_units_custom_and_aliases():
    units.set_custom({"pressure": "psi", "temperature": "°C", "speed": "nonsense"})
    try:
        assert units.convert(100, "kPa", units.CUSTOM)[1] == "psi"
        assert units.convert(100, "°C", units.CUSTOM) == (100, "°C")
        v, u = units.convert(200, "kPa gauge", units.CUSTOM)
        assert u == "psi gauge" and abs(v - 29.00754) < 1e-4
        assert units.convert(1.5, "MPa", units.IMPERIAL)[1] == "psi"
        assert units.fmt(400, "kPa", 0, units.CUSTOM) == "58.0 psi"
        assert units.custom()["speed"] == "km/h"                    # bad choice ignored
        assert "psi" in units.describe(units.CUSTOM)
    finally:
        units.set_custom(units.PRESETS[units.METRIC])
    assert units.convert(250, "kPa", units.METRIC) == (250, "kPa")   # metric = as reported
    assert [units.normalize_unit(x) for x in ("C", "Volts", "kph", "RPM", "minutes", "weird")] == \
        ["°C", "V", "km/h", "rpm", "min", "weird"]
    units.set_custom({"pressure": "bar"})
    try:
        assert units.fmt(250, "kPa", 0, units.CUSTOM) == "2.50 bar"
    finally:
        units.set_custom(units.PRESETS[units.METRIC])


def test_session_listener_and_send_frame():
    from canopener.can.backends import VirtualBackend, VirtualBus
    from canopener.can.frame import CanFrame
    from canopener.core.session import Session

    bus = VirtualBus()
    other = VirtualBackend(bus)
    other.open()
    s = Session(VirtualBackend(bus))
    seen = []
    s.add_listener(seen.append)
    s.add_listener(lambda f: 1 / 0)                       # a broken listener mustn't break feeding
    s.feed(CanFrame(0x123, b"\x01", False, 1.0))
    assert [f.can_id for f in seen] == [0x123]
    s.backend.open()
    s.listen_only = True
    assert not s.send_frame(CanFrame(0x601, b"\x40\x00\x10\x00", False))
    s.listen_only = False
    assert s.send_frame(CanFrame(0x601, b"\x40\x00\x10\x00", False))
    got = other.recv()
    assert (got.can_id, got.extended) == (0x601, False) and s.tx_count == 1


def test_plugins_load_from_env(tmp_path, monkeypatch, capsys):
    from canopener import cli, plugins

    pkg = tmp_path / "demo_plugin.py"
    prof = tmp_path / "demo_profiles"
    prof.mkdir()
    (prof / "demo-machine.json").write_text(json.dumps({"name": "Demo machine (plugin)"}))
    pkg.write_text(textwrap.dedent(f"""
        PLUGIN_NAME = "Demo"
        PLUGIN_VERSION = "1.2"
        def profile_dirs():
            return [{str(prof)!r}]
        def register_cli(sub):
            p = sub.add_parser("demo-hello")
            p.set_defaults(fn=lambda a: print("hello from plugin") or 0)
    """))
    (tmp_path / "broken_plugin.py").write_text("raise RuntimeError('nope')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CANOPENER_PLUGINS", "demo_plugin,broken_plugin")
    found = {p.id: p for p in plugins.discover(refresh=True)}
    try:
        assert found["demo_plugin"].name == "Demo" and found["demo_plugin"].version == "1.2"
        assert "nope" in found["broken_plugin"].error
        assert profiles.find("demo-machine").name == "Demo machine (plugin)"
        assert cli.main(["demo-hello"]) == 0 and "hello from plugin" in capsys.readouterr().out
    finally:
        monkeypatch.delenv("CANOPENER_PLUGINS")
        plugins.discover(refresh=True)
        sys.modules.pop("demo_plugin", None)
