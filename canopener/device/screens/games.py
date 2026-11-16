"""Games (hidden: System > About this device, seven times): Harness, Cargo, Loom and Arbitration,
and which of the machine's signals can play them ("Controls from CAN").

Harness and Cargo are played with the handheld turned a quarter turn anticlockwise, so the picture
is drawn portrait and turned to match: the encoder turns the piece, the d-pad's up / down (now
left / right) move it, its left (now down) drops it, and pushing the encoder swaps it for the side
piece. With just the screen: tap a column to move the piece there (tap the piece to turn it), and
the row of buttons under the board: move left, Turn, Swap, Drop, move right.

Loom and Arbitration are played the ordinary way up: ``games_loom.py`` and ``games_arbitration.py``.
"""

import math
import os
import random
import time

import pygame

from ...core import profiles, themes
from .. import cargo as cg
from .. import doom, doom_controls
from .. import game_controls as gc
from .. import harness as hz
from .. import input as inp
from ..ui import UI
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm

BEST = "games.harness_best"
CARGO_BEST = "games.cargo_best"
LOOM_LEVEL = "games.loom_level"
ARBITRATION_BEST = "games.arbitration_best"


def status(ctx) -> str:
    s = ctx.settings
    best = [f"{name} {int(s.get(key) or 0)}" for name, key in (("Harness", BEST), ("Cargo", CARGO_BEST))
            if int(s.get(key) or 0)]
    return "Best: " + ", ".join(best) if best else "Harness, Cargo, Loom, Arbitration"


def _profile_id() -> str:
    p = profiles.active()
    return p.id if p is not None else "generic"


# --- Doom: not ours and not installed with CANopener; fetched when asked (device/doom.py) ---
DOOM_ASKED = "games.doom_asked"


def offer_doom(app, always: bool = False) -> bool:
    """Ask whether to download Doom (once, when the games menu is turned on; ``always``: asked
    for from the Games menu). True if the question was put."""
    s = app.ctx.settings
    if doom.installed() or not doom.can_install() or (s.get(DOOM_ASKED) and not always):
        return False
    s.set(DOOM_ASKED, True)
    confirm(app, "Install Doom?",
            "Doom isn't part of CANopener, but the handheld can fetch it: the Chocolate Doom engine and the "
            "free Freedoom levels, from the system's package archive (about 40 MB; needs internet).\n\n"
            "Say No and it stays off; the Doom row under Games asks again whenever you like.",
            "Install", lambda: install_doom(app), danger=False)
    return True


def install_doom(app):
    def done(result):
        ok, msg = result
        if ok:
            app.ctx.toast(f"Doom installed ({doom.wad_name()}): it's under Games")
        else:
            app.push(InfoDialog(app, "Doom wasn't installed", [msg, "", "To do it by hand, in a terminal:",
                                                               "sudo apt-get install chocolate-doom freedoom"]))
    app.ctx.run_job("Installing Doom", doom.install, done)


def play_doom(app):
    ctx = app.ctx
    argv = doom.command()
    if argv is None:
        if not offer_doom(app, always=True):
            app.push(InfoDialog(app, "Doom isn't installed", [
                "It needs the Chocolate Doom engine (chocolate-doom) and game data: Freedoom, or your own "
                f"doom.wad in {doom.data_dir()}."]))
        return
    if ctx.busy:
        ctx.toast(f"Busy: {ctx.busy}")
        return
    if ctx.recording:
        ctx.toast("A log is recording: stop it first (Doom takes over the screen)")
        return
    bridge, note = None, ""
    try:                                    # the machine's signals as Doom's controls, if any are set
        bridge = doom_controls.DoomControls.for_profile(ctx.settings, _profile_id(), ctx.session)
    except OSError as e:
        note = (f"Controls from CAN didn't reach Doom: no access to {doom_controls.DEVICE} ({e.strerror or e}). "
                "Run deploy/raspberry-pi/install.sh --device, then log out and in.")
    if bridge is not None:
        bridge.start()
    try:
        ok, msg = app.run_external(argv)
    finally:
        if bridge is not None:
            bridge.stop()
    if not ok:
        app.push(InfoDialog(app, "Doom stopped", [msg]))
    elif note:
        ctx.toast(note)


class GamesScreen(Screen):
    title = "Games"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _rows(self):
        s = self.ctx.settings
        bound = len((s.get(gc.SETTING) or {}).get(_profile_id()) or [])
        return [Row("Harness", lambda: f"best {int(s.get(BEST) or 0)}", select=self._play,
                    hint="turn the handheld sideways; wire bar to bar (or back to the same bar)"),
                Row("Cargo", lambda: f"best {int(s.get(CARGO_BEST) or 0)}", select=self._play_cargo,
                    hint="turn the handheld sideways; pack full rows to ship them"),
                Row("Loom", lambda: f"level {int(s.get(LOOM_LEVEL) or 1)}", select=self._play_loom,
                    hint="a puzzle, no hurry: turn the tiles until the battery lights every lamp"),
                Row("Arbitration", lambda: f"best {int(s.get(ARBITRATION_BEST) or 0)}", select=self._play_arbitration,
                    hint="several frames start at once: pick the one that wins the bus"),
                Row("Doom", doom.wad_name() if doom.installed() else "not installed",
                    select=lambda: play_doom(self.app),
                    hint="the real one; d-pad to move, push to fire, soft keys: use, weapon, map, strafe"
                    if doom.installed() else "not part of CANopener: select to download it"),
                Row("Configure controls", f"{bound} from CAN" if bound else "", kind="action", select=self._controls,
                    hint="how you hold it, the encoder, Doom, and the machine's own signals as controls")]

    def softkeys(self):
        return [SoftKey("Harness", self._play), SoftKey("Cargo", self._play_cargo),
                SoftKey("Loom", self._play_loom), SoftKey("Arbitration", self._play_arbitration)]

    def _play(self):
        self.app.push(HarnessScreen(self.app))

    def _play_cargo(self):
        self.app.push(CargoScreen(self.app))

    def _play_loom(self):
        from .games_loom import LoomScreen
        self.app.push(LoomScreen(self.app))

    def _play_arbitration(self):
        from .games_arbitration import ArbitrationScreen
        self.app.push(ArbitrationScreen(self.app))

    def _controls(self):
        self.app.push(GameControlsScreen(self.app))

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


# How the handheld's own controls work in the games (Games > Configure controls).
HOLD = "games.hold"                     # Harness / Cargo: which way the handheld is turned
HOLD_ANTICLOCKWISE, HOLD_CLOCKWISE = "anticlockwise", "clockwise"
PUSH = "games.push"                     # Harness / Cargo: what pushing the encoder does
PUSH_SWAP, PUSH_DROP = "swap", "drop"
ENCODER_REVERSE = "games.encoder_reverse"
DOOM_TURN_STEPS = (10, 20, 40, 60, 80, 120)


def encoder(ctx, n: int) -> int:
    """An encoder step count as the games use it (System-wide direction, then the games' own)."""
    return -n if ctx.settings.get(ENCODER_REVERSE) else n


def held_clockwise(ctx) -> bool:
    return ctx.settings.get(HOLD) == HOLD_CLOCKWISE


class GameControlsScreen(Screen):
    """Games > Configure controls: how the handheld's buttons work in the games, Doom's setup, and
    which of the machine's signals does what (kept per profile)."""

    title = "Configure controls"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def softkeys(self):
        return [SoftKey("Clear CAN", self._clear, enabled=bool(self._bound()))]

    def _bound(self) -> list:
        return (self.ctx.settings.get(gc.SETTING) or {}).get(_profile_id()) or []

    def _rows(self):
        s = self.ctx.settings
        p = profiles.active()
        clockwise = held_clockwise(self.ctx)
        drop = s.get(PUSH) == PUSH_DROP
        rows = [
            Row("Handheld buttons", kind="header"),
            Row("Hold it for Harness and Cargo", "turned clockwise" if clockwise else "turned anticlockwise",
                select=lambda: s.set(HOLD, HOLD_ANTICLOCKWISE if clockwise else HOLD_CLOCKWISE),
                adjust=lambda _: s.set(HOLD, HOLD_ANTICLOCKWISE if clockwise else HOLD_CLOCKWISE),
                hint="a quarter turn either way; the d-pad and the picture follow"),
            Row("Encoder push in Harness and Cargo", "Drop" if drop else "Swap with the side piece",
                select=lambda: s.set(PUSH, PUSH_SWAP if drop else PUSH_DROP),
                adjust=lambda _: s.set(PUSH, PUSH_SWAP if drop else PUSH_DROP),
                hint="the d-pad drops too; the Swap button still swaps"),
            Row("Reverse the encoder in games", select=lambda: s.set(ENCODER_REVERSE, not s.get(ENCODER_REVERSE)),
                check=lambda: bool(s.get(ENCODER_REVERSE)),
                hint="which way a click turns the piece, a Loom tile, or moves Arbitration's marker"),
            Row(f"Controls from CAN  ·  {p.name if p else 'no profile'}", kind="header"),
        ]
        rows += [self._binding_row(do) for do in gc.ACTIONS]
        rows.append(Row("Doom", kind="header"))
        rows += [self._binding_row(do) for do in gc.DOOM_ACTIONS]
        turn = float(s.get(doom_controls.TURN_SETTING) or doom_controls.TURN_COUNTS)
        rows += [
            Row("Turn speed from CAN", lambda: f"{abs(turn):g}", select=lambda: self._doom_turn(None),
                adjust=self._doom_turn, hint="how far a step of \"Turn the piece\" turns you in Doom"),
            Row("Reverse Doom's turning from CAN", select=lambda: s.set(doom_controls.TURN_SETTING, -turn),
                check=lambda: turn < 0),
            Row("Doom's keys: back to the handheld's", kind="action", select=self._reset_doom_keys,
                hint="if they were changed in Doom's own menus"),
            Row("Doom's buttons: " + " ".join(doom.CONTROLS[:2]), kind="info"),
            Row("Pick a signal while the machine is connected and its value is on the bus (CAN > Live). The "
                "games' actions are steps; Doom's are held for as long as the signal is past its threshold. "
                "Fine-tune in settings.json, games.controls.", kind="info"),
        ]
        return rows

    def _binding_row(self, do) -> Row:
        b = gc.binding_for(self.ctx.settings, _profile_id(), do)
        hint = _describe(b)
        if b:                                   # the signal's value now, to see it move
            row = self._row_for(b["signal"])
            if row is not None and isinstance(row.value, (int, float)):
                hint = f"{hint}  ·  now {row.value:g} {row.sig.unit}".rstrip()
        return Row(gc.ACTION_LABELS[do], (b or {}).get("signal", "Off"), select=lambda: self._bind(do), hint=hint)

    def _row_for(self, name: str):
        m = self.ctx.can_model
        return next((m.rows[k] for k in m.order if m.rows[k].sig.name == name), None)

    def _doom_turn(self, d):
        s = self.ctx.settings
        turn = float(s.get(doom_controls.TURN_SETTING) or doom_controls.TURN_COUNTS)
        steps = DOOM_TURN_STEPS
        mag = abs(turn)
        if d is None:                           # Select: the next speed, round to the first
            mag = next((v for v in steps if v > mag), steps[0])
        else:
            i = min(range(len(steps)), key=lambda i: abs(steps[i] - mag))
            mag = steps[max(0, min(len(steps) - 1, i + d))]
        s.set(doom_controls.TURN_SETTING, -mag if turn < 0 else mag)

    def _reset_doom_keys(self):
        def go():
            for name in ("default.cfg", "chocolate-doom.cfg"):
                try:
                    os.remove(os.path.join(doom.data_dir(), name))
                except FileNotFoundError:
                    pass
            self.ctx.toast("Doom gets the handheld's keys again next time it starts")
        confirm(self.app, "Reset Doom's keys?", "Doom's key and screen settings go back to the handheld's own "
                "the next time it starts. Saved games are kept.", "Reset", go, danger=False)

    def _clear(self):
        pid = _profile_id()

        def go():
            allp = dict(self.ctx.settings.get(gc.SETTING) or {})
            allp.pop(pid, None)
            self.ctx.settings.set(gc.SETTING, allp)
            self.ctx.toast("Controls from CAN cleared for this profile")
        confirm(self.app, "Clear controls from CAN?", f"Every signal picked as a game control for this profile "
                f"({len(self._bound())}) is forgotten. Other profiles keep theirs.", "Clear", go)

    def _bind(self, do):
        model = self.ctx.can_model
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
    if b.get("do") in gc.DOOM_ACTIONS:
        return f"held while at or below {b['below']:g}" if "below" in b else \
            f"held while at or above {b.get('above', 0.5):g}"
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



def draw_wire(ui: UI, r: pygame.Rect, mask: int, wire, bg=None, turn: float = 0.0):
    """A wire tile: a line from the middle to each edge in ``mask`` (harness.N / E / S / W bits).
    ``turn`` (degrees) draws it part-way through a turn."""
    t = ui.theme
    if bg is not None:
        ui.fill(r, bg, max(2, t.radius // 2))
    c = ui.color(wire)
    w = max(3, round(r.w * .2))
    cx, cy = r.center
    half = r.w / 2
    for bit, deg in ((hz.N, -90), (hz.E, 0), (hz.S, 90), (hz.W, 180)):
        if mask & bit:
            a = math.radians(deg + turn)
            pygame.draw.line(ui.surface, c, (cx, cy), (cx + math.cos(a) * half, cy + math.sin(a) * half), w)
    pygame.draw.circle(ui.surface, c, (cx, cy), w * .75)


def draw_star(ui: UI, center, radius: float, color, width: int = 0):
    cx, cy = center
    pts = [(cx + math.sin(i * math.pi / 5) * radius * (1 if i % 2 == 0 else .45),
            cy - math.cos(i * math.pi / 5) * radius * (1 if i % 2 == 0 else .45)) for i in range(10)]
    pygame.draw.polygon(ui.surface, ui.color(color), pts, width)


def result_card(ui: UI, area: pygame.Rect, lines: list, w: float, accent="accent") -> pygame.Rect:
    """The card over a game: paused, over, solved. ``lines``: (text, font, colour), top to bottom."""
    t = ui.theme
    heights = [t.font(font).get_linesize() for _, font, _ in lines]
    o = pygame.Rect(0, 0, w, sum(heights) + t.pad * (len(lines) + 3))
    o.center = area.center
    ui.card(o, "panel2", t.radius * 2)
    ui.fill(o, accent, t.radius * 2, 2)
    y = o.y + t.pad * 2
    for (text, font, color), h in zip(lines, heights):
        ui.text(text, (o.centerx, y + h / 2), font, color, align="center", max_w=o.w - 2 * t.pad)
        y += h + t.pad
    return o


class Effects:
    """The moving bits over a board: sparks where something cleared, points floating up, the streak
    a dropped piece leaves, a shake. Positions are in board cells; nothing here changes the game."""

    def __init__(self):
        self.rng = random.Random()
        self.sparks: list = []      # [x, y, vx, vy, t0, life, colour]
        self.popups: list = []      # (text, x, y, t0, font, colour)
        self.trails: list = []      # (x, y0, y1, t0)
        self.flashes: list = []     # (cells, t0)
        self.shake_until = 0.0
        self.shake_amp = 0.0

    def burst(self, x: float, y: float, color, now: float, n: int = 3):
        for _ in range(n):
            a = self.rng.uniform(0, math.tau)
            v = self.rng.uniform(2, 7)
            self.sparks.append([x, y, math.cos(a) * v, math.sin(a) * v - 2, now, self.rng.uniform(.35, .7), color])
        del self.sparks[:-90]

    def popup(self, text: str, x: float, y: float, now: float, font: str = "body", color="text"):
        self.popups.append((text, x, y, now, font, color))

    def trail(self, x: int, y0: float, y1: float, now: float):
        self.trails.append((x, y0, y1, now))

    def flash(self, cells, now: float):
        self.flashes.append((list(cells), now))

    def shake(self, now: float, amp: float = .12, secs: float = .25):
        self.shake_until, self.shake_amp = now + secs, amp

    def offset(self, now: float, cell: float) -> tuple[float, float]:
        if now >= self.shake_until:
            return 0.0, 0.0
        k = self.shake_amp * cell * (self.shake_until - now) * 4
        return math.sin(now * 90) * k, math.cos(now * 70) * k

    def draw(self, ui: UI, bx: float, by: float, cell: float, now: float, cols: int):
        self.trails = [tr for tr in self.trails if now - tr[3] < .18]
        for x, y0, y1, t0 in self.trails:
            w = cell * .5 * (1 - (now - t0) / .18)
            ui.fill(pygame.Rect(bx + (x + .5) * cell - w / 2, by + y0 * cell, w, (y1 - y0) * cell), "panel_hi",
                    round(w / 2))
        self.flashes = [f for f in self.flashes if now - f[1] < .12]
        for cells, _ in self.flashes:
            for x, y in cells:
                ui.fill(pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-2, -2), "white",
                        max(2, ui.theme.radius // 3), max(2, round(cell * .12)))
        self.sparks = [sp for sp in self.sparks if now - sp[4] < sp[5]]
        for x, y, vx, vy, t0, life, color in self.sparks:
            dt = now - t0
            size = max(2, cell * .22 * (1 - dt / life))
            sx, sy = bx + (x + vx * dt) * cell, by + (y + vy * dt + 9 * dt * dt) * cell
            ui.fill(pygame.Rect(sx - size / 2, sy - size / 2, size, size), color)
        self.popups = [p for p in self.popups if now - p[3] < 1.0]
        for text, x, y, t0, font, color in self.popups:
            dt = now - t0
            px = min(max(bx + x * cell, bx + cell * 1.2), bx + (cols - 1.2) * cell)
            ui.text(text, (px, by + (y - 1.6 * (1 - (1 - dt) ** 2)) * cell), font, color, align="center")


class SidewaysGame(Screen):
    """What Harness and Cargo share: played sideways (drawn portrait, turned), the same controls
    (one command interface: buttons, encoder, touch, CAN signals), the score header with the side
    and next pieces, the button row, the effects and the pause / game-over card. A game supplies
    ``new_game``, ``COLS`` / ``ROWS``, ``best_key`` and the drawing of its board and pieces."""

    COLS, ROWS = 8, 12
    best_key = BEST
    bars = False                            # Harness: bus bars down both sides of the board
    help: list[str] = []
    DPAD = {inp.UP: gc.LEFT, inp.DOWN: gc.RIGHT, inp.LEFT: gc.DROP, inp.RIGHT: None}
    DPAD_CLOCKWISE = {inp.DOWN: gc.LEFT, inp.UP: gc.RIGHT, inp.RIGHT: gc.DROP, inp.LEFT: None}
    CONTROLS_HELP = [
        "",
        "Hold the handheld turned a quarter turn {way} (Games > Configure controls to change it).",
        "Encoder: turn the piece. Push it: {push}.",
        "D-pad: left / right move, down drops. A piece that has landed can still be slid or turned "
        "for a moment.",
        "Touch: tap a column to send the piece there, tap the piece to turn it, or use the buttons "
        "under the board. Tap the score to pause.",
        "Games > Controls from CAN lets the machine's own signals play.",
    ]

    def __init__(self, app):
        super().__init__(app)
        self.game = self.new_game(time.monotonic())
        self.controls = None
        self.fx = Effects()
        self._saved = False
        self._new_best = False
        self._shown = 0.0                   # the score as drawn: it counts up to the real one
        self._clear_t0 = 0.0
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
                SoftKey("New game", self._new), SoftKey("How to play", self._help)]

    def _pause(self):
        self.game.toggle_pause(time.monotonic())

    def _new(self):
        self.game, self.fx = self.new_game(time.monotonic()), Effects()
        self._saved, self._new_best, self._shown = False, False, 0.0

    def _help(self):
        if not self.game.paused and not self.game.over:
            self._pause()
        way = "clockwise" if held_clockwise(self.ctx) else "anticlockwise"
        push = "drop the piece" if self.ctx.settings.get(PUSH) == PUSH_DROP else \
            "swap with the side piece (once per piece)"
        controls = [line.format(way=way, push=push) for line in self.CONTROLS_HELP]
        self.app.push(InfoDialog(self.app, f"{self.title}: how to play", self.help + controls))

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
                g.command(gc.DROP if self.ctx.settings.get(PUSH) == PUSH_DROP else gc.SWAP, 1, now)
        elif action == inp.ROTATE:
            g.command(gc.TURN, encoder(self.ctx, n), now)
        elif action in (inp.UP, inp.DOWN, inp.LEFT, inp.RIGHT):
            # held sideways the d-pad is turned too: turned anticlockwise its up points left and its
            # left points down; turned clockwise its down points left and its right points down
            cmd = (self.DPAD_CLOCKWISE if held_clockwise(self.ctx) else self.DPAD)[action]
            if cmd is None:
                return False
            g.command(cmd, 1, now)
        else:
            return False
        return True

    # --- per frame ---
    def draw(self, ui, rect):
        g, now = self.game, time.monotonic()
        if self.controls is not None and not g.paused:
            for cmd, n in self.controls.poll(self.ctx.session):
                g.command(cmd, n, now)
        g.update(now)
        self._events(now)
        if g.over and not self._saved:
            self._saved = True
            self.ctx.cue("game_over")
            if g.score > self._best():
                self._new_best = True
                self.ctx.settings.set(self.best_key, g.score)
        self._shown = g.score if g.score - self._shown < 1 else self._shown + (g.score - self._shown) * .25
        portrait = self._portrait
        if portrait is None or portrait.get_size() != (rect.h, rect.w):     # made once, not every frame
            portrait = self._portrait = pygame.Surface((rect.h, rect.w))
        portrait.fill(ui.color("bg"))
        pu = UI(portrait, ui.theme)
        pu._cache = ui._cache                   # share rendered text between frames
        self._draw_portrait(pu, portrait.get_rect(), now)
        clockwise = held_clockwise(self.ctx)
        # held anticlockwise the top of the picture goes to the screen's right; clockwise, its left
        ui.surface.blit(pygame.transform.rotate(portrait, 90 if clockwise else -90), rect.topleft)
        ui.hit(rect, lambda: None)
        for pr, fn in pu.hits:                  # the picture's taps, turned the same way
            if clockwise:
                ui.hit(pygame.Rect(rect.x + pr.y, rect.bottom - pr.right, pr.h, pr.w), fn)
            else:
                ui.hit(pygame.Rect(rect.right - pr.bottom, rect.y + pr.x, pr.h, pr.w), fn)

    def _events(self, now: float):
        """What the game says just happened -> sounds and effects."""
        g, fx = self.game, self.fx
        for ev in g.events:
            if ev[0] == "lock":
                fx.flash(ev[1], now)
                if ev[2] > 1:
                    for x in {x for x, _ in ev[1]}:
                        bottom = max(y for cx, y in ev[1] if cx == x)
                        fx.trail(x, bottom - ev[2], bottom, now)
            elif ev[0] == "clear":
                self._clear_t0 = now
                self.ctx.cue("game_clear")
                self._cleared(ev, now)
            elif ev[0] == "sweep":
                fx.popup(f"Clean sweep +{ev[1]}", self.COLS / 2, self.ROWS / 2, now, "big", "focus")
                fx.shake(now, .2, .35)
            elif ev[0] == "level":
                self.ctx.cue("game_level")
                fx.popup(f"Level {ev[1]}", self.COLS / 2, 3.5, now, "big", "accent")
        g.events.clear()

    def _cleared(self, ev, now: float):
        raise NotImplementedError

    def _clear_frac(self, now: float) -> float:
        """0-1 through the moment cleared cells stay lit (they shrink away as it runs out)."""
        return max(0.0, min(1.0, (now - self._clear_t0) / .35)) if not self.game.paused else 0.0

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

    # --- what a game tells the shared drawing ---
    def _progress(self) -> float:
        """0-1 towards the next level."""
        return 0.0

    def _danger(self) -> bool:
        """The pile is nearly at the top."""
        return False

    def _stats(self) -> str:
        return ""

    # --- drawing ---
    def _draw_portrait(self, ui: UI, r: pygame.Rect, now: float):
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
        for b, item, dim in ((side, g.hold, g.held), (nxt, g.next, False)):
            ui.card(b, "panel", t.radius)
            if item is not None:
                self._draw_preview(ui, b.inflate(-t.pad, -t.pad), item, dim)
        mid = r.centerx
        ui.text(f"{round(self._shown)}", (mid, r.y + head * .34), "title", "text", align="center")
        best = max(self._best(), g.score)
        ui.text(f"Level {g.level}   Best {best}", (mid, r.y + head * .7), "small", "dim", align="center")
        bar = pygame.Rect(0, 0, nxt.x - side.right - 4 * t.pad, max(3, t.pad // 2))    # towards the next level
        bar.center = (mid, r.y + head * .9)
        ui.fill(bar, "panel_hi", bar.h // 2)
        if self._progress() > 0:
            ui.fill(pygame.Rect(bar.x, bar.y, bar.w * self._progress(), bar.h), "accent", bar.h // 2)
        # board
        sx, sy = self.fx.offset(now, cell)
        bx, by = bx + sx, by + sy
        board = pygame.Rect(bx, by, bw, bh)
        ui.card(board.inflate(4, 4), "panel", t.radius)
        for x in range(1, self.COLS):           # faint lane lines help aim
            lx = bx + x * cell
            pygame.draw.line(ui.surface, ui.color("panel_hi"), (lx, by + 2), (lx, by + bh - 3), 1)
        if self.bars:
            bar_w = cell * .4
            for x in (bx - bar_w - 2, bx + bw + 2):
                ui.fill(pygame.Rect(x, by, bar_w, bh), "accent", t.radius)
        if self._danger() and not g.over and now % .8 < .4:
            ui.fill(board.inflate(4, 4), "bad", t.radius, 2)
        for x in range(self.COLS):              # tap a column: the piece goes there (or turns)
            ui.hit(pygame.Rect(bx + x * cell, by, cell, bh), lambda x=x: self._tap_column(x))
        self._draw_board(ui, bx, by, cell, now)
        if not g.paused:
            self.fx.draw(ui, bx, by, cell, now, self.COLS)
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
            if g.over:
                was = ("New best!", "body", "focus") if self._new_best else (f"Best {best}", "body", "dim")
                lines = [("Game over", "big", "text"), was,
                         (f"Score {g.score}", "big", "accent"), (self._stats(), "small", "text"),
                         ("Tap or push: new game  ·  Back: leave", "small", "dim")]
            else:
                lines = [("Paused", "big", "text"), ("Tap or push: resume", "body", "text"),
                         ("Back: leave", "small", "dim")]
            result_card(ui, pygame.Rect(r.x, by, r.w, bh), lines, max(bw * .9, r.w * .8))
            ui.hit(r, self._tap_overlay)

    def _draw_board(self, ui: UI, bx, by, cell, now: float):
        raise NotImplementedError

    def _draw_preview(self, ui: UI, box: pygame.Rect, item, dim: bool = False):
        raise NotImplementedError


class HarnessScreen(SidewaysGame):
    title = "Harness"
    COLS, ROWS = hz.COLS, hz.ROWS
    best_key = BEST
    bars = True
    help = [
        "Wire segments fall between two bus bars. Join them up into a wire that touches the bars at "
        "two points and the whole circuit lights up and clears, branches and all.",
        "Bar to bar scores double; a loop back to the same bar scores single. Bigger circuits score more.",
        "Wires above fall into the gap. If that closes another circuit it's a chain, and each link "
        "in a chain multiplies the score.",
        "Lit wire already reaches a bar. The outline shows where the segment will land: it turns "
        "the accent colour if it would join lit wire, and bright if it would close a circuit.",
        "Segments that join nothing pile up. The game ends when the middle column is full.",
    ]

    def new_game(self, now):
        return hz.Harness(now=now)

    def _progress(self):
        return (self.game.circuits % hz.CIRCUITS_PER_LEVEL) / hz.CIRCUITS_PER_LEVEL

    def _danger(self):
        return any(self.game.grid[y][hz.COLS // 2] for y in range(1, 4))

    def _stats(self):
        g = self.game
        chain = f"  ·  best chain ×{g.best_chain}" if g.best_chain > 1 else ""
        return f"{g.circuits} circuit{'' if g.circuits == 1 else 's'}{chain}  ·  level {g.level}"

    def _cleared(self, ev, now):
        _, cells, points, chain, spans = ev
        for x, y in cells[::max(1, len(cells) // 24)]:
            self.fx.burst(x + .5, y + .5, "focus", now, 2)
        cx = sum(x for x, _ in cells) / len(cells) + .5
        cy = min(y for _, y in cells)
        self.fx.popup(f"+{points}", cx, cy, now, "big", "focus")
        note = f"Chain ×{chain}" if chain > 1 else ("Bar to bar" if spans else "")
        if note:
            self.fx.popup(note, cx, cy + 1.1, now, "small", "text")
        if chain > 1 or len(cells) >= 14:
            self.fx.shake(now)

    def _draw_preview(self, ui, box, mask, dim=False):
        draw_wire(ui, box, mask, "dim" if dim else "text", "panel2")

    def _draw_board(self, ui, bx, by, cell, now):
        g, t = self.game, ui.theme
        live = g.live_cells()
        shrink = round(cell * .5 * self._clear_frac(now) ** 3)
        for y in range(hz.ROWS):
            for x in range(hz.COLS):
                mask = g.grid[y][x]
                if not mask:
                    continue
                r = pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-2, -2)
                if (x, y) in g.lit:             # a closed circuit: bright, then it shrinks away
                    draw_wire(ui, r.inflate(-shrink, -shrink), mask, "focus" if now % .14 < .07 else "accent",
                              "panel2")
                else:
                    draw_wire(ui, r, mask, "accent" if (x, y) in live else "dim", "panel2")
        p = g.piece
        if p is not None:
            gy = g.ghost_y()
            if gy is not None and gy != p.y:
                what = g.preview()              # what dropping it here would do
                gr = pygame.Rect(bx + p.x * cell, by + gy * cell, cell, cell).inflate(-4, -4)
                if what:
                    draw_wire(ui, gr, p.mask, "accent_dim" if what == "live" else "dim")
                ui.fill(gr, {"circuit": "focus", "live": "accent"}.get(what, "line"), t.radius,
                        3 if what == "circuit" else 2)
            pr = pygame.Rect(bx + p.x * cell, by + (p.y + g.fall_frac(now)) * cell, cell, cell).inflate(-2, -2)
            draw_wire(ui, pr, p.mask, "text", "panel2")
            ui.fill(pr, "focus", t.radius, max(2, t.pad // 3))


# Crate colours, by shape: a theme colour each, so every theme gets its own set.
CRATE_ROLES = {"single": "dim", "pair": "sim", "bar": "ok", "corner": "warn", "pallet": "off", "p": "idle",
               "u": "bad", "v": "accent", "t": "wait", "l": "edge_accent"}


class CargoScreen(SidewaysGame):
    title = "Cargo"
    COLS, ROWS = cg.COLS, cg.ROWS
    best_key = CARGO_BEST
    help = [
        "Crates drop into a trailer. Pack a row wall to wall and it ships out; everything above "
        "settles down a row.",
        "Several rows at once are worth much more than the same rows one at a time.",
        "Ship something with crate after crate and a streak builds: a bonus each time. Empty the "
        "trailer completely for a clean sweep.",
        "The crates come in very different sizes, from singles to five-crate loads. Use the side "
        "bay to keep an awkward one for later.",
        "The game ends when the load reaches the top.",
    ]

    def new_game(self, now):
        return cg.Cargo(now=now)

    def _progress(self):
        return (self.game.rows_cleared % cg.ROWS_PER_LEVEL) / cg.ROWS_PER_LEVEL

    def _danger(self):
        return any(kind for row in self.game.grid[:5] for kind in row)

    def _stats(self):
        g = self.game
        streak = f"  ·  best streak ×{g.best_streak}" if g.best_streak > 1 else ""
        return f"{g.rows_cleared} row{'' if g.rows_cleared == 1 else 's'} shipped{streak}  ·  level {g.level}"

    def _cleared(self, ev, now):
        _, rows, points, streak = ev
        for y in rows:
            for x in range(0, cg.COLS, 2):
                self.fx.burst(x + 1, y + .5, "focus", now, 2)
        names = {2: "Double", 3: "Triple", 4: "Full load", 5: "Full load"}
        note = names.get(len(rows), "")
        if streak > 1:
            note = f"{note}  streak ×{streak}".strip()
        self.fx.popup(f"+{points}", cg.COLS / 2, rows[0], now, "big", "focus")
        if note:
            self.fx.popup(note, cg.COLS / 2, rows[0] + 1.3, now, "small", "text")
        if len(rows) >= 3:
            self.fx.shake(now, .1 * len(rows) / 3)

    def _piece_under(self, x) -> bool:
        return any(cx == x for cx, _ in self.game.piece.at())

    def _crate(self, ui: UI, r: pygame.Rect, kind: str, lit: bool = False, dim: bool = False):
        fill = ui.color("focus" if lit else CRATE_ROLES.get(kind, "dim"))
        if dim:
            fill = themes.mix(fill, ui.color("panel"), .6)
        rad = max(2, ui.theme.radius // 3)
        pygame.draw.rect(ui.surface, fill, r, border_radius=rad)
        pygame.draw.line(ui.surface, themes.mix(fill, ui.color("white"), .45), (r.x + rad, r.y + 1),
                         (r.right - rad - 1, r.y + 1), max(1, r.h // 10))
        pygame.draw.rect(ui.surface, themes.mix(fill, ui.color("black"), .4), r, max(1, r.w // 14), border_radius=rad)

    def _draw_preview(self, ui, box, kind, dim=False):
        cells = cg.SHAPES[kind]
        w = 1 + max(x for x, _ in cells)
        h = 1 + max(y for _, y in cells)
        c = min(box.w / max(w, 3), box.h / max(h, 3))
        ox = box.centerx - w * c / 2
        oy = box.centery - h * c / 2
        for x, y in cells:
            self._crate(ui, pygame.Rect(ox + x * c, oy + y * c, c, c).inflate(-2, -2), kind, dim=dim)

    def _draw_board(self, ui, bx, by, cell, now):
        g, t = self.game, ui.theme
        frac = self._clear_frac(now)
        for y, row in enumerate(g.grid):
            lit = y in g.lit
            for x, kind in enumerate(row):
                if kind is None:
                    continue
                r = pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-2, -2)
                if lit:                         # a shipped row: lights up, then goes from the middle out
                    if abs(x + .5 - cg.COLS / 2) / (cg.COLS / 2) >= frac * 1.2 - .2:
                        self._crate(ui, r, kind, lit=True)
                else:
                    self._crate(ui, r, kind)
        p = g.piece
        if p is not None:
            gy = g.ghost_y()
            if gy is not None and gy != p.y:
                for x, y in p.at(y=gy):
                    ui.fill(pygame.Rect(bx + x * cell, by + y * cell, cell, cell).inflate(-4, -4), "line",
                            max(2, t.radius // 3), 2)
            off = g.fall_frac(now)
            for x, y in p.at():
                self._crate(ui, pygame.Rect(bx + x * cell, by + (y + off) * cell, cell, cell).inflate(-2, -2), p.kind)
