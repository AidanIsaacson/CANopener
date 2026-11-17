"""The bus explorer: every field on the bus, live, named or not. Pick one and give it a name.

The less guided way to map a machine (the Profile builder is the guided one): work a control and
watch what lights up at the top of the list, then pick it and name it from the list of plain
names. Names go into the profile in use (a profile on top of it when that one is built in or
from a plugin) or a new profile built on it, saved at once and used straight away by Live and
the data log. The model is ``core.fieldwatch`` (``ctx.fieldwatch``); saving is
``core.observed.save_entries``.
"""

import os
import time

import pygame

from ...core import changes, fieldwatch, observed, profiles
from .. import input as inp
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose

MOVING_SECS = 2.0           # a field that changed this recently is 'moving now' (a lit dot)
HOLD_SECS = 3.0             # after a key press the list holds its order this long
SORTS = (("moving", "Moving first"), ("bus", "Bus order"))
SHOWS = (("changing", "Changing"), ("unnamed", "Unnamed"), ("all", "All"))

HELP = ["Every value on the bus, live: named ones (from the profile in use or the standard tables) and the "
        "ones nobody has named yet.",
        "", "Work a control on the machine and watch the top of the list: with 'Moving first' whatever just "
            "changed comes up there, with a lit dot. The list holds still for a moment whenever you press a key.",
        "", "Select one to see it bigger with its recent history, then Name it to pick a name. The name is saved "
            "straight away into the profile in use (or a new one), and Live shows it.",
        "", "Show: Changing hides values that never moved; Unnamed hides the ones already named; All shows "
            "everything, the fixed ones too.",
        "", "It only listens: nothing is sent to the machine."]


def fmt_field(info: fieldwatch.FieldInfo | None, raw: int | None) -> str:
    """A field's value for a row: on / off for a bit, the decoded number for a named value, else raw."""
    if raw is None:
        return "--"
    if info is not None and info.is_bit:
        return "on" if raw else "off"
    if info is not None and info.scale:
        scale, offset, unit = info.scale
        v = raw * scale + offset
        return f"{v:.0f} {unit}".strip() if abs(scale) >= 1 else f"{v:.1f} {unit}".strip()
    return str(raw)


def field_title(info: fieldwatch.FieldInfo) -> str:
    """The known name, or where it is in words."""
    return info.known or f"{info.name or info.label}: {fieldwatch.place_words(info.where)}"


def moving_level(watch, key: str, now: float) -> str:
    t = watch.last_change(key)
    return "ok" if t is not None and now - t <= MOVING_SECS else "idle"


def name_choices() -> list:
    groups = []
    for group, _n, _u in observed.GENERIC_NAMES:
        if group not in groups:
            groups.append(group)
    return groups


def pick_name(app, then):
    """Group, then name, from the plain names (a handheld has no keyboard). ``then(group, name, unit)``."""
    def group_picked(group):
        names = [(n, (group, n, u)) for g, n, u in observed.GENERIC_NAMES if g == group]
        choose(app, f"{group}: which one?", names, None, lambda v: then(*v))
    choose(app, "Name it: what kind of thing?", [(g, g) for g in name_choices()], None, group_picked)


def save_target_options(ctx) -> list:
    """(label, how) for where names go: the profile in use (or one on top of it) and a new profile."""
    active = profiles.active()
    opts = []
    target = observed.in_use_target(active)
    if target is not None:
        opts.append((target.label + (f" ({target.note})" if target.note else ""), "in_use"))
    base = active.name if active is not None else "nothing"
    opts.append((f"A new profile, built on {base}", "new"))
    return opts


class ExploreScreen(Screen):
    title = "Explore the bus"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)
        self.sort, self.show = "moving", "changing"
        self._order: list = []          # field keys in the order last shown
        self._touched = 0.0
        self.where_to = ""              # "in_use" / "new" once asked; "" = ask on the first name
        self.new_path = ""              # the new profile made this visit (later names go into it too)

    # --- model ---
    def _keys(self, now: float) -> list:
        watch = self.ctx.fieldwatch
        infos = {f.key: f for f in watch.fields()}
        keep = []
        for key, f in infos.items():
            if self.show != "all" and f.kind == changes.CONSTANT:
                continue
            if self.show != "all" and f.kind in (changes.COUNTER, changes.NOISE):
                continue
            if self.show == "unnamed" and f.known:
                continue
            keep.append(key)
        if now - self._touched < HOLD_SECS and self._order:
            known = [k for k in self._order if k in infos and k in keep]
            return known + [k for k in keep if k not in known]      # newcomers at the bottom
        if self.sort == "moving":
            keep.sort(key=lambda k: (-(watch.last_change(k) or 0.0), k))
        else:
            keep.sort()
        self._order = keep
        return keep

    def _rows(self):
        ctx, now = self.ctx, time.time()
        watch = ctx.fieldwatch
        rows = [Row(f"Profile in use: {ctx.profile_name()}", kind="info",
                    hint=f"{ctx.frames_per_second():.0f} frames a second" if ctx.live else "Not connected")]
        if not ctx.live:
            rows.append(Row("Connect to the machine's bus first", kind="info", level=lambda: "bad",
                            hint="CAN > Connect. Nothing to show until frames arrive."))
            return rows
        if not watch.judged:
            rows.append(Row("Listening: the list fills in a few seconds", kind="info", level=lambda: "wait"))
            return rows
        keys = self._keys(now)
        rows.append(Row(f"{len(keys)} values ({dict(SHOWS)[self.show].lower()}, {dict(SORTS)[self.sort].lower()})",
                        kind="header"))
        for key in keys:
            info = watch.info(key)
            if info is None:
                continue
            rows.append(Row(field_title(info), lambda k=key, i=info: fmt_field(i, watch.value(k)),
                            select=lambda k=key: self.open(k), level=lambda k=key: moving_level(watch, k, time.time()),
                            hint=(f"{info.label} {info.where}" if info.known else info.name or info.label)
                            + (f" · {watch.recent_changes(key)} changes in 10 s" if watch.recent_changes(key) else "")))
        return rows

    # --- keys ---
    def open(self, key: str):
        self.app.push(FieldScreen(self.app, self, key))

    def toggle_sort(self):
        self.sort = "bus" if self.sort == "moving" else "moving"
        self._touched = 0.0

    def cycle_show(self):
        names = [k for k, _ in SHOWS]
        self.show = names[(names.index(self.show) + 1) % len(names)]
        self._touched = 0.0

    def softkeys(self):
        return [SoftKey(dict(SORTS)["bus" if self.sort == "moving" else "moving"], self.toggle_sort),
                SoftKey(f"Show: {dict(SHOWS)[self.show]}", self.cycle_show),
                None,
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]

    def on_action(self, action, n=1):
        self._touched = time.time()
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.ctx.fieldwatch.refresh_soon()
        self.rows.draw(ui, rect)

    # --- naming (from the field page) ---
    def name_field(self, key: str, unsure: bool):
        """Pick a name and save it; asks where names go the first time."""
        def where_then(how):
            self.where_to = how
            pick_name(self.app, lambda group, name, unit: self._save(key, group, name, unit, unsure))
        if not self.where_to:
            choose(self.app, "Where should names go?", save_target_options(self.ctx), None, where_then)
        else:
            where_then(self.where_to)

    def _save(self, key: str, group: str, name: str, unit: str, unsure: bool):
        ctx = self.ctx
        watch = ctx.fieldwatch
        stream, f = watch.stream_field(key)
        if stream is None or f is None:
            ctx.toast("That value isn't on the bus any more: try again in a moment")
            return
        rel = None
        report = watch.report()
        if report is not None and f.kind == changes.VALUE:
            rel = next((r for r in report.relations() if r.stream is stream and r.field is f), None)
        cand = observed.Candidate(stream, f, 0.0, [f"Picked in the bus explorer ({fieldwatch.place_words(f.where)})"],
                                  rel)
        hint = cand.suggestion()
        same_unit = bool(hint) and hint.get("unit") == unit
        label = f"{name} ?" if unsure else name
        entry = observed.signal_entry(cand, label, unit, hint["scale"] if same_unit else 1.0,
                                      hint["offset"] if same_unit else 0.0, group, "unsure" if unsure else "checked")
        proto = ctx.protocol if ctx.protocol in profiles.GENERIC else profiles.J1939
        bus = observed.bus_fingerprint([k for k, _n, _r in watch.streams()])
        machine = dict(ctx.capture.machine)
        try:
            if self.where_to == "new" and self.new_path:
                path = observed.save_profile(observed.build_profile("", [entry], proto, "bus explorer", machine,
                                                                    base=observed.load_doc(self.new_path), bus=bus),
                                             self.new_path)
            elif self.where_to == "new":
                path = observed.save_entries([entry], profiles.active(),
                                             new_name=f"Explored {time.strftime('%Y-%m-%d %H%M')}",
                                             protocol=proto, source="bus explorer", machine=machine, bus=bus)
                self.new_path = path
            else:
                path = observed.save_entries([entry], profiles.active(), protocol=proto, source="bus explorer",
                                             machine=machine, bus=bus)
        except (OSError, profiles.ProfileError) as e:
            ctx.toast(f"Couldn't save the name: {e}")
            return
        ctx.apply_profile(os.path.splitext(os.path.basename(path))[0])
        watch.refresh()                     # so the row shows its new name now
        ctx.toast(f"Named {fieldwatch.place_words(f.where)} '{label}' in {ctx.profile_name()}")
        ctx.share_profile(path)


class FieldScreen(Screen):
    """One field, big: its value now, the last minute, what's known about it; Name it."""

    title = "Value"

    def __init__(self, app, explorer: ExploreScreen, key: str):
        super().__init__(app)
        self.explorer, self.key = explorer, key
        self.unsure = False

    def softkeys(self):
        return [SoftKey("Name it", self.name_it),
                SoftKey("Not sure (?)", self.toggle_unsure, active=self.unsure),
                None,
                SoftKey("Close", lambda: self.app.pop(self))]

    def toggle_unsure(self):
        self.unsure = not self.unsure

    def name_it(self):
        self.explorer.name_field(self.key, self.unsure)

    def on_action(self, action, n=1):
        if action in (inp.BACK, inp.SELECT):
            self.app.pop(self)
            return True
        return False

    def draw(self, ui, rect):
        t = ui.theme
        watch = self.ctx.fieldwatch
        info = watch.info(self.key)
        now = time.time()
        pad = t.pad * 2
        if info is None:
            ui.paragraph("This value isn't on the bus right now.", rect.inflate(-pad * 2, -pad * 2), "body", "dim")
            return
        y = rect.y + pad
        ui.text(field_title(info), (rect.x + pad, y), "big", "text", valign="top", max_w=rect.w - 2 * pad)
        y += t.font("big").get_linesize() + t.pad
        value = fmt_field(info, watch.value(self.key))
        lit = moving_level(watch, self.key, now) == "ok"
        vr = ui.text(value, (rect.x + pad, y), "huge", "accent" if lit else "text", valign="top",
                     max_w=rect.w // 2)
        y = vr.bottom + t.pad
        spark = pygame.Rect(rect.x + pad, y, rect.w - 2 * pad, max(t.touch, rect.h * .28))
        ui.panel(spark.inflate(t.pad, t.pad), "panel", t.radius)
        ui.sparkline(spark, watch.series(self.key, since=now - 60), "accent")
        y = spark.bottom + t.pad * 2
        facts = [f"Where: {info.label}, {fieldwatch.place_words(info.where)}  ({info.label} {info.where})",
                 f"Message: {info.name}" if info.name else "",
                 f"Named: {info.known}" if info.known else "Not named yet",
                 f"Behaves like: {_kind_words(info)}  ·  "
                 f"{watch.recent_changes(self.key, 60)} changes in the last minute",
                 "Marked 'not sure': the name gets a '?'" if self.unsure else ""]
        for line in [f for f in facts if f]:
            ui.text(line, (rect.x + pad, y), "small", "dim", valign="top", max_w=rect.w - 2 * pad)
            y += t.font("small").get_linesize() + 2


def _kind_words(info) -> str:
    return {changes.SWITCH: "a switch (on / off)" if info.is_bit else "a few set values",
            changes.VALUE: "a reading that moves", changes.CONSTANT: "fixed (it hasn't changed)",
            changes.COUNTER: "a counter (ticks over by itself)", changes.NOISE: "noise or a checksum"}.get(info.kind,
                                                                                                         info.kind)
