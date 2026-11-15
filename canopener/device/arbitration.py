"""Arbitration: the handheld's CAN quiz game (Games).

Several nodes start sending at the same instant. Only one frame gets the bus: the others see a
dominant bit where they sent a recessive one and back off. Pick the winner before the time runs
out. It's how CAN really decides (ISO 11898-1), so the game is also practice at reading IDs:

* Identifiers are compared a bit at a time from the top; a 0 (dominant) beats a 1 (recessive), so
  the **lowest identifier wins**.
* With the same identifier, a **data frame beats a remote request** (the RTR bit is dominant in a
  data frame).
* An 11-bit frame and a 29-bit one are compared on their first 11 bits; if those are the same,
  the **11-bit frame wins** (the 29-bit frame's SRR and IDE bits are recessive).
* A J1939 identifier starts with its priority, so the lowest priority number wins, whatever
  the PGN.

The levels bring these in one at a time, with more frames and less time. A wrong answer (or
none in time) adds to the transmit error counter, a right one takes a little off again; at 256
the node goes bus-off and the game is over, as on a real bus.

Pure logic (no Pygame), driven by ``update(now)`` with the clock passed in. It takes the shared
game commands (``game_controls``), so CAN signals can play it. The identifiers are random
numbers, not any machine's. The screen is ``device/screens/games_arbitration.py``.
"""

import random
from dataclasses import dataclass

from .game_controls import COLUMN, DROP, LEFT, PAUSE, RIGHT, SWAP, TURN

READY, ASK, REVEAL, OVER = "ready", "ask", "reveal", "over"
WINS_PER_LEVEL = 5
ERROR_STEP, RECOVER_STEP = 64, 8        # the error counter: up for a mistake, down for a right answer
WARNING, PASSIVE, BUS_OFF = 96, 128, 256
RIGHT_S = 1.3                           # how long a right answer's reveal stays up by itself
HEX, J1939 = "hex", "j1939"            # how a frame's identifier is written on its card

# What each level adds (the last entry goes on for ever).
STD, CLOSE, EXT, REMOTE, MIXED, FIELDS = "std", "close", "ext", "remote", "mixed", "fields"
LEVELS = (
    ((STD,), 2), ((STD,), 3), ((STD, CLOSE), 3), ((CLOSE, EXT), 3), ((EXT, REMOTE), 3),
    ((REMOTE, MIXED, CLOSE), 3), ((MIXED, FIELDS, EXT), 3), ((MIXED, FIELDS, REMOTE, CLOSE), 4),
)
LESSONS = {
    STD: "The lowest identifier wins.",
    CLOSE: "Identifiers that start the same are decided by a later bit.",
    EXT: "29-bit identifiers work the same way: the lowest wins.",
    REMOTE: "Same identifier: the data frame beats the remote request.",
    MIXED: "11-bit against 29-bit: compare the first 11 bits. A tie goes to the 11-bit frame.",
    FIELDS: "J1939: the priority comes first, so the lowest priority number wins.",
}


def time_limit(level: int) -> float:
    """Seconds to answer: 10 at level 1, less each level, never under 4."""
    return max(4.0, 10.0 - 0.75 * (level - 1))


def bus_state(tec: int) -> str:
    if tec >= BUS_OFF:
        return "Bus off"
    if tec >= PASSIVE:
        return "Error passive"
    return "Error warning" if tec >= WARNING else "Error active"


@dataclass(frozen=True)
class Frame:
    ident: int
    extended: bool = False
    remote: bool = False
    style: str = HEX

    def bits(self) -> tuple[int, ...]:
        """The arbitration field as it goes on the wire (0 = dominant). 11-bit: the identifier,
        RTR, IDE. 29-bit: the top 11 bits, SRR, IDE, the other 18 bits, RTR."""
        if not self.extended:
            return (*_bits(self.ident, 11), int(self.remote), 0)
        return (*_bits(self.ident >> 18, 11), 1, 1, *_bits(self.ident & 0x3FFFF, 18), int(self.remote))

    def bit_names(self) -> tuple[str, ...]:
        if not self.extended:
            return (*(f"ID{i}" for i in range(10, -1, -1)), "RTR", "IDE")
        return (*(f"ID{i}" for i in range(28, 17, -1)), "SRR", "IDE", *(f"ID{i}" for i in range(17, -1, -1)), "RTR")

    @property
    def label(self) -> str:
        if self.extended and self.style == J1939:
            return f"P{self.ident >> 26}  PGN {(self.ident >> 8) & 0x3FFFF:05X}  SA {self.ident & 0xFF:02X}"
        return f"0x{self.ident:08X}" if self.extended else f"0x{self.ident:03X}"

    @property
    def tag(self) -> str:
        return ("29-bit" if self.extended else "11-bit") + (" remote request" if self.remote else " data")

    @property
    def top11(self) -> int:
        return self.ident >> 18 if self.extended else self.ident


def _bits(value: int, n: int) -> tuple[int, ...]:
    return tuple((value >> i) & 1 for i in range(n - 1, -1, -1))


def winner(frames) -> int:
    """Which frame gets the bus: the one whose bits stay dominant longest."""
    return min(range(len(frames)), key=lambda i: frames[i].bits())


def lost_at(frame: Frame, won: Frame) -> int | None:
    """The bit (index into ``bits()``) where ``frame`` sent recessive against ``won``'s dominant
    and backed off; None for the winner itself."""
    return next((i for i, (a, b) in enumerate(zip(frame.bits(), won.bits())) if a != b), None)


def reason(frames, win: int) -> str:
    """Why the winner won, from the bit that settled it against the frame that held on longest."""
    won = frames[win]
    late = max((f for i, f in enumerate(frames) if i != win), key=lambda f: lost_at(f, won))
    i = lost_at(late, won)
    name = late.bit_names()[i]
    if won.extended != late.extended and i >= 11:
        return "Same first 11 bits: the 11-bit frame beats the 29-bit one (its SRR and IDE bits are recessive)."
    if name == "RTR":
        return "Same identifier: the data frame beats the remote request (RTR is dominant in a data frame)."
    if won.extended and late.extended and won.style == J1939 and i < 3:
        return f"Lowest priority number wins: decided at bit {i + 1}, inside the priority."
    return f"Lowest identifier wins: decided at bit {i + 1} ({won.bit_names()[i]}), a dominant 0 against a 1."


# --- making a round ---
def make_round(rng: random.Random, level: int) -> tuple[list[Frame], str]:
    """The frames for one round at ``level`` and what kind of round it is (a key of LESSONS)."""
    kinds, count = LEVELS[max(1, min(level, len(LEVELS))) - 1]
    kind = kinds[0] if rng.random() < 0.5 else rng.choice(kinds)       # the level's new thing comes up most
    for _ in range(200):
        frames = _MAKERS[kind](rng, count)
        if len({f.bits() for f in frames}) == len(frames):
            rng.shuffle(frames)
            return frames, kind
    raise RuntimeError(f"no round for {kind}")          # can't happen: the makers rarely collide


def _std(rng, count):
    return [Frame(i) for i in rng.sample(range(1, 0x800), count)]


def _close(rng, count):
    base = rng.randrange(0x80) << 4                     # same top 7 bits: only the last digit differs
    return [Frame(base | low) for low in rng.sample(range(16), count)]


def _j1939_id(rng, priority=None, pgn=None) -> int:
    priority = rng.randrange(8) if priority is None else priority
    pgn = rng.randrange(0x100, 0x10000) if pgn is None else pgn
    return (priority << 26) | (pgn << 8) | rng.randrange(0xFE)


def _ext(rng, count):
    return [Frame(_j1939_id(rng), extended=True) for _ in range(count)]


def _fields(rng, count):
    """J1939 fields on the card. Often two frames share the lowest priority, so the PGN decides."""
    frames = [Frame(_j1939_id(rng), extended=True, style=J1939) for _ in range(count)]
    if rng.random() < 0.5:
        low = min(f.ident >> 26 for f in frames)
        frames[rng.randrange(count)] = Frame(_j1939_id(rng, priority=low), extended=True, style=J1939)
    return frames


def _remote(rng, count):
    """A data frame and a remote request for the same identifier, and maybe a lower one that beats both."""
    extended = rng.random() < 0.3
    ident = _j1939_id(rng) if extended else rng.randrange(0x10, 0x7F0)
    frames = [Frame(ident, extended), Frame(ident, extended, remote=True)]
    while len(frames) < count:
        other = _j1939_id(rng) if extended else rng.randrange(1, 0x800)
        frames.append(Frame(other, extended, remote=rng.random() < 0.3))
    return frames


def _mixed(rng, count):
    """An 11-bit frame against 29-bit ones that start with the same 11 bits, or nearly."""
    ident = rng.randrange(0x10, 0x7F0)
    frames = [Frame(ident)]
    while len(frames) < count:
        top = ident + rng.choice((0, 0, -1, 1, rng.randrange(-8, 9)))
        frames.append(Frame((top << 18) | rng.randrange(1 << 18), extended=True))
    return frames


_MAKERS = {STD: _std, CLOSE: _close, EXT: _ext, REMOTE: _remote, MIXED: _mixed, FIELDS: _fields}


class Arbitration:
    def __init__(self, seed: int | None = None, now: float = 0.0):
        self.rng = random.Random(seed)
        self.phase = READY
        self.frames: list[Frame] = []
        self.kind = STD
        self.win = 0
        self.picked: int | None = None      # the answer given (None in REVEAL: time ran out)
        self.focus = 0
        self.score = 0
        self.points = 0                     # what the last right answer was worth
        self.wins = 0
        self.rounds = 0
        self.streak = 0
        self.best_streak = 0
        self.tec = 0                        # transmit error counter
        self.paused = False
        self.asked_at = now
        self.deadline = now
        self.reveal_until: float | None = None
        self.new_lesson: str | None = None  # this round is the first of its kind: say what's new
        self._seen: set[str] = set()
        self._paused_at = 0.0
        self.events: list[tuple] = []       # for the screen: ("right", points), ("wrong",), ("level", n), ("over",)

    @property
    def level(self) -> int:
        return 1 + self.wins // WINS_PER_LEVEL

    @property
    def over(self) -> bool:
        return self.phase == OVER

    @property
    def correct(self) -> bool:
        return self.phase in (REVEAL, OVER) and self.picked == self.win

    @property
    def state(self) -> str:
        return bus_state(self.tec)

    def time_left(self, now: float) -> float:
        """0-1: how much of the round's time is left."""
        if self.phase != ASK:
            return 0.0
        if self.paused:
            now = self._paused_at
        return max(0.0, min(1.0, (self.deadline - now) / (self.deadline - self.asked_at)))

    # --- rounds ---
    def start(self, now: float):
        if self.phase == READY:
            self._ask(now)

    def _ask(self, now: float):
        self.frames, self.kind = make_round(self.rng, self.level)
        self.win = winner(self.frames)
        self.picked, self.focus = None, min(self.focus, len(self.frames) - 1)
        self.new_lesson = None if self.kind in self._seen else LESSONS[self.kind]
        self._seen.add(self.kind)
        self.rounds += 1
        self.phase, self.asked_at, self.deadline = ASK, now, now + time_limit(self.level)

    def pick(self, i: int, now: float):
        """Answer: frame ``i`` wins."""
        if self.phase != ASK or self.paused or not 0 <= i < len(self.frames):
            return
        self._answer(i, now)

    def _answer(self, i: int | None, now: float):
        left = self.time_left(now)
        self.picked, self.phase = i, REVEAL
        if i is not None:
            self.focus = i
        if i == self.win:
            level = self.level
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            self.points = (round(50 + 50 * left) + 10 * min(self.streak - 1, 10)) * level
            self.score += self.points
            self.wins += 1
            self.tec = max(0, self.tec - RECOVER_STEP)
            self.reveal_until = now + RIGHT_S
            self.events.append(("right", self.points))
            if self.level != level:
                self.events.append(("level", self.level))
        else:                               # wrong, or too late: stays up until the player has read why
            self.streak, self.points = 0, 0
            self.tec += ERROR_STEP
            self.reveal_until = None
            self.events.append(("wrong",))

    def next(self, now: float):
        """On from the reveal: the next round, or bus-off."""
        if self.phase != REVEAL or self.paused:
            return
        if self.tec >= BUS_OFF:
            self.phase = OVER
            self.events.append(("over",))
        else:
            self._ask(now)

    def act(self, now: float):
        """The one 'do it' control (encoder push, a tap): start, answer with the focused frame, go on."""
        if self.phase == READY:
            self.start(now)
        elif self.phase == ASK:
            self.pick(self.focus, now)
        elif self.phase == REVEAL:
            self.next(now)

    def move(self, n: int):
        if self.phase == ASK and not self.paused and self.frames:
            self.focus = (self.focus + n) % len(self.frames)

    def command(self, cmd: str, n: int = 1, now: float = 0.0):
        """The shared game commands: LEFT / RIGHT / TURN move the marker, COLUMN puts it on a
        frame, DROP or SWAP answers (or starts, or goes on)."""
        if cmd in (LEFT, RIGHT):
            self.move(-n if cmd == LEFT else n)
        elif cmd == TURN:
            self.move(n)
        elif cmd == COLUMN:
            if self.phase == ASK and not self.paused:
                self.focus = max(0, min(len(self.frames) - 1, n))
        elif cmd in (DROP, SWAP):
            self.act(now)
        elif cmd == PAUSE:
            self.toggle_pause(now)

    def toggle_pause(self, now: float):
        if self.phase in (READY, OVER):
            return
        self.paused = not self.paused
        if self.paused:
            self._paused_at = now
        else:
            away = max(0.0, now - self._paused_at)
            self.asked_at += away
            self.deadline += away
            if self.reveal_until is not None:
                self.reveal_until += away

    def update(self, now: float):
        if self.paused:
            return
        if self.phase == ASK and now >= self.deadline:
            self._answer(None, now)
        elif self.phase == REVEAL and self.reveal_until is not None and now >= self.reveal_until:
            self.next(now)
