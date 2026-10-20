import json

import pytest

from canopener.core import profiles, units
from canopener.j1939.addresses import sa_name
from canopener.j1939.constants import pgn_label
from canopener.j1939.signals import all_signals
from canopener.j1939.spn import spn_name


def test_unit_conversion():
    assert units.convert(100, "°C", units.IMPERIAL) == (212, "°F")
    v, u = units.convert(100, "kPa", units.IMPERIAL)
    assert u == "psi" and abs(v - 14.50377) < 1e-4
    assert units.convert(None, "km/h", units.IMPERIAL) == (None, "mph")
    assert units.convert(5, "V", units.IMPERIAL) == (5, "V")
    assert units.fmt(400, "kPa", 0, units.IMPERIAL) == "58.0 psi"
    assert units.fmt(None, "kPa") == "n/a"


def test_builtin_profiles_load():
    found, errors = profiles.available_profiles()
    assert not errors
    ids = [p.id for p in found]
    assert ids[0] == "generic" and "sim-telehandler" in ids


def test_apply_profile_overrides_labels():
    p = profiles.find("sim-telehandler")
    profiles.apply(p)
    assert sa_name(0x27) == "27 Machine controller (sim)"
    assert "sim example" in spn_name(520205)
    assert pgn_label(0xFF10).startswith("Boom status")
    sig = next(s for s in all_signals() if s.name == "Controller heartbeat")
    assert sig.sa == 0x27 and not sig.na_check and sig.decode(b"\xff") == 255
    profiles.apply(None)
    assert sa_name(0x27) == "27 Management computer #1"
    assert not any(s.name == "Controller heartbeat" for s in all_signals())


def test_user_profile_overrides_builtin_and_csv_layer(tmp_path, monkeypatch):
    d = tmp_path / "config" / "canopener" / "profiles"
    d.mkdir(parents=True)
    (d / "my-loader.json").write_text(json.dumps({"name": "My loader", "spns": {"0x1F4": "Custom 500"}}))
    (tmp_path / "config" / "canopener" / "spn_names.csv").write_text("spn,name\n501,From CSV\n")
    p = profiles.find("my-loader")
    assert p is not None and not p.builtin
    profiles.apply(p)
    assert spn_name(500) == "Custom 500" and spn_name(501) == "From CSV"


def test_bad_profile_reports_error():
    with pytest.raises(profiles.ProfileError):
        profiles.parse_profile({"name": "x", "signals": [{"name": "a", "pgn": 1, "start": 0, "length": 3}]})
    with pytest.raises(profiles.ProfileError):
        profiles.parse_profile({"sources": {}})
