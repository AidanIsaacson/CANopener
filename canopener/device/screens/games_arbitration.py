"""Arbitration's screen (the rules are ``device/arbitration.py``): the frames one under the other
with their bits lined up, like a logic analyser, played the ordinary way up. Tap the frame that
wins, or press its soft key, or move the marker (d-pad, encoder) and push.
"""

import time

import pygame

from .. import arbitration as ab
from .. import game_controls as gc
from .. import input as inp
from ..ui import UI
from .base import Screen, SoftKey
from .games import ARBITRATION_BEST, _profile_id, encoder, result_card

SWEEP_S = 0.5               # how long the bits take to run across at the reveal
SHOW_BITS_LEVELS = 2        # the first levels show the bits while you choose
RULES = [
    "Several nodes start sending at the same instant. Only one frame gets the bus.",
    "The bits go out from the top; a 0 (dominant) beats a 1 (recessive). So: the lowest identifier wins.",
    "Pick the winner before the time runs out. Mistakes add to the error counter: at 256 it's bus-off.",
]


class ArbitrationScreen(Screen):
    title = "Arbitration"

    def __init__(self, app):
        super().__init__(app)
        self.game = ab.Arbitration(now=time.monotonic())
        self.controls = None
        self._reveal_t0 = 0.0
        self._new_best = False

    def on_enter(self):
        self.controls = gc.SignalControls.for_profile(self.ctx.settings, _profile_id(), 4)

    def on_leave(self):
        if self.game.phase == ab.ASK and not self.game.paused:
            self.game.toggle_pause(time.monotonic())

    def _best(self) -> int:
        return int(self.ctx.settings.get(ARBITRATION_BEST) or 0)

    def softkeys(self):
        g = self.game
        if g.paused:
            return [SoftKey("Resume", self._pause)]
        if g.phase == ab.ASK:
            return [SoftKey(f"Frame {i + 1}", lambda i=i: self._pick(i)) for i in range(len(g.frames))]
        return [SoftKey({ab.READY: "Start", ab.OVER: "New game"}.get(g.phase, "Next"), self._act)]

    def _pause(self):
        self.game.toggle_pause(time.monotonic())

    def _pick(self, i):
        self.game.pick(i, time.monotonic())

    def _act(self):
        g = self.game
        if g.paused:
            self._pause()
        elif g.over:
            self.game, self._new_best = ab.Arbitration(now=time.monotonic()), False
            self.game.start(time.monotonic())
        else:
            g.act(time.monotonic())

    def on_action(self, action, n=1):
        g = self.game
        if action == inp.BACK:
            if g.phase != ab.ASK or g.paused:
                return False                    # leave
            self._pause()
        elif action == inp.SELECT:
            self._act()
        elif action in (inp.UP, inp.LEFT):
            g.move(-1)
        elif action in (inp.DOWN, inp.RIGHT):
            g.move(1)
        elif action == inp.ROTATE:
            g.move(encoder(self.ctx, n))
        else:
            return False
        return True

    def _events(self, now: float):
        g = self.game
        for ev in g.events:
            if ev[0] in ("right", "wrong"):
                self._reveal_t0 = now
                self.ctx.cue("game_clear" if ev[0] == "right" else "game_wrong")
            elif ev[0] == "level":
                self.ctx.cue("game_level")
            elif ev[0] == "over":
                self.ctx.cue("game_over")
                if g.score > self._best():
                    self._new_best = True
                    self.ctx.settings.set(ARBITRATION_BEST, g.score)
        g.events.clear()

    # --- drawing ---
    def draw(self, ui: UI, rect: pygame.Rect):
        g, t, now = self.game, ui.theme, time.monotonic()
        if self.controls is not None and not g.paused:
            for cmd, n in self.controls.poll(self.ctx.session):
                g.command(cmd, n, now)
        g.update(now)
        self._events(now)
        head = round(t.touch * .9)
        top = pygame.Rect(rect.x, rect.y, rect.w, head)
        self._draw_header(ui, top, now)
        foot = t.font("body").get_linesize() + t.font("small").get_linesize() + t.pad * 2
        area = pygame.Rect(rect.x + t.pad, top.bottom + t.pad * 2, rect.w - 2 * t.pad,
                           rect.h - head - foot - t.pad * 3)
        if g.phase == ab.READY:
            lines = [("Arbitration", "big", "text")] + [(line, "small", "text") for line in RULES] + \
                    [("Tap or push to start", "body", "accent")]
            result_card(ui, rect, lines, rect.w * .9)
            ui.hit(rect, self._act)
            return
        if g.paused:                            # no looking at the frames with the clock stopped
            result_card(ui, rect, [("Paused", "big", "text"), ("Tap or push: resume", "body", "text"),
                                   ("Back: leave", "small", "dim")], rect.w * .6)
            ui.hit(rect, self._act)
            return
        self._draw_frames(ui, area, now)
        self._draw_footer(ui, pygame.Rect(rect.x, area.bottom, rect.w, rect.bottom - area.bottom), now)
        if g.phase == ab.OVER:
            best = max(self._best(), g.score)
            was = ("New best!", "body", "focus") if self._new_best else (f"Best {best}", "body", "dim")
            lines = [("Bus off", "big", "bad"), was,
                     (f"Score {g.score}", "big", "accent"),
                     (f"{g.wins} right of {g.rounds}  ·  best streak {g.best_streak}  ·  level {g.level}", "small",
                      "text"),
                     ("Tap or push: new game  ·  Back: leave", "small", "dim")]
            result_card(ui, rect, lines, rect.w * .6, "bad")
            ui.hit(rect, self._act)
        elif g.phase == ab.REVEAL:
            ui.hit(rect, self._act)

    def _draw_header(self, ui: UI, r: pygame.Rect, now: float):
        g, t = self.game, ui.theme
        ui.text(f"{g.score}", (r.x + t.pad * 2, r.centery), "title", "text")
        streak = f"   Streak {g.streak}" if g.streak > 1 else ""
        ui.text(f"Level {g.level}{streak}   Best {max(self._best(), g.score)}", (r.centerx, r.centery), "small", "dim",
                align="center")
        # the error counter: a bar to 256, marked where "warning" and "passive" start
        level = "bad" if g.tec >= ab.PASSIVE else ("wait" if g.tec >= ab.WARNING or g.tec else "ok")
        bar = pygame.Rect(0, 0, r.w * .22, max(6, t.pad))
        bar.midright = (r.right - t.pad * 2, r.centery + bar.h)
        ui.text(f"{g.state}  {min(g.tec, ab.BUS_OFF)}/{ab.BUS_OFF}", (bar.right, r.centery - bar.h), "small", level,
                align="right")
        ui.fill(bar, "panel_hi", bar.h // 2)
        if g.tec:
            ui.fill(pygame.Rect(bar.x, bar.y, bar.w * min(1, g.tec / ab.BUS_OFF), bar.h), level, bar.h // 2)
        for mark in (ab.WARNING, ab.PASSIVE):
            x = bar.x + bar.w * mark / ab.BUS_OFF
            pygame.draw.line(ui.surface, ui.color("bg"), (x, bar.y), (x, bar.bottom - 1), 2)
        # the time left
        left = g.time_left(now)
        line = pygame.Rect(r.x + t.pad, r.bottom, r.w - 2 * t.pad, max(4, t.pad // 2))
        ui.fill(line, "panel_hi", line.h // 2)
        if left > 0:
            ui.fill(pygame.Rect(line.x, line.y, line.w * left, line.h), "bad" if left < .25 else "accent", line.h // 2)

    def _draw_frames(self, ui: UI, area: pygame.Rect, now: float):
        g, t = self.game, ui.theme
        n = len(g.frames)
        revealed = g.phase in (ab.REVEAL, ab.OVER)
        show = revealed or g.level <= SHOW_BITS_LEVELS
        sweep = min(1.0, (now - self._reveal_t0) / SWEEP_S) if revealed else 1.0
        row_h = min((area.h - (n - 1) * t.pad) / n, t.touch * 2.2)
        y0 = area.centery - (row_h * n + t.pad * (n - 1)) / 2
        label_w = round(area.w * .36)
        slots = max(len(f.bits()) for f in g.frames)
        won = g.frames[g.win]
        for i, f in enumerate(g.frames):
            r = pygame.Rect(area.x, y0 + i * (row_h + t.pad), area.w, row_h)
            is_win = revealed and i == g.win
            wrong = revealed and i == g.picked and not is_win
            ui.card(r, "panel2" if is_win else "panel", t.radius)
            if is_win or wrong:
                ui.fill(r, "ok" if is_win else "bad", t.radius, 3)
            elif not revealed and i == g.focus:
                ui.fill(r, "focus", t.radius, max(2, t.pad // 3))
            badge = pygame.Rect(r.x + t.pad, r.y + t.pad, row_h * .34, r.h - 2 * t.pad)
            ui.fill(badge, "ok" if is_win else ("bad" if wrong else "panel_hi"), t.radius)
            ui.text(str(i + 1), badge.center, "body", "text", align="center")
            x = badge.right + t.pad * 1.5
            lw = label_w - (x - r.x)
            ui.text(f.label, (x, r.y + r.h * .36), "big", "text", max_w=lw)
            tag = f.tag + (f"  ·  first 11 bits 0x{f.top11:03X}" if f.extended and g.kind == ab.MIXED else "")
            ui.text(tag, (x, r.y + r.h * .74), "small", "dim", max_w=lw)
            track = pygame.Rect(r.x + label_w, r.y + t.pad, r.w - label_w - t.pad, r.h - 2 * t.pad)
            self._draw_bits(ui, track, f, slots, show, sweep, None if not revealed else ab.lost_at(f, won), is_win)
            if not revealed:
                ui.hit(r, lambda i=i: self._pick(i))

    @staticmethod
    def _draw_bits(ui: UI, r: pygame.Rect, f, slots: int, show: bool, sweep: float, lost, is_win: bool):
        """One frame's arbitration field as a trace: high = recessive (1), low = dominant (0). At
        the reveal a frame's trace stops at the bit where it backed off."""
        t = ui.theme
        ui.fill(r, "bg", max(2, t.radius // 2))
        sw = r.w / slots
        hi, lo = r.y + r.h * .25, r.bottom - r.h * .25
        if not show:
            pygame.draw.line(ui.surface, ui.color("line"), (r.x + sw / 2, r.centery), (r.right - sw / 2, r.centery), 1)
            return
        bits = f.bits()
        upto = min(len(bits), int(slots * sweep + .999))
        end = upto if lost is None else min(upto, lost + 1)
        w = max(2, round(r.h * .08))
        color = ui.color("ok" if is_win else ("dim" if lost is not None else "accent"))
        pts = []
        for i in range(end):
            y = hi if bits[i] else lo
            pts += [(r.x + i * sw, y), (r.x + (i + 1) * sw, y)]
        if len(pts) > 1:
            pygame.draw.lines(ui.surface, color, False, pts, w)
        for i in range(1, slots):               # bit boundaries
            pygame.draw.line(ui.surface, ui.color("panel"), (r.x + i * sw, r.y + 2), (r.x + i * sw, r.bottom - 3), 1)
        if lost is not None and lost < upto:    # where it sent a 1, saw a 0 and backed off
            mark = pygame.Rect(r.x + lost * sw, r.y, sw, r.h)
            ui.fill(mark, "bad", max(2, t.radius // 3), 2)
            ui.text(f.bit_names()[lost], (min(mark.right + t.pad, r.right - t.pad * 6), r.centery), "small", "bad")

    def _draw_footer(self, ui: UI, r: pygame.Rect, now: float):
        g, t = self.game, ui.theme
        y1 = r.y + t.pad + t.font("body").get_linesize() / 2
        y2 = r.bottom - t.pad - t.font("small").get_linesize() / 2
        if g.phase == ab.ASK:
            ui.text("Which frame gets the bus?", (r.centerx, y1), "body", "text", align="center")
            note = g.new_lesson or ("These first levels show the bits: high is a 1 (recessive), low is a 0 "
                                    "(dominant)." if g.level <= SHOW_BITS_LEVELS else "")
            ui.text(note, (r.centerx, y2), "small", "accent" if g.new_lesson else "dim", align="center",
                    max_w=r.w - 2 * t.pad)
            return
        if g.correct:
            ui.text(f"Right: +{g.points}", (r.centerx, y1), "body", "ok", align="center")
        else:
            what = "Too late" if g.picked is None else "Wrong"
            go_on = "" if g.over else "  Tap or push to go on."
            ui.text(f"{what}: frame {g.win + 1} wins.{go_on}", (r.centerx, y1), "body", "bad", align="center")
        ui.text(ab.reason(g.frames, g.win), (r.centerx, y2), "small", "text", align="center", max_w=r.w - 2 * t.pad)
