"""The handheld's hidden Games: the rules of Harness, Cargo, Loom and Arbitration, game controls from CAN
signals, the screens and the unlock."""

import os
import threading

import pytest

from canopener.core.settings import Settings
from canopener.device import arbitration as ab
from canopener.device import cargo as cg
from canopener.device import doom, doom_controls
from canopener.device import game_controls as gc
from canopener.device import harness as hz
from canopener.device import loom as lm
from canopener.j1939 import signals
from canopener.j1939.signals import Signal

EW = hz.E | hz.W


@pytest.fixture(autouse=True)
def no_doom(monkeypatch):
    """Whatever this machine has installed: no Doom, and no way to fetch it, unless a test says so."""
    monkeypatch.setattr(doom, "ENGINE_DIRS", ())
    monkeypatch.setattr(doom, "SYSTEM_WAD_DIRS", ())
    monkeypatch.setattr(doom, "ENGINES", ("no-such-doom-engine",))
    monkeypatch.setattr(doom, "can_install", lambda: False)


def bottom_row(g, gap):
    for x in range(hz.COLS):
        if x != gap:
            g.grid[hz.ROWS - 1][x] = EW


def test_a_wire_from_bar_to_bar_lights_up_clears_and_scores():
    g = hz.Harness(seed=1)
    bottom_row(g, gap=3)
    g.piece = hz.Piece(EW, x=3, y=0)
    g.drop(now=0)
    assert len(g.lit) == hz.COLS and g.circuits == 1 and g.score == 10 * hz.COLS * 2     # bar to bar: double
    assert g.piece is None                          # nothing moves while it lights up
    g.update(hz.CLEAR_S + 0.01)
    assert g.grid[hz.ROWS - 1] == [0] * hz.COLS and g.piece is not None


def test_ends_must_meet_and_a_dead_end_doesnt_count():
    g = hz.Harness(seed=1)
    bottom_row(g, gap=3)
    g.piece = hz.Piece(hz.N | hz.S, x=3, y=0)       # a vertical wire doesn't join its neighbours
    g.drop(now=0)
    assert not g.lit and g.circuits == 0


def test_wires_that_fall_into_the_gap_can_close_a_chain():
    g = hz.Harness(seed=1)
    bottom_row(g, gap=3)
    for x in range(hz.COLS):                        # the row above, one segment still up in the air
        if x != 5:
            g.grid[hz.ROWS - 2][x] = EW
    g.grid[hz.ROWS - 3][5] = EW
    g.piece = hz.Piece(EW, x=3, y=hz.ROWS - 1)      # already in the gap (the row above covers it)
    g.drop(now=0)
    g.update(1.0)                                   # first circuit goes, the rest falls and joins up
    assert g.chain == 2 and g.circuits == 2
    assert g.score == 20 * hz.COLS * 1 + 20 * hz.COLS * 2


def test_a_loop_back_to_the_same_bar_counts_once():
    g = hz.Harness(seed=1)
    g.grid[hz.ROWS - 1][0] = hz.W | hz.N            # out of the left bar, up, and back into it
    g.piece = hz.Piece(hz.W | hz.S, x=0, y=hz.ROWS - 2)
    g.drop(now=0)
    assert g.lit == {(0, hz.ROWS - 1), (0, hz.ROWS - 2)} and g.score == 10 * 2


def test_controls_turn_move_swap_once_and_pause():
    g = hz.Harness(seed=3)
    start = g.piece.mask
    g.command(gc.TURN, 1)
    assert g.piece.mask == hz.rotate_cw(start)
    g.command(gc.TURN, -1)
    assert g.piece.mask == start
    g.command(gc.COLUMN, 0)
    assert g.piece.x == 0
    g.command(gc.LEFT)
    assert g.piece.x == 0                           # the bus bar stops it
    g.command(gc.RIGHT, 2)
    assert g.piece.x == 2
    nxt = g.next
    g.command(gc.SWAP)
    assert g.hold == start and g.piece.mask == nxt
    g.command(gc.SWAP)                              # once per segment
    assert g.hold == start
    g.command(gc.PAUSE, now=5)
    g.update(100)
    assert g.paused and g.piece.y == 0              # nothing falls while paused


def test_segments_fall_faster_each_level_and_the_game_ends_when_the_top_fills():
    assert hz.fall_interval(1) == 0.8 and hz.fall_interval(5) < hz.fall_interval(4)
    assert hz.fall_interval(99) == 0.12
    g = hz.Harness(seed=4)
    g.update(0.81)
    assert g.piece.y == 1
    for y in range(hz.ROWS):
        g.grid[y][hz.COLS // 2] = hz.N | hz.S
    g.piece = hz.Piece(hz.N | hz.S, x=0, y=0)
    g.drop(0)
    assert g.over


def test_harness_deals_from_a_bag_and_a_landed_segment_can_still_be_slid():
    g = hz.Harness(seed=7)
    dealt = [g._take_next() for _ in range(20)]         # two bags' worth (the first one is already begun)
    assert sum(bin(m).count("1") == 4 for m in dealt) in (3, 4, 5)      # crosses: 2 in every 10
    g = hz.Harness(seed=7, now=0)
    g.piece = hz.Piece(EW, x=3, y=hz.ROWS - 1)          # on the floor
    g.command(gc.LEFT, 1, now=0.7)                      # moved just before it would have set ...
    g.update(0.81)
    assert g.piece is not None and g.piece.x == 2       # ... so it gets a moment more
    g.update(0.7 + hz.LOCK_S + 0.01)
    assert g.piece is not None and g.piece.y == 0 and g.grid[hz.ROWS - 1][2] == EW      # set; the next one is out
    assert g.events[0] == ("lock", [(2, hz.ROWS - 1)], 0)


def test_harness_says_what_a_drop_would_do_and_reports_it():
    g = hz.Harness(seed=1)
    bottom_row(g, gap=3)
    g.piece = hz.Piece(hz.N | hz.S, x=3, y=0)
    assert g.preview() is None                          # joins nothing
    g.piece.mask = hz.N | hz.E
    assert g.preview() == "live"                        # joins wire that reaches a bar
    g.piece.mask = EW
    assert g.preview() == "circuit" and not g.grid[hz.ROWS - 1][3]      # only looking
    g.events.clear()
    g.drop(now=0)
    lock, clear = g.events
    assert lock == ("lock", [(3, hz.ROWS - 1)], hz.ROWS - 1)
    assert clear[0] == "clear" and len(clear[1]) == hz.COLS and clear[2:] == (160, 1, True)


def test_falling_games_survive_a_careless_player():
    import random
    for make, cols in ((hz.Harness, hz.COLS), (cg.Cargo, cg.COLS)):
        rng = random.Random(3)
        g, now = make(seed=3), 0.0
        for _ in range(3000):
            now += 0.2
            g.command(rng.choice((gc.TURN, gc.COLUMN, gc.DROP, gc.SWAP, gc.LEFT, gc.RIGHT)), rng.randrange(cols), now)
            g.update(now)
            if g.over:
                break
        assert g.over and g.events[-1] == ("over",) and g.placed > 10


# --- Cargo -------------------------------------------------------------------
def test_cargo_streaks_a_clean_sweep_and_turning_off_the_floor():
    g = cg.Cargo(seed=1)

    def fill(y, gap):
        for x in range(cg.COLS):
            g.grid[y][x] = None if x == gap else "single"
    fill(cg.ROWS - 1, 0)
    fill(cg.ROWS - 2, 0)
    g.grid[cg.ROWS - 3][5] = "pair"                     # something left over: no sweep yet
    g.piece = cg.Crate("single", cg.SHAPES["single"], x=0, y=0)
    g.drop(0)
    assert g.streak == 1 and g.score == cg.ROW_POINTS[1]
    g.update(1)
    g.piece = cg.Crate("single", cg.SHAPES["single"], x=0, y=0)
    g.drop(1)                                           # the next crate ships a row too: a streak
    assert g.streak == 2 and g.score == 2 * cg.ROW_POINTS[1] + cg.STREAK_POINTS
    g.update(2)
    assert g.sweeps == 0
    g.piece = cg.Crate("single", cg.SHAPES["single"], x=9, y=0)
    g.drop(2)                                           # ships nothing: the streak is over
    assert g.streak == 0
    g.update(3)
    g.grid = [[None] * cg.COLS for _ in range(cg.ROWS)]
    fill(cg.ROWS - 1, 4)
    g.piece = cg.Crate("single", cg.SHAPES["single"], x=4, y=0)
    score = g.score
    g.drop(3)
    g.update(4)                                         # the trailer is empty: a clean sweep
    assert g.sweeps == 1 and g.score == score + cg.ROW_POINTS[1] + cg.SWEEP_POINTS
    assert ("sweep", cg.SWEEP_POINTS) in g.events
    g.piece = cg.Crate("bar", cg.SHAPES["bar"], x=3, y=cg.ROWS - 1)     # lying on the floor
    assert g.turn(1) and g.piece.width == 1 and g.piece.y == cg.ROWS - 3     # stands up: lifted to fit
    dealt = [g._take_next() for _ in range(2 * sum(cg.WEIGHTS.values()))]
    assert all(dealt.count(k) >= cg.WEIGHTS[k] for k in cg.KINDS)        # a bag: every shape keeps coming


def test_cargo_full_rows_ship_and_score_more_together():
    g = cg.Cargo(seed=1)
    for y in (cg.ROWS - 1, cg.ROWS - 2):
        for x in range(cg.COLS):
            if x not in (4, 5):
                g.grid[y][x] = "single"
    g.piece = cg.Crate("pallet", cg.SHAPES["pallet"], x=4, y=0)
    g.drop(now=0)
    assert g.lit == {cg.ROWS - 1, cg.ROWS - 2} and g.rows_cleared == 2 and g.score == cg.ROW_POINTS[2]
    assert g.piece is None                          # nothing moves while the rows light up
    g.grid[cg.ROWS - 3][0] = "pair"                 # a crate above them settles two rows down
    g.update(cg.CLEAR_S + 0.01)
    assert g.grid[cg.ROWS - 1][0] == "pair" and not any(g.grid[cg.ROWS - 2]) and g.piece is not None


def test_cargo_controls_walls_turns_and_swap():
    g = cg.Cargo(seed=2)
    g.piece = cg.Crate("l", cg.SHAPES["l"], x=4, y=0)
    g.command(gc.LEFT, 20)
    assert g.piece.x == 0                           # the wall stops it
    g.command(gc.TURN, 1)                           # lying down now: 4 wide, 2 high
    assert g.piece.width == 4 and len(g.piece.cells) == 5
    g.command(gc.RIGHT, 20)
    assert g.piece.x == cg.COLS - 4
    g.command(gc.TURN, -1)                          # back upright, still on the board
    assert all(0 <= x < cg.COLS for x, _ in g.piece.at())
    g.command(gc.COLUMN, 0)
    assert g.piece.x == 0
    nxt, cur = g.next, g.piece.kind
    g.command(gc.SWAP)
    assert g.hold == cur and g.piece.kind == nxt
    g.command(gc.SWAP)                              # once per crate
    assert g.hold == cur
    g.command(gc.PAUSE, now=1)
    g.update(100)
    assert g.paused and g.piece.y == 0


def test_cargo_turns_are_quarter_turns():
    for cells in cg.SHAPES.values():
        assert cg.turn_cells(cells, 4) == cg.normalise(cells)
        assert cg.turn_cells(cg.turn_cells(cells, 1), -1) == cg.normalise(cells)


def test_cargo_falls_faster_each_level_and_ends_when_the_load_reaches_the_top():
    assert cg.fall_interval(3) < cg.fall_interval(2) and cg.fall_interval(99) == 0.1
    g = cg.Cargo(seed=4)
    y = g.piece.y
    g.update(cg.fall_interval(1) + 0.01)
    assert g.piece.y == y + 1
    for y in range(cg.ROWS):
        g.grid[y][cg.COLS // 2 - 1] = "single"
    g.drop(1)
    assert g.over and g.piece is None


# --- Loom --------------------------------------------------------------------
def solve(g, leave=None):
    """Turn every tile to where the board was made from (all but ``leave``); the turns that one needs."""
    left = 0
    for y in range(g.rows):
        for x in range(g.cols):
            k = next(k for k in range(4) if hz.rotate(g.grid[y][x], k) == g.solution[y][x])
            if (x, y) == leave:
                left = k
            elif k:
                g.turn(k if k < 3 else -1, now=5.0, at=(x, y))
    return left


def test_loom_boards_can_be_solved_and_par_is_the_fewest_turns():
    for level in range(1, len(lm.SIZES) + 3):
        g = lm.Loom(level, seed=level)
        assert (g.cols, g.rows) == lm.size_for(level)
        assert not g.solved and len(g.live()) < g.cols * g.rows and g.par >= g.cols * g.rows // 2
        assert all(bin(m).count("1") in (1, 2, 3) for row in g.grid for m in row)       # no four-way tiles
        lamps = g.lamps()
        solve(g)
        assert g.solved and g.turns == g.par and g.stars == 3
        assert set(lamps) <= g.live() and g.events[-1] == ("solved", g.turns, g.par)
        assert not g.turn(1)                            # solved: the tiles stay put


def test_loom_cursor_turns_stars_and_the_clock():
    g = lm.Loom(3, seed=2)
    g.cursor = (0, 0)
    g.command(gc.LEFT)
    assert g.cursor == (g.cols - 1, 0)                  # wraps round the edge
    g.command(gc.DROP)
    g.command(gc.COLUMN, 2)
    assert g.cursor == (2, 1)
    mask = g.grid[1][2]
    assert g.elapsed(9) == 0                            # the clock starts with the first turn
    g.command(gc.TURN, 1, now=10)
    assert g.grid[1][2] == hz.rotate_cw(mask) and g.turns == 1 and g.elapsed(12) == 2
    g.command(gc.TURN, -1, now=11)
    assert g.grid[1][2] == mask and g.turns == 2
    g.turn(3)
    assert g.turns == 3                                 # three clockwise is one the other way
    assert (lm.stars(10, 10), lm.stars(15, 10), lm.stars(17, 10)) == (3, 2, 1)


# --- Arbitration ---------------------------------------------------------------
def test_arbitration_follows_the_can_rules():
    F = ab.Frame
    assert ab.winner([F(0x39A), F(0x20C), F(0x7FF)]) == 1                   # lowest identifier
    assert ab.winner([F(0x123, remote=True), F(0x123)]) == 1                # data beats a remote request
    assert ab.winner([F(0x123 << 18, extended=True), F(0x123)]) == 1        # same 11 bits: 11-bit wins
    assert ab.winner([F(0x123 << 18, extended=True), F(0x123, remote=True)]) == 1   # ... even its remote request
    assert ab.winner([F(0x122 << 18 | 0x3FFFF, extended=True), F(0x123)]) == 0      # a lower base still wins
    assert ab.winner([F(0x18FEF100, True), F(0x0CF00400, True), F(0x18F00400, True)]) == 1     # J1939: priority
    assert len(F(0x123).bits()) == 13 and len(F(0x18FEF100, True).bits()) == 32
    assert F(0x18FEF100, True).bits()[11:13] == (1, 1)                      # SRR, IDE: recessive
    assert F(0x18FEF100, True, style=ab.J1939).label == "P6  PGN 0FEF1  SA 00"
    frames = [F(0x39A), F(0x20C)]
    assert ab.lost_at(frames[0], frames[1]) == 2 and ab.lost_at(frames[1], frames[1]) is None
    assert "bit 3 (ID8)" in ab.reason(frames, 1)
    assert "remote" in ab.reason([F(0x123, remote=True), F(0x123)], 1)
    assert "11-bit frame beats" in ab.reason([F(0x123 << 18, extended=True), F(0x123)], 1)


def test_arbitration_rounds_always_have_one_winner():
    import random
    rng = random.Random(4)
    for level in range(1, len(ab.LEVELS) + 3):
        kinds, count = ab.LEVELS[min(level, len(ab.LEVELS)) - 1]
        for _ in range(200):
            frames, kind = ab.make_round(rng, level)
            assert kind in kinds and len(frames) == count
            assert len({f.bits() for f in frames}) == count
            assert all(0 <= f.ident < (1 << 29 if f.extended else 1 << 11) for f in frames)
            assert ab.reason(frames, ab.winner(frames))
    assert ab.time_limit(1) == 10 and ab.time_limit(3) < ab.time_limit(2) and ab.time_limit(99) == 4


def test_arbitration_scores_counts_errors_and_goes_bus_off():
    g = ab.Arbitration(seed=1, now=0)
    assert g.phase == ab.READY
    g.update(100)                                       # nothing runs until it's started
    g.command(gc.DROP, now=100)
    assert g.phase == ab.ASK and len(g.frames) == 2 and g.new_lesson
    g.pick(g.win, now=100)                              # at once: full marks
    assert g.correct and g.score == 100 and g.streak == 1 and g.tec == 0
    g.update(100 + ab.RIGHT_S + 0.01)                   # a right answer goes on by itself
    assert g.phase == ab.ASK and g.rounds == 2 and g.new_lesson is None
    g.focus = 0
    g.command(gc.RIGHT)
    assert g.focus == 1
    g.toggle_pause(103)
    g.update(500)                                       # the clock stands still while paused
    g.pick(g.win, 500)
    assert g.phase == ab.ASK
    g.toggle_pause(500)
    g.pick(1 - g.win, 501)                              # wrong
    assert not g.correct and g.tec == ab.ERROR_STEP and g.streak == 0 and g.state == "Error active"
    g.update(10_000)
    assert g.phase == ab.REVEAL                         # stays up until you've read why
    g.act(10_000)
    g.pick(g.win, 10_001)
    assert g.tec == ab.ERROR_STEP - ab.RECOVER_STEP     # a right answer takes a little off again
    g.next(10_002)
    for _ in range(4):
        assert g.phase == ab.ASK
        g.update(g.deadline + 0.01)                     # too late counts as wrong
        assert g.picked is None and not g.correct
        g.next(g.deadline + 1)
    assert g.over and g.state == "Bus off" and g.events[-1] == ("over",)
    g = ab.Arbitration(seed=2)
    g.start(0)
    g.wins = ab.WINS_PER_LEVEL - 1
    g.pick(g.win, 0)
    assert g.level == 2 and ("level", 2) in g.events


# --- controls from CAN -------------------------------------------------------
class FakeSession:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}
        self.latest_std = {}


@pytest.fixture
def profile_signals():
    old = list(signals.PROFILE_SIGNALS)
    steer = Signal("Steering angle", 0xFF20, 0, 1, 1.0, -100.0, "deg", na_check=False)
    lower = Signal("Lower switch", 0xFF20, 1, 1, 1.0, 0.0, "", bit=0, bits=2, enum=((0, "Off"), (1, "On")))
    signals.set_profile_signals([steer, lower])
    yield steer, lower
    signals.set_profile_signals(old)


def frame(steer_deg, lower):
    return bytes([steer_deg + 100, lower, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])


def test_steering_turns_the_piece_and_the_lower_switch_drops_it(profile_signals):
    sess = FakeSession()
    ctl = gc.SignalControls([gc.Binding("Steering angle", gc.TURN, every=15), gc.Binding("Lower switch", gc.DROP)],
                            hz.COLS)
    sess.latest[(0xFF20, 0x27)] = (1.0, frame(0, 1))
    assert ctl.poll(sess) == []                     # first look: just where things start (switch already on)
    sess.latest[(0xFF20, 0x27)] = (1.1, frame(31, 0))
    assert ctl.poll(sess) == [(gc.TURN, 2)]
    sess.latest[(0xFF20, 0x27)] = (1.2, frame(31, 1))
    assert ctl.poll(sess) == [(gc.DROP, 1)]
    sess.latest[(0xFF20, 0x27)] = (1.3, frame(-15, 1))
    assert ctl.poll(sess) == [(gc.TURN, -3)]
    assert gc.SignalControls([gc.Binding("No such signal", gc.DROP)], hz.COLS).poll(sess) == []


def test_bindings_edge_delta_and_position():
    sw = gc.Binding("s", gc.SWAP, below=2)
    assert [sw.step(v, 8) for v in (5, 3, 1, 1, 4, 0)] == [[], [], [(gc.SWAP, 1)], [], [], [(gc.SWAP, 1)]]
    mv = gc.Binding("j", gc.MOVE, every=-10)        # negative: reversed
    assert [mv.step(v, 8) for v in (0, 25)] == [[], [(gc.LEFT, 2)]]
    pos = gc.Binding("j", gc.COLUMN, min=0, max=70)
    assert [pos.step(v, 8) for v in (0, 4, 35, 70, 99)] == [[(gc.COLUMN, 0)], [], [(gc.COLUMN, 4)],
                                                           [(gc.COLUMN, 7)], []]


def test_suggested_bindings_are_saved_per_profile(profile_signals, tmp_path, monkeypatch):
    steer, lower = profile_signals
    s = Settings()
    assert gc.suggest(gc.DROP, lower, 0, 1) == {"signal": "Lower switch", "do": gc.DROP, "above": 0.5}
    assert gc.suggest(gc.TURN, steer, -30, 30) == {"signal": "Steering angle", "do": gc.TURN, "every": 10.0}
    assert "min" not in gc.suggest(gc.COLUMN, steer, None, None)          # needs the range seen first
    gc.save_binding(s, "loader", gc.suggest(gc.TURN, steer, -30, 30), gc.TURN)
    gc.save_binding(s, "loader", gc.suggest(gc.DROP, lower, 0, 1), gc.DROP)
    gc.save_binding(s, "tractor", {"signal": "Lower switch", "do": gc.SWAP}, gc.SWAP)
    assert gc.binding_for(s, "loader", gc.TURN)["every"] == 10.0
    assert gc.binding_for(s, "tractor", gc.TURN) is None
    gc.save_binding(s, "loader", None, gc.TURN)
    assert gc.binding_for(s, "loader", gc.TURN) is None
    ctl = gc.SignalControls.for_profile(s, "loader", hz.COLS)
    assert [b.signal for b in ctl.bindings] == ["Lower switch"]


# --- the screens ---------------------------------------------------------------
pygame = pytest.importorskip("pygame")


@pytest.fixture
def app():
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    ctx = DeviceContext(Settings())
    a = DeviceApp(ctx, (1024, 600), encoder=False)
    a.step([])
    yield a
    ctx.shutdown()
    pygame.quit()


def press(app, k):
    app.step([pygame.event.Event(pygame.KEYDOWN, key=k, mod=0), pygame.event.Event(pygame.KEYUP, key=k, mod=0)])


def test_games_stay_hidden_until_about_is_opened_seven_times(app):
    from canopener.device import modes
    assert "games" not in [m.id for m in modes.shown(app.ctx)]
    app.open_mode("settings")
    about = next(r for r in app.top.rows.refresh() if r.label == "About this device")
    for _ in range(6):
        about.select()
        app.pop()
    assert not app.ctx.settings.get("device.games") and "1 more" in app.ctx.current_toast()
    about.select()
    app.pop()
    assert app.ctx.settings.get("device.games")
    shown = [m.id for m in modes.shown(app.ctx)]
    assert shown[-1] == "games" and shown[:4] == ["can", "meter", "log", "settings"]   # games tile last
    next(r for r in app.top.rows.refresh() if r.label == "Software").select()
    hide = next(r for r in app.top.rows.refresh() if r.label == "Show games")
    hide.select()
    assert "games" not in [m.id for m in modes.shown(app.ctx)]


def tap(app, pos):
    app.step([pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos),
              pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=pos)])


def hit_point(app, match):
    """The centre of the newest tap region whose handler passes ``match``."""
    rect = next(r for r, fn in reversed(app.ui.hits) if match(fn))
    return rect.center


def test_about_has_a_show_games_button(app):
    from canopener.device import modes
    from canopener.device.screens.base import InfoDialog
    app.open_mode("settings")
    next(r for r in app.top.rows.refresh() if r.label == "About this device").select()
    app.step([])
    assert isinstance(app.top, InfoDialog)
    toggle = app.top.corner[1]
    assert app.top.corner[0]() == "Show games menu"
    tap(app, hit_point(app, lambda fn: fn == toggle))
    assert app.ctx.settings.get("device.games") and "games" in [m.id for m in modes.shown(app.ctx)]
    assert app.top.corner[0]() == "Hide games menu"
    tap(app, hit_point(app, lambda fn: fn == toggle))
    assert not app.ctx.settings.get("device.games")


def test_harness_plays_by_touch(app):
    from canopener.device.screens.games import HarnessScreen
    app.push(HarnessScreen(app))
    app.step([])
    g = app.top.game

    def default(fn, value):
        return getattr(fn, "__defaults__", None) == (value,)
    # held sideways: the board's columns run down the screen, its buttons along the left edge
    col0, col7 = hit_point(app, lambda fn: default(fn, 0)), hit_point(app, lambda fn: default(fn, hz.COLS - 1))
    assert col0[1] < col7[1]
    drop = hit_point(app, lambda fn: default(fn, gc.DROP))
    assert drop[0] < col0[0]
    tap(app, col0)                                  # tap a column: the piece goes there
    assert g.piece.x == 0
    mask = g.piece.mask
    tap(app, col0)                                  # ... tap it again: it turns
    assert g.piece.mask == hz.rotate_cw(mask)
    tap(app, hit_point(app, lambda fn: default(fn, gc.SWAP)))
    assert g.hold == hz.rotate_cw(mask) and g.held
    x = g.piece.x                                   # the swapped-in piece starts in the middle
    tap(app, drop)
    assert g.grid[hz.ROWS - 1][x] or g.lit
    g.toggle_pause(0)
    app.step([])
    tap(app, col7)                                  # paused: a tap anywhere on the board resumes
    assert not g.paused


def test_both_games_have_move_buttons(app):
    from canopener.device.screens.games import CargoScreen, HarnessScreen
    for screen in (HarnessScreen, CargoScreen):
        app.push(screen(app))
        app.step([])
        g = app.top.game
        x = g.piece.x

        def default(fn, value):
            return getattr(fn, "__defaults__", None) == (value,)
        left, right = hit_point(app, lambda fn: default(fn, gc.LEFT)), hit_point(app, lambda fn: default(fn, gc.RIGHT))
        assert left[1] < right[1]                   # held sideways, the board's left is up the screen
        tap(app, left)
        assert g.piece.x == x - 1
        tap(app, right)
        tap(app, right)
        assert g.piece.x == x + 1
        app.top.game.over = True
        app.step([])
        app.pop()


def test_cargo_plays_by_touch_and_keeps_its_best(app):
    from canopener.device.screens.games import CARGO_BEST, CargoScreen
    app.ctx.settings.set("device.games", True)
    app.open_mode("games")
    press(app, pygame.K_F2)                         # Cargo
    scr = app.top
    assert isinstance(scr, CargoScreen)
    g = scr.game
    g.piece = cg.Crate("bar", cg.SHAPES["bar"], x=3, y=0)

    def default(fn, value):
        return getattr(fn, "__defaults__", None) == (value,)
    app.step([])
    tap(app, hit_point(app, lambda fn: default(fn, 4)))      # tap the crate: it turns upright
    assert g.piece.width == 1
    tap(app, hit_point(app, lambda fn: default(fn, 0)))      # tap a column: it goes there
    assert g.piece.x == 0
    press(app, pygame.K_LEFT)                       # d-pad left (now down): drop
    assert g.grid[cg.ROWS - 1][0] == "bar"
    g.score, g.over = 1234, True
    app.step([])
    assert app.ctx.settings.get(CARGO_BEST) == 1234


def test_harness_is_played_sideways(app):
    from canopener.device.screens.games import HarnessScreen
    app.ctx.settings.set("device.games", True)
    app.open_mode("games")
    press(app, pygame.K_F1)                         # Play
    scr = app.top
    assert isinstance(scr, HarnessScreen)
    g = scr.game
    x, mask = g.piece.x, g.piece.mask
    press(app, pygame.K_UP)                         # d-pad up, now pointing left
    assert g.piece.x == x - 1
    press(app, pygame.K_DOWN)
    assert g.piece.x == x
    press(app, pygame.K_PAGEDOWN)                   # encoder clockwise
    assert g.piece.mask == hz.rotate_cw(mask)
    press(app, pygame.K_RETURN)                     # encoder push: swap
    assert g.hold == hz.rotate_cw(mask)
    press(app, pygame.K_LEFT)                       # d-pad left, now pointing down: drop
    assert g.grid[hz.ROWS - 1][x] or g.lit
    press(app, pygame.K_ESCAPE)                     # Back pauses ...
    assert g.paused and app.top is scr
    for _ in range(3):
        app.step([])
    press(app, pygame.K_ESCAPE)                     # ... and Back again leaves
    assert app.top is not scr


@pytest.mark.parametrize("size", [(800, 480), (1024, 600), (1280, 720)])
def test_games_draw_at_panel_sizes(size):
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.screens.games import CargoScreen, HarnessScreen
    ctx = DeviceContext(Settings())
    try:
        a = DeviceApp(ctx, size, encoder=False)
        a.push(HarnessScreen(a))
        bottom_row(a.top.game, gap=2)
        for _ in range(3):
            a.step([])
        a.top.game.over = True
        a.step([])
        a.pop()
        a.push(CargoScreen(a))
        a.top.game.grid[cg.ROWS - 1] = ["pair"] * cg.COLS
        for _ in range(3):
            a.step([])
        a.top.game.toggle_pause(0)
        a.step([])
    finally:
        ctx.shutdown()
        pygame.quit()


def test_the_games_menu_opens_all_four_and_the_can_controls(app):
    from canopener.device.screens import games
    from canopener.device.screens.games_arbitration import ArbitrationScreen
    from canopener.device.screens.games_loom import LoomScreen
    app.ctx.settings.set("device.games", True)
    for key, screen in ((pygame.K_F1, games.HarnessScreen), (pygame.K_F2, games.CargoScreen),
                        (pygame.K_F3, LoomScreen), (pygame.K_F4, ArbitrationScreen)):
        app.open_mode("games")
        press(app, key)
        assert isinstance(app.top, screen)
        app.step([])
    app.open_mode("games")
    next(r for r in app.top.rows.refresh() if r.label == "Controls from CAN").select()
    assert isinstance(app.top, games.GameControlsScreen)
    app.step([])
    assert [r.label for r in app.top.rows.rows if r.selectable] == [
        gc.ACTION_LABELS[a] for a in gc.ACTIONS + gc.DOOM_ACTIONS]


def test_a_falling_game_shows_what_happened_and_explains_itself(app):
    from canopener.device.screens.base import InfoDialog
    from canopener.device.screens.games import BEST, HarnessScreen
    app.push(HarnessScreen(app))
    app.step([])
    scr, g = app.top, app.top.game
    bottom_row(g, gap=3)
    g.piece = hz.Piece(EW, x=3, y=0)
    press(app, pygame.K_LEFT)                           # drop: closes the circuit
    assert scr.fx.popups[0][0] == "+160" and scr.fx.sparks and not g.events
    press(app, pygame.K_F3)                             # How to play: pauses and explains
    assert isinstance(app.top, InfoDialog) and g.paused
    app.step([])
    app.pop()
    g.over = True
    app.step([])
    assert scr._new_best and app.ctx.settings.get(BEST) == 160
    press(app, pygame.K_RETURN)                         # push: a new game
    assert scr.game is not g and not scr._new_best


def test_loom_plays_by_buttons_and_touch_and_opens_the_next_level(app):
    from canopener.device.screens.games import LOOM_LEVEL
    from canopener.device.screens.games_loom import LoomScreen
    app.push(LoomScreen(app))
    app.step([])
    scr, g = app.top, app.top.game
    assert g.level == 1 and g.cursor == g.source
    x, y = g.cursor
    press(app, pygame.K_RIGHT)
    press(app, pygame.K_DOWN)
    assert g.cursor == ((x + 1) % g.cols, (y + 1) % g.rows)
    cx, cy = g.cursor
    mask = g.grid[cy][cx]
    press(app, pygame.K_PAGEDOWN)                       # encoder: turn the tile under the cursor
    assert g.grid[cy][cx] == hz.rotate_cw(mask)
    press(app, pygame.K_PAGEUP)
    assert g.grid[cy][cx] == mask
    mask = g.grid[0][0]
    tap(app, hit_point(app, lambda fn: getattr(fn, "__defaults__", None) == (0, 0)))    # tap a tile: it turns
    assert g.grid[0][0] == hz.rotate_cw(mask) and g.cursor == (0, 0)
    last = (g.cols - 1, g.rows - 1)
    if g.grid[last[1]][last[0]] == g.solution[last[1]][last[0]]:
        g.turn(1, at=last)                              # so there's a last tile left to turn
    k = solve(g, leave=last)
    g.cursor = last
    for _ in range(k):
        press(app, pygame.K_RETURN)                     # push: a quarter turn
    assert g.solved
    app.step([])
    assert app.ctx.settings.get(LOOM_LEVEL) == 2
    press(app, pygame.K_RETURN)                         # push again: the next level
    assert scr.game.level == 2 and not scr.game.solved
    press(app, pygame.K_F2)                             # Level -
    assert scr.game.level == 1
    press(app, pygame.K_ESCAPE)
    assert app.top is not scr


def test_arbitration_plays_by_soft_keys_touch_and_the_encoder(app):
    from canopener.device.screens.games import ARBITRATION_BEST
    from canopener.device.screens.games_arbitration import ArbitrationScreen
    app.push(ArbitrationScreen(app))
    app.step([])
    scr, g = app.top, app.top.game
    press(app, pygame.K_RETURN)                         # start
    assert g.phase == ab.ASK
    app.step([])
    press(app, (pygame.K_F1, pygame.K_F2)[g.win])       # the winner's soft key
    assert g.correct and g.score > 0
    app.step([])
    press(app, pygame.K_RETURN)                         # on to the next round
    assert g.phase == ab.ASK and g.rounds == 2
    app.step([])
    tap(app, hit_point(app, lambda fn: getattr(fn, "__defaults__", None) == (1 - g.win,)))   # tap the loser
    assert g.phase == ab.REVEAL and not g.correct and g.tec == ab.ERROR_STEP
    app.step([])
    press(app, pygame.K_RETURN)
    g.focus = 0
    press(app, pygame.K_PAGEDOWN)                       # encoder: move the marker
    assert g.focus == 1
    press(app, pygame.K_ESCAPE)                         # Back pauses a round ...
    assert g.paused and app.top is scr
    app.step([])
    press(app, pygame.K_RETURN)
    assert not g.paused
    g.tec = ab.BUS_OFF - ab.ERROR_STEP
    g.focus = 1 - g.win
    press(app, pygame.K_RETURN)                         # one mistake too many
    press(app, pygame.K_RETURN)
    assert g.over
    app.step([])
    assert app.ctx.settings.get(ARBITRATION_BEST) == g.score
    press(app, pygame.K_ESCAPE)                         # ... and leaves once it's over
    assert app.top is not scr


@pytest.mark.parametrize("size", [(800, 480), (1024, 600), (1280, 720)])
def test_loom_and_arbitration_draw_at_panel_sizes(size):
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.screens.games_arbitration import ArbitrationScreen
    from canopener.device.screens.games_loom import LoomScreen
    ctx = DeviceContext(Settings())
    try:
        a = DeviceApp(ctx, size, encoder=False)
        a.push(LoomScreen(a, len(lm.SIZES)))
        a.step([])
        a.top.game.turn(1, 0.0)
        a.step([])
        solve(a.top.game)
        a.step([])
        a.pop()
        a.push(ArbitrationScreen(a))
        g = a.top.game
        a.step([])
        g.start(0.0)
        g.wins = 40                                     # the last level: four frames, 29-bit ones among them
        g.next(0.0) if g.phase == ab.REVEAL else g._ask(0.0)
        g.deadline = 1e12
        a.step([])
        g.pick(1 if g.win == 0 else 0, 1.0)
        a.step([])
        g.tec = ab.BUS_OFF
        g.next(2.0)
        a.step([])
    finally:
        ctx.shutdown()
        pygame.quit()


# --- Doom: an optional download ------------------------------------------------
@pytest.fixture
def fake_doom(tmp_path, monkeypatch):
    """A stand-in engine (a script that records how it was started) and a WAD, in 'system' folders."""
    bin_dir, wads = tmp_path / "bin", tmp_path / "wads"
    bin_dir.mkdir()
    wads.mkdir()
    exe = bin_dir / "chocolate-doom"
    exe.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/started\n')
    exe.chmod(0o755)
    (wads / "freedoom1.wad").write_bytes(b"IWAD")
    monkeypatch.setattr(doom, "ENGINES", ("chocolate-doom",))

    def put_in_place():
        monkeypatch.setattr(doom, "ENGINE_DIRS", (str(bin_dir),))
        monkeypatch.setattr(doom, "SYSTEM_WAD_DIRS", (str(wads),))
    put_in_place.started = tmp_path / "started"
    return put_in_place


def test_doom_is_found_configured_and_prefers_your_own_wad(fake_doom):
    assert not doom.installed() and doom.command() is None and doom.wad_name() == ""
    fake_doom()
    assert doom.installed() and doom.wad_name() == "Freedoom"
    argv = doom.command()
    main, extra = argv[argv.index("-config") + 1], argv[argv.index("-extraconfig") + 1]
    assert argv[0].endswith("chocolate-doom") and argv[argv.index("-iwad") + 1].endswith("freedoom1.wad")
    assert os.path.dirname(main) == doom.data_dir() and argv[-1] == doom.data_dir() + os.sep
    keys = dict(line.split() for line in open(main).read().splitlines() + open(extra).read().splitlines())
    assert keys["key_fire"] == "28" and keys["key_use"] == "59" and keys["key_menu_activate"] == "63"
    assert keys["key_menu_help"] == "0" and keys["fullscreen"] == "1"       # the soft keys are Doom's now
    with open(main, "a") as f:
        f.write("sfx_volume 3\n")
    doom.command()
    assert "sfx_volume 3" in open(main).read()          # a setup that's there is left alone
    open(os.path.join(doom.data_dir(), "DOOM.WAD"), "wb").close()
    assert doom.wad_name() == "Doom" and doom.command()[2].endswith("DOOM.WAD")     # your own copy comes first


def test_doom_installs_with_fixed_commands_only(fake_doom, monkeypatch):
    assert doom.install() == (False, "This system has no apt-get: install chocolate-doom and a WAD by hand.")
    monkeypatch.setattr(doom, "can_install", lambda: True)
    ran = []

    def runner(steps, timeout, optional):
        ran.append((steps, timeout, optional))
        fake_doom()                                     # the packages land
        return True, "ok"
    assert doom.install(runner) == (True, "ok")
    assert ran == [([["apt-get", "update"],
                     ["apt-get", "install", "-y", "--no-install-recommends", "chocolate-doom", "freedoom"]],
                    doom.INSTALL_TIMEOUT_S, {0})]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sudoers = " ".join(open(os.path.join(here, "deploy", "raspberry-pi", "canopener.sudoers")).read().split())
    for step in doom.install_steps():                   # the sudoers file allows exactly these
        assert "/usr/bin/" + " ".join(step) in sudoers
    assert doom.install(lambda steps, timeout, optional: (False, "no network")) == (False, "no network")


def test_turning_games_on_asks_once_whether_to_install_doom(app, fake_doom, monkeypatch):
    from canopener.device import modes
    from canopener.device.screens import games
    from canopener.device.screens.base import ConfirmDialog, InfoDialog
    monkeypatch.setattr(doom, "can_install", lambda: True)
    app.open_mode("settings")
    settings = app.top
    settings._toggle_games()                            # games on: the question
    assert isinstance(app.top, ConfirmDialog) and app.top.title == "Install Doom?" and app.top.focus == 0
    app.step([])
    press(app, pygame.K_RETURN)                         # No (the default)
    assert not doom.installed() and "games" in [m.id for m in modes.shown(app.ctx)]
    settings._toggle_games()
    settings._toggle_games()                            # off and on again: not asked twice
    assert not isinstance(app.top, ConfirmDialog)
    app.open_mode("games")
    row = next(r for r in app.top.rows.refresh() if r.label == "Doom")
    assert row.value_text() == "not installed"
    row.select()                                        # ... but the Doom row asks whenever you like
    assert isinstance(app.top, ConfirmDialog)
    installs = []

    def install():
        installs.append(1)
        fake_doom()
        return True, "ok"
    monkeypatch.setattr(doom, "install", install)
    app.top._answer(True)
    app.ctx.wait_idle()
    assert installs == [1] and "Doom installed (Freedoom)" in app.ctx.current_toast()
    row = next(r for r in app.top.rows.refresh() if r.label == "Doom")
    assert row.value_text() == "Freedoom"
    size = app.screen.get_size()
    row.select()                                        # play: the screen is handed over and taken back
    assert fake_doom.started.read_text().startswith("-iwad ")
    assert isinstance(app.top, games.GamesScreen) and app.screen.get_size() == size
    app.step([])                                        # and the handheld draws again
    monkeypatch.setattr(doom, "install", lambda: (False, "no network"))
    monkeypatch.setattr(doom, "installed", lambda: False)
    games.install_doom(app)
    app.ctx.wait_idle()
    assert isinstance(app.top, InfoDialog) and app.top.lines[0] == "no network"


# --- Doom: controls from CAN -----------------------------------------------------
class FakeOutput:
    def __init__(self):
        self.events, self.closed = [], False

    def key(self, code, down):
        self.events.append(("key", code, down))

    def turn(self, counts):
        self.events.append(("turn", counts))

    def close(self):
        self.closed = True


def test_doom_is_driven_by_the_machines_signals(profile_signals):
    dc = doom_controls
    sess, out, clock = FakeSession(), FakeOutput(), [100.0]
    ctl = gc.SignalControls([gc.Binding("Steering angle", gc.TURN, every=10), gc.Binding("Lower switch", gc.FIRE),
                             gc.Binding("Steering angle", gc.FORWARD, above=50),
                             gc.Binding("Steering angle", gc.COLUMN, min=-100, max=100)], 1)
    assert dc.wanted(ctl.bindings) and not dc.wanted(ctl.bindings[3:])      # "follows value" means nothing to Doom
    bridge = dc.DoomControls(ctl, sess, out, turn_counts=40, clock=lambda: clock[0])

    def look(steer, lower):
        out.events.clear()
        sess.latest[(0xFF20, 0x27)] = (clock[0], frame(steer, lower))
        bridge.tick()
        return out.events
    assert look(0, 1) == [("key", dc.KEY_ENTER, True)]          # the switch is on: fire is held from the start
    assert look(5, 1) == [("turn", 20)]                         # half a step of steering: half the counts
    assert look(-5, 1) == [("turn", -40)]
    assert look(-5, 0) == [("key", dc.KEY_ENTER, False)]
    assert look(60, 0) == [("key", dc.KEY_UP, True), ("turn", 260)]     # past 50: walking, and turning
    assert look(60, 0) == []                                    # held: nothing new to say
    out.events.clear()
    clock[0] += dc.STALE_S + 0.1                                # the bus goes quiet: the key is let go
    bridge.tick()
    assert out.events == [("key", dc.KEY_UP, False)]
    assert look(-60, 0) == []                                   # back again: no jump from where it was
    assert look(60, 1) == [("key", dc.KEY_ENTER, True), ("key", dc.KEY_UP, True), ("turn", 480)]
    out.events.clear()
    bridge.start()
    bridge.stop()                                               # Doom ended: everything released, devices gone
    assert out.events[-2:] == [("key", dc.KEY_ENTER, False), ("key", dc.KEY_UP, False)] and out.closed


def test_doom_bindings_are_suggested_as_holds_and_only_made_when_wanted(profile_signals):
    steer, lower = profile_signals
    assert gc.suggest(gc.FIRE, lower, 0, 1) == {"signal": "Lower switch", "do": gc.FIRE, "above": 0.5}
    assert gc.suggest(gc.FORWARD, steer, -80, 80)["above"] == 20        # a lever that goes both ways
    assert gc.suggest(gc.BACK, steer, -80, 80)["below"] == -20
    assert gc.suggest(gc.FORWARD, steer, 0, 100)["above"] == 25         # a pedal
    assert gc.Binding("s", gc.BACK, below=-20).held(-30) and not gc.Binding("s", gc.BACK, below=-20).held(0)
    s = Settings()
    made = []
    assert doom_controls.DoomControls.for_profile(s, "loader", FakeSession(), made) is None     # nothing bound
    gc.save_binding(s, "loader", {"signal": "Lower switch", "do": gc.DROP}, gc.DROP)
    assert doom_controls.DoomControls.for_profile(s, "loader", FakeSession(), made) is None     # nothing of Doom's
    gc.save_binding(s, "loader", gc.suggest(gc.FIRE, lower, 0, 1), gc.FIRE)
    assert doom_controls.DoomControls.for_profile(s, "loader", None, made) is None              # not connected
    s.set(doom_controls.TURN_SETTING, -25)
    bridge = doom_controls.DoomControls.for_profile(s, "loader", FakeSession(), made)
    assert bridge.turn_counts == -25 and [b.do for b in bridge.controls.bindings] == [gc.DROP, gc.FIRE]


def test_the_virtual_input_devices_are_set_up_and_written_the_kernels_way(tmp_path):
    dc = doom_controls
    path = tmp_path / "uinput"
    path.write_bytes(b"")
    calls = []
    dev = dc.VirtualDevice("test keys", keys=(dc.KEY_UP,), rels=(dc.REL_X,), path=str(path),
                           ioctl=lambda fd, req, arg=0: calls.append((req, arg)))
    assert calls == [(dc.UI_SET_EVBIT, dc.EV_SYN), (dc.UI_SET_EVBIT, dc.EV_KEY), (dc.UI_SET_KEYBIT, dc.KEY_UP),
                     (dc.UI_SET_EVBIT, dc.EV_REL), (dc.UI_SET_RELBIT, dc.REL_X), (dc.UI_DEV_CREATE, 0)]
    dev.emit(dc.EV_KEY, dc.KEY_UP, 1)
    dev.close()
    dev.close()                                                 # twice is fine
    assert calls[-1] == (dc.UI_DEV_DESTROY, 0)
    data = path.read_bytes()
    assert dc.USER_DEV.size == 1116 and data[:9] == b"test keys" and len(data) == 1116 + 2 * dc.EVENT.size
    events = [dc.EVENT.unpack_from(data, 1116 + i * dc.EVENT.size)[2:] for i in range(2)]
    assert events == [(dc.EV_KEY, dc.KEY_UP, 1), (dc.EV_SYN, dc.SYN_REPORT, 0)]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rule = open(os.path.join(here, "deploy", "raspberry-pi", "72-canopener-uinput.rules")).read()
    assert 'KERNEL=="uinput"' in rule and 'GROUP="input"' in rule
    assert "72-canopener-uinput.rules" in open(os.path.join(here, "deploy", "raspberry-pi", "install.sh")).read()
    assert set(dc.KEYS.values()) == {28, 59, 103, 108}          # the keys Doom's setup listens for
    assert (doom.MAIN_CONFIG["key_fire"], doom.MAIN_CONFIG["key_use"]) == (28, 59)


def test_playing_doom_runs_the_can_controls_alongside(app, fake_doom, monkeypatch):
    from canopener.device.screens import games
    fake_doom()
    log = []

    class Bridge:
        def start(self):
            log.append("start")

        def stop(self):
            log.append("stop" if fake_doom.started.exists() else "stopped early")
    monkeypatch.setattr(doom_controls.DoomControls, "for_profile", classmethod(lambda cls, *a: Bridge()))
    app.push(games.GamesScreen(app))
    games.play_doom(app)
    assert log == ["start", "stop"]                             # there for the whole of Doom, gone after

    def no_access(cls, *a):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(doom_controls.DoomControls, "for_profile", classmethod(no_access))
    fake_doom.started.unlink()
    games.play_doom(app)                                        # Doom still plays; the reason is shown after
    assert fake_doom.started.exists() and "install.sh --device" in app.ctx.current_toast()
