"""CANopen: the protocol library, the passive sniffer, the SDO / NMT client against the simulator,
mixed J1939 + CANopen buses, and CANopen in profiles, labels, Live Data, reports and the CLI."""

import pathlib
import time

import pytest

from canopener import canopen, cli
from canopener.can.backends import VirtualBackend, VirtualBus
from canopener.can.frame import CanFrame
from canopener.canopen import CanopenSniffer, cob, emcy, nmt, objects, sdo
from canopener.core import analyze, connection, labeling, profiles, report, scan
from canopener.core import canopen as co
from canopener.core.live import LiveData
from canopener.core.session import Session
from canopener.sim.canopen import BATTERY, PUMP, TRACTION, CanopenSimulator


def test_protocol_library_is_pure():
    root = pathlib.Path(canopen.__file__).parent
    src = "\n".join(p.read_text() for p in root.glob("*.py"))
    for banned in ("import canopener.core", "from ..core", "from ..gui", "from ..sim", "from ..device"):
        assert banned not in src


def test_cob_ids():
    assert cob.classify(0x000) == ("nmt", 0)
    assert cob.classify(0x080) == ("sync", 0)
    assert cob.classify(0x085) == ("emcy", 5)
    assert cob.classify(0x185) == ("tpdo1", 5) and cob.classify(0x4FF) == ("tpdo4", 0x7F)
    assert cob.classify(0x205) == ("rpdo1", 5)
    assert cob.classify(0x581) == ("sdo_tx", 1) and cob.classify(0x601) == ("sdo_rx", 1)
    assert cob.classify(0x77F) == ("heartbeat", 0x7F)
    assert cob.classify(0x7E5) == ("lss", 0)
    assert cob.classify(0x180) == (None, 0)                 # node 0 isn't a node
    assert cob.label(0x1A0) == "TPDO1 node 20" and cob.label(0x7DF) == ""


def test_nmt_and_emcy():
    assert nmt.command_frame(nmt.RESET_NODE, 5) == (0x000, b"\x81\x05")
    assert nmt.parse_command(b"\x01\x00") == (nmt.START, 0)
    with pytest.raises(ValueError):
        nmt.command_frame(0x55, 1)
    assert nmt.parse_heartbeat(b"\x05") == nmt.OPERATIONAL and nmt.parse_heartbeat(b"\x42") is None
    assert nmt.parse_heartbeat(b"\x85") == nmt.OPERATIONAL      # node-guarding toggle bit
    assert nmt.parse_heartbeat(bytes(8)) is None                # an OBD-II answer on 0x7E8: not a heartbeat
    e = emcy.decode(emcy.encode(0x8130, 0x11, b"\x01\x02"))
    assert (e.code, e.register, e.data) == (0x8130, 0x11, b"\x01\x02\x00\x00\x00")
    assert e.text == "Life guard or heartbeat error" and e.register_text == "generic, communication"
    assert emcy.describe(0x2310) == "Current, device output side"
    assert emcy.describe(0x4201) == "Device temperature" and emcy.describe(0x4000) == "Temperature"
    assert emcy.describe(0xFF42) == "Device specific" and emcy.decode(b"\x00\x00\x00").reset


def test_sdo_frames_round_trip():
    req = sdo.upload_request(0x1018, 4)
    f = sdo.parse(req, from_node=False)
    assert (f.kind, f.index, f.sub) == ("upload", 0x1018, 4)
    [one] = sdo.upload_answer(0x1018, 4, (1234).to_bytes(4, "little"))
    f = sdo.parse(one, from_node=True)
    assert f.expedited and objects.as_uint(f.data) == 1234
    short = sdo.parse(sdo.upload_answer(0x1001, 0, b"\x09")[0], from_node=True)
    assert short.data == b"\x09"                            # size indicated: 1 byte, not 4
    frames = sdo.upload_answer(0x1008, 0, b"A long device name")
    head = sdo.parse(frames[0], from_node=True)
    assert not head.expedited and head.size == 18
    segs = [sdo.parse(x, from_node=True) for x in frames[1:]]
    assert [s.toggle for s in segs] == [0, 1, 0] and segs[-1].last
    assert b"".join(s.data for s in segs) == b"A long device name"
    ab = sdo.parse(sdo.abort_answer(0x2000, 0, 0x06020000), from_node=True)
    assert ab.kind == "abort" and sdo.abort_text(ab.abort_code).startswith("Object does not exist")
    assert sdo.parse(sdo.segment_request(1), from_node=False).toggle == 1


def test_sniffer_follows_the_bus():
    sn = CanopenSniffer()
    assert sn.feed(0x7E8, bytes(8), 1.0) is None            # OBD-II answer in the heartbeat range
    for i in range(5):                                      # plain 11-bit traffic in CANopen ranges
        sn.feed(0x201, bytes(8), 0.5 + i)
        sn.feed(0x0A3, bytes(8), 0.5 + i)
    assert not sn.nodes and not sn.emcy_log
    sn.feed(0x085, emcy.encode(0x5000, 0x01), 0.9)          # an EMCY just before the first heartbeat
    sn.feed(0x705, b"\x00", 1.0)
    assert sn.emcy_log[0].emcy.code == 0x5000 and set(sn.nodes) == {5}
    sn.feed(0x085, emcy.encode(0x0000, 0x00), 1.05)
    sn.feed(0x000, b"\x01\x00", 1.1)
    for i in range(4):
        sn.feed(0x705, b"\x05", 1.5 + i * 0.5)
    node = sn.nodes[5]
    assert node.state == nmt.OPERATIONAL and node.boots == 1 and node.heartbeat_ms == 500
    assert any("NMT Start" in t for _, t in sn.events)
    sn.feed(0x085, emcy.encode(0x3220, 0x05), 3.0)
    sn.feed(0x085, emcy.encode(0x4200, 0x09), 3.1)
    assert [e.emcy.code for e in sn.active_errors()] == [0x3220, 0x4200]
    sn.feed(0x085, emcy.encode(0x0000, 0x00), 4.0)
    assert sn.active_errors() == [] and len(sn.emcy_log) == 5
    for fr in sdo.upload_answer(0x1008, 0, b"Some controller name"):       # someone else's read
        sn.feed(0x585, fr, 5.0)
    assert objects.as_text(node.value(0x1008)) == "Some controller name"
    sn.feed(0x585, sdo.abort_answer(0x1003, 0, 0x06020000), 5.1)
    assert node.aborts[(0x1003, 0)] == 0x06020000
    sn.feed(0x185, bytes(8), 5.2)
    assert 0x185 in node.cobs and node.copy().cobs[0x185].count == 1


@pytest.fixture
def canopen_bus():
    conn = connection.open_connection(connection.CANOPEN_SIM)
    time.sleep(1.6)             # the battery module's heartbeat is every second
    yield conn.session, conn.simulator
    conn.close()


def test_client_against_the_simulator(canopen_bus):
    s, sim = canopen_bus
    assert {n.node for n in s.canopen_nodes()} == {TRACTION, PUMP, BATTERY}
    assert s.has_canopen and not s.has_j1939
    assert all(n.state == nmt.OPERATIONAL for n in s.canopen_nodes())
    c = co.CanopenClient(s)
    try:
        info = dict(c.node_info(TRACTION))
        assert info["Manufacturer device name"] == "SIM TRACTION CONTROLLER"            # segmented read
        assert "CiA 402" in info["Device type"] and info["Serial number"] == "0x00001092"
        assert c.find_nodes() == [TRACTION, PUMP, BATTERY]
        assert c.error_history(BATTERY) == []                   # no 1003 object: aborted, handled
        with pytest.raises(co.SdoAbort) as ex:
            c.upload(TRACTION, 0x2000)
        assert ex.value.code == 0x06020000
        with pytest.raises(co.SdoError):
            c.upload(0x33, 0x1000)                              # nobody there
        history = [v & 0xFFFF for v in c.error_history(TRACTION)]     # newest first; 4200 once it's happened
        assert history[-2:] == [0x3220, 0x8130] and set(history) <= {0x4200, 0x3220, 0x8130}
        stored = [x for x in co.codes(s) if x.status == "Stored"]
        assert {(x.node, x.code) for x in stored} == {(TRACTION, code) for code in history}
        c.clear_error_history(TRACTION)
        c.upload(TRACTION, 0x1003, 0)
        assert not [x for x in co.codes(s) if x.status == "Stored"]
        with pytest.raises(co.SdoAbort):                        # the sim takes no other writes
            c._download_expedited(PUMP, 0x1017, 0, b"\x10\x00")
        c.nmt(nmt.STOP, PUMP)
        time.sleep(0.7)
        assert {n.node: n.state for n in s.canopen_nodes()}[PUMP] == nmt.STOPPED
        boots = next(n for n in s.canopen_nodes() if n.node == PUMP).boots
        c.nmt(nmt.RESET_NODE, PUMP)
        time.sleep(1.2)
        pump = next(n for n in s.canopen_nodes() if n.node == PUMP)
        assert pump.boots == boots + 1 and pump.state == nmt.OPERATIONAL
        assert sim.nmt_log[-1] == (nmt.RESET_NODE, PUMP)
        time.sleep(2.5)                                         # the sim's EMCYs (at 2 s, 4 s)
        active = co.codes(s)
        assert [(x.node, x.code) for x in active if x.status == "Active"] == [(TRACTION, 0x4200)]
    finally:
        c.close()
    s.listen_only = True
    c = co.CanopenClient(s)
    with pytest.raises(co.SdoError):
        c.upload(TRACTION, 0x1000)
    with pytest.raises(co.SdoError):
        c.nmt(nmt.START)
    c.close()


def test_scans_cover_both_on_a_mixed_bus():
    from canopener.sim import MachineSimulator
    bus = VirtualBus()
    j1939 = MachineSimulator(VirtualBackend(bus, "j1939"), seed=1)
    cano = CanopenSimulator(VirtualBackend(bus, "canopen"))
    j1939.start()
    cano.start()
    s = Session(VirtualBackend(bus, "tool"))
    s.start()
    try:
        time.sleep(1.5)
        assert s.has_j1939 and s.has_canopen
        res = scan.run_preset(s, "stored")                  # no profile = J1939: nothing CANopen
        assert res.rows and not res.canopen
        assert "CANopen" not in report.build_report(res, s, full=True)
        profiles.apply(profiles.find("generic-j1939-canopen"))
        res = scan.run_preset(s, "stored")
        assert res.rows and not res.j1939_skipped                       # J1939 DM2 codes
        assert {c.code for c in res.canopen if c.node == TRACTION} >= {0x3220, 0x8130}
        text = report.build_report(res, s, full=True)
        assert "EMCY 3220" in text and "CANopen nodes" in text and "Bus survey" in text
    finally:
        s.stop()
        j1939.stop()
        cano.stop()


@pytest.mark.parametrize("pid", ["generic-canopen", "generic-j1939-canopen"])
def test_canopen_only_bus_gets_no_29bit_requests(canopen_bus, pid):
    s, _ = canopen_bus
    profiles.apply(profiles.find(pid))
    res = scan.run_preset(s, "full")
    assert res.j1939_skipped and res.response_summary() == []
    with s.lock:
        sent = [f for f in s.raw if f.tx]
    assert sent and not any(f.extended for f in sent)
    text = report.build_report(res, s, full=True)
    assert "(no DM responses received)" not in text and "Bus survey" not in text
    assert ("No J1939 traffic on this bus" in text) == (pid == "generic-j1939-canopen")
    assert "SIM PUMP CONTROLLER" in text                     # identity read by the info part


def test_protocols():
    for proto, pid in profiles.GENERIC.items():
        assert profiles.find(pid).protocol == proto
    assert profiles.speaks_j1939("j1939") and not profiles.speaks_canopen("j1939")
    assert profiles.speaks_canopen("canopen") and not profiles.speaks_j1939("canopen")
    assert profiles.speaks_j1939("j1939+canopen") and profiles.speaks_canopen("j1939+canopen")
    assert not profiles.speaks_j1939("obd2") and not profiles.speaks_canopen("obd2")
    assert profiles.find("sim-canopen").protocol == "canopen"
    mine = labeling.create("My forklift", profiles.find("generic-canopen"))
    assert profiles.find(mine).protocol == "canopen"          # a user profile inherits it


def test_profile_ids_nodes_and_big_endian():
    p = profiles.find("sim-canopen")
    assert p.nodes[0x20] == "Battery module (sim)" and not p.detect_nodes
    profiles.apply(p)
    assert canopen.node_name(1) == "Node 01 Traction controller (sim)"
    assert emcy.describe(0x4200).startswith("Controller hot")
    sig = profiles.parse_signal({"name": "x", "id": "0x181", "start": 0, "length": 2, "signed": True})
    assert sig.can_id == 0x181 and sig.ref == "ID 181" and not sig.na_check
    assert sig.decode((-5).to_bytes(2, "little", signed=True)) == -5
    be = profiles.parse_signal({"name": "y", "pgn": "0xFF10", "start": 1, "length": 2, "order": "big"})
    assert be.decode(b"\x00\x12\x34") == 0x1234 and be.location != sig.location
    buf = bytearray(4)
    be.encode_into(buf, 0x0102)
    assert bytes(buf) == b"\x00\x01\x02\x00"
    for bad in ({"name": "z", "id": "0x900", "start": 0}, {"name": "z", "pgn": "0xFF10", "start": 0, "bits": 4,
                                                            "order": "big"}):
        with pytest.raises(ValueError):
            profiles.parse_signal(bad)
    doc = {"name": "Truck", "nodes": {"0x01": "Drive"}, "detect": {"nodes": ["0x01", "0x02"]}}
    t = profiles.parse_profile(doc, "truck")
    assert profiles.detect([], [t], seen_nodes=[1, 2])[0].matched == 2
    assert profiles.detect([], [t], seen_nodes=[3]) == []
    profiles.apply(None)
    assert canopen.node_name(1) == "Node 01"


def test_labels_for_nodes_and_11_bit_signals():
    pid = labeling.create("Battery truck", profiles.find("sim-canopen"))
    labeling.set_module_name(pid, co.node_src(0x01), "Drive controller")
    labeling.put_signal(pid, {"name": "Speed BE", "id": 0x281, "start": 2, "length": 2, "order": "big"})
    doc = labeling.read_doc(pid)
    assert doc["nodes"] == {"0x01": "Drive controller"}
    assert doc["signals"][0] == {"name": "Speed BE", "id": "0x281", "start": 2, "length": 2, "order": "big"}
    p = profiles.find(pid)
    assert p.nodes[1] == "Drive controller" and p.nodes[2] == "Pump controller (sim)"
    assert labeling.module_names(pid) == {co.node_src(1): "Drive controller"}
    assert labeling.set_detect(pid, [], [1, 2]) == [co.node_src(1), co.node_src(2)]
    assert profiles.find(pid).detect_nodes == {1, 2}
    labeling.set_module_name(pid, co.node_src(0x01), "")
    assert profiles.find(pid).nodes[1] == "Traction controller (sim)"


def test_find_changes_on_11_bit_ids_and_byte_order():
    frames = []
    for i in range(20):
        a = 0x0100 + (i % 3)
        b = 0x3400 + (i % 3)
        frames.append(CanFrame(0x182, a.to_bytes(2, "big") + b"\0" * 6, False, 100 + i * 0.1))
        frames.append(CanFrame(0x182, b.to_bytes(2, "big") + b"\0" * 6, False, 200 + i * 0.1))
    frames.sort(key=lambda f: f.timestamp)
    best = analyze.find_changes(frames, (100, 102), (200, 202))[0]
    assert not best.extended and best.signal_draft()["id"] == 0x182
    pair = next(c for c in analyze.find_changes(frames, (100, 102), (200, 202)) if c.length == 2)
    assert pair.big_endian and pair.signal_draft()["order"] == "big"


def test_live_data_reports_and_profile_sim(canopen_bus):
    s, _ = canopen_bus
    profiles.apply(profiles.find("sim-canopen"))
    ld = LiveData()
    ld.sample(s)
    rows = {ld.rows[k].sig.name: ld.rows[k] for k in ld.order}
    assert {"Motor speed", "State of charge", "Lift pressure"} <= set(rows)
    soc = rows["State of charge"]
    assert ld.source_label(soc.sa) == "Node 20 Battery module (sim)" and ld.ids(soc) == ("ID 1A0", "")
    assert 5 <= soc.value <= 92
    assert any("State of charge" in line for line in report.live_snapshot(s))
    survey = "\n".join(report.canopen_survey(s))
    assert "Node 01 Traction controller (sim): Operational" in survey and "TPDO1" in survey

    conn = connection.open_connection(connection.PROFILE_SIM, profile=profiles.active())
    try:
        time.sleep(0.5)
        ld2 = LiveData()
        ld2.sample(conn.session)
        assert {ld2.rows[k].sig.name for k in ld2.order} >= {"Motor speed", "State of charge"}
    finally:
        conn.close()


def test_cli_canopen(capsys):
    assert cli.main(["--profile", "sim-canopen", "canopen", "--sim", "--listen", "2.5", "--info",
                     "--errors"]) == 0
    out = capsys.readouterr().out
    assert "Node 20 Battery module (sim): Operational" in out and "SIM PUMP CONTROLLER" in out
    assert "Active   Node 01" in out and "EMCY 3220" in out
    assert cli.main(["canopen", "--sim", "--listen", "1", "--nmt", "stop", "--node", "02"]) == 1   # not confirmed
