"""can.kwp: a KWP2000 session addressed to one module, against the simulated adapter."""

import pytest

from canopener.can import elm327, kwp
from canopener.sim.elm import SimulatedCar


def _backend(vehicle):
    car = SimulatedCar(vehicle=vehicle)
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket))
    b.open()
    return car, b


def test_session_wakes_the_module_reads_and_hands_the_adapter_back():
    car, b = _backend("kwp")
    try:
        s = kwp.KwpSession(b, 0x10, tester=0xFC, settle=0)
        s.open()
        sent = car.adapter.commands
        assert "ATSH8110FC" in sent and "ATWM8110FC3E" in sent and sent.index("ATFI") > sent.index("ATSH8110FC")
        assert s.is_open and s.key_bytes == "1:8F 2:E9"
        assert s.request(bytes.fromhex("2211010401")) == bytes.fromhex("62110150")
        assert s.request(b"\x3e") == b"\x7e"
        with pytest.raises(kwp.KwpNegative) as e:
            s.request(bytes.fromhex("2212000401"))
        assert (e.value.service, e.value.code) == (0x22, 0x31) and "out of range" in str(e.value)
        s.close()
        assert not s.is_open and b.protocol == "5" and car.adapter.commands[-1] == "ATDPN"
        with pytest.raises(kwp.KwpError):
            s.request(b"\x3e")
    finally:
        b.close()
        car.stop()


def test_session_reports_a_module_that_does_not_wake_and_restores_the_adapter():
    car, b = _backend("iso9141")
    try:
        s = kwp.KwpSession(b, 0x10, settle=0)
        with pytest.raises(kwp.KwpError) as e:
            s.open()
        assert "didn't wake up" in str(e.value) and not s.is_open
        assert b.protocol == "3"                    # ordinary OBD-II works again
    finally:
        b.close()
        car.stop()


def test_session_refuses_a_can_car():
    car, b = _backend("11")
    try:
        with pytest.raises(kwp.KwpError):
            kwp.KwpSession(b, 0x10)
    finally:
        b.close()
        car.stop()
