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
    driving = bytes.fromhex("366105D200080841")           # 0x05D2 high byte first = 14.90 Hz
    assert round(_sig(p, "Traction frequency ?").decode(driving), 2) == 14.9
    assert _sig(p, "Battery charge").decode(driving) == 97
    assert _sig(p, "Traction running").decode(driving) == 1
    assert round(_sig(p, "Traction frequency ?").decode(bytes.fromhex("2661F0CE00080841")), 2) == -38.9
    wheels = bytes.fromhex("0081FFFFA2FEA3FE")            # both drive motors about 3.5 km/h in reverse
    assert round(_sig(p, "Drive motor 1 speed ?").decode(wheels), 2) == -3.5
    assert round(_sig(p, "Drive motor 2 speed ?").decode(wheels), 2) == -3.49
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
    assert _sig(p, "Pump frequency request").decode(lift) == 78.0
    assert _sig(p, "Pump requested ?").decode(lift) == 1
    pump = bytes.fromhex("000040007BF76400")
    assert _sig(p, "Lift").decode(pump) == 1
    assert _sig(p, "Lift speed command").decode(pump) == 100
    assert _sig(p, "Pump motor speed").decode(pump) == 2181         # sent negative, shown positive
    freq = bytes.fromhex("3632E18800080843")              # 0xE188 = -7800 -> 78.00 Hz = 2340 rpm on 4 poles
    assert round(_sig(p, "Pump motor frequency").decode(freq), 2) == 78.0
    assert _sig(p, "Tilt request").decode(bytes.fromhex("0000080000000000")) == 1
    assert _sig(p, "Sideshift / accessory 1").decode(bytes.fromhex("0000100000000000")) == 1


def test_zapi_alarm_and_emcy_codes():
    """Code text is from ZAPI's ACE2 2uC manual (AF2ZP0DA, section 10.1), reworded."""
    p = profiles.find("zapi-chinese-ac")
    alarms = dict(_sig(p, "Traction alarm code").enum)
    assert alarms[221] == "221 Drive requested with the handbrake on"   # flashed before 79 on the truck
    assert alarms[224] == "224 Waiting for another node on the bus"     # the blip at key-on
    assert alarms[79] == "79 Incorrect start"
    assert dict(_sig(p, "Pump alarm code").enum) == alarms == dict(_sig(p, "Active error code").enum)
    handbrake = bytes.fromhex("0800DD2E000C0000")         # ZAPI code 221, MDI / LED number 46
    assert _sig(p, "Traction alarm code").decode(handbrake) == 221
    assert _sig(p, "Traction alarm (MDI / LED number)").decode(handbrake) == 46
    assert p.emcy[0xFF4F] == "Incorrect start" and p.emcy[0xFFDD] == "Drive requested with the handbrake on"
    assert 0x0000 not in p.emcy
