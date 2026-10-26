"""Live Data: every decoded value on the bus *and* every meter probe, in one list.

Tick (Select / tap) the values that matter, then Grid shows them big with a trace, a probe
voltage next to engine speed (DESIGN.md's pedal-sensor use case, step 4). The models are
``core.live`` (J1939 / OBD-II) and ``core.measure.MeasurementLiveData`` (meters).
"""

import time

import pygame

from ...core import profiles, units
from .. import input as inp
from ..ui import GridView, ListView
from .base import Screen, SoftKey

TRACE_SECONDS = 60


def fmt_value(model, row, system: str) -> tuple[str, str, str, bool]:
    """(value, unit, "min - max", out of range) for one live row."""
    sig = row.sig
    flag = getattr(model, "flags", {}).get(row.key, "")
    if row.value is None:
        return (flag or "--"), "", "", bool(flag)
    text = sig.text(row.value) if getattr(sig, "enum", False) else None
    if text is not None:
        return text, "", "", False
    v, unit = units.convert(row.value, sig.unit, system)
    nd = units.display_decimals(sig.unit, unit, sig.decimals)
    lo = units.convert(row.lo, sig.unit, system)[0]
    hi = units.convert(row.hi, sig.unit, system)[0]
    lohi = "" if lo is None else f"{lo:.{nd}f} - {hi:.{nd}f}"
    return f"{v:.{nd}f}", unit, lohi, sig.out_of_range(row.value)


def trace_points(row, system: str) -> list:
    cut = (row.t or time.time()) - TRACE_SECONDS       # back from the newest value (a replayed log too)
    pts = list(row.points)[-TRACE_SECONDS * 20:]
    return [(t, units.convert(v, row.sig.unit, system)[0]) for t, v in pts if t >= cut]


def draw_value_tile(ui, r, model, row, system: str, focused: bool = False, source: str = ""):
    t = ui.theme
    value, unit, lohi, bad = fmt_value(model, row, system)
    ui.fill(r, "panel", t.radius * 2)
    if focused:
        ui.fill(r, "focus", t.radius * 2, max(3, t.pad // 2))
    pad = t.pad
    ui.text(row.sig.name, (r.x + pad, r.y + pad), "body", "dim", valign="top", max_w=r.w - 2 * pad)
    if source:
        ui.text(source, (r.right - pad, r.y + pad + t.font("body").get_linesize()), "small", "faint",
                align="right", valign="top", max_w=r.w - 2 * pad)
    px = min(t.font_px["huge"], round(r.h * .38))
    vr = ui.text(value, (r.x + pad, r.y + r.h * .48), "huge", "bad" if bad else "text", px=px,
                 max_w=r.w - 2 * pad - t.touch)
    if unit:
        ui.text(unit, (vr.right + pad // 2, vr.bottom - t.pad), "body", "dim", valign="center")
    spark = pygame.Rect(r.x + pad, r.y + r.h * .7, r.w - 2 * pad, r.h * .22)
    ui.sparkline(spark, trace_points(row, system), "warn" if bad else "accent")
    if lohi:
        ui.text(lohi, (r.right - pad, r.bottom - pad), "small", "faint", align="right", valign="center")


class LivePage(Screen):
    title = "Live"

    def __init__(self, app):
        super().__init__(app)
        self.view = ListView()
        self.grid = GridView()
        self.big = False
        self.only_picked = False
        self.entries: list = []

    # --- data ---
    def models(self):
        return [self.ctx.can_model, self.ctx.meter_live]

    def _entries(self):
        out = []
        for m in self.models():
            for k in list(m.order):
                if not self.only_picked or k in m.selected:
                    out.append((m, k))
        return out

    def picked(self):
        return [(m, k) for m in self.models() for k in m.selected if k in m.rows]

    def on_enter(self):
        ctx = self.ctx
        if ctx.focus_spn is not None:
            key = ctx.j1939_live.find_spn(ctx.focus_spn)
            if key is not None:
                ctx.j1939_live.select(key, True)
                self.big = True
                self.grid.focus = len(self.picked()) - 1
                ctx.toast("Showing the parameter behind the code")
            else:
                ctx.toast("That parameter isn't on the bus (yet)")
            ctx.focus_spn = None

    def _take_obd_focus(self):
        ctx = self.ctx
        if not ctx.focus_pids:
            return
        found = ctx.obd_live.find_pids(ctx.focus_pids)
        for k in found:
            ctx.obd_live.select(k, True)
        if found:
            self.big = True
            ctx.focus_pids = [p for p in ctx.focus_pids if p not in {ctx.obd_live.rows[k].sig.pid for k in found}]

    # --- input ---
    def softkeys(self):
        ctx = self.ctx
        keys = [SoftKey("List" if self.big else "Grid", self.toggle_big, enabled=bool(self.picked()) or self.big),
                SoftKey("All" if self.only_picked else "Picked", self.toggle_picked, active=self.only_picked),
                SoftKey("Reset min/max", self.reset)]
        if ctx.protocol == profiles.OBD2:
            st = ctx.obd_streamer
            keys.append(SoftKey("Stop" if st and st.running else "Stream", ctx.toggle_obd_stream,
                                enabled=st is not None, active=bool(st and st.running)))
        else:
            keys.append(SoftKey("Clear picks", self.clear_picks, enabled=bool(self.picked())))
        return keys

    def toggle_big(self):
        self.big = not self.big

    def toggle_picked(self):
        self.only_picked = not self.only_picked

    def reset(self):
        for m in self.models():
            m.reset_minmax()

    def clear_picks(self):
        for m in self.models():
            m.selected.clear()
        self.big = False

    def toggle(self, i):
        if 0 <= i < len(self.entries):
            m, k = self.entries[i]
            m.select(k, k not in m.selected)

    def on_action(self, action, n=1):
        if self.big:
            if action == inp.ROTATE:
                self.grid.move(n)
            elif action in (inp.UP, inp.DOWN):
                self.grid.move2d(0, -1 if action == inp.UP else 1)
            elif action == inp.SELECT:
                self.big = False
            elif action == inp.BACK:
                self.big = False
            else:
                return False
            return True
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1), len(self.entries))
        elif action == inp.SELECT:
            self.toggle(self.view.focus)
        else:
            return False
        return True

    # --- drawing ---
    def draw(self, ui, rect):
        self._take_obd_focus()
        system = self.ctx.units
        self.entries = self._entries()
        if self.big:
            items = self.picked()
            if not items:
                self.big = False
            else:
                tiles = self.grid.layout(rect, len(items), round(ui.theme.touch * 4), round(ui.theme.touch * 2.2), 3)
                for i, ((m, k), r) in enumerate(zip(items, tiles)):
                    draw_value_tile(ui, r, m, m.rows[k], system, i == self.grid.focus, m.source_label(m.rows[k].sa))
                    ui.hit(r, lambda i=i: setattr(self.grid, "focus", i))
                return
        if not self.entries:
            msg = ("Nothing yet. Connect to a bus (Connect page), or switch on a meter."
                   if not self.only_picked else "Nothing picked: press All, then tick values with Select.")
            if self.ctx.protocol == profiles.OBD2 and self.ctx.live:
                msg = "A car only answers when asked: press Stream."
            ui.paragraph(msg, rect.inflate(-ui.theme.pad * 4, -ui.theme.pad * 4), "body", "dim")
            return
        t = ui.theme

        def draw_row(ui, i, r, focused):
            m, k = self.entries[i]
            row = m.rows[k]
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            ui.fill(inner, "panel", t.radius)
            if focused:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            box = pygame.Rect(0, 0, t.touch * .5, t.touch * .5)
            box.midleft = (inner.x + t.pad, inner.centery)
            ui.checkbox(box, k in m.selected)
            value, unit, lohi, bad = fmt_value(m, row, system)
            vx = inner.right - t.pad - round(inner.w * .16)
            ui.text(unit, (inner.right - t.pad, inner.centery), "small", "dim", align="right")
            vr = ui.text(value, (vx, inner.centery), "big", "bad" if bad else "text", align="right",
                         max_w=round(inner.w * .3))
            name_w = vr.left - box.right - 3 * t.pad
            ui.text(row.sig.name, (box.right + t.pad, inner.y + inner.h * .36), "body", "text", max_w=name_w)
            sub = m.source_label(row.sa) + (f"   {lohi}" if lohi else "")
            ui.text(sub, (box.right + t.pad, inner.y + inner.h * .76), "small", "faint", max_w=name_w)

        self.view.draw(ui, rect, len(self.entries), round(t.row * 1.15), draw_row, self.toggle)
