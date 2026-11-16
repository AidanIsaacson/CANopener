"""Installed options -> the Pi's boot settings: the root helper (deploy/raspberry-pi/canopener-boot-config)
on a scratch config.txt, the app's plan (core.bootconfig), and that the two agree."""

import importlib.machinery
import importlib.util
import os

import pytest

from canopener.core import bootconfig, hardware
from canopener.core.settings import Settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER_PATH = os.path.join(ROOT, "deploy", "raspberry-pi", "canopener-boot-config")
STOCK = "# stock\ndtparam=audio=on\n\n[pi5]\ndtoverlay=nospi10\n\n[all]\n"


def _helper():
    loader = importlib.machinery.SourceFileLoader("canopener_boot_config", HELPER_PATH)
    spec = importlib.util.spec_from_loader("canopener_boot_config", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.txt"
    p.write_text(STOCK)
    return p


def test_app_and_helper_tables_agree():
    h = _helper()
    assert h.PARTS == bootconfig.PARTS and h.LIVE == bootconfig.LIVE and h.BEGIN == bootconfig.BEGIN
    assert h.END == bootconfig.END and h.TOKENS_PREFIX == bootconfig.TOKENS_PREFIX
    for part in h.PARTS:
        for osc, spi, pi5 in ((20000000, 0, True), (40000000, 1000000, False)):
            assert h.lines(part, osc, spi, pi5) == bootconfig.lines(part, osc, spi, pi5), part
    assert {v for _l, v in bootconfig.OSCILLATORS} <= set(h.OSCILLATORS)
    assert {v for _l, v in bootconfig.SPI_SPEEDS if v} <= set(h.SPI_SPEEDS)
    assert set(bootconfig.PARTS) <= set(hardware.BY_ID)


def test_controls_lines_match_config_controls():
    path = os.path.join(ROOT, "deploy", "raspberry-pi", "config-controls.txt")
    rows = [r.strip() for r in open(path) if r.strip() and not r.lstrip().startswith("#")]
    assert bootconfig.lines("controls") == rows


def test_config_hardware_txt_still_matches():
    text = open(os.path.join(ROOT, "deploy", "raspberry-pi", "config-hardware.txt")).read()
    assert "dtoverlay=mcp251xfd,spi0-0,oscillator=20000000,interrupt=25" in text
    assert bootconfig.lines("can_a")[1] == "dtoverlay=mcp251xfd,spi0-0,oscillator=20000000,interrupt=25"


def test_helper_writes_only_its_block_and_takes_it_out_again(cfg):
    h = _helper()
    assert h.main(["--config", str(cfg), "set", "can_a:osc=20000000:spi=1000000", "adc_pi"]) == 0
    text = cfg.read_text()
    assert text.startswith(STOCK.rstrip())                          # what was there is untouched
    assert text.count("dtparam=spi=on") == 1 and "speed=1000000" in text and "dtoverlay=ads1115" in text
    assert text.split(h.BEGIN)[1].lstrip().startswith(h.TOKENS_PREFIX + " can_a:osc=20000000:spi=1000000 adc_pi\n[all]")
    assert (cfg.parent / "config.txt.canopener-bak").read_text() == STOCK
    assert bootconfig.current(str(cfg)) == ["can_a:osc=20000000:spi=1000000", "adc_pi"]
    assert h.main(["--config", str(cfg), "set", "adc_pi"]) == 0     # rewritten, not appended
    text = cfg.read_text()
    assert text.count(h.BEGIN) == 1 and "mcp251xfd" not in text and "ads1115" in text
    assert h.main(["--config", str(cfg), "set"]) == 0               # nothing installed: no block at all
    assert cfg.read_text() == STOCK


def test_helper_refuses_anything_outside_its_table(cfg, capsys):
    h = _helper()
    for bad in (["set", "evil"], ["set", "can_a:osc=12345"], ["set", "adc_pi:osc=20000000"],
                ["set", 'can_a:spi=1000000\ndtoverlay=evil'], ["set", "can_a", "can_a"], ["live", "rm"], ["bogus"]):
        assert h.main(["--config", str(cfg)] + bad) == 2, bad
    assert cfg.read_text() == STOCK
    assert "unknown option" in capsys.readouterr().err


def test_helper_skips_lines_already_in_the_file(cfg):
    cfg.write_text(STOCK + "dtparam=i2c_arm=on\n")
    h = _helper()
    assert h.main(["--config", str(cfg), "set", "adc_pi"]) == 0
    block = cfg.read_text().split(h.BEGIN)[1]
    assert "dtparam=i2c_arm=on" not in block and "dtoverlay=ads1115" in block


def test_helper_live_loads_with_dtoverlay_and_says_when_a_restart_is_needed(capsys):
    h = _helper()
    assert h.main(["--dry-run", "live", "can_a:osc=20000000:spi=1000000"]) == 0
    out = capsys.readouterr().out
    assert "would run: dtparam spi=on" in out
    assert "would run: dtoverlay mcp251xfd spi0-0 oscillator=20000000 interrupt=25 speed=1000000" in out
    assert h.main(["--dry-run", "live", "esp32"]) == 3 and h.main(["--dry-run", "live", "controls"]) == 3


def test_plan_follows_the_installed_options(cfg):
    s = Settings()
    hw = hardware.Hardware(s)
    p = bootconfig.plan(hw, str(cfg))
    assert p.empty and p.tokens == []
    hw.set_state("adc_pi", hardware.YES)
    hw.set_state("can_a", hardware.AUTO)
    hw.set_state("meter_pico", hardware.YES)                        # USB: nothing in the boot config
    s.set("hardware.can_a.spi_hz", 1000000)
    p = bootconfig.plan(hw, str(cfg))
    assert p.tokens == ["can_a:osc=20000000:spi=1000000", "adc_pi"] and p.add == p.tokens and not p.remove
    text = "\n".join(p.describe())
    assert "CAN stack A: adds" in text and "speed=1000000" in text and "Loads now: CAN stack A, ADC on the Pi." in text
    _helper().main(["--config", str(cfg), "set", *p.tokens])
    assert bootconfig.plan(hw, str(cfg)).empty
    hw.set_state("can_a", hardware.NO)
    hw.set_state("controls", hardware.YES)
    p = bootconfig.plan(hw, str(cfg))
    assert p.add == ["controls"] and p.remove == ["can_a"]
    assert "A restart finishes it (Buttons and encoder, CAN stack A)." in "\n".join(p.describe())


def test_apply_runs_the_helper_and_reports_what_needs_a_restart(cfg, monkeypatch):
    s = Settings()
    hw = hardware.Hardware(s)
    hw.set_state("adc_pi", hardware.YES)
    hw.set_state("esp32", hardware.YES)
    p = bootconfig.plan(hw, str(cfg))
    monkeypatch.setattr(bootconfig, "helper_installed", lambda: False)
    r = bootconfig.apply(p, s, run=lambda steps, **kw: (True, "ok"))
    assert not r.ok and "raspberry-pi/install.sh" in r.message
    monkeypatch.setattr(bootconfig, "helper_installed", lambda: True)
    ran = []

    def run(steps, **kw):
        ran.append(steps[0])
        return True, "ok"
    r = bootconfig.apply(p, s, run=run)
    assert ran == [[bootconfig.HELPER, "set", "adc_pi", "esp32"], [bootconfig.HELPER, "live", "adc_pi"]]
    assert r.ok and r.loaded == ["adc_pi"] and r.restart and "Working now: ADC on the Pi" in r.message
    assert s.get("hardware.restart_after") > 0
    r = bootconfig.apply(bootconfig.Plan(["adc_pi"], add=["adc_pi"]), s, run=lambda steps, **kw: (
        (False, "dtoverlay failed") if steps[0][1] == "live" else (True, "ok")))
    assert r.ok and r.restart and not r.loaded                      # couldn't load now: a restart will
    r = bootconfig.apply(p, s, run=lambda steps, **kw: (False, "a password is required"))
    assert not r.ok and "a password is required" in r.message
