"""The meter: every measurement channel as a big readout with a trace, and each source's health.

The readings come from ``core.measure.MeasurementHub`` (sources in ``core.meters``); the same
channels show in CAN > Live and are recorded by the data log, on the bus's timebase.
"""

import pygame

from ...core import measure
from .. import input as inp
from ..ui import GridView
from .base import Screen, SoftKey
from .live import draw_value_tile


def status(ctx) -> str:
    srcs = ctx.hub.sources()
    if not srcs:
        return "No meter switched on"
    bad = [s for s in srcs if s.status.level == measure.BAD]
    if bad:
        return f"{bad[0].name}: {bad[0].status.text}"
    return f"{len(ctx.hub.channels())} channel(s)"


class MeterScreen(Screen):
    title = "Meter"

    def __init__(self, app):
        super().__init__(app)
        self.grid = GridView()
        self.hold: dict | None = None       # row key -> (value, lo, hi) frozen by Hold

    def softkeys(self):
        ctx = self.ctx
        if not ctx.hub.sources():
            return [SoftKey("Simulated", self.use_sim), None, None, SoftKey("Setup", self.setup)]
        return [SoftKey("Hold", self.toggle_hold, active=self.hold is not None),
                SoftKey("Reset min/max", ctx.meter_live.reset_minmax),
                SoftKey("Stop log" if ctx.recording else "Log", ctx.toggle_log, active=ctx.recording),
                SoftKey("Setup", self.setup)]

    def use_sim(self):
        self.ctx.settings.set("meters.sim", True)
        self.ctx.apply_meters()
        self.ctx.toast("Simulated meter on (System > Meters to turn it off)")

    def setup(self):
        from .settings import SettingsScreen
        self.app.push(SettingsScreen(self.app, section="Meters"))

    def toggle_hold(self):
        m = self.ctx.meter_live
        self.hold = None if self.hold is not None else {k: (r.value, r.lo, r.hi) for k, r in m.rows.items()}

    def on_action(self, action, n=1):
        if action == inp.ROTATE:
            self.grid.move(n)
        elif action in (inp.LEFT, inp.RIGHT):
            self.grid.move2d(-1 if action == inp.LEFT else 1, 0)
        elif action in (inp.UP, inp.DOWN):
            self.grid.move2d(0, -1 if action == inp.UP else 1)
        elif action == inp.SELECT:
            self.toggle_hold()
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
        tiles = self.grid.layout(area, len(keys), round(t.touch * 4), round(t.touch * 2.2), 4)
        for i, (k, r) in enumerate(zip(keys, tiles)):
            row = m.rows[k]
            if self.hold is not None and k in self.hold:
                saved = (row.value, row.lo, row.hi)
                row.value, row.lo, row.hi = self.hold[k]
                draw_value_tile(ui, r, m, row, ctx.units, i == self.grid.focus, "HOLD")
                row.value, row.lo, row.hi = saved
            else:
                draw_value_tile(ui, r, m, row, ctx.units, i == self.grid.focus)
            ui.hit(r, lambda i=i: setattr(self.grid, "focus", i))
