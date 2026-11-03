"""Games (hidden: System > About this device, seven times): Harness, Cargo, and which of the
machine's signals can play them ("Controls from CAN").

Both games are played with the handheld turned a quarter turn anticlockwise, so the picture is
drawn portrait and turned to match: the encoder turns the piece, the d-pad's up / down (now left /
right) move it, its left (now down) drops it, and pushing the encoder swaps it for the side piece.
With just the screen: tap a column to move the piece there (tap the piece to turn it), and the
row of buttons under the board: move left, Turn, Swap, Drop, move right.
"""

import time

import pygame

from ...core import profiles
from .. import cargo as cg
from .. import game_controls as gc
from .. import harness as hz
from .. import input as inp
from ..ui import UI
from .base import Row, RowList, Screen, SoftKey, choose

BEST = "games.harness_best"
CARGO_BEST = "games.cargo_best"


def status(ctx) -> str:
    s = ctx.settings
    best = [f"{name} {int(s.get(key) or 0)}" for name, key in (("Harness", BEST), ("Cargo", CARGO_BEST))
            if int(s.get(key) or 0)]
    return "Best: " + ", ".join(best) if best else "Harness, Cargo"


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
                Row("Cargo", lambda: f"best {int(s.get(CARGO_BEST) or 0)}", select=self._play_cargo,
                    hint="turn the handheld sideways; pack full rows to ship them"),
                Row(f"Controls from CAN ({p.name if p else 'no profile'})", kind="header")]
        for do in gc.ACTIONS:
            b = gc.binding_for(s, pid, do)
            rows.append(Row(gc.ACTION_LABELS[do], (b or {}).get("signal", "Off"),
                            select=lambda do=do: self._bind(do), hint=_describe(b)))
        rows.append(Row("Pick from signals on the bus now (Live Data). Fine-tune in settings.json, "
                        "games.controls.", kind="info"))
        return rows

    def softkeys(self):
        return [SoftKey("Harness", self._play), SoftKey("Cargo", self._play_cargo)]

    def _play(self):
        self.app.push(HarnessScreen(self.app))

    def _play_cargo(self):
        self.app.push(CargoScreen(self.app))

    def _bind(self, do):
        model = self.ctx.can_model()
        seen = {}
        for key in model.order:
            row = model.rows[key]
            if row.value is not None and not isinstance(row.value, str) and row.sig.name not in seen:
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


def icon_left(ui: UI, r, c):
    pygame.draw.polygon(ui.surface, c, [(r.x + r.w * .22, r.centery), (r.right - r.w * .25, r.y + r.h * .18),
                                        (r.right - r.w * .25, r.bottom - r.h * .18)])


def icon_right(ui: UI, r, c):
    pygame.draw.polygon(ui.surface, c, [(r.right - r.w * .22, r.centery), (r.x + r.w * .25, r.y + r.h * .18),
                                        (r.x + r.w * .25, r.bottom - r.h * .18)])


class SidewaysGame(Screen):
    """What both games share: played sideways (drawn portrait, turned), the same controls (one
    command interface: buttons, encoder, touch, CAN signals), the score header with the side and
    next pieces, the button row and the pause / game-over card. A game supplies ``new_game``,
    ``COLS`` / ``ROWS``, ``best_key`` and the drawing of its board and pieces."""

    COLS, ROWS = 8, 12
    best_key = BEST
    bars = False                            # Harness: bus bars down both sides of the board

    def __init__(self, app):
        super().__init__(app)
        self.game = self.new_game(time.monotonic())
        self.controls = None
        self._saved = False
        self._was_lit = False
        self._portrait = None

    def new_game(self, now: float):
        raise NotImplementedError

    def on_enter(self):
        self.controls = gc.SignalControls.for_profile(self.ctx.settings, _profile_id(), self.COLS)

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
        self.game, self._saved, self._was_lit = self.new_game(time.monotonic()), False, False

    def _best(self) -> int:
        return int(self.ctx.settings.get(self.best_key) or 0)

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

    # --- per frame ---
    def draw(self, ui, rect):
        g, now = self.game, time.monotonic()
        if self.controls is not None and not g.paused:
            for cmd, n in self.controls.poll(self.ctx.session):
                g.command(cmd, n, now)
        g.update(now)
        if g.lit and not self._was_lit:
            self.ctx.cue("game_clear")
        self._was_lit = bool(g.lit)
        if g.over and not self._saved:
            self._saved = True
            self.ctx.cue("game_over")
            if g.score > self._best():
                self.ctx.settings.set(self.best_key, g.score)
        portrait = self._portrait
        if portrait is None or portrait.get_size() != (rect.h, rect.w):     # made once, not every frame
            portrait = self._portrait = pygame.Surface((rect.h, rect.w))
        portrait.fill(ui.color("bg"))
        pu = UI(portrait, ui.theme)
        pu._cache = ui._cache                   # share rendered text between frames
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
        if p is not None and self._piece_under(x):
            self._cmd(gc.TURN)
        else:
            self._cmd(gc.COLUMN, x)

    def _piece_under(self, x) -> bool:
        return self.game.piece.x == x

    def _tap_overlay(self):
        if self.game.over:
            self._new()
        else:
            self._pause()

    # --- drawing ---
    def _draw_portrait(self, ui: UI, r: pygame.Rect):
        g, t = self.game, ui.theme
        head = round(r.h * .17)
        keys = t.touch                          # the button row along the bottom
        side_w = .8 if self.bars else .2
        cell = min((r.w - 2 * t.pad) / (self.COLS + side_w), (r.h - head - keys - 3 * t.pad) / self.ROWS)
        bw, bh = cell * self.COLS, cell * self.ROWS
        bx = r.centerx - bw / 2
        by = r.bottom - 2 * t.pad - keys - bh
        ui.hit(pygame.Rect(r.x, r.y, r.w, head), self._pause)      # tap the score: pause
        # header: side piece, score, next piece
        box = min(head - 2 * t.pad, cell * 2.2, r.w * .24)
        side = pygame.Rect(r.x + t.pad, r.y + t.pad + t.touch * .4, box, box)
        nxt = pygame.Rect(r.right - t.pad - box, side.y, box, box)
        ui.text("Side", (side.centerx, r.y + t.pad + t.touch * .2), "small", "dim", align="center")
        ui.text("Next", (nxt.centerx, r.y + t.pad + t.touch * .2), "small", "dim", align="center")
        for b, item in ((side, g.hold), (nxt, g.next)):
            ui.card(b, "panel", t.radius)
            if item is not None:
                self._draw_preview(ui, b.inflate(-t.pad, -t.pad), item)
        mid = r.centerx
        ui.text(f"{g.score}", (mid, r.y + head * .38), "title", "text", align="center")
        best = max(self._best(), g.score)
        ui.text(f"Level {g.level}   Best {best}", (mid, r.y + head * .78), "small", "dim", align="center")
        # board
        board = pygame.Rect(bx, by, bw, bh)
        ui.card(board.inflate(4, 4), "panel", t.radius)
        for x in range(1, self.COLS):           # faint lane lines help aim
            lx = bx + x * cell
            pygame.draw.line(ui.surface, ui.color("panel_hi"), (lx, by + 2), (lx, by + bh - 3), 1)
        if self.bars:
            bar = cell * .4
            for x in (bx - bar - 2, bx + bw + 2):
                ui.fill(pygame.Rect(x, by, bar, bh), "accent", t.radius)
        for x in range(self.COLS):              # tap a column: the piece goes there (or turns)
            ui.hit(pygame.Rect(bx + x * cell, by, cell, bh), lambda x=x: self._tap_column(x))
        self._draw_board(ui, bx, by, cell)
        # buttons: move left, Turn, Swap, Drop, move right
        n = 5
        kw = (r.w - (n + 1) * t.pad) / n
        ky = r.bottom - t.pad - keys
        buttons = (("", gc.LEFT, icon_left), ("Turn", gc.TURN, None), ("Swap", gc.SWAP, None),
                   ("Drop", gc.DROP, None), ("", gc.RIGHT, icon_right))
        for i, (label, cmd, icon) in enumerate(buttons):
            ui.button(pygame.Rect(r.x + t.pad + i * (kw + t.pad), ky, kw, keys), label,
                      lambda cmd=cmd: self._cmd(cmd), icon=icon,
                      enabled=not g.busy and not (cmd == gc.SWAP and g.held),
                      active=cmd == gc.DROP)
        if g.over or g.paused:
            msg = ("Game over", f"Score {g.score}", "Tap or push: new game  ·  Back: leave") if g.over else \
                ("Paused", "Tap or push: resume", "Back: leave")
            o = pygame.Rect(0, 0, max(bw * .9, r.w * .8), t.touch * 2.6)
            o.center = (r.centerx, by + bh / 2)
            ui.card(o, "panel2", t.radius * 2)
            ui.fill(o, "accent", t.radius * 2, 2)
            ui.text(msg[0], (o.centerx, o.y + o.h * .25), "big", "text", align="center")
            ui.text(msg[1], (o.centerx, o.y + o.h * .55), "body", "text", align="center")
            ui.text(msg[2], (o.centerx, o.y + o.h * .8), "small", "dim", align="center", max_w=o.w - t.pad)
            ui.hit(r, self._tap_overlay)

    def _draw_board(self, ui: UI, bx, by, cell):
        raise NotImplementedError

    def _draw_preview(self, ui: UI, box: pygame.Rect, item):
        raise NotImplementedError


class HarnessScreen(SidewaysGame):
    title = "Harness"
    COLS, ROWS = hz.COLS, hz.ROWS
    best_key = BEST
    bars = True

    def new_game(self, now):
        return hz.Harness(now=now)

    def _draw_preview(self, ui, box, mask):
        self._tile(ui, box, mask, "text", "panel2")

    def _draw_board(self, ui, bx, by, cell):
        g, t = self.game, ui.theme
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


# Crate colours, by shape: (fill, lighter top edge). Plain RGB, nothing blended.
CRATE_COLORS = {
    "single": ((150, 158, 172), (196, 202, 212)), "pair": ((64, 156, 255), (130, 192, 255)),
    "bar": ((47, 182, 96), (110, 214, 146)), "corner": ((240, 160, 40), (250, 200, 120)),
    "pallet": ((168, 120, 72), (206, 164, 118)), "p": ((110, 100, 220), (160, 152, 240)),
    "u": ((230, 90, 140), (244, 150, 186)), "v": ((40, 178, 190), (110, 214, 222)),
    "t": ((224, 200, 60), (240, 226, 140)), "l": ((230, 96, 60), (244, 152, 126)),
}


class CargoScreen(SidewaysGame):
    title = "Cargo"
    COLS, ROWS = cg.COLS, cg.ROWS
    best_key = CARGO_BEST

    def new_game(self, now):
        return cg.Cargo(now=now)

    def _piece_under(self, x) -> bool:
        return any(cx == x for cx, _ in self.game.piece.at())

    def _crate(self, ui: UI, r: pygame.Rect, kind: str, lit: bool = False):
        fill, edge = CRATE_COLORS.get(kind, CRATE_COLORS["single"])
        if lit:
            fill, edge = ui.color("focus"), ui.color("white")
        rad = max(2, ui.theme.radius // 3)
        pygame.draw.rect(ui.surface, fill, r, border_radius=rad)
        pygame.draw.line(ui.surface, edge, (r.x + rad, r.y + 1), (r.right - rad - 1, r.y + 1), max(1, r.h // 10))
        pygame.draw.rect(ui.surface, tuple(v * 3 // 5 for v in fill), r, max(1, r.w // 14), border_radius=rad)

    def _draw_preview(self, ui, box, kind):
        cells = cg.SHAPES[kind]
        w = 1 + max(x for x, _ in cells)
        h = 1 + max(y for _, y in cells)
        c = min(box.w / max(w, 3), box.h / max(h, 3))
        ox = box.centerx - w * c / 2
        oy = box.centery - h * c / 2
        for x, y in cells:
            self._crate(ui, pygame.Rect(ox + x * c, oy + y * c, c, c).inflate(-2, -2), kind)

    def _draw_board(self, ui, bx, by, cell):
        g, t = self.game, ui.theme
        for y, row in enumerate(g.grid):
            lit = y in g.lit
            for x, kind in enumerate(row):
                if kind is not None:
                    self._crate(ui, pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-2, -2), kind, lit)
        p = g.piece
        if p is not None:
            gy = g.ghost_y()
            if gy is not None and gy != p.y:
                for x, y in p.at(y=gy):
                    ui.fill(pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-4, -4), "line",
                            max(2, t.radius // 3), 2)
            for x, y in p.at():
                self._crate(ui, pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-2, -2), p.kind)
