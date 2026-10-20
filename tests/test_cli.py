from canopener import cli
from canopener.can.frame import CanFrame
from canopener.can.logfile import write_log
from canopener.j1939.dtc import Dtc, encode_dm


def test_decode_command(tmp_path, capsys):
    path = tmp_path / "x.log"
    write_log(str(path), [CanFrame(0x18FECA00, encode_dm(0x04, 0xFF, [Dtc(110, 0, 2)]), True, 1.0)])
    assert cli.main(["decode", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Engine coolant temperature" in out and "Active" in out


def test_scan_simulator_imperial(capsys):
    assert cli.main(["--units", "imperial", "scan", "--sim", "--stored"]) == 0
    out = capsys.readouterr().out
    assert "Battery potential" in out


def test_profiles_command(capsys):
    assert cli.main(["profiles"]) == 0
    assert "sim-telehandler" in capsys.readouterr().out
