"""System settings: everything that isn't tied to one mode. Stored in ``core.settings``
(``~/.config/canopener/settings.json``), shared with every front-end.

Left / right on the d-pad changes the focused value; Select opens a list to pick from.
"""

import glob
import os
import subprocess

from ... import __version__
from ...core import config, meters, sound, storage, system, units
from ..input import ACTION_NAMES
from .base import InfoDialog, Row, RowList, Screen, choose, confirm, cycle, label_of


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
RATIOS = [("1 (straight to the ADC)", 1.0), ("6 (50k / 10k divider)", 6.0), ("11 (100k / 10k)", 11.0)]


def serial_ports() -> list[str]:
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/serial/by-id/*"))


class SettingsScreen(Screen):
    title = "System"

    def __init__(self, app, section: str | None = None):
        super().__init__(app)
        self.rows = RowList(self._rows)
        if section:
            rows = self.rows.refresh()
            i = next((i for i, r in enumerate(rows) if r.kind == "header" and r.label == section), 0)
            self.rows.view.focus = i + 1

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

    def _rows(self):
        ctx, s = self.ctx, self.ctx.settings
        rows = [Row("Display", kind="header")]
        level = system.brightness()
        if level is not None:
            rows.append(Row("Brightness", f"{round(level * 100)}%", adjust=self._brightness,
                            select=lambda: self._brightness(1)))
        rows += [
            self._row("Screen size", "device.diagonal_in", DIAGONALS, hint="sizes touch targets to 10 mm"),
            self._row("Rotate picture", "device.rotate", ROTATIONS, hint="takes effect at the next start"),
            self._row("Window size (not full screen)", "device.window", WINDOWS, hint="trying it on a laptop; "
                      "takes effect at the next start"),
            Row("Controls", kind="header"),
            self._row("Hardware buttons on the", "device.softkeys_edge", EDGES, hint="soft-key labels sit by them"),
            self._row("Number of buttons", "device.buttons", BUTTONS),
            self._row("Button 5 does", "device.button5", BUTTON5),
            self._row("Hold Select for Back", "device.long_press_s", LONG_PRESS),
            Row("Key map", select=self._keymap, hint="which key / button does what"),
            Row("Units", kind="header"),
            self._row("Units", "units", UNITS),
            Row("Sound", kind="header"),
            self._toggle("Speaker", "sound.speaker"),
            self._toggle("Chime when CAN traffic drops / returns", "sound.link_cues"),
            Row("Buzzer", lambda: s.get("sound.buzzer"), select=self._buzzer, hint="auto, off, or pwm:CHIP:CHANNEL"),
            Row("Test sound", select=lambda: ctx.cue("test")),
            Row("Meters", kind="header"),
            self._toggle("Simulated meter", "meters.sim", ctx.apply_meters, hint="four probes on a pretend sensor"),
            Row("USB / serial meter", lambda: s.get("meters.serial") or "Off", select=self._serial,
                hint="Pico / ESP32 bridge, JSON lines"),
            Row("ADC on the Pi", lambda: s.get("meters.iio") or "Off", select=self._iio, hint="kernel IIO driver"),
            self._row("ADC input divider", "meters.iio_ratio", RATIOS, ctx.apply_meters),
        ]
        for src in ctx.hub.sources():
            rows.append(Row(src.name, src.status.text, kind="info", level=lambda src=src: src.status.level,
                            hint=src.status.detail))
        rows.append(Row("Storage", kind="header"))
        free = system.disk_free(config.data_dir() if os.path.isdir(config.data_dir()) else os.path.expanduser("~"))
        rows.append(Row("Logs folder", config.data_dir(), kind="info",
                        hint=f"{system.fmt_bytes(free[0])} free of {system.fmt_bytes(free[1])}" if free else ""))
        rows.append(Row("Copy all logs to a USB stick", select=self._export_all))
        rows += [
            Row("System", kind="header"),
            Row("About this device", select=self._about),
            Row("Desktop app instead", select=self._to_desktop, hint="switch to the laptop window (Qt)"),
            Row("Close the app", select=lambda: confirm(self.app, "Close CANopener?",
                                                         "A recording in progress is stopped and kept.", "Close",
                                                         lambda: setattr(self.app, "running", False))),
            Row("Restart", select=lambda: self._power("Restart", system.REBOOT)),
            Row("Power off", select=lambda: self._power("Power off", system.POWER_OFF)),
        ]
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
        self.app.push(InfoDialog(self.app, "About", lines))

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
                "Settings > Handheld (Pi) screen there switches back. A recording in progress is stopped and kept.",
                "Switch", go, danger=False)

    def _power(self, what, argv):
        def go():
            self.ctx.shutdown()
            try:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.ctx.toast(f"{what} failed: {e}")
        confirm(self.app, f"{what}?", "A recording in progress is stopped and kept, and the bus disconnected.",
                what, go)

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
