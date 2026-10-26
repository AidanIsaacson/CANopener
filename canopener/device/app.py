"""The handheld's full-screen instrument UI (Pygame): ``canopener-device``.

    canopener-device --sim                    # try it on a desktop: simulated machine + meter
    canopener-device --size 1280x720          # the 7" Touch Display 2 in landscape
    canopener-device --fullscreen --rotate 90 # on the Pi: a portrait panel used landscape

The layout is fixed, like an instrument: soft-key labels on the edge with the hardware buttons
(top by default), a status strip on the opposite edge, one screen in between. Screens stack:
Back pops, Home goes to the launcher. See docs/DEVICE.md.
"""

import argparse
import os
import sys
import time

import pygame

from .. import __version__, plugins
from ..core.settings import Settings
from . import input as inp
from . import modes, ui
from .context import DeviceContext
from .screens.base import Screen, SoftKey
from .ui import UI, Theme

TAP_SLOP_MM = 2.5           # a finger moving less than this is a tap, more is a drag


class DeviceApp:
    def __init__(self, ctx: DeviceContext, size: tuple[int, int] = (800, 480), *, rotate: int | None = None,
                 fullscreen: bool = False, encoder: bool = True):
        self.ctx = ctx
        s = ctx.settings
        self.rotate = (s.get("device.rotate") if rotate is None else rotate) % 360
        if self.rotate not in (0, 90, 180, 270):
            self.rotate = 0
        sideways = self.rotate in (90, 270)
        if fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            pygame.mouse.set_visible(False)
        else:
            phys = (size[1], size[0]) if sideways else size
            self.screen = pygame.display.set_mode(phys)
        pw, ph = self.screen.get_size()
        self.size = (ph, pw) if sideways else (pw, ph)
        self.canvas = pygame.Surface(self.size) if self.rotate else self.screen
        self.theme = Theme(self.size, float(s.get("device.diagonal_in") or 7.0))
        self.ui = UI(self.canvas, self.theme)
        self.keymap = inp.KeyMap(s.get("device.keys"), s.get("device.button5"))
        self.press = inp.PressTracker(float(s.get("device.long_press_s") or 0.7))
        self.encoder = None
        if encoder and s.get("device.encoder") not in ("none", "", None) and sys.platform.startswith("linux"):
            self.encoder = inp.EvdevReader(s.get("device.encoder"))
        self.ladder = None
        if s.get("device.ladder"):
            levels = list(s.get("device.ladder_levels"))
            acts = (list(inp.SOFT[:4]) + [inp.BUTTON5])[:len(levels)]
            self.ladder = inp.LadderButtons(levels[:len(acts)], acts, inp.iio_volts_reader(s.get("device.ladder")))
        self.clock = pygame.time.Clock()
        self.running = True
        self.fullscreen = fullscreen
        self.switch_to: str | None = None   # another front-end to start once this one has closed
        self._touch = None              # (start pos, tap fn, scroller, moved, last pos)
        self.content = pygame.Rect(0, 0, *self.size)
        if not modes.modes():
            modes.register_builtin()
        for p in plugins.loaded():
            hook = p.hook("register_device")
            if hook:
                try:
                    hook(modes.DeviceApi(self))
                except Exception as e:      # a broken plugin must not stop the instrument
                    ctx.toast(f"Plugin {p.name}: {e}")
        from .screens.home import HomeScreen
        self.stack: list[Screen] = [HomeScreen(self)]
        s.on_change(self._setting_changed)

    def _setting_changed(self, key, value):
        if key in ("device.keys", "device.button5"):
            self.keymap = inp.KeyMap(self.ctx.settings.get("device.keys"), self.ctx.settings.get("device.button5"))
        elif key == "device.diagonal_in":
            self.theme = Theme(self.size, float(value or 7.0))
            self.ui = UI(self.canvas, self.theme)
        elif key == "device.long_press_s":
            self.press.long_press = float(value or 0.7)

    # ------------------------------------------------------------------
    # screens
    # ------------------------------------------------------------------
    @property
    def top(self) -> Screen:
        return self.stack[-1]

    def push(self, screen: Screen):
        if not screen.modal:
            self.top.on_leave()
        self.stack.append(screen)
        screen.on_enter()

    def pop(self, screen: Screen | None = None):
        if len(self.stack) <= 1 or (screen is not None and self.top is not screen):
            return
        old = self.stack.pop()
        old.on_leave()
        if not old.modal:
            self.top.on_enter()

    def home(self):
        while len(self.stack) > 1:
            self.pop()

    def open_mode(self, mode_id: str):
        m = modes.get(mode_id)
        if m is None:
            return
        self.home()
        self.push(m.factory(self))

    def breadcrumb(self) -> str:
        titles = [s.title for s in self.stack[1:] if s.title and not s.modal]
        return " › ".join(titles) if titles else "CANopener"

    # ------------------------------------------------------------------
    # input
    # ------------------------------------------------------------------
    def softkey_slots(self) -> list:
        """What each hardware button does right now: SoftKey or None, one per button."""
        n = 5 if int(self.ctx.settings.get("device.buttons") or 5) >= 5 else 4
        keys = list(self.top.softkeys() or [])
        five_soft = self.keymap.button5 == inp.SOFT[4]
        slots = (keys + [None] * 5)[: (5 if n == 5 and five_soft else 4)]
        if n == 5 and not five_soft:
            b5 = self.keymap.button5
            if b5 == inp.BACK:
                slots.append(SoftKey("Back", lambda: self.action(inp.BACK), enabled=len(self.stack) > 1))
            elif b5 == inp.LOG:
                slots.append(SoftKey("Stop log" if self.ctx.recording else "Log", self.ctx.toggle_log,
                                     active=self.ctx.recording))
            elif b5 == inp.TAG:
                slots.append(SoftKey("Timestamp", self.ctx.tag))
        return slots

    def action(self, action: str, n: int = 1):
        if action == inp.QUIT:
            self.running = False
        elif action == inp.HOME:
            self.home()
        elif action == inp.LOG:
            self.ctx.toggle_log()
        elif action == inp.TAG:
            self.ctx.tag()
        elif action in inp.SOFT:
            slots = self.softkey_slots()
            i = inp.SOFT.index(action)
            key = slots[i] if i < len(slots) else None
            if key is not None and key.enabled and key.fn is not None:
                key.fn()
        elif not self.top.on_action(action, n) and action == inp.BACK:
            self.pop()

    def to_logical(self, pos) -> tuple[int, int]:
        """A point on the physical screen -> the same point in the (rotated) UI."""
        x, y = pos
        pw, ph = self.screen.get_size()
        if self.rotate == 90:
            return y, pw - 1 - x
        if self.rotate == 180:
            return pw - 1 - x, ph - 1 - y
        if self.rotate == 270:
            return ph - 1 - y, x
        return x, y

    def handle_event(self, ev):
        if ev.type == pygame.QUIT:
            self.running = False
        elif ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_q and ev.mod & pygame.KMOD_CTRL:
                self.running = False
                return
            a = self.keymap.action(pygame.key.name(ev.key))
            if a in (inp.ROTATE_CW, inp.ROTATE_CCW):
                self.action(inp.ROTATE, 1 if a == inp.ROTATE_CW else -1)
            elif a is not None:
                a = self.press.down(a)
                if a is not None:
                    self.action(a)
        elif ev.type == pygame.KEYUP:
            a = self.keymap.action(pygame.key.name(ev.key))
            if a is not None:
                a = self.press.up(a)
                if a is not None:
                    self.action(a)
        elif ev.type == pygame.MOUSEWHEEL:
            self.action(inp.ROTATE, -ev.y)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            pos = self.to_logical(ev.pos)
            self._touch = [pos, self.ui.hit_at(pos), self.ui.drag_at(pos), False, pos]
        elif ev.type == pygame.MOUSEMOTION and self._touch is not None:
            pos = self.to_logical(ev.pos)
            start, _, scroller, moved, last = self._touch
            if not moved and abs(pos[1] - start[1]) + abs(pos[0] - start[0]) > TAP_SLOP_MM * self.theme.px_per_mm:
                self._touch[3] = moved = True
            if moved and scroller is not None:
                scroller.scroll_by(last[1] - pos[1])
            self._touch[4] = pos
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1 and self._touch is not None:
            _, fn, _, moved, _ = self._touch
            self._touch = None
            if not moved and fn is not None:
                fn()

    def poll_hardware(self):
        if self.encoder is not None:
            steps = self.encoder.read()
            if steps:
                self.action(inp.ROTATE, steps)
        if self.ladder is not None:
            for kind, a in self.ladder.poll():
                if kind == "down":
                    self.action(self.keymap.button5 if a == inp.BUTTON5 else a)
        a = self.press.poll()
        if a is not None:
            self.action(a)

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------
    def layout(self) -> tuple[pygame.Rect, pygame.Rect, pygame.Rect]:
        w, h = self.size
        bar = self.theme.touch
        status = self.theme.touch
        if self.ctx.settings.get("device.softkeys_edge") == "bottom":
            keys = pygame.Rect(0, h - bar, w, bar)
            stat = pygame.Rect(0, 0, w, status)
            content = pygame.Rect(0, status, w, h - bar - status)
        else:
            keys = pygame.Rect(0, 0, w, bar)
            stat = pygame.Rect(0, h - status, w, status)
            content = pygame.Rect(0, bar, w, h - bar - status)
        return keys, stat, content

    def draw(self):
        u = self.ui
        u.begin(self.canvas)
        self.canvas.fill(ui.COLORS["bg"])
        keys, stat, content = self.layout()
        self.content = content
        # the screens: everything from the top-most full screen up, so a dialog shows over its page
        base = max(i for i, s in enumerate(self.stack) if not s.modal)
        inner = content.inflate(-self.theme.pad * 2, -self.theme.pad)
        for s in self.stack[base:]:
            self.canvas.set_clip(content)
            s.draw(u, inner if not s.modal else content)
        self.canvas.set_clip(None)
        self._draw_softkeys(u, keys)
        self._draw_status(u, stat)
        self._draw_toast(u, content)
        if self.rotate:
            self.screen.blit(pygame.transform.rotate(self.canvas, -self.rotate), (0, 0))

    def _draw_softkeys(self, u: UI, rect):
        t = self.theme
        u.fill(rect, "panel")
        slots = self.softkey_slots()
        n = len(slots)
        gap = t.pad
        w = (rect.w - gap * (n + 1)) // n
        for i, key in enumerate(slots):
            r = pygame.Rect(rect.x + gap + i * (w + gap), rect.y + t.pad // 2, w, rect.h - t.pad)
            if key is None:
                u.fill(r, "bg", t.radius)
                continue
            u.button(r, key.label, (lambda a=inp.SOFT[i]: self.action(a)) if key.fn else None,
                     enabled=key.enabled, active=key.active)

    def _draw_status(self, u: UI, rect):
        t, ctx = self.theme, self.ctx
        u.fill(rect, "panel")
        x = rect.x + t.pad // 2
        bw = round(t.touch * 1.05)
        for icon, fn, on in ((ui.icon_back, lambda: self.action(inp.BACK), len(self.stack) > 1),
                             (ui.icon_home, self.home, len(self.stack) > 1)):
            r = pygame.Rect(x, rect.y + t.pad // 2, bw, rect.h - t.pad)
            u.button(r, "", fn, enabled=on, icon=icon)
            x = r.right + t.pad // 2
        right = rect.right - t.pad
        right = u.text(time.strftime("%H:%M"), (right, rect.centery), "body", "dim", align="right").left - t.pad
        if ctx.recording:
            c = ctx.capture
            label = f"REC {int(c.elapsed()) // 60:02d}:{int(c.elapsed()) % 60:02d}"
            r = u.text(label, (right, rect.centery), "body", "rec", align="right")
            pygame.draw.circle(u.surface, ui.COLORS["rec"], (r.left - t.pad, rect.centery), t.pad * .7)
            right = r.left - t.pad * 2.5
        pw = min(round(t.touch * 3.4), (right - x) // 2)
        srcs = ctx.hub.sources()
        if srcs:
            worst = _worst([s.status.level for s in srcs])
            r = pygame.Rect(right - round(t.touch * 2), rect.y + t.pad, round(t.touch * 2), rect.h - 2 * t.pad)
            u.pill(r, worst, "Meter")
            u.hit(r, lambda: self.open_mode("meter"))
            right = r.left - t.pad
        if ctx.connection is not None or ctx.finding or ctx.adapter.level in ("bad", "wait"):
            st = ctx.bus if ctx.adapter.level not in ("bad",) or ctx.connection is not None else ctx.adapter
            r = pygame.Rect(right - pw, rect.y + t.pad, pw, rect.h - 2 * t.pad)
            u.pill(r, st.level, st.text.replace("Bus: ", "CAN: "))
            u.hit(r, lambda: self.open_mode("can"))
            right = r.left - t.pad
        title = f"{ctx.busy}…" if ctx.busy else self.breadcrumb()
        u.text(title, (x + t.pad, rect.centery), "body", "accent" if ctx.busy else "text", max_w=right - x - t.pad)

    def _draw_toast(self, u: UI, content):
        msg = self.ctx.current_toast()
        if not msg:
            return
        t = self.theme
        lines = u.wrap(msg, "body", content.w * .8)[:3]
        lh = t.font("body").get_linesize()
        r = pygame.Rect(0, 0, content.w * .86, lh * len(lines) + t.pad * 2)
        r.midbottom = (content.centerx, content.bottom - t.pad)
        u.fill(r, (0, 0, 0), t.radius)
        u.fill(r, "accent", t.radius, 2)
        for i, line in enumerate(lines):
            u.text(line, (r.centerx, r.y + t.pad + lh * i + lh // 2), "body", "text", align="center")

    # ------------------------------------------------------------------
    def step(self, events=None):
        """One frame: input, state, drawing. ``events`` for tests (default: Pygame's queue)."""
        for ev in (pygame.event.get() if events is None else events):
            self.handle_event(ev)
        self.poll_hardware()
        self.ctx.tick()
        self.draw()
        pygame.display.flip()

    def run(self):
        fps = int(self.ctx.settings.get("device.fps") or 30)
        while self.running:
            self.step()
            self.clock.tick(fps)

    def screenshot(self, path: str):
        pygame.image.save(self.screen, path)


def _worst(levels: list[str]) -> str:
    for lvl in ("bad", "wait", "idle", "ok", "sim"):
        if lvl in levels:
            return lvl
    return "off"


def _size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canopener-device", description="CANopener handheld UI (Pygame)")
    ap.add_argument("--sim", action="store_true", help="connect to the simulated machine and switch on the "
                    "simulated meter (try it without hardware)")
    ap.add_argument("--obd-sim", action="store_true", help="connect to the simulated OBD-II car")
    ap.add_argument("--size", type=_size, help="window size, e.g. 1280x720 (default: the saved one, 800x480)")
    ap.add_argument("--fullscreen", action="store_true", help="fill the display, no mouse pointer (on the Pi)")
    ap.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), help="rotate the picture and touch "
                    "(default: the saved setting)")
    ap.add_argument("--diagonal", type=float, help="screen diagonal in inches (sizes the touch targets)")
    ap.add_argument("--screenshot", metavar="PNG", help="draw a few frames, save a screenshot and exit")
    ap.add_argument("--version", action="version", version=f"canopener-device {__version__}")
    a = ap.parse_args(argv)

    settings = Settings()
    if a.diagonal:
        settings.set("device.diagonal_in", a.diagonal)
    if a.sim:
        settings.set("meters.sim", True)
    if a.screenshot:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    try:
        size = a.size or _size(settings.get("device.window") or "800x480")
    except ValueError:
        size = (800, 480)
    pygame.display.init()           # not pygame.init(): no mixer (sound cues go through core.sound)
    pygame.font.init()
    pygame.display.set_caption(f"CANopener {__version__}")
    ctx = DeviceContext(settings)
    app = DeviceApp(ctx, size, rotate=a.rotate, fullscreen=a.fullscreen, encoder=not a.screenshot)
    if a.sim or a.obd_sim:
        ctx.connect("obd-sim" if a.obd_sim else "sim")
    elif settings.get("can.connect_on_start"):
        ctx.connect()
    if ctx.pending_captures():
        ctx.toast("An unsaved log was found: open Data log to save it")
    try:
        if a.screenshot:
            for _ in range(10):
                app.step()
                time.sleep(0.05)
            app.screenshot(a.screenshot)
        else:
            app.run()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.shutdown()
        pygame.quit()
    if app.switch_to:                       # System > Desktop app instead
        from .. import frontends
        frontends.exec_ui(app.switch_to, fullscreen=app.fullscreen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
