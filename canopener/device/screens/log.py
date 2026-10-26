"""The data log: record the bus *and* every meter at once, tag timestamps, save, trim, copy to USB.

Device-wide on purpose: it records whatever the instrument can read (CAN frames and every
meter channel), so it's a top-level mode and also a page in CAN. Start/stop and Timestamp also
work from any screen (the LOG / TAG actions, bindable to a hardware button). The capture logic
is ``core.capture``, shared with the desktop Log tool.
"""

import os

import pygame

from ...core import capture as cap
from ...core import config, storage
from ...core.capture import Capture, fmt_elapsed
from .. import input as inp
from ..ui import ListView
from .base import Screen, SoftKey, choose, confirm


def status(ctx) -> str:
    c = ctx.capture
    if c.state == cap.RECORDING:
        return f"Recording {fmt_elapsed(c.elapsed())[1:]}"
    if c.state == cap.STOPPED:
        return "Stopped: not saved yet"
    if ctx.pending_captures():
        return "Unsaved log found"
    return "Ready"


STATE_TEXT = {cap.IDLE: ("READY", "dim"), cap.RECORDING: ("RECORDING", "rec"),
              cap.STOPPED: ("STOPPED, NOT SAVED", "warn"), cap.SAVED: ("SAVED", "ok")}


class LogScreen(Screen):
    title = "Data log"

    def __init__(self, app):
        super().__init__(app)
        self.view = ListView()

    @property
    def c(self) -> Capture:
        return self.ctx.capture

    # --- actions ---
    def softkeys(self):
        ctx, c = self.ctx, self.c
        busy = bool(ctx.busy)
        if c.state == cap.RECORDING:
            return [SoftKey("Stop", ctx.stop_log, active=True), SoftKey("Timestamp", ctx.tag),
                    SoftKey("Undo tag", ctx.undo_tag, enabled=bool(c.tagged()))]
        if c.state == cap.STOPPED:
            return [SoftKey("Save", ctx.save_log, enabled=not busy), SoftKey("Trim", self.trim, enabled=not busy),
                    SoftKey("Discard", self.discard, enabled=not busy), SoftKey("New log", ctx.start_log)]
        if c.state == cap.SAVED:
            return [SoftKey("New log", ctx.start_log, enabled=not busy), SoftKey("Trim", self.trim, enabled=not busy),
                    SoftKey("Copy to USB", self.export, enabled=not busy)]
        keys = [SoftKey("Start", ctx.start_log, enabled=not busy), SoftKey("Plan", self.plan),
                SoftKey("+ Timestamp", ctx.tag)]
        if ctx.pending_captures():
            keys.append(SoftKey("Recover", self.recover))
        return keys

    def plan(self):
        templates = cap.load_templates()
        opts = [("Empty: just press Timestamp as things happen", None)]
        opts += [(f"{name}: {', '.join(labels[:4])}{'...' if len(labels) > 4 else ''}", name)
                 for name, labels in templates.items()]
        choose(self.app, "Timestamps to expect", opts, None,
               lambda name: self.ctx.set_plan(list(templates.get(name, [])) if name else []))

    def trim(self):
        tw = self.c.trim_window()
        if tw is None:
            self.ctx.toast("Tag at least one timestamp first: trimming keeps the time around them")
            return
        keep = fmt_elapsed(tw[1] - tw[0])[1:]
        c, ctx = self.c, self.ctx

        def go():
            def work():
                kept, dropped = c.trim(keep_full=True)
                c.save(system=ctx.units)
                return kept, dropped
            ctx.run_job("Trimming", work, lambda r: ctx.toast(f"Trimmed: kept {r[0]:,} frames, cut {r[1]:,}"))
        confirm(self.app, "Trim this log?",
                f"Keep 5 min before the first timestamp to 2 min after the last ({keep}), and cut the rest. "
                "The full recording is kept beside it as capture_full.log.", "Trim", go, danger=False)

    def discard(self):
        confirm(self.app, "Discard this log?", "The recording and its timestamps are deleted.", "Discard",
                self.ctx.discard_log)

    def recover(self):
        pending = self.ctx.pending_captures()
        opts = [(os.path.basename(p).replace(cap.PENDING_PREFIX, "Unsaved "), p) for p in pending]

        def load(folder):
            try:
                self.ctx.capture = Capture.load(folder)
            except (OSError, ValueError) as e:
                self.ctx.toast(f"Can't read it: {e}")
                return
            self.ctx.toast("Recovered: Save keeps it")
        choose(self.app, "Recover an unsaved log", opts, None, load)

    def export(self):
        drives = storage.removable_drives()
        folder = self.c.folder
        if not drives:
            self.ctx.toast("No USB stick found. Plug one in and try again.")
            return
        if not folder:
            return
        ctx = self.ctx

        def copy(path):
            ctx.run_job("Copying to USB", lambda: storage.export_items([folder], path),
                        lambda r: ctx.toast(f"Copied {r[1]} file(s) to {r[0]}"))
        choose(self.app, "Copy to", [(d.describe(), d.path) for d in drives], None, copy)

    def on_action(self, action, n=1):
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1), len(self.c.markers))
            return True
        if action == inp.SELECT:
            self.tap(self.view.focus)
            return True
        return False

    def tap(self, i):
        c = self.c
        if c.state == cap.RECORDING and 0 <= i < len(c.markers):
            j = c.tag(i)
            if j is not None:
                self.ctx.toast(f"Tagged {c.title(j)} at {fmt_elapsed(c.markers[j].t - c.started)}")

    # --- drawing ---
    def draw(self, ui, rect):
        t, ctx, c = ui.theme, self.ctx, self.c
        left = pygame.Rect(rect.x, rect.y, round(rect.w * .42), rect.h)
        right = pygame.Rect(left.right + t.pad, rect.y, rect.right - left.right - t.pad, rect.h)
        ui.fill(left, "panel", t.radius * 2)
        x, y = left.x + t.pad * 2, left.y + t.pad * 2
        text, color = STATE_TEXT.get(c.state, ("", "dim"))
        ui.text(text, (x, y), "big", color, valign="top", max_w=left.w - 4 * t.pad)
        y += round(t.touch * .8)
        if c.started:
            ui.text(fmt_elapsed(c.elapsed())[1:], (x, y), "huge", "text", valign="top")
            y += round(t.font_px["huge"] * 1.0)
        lines = []
        if c.state in (cap.RECORDING, cap.STOPPED, cap.SAVED):
            lines.append(f"{c.recorded_frames:,} CAN frames")
            if c.recorded_samples or ctx.hub.sources():
                lines.append(f"{c.recorded_samples:,} meter readings")
        if c.state == cap.RECORDING:
            lines.append(f"Next: {c.next_title()}")
        elif c.state == cap.IDLE:
            src = ctx.describe_connection() if ctx.live else "No bus connected"
            meters = len(ctx.hub.channels())
            lines += [src, f"{meters} meter channel(s)" if meters else "No meters on",
                      "Records everything on the bus and every meter channel, on one timeline."]
        if c.state == cap.SAVED and c.folder:
            lines.append(f"In {os.path.relpath(c.folder, config.data_dir())}")
        for line in lines:
            used = ui.paragraph(line, pygame.Rect(x, y, left.w - 4 * t.pad, left.bottom - y), "body", "dim")
            y += used + t.pad // 2
        # timestamps
        if not c.markers:
            ui.paragraph("No timestamps planned. Plan picks a list (key on, start, ...); Timestamp tags the next "
                         "one while recording, or adds one now.", right.inflate(-t.pad * 2, -t.pad * 2), "body", "dim")
            return

        def draw_row(ui, i, r, focused):
            m = c.markers[i]
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            ui.fill(inner, "panel2" if m.tagged else "panel", t.radius)
            if focused:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            when = fmt_elapsed(m.t - c.started) if m.tagged and c.started else "not tagged"
            wr = ui.text(when, (inner.right - t.pad, inner.centery), "body", "ok" if m.tagged else "faint",
                         align="right")
            ui.text(c.title(i), (inner.x + t.pad, inner.centery), "body", "text", max_w=wr.left - inner.x - 2 * t.pad)

        self.view.draw(ui, right, len(c.markers), t.row, draw_row, self.tap)
