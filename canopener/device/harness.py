"""Harness: the handheld's hidden game (System > About, seven times).

Wire segments fall one at a time into a board between two bus bars. Turn the encoder to rotate a
segment, move it sideways, drop it. When a wire touches the bus bars at two points, that whole
circuit (branches included) lights up and clears: bar to bar scores double, a loop back to the
same bar single. The wires above fall into the gap, which can close another circuit: a chain.
Segments that don't connect pile up; the game ends when a new one has nowhere to appear.

Pure logic (no Pygame), driven by ``update(now)`` with the clock passed in, so tests can play it.
Every control goes through ``Harness.command``: the handheld's buttons and encoder, and the
machine's own signals via ``game_controls`` (steer to turn, the lower switch drops), so the game
can be played from the bus. The screen is ``device/screens/games.py``.
"""

import random
from dataclasses import dataclass

from .game_controls import COLUMN, DROP, LEFT, PAUSE, RIGHT, SWAP, TURN

COLS, ROWS = 8, 12

# Connection bits: which edges of its cell a segment's wire reaches.
N, E, S, W = 1, 2, 4, 8
STEP = {N: (0, -1), E: (1, 0), S: (0, 1), W: (-1, 0)}
OPPOSITE = {N: S, S: N, E: W, W: E}

# Segment kinds (drawn pointing this way before rotating) and how often each comes up. Tuned with
# a greedy test player: with fewer tees and crosses the board silts up after ~100 segments.
STRAIGHT, BEND, TEE, CROSS = N | S, N | E, N | E | S, N | E | S | W
KINDS = ((STRAIGHT, 2), (BEND, 3), (TEE, 3), (CROSS, 2))

CLEAR_S = 0.35              # how long a finished circuit lights up before it goes
CIRCUITS_PER_LEVEL = 5


def rotate_cw(mask: int) -> int:
    return ((mask << 1) | (mask >> 3)) & 0xF


def rotate(mask: int, turns: int) -> int:
    for _ in range(turns % 4):
        mask = rotate_cw(mask)
    return mask


def fall_interval(level: int) -> float:
    """Seconds per row: 0.8 s at level 1, quicker each level, never below 0.12 s."""
    return max(0.12, 0.8 * 0.85 ** (level - 1))


@dataclass
class Piece:
    mask: int
    x: int = 0
    y: int = 0


class Harness:
    def __init__(self, seed: int | None = None, now: float = 0.0):
        self.rng = random.Random(seed)
        self.grid: list[list[int]] = [[0] * COLS for _ in range(ROWS)]     # grid[y][x], 0 = empty
        self.score = 0
        self.circuits = 0
        self.chain = 0              # circuits closed by the current drop (1, then 2 for a chain, ...)
        self.best_chain = 0
        self.over = False
        self.paused = False
        self.hold: int | None = None
        self.held = False           # one swap per segment
        self.lit: set[tuple[int, int]] = set()     # cells lighting up before they clear
        self._clear_at = 0.0
        self.next = self._random()
        self.piece: Piece | None = None
        self._fall_at = now + fall_interval(1)
        self._spawn(self._take_next(), now)

    @property
    def level(self) -> int:
        return 1 + self.circuits // CIRCUITS_PER_LEVEL

    # --- pieces ---
    def _random(self) -> int:
        kinds, weights = zip(*KINDS)
        return rotate(self.rng.choices(kinds, weights)[0], self.rng.randrange(4))

    def _take_next(self) -> int:
        mask, self.next = self.next, self._random()
        return mask

    def _spawn(self, mask: int, now: float):
        p = Piece(mask, COLS // 2)
        if self.grid[p.y][p.x]:
            self.piece, self.over = None, True
            return
        self.piece, self.held = p, False
        self._fall_at = now + fall_interval(self.level)

    def _free(self, x: int, y: int) -> bool:
        return 0 <= x < COLS and 0 <= y < ROWS and not self.grid[y][x]

    @property
    def busy(self) -> bool:
        """No input while paused, over, or a circuit is lighting up."""
        return self.over or self.paused or self.piece is None

    # --- controls ---
    def command(self, cmd: str, n: int = 1, now: float = 0.0):
        """One control input, from whichever source (buttons, encoder, CAN). TURN: steps,
        + clockwise; LEFT / RIGHT: columns; COLUMN: the column to head for."""
        if cmd == COLUMN:
            if not self.busy:
                self.move(n - self.piece.x)
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
            moved = self._step(step) or moved
        return moved

    def _step(self, dx: int) -> bool:
        if self.busy or not dx or not self._free(self.piece.x + dx, self.piece.y):
            return False
        self.piece.x += dx
        return True

    def turn(self, steps: int):
        """Encoder: +1 per click clockwise, -1 anticlockwise."""
        if not self.busy:
            self.piece.mask = rotate(self.piece.mask, steps)

    def drop(self, now: float):
        if self.busy:
            return
        while self._free(self.piece.x, self.piece.y + 1):
            self.piece.y += 1
        self._lock(now)

    def swap(self, now: float):
        """Swap the falling segment for the one on the side (once per segment)."""
        if self.busy or self.held:
            return
        cur = self.piece.mask
        mask = self.hold if self.hold is not None else self._take_next()
        self.hold = cur
        self._spawn(mask, now)
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
                self._finish_clear(now)
            return
        if self.piece is not None and now >= self._fall_at:
            if self._free(self.piece.x, self.piece.y + 1):
                self.piece.y += 1
                self._fall_at = now + fall_interval(self.level)
            else:
                self._lock(now)

    def _lock(self, now: float):
        p, self.piece = self.piece, None
        self.grid[p.y][p.x] = p.mask
        self.chain = 0
        self._check(now)

    def _check(self, now: float):
        found = self.find_circuits()
        if found:
            self.chain += 1
            self.best_chain = max(self.best_chain, self.chain)
            self.lit = set().union(*found)
            self._clear_at = now + CLEAR_S
            self.circuits += len(found)
            self.score += sum(10 * len(g) * (2 if self.spans(g) else 1) for g in found) * self.chain * len(found)
        else:
            self._spawn(self._take_next(), now)

    def _finish_clear(self, now: float):
        for x, y in self.lit:
            self.grid[y][x] = 0
        self.lit = set()
        self._settle()
        self._check(now)            # the wires that fell may close another circuit

    def _settle(self):
        """Every column's segments fall to the bottom, keeping their order."""
        for x in range(COLS):
            stack = [self.grid[y][x] for y in range(ROWS) if self.grid[y][x]]
            for y in range(ROWS):
                self.grid[y][x] = 0
            for i, mask in enumerate(reversed(stack)):
                self.grid[ROWS - 1 - i][x] = mask

    # --- circuits ---
    def neighbours(self, x: int, y: int):
        """Cells this segment's wire really joins: both ends have to meet."""
        mask = self.grid[y][x]
        for bit, (dx, dy) in STEP.items():
            nx, ny = x + dx, y + dy
            if mask & bit and 0 <= nx < COLS and 0 <= ny < ROWS and self.grid[ny][nx] & OPPOSITE[bit]:
                yield nx, ny

    def find_circuits(self) -> list[set[tuple[int, int]]]:
        """Connected groups of wire touching the bus bars at two or more points (W ends in the
        left column, E ends in the right one)."""
        seen: set[tuple[int, int]] = set()
        out = []
        starts = [(0, y) for y in range(ROWS) if self.grid[y][0] & W]
        starts += [(COLS - 1, y) for y in range(ROWS) if self.grid[y][COLS - 1] & E]
        for start in starts:
            if start in seen:
                continue
            group, todo = {start}, [start]
            while todo:
                cell = todo.pop()
                for n in self.neighbours(*cell):
                    if n not in group:
                        group.add(n)
                        todo.append(n)
            seen |= group
            if self.contacts(group) >= 2:
                out.append(group)
        return out

    def contacts(self, group) -> int:
        """How many wire ends in ``group`` touch a bus bar."""
        return sum((x == 0 and bool(self.grid[y][x] & W)) + (x == COLS - 1 and bool(self.grid[y][x] & E))
                   for x, y in group)

    def spans(self, group) -> bool:
        """Bar to bar (worth double), not just back to the bar it started from."""
        return any(x == 0 and self.grid[y][x] & W for x, y in group) and \
            any(x == COLS - 1 and self.grid[y][x] & E for x, y in group)

    def ghost_y(self) -> int | None:
        """Where the falling segment would land (drawn as an outline)."""
        if self.piece is None:
            return None
        y = self.piece.y
        while self._free(self.piece.x, y + 1):
            y += 1
        return y
