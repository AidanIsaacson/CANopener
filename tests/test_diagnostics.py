"""Diagnostics for the installed options: checks against a fake root and fake commands, the
report, saving, and the CLI."""

import os

from canopener.core import diagnostics as diag
from canopener.core import hardware
from canopener.core.settings import Settings


def fake_root(tmp_path, with_iface=True):
    root = tmp_path / "root"
    (root / "boot/firmware").mkdir(parents=True)
    (root / "boot/firmware/config.txt").write_text(
        "# comment\n[all]\ndtparam=spi=on\ndtoverlay=mcp251xfd,spi0-0,oscillator=20000000,interrupt=25\n"
        "dtoverlay=vc4-kms-v3d\ncamera_auto_detect=1\n")
    spi = root / "sys/bus/spi/devices/spi0.0"
    (spi / "net" / "can1").mkdir(parents=True) if with_iface else spi.mkdir(parents=True)
    iio = root / "sys/bus/iio/devices/iio:device0"
    iio.mkdir(parents=True)
    (iio / "name").write_text("ads1115\n")
    return str(root)


def commands(log):
    def run(argv):
        log.append(argv)
        if argv[0] == "dmesg":
            return 1, "dmesg: read kernel buffer failed: Operation not permitted"
        if argv[0] == "journalctl":
            return 0, ("[1.0] PV spinlocks enabled\n[2.0] spi-bcm2835 ok\n"
                       "[3.1] mcp251xfd spi0.0 can1: MCP2518FD rev0.0 (-RX_INT) successfully initialized.")
        if argv[0] == "i2cdetect":
            return None, "i2cdetect isn't installed"
        if argv[:2] == ["ip", "-details"]:
            return 0, "4: can1: <NOARP,ECHO> mtu 16 state DOWN\n    can state STOPPED"
        return 0, f"ran {' '.join(argv)}"
    return run


def test_can_checks_read_config_kernel_and_sysfs(tmp_path):
    log = []
    r = diag.Runner(root=fake_root(tmp_path), run_cmd=commands(log))
    res = {c.title: r.run(c) for c in diag.checks_for("can_a")}
    cfg = res["config.txt"].output
    assert "dtoverlay=mcp251xfd" in cfg and "[all]" in cfg and "vc4" not in cfg and "comment" not in cfg
    assert res["Interface on spi0.0"].output == "/sys/bus/spi/devices/spi0.0/net/can1"
    kernel = res["Kernel messages"]
    assert kernel.shown.startswith("journalctl -k -b")              # dmesg was restricted: fell back
    assert "successfully initialized" in kernel.output and "spinlocks" not in kernel.output
    assert ["ip", "-details", "-statistics", "link", "show", "can1"] in log
    assert "can state STOPPED" in res["Interface details"].output


def test_missing_parts_and_tools_are_reported_not_fatal(tmp_path):
    root = fake_root(tmp_path, with_iface=False)
    r = diag.Runner(root=root, run_cmd=commands([]))
    res = {c.title: r.run(c) for c in diag.checks_for("can_a") + diag.checks_for("adc_pi")}
    assert res["Interface on spi0.0"].output == "(nothing)"
    assert "no CAN interface on spi0.0" in res["Interface details"].output
    i2c = res["I2C bus 1"]
    assert not i2c.ran and "isn't installed" in i2c.output
    assert res["IIO devices"].output == "iio:device0: ads1115"
    os.remove(os.path.join(root, "boot/firmware/config.txt"))
    assert r.run(diag.checks_for("can_a")[0]).output == "not found"


def test_every_option_has_checks_and_the_meter_parts_use_its_report():
    assert set(diag.CHECKS) == set(hardware.BY_ID)
    hw = hardware.Hardware(Settings())
    r = diag.Runner(hardware=hw, run_cmd=lambda argv: (0, ""))
    assert "hasn't reported" in r.run(diag.checks_for("ohms")[0]).output
    hw.meter_report(["dac", "ohms"])
    assert r.run(diag.checks_for("ohms")[0]).output == "dac, ohms"


def test_report_and_save(tmp_path):
    hw = hardware.Hardware(Settings())
    hw.set_state("can_a", hardware.YES)
    runner = diag.Runner(root=fake_root(tmp_path), run_cmd=commands([]), hardware=hw)
    text = diag.report(diag.run(["can_a"], hw, runner), hw, "9.9", now=0)
    assert text.startswith("CANopener diagnostics, ") and "CANopener 9.9" in text
    assert "CAN stack A (can_a, stage 1)" in text and "Set to: yes" in text
    assert "$ cat /boot/firmware/config.txt" in text and "# What Installed options looks for" in text
    p1 = diag.save(text, str(tmp_path / "out"), now=0)
    p2 = diag.save(text, str(tmp_path / "out"), now=0)                  # same second: doesn't overwrite
    assert p1 != p2 and open(p1).read() == text
    assert diag.saved_reports(str(tmp_path / "out")) == sorted([p1, p2])


def test_output_is_capped_newest_kernel_lines_kept():
    text = "\n".join(f"mcp251xfd line {i}" for i in range(200))
    out = diag._filter(text, "mcp", newest=True).splitlines()
    assert len(out) == diag.MAX_LINES + 1 and out[0].startswith("... 140 earlier") and out[-1].endswith("199")


def test_cli_diagnose_prints_and_saves(capsys, monkeypatch):
    from canopener import cli
    monkeypatch.setattr(diag, "run", lambda ids, hw=None, runner=None: {
        "system": [], **{i: [diag.Result(diag.Check("x", "cmd"), "echo", f"checked {i}")] for i in ids}})
    assert cli.main(["hardware", "--diagnose", "can_a", "--save"]) == 0
    out = capsys.readouterr().out
    assert "checked can_a" in out and "Saved: " in out
    assert len(diag.saved_reports()) == 1
