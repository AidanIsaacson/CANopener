"""The Zapi console mode on the handheld: a home tile, its setup and the messages going by.

Setup picks the tap on each wire and the framing; **Listen** opens them (listen-only) and the
page shows each message: who sent it, its bytes, and what we make of it. **Mark** tags the data
log, **Analyze** runs the protocol analysis on what's been heard, and button 5 starts / stops the
data log, which records the console lines beside the bus while the mode listens.
"""

import time

import pygame

from ...device import input as inp
from ...device.screens.base import InfoDialog, Row, RowList, Screen, SoftKey, choose
from ...device.ui import ListView, draw_icon
from ...serialline import port as sp
from . import shared_console
from .console import LINES

FRAMINGS = [f"{b} {f}" for b in (1200, 2400, 4800, 9600, 19200, 38400) for f in ("8N1", "8E1", "7E1")]


def status(ctx) -> str:
    con = getattr(ctx, "zapi_console", None)
    return con.status() if con is not None else "Serial console port"


def _console_icon(ui, r, c):
    """A handheld console: a body, its screen and four keys, and the lead."""
    lw = max(2, r.w // 14)
    body = pygame.Rect(r.x + r.w * .2, r.y + r.h * .05, r.w * .6, r.h * .78)
    pygame.draw.rect(ui.surface, c, body, lw, border_radius=r.w // 10)
    screen = pygame.Rect(body.x + body.w * .18, body.y + body.h * .14, body.w * .64, body.h * .3)
    pygame.draw.rect(ui.surface, c, screen, lw)
    for i in range(2):
        for j in range(2):
            pygame.draw.circle(ui.surface, c, (body.x + body.w * (.33 + .34 * i), body.y + body.h * (.62 + .2 * j)),
                               max(2, r.w // 18))
    pygame.draw.line(ui.surface, c, (body.centerx, body.bottom), (body.centerx, r.bottom), lw)


def icon(ui, r, c):
    draw_icon(ui, _console_icon, r, c)


class ZapiScreen(Screen):
    title = "Zapi console"

    def __init__(self, app):
        super().__init__(app)
        self.con = shared_console(self.ctx)
        self.setup = not self.con.listening
        self.rows = RowList(self._rows)
        self.view = ListView()
        self.follow = True              # stay on the newest message until the list is scrolled

    # --- setup rows ---
    def _rows(self):
        s = self.ctx.settings
        rows = [Row("Taps (one per wire)", kind="header")]
        for line, key, what in LINES:
            rows.append(Row(line.capitalize(), lambda key=key: s.get(key) or "not set",
                            select=lambda key=key, line=line: self._pick_port(key, line), hint=what))
        rows += [Row("Framing", lambda: s.get("zapi.framing") or "9600 8N1",
                     select=lambda: choose(self.app, "Framing", [(f, f) for f in FRAMINGS],
                                           s.get("zapi.framing"), lambda v: s.set("zapi.framing", v)),
                     hint="a guess until it's measured: canopener-cli serial baud"),
                 Row("Listen", kind="action", select=self.toggle_listen),
                 Row("Listen-only: nothing is sent to the controller.", kind="info",
                     hint="Wiring the taps: docs/SERIAL-SNIFFING.md")]
        return rows

    def _pick_port(self, key: str, line: str):
        if not sp.available():
            self.ctx.toast(sp.INSTALL_HINT)
            return
        options = [(f"{p.device}  {p.description}".strip(), sp.by_id(p.device)) for p in sp.list_ports()]
        options.append(("None", ""))
        choose(self.app, f"{line.capitalize()} tap", options, self.ctx.settings.get(key),
               lambda v: self.ctx.settings.set(key, v))

    # --- actions ---
    def toggle_listen(self):
        if self.con.listening:
            self.con.stop()
            self.ctx.serial = None
            self.ctx.toast("Stopped listening")
            return
        why = self.con.start()
        if why:
            self.ctx.toast(why)
            return
        self.ctx.serial = self.con.sniffer
        if self.ctx.recording:
            self.ctx.toast("Listening. The log running now started without it: stop and start the log to record it")
        else:
            self.ctx.toast("Listening")
        self.setup = False

    def analyze(self):
        r = self.con.analyze()
        if not r.messages:
            self.ctx.toast("Nothing heard yet")
            return
        self.app.push(InfoDialog(self.app, "What it heard", r.lines()))

    def softkeys(self):
        return [SoftKey("Stop" if self.con.listening else "Listen", self.toggle_listen, active=self.con.listening),
                SoftKey("Messages" if self.setup else "Setup", lambda: setattr(self, "setup", not self.setup)),
                SoftKey("Mark", self.ctx.tag, enabled=self.ctx.recording),
                SoftKey("Analyze", self.analyze, enabled=bool(self.con.chunks))]

    def key5(self):
        ctx = self.ctx
        return SoftKey("Stop log" if ctx.recording else "Start log", ctx.toggle_log, active=ctx.recording)

    def on_action(self, action, n=1):
        if self.setup:
            return self.rows.on_action(action, n)
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.follow = False
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1), len(self.con.recent(200)))
            return True
        if action == inp.SELECT:
            self.follow = True
            return True
        return False

    def on_leave(self):
        pass                            # keeps listening (and recording) in the background

    # --- drawing ---
    def draw(self, ui, rect):
        self.con.poll()
        t = ui.theme
        level = "bad" if self.con.state == "failed" else ("accent" if self.con.listening else "dim")
        top = pygame.Rect(rect.x, rect.y, rect.w, t.row)
        ui.text(self.con.status(), (top.x + t.pad, top.centery), "body", level, max_w=top.w - 2 * t.pad)
        body = pygame.Rect(rect.x, top.bottom, rect.w, rect.h - top.h)
        if self.setup:
            self.rows.draw(ui, body)
            return
        shown = self.con.recent(200)
        if not shown:
            msg = ("Nothing heard yet. Is the console plugged in and the key on?" if self.con.listening
                   else "Not listening: press Listen (set the taps up first, under Setup).")
            ui.paragraph(msg, body.inflate(-t.pad * 4, -t.pad * 4), "body", "dim")
            return
        if self.follow:
            self.view.focus = len(shown) - 1
        now = time.time()

        def draw_row(ui, i, r, focused):
            s = shown[i]
            inner = r.inflate(-t.pad // 2, -t.pad // 4)
            ui.panel(inner, "panel", t.radius)
            if focused and not self.follow:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            m = s.message
            who = ui.text(m.line[:10], (inner.x + t.pad, inner.y + inner.h * .32), "small",
                          "accent" if m.line == "console" else "text")
            ui.text(f"-{now - m.t:.1f}s", (inner.right - t.pad, inner.y + inner.h * .32), "small", "faint",
                    align="right")
            ui.text(s.hex, (who.right + t.pad * 2, inner.y + inner.h * .32), "small", "text",
                    max_w=inner.w - who.w - t.pad * 12)
            if s.text:
                ui.text(s.text, (inner.x + t.pad, inner.y + inner.h * .74), "small", "dim", max_w=inner.w - 2 * t.pad)

        self.view.draw(ui, body, len(shown), round(t.row * 1.1), draw_row)
