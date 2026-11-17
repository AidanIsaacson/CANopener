"""Standard messages added from real captures (2026-11): the values are the ones the machines sent."""

from canopener.j1939 import constants, signals


def _decode(pgn, hexs):
    data = bytes.fromhex(hexs)
    return {s.name: (s.text(s.decode(data)) or s.decode(data)) for s in signals.SIGNALS_BY_PGN[pgn]}


def test_tsc1_is_what_another_controller_asks_the_engine_for():
    got = _decode(0x0000, "01581BFA00000000")
    assert got["Override control mode"] == "Speed control"
    assert got["Requested speed / speed limit"] == 875 and got["Requested torque / torque limit"] == 125
    # 3 is a mode here, not "not available"
    assert _decode(0x0000, "03581BFA00000000")["Override control mode"] == "Speed / torque limit"
    assert constants.pgn_label(0).startswith("TSC1")


def test_messages_from_the_lpg_truck_and_the_aftertreatment_bus():
    assert round(_decode(0xF01A, "8318FFFFFFFFFFFF")["Throttle actuator 1 command"], 1) == 15.7
    assert _decode(0xFEAF, "1B0000001B000000") == {"Trip fuel (gaseous)": 13.5, "Total fuel used (gaseous)": 13.5}
    assert _decode(0xD000, "FA00000000000000")["Instrument lighting level"] == 100
    assert _decode(0xFD9B, "3C84FFFFFFF3FFFF") == {"DEF temp 2": 20, "DEF concentration": 33.0}
    scr = _decode(0xFD3E, "9344FF9642FFFFFF")
    assert round(scr["SCR inlet temp"]) == 276 and round(scr["SCR outlet temp"]) == 260
    cab = _decode(0xE000, "66FFFFFFFFF0FFFF")
    assert cab["Regeneration inhibit switch"] == "Off" and round(cab["Requested fan speed"], 1) == 40.8


def test_et2_is_pgn_65188():
    assert 65188 == 0xFEA4 and constants.pgn_label(0xFEA4).startswith("ET2")
    got = _decode(0xFEA4, "FFFFC725FFFF9726")
    assert got["Oil temp 2"] is None and round(got["Engine ECU temp"], 1) == 29.2 and round(got["EGR temp"], 1) == 35.7
    assert 0xFE84 not in signals.SIGNALS_BY_PGN            # 65156 is ignition timing, not temperatures
