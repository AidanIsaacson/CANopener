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
    press(app, pygame.K_F2)                               # soft key 2 = Meter
    assert app.top.title == "Meter"
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
    tap(app, (keys.x + slot_w * 3 + slot_w // 2, keys.centery))      # 4th soft key on screen
    assert app.top.title == "System"
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
    app.open_mode("settings")
    screen = app.top
    assert isinstance(screen, SettingsScreen)
    row = next(r for r in screen.rows.refresh() if r.label == "Desktop app instead")
    row.select()                                        # asks first, No by default
    app.action(inp.SELECT)
    assert app.running and app.switch_to is None
    row.select()
    app.action(inp.ROTATE, 1)
    app.action(inp.SELECT)
    assert not app.running and app.switch_to == "desktop" and app.ctx.settings.get("ui") == "desktop"


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
    assert kinds() == ["socketcan"]
    ctx.set_show_simulators(True)
    assert "sim" in kinds() and "obd-sim" in kinds()
    ctx.settings.set("can.source", "obd-sim")
    ctx.set_show_simulators(False)
    assert kinds() == ["socketcan"] and ctx.settings.get("can.source") == "socketcan"
