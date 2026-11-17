"""The meter: every measurement channel as a big readout with a trace, and each source's health.

Hold freezes what's on the screen: the readings, their min / max and the traces (the meter and
the data log keep running underneath). Double-tapping a channel enlarges it, with the others
stacked beside it; the enlarged channel is kept (``device.meter_zoom`` in settings.json), so it's
still enlarged after another screen or a restart.

The readings come from ``core.measure.MeasurementHub`` (sources in ``core.meters``); the same
channels show in CAN > Live and are recorded by the data log, on the bus's timebase.
"""

import copy

import pygame

from ...core import measure
from .. import input as inp
from ..ui import GridView
from .base import Row, RowList, Screen, SoftKey
from .live import draw_value_tile

ZOOM = "device.meter_zoom"          # the enlarged channel's row key, kept between screens and restarts


def status(ctx) -> str:
    srcs = ctx.hub.sources()
    if not srcs:
        return "No meter switched on"
    bad = [s for s in srcs if s.status.level == measure.BAD]
    if bad:
        return f"{bad[0].name}: {bad[0].status.text}"
    on, every = len(ctx.hub.channels()), len(ctx.hub.all_channels())
    return f"{on} channel(s)" + (f", {every - on} switched off" if every > on else "")


class InputsScreen(Screen):
    """Which inputs are in use: one with nothing on it can be switched off, so it isn't shown,
    charted or recorded. Meter setup (which meter, calibration) is one row down."""
    title = "Inputs"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _rows(self):
        ctx = self.ctx
        names = {src.id: src.name for src in ctx.hub.sources()}
        rows = [Row("Inputs in use", kind="header")]
        for ch in ctx.hub.all_channels():
            rows.append(Row(ch.name, check=lambda ch=ch: not ctx.hub.is_off(ch.id),
                            select=lambda ch=ch: ctx.set_meter_channel(ch.id, ctx.hub.is_off(ch.id)),
                            hint=f"{names.get(ch.source, ch.source)}: off = not shown, charted or recorded"))
        rows.append(Row("Meters", kind="header"))
        rows.append(Row("Meter setup", kind="menu", select=self._setup,
                        hint="which meter, the Pi's ADC, calibration"))
        return rows

    def _setup(self):
        from .settings import SettingsScreen
        self.app.push(SettingsScreen(self.app, section="Meters"))

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class MeterScreen(Screen):
    title = "Meter"

    def __init__(self, app):
        super().__init__(app)
        self.grid = GridView()
        self.hold: dict | None = None       # row key -> a copy of its row (value, min / max, trace) at Hold

    def softkeys(self):
        ctx = self.ctx
        if not ctx.hub.sources():
            return [SoftKey("Simulated", self.use_sim), SoftKey("Setup", self.setup)]
        return [SoftKey("Hold", self.toggle_hold, active=self.hold is not None),
                SoftKey("Inputs", self.inputs),
                SoftKey("Reset min/max", ctx.meter_live.reset_minmax),         # where Live has it
                SoftKey("Stop log" if ctx.recording else "Start log", ctx.toggle_log, active=ctx.recording)]

    def use_sim(self):
        self.ctx.settings.set("meters.sim", True)
        self.ctx.apply_meters()
        self.ctx.toast("Simulated meter on (System > Meters to turn it off)")

    def inputs(self):
        self.app.push(InputsScreen(self.app))

    def setup(self):
        from .settings import SettingsScreen
        self.app.push(SettingsScreen(self.app, section="Meters"))

    def toggle_hold(self):
        m = self.ctx.meter_live
        if self.hold is not None:
            self.hold = None
            return
        self.hold = {}
        for k, r in m.rows.items():
            frozen = copy.copy(r)
            frozen.points = list(r.points)      # the trace stops here too
            self.hold[k] = frozen

    def on_action(self, action, n=1):
        if action == inp.ROTATE:
            self.grid.move(n)
        elif action in (inp.LEFT, inp.RIGHT):
            self.grid.move2d(-1 if action == inp.LEFT else 1, 0)
        elif action in (inp.UP, inp.DOWN):
            self.grid.move2d(0, -1 if action == inp.UP else 1)
        elif action == inp.SELECT:
            self.toggle_hold()
        elif action == inp.BACK and self.grid.unzoom():
            self.ctx.settings.set(ZOOM, None)   # Back first puts an enlarged channel back in the grid
        else:
            return False
        return True

    def draw(self, ui, rect):
        t, ctx = ui.theme, self.ctx
        srcs = ctx.hub.sources()
        if not srcs:
            ui.paragraph("No meter is switched on.\n\nSimulated gives four probes on a pretend pedal sensor, "
                         "to try the meter and the data log. Setup chooses a USB meter (the Pico / ESP32 "
                         "bridge) or an ADC wired to the Pi.", rect.inflate(-t.pad * 4, -t.pad * 4), "body", "dim")
            return
        # source health along the top
        h = round(t.touch * .62)
        x = rect.x
        for s in srcs:
            w = min(round(t.touch * 5), (rect.w - t.pad * (len(srcs) - 1)) // len(srcs))
            ui.pill(pygame.Rect(x, rect.y, w, h), s.status.level, f"{s.name}: {s.status.text}")
            x += w + t.pad
        area = pygame.Rect(rect.x, rect.y + h + t.pad, rect.w, rect.h - h - t.pad)
        m = ctx.meter_live
        keys = list(m.order)
        if not keys:
            bad = next((s for s in srcs if s.status.detail), None)
            ui.paragraph(bad.status.detail if bad else "Waiting for readings...", area.inflate(-t.pad * 4, -t.pad * 4),
                         "body", "dim")
            return
        saved = ctx.settings.get(ZOOM)
        saved = tuple(saved) if saved else None
        self.grid.zoom = keys.index(saved) if saved in keys else None
        tiles = self.grid.layout(area, len(keys), round(t.touch * 4), round(t.touch * 2.2), 4)
        for i, (k, r) in enumerate(zip(keys, tiles)):
            if self.hold is not None and k in self.hold:
                draw_value_tile(ui, r, m, self.hold[k], ctx.units, i == self.grid.focus, "HOLD")
            else:
                draw_value_tile(ui, r, m, m.rows[k], ctx.units, i == self.grid.focus)
            ui.hit(r, lambda i=i: self._tap(i, keys))

    def _tap(self, i, keys):
        if self.grid.tap(i):                    # a double tap: enlarged, or back in the grid
            z = self.grid.zoom
            self.ctx.settings.set(ZOOM, list(keys[z]) if z is not None and z < len(keys) else None)
