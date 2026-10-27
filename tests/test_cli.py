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


def test_obd_scan_simulator(capsys):
    assert cli.main(["--units", "imperial", "obd", "--sim", "--all"]) == 0
    out = capsys.readouterr().out
    assert "2 module(s) answered, 11-bit CAN" in out and "TCM (7E9)" in out
    assert "P0730  Incorrect Gear Ratio" in out and "Evaporative system           INCOMPLETE" in out
    assert "Catalyst B1" in out and "FAIL" in out
    assert "Engine coolant temp:" in out and "°F" in out


def test_obd_clear_needs_confirmation(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["obd", "--sim", "--clear"]) == 1                     # not confirmed: nothing sent
    assert "not confirmed" in capsys.readouterr().err
    assert cli.main(["obd", "--sim", "--clear", "--yes"]) == 0
    assert "ECM (7E8): cleared" in capsys.readouterr().out
    assert cli.main(["obd", "--sim", "--evap-test", "--yes"]) == 0
    assert "ECM (7E8): started" in capsys.readouterr().out


def test_update_command(capsys, monkeypatch):
    from canopener.core import updates
    s = updates.UpdateStatus(True, "An update is available (version 0.1.5): 1 change(s).", latest="0.1.5",
                             behind=1, commits=["Faster live data"])
    monkeypatch.setattr(updates, "check", lambda *a, **k: s)
    monkeypatch.setattr(updates, "apply", lambda *a, **k: (True, "Updated. Restart CANopener to use the new version."))
    assert cli.main(["update", "--check"]) == 0
    assert "Faster live data" in capsys.readouterr().out
    assert cli.main(["update", "--yes"]) == 0
    assert "Updated." in capsys.readouterr().out


def test_capture_trim_command(tmp_path, capsys):
    import os

    from canopener.core import capture as cap
    folder = tmp_path / "cap"
    folder.mkdir()
    t0 = 1_700_000_000.0
    with open(folder / cap.CAPTURE_LOG, "w") as f:
        for s in range(1201):
            f.write(f"({t0 + s:.6f}) can0 18FEF100#0011223344556677\n")
    c = cap.Capture("bench", plan=["fault"])
    c.folder, c.started, c.stopped, c.state = str(folder), t0, t0 + 1200, cap.STOPPED
    c.tag(0, t0 + 900)
    c._write_state()
    assert cli.main(["capture", str(folder), "--trim", "2", "1", "--keep-full"]) == 0
    out = capsys.readouterr()
    assert "Trimmed: kept 181 frames" in out.err and "Trimmed      to 2 min before" in out.out
    saved = [d for d in tmp_path.iterdir() if d.name.endswith("_bench")]    # saving moved it to its final name
    assert len(saved) == 1 and os.path.exists(saved[0] / cap.FULL_LOG)


def test_clear_j1939_codes_on_the_simulator(capsys, monkeypatch):
    assert cli.main(["clear", "--sim", "--stored", "--module", "27", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "27 Management computer #1: DM3 ACK" in out
    assert "27 Management computer #1  no codes reported" in out and "Water in fuel" in out   # engine kept its
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)                                     # without --yes:
    assert cli.main(["clear", "--sim"]) == 1                                                   # not confirmed
    assert "not confirmed" in capsys.readouterr().err
    assert cli.main(["clear", "--sim", "--module", "zz", "--yes"]) == 2
