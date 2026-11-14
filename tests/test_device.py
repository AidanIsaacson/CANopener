"""The handheld UI: controls, the Qt-free context against the simulators, and the Pygame app
headless (SDL's dummy video driver)."""

import os
import struct
import time

import pytest

from canopener.core import capture as cap
from canopener.core.settings import Settings
from canopener.device import input as inp
from canopener.device.context import DeviceContext


# --- controls -----------------------------------------------------------------
def test_keymap_defaults_overrides_and_button5():
    km = inp.KeyMap({"F6": "log"}, button5="tag")
    assert km.action("f1") == inp.SOFT[0] and km.action("Return") == inp.SELECT
    assert km.action("f6") == inp.LOG and km.action("f5") == inp.TAG and km.action("x") is None
    assert inp.KeyMap(button5="soft").action("f5") == inp.SOFT[4]
    assert inp.KeyMap(button5="nonsense").action("f5") == inp.BACK


def test_select_short_press_selects_long_press_goes_back():
    p = inp.PressTracker(long_press=0.7)
    assert p.down(inp.SELECT, now=0.0) is None
    assert p.poll(now=0.3) is None and p.up(inp.SELECT, now=0.3) == inp.SELECT
    p.down(inp.SELECT, now=1.0)
    assert p.poll(now=1.8) == inp.BACK and p.poll(now=1.9) is None
    assert p.up(inp.SELECT, now=2.0) is None             # the release after a long press does nothing
    assert p.down(inp.UP) == inp.UP


def test_ladder_decodes_debounces_and_releases():
    lad = inp.LadderButtons([0.0, 0.6, 1.2, 1.8, 2.4], list(inp.SOFT[:4]) + [inp.BUTTON5])
    assert lad.decode(3.3) is None and lad.decode(0.65) == 1 and lad.decode(0.9) is None
    assert lad.update(1.21) == []                         # first reading: not trusted yet
    assert lad.update(1.19) == [("down", inp.SOFT[2])]
    assert lad.update(1.2) == []
    lad.update(3.3)
    assert lad.update(3.3) == [("up", inp.SOFT[2])]
    with pytest.raises(ValueError):
        inp.LadderButtons([0.0, 1.0], [inp.SOFT[0]])


def test_evdev_encoder_found_and_read(tmp_path):
    sysfs = tmp_path / "sys"
    for n, name, rel in (("event0", "gpio_keys", "0"), ("event1", "rotary@18", "1")):
        d = sysfs / n / "device"
        (d / "capabilities").mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "capabilities" / "rel").write_text(rel + "\n")
    assert inp.find_encoder(str(sysfs)) == "/dev/input/event1"
    fifo = str(tmp_path / "event1")
    os.mkfifo(fifo)
    r = inp.EvdevReader(fifo)
    assert r.read() == 0 and r.error is None              # opened, nothing yet
    w = os.open(fifo, os.O_WRONLY)
    ev = lambda t, c, v: struct.pack("llHHi", 0, 0, t, c, v)  # noqa: E731
    os.write(w, ev(inp.EV_REL, inp.REL_X, 1) + ev(0, 0, 0) + ev(inp.EV_REL, inp.REL_X, 1)
             + ev(inp.EV_REL, inp.REL_X, -3) + ev(inp.EV_KEY, 28, 1))
    assert r.read() == -1
    os.close(w)
    r.close()


def test_context_and_input_dont_need_pygame():
    import subprocess
    import sys
    code = ("import sys; import canopener.device.context, canopener.device.input, canopener.core.meters; "
            "import canopener.core.themes; "
            "assert 'pygame' not in sys.modules")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# --- the context (no Pygame) ---------------------------------------------------
@pytest.fixture
def ctx():
    s = Settings()
    s.set("meters.sim", True)
    c = DeviceContext(s)
    yield c
    c.shutdown()


def test_context_sim_codes_live_and_a_log_with_meters(ctx):
    ctx.connect("sim")
    assert ctx.wait_idle() and ctx.live and "Simulator" in ctx.describe_connection()
    assert ctx.read_codes("active") and ctx.wait_idle(10)
    assert any(r.spn == 3719 for r in ctx.result.rows)
    end = time.time() + 3
    while time.time() < end and not (ctx.j1939_live.order and ctx.meter_live.order):
        ctx.tick()
        time.sleep(0.05)
    assert ctx.j1939_live.order and len(ctx.meter_live.order) == 4
    ctx.set_plan(["key on", "fault"])
    assert ctx.start_log() and ctx.recording
    time.sleep(0.3)
    ctx.tag()
    time.sleep(0.3)
    ctx.toggle_log()
    assert ctx.capture.state == cap.STOPPED
    ctx.save_log()
    assert ctx.wait_idle(20) and ctx.capture.state == cap.SAVED
    files = os.listdir(ctx.last_saved)
    assert "measurements.csv" in files and "capture.log" in files
    meta = ctx.capture.meta
    assert meta["front_end"].startswith("device") and len(meta["channels"]) == 4
    assert ctx.settings.get("log.plan") == ["key on", "fault"]
    ctx.disconnect()
    assert ctx.session is None


def test_context_logs_meters_without_a_bus_and_refuses_with_nothing(ctx):
    assert ctx.start_log()
    time.sleep(0.2)
    ctx.stop_log()
    assert ctx.capture.recorded_frames == 0 and ctx.capture.samples > 0
    ctx.discard_log()
    assert ctx.capture.state == cap.IDLE
    ctx.settings.set("meters.sim", False)
    ctx.apply_meters()
    assert not ctx.start_log() and "Nothing to record" in ctx.current_toast()


def test_context_finder_lock_connects(ctx, monkeypatch):
    connected = []
    monkeypatch.setattr(ctx, "connect", lambda kind=None: connected.append(kind))
    from canopener.core.link import FinderEvent

    class FakeFinder:
        def stop(self):
            pass
    ctx.finder, ctx._connect_after_find = FakeFinder(), True
    ctx._finder_event(FinderEvent("searching", "250k: nothing heard", 250000))
    assert ctx.finding and "250k" in ctx.finder_log[-1]
    ctx._finder_event(FinderEvent("locked", "Locked on 500k", 500000))
    assert not ctx.finding and connected == ["socketcan"] and ctx.settings.get("can.last_bitrate") == 500000


def test_context_job_errors_become_toasts(ctx):
    ctx.run_job("Exploding", lambda: 1 / 0)
    assert ctx.wait_idle() and "Exploding failed" in ctx.current_toast()


# --- the Pygame app --------------------------------------------------------------
pygame = pytest.importorskip("pygame")


@pytest.fixture
def app_for():
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    made = []

    def make(size=(800, 480), rotate=0, **settings):
        from canopener.device.app import DeviceApp
        s = Settings()
        for k, v in settings.items():
            s.set(k.replace("__", "."), v)
        ctx = DeviceContext(s)
        app = DeviceApp(ctx, size, rotate=rotate, encoder=False)
        made.append(ctx)
        app.step([])
        return app
    yield make
    for c in made:
        c.shutdown()
    pygame.quit()


def system_row(app, label):
    """A row of System, wherever it is: on the menu, or on one of its pages (left open)."""
    app.open_mode("settings")
    menu = app.top
    for r in menu.rows.refresh():
        if r.label == label and r.kind != "menu":
            return r
    for group in [r for r in menu.rows.refresh() if r.kind == "menu"]:
        group.select()
        found = next((r for r in app.top.rows.refresh() if r.label == label), None)
        if found is not None:
            return found
        app.pop()
    raise AssertionError(f"no System row {label!r}")


def key(k, up=False):
    return pygame.event.Event(pygame.KEYUP if up else pygame.KEYDOWN, key=k, mod=0)


def press(app, k):
    app.step([key(k), key(k, up=True)])


def tap(app, pos):
    app.step([pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1),
              pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1)])


def test_home_tiles_keys_and_soft_keys(app_for):
    app = app_for()
    titles = [m.title for m in __import__("canopener.device.modes", fromlist=["modes"]).modes()]
    assert titles[:4] == ["CAN", "Meter", "Data log", "System"]
    assert [k.label for k in app.softkey_slots()[:4]] == ["Connect", "Codes", "Live data", "Start log"]
    press(app, pygame.K_F3)                               # soft key 3 = straight to CAN > Live
    assert app.top.title == "CAN · Live"
    press(app, pygame.K_ESCAPE)
    assert len(app.stack) == 1
    app.step([pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=-1)])     # encoder / wheel moves the focus
    press(app, pygame.K_RETURN)
    assert app.top.title == "Meter"
    press(app, pygame.K_HOME)
    assert len(app.stack) == 1


def test_soft_key_and_tile_taps(app_for):
    app = app_for()
    keys, _, content = app.layout()
    slot_w = keys.w // 5
    tap(app, (keys.x + slot_w * 1 + slot_w // 2, keys.centery))      # 2nd soft key on screen: Codes
    assert app.top.title == "CAN · Codes"
    app.home()
    app.step([])
    tap(app, (content.x + 40, content.centery))                      # first tile
    assert app.top.title.startswith("CAN")


def test_rotation_maps_touch_back_to_the_ui(app_for):
    for rot in (90, 180, 270):
        app = app_for(rotate=rot)
        assert app.size == (800, 480)
        assert app.screen.get_size() == ((480, 800) if rot in (90, 270) else (800, 480))
        # draw a marker at a UI point, rotate as the app does, find it on the panel, map it back
        app.canvas.fill((0, 0, 0))
        app.canvas.set_at((100, 30), (255, 0, 0))
        out = pygame.transform.rotate(app.canvas, -rot)
        found = next((x, y) for x in range(out.get_width()) for y in range(out.get_height())
                     if out.get_at((x, y))[:3] == (255, 0, 0))
        assert app.to_logical(found) == (100, 30)


def test_softkeys_on_the_bottom_edge_and_four_buttons(app_for):
    app = app_for(device__softkeys_edge="bottom", device__buttons=4)
    keys, stat, _ = app.layout()
    assert keys.bottom == 480 and stat.top == 0
    assert len(app.softkey_slots()) == 4


def test_confirm_defaults_to_no(app_for):
    from canopener.device.screens.base import confirm
    app = app_for()
    said = []
    confirm(app, "Power off?", "Sure?", "Power off", lambda: said.append("yes"))
    app.step([])
    press(app, pygame.K_RETURN)                           # encoder push straight away = No
    assert said == [] and len(app.stack) == 1
    confirm(app, "Power off?", "Sure?", "Power off", lambda: said.append("yes"))
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)
    assert said == ["yes"]


def test_can_mode_against_the_simulator(app_for):
    app = app_for(meters__sim=True)
    ctx = app.ctx
    ctx.connect("sim")
    assert ctx.wait_idle()
    app.open_mode("can")
    can = app.top
    assert can.page.title == "Codes"
    press(app, pygame.K_F1)                               # Active
    assert ctx.wait_idle(10) and ctx.result is not None
    app.step([])
    assert can.page.items
    press(app, pygame.K_LEFT)
    assert can.page.title == "Connect"
    press(app, pygame.K_RIGHT)
    press(app, pygame.K_RIGHT)
    assert can.page.title == "Live"
    for _ in range(20):
        app.step([])
        time.sleep(0.03)
    live = can.page
    assert live.entries and any(m is ctx.meter_live for m, _ in live.entries)   # probes beside CAN values
    press(app, pygame.K_RETURN)                           # tick the focused value
    assert live.picked()
    press(app, pygame.K_F1)                               # Grid
    assert live.big
    app.step([])
    # a drag scrolls the list instead of tapping it
    live.big = False
    app.step([])
    _, _, content = app.layout()
    x, y = content.centerx, content.centery
    before = live.view.offset
    app.step([pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(x, y), button=1),
              pygame.event.Event(pygame.MOUSEMOTION, pos=(x, y - 120), buttons=(1, 0, 0)),
              pygame.event.Event(pygame.MOUSEBUTTONUP, pos=(x, y - 120), button=1)])
    assert live.view.offset > before and len(live.picked()) == 1
    for page in range(5):                                 # every page draws
        can._go(page)
        app.step([])


def test_obd_car_codes_to_live_data(app_for):
    app = app_for()
    ctx = app.ctx
    ctx.connect("obd-sim")
    assert ctx.wait_idle() and ctx.protocol == "obd2"
    app.open_mode("can")
    can = app.top
    press(app, pygame.K_F1)                               # Read codes
    assert ctx.wait_idle(15) and ctx.obd_codes
    app.step([])
    codes = can.page
    codes.view.focus = next(i for i, it in enumerate(codes.items) if codes._has_live(it))
    press(app, pygame.K_F4)                               # Live data for that code
    assert can.page.title == "Live"
    end = time.time() + 5
    while time.time() < end and not can.page.picked():
        app.step([])
        time.sleep(0.05)
    assert can.page.picked() and ctx.obd_streamer.running


@pytest.mark.parametrize("size", [(800, 480), (1280, 720), (1280, 800)])
def test_every_screen_draws_at_common_panel_sizes(app_for, size):
    from canopener.device import modes
    app = app_for(size, meters__sim=True)
    for m in modes.modes():
        app.open_mode(m.id)
        for _ in range(3):
            app.step([])
        assert app.top.title
    app.ctx.toast("A fairly long message that has to wrap over more than one line on a small screen")
    app.step([])


def test_config_txt_controls_send_the_keys_the_app_expects():
    """deploy/raspberry-pi/config-controls.txt and input.DEFAULT_KEYS must agree (CLAUDE.md)."""
    import re
    linux_to_pygame = {59: "f1", 60: "f2", 61: "f3", 62: "f4", 63: "f5", 103: "up", 108: "down",
                       105: "left", 106: "right", 28: "return"}
    path = os.path.join(os.path.dirname(__file__), "..", "deploy", "raspberry-pi", "config-controls.txt")
    text = open(path).read()
    codes = [int(c) for c in re.findall(r"^dtoverlay=gpio-key,.*keycode=(\d+)", text, re.M)]
    assert len(codes) == 10 and len(set(codes)) == 10     # 5 top buttons, 4 d-pad, encoder push
    assert {inp.DEFAULT_KEYS[linux_to_pygame[c]] for c in codes} == {
        *inp.SOFT[:4], inp.BUTTON5, inp.UP, inp.DOWN, inp.LEFT, inp.RIGHT, inp.SELECT}
    pins = [int(p) for p in re.findall(r"gpio=(\d+),active_low", text)] + [24, 4]
    reserved = {2, 3, 7, 8, 9, 10, 11, 25, 14, 15, 18, 19, 20, 21}
    assert len(set(pins)) == len(pins) and not reserved & set(pins)


# --- switching between the desktop app and the handheld UI --------------------------
def test_frontend_argv_and_the_canopener_command_follows_the_switch(monkeypatch):
    import sys

    from canopener import frontends
    from canopener.gui import app as gui_app
    argv = frontends.argv_for(frontends.DESKTOP, fullscreen=True)
    assert argv == [sys.executable, "-m", "canopener", "--desktop", "--fullscreen"]
    assert frontends.argv_for(frontends.DEVICE, sim=True)[2:] == ["canopener.device.app", "--sim"]
    calls = []
    monkeypatch.setattr(frontends, "exec_ui", lambda ui, **kw: calls.append((ui, kw)))
    monkeypatch.setattr(frontends, "available", lambda ui: (True, ""))
    Settings().set("ui", "device")
    assert gui_app.main(["--sim"]) == 0
    assert calls == [("device", {"fullscreen": False, "sim": True})]


def test_handheld_system_screen_switches_to_the_desktop_app(app_for, monkeypatch):
    from canopener import frontends
    from canopener.device.screens.settings import SettingsScreen
    monkeypatch.setattr(frontends, "available", lambda ui: (True, ""))
    app = app_for(ui="device")
    row = system_row(app, "Desktop app instead")
    assert isinstance(app.top, SettingsScreen) and app.top.title == "Software"
    row.select()                                        # asks first, No by default
    app.action(inp.SELECT)
    assert app.running and app.switch_to is None
    row.select()
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)
    assert not app.running and app.switch_to == "desktop" and app.ctx.settings.get("ui") == "desktop"


def test_handheld_exit_to_desktop_closes_without_switching(app_for):
    """System > Exit to desktop closes cleanly (exit 0, so the Pi's service doesn't restart it) and
    leaves the chosen UI alone, unlike Desktop app instead."""
    from canopener.device.screens.settings import SettingsScreen
    app = app_for(ui="device")
    row = system_row(app, "Exit to desktop")
    assert isinstance(app.top, SettingsScreen) and app.top.title == "System"      # on the menu itself
    row.select()                                        # asks first, No by default
    app.action(inp.SELECT)
    assert app.running
    row.select()
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)
    assert not app.running and app.switch_to is None and app.ctx.settings.get("ui") == "device"


def test_switching_starts_a_fresh_process_or_asks_systemd_to(monkeypatch, tmp_path):
    """The other UI starts as a new process (own session, output logged), never inside the old one."""
    import subprocess

    from canopener import frontends
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("INVOCATION_ID", "from-gnome-terminal")     # a laptop terminal: not the service
    monkeypatch.delenv("CANOPENER_SERVICE", raising=False)
    started = []
    assert frontends.switch("desktop", popen=lambda argv, **kw: started.append((argv, kw))) == 0
    argv, kw = started[0]
    assert argv[1:] == ["-m", "canopener", "--desktop"] and kw["start_new_session"] and kw["close_fds"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.STDOUT
    assert "switching to the desktop UI" in open(frontends.log_path()).read()
    monkeypatch.setenv("CANOPENER_SERVICE", "1")        # the Pi's systemd service: it restarts the launcher
    assert frontends.switch("device", popen=lambda *a, **k: started.append(a)) == 75 and len(started) == 1


def test_closing_the_handheld_saves_a_recording(ctx):
    assert ctx.start_log()
    time.sleep(0.3)
    assert ctx.unsaved
    ctx.shutdown()
    assert ctx.capture.state == cap.SAVED and ctx.last_saved and not ctx.pending_captures()
    assert "measurements.csv" in os.listdir(ctx.last_saved)


def test_a_handheld_that_cant_start_hands_back_to_the_desktop_app(monkeypatch):
    from canopener import frontends
    from canopener.device import app as dev_app
    switched = []
    monkeypatch.setattr(frontends, "switch", lambda ui, **kw: switched.append(ui) or 0)
    monkeypatch.setattr(frontends, "available", lambda ui: (True, ""))
    s = Settings()
    assert dev_app._fall_back(s) == 1 and switched == []          # not the chosen UI: just fail
    s.set("ui", "device")
    assert dev_app._fall_back(s) == 0 and switched == ["desktop"] and s.get("ui") == "desktop"


def test_device_main_switches_to_the_desktop_app_after_closing(monkeypatch):
    from canopener import frontends
    from canopener.device import app as dev_app
    monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
    switched = []
    monkeypatch.setattr(frontends, "switch", lambda ui, **kw: switched.append(ui) or 0)

    def run(self):
        self.ctx.start_log()
        self.resize_window(1000, 600)                  # the user resized the window
        self.ctx.settings.set("ui", "desktop")
        self.switch_to, self.running = "desktop", False
    monkeypatch.setattr(dev_app.DeviceApp, "run", run)
    Settings().set("meters.sim", True)
    assert dev_app.main([]) == 0 and switched == ["desktop"]
    s = Settings()
    assert s.get("device.window") == "1000x600"       # reopens at that size
    assert not __import__("canopener.core.capture", fromlist=["x"]).pending_captures()   # log saved


def test_window_resizes_and_maximizes(app_for):
    app = app_for()
    small = app.theme.touch
    app.step([pygame.event.Event(pygame.VIDEORESIZE, w=1600, h=960, size=(1600, 960))])
    assert app.size == (1600, 960) and app.screen.get_size() == (1600, 960)
    assert abs(app.theme.touch - 2 * small) <= 1        # same 7" layout, just bigger
    keys, stat, content = app.layout()
    assert keys.w == 1600 and stat.bottom == 960
    app.step([pygame.event.Event(pygame.VIDEORESIZE, w=100, h=100, size=(100, 100))])
    assert app.size == (320, 320)                       # never smaller than usable
    app.ctx.settings.set("device.window", "1280x720")   # System > Window size applies straight away
    assert app.size == (1280, 720)
    app.step([])


def test_rotated_window_resizes_too(app_for):
    app = app_for(rotate=90)
    app.step([pygame.event.Event(pygame.VIDEORESIZE, w=600, h=1000, size=(600, 1000))])
    assert app.screen.get_size() == (600, 1000) and app.size == (1000, 600)
    assert app.to_logical((599, 0)) == (0, 0)
    app.step([])


def test_only_the_pi_service_counts_as_running_under_systemd(monkeypatch, tmp_path):
    """INVOCATION_ID leaks into every app started from GNOME Terminal (a systemd service): it must
    not make the app exit expecting a restart that never comes."""
    from canopener.core import updates
    laptop, pi = tmp_path / "laptop", tmp_path / "pi"
    laptop.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                      "app-org.gnome.Terminal.slice/vte-spawn-1234.scope\n")
    pi.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/canopener.service\n")
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    monkeypatch.delenv("CANOPENER_SERVICE", raising=False)
    assert not updates.under_systemd(str(laptop))
    assert not updates.under_systemd(str(tmp_path / "missing"))
    assert updates.under_systemd(str(pi))                   # a unit installed before the variable
    monkeypatch.setenv("CANOPENER_SERVICE", "1")
    assert updates.under_systemd(str(laptop))
    monkeypatch.setenv("CANOPENER_SERVICE", "0")
    assert not updates.under_systemd(str(pi))
    unit = open(os.path.join(os.path.dirname(__file__), "..", "deploy", "raspberry-pi", "canopener.service")).read()
    assert "Environment=CANOPENER_SERVICE=1" in unit


def test_pi_autostart_starts_the_service_at_desktop_login():
    """Raspberry Pi OS's labwc desktop never activates graphical-session.target, so the service's
    WantedBy= never fired (found on a Pi 5): the desktop's own autostart has to start it."""
    here = os.path.join(os.path.dirname(__file__), "..", "deploy", "raspberry-pi")
    entry = open(os.path.join(here, "canopener-autostart.desktop")).read()
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
    assert "systemctl --user start canopener.service" in exec_line
    assert "import-environment" in exec_line and "WAYLAND_DISPLAY" in exec_line
    installer = open(os.path.join(here, "install.sh")).read()
    assert 'canopener-autostart.desktop" "$HOME/.config/autostart/canopener.desktop"' in installer


def test_a_canopener_folder_in_the_working_directory_cant_shadow_the_app(tmp_path):
    """Menu entries start in the home folder, where the logs folder ~/canopener lives: Python would
    import that folder instead of the app (it broke launching after the first saved log)."""
    import subprocess
    import sys

    from canopener.core import config
    (tmp_path / "canopener" / "logs").mkdir(parents=True)
    r = subprocess.run([sys.executable, "-m", "canopener", "--version"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().startswith("canopener "), r.stderr
    assert os.path.exists(os.path.join(config.install_dir(), "canopener", "__init__.py"))


def test_every_way_of_starting_the_app_uses_its_own_folder(monkeypatch, tmp_path):
    from canopener import desktop, frontends
    from canopener.core import config
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CANOPENER_SERVICE", raising=False)
    started = []
    frontends.switch("desktop", popen=lambda argv, **kw: started.append(kw))
    assert started[0]["cwd"] == config.install_dir()
    assert f"Path={config.install_dir()}" in desktop.entry_text("/icon.svg").splitlines()
    calls = []
    monkeypatch.setattr(frontends.os, "chdir", lambda d: calls.append(("chdir", d)))
    monkeypatch.setattr(frontends.os, "execv", lambda p, a: calls.append(("execv", a[2])))
    frontends.exec_ui("device")
    assert calls == [("chdir", config.install_dir()), ("execv", "canopener.device.app")]


def test_clearing_j1939_codes_asks_first_then_reads_back(app_for):
    from canopener.device.screens.base import ChoiceDialog, ConfirmDialog, InfoDialog
    app = app_for()
    ctx = app.ctx
    ctx.connect("sim")
    assert ctx.wait_idle()
    app.open_mode("can")
    codes = app.top.page
    press(app, pygame.K_F1)                               # Active
    assert ctx.wait_idle(10) and any(r.spn == 3719 for r in ctx.result.rows)
    app.step([])

    def to_confirm():
        press(app, pygame.K_F3)                           # More...
        assert isinstance(app.top, ChoiceDialog)
        app.action(inp.ROTATE, 1)                         # "Clear codes..."
        app.action(inp.SELECT)
        assert isinstance(app.top, ChoiceDialog) and app.top.title == "Clear which codes?"
        labels = [lb for lb, _ in app.top.options]
        assert labels[0] == "Active codes (DM11), every module" and "00 Engine #1 only" in labels[3]
        app.action(inp.SELECT)                            # active, every module
        assert isinstance(app.top, ConfirmDialog) and "every module" in app.top.text
    to_confirm()
    app.action(inp.SELECT)                                # Select straight away = No
    assert not isinstance(app.top, ConfirmDialog) and not ctx.busy
    to_confirm()
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)                                # Clear
    assert ctx.wait_idle(15)
    app.step([])
    assert isinstance(app.top, InfoDialog) and "sent to every module" in app.top.lines[0]
    assert not any(r.spn == 3719 for r in ctx.result.rows) and codes.title == "Codes"


def test_clearing_obd_codes_asks_first(app_for):
    from canopener.device.screens.base import ConfirmDialog, InfoDialog
    app = app_for()
    ctx = app.ctx
    ctx.connect("obd-sim")
    assert ctx.wait_idle()
    app.open_mode("can")
    press(app, pygame.K_F1)
    assert ctx.wait_idle(15) and any(c for k in ctx.obd_codes.values() for c in k.values())
    app.step([])
    press(app, pygame.K_F3)                               # Clear...
    assert isinstance(app.top, ConfirmDialog) and "Mode 04" in app.top.text
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)
    assert ctx.wait_idle(20)
    app.step([])
    assert isinstance(app.top, InfoDialog)
    assert not any(ctx.obd_codes[e]["stored"] for e in ctx.obd_codes)


def test_clearing_is_refused_in_listen_only(app_for):
    app = app_for(can__listen_only=True)
    ctx = app.ctx
    ctx.connect("sim")
    assert ctx.wait_idle() and ctx.session.listen_only
    assert not ctx.clear_j1939("active", 0xFF) and "listen-only" in ctx.current_toast()
    assert ctx.session.tx_count == 0


def test_handheld_connects_through_python_can(ctx):
    pytest.importorskip("can")
    from canopener.can.pythoncan import PythonCanBackend
    from canopener.sim import MachineSimulator
    sim = MachineSimulator(PythonCanBackend("virtual:dev1"), seed=1)
    sim.start()
    try:
        ctx.settings.update({"can.source": "socketcan", "can.iface": "virtual:dev1", "can.bitrate": 0})
        ctx.connect()
        assert ctx.connection is None and "Pick a bitrate" in ctx.current_toast()
        assert not ctx.start_finder() and ctx.finder is None
        ctx.settings.set("can.bitrate", 250000)
        ctx.connect()
        assert ctx.wait_idle() and ctx.live and "python-can" in ctx.describe_connection()
        assert ctx.read_codes("active") and ctx.wait_idle(10) and any(r.spn == 3719 for r in ctx.result.rows)
    finally:
        ctx.disconnect()
        sim.stop()


def test_handheld_simulators_hidden_by_default(ctx):
    kinds = lambda: [k for k, _ in ctx.sources()]   # noqa: E731
    assert kinds() == ["socketcan", "elm"]
    ctx.set_show_simulators(True)
    assert "sim" in kinds() and "obd-sim" in kinds() and "elm-sim" in kinds()
    ctx.settings.set("can.source", "obd-sim")
    ctx.set_show_simulators(False)
    assert kinds() == ["socketcan", "elm"] and ctx.settings.get("can.source") == "socketcan"


def test_system_screen_installed_options(app_for):
    from canopener.core import hardware
    app = app_for()
    system_row(app, "Set all from build stage")
    app.step([])
    screen = app.top
    assert screen.title == "Installed options"
    hw = app.ctx.hardware
    hw.set_stage("2b")
    row = next(r for r in screen.rows.refresh() if r.label.startswith("Meter Pico"))
    assert row.value_text() == "Installed"
    row.adjust(1)                                         # Installed -> Use if found
    assert hw.state("meter_pico") == hardware.AUTO
    app.step([])                                          # draws with the fault lamps, no hang


# --- sound settings ------------------------------------------------------------
def test_event_tones_volume_and_touch_chirps(app_for, monkeypatch):
    from canopener.core import sound
    played = []
    monkeypatch.setattr(sound, "play_buzzer", lambda tone, spec=sound.AUTO, wait=False: False)
    app = app_for()
    app.ctx.speaker = lambda tone, volume: played.append((tone, volume)) or True
    s = app.ctx.settings
    assert app.ctx.cue("tag") and played[-1] == ("blip", 0.8)
    s.set("sound.tones", {"tag": "alert", "codes": "off"})
    app.ctx.cue("tag")
    assert played[-1][0] == "alert" and not app.ctx.cue("codes")
    s.set("sound.volume", 0)
    n = len(played)
    app.ctx.cue("tag")
    assert len(played) == n                         # volume off: the speaker stays quiet
    s.set("sound.volume", 50)
    # touch chirps: off by default, on in System > Sound; once per press, not per soft key too
    keys, _, _ = app.layout()
    pos = (keys.x + keys.w // 5 * 2 + keys.w // 10, keys.centery)     # soft key 3: Live data, then Reset min/max
    app.step([pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos),
              pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=pos)])
    assert len(played) == n
    s.set("sound.touch", True)
    app.step([pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos),
              pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=pos)])
    assert played[n:] == [("chirp", 0.5)]
    app.step([key(pygame.K_DOWN), key(pygame.K_DOWN, up=True)])
    assert played[-1] == ("chirp", 0.5) and len(played) == n + 2
    assert sound.tone_for("test") == "test" and sound.tone_for("nonsense") is None


def test_tones_screen_picks_and_previews(app_for, monkeypatch):
    from canopener.core import sound
    from canopener.device.screens.settings import TonesScreen
    monkeypatch.setattr(sound, "play_buzzer", lambda *a, **k: False)
    app = app_for()
    played = []
    app.ctx.speaker = lambda tone, volume: played.append(tone) or True
    row = system_row(app, "Tones for events")
    row.select()
    assert isinstance(app.top, TonesScreen)
    app.step([])
    log = next(r for r in app.top.rows.refresh() if r.label == "Log started")
    log.adjust(1)
    tone = app.ctx.settings.get("sound.tones")["rec_start"]
    assert tone != "rising" and played[-1] == tone
    next(r for r in app.top.rows.refresh() if r.label.startswith("Back to the standard")).select()
    assert app.ctx.settings.get("sound.tones") == {}
    app.step([])


def test_sound_wav_follows_the_volume(tmp_path):
    from canopener.core import sound
    loud, quiet = sound.cue_path("chirp", str(tmp_path), 1.0), sound.cue_path("chirp", str(tmp_path), 0.3)
    assert loud != quiet
    import array
    import wave

    def peak(p):
        with wave.open(p) as w:
            return max(abs(v) for v in array.array("h", w.readframes(w.getnframes())))
    assert peak(quiet) < peak(loud) * 0.2


# --- updates from the handheld ----------------------------------------------------
def test_check_for_updates_install_and_restart(app_for, monkeypatch):
    from canopener.core import updates
    from canopener.device.screens.base import ConfirmDialog, InfoDialog
    st = updates.UpdateStatus(True, "An update is available (version 9.9.9): 2 change(s).", latest="9.9.9",
                              behind=2, commits=["Cargo", "Sound settings"])
    monkeypatch.setattr(updates, "check", lambda: st)
    monkeypatch.setattr(updates, "apply", lambda: (True, "Updated. Restart CANopener to use the new version."))
    app = app_for()
    system_row(app, "Check for updates").select()
    assert app.ctx.wait_idle()
    assert isinstance(app.top, InfoDialog) and "- Cargo" in app.top.lines
    install = next(k for k in app.top.softkeys() if k is not None and k.label == "Install")
    install.fn()
    assert isinstance(app.top, ConfirmDialog)
    app.top._answer(True)
    assert app.ctx.wait_idle()
    assert isinstance(app.top, ConfirmDialog) and app.top.title == "Restart now?"
    app.top._answer(True)
    assert app.restart and not app.running


def test_no_install_button_when_up_to_date(app_for, monkeypatch):
    from canopener.core import updates
    monkeypatch.setattr(updates, "check", lambda: updates.UpdateStatus(True, "CANopener 0.2.6 is up to date."))
    app = app_for()
    system_row(app, "Check for updates").select()
    assert app.ctx.wait_idle()
    assert [k.label for k in app.top.softkeys() if k is not None] == ["Close"]


# --- themes (System > Display > Theme) -------------------------------------------
def test_builtin_themes_are_complete_and_readable():
    from canopener.core import themes
    for tid in themes.BUILTIN:
        look = themes.load(tid)
        assert look.id == tid and not look.problems, look.problems
        assert set(themes.COLOR_KEYS) <= set(look.colors)
        c = look.colors
        assert themes.contrast(c["text"], c["bg"]) >= 7 and themes.contrast(c["text"], c["panel"]) >= 7
        assert themes.contrast(c["dim"], c["panel"]) >= 4.5
        if tid != "basic":                          # basic keeps the original look
            assert themes.contrast(c["on_accent"], c["accent"]) >= 4.5
            assert themes.contrast(c["focus"], c["panel"]) >= 3     # the encoder's ring stands out


def test_user_theme_files_build_on_a_builtin_and_report_mistakes():
    from canopener.core import themes
    os.makedirs(themes.themes_dir())
    with open(os.path.join(themes.themes_dir(), "shop.json"), "w") as f:
        f.write('{"name": "Shop", "base": "daylight", "colors": {"accent": "#f60", "bg": "nope", "glow": "#fff"},'
                ' "style": {"radius": 0.3, "icons": "fancy"}, "fonts": {"text": "No Such Font"}}')
    for name in ("basic", "night"):                         # a built-in theme, or its old name
        with open(os.path.join(themes.themes_dir(), name + ".json"), "w") as f:
            f.write('{"name": "Not allowed to replace a built-in one"}')
    assert ("Shop", "shop") in themes.available()
    ids = [tid for _, tid in themes.available()]
    assert ids.count("basic") == 1 and "night" not in ids
    look = themes.load("shop")
    assert look.name == "Shop" and look.colors["accent"] == (255, 102, 0)
    assert look.colors["panel"] == themes.load("daylight").colors["panel"]       # the rest from its base
    assert look.style["radius"] == 0.3 and look.style["icons"] == "line"         # a bad choice is ignored
    assert len(look.problems) == 3 and any("bg" in p for p in look.problems)
    assert themes.first_font(look.fonts["text"]) is None                          # -> Pygame's own font
    assert themes.load("night").id == "element-zero" and themes.load("classic").id == "basic"   # old names
    missing = themes.load("gone")
    assert missing.id == themes.DEFAULT and missing.problems


def test_accent_colour_swaps_in_with_readable_text():
    from canopener.core import themes
    look = themes.load("graphite", "#ff8a1f")
    assert look.colors["accent"] == (255, 138, 31)
    assert themes.contrast(look.colors["on_accent"], look.colors["accent"]) >= 4.5
    assert look.colors["accent_dim"] != themes.load("graphite").colors["accent_dim"]


@pytest.mark.parametrize("theme", ["graphite", "daylight", "basic"])
def test_every_screen_draws_in_every_theme(app_for, theme):
    from canopener.device import modes
    from canopener.device.screens.base import confirm
    app = app_for((800, 480), meters__sim=True, device__theme=theme)
    assert app.theme.look.id == theme
    for m in modes.modes():
        app.open_mode(m.id)
        for _ in range(2):
            app.step([])
    app.home()
    app.step([])
    assert app.canvas.get_at((2, app.size[1] // 2))[:3] == app.theme.colors["bg"]
    confirm(app, "Power off?", "Sure?", "Power off", lambda: None)
    app.step([])


def test_theme_changes_apply_straight_away(app_for):
    app = app_for()
    assert app.theme.look.id == "element-zero" and app.theme.look.name == "Element Zero"
    bg = app.canvas.get_at((2, 240))[:3]
    app.ctx.settings.set("device.theme", "daylight")
    app.step([])
    assert app.theme.look.id == "daylight" and app.canvas.get_at((2, 240))[:3] != bg
    app.ctx.settings.set("device.accent", "#3cbf5c")
    assert app.theme.colors["accent"] == (60, 191, 92)
    theme_row = system_row(app, "Theme")
    assert theme_row.value_text() == "Daylight"
    theme_row.adjust(1)                                         # d-pad right: the next theme
    assert app.ctx.settings.get("device.theme") == "basic"


# --- Bluetooth OBD on the handheld ---------------------------------------------------
VLINKER = "00:1D:A5:68:98:8A"


def test_context_bluetooth_obd(ctx, fake_bt):
    from canopener.core.connection import ELM
    assert (ELM, "Bluetooth OBD") in ctx.sources()
    ctx.settings.set("can.source", ELM)
    ctx.connect()                                         # nothing picked yet: says so, doesn't connect
    assert ctx.connection is None and "Pick a Bluetooth OBD adapter" in ctx.current_toast()
    ctx.bt_refresh()
    assert ctx.wait_idle() and ctx.bt_state.powered and ctx.bt_paired == []
    ctx.bt_scan()
    assert ctx.wait_idle(20) and ctx.bt_found[0].address == VLINKER and ctx.bt_found[0].kind == "serial"
    ctx.bt_pair(VLINKER, "1234")
    assert ctx.wait_idle(20)
    ctx.wait_idle(10)                                     # the refresh after pairing
    assert ctx.settings.get("elm.adapter") == VLINKER and ctx.bt_label() == "vLinker MC-Android"
    assert [a for a, _ in ctx.bt_choices()] == [VLINKER] and not any(d.address == VLINKER for d in ctx.bt_found)
    ctx.bt_rename(VLINKER, "vLinker (truck)")
    assert ctx.bt_label() == "vLinker (truck)"
    ctx._update_link(time.time() + 5)
    assert ctx.adapter.text == "Bluetooth: vLinker (truck)"
    ctx.bt_forget(VLINKER)
    assert ctx.wait_idle(10) and ctx.settings.get("elm.adapter") == ""


def test_context_connects_through_the_simulated_adapter(ctx):
    from canopener.core.connection import ELM_SIM
    ctx.connect(ELM_SIM)
    assert ctx.wait_idle(15) and ctx.connection is not None and ctx.protocol == "obd2"
    assert ctx.obd_client is not None and not ctx.session.listen_only
    assert ctx.read_codes("active") and ctx.wait_idle(15)
    assert any("P0420" in kinds["stored"] for kinds in ctx.obd_codes.values())
    ctx._update_link(time.time() + 5)
    assert ctx.adapter.level == "sim"


def test_connect_page_and_bluetooth_screen(app_for, fake_bt):
    from canopener.core.connection import ELM
    from canopener.device.screens.bluetooth import BluetoothScreen, name_choices
    app = app_for(can__source=ELM)
    ctx = app.ctx
    app.open_mode("can")
    rows = app.top.page.rows.refresh()
    labels = [r.label for r in rows]
    assert "Adapter" in labels and "Bluetooth adapters" in labels and "Slower than a wired CAN adapter" in labels
    assert "Interface" not in labels and "Bitrate" not in labels and "Listen-only (never transmit)" not in labels
    next(r for r in rows if r.label == "Slower than a wired CAN adapter").select()
    assert "Transmit aren't available" in app.top.lines[0]
    app.pop()
    next(r for r in rows if r.label == "Bluetooth adapters").select()
    screen = app.top
    assert isinstance(screen, BluetoothScreen)
    assert ctx.wait_idle()                                # it checked Bluetooth on the way in
    app.step([])
    ctx.bt_scan()
    assert ctx.wait_idle(20)
    row = next(r for r in screen.rows.refresh() if r.label == "vLinker MC-Android")
    row.select()                                          # pick the PIN, then it pairs
    assert app.top.title == "PIN, if it asks"
    app.top._pick(0)
    assert ctx.wait_idle(20)
    ctx.wait_idle(10)
    assert fake_bt.state()["paired"] == [VLINKER] and ctx.settings.get("elm.adapter") == VLINKER
    ble = next(r for r in screen.rows.refresh() if r.label == "vLinker MC-IOS")
    ble.select()                                          # BLE: explained, not paired
    assert "Low Energy" in app.top.lines[0]
    app.pop()
    assert name_choices("vLinker MC-Android")[:2] == [("vLinker MC-Android", "vLinker MC-Android"),
                                                     ("vLinker MC (car)", "vLinker MC (car)")]
    for _ in range(3):
        app.step([])                                       # draws


def test_handheld_vehicle_choice_j1939_and_battery_channel(app_for):
    from canopener.core.connection import ELM_SIM
    app = app_for(show_simulators=True, can__source=ELM_SIM)
    ctx = app.ctx
    app.open_mode("can")
    rows = {r.label: r for r in app.top.page.rows.refresh()}
    assert rows["Vehicle"].value_text() == "Car or light truck (OBD-II)"
    assert "Listen-only (never transmit)" not in rows
    ctx.settings.set("elm.protocol", "A")
    rows = {r.label: r for r in app.top.page.rows.refresh()}
    assert rows["Vehicle"].value_text() == "Truck or equipment (J1939, 250k)" and "Listen-only (never transmit)" in rows
    ctx.connect()
    assert ctx.wait_idle(15) and ctx.connection is not None and ctx.session.backend.j1939
    assert ctx.protocol != "obd2"
    end = time.time() + 5
    while time.time() < end and ctx.hub.latest("obd.battery") is None:
        ctx.tick()
        time.sleep(0.05)
    assert ctx.hub.latest("obd.battery").value == 12.6
    ctx.disconnect()
    assert not any(s.id == "obd" for s in ctx.hub.sources())


# --- meter calibration ------------------------------------------------------------
def test_calibration_runs_channel_to_channel_from_system_meters(app_for):
    """System > Meters > Calibration: Full on CH1 (zero, hold, adjust with the encoder, save), then
    straight on to CH2 without a menu; Back stops and keeps what's saved. Then a Quick run."""
    from canopener.core.measure import Channel, MeasurementSource, Sample
    from canopener.device.screens.calibrate import CalibrationScreen, CalRunScreen

    class Probes(MeasurementSource):
        id, name = "t", "Test meter"

        def channels(self):
            return [Channel("t.p1", "Probe 1", "V", "t"), Channel("t.p2", "Probe 2", "V", "t")]

    app = app_for()
    hub, cal = app.ctx.hub, app.ctx.calibration
    hub.add_source(Probes())
    clock = [time.time()]

    def hold(ch, v, secs=1.3):
        for _ in range(int(secs * 20)):
            clock[0] += 0.05
            hub.push(Sample(ch, clock[0], v))
        app.step([])

    system_row(app, "Calibration").select()
    assert isinstance(app.top, CalibrationScreen)
    press(app, pygame.K_F2)                               # Full
    run = app.top
    assert isinstance(run, CalRunScreen) and run.session.step == "zero"
    hold("t.p1", 0.0)
    press(app, pygame.K_RETURN)                           # zero
    hold("t.p1", 5.1)
    press(app, pygame.K_RETURN)                           # hold the reading
    assert run.session.step == "confirm"
    press(app, pygame.K_RIGHT)                            # focus "Adjust"
    press(app, pygame.K_RETURN)
    assert run.session.step == "adjust"
    for _ in range(5):
        press(app, pygame.K_PAGEDOWN)                     # encoder: +0.01 V a click
    press(app, pygame.K_RETURN)                           # save
    assert cal.get("t.p1").gain == pytest.approx(5.15 / 5.1)
    press(app, pygame.K_RETURN)                           # next, without waiting
    assert run.session.current.label == "CH2" and run.session.step == "zero"
    hub.push(Sample("t.p1", clock[0], 5.1))
    assert hub.latest("t.p1").value == pytest.approx(5.15)          # readings now corrected
    press(app, pygame.K_ESCAPE)                           # Back: stop
    assert isinstance(app.top, CalibrationScreen) and "CH1 saved" in app.ctx.current_toast()
    assert cal.get("t.p2") is None

    press(app, pygame.K_F1)                               # Quick
    run = app.top
    assert run.session.step == "ref"
    run.on_action(inp.ROTATE, round((5.15 - run.session.ref) / 0.01))      # turn the encoder down to 5.15 V
    press(app, pygame.K_RETURN)                           # start
    assert app.ctx.settings.get("meters.cal_ref") == pytest.approx(5.15)
    for ch in ("t.p1", "t.p2"):
        hold(ch, 0.01)
        hold(ch, 5.0)
        assert run.session.step == "saved"
        press(app, pygame.K_RETURN)
    assert run.session.step == "done"
    assert cal.get("t.p2").gain == pytest.approx(5.15 / 4.99)
    press(app, pygame.K_RETURN)
    assert isinstance(app.top, CalibrationScreen)
    app.step([])


# ------------------------------------------------- the meter's Hold, and enlarging a reading

def test_grid_double_tap_enlarges_a_tile_and_stacks_the_rest():
    from canopener.device.ui import GridView
    now = [100.0]
    g = GridView(clock=lambda: now[0])
    area = pygame.Rect(0, 0, 1200, 600)
    plain = g.layout(area, 4, 200, 120)
    assert len({r.size for r in plain}) == 1
    assert not g.tap(2) and g.zoom is None and g.focus == 2            # one tap only moves the focus
    now[0] += 1.0
    assert not g.tap(2) and g.zoom is None                             # too slow to be a double tap
    now[0] += 0.2
    assert g.tap(2) and g.zoom == 2
    tiles = g.layout(area, 4, 200, 120)
    big, others = tiles[2], [r for i, r in enumerate(tiles) if i != 2]
    assert big.w * big.h > 0.6 * area.w * area.h                       # most of the screen
    assert all(r.x >= big.right for r in others)                       # stacked beside it ...
    assert [r.y for r in others] == sorted(r.y for r in others) and len({r.x for r in others}) == 1
    assert all(area.contains(r) for r in tiles)
    assert not any(a.colliderect(b) for i, a in enumerate(tiles) for b in tiles[i + 1:])
    tall = g.layout(pygame.Rect(0, 0, 600, 1000), 4, 200, 120)         # ... or below it on a tall screen
    assert all(r.y >= tall[2].bottom for i, r in enumerate(tall) if i != 2)
    many = g.layout(area, 12, 200, 120)                                # a lot of readings: still all on screen
    assert all(area.contains(r) and r.w > 0 and r.h > 0 for r in many) and many[2].w >= area.w // 2 - 20
    assert not any(a.colliderect(b) for i, a in enumerate(many) for b in many[i + 1:])
    now[0] += 5
    g.tap(0)
    now[0] += 0.2
    assert g.tap(0) and g.zoom == 0                                    # another tile takes over
    now[0] += 5
    g.tap(0)
    now[0] += 0.2
    assert g.tap(0) and g.zoom is None                                 # the big one again: back to the grid
    g.zoom = 3
    assert g.layout(area, 2, 200, 120) and g.zoom is None              # that tile is gone
    g.zoom = 0
    assert g.unzoom() and not g.unzoom()


def _meter_screen(app_for):
    app = app_for(meters__sim=True)
    app.open_mode("meter")
    end = time.time() + 5
    while time.time() < end and len(app.ctx.meter_live.order) < 4:
        app.step([])
        time.sleep(0.03)
    assert len(app.ctx.meter_live.order) == 4
    return app, app.top


def test_meter_hold_freezes_the_trace_too(app_for):
    app, meter = _meter_screen(app_for)
    live = app.ctx.meter_live
    key0 = live.order[0]
    press(app, pygame.K_F1)                                # Hold
    assert meter.hold is not None and set(meter.hold) == set(live.rows)
    held = meter.hold[key0]
    frozen = (held.value, held.lo, held.hi, list(held.points))
    n = len(live.rows[key0].points)
    end = time.time() + 5
    while time.time() < end and len(live.rows[key0].points) < n + 5:
        app.step([])
        time.sleep(0.03)
    assert len(live.rows[key0].points) >= n + 5            # the meter kept reading underneath
    assert (held.value, held.lo, held.hi, list(held.points)) == frozen       # the screen didn't move
    from canopener.device.screens.live import trace_points
    assert trace_points(held, app.ctx.units)[-1][0] == frozen[3][-1][0]
    press(app, pygame.K_F1)                                # release: live again
    assert meter.hold is None


def test_meter_double_tap_enlarges_a_channel_and_back_restores(app_for):
    app, meter = _meter_screen(app_for)
    w, h = app.size
    pos = next((x, y) for y in range(h // 4, h, 20) for x in range(w // 8, w, 20)    # somewhere on a channel's tile
               if (app.ui.hit_rect_at((x, y))[0] or pygame.Rect(0, 0, 0, 0)).h > h // 5)
    tap(app, pos)
    assert meter.grid.zoom is None
    tap(app, pos)
    assert meter.grid.zoom is not None
    app.step([])
    press(app, pygame.K_ESCAPE)                            # Back un-enlarges before it leaves the screen
    assert meter.grid.zoom is None and app.top is meter
    press(app, pygame.K_ESCAPE)
    assert app.top is not meter


def test_kline_deep_probe_runs_from_the_codes_page_and_lands_in_a_saved_log(app_for, monkeypatch):
    """Codes > More... > Deep probe on a K-line car: says what it does first, records a log for
    itself, writes the transcript into it and saves it (so it can be uploaded)."""
    from canopener.can import kline_probe
    from canopener.core import capture as cap
    from canopener.core.connection import ELM_SIM
    from canopener.device.screens.base import ChoiceDialog, ConfirmDialog, InfoDialog
    monkeypatch.setattr(kline_probe, "SETTLE", 0.0)
    app = app_for(show_simulators=True, can__source=ELM_SIM, elm__sim_vehicle="iso9141")
    ctx = app.ctx
    ctx.connect()
    assert ctx.wait_idle(15) and ctx.session.backend.protocol == "3"
    assert ctx.kline_probe_refusal() == ""
    app.open_mode("can")
    press(app, pygame.K_F2)                               # More...
    assert isinstance(app.top, ChoiceDialog)
    app.action(inp.SELECT)
    assert isinstance(app.top, ConfirmDialog) and "doesn't clear codes" in app.top.text
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)
    assert ctx.recording or ctx.busy
    assert ctx.wait_idle(60)
    app.step([])
    assert isinstance(app.top, InfoDialog), ctx.current_toast()
    assert any("Lamp status" in line for line in app.top.lines)
    assert ctx.wait_idle(30) and ctx.capture.state == cap.SAVED
    folder = ctx.capture.folder
    text = open(os.path.join(folder, kline_probe.REPORT_FILE)).read()
    assert "486B1041018207E50477" in text and "== What it found ==" in text
    assert "kline_probe.txt" in open(os.path.join(folder, "README.md")).read()
    assert "> 0101" in open(os.path.join(folder, "adapter_trace.txt")).read()
    assert ctx.session.backend.protocol == "3"            # the adapter was set up again
    ctx.disconnect()


def test_kline_deep_probe_is_refused_on_a_can_car(app_for):
    app = app_for()
    ctx = app.ctx
    ctx.connect("obd-sim")
    assert ctx.wait_idle()
    assert "Bluetooth OBD adapter" in ctx.kline_probe_refusal()
    assert not ctx.kline_probe() and not ctx.recording


def test_kline_values_reads_what_the_last_full_scan_found(app_for, monkeypatch, tmp_path):
    """Codes > More... > Read the values: the plan comes from the newest saved kline_probe.txt."""
    from canopener.can import kline_probe
    from canopener.core import capture as cap
    from canopener.core import config
    from canopener.core.connection import ELM_SIM
    monkeypatch.setattr(kline_probe, "SETTLE", 0.0)
    app = app_for(show_simulators=True, can__source=ELM_SIM, elm__sim_vehicle="kwp")
    ctx = app.ctx
    ctx.connect()
    assert ctx.wait_idle(15)
    assert ctx.kline_plan() is None and not ctx.kline_values()
    old = os.path.join(config.logs_dir(), "20000101_000000_capture")
    os.makedirs(old)
    with open(os.path.join(old, kline_probe.REPORT_FILE), "w") as f:
        f.write("- Service 22 reacts to the shape '22 1101 0401' (module 0x10, tester 0xF1, fast init): answered\n"
                "- Common identifiers that answer (22 11 xx, module 0x10, tester 0xF1, fast init): 1 (1 b): 01\n")
    assert ctx.kline_plan().commands == ["2211010401"]
    seen = []
    assert ctx.kline_values(then=lambda title, lines: seen.append(lines))
    assert ctx.wait_idle(30) and ctx.wait_idle(30) and ctx.capture.state == cap.SAVED
    text = open(os.path.join(ctx.capture.folder, kline_probe.VALUES_FILE)).read().splitlines()
    assert text[0] == kline_probe.VALUES_HEADER and sum(",2211010401,50,ok" in line for line in text) == 2
    assert seen and seen[0][0].startswith("2 values read")
    ctx.disconnect()


def test_thin_module_warning_on_the_handheld(app_for):
    """A module that lists no live data: 'no codes' comes with a warning, and More... explains it."""
    from canopener.core.connection import ELM_SIM
    from canopener.device.screens.base import ChoiceDialog, InfoDialog
    app = app_for(show_simulators=True, can__source=ELM_SIM, elm__sim_vehicle="thin-kline")
    ctx = app.ctx
    ctx.connect()
    assert ctx.wait_idle(15)
    app.open_mode("can")
    assert ctx.read_codes() and ctx.wait_idle(15)
    assert ctx.obd_thin == [0x7E8] and "may not be the whole story" in ctx.current_toast()
    app.step([])
    press(app, pygame.K_F2)                               # More...
    assert isinstance(app.top, ChoiceDialog)
    app.action(inp.SELECT)                                # the first entry: why 'no codes' may be wrong
    assert isinstance(app.top, InfoDialog) and "formality" in app.top.lines[0]
    ctx.disconnect()
