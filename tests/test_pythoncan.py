"""The optional python-can backend, against python-can's own in-process 'virtual' bus."""

import time

import pytest

from canopener.can import pythoncan
from canopener.can.backends import BackendError
from canopener.can.frame import CanFrame


def test_spec_names():
    assert pythoncan.is_spec("pcan:PCAN_USBBUS1") and not pythoncan.is_spec("can0") and not pythoncan.is_spec("")
    assert pythoncan.parse_spec("pcan:PCAN_USBBUS1") == ("pcan", "PCAN_USBBUS1", {})
    assert pythoncan.parse_spec("slcan:/dev/ttyACM0;tty_baudrate=115200;fd=false") == (
        "slcan", "/dev/ttyACM0", {"tty_baudrate": 115200, "fd": False})
    assert pythoncan.parse_spec("socketcand:;host=10.0.0.2;channel=can0") == (
        "socketcand", None, {"host": "10.0.0.2", "channel": "can0"})
    for bad in (":x", "pcan:x;nokey", "pcan:x;=1"):
        with pytest.raises(ValueError):
            pythoncan.parse_spec(bad)


def test_without_python_can_it_says_how_to_get_it(monkeypatch):
    from canopener.core import link
    monkeypatch.setattr(pythoncan, "available", lambda: False)
    with pytest.raises(BackendError, match="pip install"):
        pythoncan.PythonCanBackend("pcan:PCAN_USBBUS1").open()
    assert pythoncan.detect() == []
    assert link.adapter_status("socketcan", "pcan:PCAN_USBBUS1").level == link.BAD


@pytest.fixture
def can():
    """python-can itself: the tests below need it installed."""
    return pytest.importorskip("can")


def test_scan_and_clear_through_python_can(can):
    from canopener.core import connection, j1939_clear, link, scan
    from canopener.sim import MachineSimulator
    sim = MachineSimulator(pythoncan.PythonCanBackend("virtual:bench1"), seed=1)
    sim.start()
    conn = connection.open_connection(connection.SOCKETCAN, "virtual:bench1", bitrate=250000)
    try:
        time.sleep(1.0)
        assert conn.describe() == "virtual:bench1 at 250k (python-can)" and conn.session.rx_count > 20
        result = scan.run_preset(conn.session, "full")            # single frames, BAM and RTS/CTS
        assert {(r.sa, r.spn) for r in result.rows} >= {(0x00, 3719), (0x00, 97), (0x27, 522001)}
        assert any(i.label == "Component ID" for i in result.info())
        r = j1939_clear.clear_codes(conn.session, j1939_clear.STORED, da=0x27)
        assert [(a.sa, a.control) for a in r.answers] == [(0x27, 0)]
        assert link.adapter_status("socketcan", "virtual:bench1").level == link.OK
    finally:
        conn.close()
        sim.stop()


def test_listen_only_never_transmits(can):
    from canopener.core import connection, j1939_clear
    peer = can.Bus(interface="virtual", channel="bench2")
    conn = connection.open_connection(connection.SOCKETCAN, "virtual:bench2", listen_only=True, bitrate=500000)
    try:
        assert conn.session.listen_only and not conn.session.request(0xFECA)
        assert j1939_clear.clear_codes(conn.session, j1939_clear.BOTH).refused
        assert peer.recv(0.3) is None                               # nothing reached the bus
    finally:
        conn.close()
        peer.shutdown()


def test_frames_both_ways_error_frames_and_remote_frames(can):
    peer = can.Bus(interface="virtual", channel="bench3")
    b = pythoncan.PythonCanBackend("virtual:bench3", 250000)
    b.open()
    try:
        b.send(CanFrame(0x18EAFFF9, b"\xca\xfe\x00", True))
        msg = peer.recv(1.0)
        assert (msg.arbitration_id, bytes(msg.data), msg.is_extended_id) == (0x18EAFFF9, b"\xca\xfe\x00", True)
        before = time.time()
        peer.send(can.Message(arbitration_id=0x7E8, data=b"\x02\x41\x0c", is_extended_id=False))
        peer.send(can.Message(arbitration_id=0x123, is_remote_frame=True, is_extended_id=False))
        peer.send(can.Message(arbitration_id=0x20000004, data=b"\x00\x04", is_error_frame=True))
        fr = b.recv()
        assert (fr.can_id, fr.data, fr.extended) == (0x7E8, b"\x02\x41\x0c", False) and fr.timestamp >= before
        assert b.recv() is None                                     # remote frame: ignored
        err = b.recv()
        assert err.error and "controller" in err.error.lower()
    finally:
        b.close()
        peer.shutdown()


def test_bad_specs_and_auto_bitrate_are_explained(can):
    with pytest.raises(BackendError, match="Pick a bitrate"):
        pythoncan.PythonCanBackend("virtual:x", bitrate=0).open()
    with pytest.raises(BackendError, match="nosuchbus"):
        pythoncan.PythonCanBackend("nosuchbus:0").open()
    assert isinstance(pythoncan.detect(("virtual",), timeout=0.5), list)
