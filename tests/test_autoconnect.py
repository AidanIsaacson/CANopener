"""core.autoconnect: what's set by hand, back to Auto, and what to offer once connected."""

from canopener.core import autoconnect, profiles, suggest
from canopener.core.settings import Settings


def test_manual_settings_and_back_to_auto(tmp_path):
    s = Settings(str(tmp_path / "settings.json"))
    autoconnect.reset(s)
    assert autoconnect.manual(s, two_ports=True) == []
    s.set("can.bitrate", 125000)
    s.set("can.listen_only", True)
    s.set("can.main_auto", False)
    assert autoconnect.manual(s, two_ports=True) == ["125k", "listen-only", "main bus picked by hand"]
    assert autoconnect.manual(s, two_ports=False) == ["125k", "listen-only"]
    s.set("can.source", "sim")
    assert autoconnect.manual(s)[0] == "not the CAN adapter"
    autoconnect.reset(s)
    assert autoconnect.manual(s, two_ports=True) == [] and s.get("can.source") == "socketcan"


def test_a_suggestion_says_what_it_found():
    p = profiles.parse_profile({"name": "Truck A", "protocol": "canopen", "signals": [
        {"name": "Lift input", "id": "0x1A2", "start": 3, "length": 1, "signed": True}]}, "truck-a")
    b = suggest.suggest({("11-bit", 0x1A2, None): [bytes([0, 0, 0, v, 0, 0, 0, 0]) for v in range(0, 200, 10)]}, [p])
    sug = autoconnect.Suggestion(None, b)
    assert not sug.empty and sug.values == 1 and sug.sources() == ["Truck A"]
    assert sug.summary() == "1 more value(s) are known from Truck A"
    assert sug.entries()[0]["observed"]["from"] == "Truck A"
    assert autoconnect.Suggestion().empty
