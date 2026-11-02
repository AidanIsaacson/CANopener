"""Cargo: the handheld's falling-block game (Games, next to Harness).

Crates of different shapes drop into a trailer one at a time. Steer each one, turn it, drop it;
a row packed wall to wall ships out and everything above settles down a row. Several rows at once
are worth more. The load reaches the top and the game is over.

The crates are Cargo's own: singles, pairs, a pallet, three-crate bars and corners, and five-crate
loads (P, U, V, T and L shapes), so the pieces come in very different sizes. There's a side bay to
swap the falling crate into (once per crate), like Harness.

Pure logic (no Pygame), driven by ``update(now)`` with the clock passed in, so tests can play it.
Every control goes through ``Cargo.command``, the same commands as Harness (``game_controls``),
so the buttons, the encoder, the touchscreen and the machine's own CAN signals all play it. The
screen is ``device/screens/games.py``.
"""

import random
from dataclasses import dataclass

from .game_controls import COLUMN, DROP, LEFT, PAUSE, RIGHT, SWAP, TURN

COLS, ROWS = 10, 18

# Shapes as (x, y) cells, and how often each comes up. Small crates are common so a gap can
# always be filled; the five-crate loads keep it interesting.
SHAPES = {
    "single": ((0, 0),),
    "pair": ((0, 0), (1, 0)),
    "bar": ((0, 0), (1, 0), (2, 0)),
    "corner": ((0, 0), (0, 1), (1, 1)),
    "pallet": ((0, 0), (1, 0), (0, 1), (1, 1)),
    "p": ((0, 0), (1, 0), (0, 1), (1, 1), (0, 2)),
    "u": ((0, 0), (2, 0), (0, 1), (1, 1), (2, 1)),
    "v": ((0, 0), (0, 1), (0, 2), (1, 2), (2, 2)),
    "t": ((0, 0), (1, 0), (2, 0), (1, 1), (1, 2)),
    "l": ((0, 0), (0, 1), (0, 2), (0, 3), (1, 3)),
}
WEIGHTS = {"single": 2, "pair": 4, "bar": 4, "corner": 5, "pallet": 3, "p": 3, "u": 2, "v": 2, "t": 2, "l": 3}
KINDS = tuple(SHAPES)

CLEAR_S = 0.3               # how long full rows light up before they ship
ROWS_PER_LEVEL = 8
ROW_POINTS = {1: 100, 2: 250, 3: 450, 4: 700, 5: 1000}     # rows shipped at once -> points (x level)
KICKS = (0, -1, 1, -2, 2)   # sideways nudges tried when a turn would hit a wall or the load


def fall_interval(level: int) -> float:
    """Seconds per row: 0.75 s at level 1, quicker each level, never below 0.1 s."""
    return max(0.1, 0.75 * 0.86 ** (level - 1))


def normalise(cells) -> tuple:
    mx = min(x for x, _ in cells)
    my = min(y for _, y in cells)
    return tuple(sorted((x - mx, y - my) for x, y in cells))


def turn_cells(cells, steps: int = 1) -> tuple:
    """Quarter turns clockwise (+) or anticlockwise (-), normalised to the top left."""
    for _ in range(steps % 4):
        cells = [(-y, x) for x, y in cells]
    return normalise(cells)


@dataclass
class Crate:
    kind: str
    cells: tuple            # (x, y) relative to (x, y) below
    x: int = 0
    y: int = 0

    @property
    def width(self) -> int:
        return 1 + max(cx for cx, _ in self.cells)

    def at(self, x: int | None = None, y: int | None = None, cells=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        return [(x + cx, y + cy) for cx, cy in (cells or self.cells)]


class Cargo:
    def __init__(self, seed: int | None = None, now: float = 0.0):
        self.rng = random.Random(seed)
        self.grid: list[list[str | None]] = [[None] * COLS for _ in range(ROWS)]   # grid[y][x]: kind or None
        self.score = 0
        self.rows_cleared = 0
        self.best_at_once = 0
        self.over = False
        self.paused = False
        self.hold: str | None = None
        self.held = False
        self.lit: set[int] = set()          # full rows lighting up before they ship
        self._clear_at = 0.0
        self.next = self._random()
        self.piece: Crate | None = None
        self._fall_at = now + fall_interval(1)
        self._spawn(self._take_next(), now)

    @property
    def level(self) -> int:
        return 1 + self.rows_cleared // ROWS_PER_LEVEL

    @property
    def busy(self) -> bool:
        return self.over or self.paused or self.piece is None

    # --- pieces ---
    def _random(self) -> str:
        return self.rng.choices(KINDS, [WEIGHTS[k] for k in KINDS])[0]

    def _take_next(self) -> str:
        kind, self.next = self.next, self._random()
        return kind

    def _spawn(self, kind: str, now: float):
        cells = SHAPES[kind]
        p = Crate(kind, cells)
        p.x = (COLS - p.width) // 2
        if not self._fits(p.at()):
            self.piece, self.over = None, True
            return
        self.piece, self.held = p, False
        self._fall_at = now + fall_interval(self.level)

    def _fits(self, cells) -> bool:
        return all(0 <= x < COLS and 0 <= y < ROWS and self.grid[y][x] is None for x, y in cells)

    # --- controls ---
    def command(self, cmd: str, n: int = 1, now: float = 0.0):
        """One control input, from whichever source. TURN: steps, + clockwise; LEFT / RIGHT:
        columns; COLUMN: the column to put the crate's middle over."""
        if cmd == COLUMN:
            if not self.busy:
                self.move(n - (self.piece.x + (self.piece.width - 1) // 2))
        elif cmd == LEFT:
            self.move(-n)
        elif cmd == RIGHT:
            self.move(n)
        elif cmd == DROP:
            self.drop(now)
        elif cmd == TURN:
            self.turn(n)
        elif cmd == SWAP:
            self.swap(now)
        elif cmd == PAUSE:
            self.toggle_pause(now)

    def move(self, dx: int) -> bool:
        step = 1 if dx > 0 else -1
        moved = False
        for _ in range(abs(dx)):
            if self.busy or not self._fits(self.piece.at(x=self.piece.x + step)):
                break
            self.piece.x += step
            moved = True
        return moved

    def turn(self, steps: int) -> bool:
        if self.busy or not steps % 4:
            return False
        p = self.piece
        cells = turn_cells(p.cells, steps)
        for dx in KICKS:
            if self._fits(p.at(x=p.x + dx, cells=cells)):
                p.cells, p.x = cells, p.x + dx
                return True
        return False

    def drop(self, now: float):
        if self.busy:
            return
        self.piece.y = self.ghost_y()
        self._lock(now)

    def swap(self, now: float):
        """Swap the falling crate for the one in the side bay (once per crate)."""
        if self.busy or self.held:
            return
        cur = self.piece.kind
        kind = self.hold if self.hold is not None else self._take_next()
        self.hold = cur
        self._spawn(kind, now)
        self.held = True

    def toggle_pause(self, now: float):
        if self.over:
            return
        self.paused = not self.paused
        if not self.paused:
            self._fall_at = now + fall_interval(self.level)
            self._clear_at = max(self._clear_at, now + CLEAR_S if self.lit else 0)

    # --- time ---
    def update(self, now: float):
        if self.over or self.paused:
            return
        if self.lit:
            if now >= self._clear_at:
                self._ship(now)
            return
        if self.piece is not None and now >= self._fall_at:
            p = self.piece
            if self._fits(p.at(y=p.y + 1)):
                p.y += 1
                self._fall_at = now + fall_interval(self.level)
            else:
                self._lock(now)

    def _lock(self, now: float):
        p, self.piece = self.piece, None
        for x, y in p.at():
            self.grid[y][x] = p.kind
        full = {y for y in range(ROWS) if all(self.grid[y][x] is not None for x in range(COLS))}
        if full:
            n = len(full)
            self.lit = full
            self._clear_at = now + CLEAR_S
            self.rows_cleared += n
            self.best_at_once = max(self.best_at_once, n)
            self.score += ROW_POINTS.get(n, ROW_POINTS[5] + 300 * (n - 5)) * self.level
        else:
            self._spawn(self._take_next(), now)

    def _ship(self, now: float):
        keep = [row for y, row in enumerate(self.grid) if y not in self.lit]
        self.grid = [[None] * COLS for _ in range(ROWS - len(keep))] + keep
        self.lit = set()
        self._spawn(self._take_next(), now)

    def ghost_y(self) -> int | None:
        """Where the falling crate would land (drawn as an outline)."""
        if self.piece is None:
            return None
        y = self.piece.y
        while self._fits(self.piece.at(y=y + 1)):
            y += 1
        return y
