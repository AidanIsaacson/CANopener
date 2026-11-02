"""The handheld's hidden Games: Harness's rules, game controls from CAN signals, and the unlock."""

import os
import threading

import pytest

from canopener.core.settings import Settings
from canopener.device import game_controls as gc
from canopener.device import harness as hz
from canopener.j1939 import signals
from canopener.j1939.signals import Signal

EW = hz.E | hz.W


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
    assert shown[-1] == "games" and shown[:4] == ["can", "meter", "log", "settings"]   # soft keys unchanged
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
def test_harness_draws_at_panel_sizes(size):
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.screens.games import HarnessScreen
    ctx = DeviceContext(Settings())
    try:
        a = DeviceApp(ctx, size, encoder=False)
        a.push(HarnessScreen(a))
        bottom_row(a.top.game, gap=2)
        for _ in range(3):
            a.step([])
        a.top.game.over = True
        a.step([])
    finally:
        ctx.shutdown()
        pygame.quit()
