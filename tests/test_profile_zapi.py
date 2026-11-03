"""The Zapi COMBIACX pallet-truck profile (unconfirmed) decodes frames like the ones captured."""

from canopener.core import profiles


def _sig(p, name):
    return next(s for s in p.signals if s.name == name)


def test_zapi_profile_loads_and_is_marked_unconfirmed():
    p = profiles.find("zapi-combiacx")
    assert p is not None and p.builtin and p.protocol == profiles.CANOPEN
    assert "Unconfirmed" in p.name
    assert p.nodes[0x08].startswith("Zapi AXM") and p.detect_nodes == {0x08, 0x09, 0x60, 0x70} and p.detect_min == 3


def test_zapi_profile_decodes_sample_frames():
    p = profiles.find("zapi-combiacx")
    tiller = bytes.fromhex("0000ff0f00000000")        # full throttle one way
    assert round(_sig(p, "Throttle").decode(tiller)) == 100
    assert _sig(p, "Throttle in neutral").decode(tiller) == 0
    assert _sig(p, "Lift button").decode(bytes.fromhex("4100000000000000")) == 1
    assert _sig(p, "Lower button").decode(bytes.fromhex("8100000000000000")) == 1
    assert _sig(p, "Horn button").decode(bytes.fromhex("0900000000000000")) == 1
    stopped_low = bytes.fromhex("810a000000881040")     # stopped, 10 %, low-battery flags set
    assert _sig(p, "Battery charge").decode(stopped_low) == 10
    assert _sig(p, "Low battery").decode(stopped_low) == 1
    assert _sig(p, "Motor frequency").decode(stopped_low) == 0
    driving = bytes.fromhex("7664fe0c00501104")         # 100 %, 0xFE0C = -500 -> -5.00 Hz, negative way
    assert _sig(p, "Motor frequency").decode(driving) == -5.0
    assert _sig(p, "Direction requested").decode(driving) == 2
    assert _sig(p, "Traction active").decode(driving) == 1
    assert _sig(p, "Low battery").decode(driving) == 0
    assert _sig(p, "Lifting").decode(bytes.fromhex("1664000000001108")) == 1
    assert _sig(p, "Lowering").decode(bytes.fromhex("0064000000001110")) == 1
