"""Profiles made from other tools' files: DBC, CANopen EDS, and a node's PDO mapping read off the
bus (against the CANopen simulator, which describes its own PDOs)."""

import json
import time

from canopener.canopen import eds as edslib
from canopener.canopen import pdo as pdolib
from canopener.core import connection, importers, profiles
from canopener.core.canopen import CanopenClient

DBC = '''VERSION ""
BO_ 2364540158 EEC1: 8 Vector__XXX
 SG_ EngineSpeed : 24|16@1+ (0.125,0) [0|8031.875] "rpm" Vector__XXX
 SG_ ActualEngineTorque : 16|8@1+ (1,-125) [-125|125] "%" Vector__XXX
 SG_ TorqueMode : 0|4@1+ (1,0) [0|15] "" Vector__XXX

BO_ 2566852608 Custom_Boom: 8 ECU27
 SG_ Boom_Angle : 7|16@0- (0.1,0) [-90|90] "deg" Vector__XXX
 SG_ Seat_Switch : 20|1@1+ (1,0) [0|1] "" Vector__XXX
 SG_ Muxed m1 : 32|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ Odd_BE : 30|12@0+ (1,0) [0|4095] "" Vector__XXX

BO_ 385 Drive_Status: 8 Node1
 SG_ Motor_Speed : 0|16@1- (1,0) [-3000|3000] "rpm" Vector__XXX
 SG_ Motor_Current : 16|16@1+ (0.1,0) [0|400] "A" Vector__XXX

VAL_ 2566852608 Seat_Switch 0 "Empty" 1 "Occupied" ;
VAL_ 2364540158 TorqueMode 0 "Low idle" 1 "Accelerator pedal" ;
'''


def test_dbc_becomes_a_profile_that_decodes():
    doc, notes = importers.dbc_profile(DBC, "Test machine")
    assert doc["protocol"] == "j1939+canopen" and len(doc["signals"]) == 7
    assert any("multiplexed" in n for n in notes) and any("big-endian" in n for n in notes)
    assert doc["pgns"] == {"0xF004": "EEC1", "0xFF10": "Custom Boom"}
    p = profiles.parse_profile(doc, "test-machine")
    by = {s.name: s for s in p.signals}
    eec1 = bytes([0xF1, 0x7D, 0x7D + 20, 0x40, 0x1F, 0xFF, 0xFF, 0xFF])
    assert by["EngineSpeed"].pgn == 0xF004 and by["EngineSpeed"].decode(eec1) == 1000.0
    assert by["ActualEngineTorque"].decode(eec1) == 20.0 and by["EngineSpeed"].sa is None     # SA FE = anyone
    assert by["TorqueMode"].text(by["TorqueMode"].decode(eec1)) == "Accelerator pedal"
    boom = bytes([0xFF, 0x38, 0x10, 0, 0, 0, 0, 0])               # -200 big-endian = -20.0 deg
    assert by["Boom Angle"].pgn == 0xFF10 and by["Boom Angle"].sa == 0x00
    assert by["Boom Angle"].decode(boom) == -20.0 and by["Boom Angle"].unit == "°"
    assert by["Seat Switch"].text(by["Seat Switch"].decode(boom)) == "Occupied"
    drive = (-1500).to_bytes(2, "little", signed=True) + (1234).to_bytes(2, "little") + bytes(4)
    assert by["Motor Speed"].can_id == 0x181 and by["Motor Speed"].decode(drive) == -1500
    assert abs(by["Motor Current"].decode(drive) - 123.4) < 1e-9


EDS = '''[DeviceInfo]
VendorName=Example Drives
ProductName=EX-100 motor controller

[1800]
ParameterName=TPDO1 communication
[1800sub1]
ParameterName=COB-ID
DataType=0x0007
DefaultValue=$NODEID+0x180

[1A00]
ParameterName=TPDO1 mapping
[1A00sub0]
ParameterName=Number of entries
DefaultValue=4
[1A00sub1]
DefaultValue=0x60410010
[1A00sub2]
DefaultValue=0x606C0020
[1A00sub3]
DefaultValue=0x00050008
[1A00sub4]
DefaultValue=0x20010108

[6041]
ParameterName=Statusword
DataType=0x0006
PDOMapping=1
[606C]
ParameterName=Velocity actual value
DataType=0x0004
PDOMapping=1
[2001]
ParameterName=Temperatures
[2001sub0]
ParameterName=Highest sub-index
[2001sub1]
ParameterName=Heatsink
DataType=0x0002
PDOMapping=1
'''


def test_eds_gives_the_devices_default_pdos_with_names():
    eds = edslib.loads(EDS)
    assert eds.product == "EX-100 motor controller" and edslib.number("$NODEID+0x180", 5) == 0x185
    pdos = edslib.tpdos(eds, 5)
    assert len(pdos) == 1 and pdos[0].can_id == 0x185 and [m.bits for m in pdos[0].mapped] == [16, 32, 8, 8]
    doc, notes = importers.eds_profile(eds, 5)
    assert not notes and doc["name"] == "EX-100 motor controller" and doc["nodes"] == {"0x05": doc["name"]}
    p = profiles.parse_profile(doc, "ex-100")
    by = {s.name: s for s in p.signals}
    assert set(by) == {"Statusword", "Velocity actual value", "Temperatures: Heatsink"}      # the padding is skipped
    frame = (0x0637).to_bytes(2, "little") + (-1200).to_bytes(4, "little", signed=True) + bytes([0, 0xF6])
    assert by["Statusword"].decode(frame) == 0x0637 and by["Velocity actual value"].decode(frame) == -1200
    assert by["Temperatures: Heatsink"].start == 7 and by["Temperatures: Heatsink"].decode(frame) == -10


def test_pdo_mapping_read_off_the_bus_decodes_the_simulators_data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    conn = connection.open_connection(connection.CANOPEN_SIM)
    client = CanopenClient(conn.session)
    try:
        time.sleep(0.8)
        maps = {n: client.read_tpdos(n) for n in (0x01, 0x02, 0x20)}
        assert [p.can_id for p in maps[0x01]] == [0x181, 0x281] and len(maps[0x01][0].mapped) == 8
        assert maps[0x01][0].mapped[-1].is_padding and maps[0x20][0].can_id == 0x1A0
        doc, notes = importers.pdo_profile(maps, "Read off the sim")
        assert not notes and doc["protocol"] == "canopen"
        path = importers.save(doc)
        assert json.load(open(path))["name"] == "Read off the sim"
        try:
            importers.save(doc)
            raise AssertionError("saved over an existing profile")
        except FileExistsError:
            pass
        ours = {s.location: s for s in profiles.parse_profile(doc, "x").signals}
        ref = profiles.find("sim-canopen")                 # the hand-written profile for the same simulator
        same = [s for s in ref.signals if s.location in ours]
        assert len(same) >= 12, [s.name for s in ref.signals if s.location not in ours]
        assert pdolib.Mapped.from_value(0x60410010) == pdolib.Mapped(0x6041, 0, 16)
    finally:
        client.close()
        conn.close()
