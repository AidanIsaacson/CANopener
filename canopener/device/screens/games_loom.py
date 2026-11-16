"""Loom's screen (the rules are ``device/loom.py``): a board of wire tiles, played the ordinary
way up. D-pad: move the cursor. Encoder: turn the tile under it (push: a quarter turn). Touch:
tap a tile to turn it.
"""

import time

import pygame

from .. import game_controls as gc
from .. import input as inp
from .. import loom as lm
from ..ui import UI
from .base import InfoDialog, Screen, SoftKey
from .games import LOOM_LEVEL, _profile_id, draw_star, draw_wire, encoder, result_card

TURN_S = 0.12               # how long a tile takes to swing round
HELP = [
    "The loom has been pulled apart: every tile holds its piece of wire, turned the wrong way.",
    "Turn the tiles until the battery (the square) feeds every wire and every lamp (the round wire "
    "ends) is lit. Wire that's fed is drawn lit.",
    "There's no clock and nothing falls. Fewer turns than par earns three stars.",
    "Solving a level opens the next, bigger one.",
    "",
    "D-pad: move the cursor (it wraps round the edges). Encoder: turn the tile, either way. Push it: "
    "a quarter turn clockwise.",
    "Touch: tap a tile to turn it clockwise.",
]


class LoomScreen(Screen):
    title = "Loom"

    def __init__(self, app, level: int | None = None):
        super().__init__(app)
        self.game = lm.Loom(level or self.unlocked)
        self.controls = None
        self._spin: dict = {}               # (x, y) -> (when it was turned, quarter turns)

    @property
    def unlocked(self) -> int:
        return max(1, int(self.ctx.settings.get(LOOM_LEVEL) or 1))

    def on_enter(self):
        self.controls = gc.SignalControls.for_profile(self.ctx.settings, _profile_id(), self.game.cols)

    def softkeys(self):
        g = self.game
        return [SoftKey("Next level" if g.solved else "New board", self._next if g.solved else self._again),
                SoftKey("Level −", lambda: self._level(g.level - 1), enabled=g.level > 1),
                SoftKey("Level +", lambda: self._level(g.level + 1), enabled=g.level < self.unlocked),
                SoftKey("How to play", lambda: self.app.push(InfoDialog(self.app, "Loom: how to play", HELP)))]

    def _level(self, level: int):
        self.game, self._spin = lm.Loom(max(1, min(level, self.unlocked))), {}
        self.on_enter()

    def _again(self):
        self._level(self.game.level)

    def _next(self):
        self._level(self.game.level + 1)

    def on_action(self, action, n=1):
        g, now = self.game, time.monotonic()
        moves = {inp.UP: (0, -1), inp.DOWN: (0, 1), inp.LEFT: (-1, 0), inp.RIGHT: (1, 0)}
        if action in moves:
            g.move(*moves[action])
        elif action == inp.ROTATE:
            g.turn(encoder(self.ctx, n), now)
        elif action == inp.SELECT:
            if g.solved:
                self._next()
            else:
                g.turn(1, now)
        else:
            return False
        return True

    def _tap(self, x, y):
        if self.game.solved:
            self._next()
        else:
            self.game.turn(1, time.monotonic(), at=(x, y))

    def _events(self, now: float):
        g = self.game
        for ev in g.events:
            if ev[0] == "turn":
                self._spin[(ev[1], ev[2])] = (now, ev[3])
            elif ev[0] == "solved":
                self.ctx.cue("game_level")
                if g.level >= self.unlocked:
                    self.ctx.settings.set(LOOM_LEVEL, g.level + 1)
        g.events.clear()

    def draw(self, ui: UI, rect: pygame.Rect):
        g, t, now = self.game, ui.theme, time.monotonic()
        if self.controls is not None:
            for cmd, n in self.controls.poll(self.ctx.session):
                g.command(cmd, n, now)
        self._events(now)
        live = g.live()
        lamps = g.lamps()
        head = round(t.touch * .8)
        y = rect.y + head / 2 + t.pad // 2
        ui.text(f"Level {g.level}", (rect.x + t.pad * 2, y), "big", "text")
        ui.text(f"Turns {g.turns}   Par {g.par}", (rect.centerx, y), "body", "text", align="center")
        secs = int(g.elapsed(now))
        ui.text(f"Lamps {sum(c in live for c in lamps)}/{len(lamps)}   {secs // 60}:{secs % 60:02d}",
                (rect.right - t.pad * 2, y), "body", "dim", align="right")
        area = pygame.Rect(rect.x + t.pad, rect.y + head + t.pad, rect.w - 2 * t.pad, rect.h - head - 2 * t.pad)
        cell = int(min(area.w / g.cols, area.h / g.rows))
        bx = area.centerx - cell * g.cols // 2
        by = area.centery - cell * g.rows // 2
        gap = max(2, cell // 16)
        for ty in range(g.rows):
            for tx in range(g.cols):
                r = pygame.Rect(bx + tx * cell, by + ty * cell, cell, cell).inflate(-gap, -gap)
                fed = (tx, ty) in live
                ui.fill(r, "panel2" if fed else "panel", t.radius)
                turn = 0.0
                spin = self._spin.get((tx, ty))
                if spin is not None:
                    left = 1 - (now - spin[0]) / TURN_S
                    if left > 0:
                        turn = -90 * spin[1] * left * left      # swings in from where it was
                    else:
                        del self._spin[(tx, ty)]
                mask = g.grid[ty][tx]
                draw_wire(ui, r, mask, "accent" if fed else "dim", turn=turn)
                node = cell * .2
                if (tx, ty) == g.source:        # the battery
                    b = pygame.Rect(0, 0, node * 2.2, node * 2.2)
                    b.center = r.center
                    ui.fill(b, "focus", max(2, t.radius // 2))
                    k = node * .55
                    for a, c in (((-k, 0), (k, 0)), ((0, -k), (0, k))):
                        pygame.draw.line(ui.surface, ui.color("bg"), (b.centerx + a[0], b.centery + a[1]),
                                         (b.centerx + c[0], b.centery + c[1]), max(2, cell // 22))
                elif bin(mask).count("1") == 1:     # a lamp at the wire's end
                    if fed:
                        pygame.draw.circle(ui.surface, ui.color("focus"), r.center, node * 1.25)
                        pygame.draw.circle(ui.surface, ui.color("white"), r.center, node * .6)
                    else:
                        pygame.draw.circle(ui.surface, ui.color("panel"), r.center, node * 1.25)
                        pygame.draw.circle(ui.surface, ui.color("dim"), r.center, node * 1.25, max(2, cell // 22))
                if (tx, ty) == g.cursor and not g.solved:
                    ui.fill(r, "focus", t.radius, max(2, t.pad // 3))
                ui.hit(r, lambda tx=tx, ty=ty: self._tap(tx, ty))
        if g.solved:
            secs = int(g.elapsed(now))
            lines = [("Solved!", "big", "text"), ("", "title", "text"),
                     (f"{g.turns} turns (par {g.par})   {secs // 60}:{secs % 60:02d}", "body", "text"),
                     ("Tap or push: next level", "small", "dim")]
            o = result_card(ui, rect, lines, min(rect.w * .6, t.touch * 7))
            size = t.font("title").get_linesize()
            sy = o.y + t.pad * 3 + t.font("big").get_linesize() + size / 2
            for i in range(3):
                draw_star(ui, (o.centerx + (i - 1) * size * 1.2, sy), size * .5,
                          "focus" if i < g.stars else "line", 0 if i < g.stars else 2)
            ui.hit(rect, self._next)
