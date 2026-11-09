"""Pairing and naming Bluetooth OBD adapters (core.bluetooth) and the CLI for it, against a
stand-in for bluetoothctl (tests/fake_bluetoothctl.py)."""

import pytest

from canopener import cli
from canopener.core import bluetooth as bt
from canopener.core.settings import Settings

VLINKER, VLINKER_BLE, HEADPHONES = "00:1D:A5:68:98:8A", "C4:7F:0E:11:22:33", "AC:80:0A:44:55:66"


def test_controller_state_and_power(fake_bt):
    st = bt.controller()
    assert st.present and st.powered and st.name == "canopener" and st.problem() == ""
    fake_bt.set(powered=False)
    st = bt.controller()
    assert not st.powered and "Bluetooth is off" in st.problem()
    assert bt.power_on() and bt.controller().powered


def test_missing_bluetoothctl(monkeypatch):
    monkeypatch.setattr(bt, "BLUETOOTHCTL", "/nonexistent/bluetoothctl")
    assert not bt.available()
    assert "sudo apt install bluez" in bt.controller().problem()
    with pytest.raises(bt.BluetoothError):
        bt.pair(VLINKER)


def test_scan_puts_the_classic_serial_side_of_an_adapter_first(fake_bt):
    found = bt.scan(seconds=1)
    assert [d.address for d in found] == [VLINKER, VLINKER_BLE, HEADPHONES]
    classic, ble, phones = found
    assert classic.kind == "serial" and classic.usable and classic.looks_like_obd and classic.rssi == -48
    assert ble.kind == "ble" and not ble.usable and "Low Energy" in ble.kind_text
    assert phones.kind == "classic" and not phones.looks_like_obd


def test_pair_with_the_pin_and_forget(fake_bt):
    with pytest.raises(bt.BluetoothError, match="wrong PIN"):
        bt.pair(VLINKER, "0000", timeout=5)
    assert bt.pair(VLINKER, "1234", timeout=5) == "Paired"
    assert [d.address for d in bt.paired_devices()] == [VLINKER]
    assert bt.info(VLINKER).paired
    with pytest.raises(bt.BluetoothError, match="isn't in range"):
        bt.pair("11:22:33:44:55:66", timeout=5)
    assert bt.remove(VLINKER) and bt.paired_devices() == []


def test_names_and_the_adapter_picker(fake_bt):
    s = Settings()
    fake_bt.set(paired=[VLINKER, HEADPHONES])
    paired = [bt.info(a) for a in (VLINKER, HEADPHONES)]
    assert bt.adapter_choices(s, paired) == [(VLINKER, "vLinker MC-Android")]   # headphones aren't offered
    bt.set_name(s, VLINKER, "vLinker (truck)")
    s.set("elm.adapter", VLINKER)
    assert bt.label(s, VLINKER) == "vLinker (truck)"
    assert bt.adapter_choices(s, paired) == [(VLINKER, "vLinker (truck)")]
    bt.forget_name(s, VLINKER)
    assert s.get("elm.adapter") == "" and bt.label(s, VLINKER, "vLinker MC-Android") == "vLinker MC-Android"


def test_cli_bluetooth_commands(fake_bt, capsys, monkeypatch):
    assert cli.main(["bluetooth"]) == 0
    assert "No Bluetooth OBD adapters yet" in capsys.readouterr().out
    assert cli.main(["bluetooth", "scan"]) == 0
    out = capsys.readouterr().out
    assert "vLinker MC-Android" in out and "use this one" in out and "can't be used" in out
    assert cli.main(["bluetooth", "pair", VLINKER, "--name", "vLinker (truck)"]) == 0
    assert "now the Bluetooth OBD adapter" in capsys.readouterr().out
    assert Settings().get("elm.adapter") == VLINKER
    assert cli.main(["bluetooth", "list"]) == 0
    assert f"* {VLINKER}  vLinker (truck)" in capsys.readouterr().out
    assert cli.main(["bluetooth", "rename", VLINKER, "--name", "Truck"]) == 0
    assert bt.label(Settings(), VLINKER) == "Truck"
    assert cli.main(["bluetooth", "rename"]) == 2                           # no address
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["bluetooth", "forget", VLINKER]) == 1                  # not confirmed
    assert cli.main(["bluetooth", "forget", VLINKER, "--yes"]) == 0
    assert fake_bt.state()["paired"] == [] and Settings().get("elm.adapter") == ""


def test_cli_obd_and_uds_through_the_simulated_adapter(capsys):
    assert cli.main(["obd", "--elm", "sim"]) == 0
    out = capsys.readouterr().out
    assert "P0420" in out and "11-bit CAN" in out
    assert cli.main(["obd", "--elm", "sim", "--bench", "1"]) == 0
    out = capsys.readouterr().out
    assert "round trip" in out and "values/s" in out
    assert cli.main(["uds", "--elm", "sim", "--did", "F190"]) == 0
    assert "1HGCM82633A004352" in capsys.readouterr().out


def test_cli_elm_needs_an_adapter():
    with pytest.raises(SystemExit, match="No Bluetooth OBD adapter picked"):
        cli.main(["obd", "--elm"])
    with pytest.raises(SystemExit, match="not an adapter name"):
        cli.main(["obd", "--elm", "my truck"])


def test_cli_probe_console_and_j1939_scan(capsys, monkeypatch):
    import io
    assert cli.main(["bluetooth", "probe", "sim", "--protocols"]) == 0
    out = capsys.readouterr().out
    assert "Chip / version" in out and "SAE J1850 VPW (10.4 kbaud)" in out and "BLUETOOTH-OBD.md" in out
    assert cli.main(["bluetooth", "probe", "sim:vpw"]) == 0
    assert "SAE J1850 VPW" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("ATI\n0100\nATPP 0C SV 23\nquit\n"))
    assert cli.main(["bluetooth", "console", "sim"]) == 0
    out = capsys.readouterr().out
    assert "ELM327 v2.1" in out and out.count("refused") == 2
    assert cli.main(["scan", "--elm", "sim"]) == 0             # J1939 through the simulated adapter
    out = capsys.readouterr().out
    assert "SPN" in out or "spn" in out.lower()
    assert cli.main(["obd", "--elm", "sim:iso9141"]) == 0
    out = capsys.readouterr().out
    assert "P0420" in out and "engine (0x10)" in out
