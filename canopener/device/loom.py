"""Loom: the handheld's wiring puzzle (Games, next to Harness and Cargo).

A wiring loom has been pulled apart: every tile holds its piece of wire, turned the wrong way.
Turn the tiles until the battery feeds every wire again and every lamp is lit. Nothing falls and
nothing is timed, so it's the calm one: move the cursor with the d-pad, turn the tile under it
with the encoder (or tap a tile). Fewer turns than par earns three stars; solving a level opens
the next, bigger one.

A board is made from a random tree of wire grown out from the battery, so it can always be solved,
then every tile is given a random turn. It's solved when every tile is fed from the battery: a
tree has no spare wire ends, so all tiles fed means no loose ends either.

Pure logic (no Pygame). The commands are the ones the other games take (``game_controls``), so
the machine's CAN signals can play it too. The screen is ``device/screens/games_loom.py``.
"""

import random

from .game_controls import COLUMN, DROP, LEFT, RIGHT, SWAP, TURN
from .harness import OPPOSITE, STEP, E, N, S, W, rotate

# Board size (columns, rows) by level; the last one repeats for ever with new boards.
SIZES = ((4, 3), (5, 3), (5, 4), (6, 4), (7, 4), (7, 5), (8, 5), (9, 5), (9, 6), (10, 6))
BRANCHING = 0.45            # how often the wire grows from a random tile instead of the newest one

__all__ = ["Loom", "SIZES", "size_for", "stars", "N", "E", "S", "W"]


def size_for(level: int) -> tuple[int, int]:
    return SIZES[max(1, min(level, len(SIZES))) - 1]


def stars(turns: int, par: int) -> int:
    """3: par or better; 2: within half as many again; 1: solved."""
    if turns <= par:
        return 3
    return 2 if turns <= par * 1.5 + 1 else 1


def turns_between(mask: int, target: int) -> int | None:
    """Fewest quarter turns (either way) that make ``mask`` look like ``target``."""
    costs = [min(k, 4 - k) for k in range(4) if rotate(mask, k) == target]
    return min(costs) if costs else None


class Loom:
    def __init__(self, level: int = 1, seed: int | None = None):
        self.rng = random.Random(seed)
        self.level = max(1, int(level))
        self.cols, self.rows = size_for(self.level)
        self.solution: list[list[int]] = []
        self.grid: list[list[int]] = []         # grid[y][x]: connection bits, as the tile is turned now
        self.source = (0, 0)
        self.cursor = (0, 0)
        self.turns = 0
        self.par = 0
        self.solved = False
        self.started_at: float | None = None    # the clock starts with the first turn
        self.solved_at: float | None = None
        self.events: list[tuple] = []           # for the screen: ("turn", x, y, steps), ("solved", turns, par)
        self._generate()

    # --- making a board ---
    def _generate(self):
        cols, rows, rng = self.cols, self.rows, self.rng
        sol = [[0] * cols for _ in range(rows)]
        self.source = (rng.randrange(1, cols - 1), rng.randrange(rows))
        grown = [self.source]
        seen = {self.source}
        while len(seen) < cols * rows:
            x, y = grown[-1] if rng.random() > BRANCHING else rng.choice(grown)
            ways = [(bit, x + dx, y + dy) for bit, (dx, dy) in STEP.items()
                    if 0 <= x + dx < cols and 0 <= y + dy < rows and (x + dx, y + dy) not in seen
                    and bin(sol[y][x]).count("1") < 3]          # no four-way tiles: turning one does nothing
            if not ways:
                grown.remove((x, y))
                continue
            bit, nx, ny = rng.choice(ways)
            sol[y][x] |= bit
            sol[ny][nx] |= OPPOSITE[bit]
            seen.add((nx, ny))
            grown.append((nx, ny))
        self.solution = sol
        while True:
            self.grid = [[rotate(m, rng.randrange(4)) for m in row] for row in sol]
            self.par = sum(turns_between(self.grid[y][x], sol[y][x]) for y in range(rows) for x in range(cols))
            if self.par >= cols * rows // 2 and len(self.live()) < cols * rows:
                break
        self.cursor = self.source

    # --- what's fed ---
    def joined(self, x: int, y: int):
        """The tiles this one's wire really joins: both ends have to meet."""
        mask = self.grid[y][x]
        for bit, (dx, dy) in STEP.items():
            nx, ny = x + dx, y + dy
            if mask & bit and 0 <= nx < self.cols and 0 <= ny < self.rows and self.grid[ny][nx] & OPPOSITE[bit]:
                yield nx, ny

    def live(self) -> set[tuple[int, int]]:
        """Every tile fed from the battery."""
        seen, todo = {self.source}, [self.source]
        while todo:
            for n in self.joined(*todo.pop()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen

    def lamps(self) -> list[tuple[int, int]]:
        """Wire ends (a tile with one connection) other than the battery: each one is a lamp."""
        return [(x, y) for y in range(self.rows) for x in range(self.cols)
                if bin(self.grid[y][x]).count("1") == 1 and (x, y) != self.source]

    # --- controls ---
    def command(self, cmd: str, n: int = 1, now: float = 0.0):
        """The shared game commands: LEFT / RIGHT move the cursor along the row, SWAP / DROP up and
        down, COLUMN puts it in a column, TURN turns the tile under it."""
        if cmd == LEFT:
            self.move(-n, 0)
        elif cmd == RIGHT:
            self.move(n, 0)
        elif cmd == DROP:
            self.move(0, n)
        elif cmd == SWAP:
            self.move(0, -n)
        elif cmd == COLUMN:
            self.cursor = (max(0, min(self.cols - 1, n)), self.cursor[1])
        elif cmd == TURN:
            self.turn(n, now)

    def move(self, dx: int, dy: int):
        """Move the cursor; it wraps round the edges."""
        x, y = self.cursor
        self.cursor = ((x + dx) % self.cols, (y + dy) % self.rows)

    def turn(self, steps: int = 1, now: float = 0.0, at: tuple[int, int] | None = None) -> bool:
        """Turn a tile (the one under the cursor, or ``at``) ``steps`` quarter turns, + clockwise."""
        if self.solved or not steps % 4:
            return False
        if at is not None:
            self.cursor = at
        x, y = self.cursor
        self.grid[y][x] = rotate(self.grid[y][x], steps)
        self.turns += min(steps % 4, 4 - steps % 4)
        if self.started_at is None:
            self.started_at = now
        self.events.append(("turn", x, y, steps))
        if len(self.live()) == self.cols * self.rows:
            self.solved, self.solved_at = True, now
            self.events.append(("solved", self.turns, self.par))
        return True

    def elapsed(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, (self.solved_at if self.solved_at is not None else now) - self.started_at)

    @property
    def stars(self) -> int:
        return stars(self.turns, self.par) if self.solved else 0
