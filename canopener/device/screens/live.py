"""Live Data: every decoded value on the bus *and* every meter probe, in one list.

Tick (Select / tap) the values that matter, then Grid shows them big with a trace, a probe
voltage next to engine speed (DESIGN.md's pedal-sensor use case, step 4). Double-tapping a tile
in the grid enlarges it, with the others stacked beside it. The models are
``core.live`` (J1939 / OBD-II) and ``core.measure.MeasurementLiveData`` (meters).

What's picked, the grid and the enlarged value are kept (``device.live_view`` in settings.json,
``SavedView``), so they're still there after another screen or a restart: a picked value comes
back as soon as it's on the bus again.

**System > Live data** groups the list under a heading per controller (who sends each value) or
per group (``core.live.sections``). Select (or a tap) on a heading folds it away; which ones are
folded is kept with the rest of the view.
"""

import time

import pygame

from ...core import profiles, units
from ...core.live import GROUP_NONE, GROUP_SOURCE, Collapsed, sections
from .. import input as inp
from ..ui import GridView, ListView
from .base import Screen, SoftKey

TRACE_SECONDS = 60
VIEW = "device.live_view"           # {"grid": bool, "picks": [[model, signal key, source], ...], "zoom": pick,
#                                      "collapsed": {grouping: [section id, ...]}}
GROUPING = "device.live_group"      # none / source / group (System > Live data)


class Head:
    """A section heading in the list: the model it's from and its ``core.live.Section``."""

    def __init__(self, model_id: str, model, section):
        self.model_id, self.model, self.section = model_id, model, section

    @property
    def id(self) -> str:
        return f"{self.model_id}:{self.section.id}"


def _ident(model_id: str, key) -> tuple:
    return (model_id, *key)


class SavedView:
    """The Live page's picks, grid and enlarged value, kept in settings. Picks saved earlier are
    picked again as their values turn up (a model's rows only exist once something arrives), and
    stay saved meanwhile, so a value that's slow to appear isn't forgotten."""

    def __init__(self, settings, key: str = VIEW):
        self.settings, self.key = settings, key
        saved = settings.get(key) or {}
        self.grid = bool(saved.get("grid"))
        self.zoom = tuple(saved["zoom"]) if saved.get("zoom") else None
        self.pending = [tuple(p) for p in saved.get("picks") or []]     # saved, not picked again yet
        self.collapsed = Collapsed(saved.get("collapsed"))

    def restore(self, models: dict):
        """Pick again the saved values that have turned up. ``models``: id -> LiveData."""
        still = []
        for p in self.pending:
            m = models.get(p[0])
            key = tuple(p[1:])
            if m is not None and key in m.rows:
                m.select(key, True)
            else:
                still.append(p)
        self.pending = still

    def picks(self, models: dict) -> list[tuple]:
        now = [_ident(mid, k) for mid, m in models.items() for k in m.selected if k in m.rows]
        return now + [p for p in self.pending if p not in now]

    def save(self, models: dict, grid: bool):
        self.grid = grid
        value = {"grid": grid, "picks": [list(p) for p in self.picks(models)],
                 "zoom": list(self.zoom) if self.zoom else None}
        if self.collapsed.to_dict():
            value["collapsed"] = self.collapsed.to_dict()
        if value != (self.settings.get(self.key) or {}):
            self.settings.set(self.key, value)

    def forget(self):
        self.pending, self.zoom = [], None


def fmt_value(model, row, system: str) -> tuple[str, str, str, bool]:
    """(value, unit, "min - max", out of range) for one live row."""
    sig = row.sig
    flag = getattr(model, "flags", {}).get(row.key, "")
    if row.value is None:
        return (flag or "--"), "", "", bool(flag)
    if isinstance(row.value, str):          # a text signal: the message, and the others it showed
        others = [m for m in getattr(row, "recent", ()) if m != row.value]
        return row.value, "", " · ".join(others[-3:]), False
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


def draw_fold_arrow(ui, r, folded: bool, color="accent"):
    """A heading's triangle: pointing right when folded, down when open (drawn: not every font
    has the arrow characters)."""
    if folded:
        pts = [(r.left, r.top), (r.right, r.centery), (r.left, r.bottom)]
    else:
        pts = [(r.left, r.top + r.h * .2), (r.right, r.top + r.h * .2), (r.centerx, r.bottom - r.h * .1)]
    pygame.draw.polygon(ui.surface, ui.color(color), pts)


def draw_value_tile(ui, r, model, row, system: str, focused: bool = False, source: str = ""):
    t = ui.theme
    value, unit, lohi, bad = fmt_value(model, row, system)
    ui.panel(r, "panel", t.radius * 2)
    if focused:
        ui.fill(r, "focus", t.radius * 2, max(3, t.pad // 2))
    pad = t.pad
    ui.text(row.sig.name, (r.x + pad, r.y + pad), "body", "dim", valign="top", max_w=r.w - 2 * pad)
    if source:
        ui.text(source, (r.right - pad, r.y + pad + t.font("body").get_linesize()), "small", "faint",
                align="right", valign="top", max_w=r.w - 2 * pad)
    roomy = r.h > t.touch * 4.5                         # an enlarged tile: bigger digits, a taller trace
    px = min(round(t.font_px["huge"] * 1.7), round(r.h * .3)) if roomy else min(t.font_px["huge"], round(r.h * .38))
    vr = ui.text(value, (r.x + pad, r.y + r.h * (.33 if roomy else .48)), "huge", "bad" if bad else "text", px=px,
                 max_w=r.w - 2 * pad - t.touch)
    if unit:
        ui.text(unit, (vr.right + pad // 2, vr.bottom - t.pad), "body", "dim", valign="center")
    small = t.font("small").get_linesize()
    top = r.y + r.h * (.52 if roomy else .68)           # the trace ends above the min - max line
    spark = pygame.Rect(r.x + pad, top, r.w - 2 * pad, max(4, r.bottom - pad - small - top))
    ui.sparkline(spark, trace_points(row, system), "warn" if bad else "accent")
    if lohi:
        ui.text(lohi, (r.right - pad, r.bottom - pad - small // 2), "small", "faint", align="right", valign="center")


class LivePage(Screen):
    title = "Live"

    def __init__(self, app):
        super().__init__(app)
        self.view = ListView()
        self.grid = GridView()
        self.saved = SavedView(self.ctx.settings)
        self.big = self.saved.grid
        self.only_picked = False
        self.entries: list = []

    # --- data ---
    def models(self):
        return [self.ctx.can_model, self.ctx.meter_live]

    def _by_id(self) -> dict:
        return {"can": self.ctx.can_model, "meter": self.ctx.meter_live}

    def _keep(self):
        """Pick again what was saved, and save what's picked now (only writes on a change)."""
        models = self._by_id()
        self.saved.restore(models)
        self.saved.save(models, self.big)

    def _ident_of(self, m, k) -> tuple:
        return _ident("meter" if m is self.ctx.meter_live else "can", k)

    def _tap_tile(self, i, items):
        if self.grid.tap(i):                    # a double tap: enlarged, or back in the grid
            z = self.grid.zoom
            self.saved.zoom = self._ident_of(*items[z]) if z is not None and z < len(items) else None

    @property
    def grouping(self) -> str:
        return self.ctx.settings.get(GROUPING) or GROUP_NONE

    def _entries(self):
        """(model, key) per value, with a ``Head`` before each section when the list is grouped
        (a folded section's values are left out)."""
        by = self.grouping
        out = []
        for mid, m in self._by_id().items():
            keys = [k for k in list(m.order) if not self.only_picked or k in m.selected]
            if by == GROUP_NONE or not keys:
                out += [(m, k) for k in keys]
                continue
            for sec in sections(m, by, keys):
                head = Head(mid, m, sec)
                out.append(head)
                if not self.saved.collapsed.is_folded(by, head.id):
                    out += [(m, k) for k in sec.keys]
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

    def key5(self):
        """Watching values is when a log is wanted."""
        ctx = self.ctx
        return SoftKey("Stop log" if ctx.recording else "Start log", ctx.toggle_log, active=ctx.recording)

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
        self.saved.forget()
        self.grid.unzoom()
        self.big = False

    def toggle(self, i):
        if 0 <= i < len(self.entries):
            e = self.entries[i]
            if isinstance(e, Head):         # a heading: fold or unfold its section
                self.saved.collapsed.toggle(self.grouping, e.id)
                self.entries = self._entries()
                return
            m, k = e
            m.select(k, k not in m.selected)

    def on_action(self, action, n=1):
        if self.big:
            if action == inp.ROTATE:
                self.grid.move(n)
            elif action in (inp.UP, inp.DOWN):
                self.grid.move2d(0, -1 if action == inp.UP else 1)
            elif action == inp.BACK and self.grid.unzoom():
                self.saved.zoom = None          # Back first puts an enlarged value back in the grid
            elif action in (inp.SELECT, inp.BACK):
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
        self._keep()
        system = self.ctx.units
        self.entries = self._entries()
        if self.big:
            items = self.picked()
            if not items and self.saved.pending:    # picked before: wait for them to turn up
                ui.paragraph("Waiting for the picked values to come in. Connect to the bus (Connect page), or "
                             "press List to pick others.", rect.inflate(-ui.theme.pad * 4, -ui.theme.pad * 4),
                             "body", "dim")
                return
            if not items:
                self.big = False
            else:
                ids = [self._ident_of(m, k) for m, k in items]
                self.grid.zoom = ids.index(self.saved.zoom) if self.saved.zoom in ids else None
                tiles = self.grid.layout(rect, len(items), round(ui.theme.touch * 4), round(ui.theme.touch * 2.2), 3)
                for i, ((m, k), r) in enumerate(zip(items, tiles)):
                    draw_value_tile(ui, r, m, m.rows[k], system, i == self.grid.focus, m.source_label(m.rows[k].sa))
                    ui.hit(r, lambda i=i: self._tap_tile(i, items))
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
            e = self.entries[i]
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            if isinstance(e, Head):
                folded = self.saved.collapsed.is_folded(self.grouping, e.id)
                ui.panel(inner, "panel2", t.radius)
                if focused:
                    ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
                n = len(e.section.keys)
                what = "value" if n == 1 else "values"
                if e.model is self.ctx.meter_live and self.grouping != GROUP_SOURCE:
                    what = "probe" if n == 1 else "probes"
                count = ui.text(f"{n} {what}", (inner.right - t.pad, inner.centery), "small", "dim", align="right")
                arrow = pygame.Rect(0, 0, t.touch * .3, t.touch * .3)
                arrow.midleft = (inner.x + t.pad + t.touch * .1, inner.centery)
                draw_fold_arrow(ui, arrow, folded)
                ui.text(e.section.title, (arrow.right + t.pad, inner.centery), "body", "accent",
                        max_w=count.left - arrow.right - 3 * t.pad)
                return
            m, k = e
            row = m.rows[k]
            ui.panel(inner, "panel", t.radius)
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
            src = "" if self.grouping == GROUP_SOURCE else m.source_label(row.sa)     # the heading says it
            sub = "   ".join(x for x in (src, lohi) if x)
            ui.text(sub, (box.right + t.pad, inner.y + inner.h * .76), "small", "faint", max_w=name_w)

        self.view.draw(ui, rect, len(self.entries), round(t.row * 1.15), draw_row, self.toggle)
