"""Game controls from CAN: a machine's own sensors and switches, decoded by the active profile,
drive the handheld's games (steer to turn the piece, the lower switch drops it, ...).

A binding names a signal (as Live Data lists it) and what it does:

* **edge** (``above`` / ``below``): fires once each time the value crosses the threshold: a
  switch. Default for drop / swap / pause.
* **delta** (``every``): one step per ``every`` units of change, signed (a negative ``every``
  reverses it): turn the piece as the steering angle changes, or move it.
* **position** (``min`` / ``max``): the value's range spread across the board's columns.

Bindings are kept per profile (``games.controls`` in settings.json: profile id -> list), e.g.
``{"signal": "Steering angle", "do": "turn", "every": 15}``. Reading only: nothing is sent.
Standard library only, no Pygame.
"""

from dataclasses import dataclass, field

from ..j1939.signals import Signal, all_signals

# Commands any control (buttons, encoder, CAN) gives a game. TURN and the moves come with a step
# count; COLUMN with the column to go to.
LEFT, RIGHT, DROP, TURN, SWAP, PAUSE, COLUMN = "left", "right", "drop", "turn", "swap", "pause", "column"
MOVE = "move"               # a binding's action: delta steps become LEFT / RIGHT
ACTIONS = (TURN, MOVE, COLUMN, DROP, SWAP, PAUSE)
ACTION_LABELS = {TURN: "Turn the piece", MOVE: "Move the piece", COLUMN: "Piece follows value",
                 DROP: "Drop", SWAP: "Swap with the side piece", PAUSE: "Pause"}
SETTING = "games.controls"


@dataclass
class Binding:
    signal: str                 # Signal.name (or Signal.key)
    do: str
    above: float | None = None
    below: float | None = None
    every: float | None = None
    min: float | None = None
    max: float | None = None
    source: int | None = None   # only this source address / node (None: any)
    _last: float | None = field(default=None, repr=False)
    _acc: float = field(default=0.0, repr=False)
    _col: int | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Binding":
        keys = ("signal", "do", "above", "below", "every", "min", "max", "source")
        return cls(**{k: d[k] for k in keys if k in d})

    def to_dict(self) -> dict:
        return {k: v for k, v in (("signal", self.signal), ("do", self.do), ("above", self.above),
                                  ("below", self.below), ("every", self.every), ("min", self.min),
                                  ("max", self.max), ("source", self.source)) if v is not None}

    @property
    def mode(self) -> str:
        if self.min is not None and self.max is not None and self.max != self.min:
            return "position"
        if self.every:
            return "delta"
        return "edge"

    def step(self, v: float, columns: int) -> list[tuple[str, int]]:
        """The commands a new value gives. The first value only sets the starting point, so a
        switch that's already on doesn't fire when the game opens."""
        last, self._last = self._last, v
        mode = self.mode
        if mode == "position":
            frac = (v - self.min) / (self.max - self.min)
            col = max(0, min(columns - 1, round(frac * (columns - 1))))
            changed, self._col = col != self._col, col
            return [(COLUMN, col)] if changed else []
        if last is None:
            return []
        if mode == "delta":
            self._acc += (v - last) / self.every
            n = int(self._acc)
            if not n:
                return []
            self._acc -= n
            if self.do == MOVE:
                return [(RIGHT if n > 0 else LEFT, abs(n))]
            return [(self.do, n)]
        if self.below is not None:
            fired = last > self.below >= v
        else:
            th = 0.5 if self.above is None else self.above
            fired = last < th <= v
        if not fired:
            return []
        return [(RIGHT, 1)] if self.do == MOVE else [(self.do, 1)]


def suggest(do: str, sig: Signal, lo: float | None, hi: float | None) -> dict:
    """Sensible settings for binding ``sig`` to ``do``, from the range seen so far (Live Data's
    min / max): a switch (enumerated, or 1-2 bits) flips at 0.5, anything else halfway."""
    seen = lo is not None and hi is not None and hi > lo
    switch = bool(sig.enum) or 0 < sig.bits <= 2
    d = {"signal": sig.name, "do": do}
    if do == COLUMN:
        if seen:
            d.update(min=lo, max=hi)
    elif do in (TURN, MOVE):
        if switch:
            d["above"] = 0.5                    # one step per press
        else:
            d["every"] = round((hi - lo) / 6, 3) if seen else 1     # about six steps end to end
    else:
        d["above"] = 0.5 if switch or not seen else round((lo + hi) / 2, 3)
    return d


class SignalControls:
    """Reads the bound signals straight from the session each frame (Live Data samples only five
    times a second, too slow for a quick switch) and turns their changes into commands."""

    def __init__(self, bindings: list[Binding], columns: int):
        self.bindings = bindings
        self.columns = columns
        self._sigs: dict[str, Signal | None] = {}
        self._sig_list: list | None = None

    @classmethod
    def for_profile(cls, settings, profile_id: str, columns: int) -> "SignalControls":
        raw = (settings.get(SETTING) or {}).get(profile_id) or []
        return cls([Binding.from_dict(d) for d in raw if isinstance(d, dict) and d.get("signal")], columns)

    def signal(self, name: str) -> Signal | None:
        sigs = all_signals()
        if sigs is not self._sig_list:          # the profile (and its signals) changed
            self._sig_list, self._sigs = sigs, {}
        if name not in self._sigs:
            self._sigs[name] = next((s for s in sigs if s.name == name), None) or next(
                (s for s in sigs if s.key == name or s.name.lower() == name.lower()), None)
        return self._sigs[name]

    def value(self, session, b: Binding) -> float | None:
        sig = self.signal(b.signal)
        if sig is None:
            return None
        with session.lock:
            if sig.can_id is not None:
                got = session.latest_std.get(sig.can_id)
                newest = got[1] if got else None
            else:
                newest, t = None, -1.0
                for (pgn, sa), (ft, d) in session.latest.items():
                    if pgn == sig.pgn and sig.accepts(sa) and (b.source is None or sa == b.source) and ft > t:
                        newest, t = d, ft
        if newest is None:
            return None
        return sig.decode(newest)

    def poll(self, session) -> list[tuple[str, int]]:
        if session is None:
            return []
        out = []
        for b in self.bindings:
            v = self.value(session, b)
            if v is not None:
                out += b.step(float(v), self.columns)
        return out


def save_binding(settings, profile_id: str, binding: dict | None, do: str):
    """Set (or with None, clear) the profile's binding for one action."""
    allp = dict(settings.get(SETTING) or {})
    mine = [d for d in allp.get(profile_id) or [] if d.get("do") != do]
    if binding:
        mine.append(binding)
    allp[profile_id] = mine
    settings.set(SETTING, allp)


def binding_for(settings, profile_id: str, do: str) -> dict | None:
    return next((d for d in (settings.get(SETTING) or {}).get(profile_id) or [] if d.get("do") == do), None)
