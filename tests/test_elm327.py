"""Bluetooth OBD adapters (ELM327-style): the text protocol, the backend over the simulated adapter,
what it refuses, and the OBD-II / UDS clients running through it unchanged."""

import time

import pytest

from canopener import obd, uds
from canopener.can import elm327
from canopener.can.frame import CanFrame
from canopener.core import connection, link
from canopener.core.obd import ObdClient
from canopener.core.session import Session
from canopener.core.uds import UdsClient
from canopener.sim.elm import SimulatedCar

ECM, TCM = 0x7E8, 0x7E9


# --- pure parsing ------------------------------------------------------------------
def test_channels():
    assert elm327.parse_channel("aa:bb:cc:dd:ee:ff") == ("bt", "AA:BB:CC:DD:EE:FF", 1)
    assert elm327.parse_channel("AA:BB:CC:DD:EE:FF/3") == ("bt", "AA:BB:CC:DD:EE:FF", 3)
    assert elm327.parse_channel("tcp:192.168.0.10:35000") == ("tcp", "192.168.0.10", 35000)
    assert elm327.parse_channel("/dev/rfcomm0") == ("serial", "/dev/rfcomm0", 38400)
    assert elm327.parse_channel("/dev/ttyUSB0@115200") == ("serial", "/dev/ttyUSB0", 115200)
    for bad in ("", "can0", "AA:BB:CC", "tcp:host", "AA:BB:CC:DD:EE:FF/x"):
        with pytest.raises(ValueError):
            elm327.parse_channel(bad)


def test_reply_lines_and_frames():
    raw = b"010C\r\rSEARCHING...\r7E8 04 41 0C 1A F8 55 55 55\r7E9064100BE3EB811\r\r"
    lines = elm327.split_reply(raw, "010C")
    assert lines == ["7E8 04 41 0C 1A F8 55 55 55", "7E9064100BE3EB811"]
    assert elm327.parse_frame_line(lines[0], False) == (0x7E8, bytes.fromhex("04410C1AF8555555"))
    assert elm327.parse_frame_line(lines[1], False) == (0x7E9, bytes.fromhex("064100BE3EB811"))
    assert elm327.parse_frame_line("18 DA F1 10 03 41 0D 32", True) == (0x18DAF110, bytes.fromhex("03410D32"))
    for not_a_frame in ("NO DATA", "OK", "7E8", "7E8 0", "ELM327 v2.1", "12.6V"):
        assert elm327.parse_frame_line(not_a_frame, False) is None
    assert elm327.classify(["NO DATA"]) == "NO DATA"
    assert elm327.classify(["7E8 03 7F 01 12", "CAN ERROR"]) == "CAN ERROR"
    assert elm327.classify(["?"]) == "?"
    assert elm327.classify(["7E8 03 41 0D 00"]) is None
    assert elm327.is_physical(0x7E0, False) and not elm327.is_physical(0x7DF, False)
    assert elm327.is_physical(0x18DA10F1, True) and not elm327.is_physical(0x18DB33F1, True)


# --- the backend over the simulated adapter ------------------------------------------
@pytest.fixture
def elm():
    conn = connection.open_connection(connection.ELM_SIM)
    yield conn
    conn.close()


def test_connecting_identifies_the_adapter_and_the_vehicle(elm):
    b = elm.session.backend
    assert isinstance(b, elm327.ElmBackend) and b.answers_only
    assert b.version == "ELM327 v2.1" and b.voltage == "12.6V" and not b.stn
    assert b.protocol == "6" and b.protocol_name.startswith("ISO 15765-4 CAN (11 bit")
    assert not elm.session.listen_only                     # it has to ask, so never listen-only
    cmds = elm.simulator.adapter.commands
    assert cmds.index("ATZ") < cmds.index("ATH1") < cmds.index("0100")   # headers on before the search
    assert "Bluetooth OBD simulator" in elm.describe()


def test_obd_client_runs_unchanged_through_the_adapter(elm):
    c = ObdClient(elm.session)
    try:
        assert c.detect_addressing() is obd.ELEVEN_BIT
        assert c.read_vin() == "1HGCM82633A004352"          # multi-frame: the adapter does the flow control
        by_ecu = c.read_dtcs_by_ecu()
        assert by_ecu[ECM] == {"stored": ["P0133", "P0420"], "pending": ["P0171"], "permanent": ["P0420"]}
        assert by_ecu[TCM]["stored"] == ["P0730"]
        live = {r.name: (r.value, r.unit) for r in c.read_live()}
        assert live["Control module voltage"] == (14.1, "V") and "Engine speed" in live
        assert c.read_readiness()[ECM].mil
        assert len(c.read_test_results()) == 4
        assert c.ecu_names[ECM] == "ECM"
        # single-PID live reads to one module ask the adapter to stop after one answer
        assert c.read_pid(ECM, 0x0C) is not None
        assert elm.simulator.adapter.commands[-1] == "010C1"
        # Mode 04: the engine answers 'response pending' first; the adapter waits for the real answer
        result = c.clear_dtcs()
        assert result[ECM] == (True, "cleared") and elm.simulator.cleared
        cmds = elm.simulator.adapter.commands
        assert cmds[cmds.index("04") - 1] == "ATSTFF" and cmds[cmds.index("04") + 1] == "ATST32"
        assert c.read_dtcs()["stored"] == []
    finally:
        c.close()


def test_answers_reach_the_session_like_a_wired_bus(elm):
    c = ObdClient(elm.session)
    try:
        c.detect_addressing()
        c.read_pid(ECM, 0x0D)
        time.sleep(0.3)
        frames = list(elm.session.raw)
        tx = [f for f in frames if f.tx]
        rx = [f for f in frames if not f.tx and not f.error]
        assert any(f.can_id == 0x7E0 and f.data[:3] == bytes([0x02, 0x01, 0x0D]) for f in tx)
        assert any(f.can_id == ECM and f.data[1:3] == bytes([0x41, 0x0D]) for f in rx)
        assert elm.session.backend.requests >= 2 and elm.session.backend.answers >= 2
    finally:
        c.close()


def test_uds_through_the_adapter_and_its_receive_filter(elm):
    u = UdsClient(elm.session)
    try:
        assert u.read_data_by_identifier(0xF190) == b"1HGCM82633A004352"
        assert [d[0] for d in u.read_dtcs()] == ["C0561-00", "P0730-00"]
    finally:
        u.close()
    other = UdsClient(elm.session, request_id=0x726, timeout=0.5)    # a body module: answers on 0x72E
    try:
        with pytest.raises(uds.UdsError):
            other.read_dtcs()                               # nothing there in the simulated car
        cmds = elm.simulator.adapter.commands
        assert "ATSH726" in cmds and "ATCRA72E" in cmds      # its answers let through the adapter's filter
    finally:
        other.close()
    back = ObdClient(elm.session)
    back.query_ecu(ECM, 0x01, 0x0D)                         # back to the engine: automatic filter again
    back.close()
    cmds = elm.simulator.adapter.commands
    after = cmds[len(cmds) - 1 - cmds[::-1].index("ATCRA72E"):]
    assert after[after.index("ATAR") - 1] in ("ATSH7DF", "ATSH7E0")


def test_what_the_adapter_refuses(elm):
    s = elm.session
    assert not s.send_frame(CanFrame(0x7E0, bytes([0x10, 0x0A, 0x2E, 0xF1, 0x90, 1, 2, 3]), extended=False))
    assert "single-frame" in s.health.tx_error              # multi-frame requests
    assert not s.send_frame(CanFrame(0x123, bytes([0xAA, 0xBB, 0xCC]), extended=False))
    assert "raw frames" in s.health.tx_error                # anything that isn't an ISO-TP request
    assert not s.request(0xFECA)                            # J1939 requests (29-bit, on an 11-bit car)
    assert s.send_frame(CanFrame(0x7E0, bytes([0x30, 0, 0]), extended=False))   # flow control: dropped


def test_the_count_digit_falls_back_when_an_adapter_rejects_it(elm):
    sim = elm.simulator.adapter
    plain = sim._request

    def no_count_digit(c):
        return ["?"] if len(c) % 2 else plain(c)
    sim._request = no_count_digit
    c = ObdClient(elm.session)
    try:
        c.detect_addressing()
        assert c.read_pid(ECM, 0x0C) is not None
        assert not elm.session.backend.fast_single and sim.commands[-2:] == ["010C1", "010C"]
        assert c.read_pid(ECM, 0x0D) is not None and sim.commands[-1] == "010D"
    finally:
        c.close()


def _manual(addressing="11", **sim_patch):
    car = SimulatedCar(addressing=addressing)
    for k, v in sim_patch.items():
        setattr(car.adapter, k, v)
    car.start()
    backend = elm327.ElmBackend("test", stream=elm327.SocketStream(car.adapter.host_socket))
    return car, backend


def test_29_bit_vehicles():
    car, backend = _manual("29")
    session = Session(backend)
    session.start()
    c = ObdClient(session)
    try:
        assert backend.protocol == "7" and backend.extended
        assert c.detect_addressing() is obd.TWENTY_NINE_BIT
        assert c.read_vin() == "1HGCM82633A004352"
        assert c.read_pid(0x18DAF110, 0x0C) is not None
        assert "ATCP18" in car.adapter.commands and "ATSHDA10F1" in car.adapter.commands
    finally:
        c.close()
        session.stop()
        car.stop()


@pytest.mark.parametrize("vehicle,protocol", [("pwm", "1"), ("vpw", "2"), ("iso9141", "3"), ("kwp", "5")])
def test_non_can_cars_read_like_can_ones(vehicle, protocol):
    conn = connection.open_connection(connection.ELM_SIM, f"sim:{vehicle}")
    c = ObdClient(conn.session)
    try:
        b = conn.session.backend
        assert b.noncan and b.protocol == protocol and not b.extended
        assert c.detect_addressing() is obd.ELEVEN_BIT
        assert c.read_vin() == "1HGCM82633A004352"          # five numbered messages, put back together
        by_ecu = c.read_dtcs_by_ecu()
        assert by_ecu[ECM]["stored"] == ["P0133", "P0420"] and by_ecu[TCM]["stored"] == ["P0730"]
        assert c.ecu_label(ECM) == "ECM (engine (0x10))" and c.ecu_label(TCM) == "TCM (transmission (0x18))"
        assert {r.name for r in c.read_live()} >= {"Engine speed", "Vehicle speed"}
        assert c.read_readiness()[ECM].mil
        assert c.read_test_results() == []                  # Mode 06's older layout: not asked for
        assert "Mode 06" in conn.session.health.tx_error
        assert c.clear_dtcs()[ECM] == (True, "cleared") and conn.simulator.cleared
        u = UdsClient(conn.session, timeout=0.3)
        with pytest.raises(uds.UdsError):
            u.read_dtcs()                                   # UDS needs a CAN car
        u.close()
        assert "UDS" in conn.session.health.tx_error
        if protocol in "345":
            assert "BUS INIT" not in " ".join(conn.simulator.adapter.commands)   # (a reply line, not a command)
    finally:
        c.close()
        conn.close()


def test_noncan_messages_become_can_answers():
    from canopener.obd import noncan
    assert noncan.parse_message("48 6B 10 41 0C 1A F8 C3") == (0x10, bytes.fromhex("410C1AF8"))
    assert noncan.parse_message("85 F1 10 41 0C 1A F8 00 00 C3", "5") is None   # KWP length says 5, there are 6
    assert noncan.parse_message("NO DATA") is None
    codes = noncan.normalize(b"\x03", [(0x10, bytes.fromhex("43013304200171")),
                                       (0x10, bytes.fromhex("43017100000000")),
                                       (0x18, bytes.fromhex("43000000000000"))])
    assert codes == {0x10: bytes.fromhex("43040133042001710171"), 0x18: bytes.fromhex("4300")}
    assert noncan.normalize(b"", [(0x10, bytes.fromhex("4901 05".replace(" ", "")))]) == {}   # message counts
    assert noncan.normalize(b"", [(0x10, bytes.fromhex("490001543F0000"))])[0x10] == bytes.fromhex("4900543F0000")
    assert noncan.normalize(b"", [(0x10, bytes.fromhex("7F0112"))])[0x10] == bytes.fromhex("7F0112")
    ids = noncan.IdMap()
    assert [ids.response_id(a) for a in (0x18, 0x10, 0x33)] == [0x7E9, 0x7E8, 0x7EC]
    assert ids.label(0x7EC) == "module 0x33" and ids.label(0x7E9) == "transmission (0x18)"
    for body in (bytes.fromhex("430201330420"), bytes([0x49, 0x02, 0x01]) + b"1HGCM82633A004352",
                 bytes.fromhex("410C1AF8"), bytes([0x49, 0x0A, 0x01]) + b"ECM\x00-EngineControl".ljust(20, b"\x00")):
        assert noncan.normalize(b"", [(0x10, m) for m in noncan.to_noncan(body)])[0x10] == body


# --- J1939 through the adapter (protocol A) --------------------------------------------
def test_j1939_listening_and_requests():
    conn = connection.open_connection(connection.ELM_SIM, "sim:j1939")
    s, b = conn.session, conn.session.backend
    try:
        assert b.j1939 and b.protocol == "A" and b.extended and not b.answers_only and not s.listen_only
        deadline = time.time() + 5
        while (len(s.ecus) < 2 or not s.latest_for(0xF004)) and time.time() < deadline:
            time.sleep(0.05)
        assert "ATCAF0" in conn.simulator.adapter.commands and "ATMA" in conn.simulator.adapter.commands
        assert {0x00, 0x27} <= set(s.ecus) and s.latest_for(0xF004)       # broadcasts heard
        assert s.request(0xFEDA, 0x00)                       # software ID: a multi-packet answer
        deadline = time.time() + 5
        while not s.latest_for(0xFEDA) and time.time() < deadline:
            time.sleep(0.05)
        assert s.latest_for(0xFEDA)[0][2].startswith(b"\x02EMR4")
        assert b.read_voltage() == "12.6V"                   # monitoring pauses for it, then carries on
        n = s.rx_count
        time.sleep(0.5)
        assert s.rx_count > n
        assert not s.send_frame(CanFrame(0x18FF00F9, bytes(8), extended=True))     # only Requests
        assert "only sends Requests" in s.health.tx_error
        assert link.adapter_status(connection.ELM, "Truck", backend=b).level == link.OK
        assert link.bus_status(s).level == link.OK
    finally:
        conn.close()


def test_j1939_listen_only_sends_nothing():
    conn = connection.open_connection(connection.ELM_SIM, "sim:j1939", listen_only=True)
    try:
        s = conn.session
        assert s.listen_only and not s.request(0xFEDA, 0x00)
        assert not any(c.startswith("ATSH") for c in conn.simulator.adapter.commands)
    finally:
        conn.close()


def test_a_car_connection_is_never_listen_only():
    conn = connection.open_connection(connection.ELM_SIM, "sim", listen_only=True)
    try:
        assert not conn.session.listen_only
    finally:
        conn.close()


# --- probe and console -----------------------------------------------------------------
def test_probe_reports_what_the_adapter_does(elm):
    from canopener.can import elm_probe
    b = elm.session.backend
    items = elm_probe.probe(b, protocols=True, listen=0.3)
    got = {(i.section, i.name): i for i in items}
    assert got[("Adapter", "Chip / version")].status == "ELM327 v2.1"
    assert got[("Adapter", "STN (OBDLink-style) commands")].status == "no"
    assert got[("Adapter", "Battery voltage at the OBD port")].status == "12.6V"
    assert got[("Commands", "J1939 protocol (ATSP A, 250k)")].status == "yes"
    assert got[("Commands", "'Expect one answer' digit (faster live data)")].status == "yes"
    assert got[("Protocols", "ISO 15765-4 CAN (11 bit, 500 kbaud)")].status == "yes"
    assert got[("Protocols", "SAE J1850 VPW (10.4 kbaud)")].status == "no"
    assert got[("Listening", "ISO 15765-4 CAN (11 bit, 500 kbaud)")].status.endswith("IDs")
    text = "\n".join(elm_probe.report_lines(items))
    assert text.startswith("Adapter") and "\nProtocols\n" in text
    c = ObdClient(elm.session)                             # the adapter still works afterwards
    try:
        c.detect_addressing()
        assert c.read_vin() == "1HGCM82633A004352"
    finally:
        c.close()


def test_console_allows_adapter_commands_only():
    assert elm327.console_refusal("ATI") == "" and elm327.console_refusal("st di") == ""
    assert "Only adapter commands" in elm327.console_refusal("0100")
    assert "Only adapter commands" in elm327.console_refusal("14 FF FF FF")
    assert "stored settings" in elm327.console_refusal("AT PP 0C SV 23")
    assert "serial speed" in elm327.console_refusal("ATBRD 23")
    assert "transmits" in elm327.console_refusal("STPX h:7E0, d:0101")
    assert elm327.is_monitor_command("AT MA") and elm327.is_monitor_command("ATMP FECA")
    assert not elm327.is_monitor_command("ATI")


def test_the_battery_voltage_is_a_meter_channel(elm):
    from canopener.core import meters
    from canopener.core.measure import MeasurementHub
    hub = MeasurementHub()
    src = meters.ObdVoltageSource(elm.session.backend, rate=20)
    hub.add_source(src)
    deadline = time.time() + 3
    while hub.latest("obd.battery") is None and time.time() < deadline:
        time.sleep(0.05)
    hub.remove_source("obd")
    assert hub.latest("obd.battery").value == 12.6 and src.status.text == "Reading"
    assert meters.parse_volts("12.6V") == 12.6 and meters.parse_volts(" 13.9 V") == 13.9
    assert meters.parse_volts("?") is None and meters.parse_volts("") is None


def test_a_dropped_link_shows_on_the_lamps(elm):
    s, b = elm.session, elm.session.backend
    assert link.adapter_status(connection.ELM, "vLinker", backend=b).level == link.OK
    elm.simulator.adapter.stop()                            # out of range / unplugged
    assert not s.send_frame(CanFrame(0x7DF, bytes([0x02, 0x01, 0x00]), extended=False))
    deadline = time.time() + 3
    while not s.health.link_error and time.time() < deadline:
        time.sleep(0.05)
    assert s.health.link_error
    st = link.adapter_status(connection.ELM, "vLinker", backend=b)
    assert st.level == link.BAD and "vLinker" in st.detail
    assert link.bus_status(s).level == link.BAD


def test_lamps_word_an_answers_only_adapter():
    assert link.adapter_status(connection.ELM, "").level == link.BAD             # none picked
    st = link.adapter_status(connection.ELM, "vLinker (truck)")
    assert st.level == link.OFF and "vLinker (truck)" in st.text
    assert link.adapter_status(connection.ELM_SIM, "").level == link.SIM
    conn = connection.open_connection(connection.ELM_SIM)
    try:
        s = conn.session
        now = time.time()
        s.last_rx_t = 0
        assert link.bus_status(s, now).text == "Bus: no answer yet"
        s.last_rx_t = now - 10
        assert link.bus_status(s, now, last_answer=now - 10).level == link.IDLE
        assert link.bus_status(s, now, last_answer=now - 10, polling=True).text == "Bus: no answer"
        st = link.adapter_status(connection.ELM, "Sim", backend=s.backend)
        assert st.level == link.OK and "battery 12.6V" in st.detail
    finally:
        conn.close()


def test_frames_without_the_length_byte_are_repaired(elm):
    b = elm.session.backend
    b._deliver(["7E8410D32"])                               # a clone printing data without the PCI byte
    fr = b._queue.get(timeout=1)
    assert fr.data == bytes([0x03, 0x41, 0x0D, 0x32])


def test_key_off_search_isnt_repeated_for_every_request():
    car, backend = _manual()
    plain, plain_at = car.adapter._request, car.adapter._at
    car.adapter._request = lambda c: ["SEARCHING...", "UNABLE TO CONNECT"]   # the vehicle's off
    car.adapter._at = lambda a: ["A0"] if a == "DPN" else plain_at(a)
    try:
        backend.open()                                      # connects to the adapter all the same
        assert backend.protocol == "" and backend.last_message == "UNABLE TO CONNECT"
        n = len(car.adapter.commands)
        with pytest.raises(elm327.ElmError, match="UNABLE TO CONNECT"):
            backend.send(CanFrame(0x7DF, bytes([0x02, 0x01, 0x00]), extended=False))
        assert len(car.adapter.commands) == n               # no second search straight away
        car.adapter._request, car.adapter._at = plain, plain_at
        backend._search_failed = 0.0                        # later, key on: the next request finds it
        backend.send(CanFrame(0x7DF, bytes([0x02, 0x01, 0x00]), extended=False))
        assert backend.protocol == "6"
    finally:
        backend.close()
        car.stop()


# --- adapter bus setups from plugins ---------------------------------------------------------
@pytest.fixture
def bus_plugin(monkeypatch):
    from canopener import plugins
    monkeypatch.setenv("CANOPENER_PLUGINS", "fake_bus_plugin")
    plugins.discover(refresh=True)
    yield
    monkeypatch.delenv("CANOPENER_PLUGINS")
    plugins.discover(refresh=True)


def test_plugin_bus_setups(bus_plugin):
    from canopener.core import adapter_buses
    from canopener.core.settings import Settings
    values = [v for _, v, _ in adapter_buses.choices()]
    assert values[:2] == [adapter_buses.CAR, adapter_buses.J1939] and "6|demo" in values and "B|empty" in values
    assert "(not filled in yet)" in dict((v, lb) for lb, v, _ in adapter_buses.choices())["B|empty"]
    s = Settings()
    adapter_buses.choose(s, "6|demo")
    assert (s.get("elm.protocol"), s.get("elm.bus")) == ("6", "demo") and adapter_buses.label(s) == "Demo second bus"

    car = SimulatedCar()
    car.start()
    b = elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket), bus=adapter_buses.find("demo"))
    try:
        b.open()                                            # its commands run, then its protocol
        cmds = car.adapter.commands
        assert cmds.index("ATI", cmds.index("ATRV")) < cmds.index("ATSP6") and b.protocol == "6"
        b.close()
        assert car.adapter.commands[-1] == "ATD"            # undone on close
    finally:
        car.stop()
    with pytest.raises(elm327.ElmError, match="haven't been filled in"):
        _open_with_bus(adapter_buses.find("empty"))
    with pytest.raises(elm327.ElmError, match="refused"):
        _open_with_bus(elm327.BusSetup("bad", "Bad", commands=["0100"]))
    with pytest.raises(elm327.ElmError, match="is the plugin"):
        connection.open_connection(connection.ELM, "AA:BB:CC:DD:EE:FF", elm_bus="nope")


def _open_with_bus(bus):
    car = SimulatedCar()
    car.start()
    try:
        elm327.ElmBackend("t", stream=elm327.SocketStream(car.adapter.host_socket), bus=bus).open()
    finally:
        car.stop()


def test_raw_trace(tmp_path, monkeypatch):
    path = tmp_path / "trace.txt"
    monkeypatch.setenv(elm327.TRACE_ENV, str(path))
    conn = connection.open_connection(connection.ELM_SIM, "sim:vpw")
    try:
        c = ObdClient(conn.session)
        c.detect_addressing()
        c.read_vin()
        c.close()
    finally:
        conn.close()
    text = path.read_text()
    assert "> ATZ" in text and "> 0902" in text and "< 486B10490201" in text
