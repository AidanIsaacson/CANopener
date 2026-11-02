"""The launcher: one big tile per mode (CAN, Meter, Data log, System, and whatever comes next)."""

import pygame

from ... import __version__
from .. import input as inp
from .. import modes
from ..ui import GridView
from .base import Screen, SoftKey


class HomeScreen(Screen):
    title = ""

    def __init__(self, app):
        super().__init__(app)
        self.grid = GridView()

    def softkeys(self):
        return [SoftKey(m.title, lambda m=m: self.app.open_mode(m.id)) for m in modes.shown(self.ctx)[:4]]

    def on_action(self, action, n=1):
        g = self.grid
        if action == inp.ROTATE:
            g.move(n)
        elif action in (inp.LEFT, inp.RIGHT):
            g.move2d(-1 if action == inp.LEFT else 1, 0)
        elif action in (inp.UP, inp.DOWN):
            g.move2d(0, -1 if action == inp.UP else 1)
        elif action == inp.SELECT:
            ms = modes.shown(self.ctx)
            if ms:
                self.app.open_mode(ms[g.focus].id)
        else:
            return False
        return True

    def draw(self, ui, rect):
        t = ui.theme
        ms = modes.shown(self.ctx)
        foot = round(t.touch * .5)
        area = pygame.Rect(rect.x, rect.y + t.pad, rect.w, rect.h - foot - t.pad)
        tiles = self.grid.layout(area, len(ms), round(t.touch * 3.2), round(t.touch * 2.4))
        for i, (m, r) in enumerate(zip(ms, tiles)):
            focused = i == self.grid.focus
            ui.card(r, "panel_hi" if focused else "panel", t.radius * 2)
            if focused:
                ui.fill(r, "focus", t.radius * 2, max(3, t.pad // 2))
            size = round(min(r.h * .3, r.w * .3))
            block = size * 1.5 + t.touch * 1.2              # icon (on its disc), title and status, centred
            ir = pygame.Rect(0, 0, size, size)
            ir.midtop = (r.centerx, r.centery - block / 2 + size * .25)
            badge = ir.inflate(size * .5, size * .5)        # a soft disc behind the icon
            pygame.draw.circle(ui.surface, ui.color("accent_dim" if focused else "panel2"), badge.center,
                               badge.w // 2)
            m.icon(ui, ir, ui.color("text"))
            below = ir.bottom + size * .25                  # under the icon's disc
            ui.text(m.title, (r.centerx, below + t.touch * .45), "big", "text", align="center", max_w=r.w - t.pad)
            status = m.status(self.ctx) if m.status else ""
            if status:
                ui.text(status, (r.centerx, below + t.touch * .95), "small", "dim", align="center",
                        max_w=r.w - 2 * t.pad)
            ui.hit(r, lambda m=m: self.app.open_mode(m.id))
        ui.text(f"CANopener {__version__}", (rect.right, rect.bottom - foot // 2), "small", "faint", align="right")
