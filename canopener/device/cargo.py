"""Cargo: the handheld's falling-block game (Games, next to Harness).

Crates of different shapes drop into a trailer one at a time. Steer each one, turn it, drop it;
a row packed wall to wall ships out and everything above settles down a row. Several rows at once
are worth more. The load reaches the top and the game is over.

The crates are Cargo's own: singles, pairs, a pallet, three-crate bars and corners, and five-crate
loads (P, U, V, T and L shapes), so the pieces come in very different sizes. There's a side bay to
swap the falling crate into (once per crate), like Harness.

Crates are dealt from a shuffled bag (every shape as often as its weight, so none stays away for
long). A crate that has landed can still be slid or turned for a moment before it sets. Shipping
rows with crate after crate builds a streak (a bonus each time), and emptying the trailer
completely is worth a clean-sweep bonus. What happened is queued in ``events`` for the screen.

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
KICKS = ((0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1), (-1, -1), (1, -1), (0, -2), (0, -3))
#                             nudges (sideways, then up off the load) tried when a turn would hit something
STREAK_POINTS = 50          # x streak x level, from the second crate in a row that ships something
SWEEP_POINTS = 1000         # x level: the trailer left completely empty
LOCK_S = 0.45               # a landed crate that was just moved or turned gets this long before it sets
MAX_SLIDES = 12             # ... at most this many times


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
        self.streak = 0                     # crates in a row that each shipped something
        self.best_streak = 0
        self.sweeps = 0
        self.placed = 0
        self.events: list[tuple] = []       # for the screen: ("lock", cells, rows dropped),
        #                 ("clear", rows, points, streak), ("sweep", points), ("level", n), ("over",)
        self._bag: list[str] = []
        self._moved_at = -1e9
        self._slides = 0
        self._paused_at = 0.0
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
        if not self._bag:
            self._bag = [k for k in KINDS for _ in range(WEIGHTS[k])]
            self.rng.shuffle(self._bag)
        return self._bag.pop()

    def _take_next(self) -> str:
        kind, self.next = self.next, self._random()
        return kind

    def _spawn(self, kind: str, now: float):
        cells = SHAPES[kind]
        p = Crate(kind, cells)
        p.x = (COLS - p.width) // 2
        if not self._fits(p.at()):
            self.piece, self.over = None, True
            self.events.append(("over",))
            return
        self.piece, self.held = p, False
        self._fall_at = now + fall_interval(self.level)
        self._slides = 0

    def _fits(self, cells) -> bool:
        return all(0 <= x < COLS and 0 <= y < ROWS and self.grid[y][x] is None for x, y in cells)

    # --- controls ---
    def command(self, cmd: str, n: int = 1, now: float = 0.0):
        """One control input, from whichever source. TURN: steps, + clockwise; LEFT / RIGHT:
        columns; COLUMN: the column to put the crate's middle over."""
        if cmd == COLUMN:
            if not self.busy and self.move(n - (self.piece.x + (self.piece.width - 1) // 2)):
                self._moved_at = now
        elif cmd in (LEFT, RIGHT):
            if self.move(-n if cmd == LEFT else n):
                self._moved_at = now
        elif cmd == DROP:
            self.drop(now)
        elif cmd == TURN:
            if self.turn(n):
                self._moved_at = now
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
        for dx, dy in KICKS:
            if self._fits(p.at(x=p.x + dx, y=p.y + dy, cells=cells)):
                p.cells, p.x, p.y = cells, p.x + dx, p.y + dy
                return True
        return False

    def drop(self, now: float):
        if self.busy:
            return
        y0, self.piece.y = self.piece.y, self.ghost_y()
        self._lock(now, self.piece.y - y0)

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
        if self.paused:
            self._paused_at = now
        else:                               # the clock stood still: what was pending stays as far off
            away = max(0.0, now - self._paused_at)
            self._fall_at += away
            self._clear_at += away
            self._moved_at += away

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
            elif self._slides < MAX_SLIDES and now - self._moved_at < LOCK_S:
                self._slides += 1           # just moved or turned: a moment more before it sets
                self._fall_at = self._moved_at + LOCK_S
            else:
                self._lock(now)

    def fall_frac(self, now: float) -> float:
        """How far the crate is into its next row (0-1), so the screen can draw it gliding."""
        if self.busy or not self._fits(self.piece.at(y=self.piece.y + 1)):
            return 0.0
        return max(0.0, min(1.0, 1 - (self._fall_at - now) / fall_interval(self.level)))

    def _lock(self, now: float, dropped: int = 0):
        p, self.piece = self.piece, None
        for x, y in p.at():
            self.grid[y][x] = p.kind
        self.placed += 1
        self.events.append(("lock", p.at(), dropped))
        full = {y for y in range(ROWS) if all(self.grid[y][x] is not None for x in range(COLS))}
        if full:
            n = len(full)
            self.lit = full
            self._clear_at = now + CLEAR_S
            level = self.level
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            points = (ROW_POINTS.get(n, ROW_POINTS[5] + 300 * (n - 5)) + STREAK_POINTS * (self.streak - 1)) * level
            self.rows_cleared += n
            self.best_at_once = max(self.best_at_once, n)
            self.score += points
            self.events.append(("clear", sorted(full), points, self.streak))
            if self.level != level:
                self.events.append(("level", self.level))
        else:
            self.streak = 0
            self._spawn(self._take_next(), now)

    def _ship(self, now: float):
        keep = [row for y, row in enumerate(self.grid) if y not in self.lit]
        self.grid = [[None] * COLS for _ in range(ROWS - len(keep))] + keep
        self.lit = set()
        if not any(kind for row in self.grid for kind in row):
            points = SWEEP_POINTS * self.level
            self.sweeps += 1
            self.score += points
            self.events.append(("sweep", points))
        self._spawn(self._take_next(), now)

    def ghost_y(self) -> int | None:
        """Where the falling crate would land (drawn as an outline)."""
        if self.piece is None:
            return None
        y = self.piece.y
        while self._fits(self.piece.at(y=y + 1)):
            y += 1
        return y
