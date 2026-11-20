"""The generic ZAPI / Chinese AC truck profile (unconfirmed) decodes frames from the captures it came from."""

from canopener.core import profiles


def _sig(p, name):
    return next(s for s in p.signals if s.name == name)


def test_loads_with_its_devices_and_detect_list():
    p = profiles.find("zapi-chinese-ac")
    assert p is not None and p.builtin and p.protocol == profiles.CANOPEN
    assert "Unconfirmed" in p.description
    assert p.nodes[0x08] == "Drive motor controller (master)"
    assert p.nodes[0x0C] == "Drive motor controller (slave)"
    assert p.nodes[0x14] == "Pump motor controller"
    assert "VAM" in p.nodes[0x40] and 0x40 not in p.detect_nodes      # dealer add-on, not on every truck
    assert p.detect_nodes == {0x08, 0x0C, 0x14, 0x30, 0x31, 0x32, 0x33} and p.detect_min == 4


def test_battery_frames():
    p = profiles.find("zapi-chinese-ac")
    pack = bytes.fromhex("1202147DF5FAFFFF")              # 53.0 V, 2.0 A out, 245 of 250 Ah
    assert _sig(p, "Battery voltage").decode(pack) == 53.0
    assert round(_sig(p, "Battery current (+ = discharge)").decode(pack), 1) == 2.0
    assert _sig(p, "Remaining capacity ?").decode(pack) == 245
    assert _sig(p, "Full capacity ?").decode(pack) == 250
    lifting = bytes.fromhex("0702F481F5FAFFFF")           # 0x81F4 - 32000 = 126.8 A
    assert round(_sig(p, "Battery current (+ = discharge)").decode(lifting), 1) == 126.8
    cells = bytes.fromhex("E70CE80CE90CEB0C")
    assert _sig(p, "Cell 1 voltage").decode(cells) == 3.303
    assert _sig(p, "Cell 4 voltage").decode(cells) == 3.307
    extremes = bytes.fromhex("3434ED0CE40CFFFF")
    assert _sig(p, "Highest cell temperature ?").decode(extremes) == 12
    assert _sig(p, "Highest cell voltage").decode(extremes) == 3.309
    assert round(_sig(p, "Lowest cell voltage").decode(extremes), 3) == 3.3


def test_operator_inputs_and_traction():
    p = profiles.find("zapi-chinese-ac")
    fwd_brake = bytes.fromhex("2E2F0000003D042F")         # pedal 4.6 V, forward, seat, park brake, brake
    assert round(_sig(p, "Accelerator pedal ?").decode(fwd_brake), 1) == 4.6
    assert _sig(p, "Forward selected").decode(fwd_brake) == 1
    assert _sig(p, "Reverse selected").decode(fwd_brake) == 0
    assert _sig(p, "Seat switch").decode(fwd_brake) == 1
    assert _sig(p, "Parking brake").decode(fwd_brake) == 1
    assert _sig(p, "Brake pedal").decode(fwd_brake) == 1
    assert _sig(p, "Reverse selected").decode(bytes.fromhex("0300000000260400")) == 1
    driving = bytes.fromhex("366105D200080841")           # 0x05D2 high byte first = 1490
    assert _sig(p, "Traction motor speed ?").decode(driving) == 1490
    assert _sig(p, "Battery charge").decode(driving) == 97
    assert _sig(p, "Traction running").decode(driving) == 1
    assert _sig(p, "Traction motor speed ?").decode(bytes.fromhex("2661F0CE00080841")) == -3890
    assert _sig(p, "Travel speed").decode(bytes.fromhex("0028000000000000")) == 4.0   # the 4 km/h peak


def test_error_code_and_hour_meters():
    p = profiles.find("zapi-chinese-ac")
    code = _sig(p, "Active error code")
    assert code.decode(bytes.fromhex("1E6E0000004F0062")) == 79
    assert dict(code.enum)[79] == "79 Incorrect start"
    assert _sig(p, "Traction alarm code").decode(bytes.fromhex("08004F4F000C0000")) == 79
    hours = bytes.fromhex("A4C7020092030100")             # 0x02C7A4 and 0x010392 tenths of a second
    assert round(_sig(p, "Key-on hours ?").decode(hours), 2) == 5.06
    assert round(_sig(p, "Seat hours ?").decode(hours), 2) == 1.85


def test_hydraulics():
    p = profiles.find("zapi-chinese-ac")
    lift = bytes.fromhex("0C030C036C033000")
    assert _sig(p, "Lift / lower lever ?").decode(lift) == 876
    assert _sig(p, "Pump speed request ?").decode(lift) == 780
    assert _sig(p, "Pump requested ?").decode(lift) == 1
    pump = bytes.fromhex("000040007BF76400")
    assert _sig(p, "Lift").decode(pump) == 1
    assert _sig(p, "Pump command ?").decode(pump) == 100
    assert _sig(p, "Pump motor speed ?").decode(pump) == -2181
