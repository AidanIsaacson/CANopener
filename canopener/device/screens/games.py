"""Games (hidden: System > About this device, seven times): Harness, and which of the machine's
signals can play it ("Controls from CAN").

Harness is played with the handheld turned a quarter turn anticlockwise, so the picture is drawn
portrait and turned to match: the encoder turns the piece, the d-pad's up / down (now left /
right) move it, its left (now down) drops it, and pushing the encoder swaps it for the side piece.
With just the screen: tap a column to move the piece there (tap its own column to turn it), and the
Turn / Swap / Drop buttons under the board.
"""

import time

import pygame

from ...core import profiles
from .. import game_controls as gc
from .. import harness as hz
from .. import input as inp
from ..ui import UI
from .base import Row, RowList, Screen, SoftKey, choose

BEST = "games.harness_best"


def status(ctx) -> str:
    best = int(ctx.settings.get(BEST) or 0)
    return f"Harness best: {best}" if best else "Harness"


def _profile_id() -> str:
    p = profiles.active()
    return p.id if p is not None else "generic"


class GamesScreen(Screen):
    title = "Games"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _rows(self):
        s = self.ctx.settings
        pid = _profile_id()
        p = profiles.active()
        rows = [Row("Harness", lambda: f"best {int(s.get(BEST) or 0)}", select=self._play,
                    hint="turn the handheld sideways; wire bar to bar (or back to the same bar)"),
                Row(f"Controls from CAN ({p.name if p else 'no profile'})", kind="header")]
        for do in gc.ACTIONS:
            b = gc.binding_for(s, pid, do)
            rows.append(Row(gc.ACTION_LABELS[do], (b or {}).get("signal", "Off"),
                            select=lambda do=do: self._bind(do), hint=_describe(b)))
        rows.append(Row("Pick from signals on the bus now (Live Data). Fine-tune in settings.json, "
                        "games.controls.", kind="info"))
        return rows

    def softkeys(self):
        return [SoftKey("Play", self._play)]

    def _play(self):
        self.app.push(HarnessScreen(self.app))

    def _bind(self, do):
        model = self.ctx.can_model()
        seen = {}
        for key in model.order:
            row = model.rows[key]
            if row.value is not None and row.sig.name not in seen:
                seen[row.sig.name] = row
        if not seen:
            self.ctx.toast("No signals on the bus yet: connect under CAN first")
            return
        opts = [("Off", None)] + [(f"{n}  ({r.value:g} {r.sig.unit})".rstrip(), n) for n, r in sorted(seen.items())]
        pid = _profile_id()
        cur = (gc.binding_for(self.ctx.settings, pid, do) or {}).get("signal")

        def pick(name):
            if name is None:
                gc.save_binding(self.ctx.settings, pid, None, do)
                return
            row = seen[name]
            b = gc.suggest(do, row.sig, row.lo, row.hi)
            if do == gc.COLUMN and "min" not in b:
                self.ctx.toast(f"Move {name} through its whole range first, then pick it again")
                return
            gc.save_binding(self.ctx.settings, pid, b, do)
        choose(self.app, gc.ACTION_LABELS[do], opts, cur, pick)

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


def _describe(b: dict | None) -> str:
    if not b:
        return ""
    if "min" in b:
        return f"{b['min']:g} to {b['max']:g} across the board"
    if "every" in b:
        return f"one step every {b['every']:g}"
    if "below" in b:
        return f"when it drops to {b['below']:g}"
    return f"when it reaches {b.get('above', 0.5):g}"


class HarnessScreen(Screen):
    title = "Harness"

    def __init__(self, app):
        super().__init__(app)
        self.game = hz.Harness(now=time.monotonic())
        self.controls = None
        self._saved = False

    def on_enter(self):
        self.controls = gc.SignalControls.for_profile(self.ctx.settings, _profile_id(), hz.COLS)

    def on_leave(self):
        if not self.game.paused and not self.game.over:
            self.game.toggle_pause(time.monotonic())

    def softkeys(self):
        g = self.game
        return [SoftKey("Resume" if g.paused else "Pause", self._pause, enabled=not g.over),
                SoftKey("New game", self._new)]

    def _pause(self):
        self.game.toggle_pause(time.monotonic())

    def _new(self):
        self.game, self._saved = hz.Harness(now=time.monotonic()), False

    def on_action(self, action, n=1):
        g, now = self.game, time.monotonic()
        if action == inp.BACK:
            if g.over or g.paused:
                return False                    # leave
            g.toggle_pause(now)
        elif action == inp.SELECT:
            if g.over:
                self._new()
            elif g.paused:
                g.toggle_pause(now)
            else:
                g.command(gc.SWAP, 1, now)
        elif action == inp.ROTATE:
            g.command(gc.TURN, n, now)
        elif action == inp.UP:                  # held sideways: the d-pad's up points left
            g.command(gc.LEFT, 1, now)
        elif action == inp.DOWN:
            g.command(gc.RIGHT, 1, now)
        elif action == inp.LEFT:                # ... and its left points down
            g.command(gc.DROP, 1, now)
        elif action != inp.RIGHT:
            return False
        return True

    # --- drawing ---
    def draw(self, ui, rect):
        g, now = self.game, time.monotonic()
        if self.controls is not None and not g.paused:
            for cmd, n in self.controls.poll(self.ctx.session):
                g.command(cmd, n, now)
        g.update(now)
        if g.over and not self._saved:
            self._saved = True
            if g.score > int(self.ctx.settings.get(BEST) or 0):
                self.ctx.settings.set(BEST, g.score)
        portrait = pygame.Surface((rect.h, rect.w))
        portrait.fill(ui.color("bg"))
        pu = UI(portrait, ui.theme)
        self._draw_portrait(pu, portrait.get_rect())
        ui.surface.blit(pygame.transform.rotate(portrait, -90), rect.topleft)    # top of the picture -> right
        ui.hit(rect, lambda: None)
        for pr, fn in pu.hits:                  # the picture's taps, turned the same way
            ui.hit(pygame.Rect(rect.right - pr.bottom, rect.y + pr.x, pr.h, pr.w), fn)

    # --- touch ---
    def _cmd(self, cmd, n=1):
        self.game.command(cmd, n, time.monotonic())

    def _tap_column(self, x):
        p = self.game.piece
        if p is not None and p.x == x:
            self._cmd(gc.TURN)
        else:
            self._cmd(gc.COLUMN, x)

    def _tap_overlay(self):
        if self.game.over:
            self._new()
        else:
            self._pause()

    def _draw_portrait(self, ui: UI, r: pygame.Rect):
        g, t = self.game, ui.theme
        head = round(r.h * .17)
        keys = t.touch                          # Turn / Swap / Drop along the bottom
        cell = min((r.w - 2 * t.pad) / (hz.COLS + .8), (r.h - head - keys - 3 * t.pad) / hz.ROWS)
        bar = cell * .4
        bw, bh = cell * hz.COLS, cell * hz.ROWS
        bx = r.centerx - bw / 2
        by = r.bottom - 2 * t.pad - keys - bh
        ui.hit(pygame.Rect(r.x, r.y, r.w, head), self._pause)      # tap the score: pause
        # header: side piece, score, next piece
        box = min(head - 2 * t.pad, cell * 1.6)
        side = pygame.Rect(r.x + t.pad, r.y + t.pad + t.touch * .4, box, box)
        nxt = pygame.Rect(r.right - t.pad - box, side.y, box, box)
        ui.text("Side", (side.centerx, r.y + t.pad + t.touch * .2), "small", "dim", align="center")
        ui.text("Next", (nxt.centerx, r.y + t.pad + t.touch * .2), "small", "dim", align="center")
        for b, mask in ((side, g.hold), (nxt, g.next)):
            ui.fill(b, "panel", t.radius)
            if mask is not None:
                self._tile(ui, b.inflate(-t.pad, -t.pad), mask, "text", "panel2")
        mid = r.centerx
        ui.text(f"{g.score}", (mid, r.y + head * .38), "title", "text", align="center")
        best = max(int(self.ctx.settings.get(BEST) or 0), g.score)
        ui.text(f"Level {g.level}   Best {best}", (mid, r.y + head * .78), "small", "dim", align="center")
        # board and bus bars
        ui.fill(pygame.Rect(bx, by, bw, bh), "panel", t.radius)
        for x in (bx - bar - 2, bx + bw + 2):
            ui.fill(pygame.Rect(x, by, bar, bh), "accent", t.radius)
        for x in range(hz.COLS):                # tap a column: the piece goes there (or turns)
            ui.hit(pygame.Rect(bx + x * cell, by, cell, bh), lambda x=x: self._tap_column(x))
        kw = (r.w - 4 * t.pad) / 3
        for i, (label, cmd) in enumerate((("Turn", gc.TURN), ("Swap", gc.SWAP), ("Drop", gc.DROP))):
            ui.button(pygame.Rect(r.x + t.pad + i * (kw + t.pad), r.bottom - t.pad - keys, kw, keys), label,
                      lambda cmd=cmd: self._cmd(cmd), enabled=not g.busy and not (cmd == gc.SWAP and g.held))
        live = self._live_cells()
        for y in range(hz.ROWS):
            for x in range(hz.COLS):
                mask = g.grid[y][x]
                if mask:
                    color = "focus" if (x, y) in g.lit else ("accent" if (x, y) in live else "dim")
                    self._tile(ui, pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-2, -2), mask,
                               color, "panel2")
        if g.piece is not None:
            gy = g.ghost_y()
            if gy is not None and gy != g.piece.y:
                ui.fill(pygame.Rect(bx + g.piece.x * cell, by + gy * cell, cell, cell).inflate(-4, -4),
                        "line", t.radius, 2)
            pr = pygame.Rect(bx + g.piece.x * cell, by + g.piece.y * cell, cell, cell).inflate(-2, -2)
            self._tile(ui, pr, g.piece.mask, "text", "panel2")
            ui.fill(pr, "focus", t.radius, max(2, t.pad // 3))
        if g.over or g.paused:
            msg = ("Game over", f"Score {g.score}", "Tap or push: new game  ·  Back: leave") if g.over else \
                ("Paused", "Tap or push: resume", "Back: leave")
            o = pygame.Rect(0, 0, bw * .9, t.touch * 2.6)
            o.center = (r.centerx, by + bh / 2)
            ui.fill(o, (0, 0, 0), t.radius)
            ui.fill(o, "accent", t.radius, 2)
            ui.text(msg[0], (o.centerx, o.y + o.h * .25), "big", "text", align="center")
            ui.text(msg[1], (o.centerx, o.y + o.h * .55), "body", "text", align="center")
            ui.text(msg[2], (o.centerx, o.y + o.h * .8), "small", "dim", align="center", max_w=o.w - t.pad)
            ui.hit(r, self._tap_overlay)

    def _live_cells(self) -> set:
        """Wires joined to a bus bar: drawn lit, so you can see how far each reaches."""
        g = self.game
        todo = [(0, y) for y in range(hz.ROWS) if g.grid[y][0] & hz.W]
        todo += [(hz.COLS - 1, y) for y in range(hz.ROWS) if g.grid[y][hz.COLS - 1] & hz.E]
        seen = set(todo)
        while todo:
            for n in g.neighbours(*todo.pop()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen

    @staticmethod
    def _tile(ui: UI, r: pygame.Rect, mask: int, wire, bg):
        t = ui.theme
        ui.fill(r, bg, max(2, t.radius // 2))
        c = ui.color(wire)
        w = max(3, round(r.w * .2))
        cx, cy = r.center
        ends = {hz.N: (cx, r.top), hz.E: (r.right, cy), hz.S: (cx, r.bottom), hz.W: (r.left, cy)}
        for bit, end in ends.items():
            if mask & bit:
                pygame.draw.line(ui.surface, c, (cx, cy), end, w)
        pygame.draw.circle(ui.surface, c, (cx, cy), w * .75)
