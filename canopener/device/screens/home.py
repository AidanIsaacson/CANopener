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
        return [SoftKey(m.title, lambda m=m: self.app.open_mode(m.id)) for m in modes.modes()[:4]]

    def on_action(self, action, n=1):
        g = self.grid
        if action == inp.ROTATE:
            g.move(n)
        elif action in (inp.LEFT, inp.RIGHT):
            g.move2d(-1 if action == inp.LEFT else 1, 0)
        elif action in (inp.UP, inp.DOWN):
            g.move2d(0, -1 if action == inp.UP else 1)
        elif action == inp.SELECT:
            ms = modes.modes()
            if ms:
                self.app.open_mode(ms[g.focus].id)
        else:
            return False
        return True

    def draw(self, ui, rect):
        t = ui.theme
        ms = modes.modes()
        foot = round(t.touch * .5)
        area = pygame.Rect(rect.x, rect.y + t.pad, rect.w, rect.h - foot - t.pad)
        tiles = self.grid.layout(area, len(ms), round(t.touch * 3.2), round(t.touch * 2.4))
        for i, (m, r) in enumerate(zip(ms, tiles)):
            ui.fill(r, "panel", t.radius * 2)
            if i == self.grid.focus:
                ui.fill(r, "focus", t.radius * 2, max(3, t.pad // 2))
            size = round(min(r.h * .42, r.w * .4))
            block = size + t.touch * 1.2                    # icon, title and status, centred in the tile
            ir = pygame.Rect(0, 0, size, size)
            ir.midtop = (r.centerx, r.centery - block / 2)
            m.icon(ui, ir, ui.color("text"))
            ui.text(m.title, (r.centerx, ir.bottom + t.touch * .45), "big", "text", align="center", max_w=r.w - t.pad)
            status = m.status(self.ctx) if m.status else ""
            if status:
                ui.text(status, (r.centerx, ir.bottom + t.touch * .95), "small", "dim", align="center",
                        max_w=r.w - 2 * t.pad)
            ui.hit(r, lambda m=m: self.app.open_mode(m.id))
        ui.text(f"CANopener {__version__}", (rect.right, rect.bottom - foot // 2), "small", "faint", align="right")
