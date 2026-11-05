"""System settings: everything that isn't tied to one mode. Stored in ``core.settings``
(``~/.config/canopener/settings.json``), shared with every front-end.

Left / right on the d-pad changes the focused value; Select opens a list to pick from.
"""

import glob
import os
import subprocess
import time

from ... import __version__
from ...core import config, hardware, meters, sound, storage, system, themes, units, updates
from ..input import ACTION_NAMES
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm, cycle, label_of


def status(ctx) -> str:
    return f"Units: {units.describe(ctx.units)}"


ROTATIONS = [("0° (as mounted)", 0), ("90°", 90), ("180° (upside down)", 180), ("270°", 270)]
DIAGONALS = [(f'{d:g}"', d) for d in (5.0, 7.0, 8.0, 10.1, 13.3)]
EDGES = [("Top", "top"), ("Bottom", "bottom")]
BUTTONS = [("4", 4), ("5", 5)]
BUTTON5 = [("Back", "back"), ("Soft key 5", "soft"), ("Start / stop log", "log"), ("Timestamp", "tag")]
UNITS = [("Metric (kPa, °C, km/h)", units.METRIC), ("Imperial (psi, °F, mph)", units.IMPERIAL)]
LONG_PRESS = [(f"{s:g} s", s) for s in (0.5, 0.7, 1.0, 1.5)]
WINDOWS = [(s, s) for s in ("800x480", "1024x600", "1280x720", "1280x800")]
VOLUMES = [("Off", 0)] + [(f"{v}%", v) for v in range(10, 101, 10)]
TONE_OPTIONS = [("Off", sound.OFF)] + [(sound.TONE_LABELS.get(t, t), t) for t in sound.TONES]
RATIOS = [("1 (straight to the ADC)", 1.0), ("6 (50k / 10k divider)", 6.0), ("11 (100k / 10k)", 11.0)]


def _home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def serial_ports() -> list[str]:
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/serial/by-id/*"))


UNLOCK_OPENS, UNLOCK_GAP_S = 7, 10.0

# System's groups: page title -> the method building its rows (SettingsScreen._page_<name>)
SECTIONS = {"Display": "display", "Controls": "controls", "Sound": "sound", "Meters": "meters",
            "Simulators": "simulators", "Installed options": "installed", "Storage": "storage",
            "Software": "software"}


class SettingsScreen(Screen):
    """System: a short menu (one row per group, a few things done straight from it); each group
    opens as its own page (``section``)."""

    title = "System"

    def __init__(self, app, section: str | None = None):
        super().__init__(app)
        self.section = section if section in SECTIONS else None
        if self.section:
            self.title = self.section
        self.rows = RowList(self._rows)
        self._about_opens: list[float] = []     # Games unlock: About, seven times in a row

    def _set(self, key, value, then=None):
        self.ctx.settings.set(key, value)
        if then:
            then()

    def _row(self, label, key, options, then=None, hint=""):
        s = self.ctx.settings
        return Row(label, lambda: label_of(options, s.get(key)),
                   select=lambda: choose(self.app, label, options, s.get(key), lambda v: self._set(key, v, then)),
                   adjust=lambda d: self._set(key, cycle(options, s.get(key), d), then), hint=hint)

    def _toggle(self, label, key, then=None, hint=""):
        s = self.ctx.settings
        return Row(label, select=lambda: self._set(key, not s.get(key), then), check=lambda: s.get(key), hint=hint)

    def _open(self, section: str):
        self.app.push(SettingsScreen(self.app, section))

    def _rows(self):
        if self.section:
            return getattr(self, "_page_" + SECTIONS[self.section])()
        return self._menu()

    # --- the top level ---
    def _menu(self):
        ctx, s = self.ctx, self.ctx.settings

        def group(name, summary, hint=""):
            return Row(name, summary, select=lambda: self._open(name), kind="menu", hint=hint)
        n_buttons = int(s.get("device.buttons") or 5)
        meters_on = len(ctx.hub.sources())
        sims = [n for n, on in (("machine", s.get("show_simulators")), ("meter", s.get("meters.sim"))) if on]
        rows = [
            group("Display", lambda: label_of(themes.available(), s.get("device.theme")),
                  "theme, brightness, screen size, rotation"),
            group("Controls", lambda: f"{n_buttons} buttons, {s.get('device.softkeys_edge')}",
                  "where the buttons are, what button 5 does, key map"),
            self._row("Units", "units", UNITS),
            group("Sound", lambda: "off" if not s.get("sound.speaker") else f"{s.get('sound.volume')}%",
                  "volume, chirps, tones for events, buzzer"),
            group("Meters", lambda: f"{meters_on} on" if meters_on else "none on",
                  "USB / serial meter, the Pi's ADC"),
            group("Simulators", lambda: ", ".join(sims) if sims else "off",
                  "a pretend machine, car and meter, to try things"),
            group("Installed options", "", "what this unit has fitted"),
            group("Storage", self._free_text, "logs folder, copy logs to USB"),
            group("Software", __version__, "updates, plugins, desktop app"),
            Row("About this device", select=self._about, hint="temperature, network, screen"),
            Row("Power", kind="header"),
            # Exits cleanly, so the Pi's service (Restart=on-failure) leaves the desktop showing.
            Row("Exit to desktop", select=lambda: confirm(
                self.app, "Exit to the desktop?",
                "CANopener closes and the Pi's desktop shows. Open it again from the menu (CANopener) or by "
                "restarting. A recording in progress is stopped and saved.", "Exit",
                lambda: setattr(self.app, "running", False)), hint="close CANopener"),
            Row("Restart", select=lambda: self._power("Restart", system.REBOOT)),
            Row("Power off", select=lambda: self._power("Power off", system.POWER_OFF)),
        ]
        return rows

    # --- the groups ---
    def _page_display(self):
        s = self.ctx.settings
        rows = [Row("Look", kind="header"),
                Row("Theme", lambda: label_of(themes.available(), s.get("device.theme")),
                    select=lambda: choose(self.app, "Theme", themes.available(), s.get("device.theme"),
                                          lambda v: self._set("device.theme", v)),
                    adjust=lambda d: self._set("device.theme", cycle(themes.available(), s.get("device.theme"), d)),
                    hint=f"add your own in {_home(themes.themes_dir())} (see the manual)"),
                self._row("Accent colour", "device.accent", themes.ACCENTS),
                Row("Screen", kind="header")]
        level = system.brightness()
        if level is not None:
            rows.append(Row("Brightness", f"{round(level * 100)}%", adjust=self._brightness,
                            select=lambda: self._brightness(1)))
        rows += [
            self._row("Screen size", "device.diagonal_in", DIAGONALS, hint="sizes touch targets to 10 mm"),
            self._row("Rotate picture", "device.rotate", ROTATIONS, hint="takes effect at the next start"),
            self._row("Window size (not full screen)", "device.window", WINDOWS, hint="trying it on a laptop; "
                      "takes effect at the next start"),
        ]
        return rows

    def _page_controls(self):
        return [
            Row("Buttons", kind="header"),
            self._row("Hardware buttons on the", "device.softkeys_edge", EDGES, hint="soft-key labels sit by them"),
            self._row("Number of buttons", "device.buttons", BUTTONS),
            self._row("Button 5 does", "device.button5", BUTTON5),
            self._row("Hold Select for Back", "device.long_press_s", LONG_PRESS),
            Row("Key map", select=self._keymap, hint="which key / button does what"),
        ]

    def _page_sound(self):
        ctx, s = self.ctx, self.ctx.settings
        return [
            Row("Speaker", kind="header"),
            self._row("Volume", "sound.volume", VOLUMES, lambda: ctx.play_tone("test"),
                      hint="the speaker (a buzzer has one loudness)"),
            self._toggle("Speaker", "sound.speaker"),
            Row("Test sound", select=lambda: ctx.play_tone("test")),
            Row("When", kind="header"),
            self._toggle("Chirp on taps and buttons", "sound.touch", lambda: ctx.cue("touch"),
                         hint="a short tick each time a press registers"),
            self._toggle("Chime when CAN traffic drops / returns", "sound.link_cues"),
            Row("Tones for events", select=lambda: self.app.push(TonesScreen(self.app)),
                hint="pick a sound (or none) for each: log started, codes found, ..."),
            Row("Buzzer", kind="header"),
            Row("Buzzer", lambda: s.get("sound.buzzer"), select=self._buzzer, hint="auto, off, or pwm:CHIP:CHANNEL"),
        ]

    def _page_meters(self):
        ctx, s = self.ctx, self.ctx.settings
        rows = [
            Row("Sources", kind="header"),
            Row("USB / serial meter", lambda: s.get("meters.serial") or "Off", select=self._serial,
                hint="Pico / ESP32 bridge, JSON lines"),
            Row("ADC on the Pi", lambda: s.get("meters.iio") or "Off", select=self._iio, hint="kernel IIO driver"),
            self._row("ADC input divider", "meters.iio_ratio", RATIOS, ctx.apply_meters),
            self._toggle("Simulated meter", "meters.sim", ctx.apply_meters, hint="four probes on a pretend sensor"),
        ]
        srcs = ctx.hub.sources()
        if srcs:
            rows.append(Row("Status", kind="header"))
        for src in srcs:
            rows.append(Row(src.name, src.status.text, kind="info", level=lambda src=src: src.status.level,
                            hint=src.status.detail))
        return rows

    def _page_simulators(self):
        ctx, s = self.ctx, self.ctx.settings
        return [
            Row("Try it without hardware", kind="header"),
            Row("Show simulators", select=lambda: ctx.set_show_simulators(not s.get("show_simulators")),
                check=lambda: s.get("show_simulators"), hint="a pretend machine and car under CAN > Connect"),
            self._toggle("Simulated meter", "meters.sim", ctx.apply_meters, hint="four probes on a pretend sensor"),
        ]

    def _page_installed(self):
        return self._hardware_rows()

    def _free_text(self) -> str:
        free = system.disk_free(config.data_dir() if os.path.isdir(config.data_dir()) else os.path.expanduser("~"))
        return f"{system.fmt_bytes(free[0])} free" if free else ""

    def _page_storage(self):
        free = system.disk_free(config.data_dir() if os.path.isdir(config.data_dir()) else os.path.expanduser("~"))
        return [
            Row("Logs", kind="header"),
            Row("Logs folder", config.data_dir(), kind="info",
                hint=f"{system.fmt_bytes(free[0])} free of {system.fmt_bytes(free[1])}" if free else ""),
            Row("Copy all logs to a USB stick", select=self._export_all),
        ]

    def _page_software(self):
        s = self.ctx.settings
        rows = [
            Row("CANopener", kind="header"),
            Row("Check for updates", __version__, select=self._check_updates,
                hint="download the newest CANopener from GitHub (needs internet)"),
            Row("Plugins", self._plugins_summary, select=self._plugins,
                hint="sign in to GitHub; install and update plugins (private ones too)"),
            Row("Desktop app instead", select=self._to_desktop, hint="switch to the laptop window (Qt)"),
        ]
        if s.get("device.games"):
            rows.append(Row("Show games", select=lambda: s.set("device.games", False), check=lambda: True,
                            hint="hide them again (System > About brings them back)"))
        return rows

    def _hardware_rows(self):
        """Installed options: what this unit has fitted. Not installed = never touched."""
        hw = self.ctx.hardware
        stages = [(f"Stage {st}", st) for st in hardware.STAGES]
        rows = [Row("Installed options", kind="header"),
                Row("Set all from build stage", select=lambda: choose(
                    self.app, "Built up to stage", stages, None,
                    lambda st: (hw.set_stage(st), self.ctx.apply_meters())),
                    hint="then change single parts below")]

        def setter(oid):
            return lambda v: (hw.set_state(oid, v), self.ctx.apply_meters())
        for opt in hardware.OPTIONS:
            st = hw.status(opt.id)
            rows.append(Row(f"{opt.name} (stage {opt.stage})",
                            lambda opt=opt: label_of(hardware.STATES, hw.state(opt.id)),
                            select=lambda opt=opt: choose(self.app, opt.name, hardware.STATES, hw.state(opt.id),
                                                          setter(opt.id)),
                            adjust=lambda d, opt=opt: setter(opt.id)(cycle(hardware.STATES, hw.state(opt.id), d)),
                            level=lambda opt=opt: hw.status(opt.id).level,
                            hint=f"{st.text}: {st.detail}" if st.detail else st.text))
        return rows

    # --- actions ---
    def _brightness(self, d):
        level = system.brightness() or 1.0
        ok, msg = system.set_brightness(round(level * 10 + d) / 10)
        if not ok:
            self.ctx.toast(msg)

    def _keymap(self):
        km = self.app.keymap
        lines = [f"{k.upper():<10} {ACTION_NAMES.get(a, a)}" for k, a in sorted(km.keys.items())]
        lines += ["", f"Button 5 (F5): {ACTION_NAMES.get(km.button5, km.button5)}",
                  "Rotary encoder: move; push: select; hold: back.",
                  "Change keys in settings.json (device.keys), e.g. {\"f6\": \"log\"}."]
        self.app.push(InfoDialog(self.app, "Key map", lines))

    def _buzzer(self):
        opts = [("Auto (find a beeper)", sound.AUTO), ("Off", "off")]
        opts += [(f"PWM {os.path.basename(p)} channel 0", f"pwm:{os.path.basename(p)[7:]}:0")
                 for p in sorted(glob.glob("/sys/class/pwm/pwmchip*"))]
        choose(self.app, "Buzzer", opts, self.ctx.settings.get("sound.buzzer"),
               lambda v: self._set("sound.buzzer", v))

    def _serial(self):
        opts = [("Off", "")] + [(p, p) for p in serial_ports()]
        choose(self.app, "USB / serial meter", opts, self.ctx.settings.get("meters.serial"),
               lambda v: self._set("meters.serial", v, self.ctx.apply_meters))

    def _iio(self):
        opts = [("Off", "")] + [(f"{name} ({os.path.basename(d)})", name) for d, name in meters.iio_devices()]
        choose(self.app, "ADC on the Pi", opts, self.ctx.settings.get("meters.iio"),
               lambda v: self._set("meters.iio", v, self.ctx.apply_meters))

    def _export_all(self):
        from ...core.capture import saved_captures
        items = saved_captures()
        drives = storage.removable_drives()
        if not items:
            self.ctx.toast("No saved logs yet")
            return
        if not drives:
            self.ctx.toast("No USB stick found. Plug one in and try again.")
            return
        ctx = self.ctx

        def copy(path):
            ctx.run_job("Copying to USB", lambda: storage.export_items(items, path),
                        lambda r: ctx.toast(f"Copied {r[1]} file(s) to {r[0]}"))
        choose(self.app, f"Copy {len(items)} log(s) to", [(d.describe(), d.path) for d in drives], None, copy)

    def _about(self):
        temp = system.cpu_temp()
        ips = system.ip_addresses()
        lines = [f"CANopener {__version__}", "",
                 f"CPU temperature: {temp:.0f} °C" if temp is not None else "CPU temperature: n/a",
                 f"Up for {system.fmt_duration(system.uptime())}",
                 "Network: " + (", ".join(f"{a} ({i})" for i, a in ips) if ips else "not connected"),
                 f"Screen: {self.app.size[0]}x{self.app.size[1]}, {self.app.theme.px_per_mm:.1f} px/mm",
                 f"Settings: {self.ctx.settings.file}", f"Logs: {config.logs_dir()}"]
        if self.app.encoder is not None:
            lines.append(f"Encoder: {self.app.encoder.path or self.app.encoder.error or 'looking'}")
        self.app.push(InfoDialog(self.app, "About", lines, corner=(self._games_label, self._toggle_games)))
        self._count_about()

    def _games_label(self) -> str:
        return "Hide games menu" if self.ctx.settings.get("device.games") else "Show games menu"

    def _toggle_games(self):
        on = not self.ctx.settings.get("device.games")
        self.ctx.settings.set("device.games", on)
        self.ctx.toast("Games: on the home screen" if on else "Games hidden")

    def _count_about(self):
        """Open About seven times in a row (each within 10 s of the last) and Games appears."""
        s = self.ctx.settings
        if s.get("device.games"):
            return
        now = time.monotonic()
        if self._about_opens and now - self._about_opens[-1] > UNLOCK_GAP_S:
            self._about_opens = []
        self._about_opens.append(now)
        left = UNLOCK_OPENS - len(self._about_opens)
        if left <= 0:
            self._about_opens = []
            s.set("device.games", True)
            self.ctx.toast("Games unlocked: they're on the home screen")
        elif left <= 3:
            self.ctx.toast(f"{left} more...")

    def _plugins(self):
        from .plugins import PluginsScreen
        self.app.push(PluginsScreen(self.app))

    @staticmethod
    def _plugins_summary():
        from ... import plugins
        n = len(plugins.loaded())
        return f"{n} loaded" if n else "none"

    # --- updates (core.updates: the same as the desktop app's Help > Check for updates) ---
    def _check_updates(self):
        self.ctx.run_job("Checking for updates", updates.check, self._update_checked)

    def _update_checked(self, st: updates.UpdateStatus):
        lines = [st.message]
        if st.commits:
            lines += ["", "What's new:"] + [f"- {c}" for c in st.commits[:15]]
            if len(st.commits) > 15:
                lines.append(f"  ... and {len(st.commits) - 15} more")
        if st.dirty:
            lines += ["", "Changed on this device (left alone): " + ", ".join(st.dirty[:6])]
        keys = [SoftKey("Install", self._install)] if st.available and not st.ahead else []
        self.app.push(InfoDialog(self.app, "Updates", lines, keys=keys))

    def _install(self):
        self.app.pop()                          # the Updates dialog
        confirm(self.app, "Install the update?",
                "CANopener downloads the new version and then asks to restart. Reading the bus carries on "
                "while it downloads.", "Install", self._run_install, danger=False)

    def _run_install(self):
        self.ctx.run_job("Installing the update", updates.apply, self._installed)

    def _installed(self, result):
        ok, msg = result
        if not ok:
            self.app.push(InfoDialog(self.app, "Update failed", msg.splitlines()))
            return

        def restart():
            self.app.restart = True
            self.app.running = False
        confirm(self.app, "Restart now?", msg + "\n\nA recording in progress is stopped and saved first.",
                "Restart", restart, danger=False)

    def _to_desktop(self):
        from ... import frontends
        ok, why = frontends.available(frontends.DESKTOP)
        if not ok:
            self.ctx.toast(why)
            return

        def go():
            self.ctx.settings.set("ui", frontends.DESKTOP)
            self.app.switch_to = frontends.DESKTOP
            self.app.running = False
        confirm(self.app, "Switch to the desktop app?",
                "CANopener closes and reopens as the desktop window, and opens that way from now on. "
                "Settings > Handheld (Pi) screen there switches back. A recording in progress is stopped and saved.",
                "Switch", go, danger=False)

    def _power(self, what, argv):
        def go():
            if self.ctx.unsaved:
                self.app.show_saving()
            self.ctx.shutdown()
            try:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.ctx.toast(f"{what} failed: {e}")
        confirm(self.app, f"{what}?", "A recording in progress is stopped and saved, and the bus disconnected.",
                what, go)

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class TonesScreen(Screen):
    """System > Sound > Tones for events: which tone (or none) each event plays. Changing one
    plays it, so you hear what you picked."""

    title = "Tones"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _tones(self) -> dict:
        return dict(self.ctx.settings.get("sound.tones") or {})

    def _current(self, event: str) -> str:
        return self._tones().get(event, sound.DEFAULT_TONES[event])

    def _set(self, event: str, tone: str):
        tones = self._tones()
        if tone == sound.DEFAULT_TONES[event]:
            tones.pop(event, None)
        else:
            tones[event] = tone
        self.ctx.settings.set("sound.tones", tones)
        self.ctx.play_tone(tone)

    def _rows(self):
        rows = [Row("Each event's sound", kind="header")]
        for event, label, _ in sound.EVENTS:
            hint = "only with Chirp on taps and buttons on" if event == "touch" else ""
            rows.append(Row(label, lambda e=event: label_of(TONE_OPTIONS, self._current(e)),
                            select=lambda e=event, lb=label: choose(self.app, lb, TONE_OPTIONS, self._current(e),
                                                                    lambda v, e=e: self._set(e, v)),
                            adjust=lambda d, e=event: self._set(e, cycle(TONE_OPTIONS, self._current(e), d)),
                            hint=hint))
        rows.append(Row("Back to the standard sounds", kind="action", select=self._reset))
        return rows

    def _reset(self):
        self.ctx.settings.set("sound.tones", {})
        self.ctx.toast("Standard sounds back")

    def softkeys(self):
        return [SoftKey("Play", self._play_focused), SoftKey("Standard", self._reset)]

    def _play_focused(self):
        row = self.rows.focused()
        event = next((e for e, label, _ in sound.EVENTS if row is not None and row.label == label), None)
        if event:
            self.ctx.play_tone(self._current(event))

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
