"""Drawing for the handheld: sizes, colours, text, buttons, lists, icons.

Immediate mode: each frame a screen draws itself and registers what can be tapped (``UI.hit``)
and dragged (``UI.drag``) as it goes, so the touch areas are always exactly what's on screen.
Sizes come from the panel's real pixel density: a touch target is 10 mm whatever the screen
(DESIGN.md: gloved hands), so the same layout works on an 800x480 7", a 1280x720 7" and a
1280x800 10".
"""

import math

import pygame

COLORS = {
    "bg": (16, 19, 24), "panel": (30, 35, 43), "panel2": (42, 49, 60), "line": (64, 72, 86),
    "text": (232, 236, 241), "dim": (140, 150, 165), "faint": (90, 98, 112), "accent": (64, 156, 255),
    "focus": (255, 196, 0), "rec": (240, 62, 62), "on": (46, 160, 90), "warn": (240, 160, 40),
    "ok": (47, 182, 96), "wait": (240, 170, 30), "bad": (230, 60, 60), "off": (110, 118, 130),
    "sim": (70, 140, 240), "idle": (110, 100, 220), "black": (0, 0, 0), "white": (255, 255, 255),
}
LEVEL = {"ok": "ok", "wait": "wait", "bad": "bad", "off": "off", "sim": "sim", "idle": "idle"}


class Theme:
    def __init__(self, size: tuple[int, int], diagonal_in: float = 7.0):
        w, h = size
        self.size = size
        self.px_per_mm = math.hypot(w, h) / (max(diagonal_in, 3.0) * 25.4)
        self.touch = max(40, round(10 * self.px_per_mm))        # 10 mm: the smallest touch target
        self.pad = max(4, round(self.touch * 0.16))
        self.radius = max(4, round(self.touch * 0.14))
        self.row = self.touch
        self.font_px = {
            "small": round(self.touch * 0.36), "body": round(self.touch * 0.46),
            "big": round(self.touch * 0.62), "title": round(self.touch * 0.8),
            "huge": round(self.touch * 1.3),
        }
        self._fonts: dict = {}

    def font(self, name: str, px: int | None = None) -> pygame.font.Font:
        px = px or self.font_px.get(name, self.font_px["body"])
        f = self._fonts.get(px)
        if f is None:
            f = self._fonts[px] = pygame.font.Font(None, px)
        return f


class UI:
    """One frame's drawing context: the surface, the theme, and the tap / drag areas."""

    def __init__(self, surface: pygame.Surface, theme: Theme):
        self.surface = surface
        self.theme = theme
        self.hits: list[tuple[pygame.Rect, object]] = []
        self.drags: list[tuple[pygame.Rect, object]] = []
        self._cache: dict = {}

    def begin(self, surface: pygame.Surface | None = None):
        if surface is not None:
            self.surface = surface
        self.hits.clear()
        self.drags.clear()
        if len(self._cache) > 3000:
            self._cache.clear()

    # --- registration ---
    def hit(self, rect, fn):
        if fn is not None:
            self.hits.append((pygame.Rect(rect), fn))

    def drag(self, rect, scroller):
        self.drags.append((pygame.Rect(rect), scroller))

    def hit_at(self, pos):
        for rect, fn in reversed(self.hits):
            if rect.collidepoint(pos):
                return fn
        return None

    def drag_at(self, pos):
        for rect, s in reversed(self.drags):
            if rect.collidepoint(pos):
                return s
        return None

    # --- primitives ---
    def color(self, c):
        return COLORS.get(c, c) if isinstance(c, str) else c

    def fill(self, rect, color, radius: int = 0, width: int = 0):
        pygame.draw.rect(self.surface, self.color(color), rect, width, border_radius=radius)

    def render(self, text: str, font: str = "body", color="text", px: int | None = None) -> pygame.Surface:
        key = (text, font, px, self.color(color))
        s = self._cache.get(key)
        if s is None:
            s = self._cache[key] = self.theme.font(font, px).render(text, True, self.color(color))
        return s

    def fit(self, text: str, font: str, max_w: int, px: int | None = None) -> str:
        f = self.theme.font(font, px)
        if max_w <= 0 or f.size(text)[0] <= max_w:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if f.size(text[:mid] + "…")[0] <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    def text(self, text: str, pos, font: str = "body", color="text", align: str = "left",
             max_w: int = 0, valign: str = "center", px: int | None = None) -> pygame.Rect:
        """Draw one line. ``pos`` is the anchor: left / center / right edge at ``align``, the
        vertical centre (valign center) or top."""
        text = self.fit(str(text), font, max_w, px) if max_w else str(text)
        s = self.render(text, font, color, px)
        x, y = pos
        r = s.get_rect()
        if align == "center":
            r.centerx = x
        elif align == "right":
            r.right = x
        else:
            r.left = x
        if valign == "center":
            r.centery = y
        else:
            r.top = y
        self.surface.blit(s, r)
        return r

    def wrap(self, text: str, font: str, max_w: int) -> list[str]:
        f = self.theme.font(font)
        out = []
        for para in str(text).split("\n"):
            line = ""
            for word in para.split(" "):
                trial = f"{line} {word}".strip()
                if f.size(trial)[0] <= max_w or not line:
                    line = trial
                else:
                    out.append(line)
                    line = word
            out.append(line)
        return out

    def paragraph(self, text: str, rect, font: str = "body", color="text") -> int:
        """Wrapped text from the top of ``rect``; returns the height used."""
        rect = pygame.Rect(rect)
        lh = self.theme.font(font).get_linesize()
        y = rect.y
        for line in self.wrap(text, font, rect.w):
            if y + lh > rect.bottom:
                break
            self.text(line, (rect.x, y), font, color, valign="top")
            y += lh
        return y - rect.y

    # --- widgets ---
    def button(self, rect, label: str, fn=None, *, enabled: bool = True, active: bool = False,
               focused: bool = False, font: str = "body", icon=None, color=None):
        rect = pygame.Rect(rect)
        t = self.theme
        bg = color or ("accent" if active else "panel2")
        self.fill(rect, bg if enabled else "panel", t.radius)
        if focused:
            self.fill(rect, "focus", t.radius, max(2, t.pad // 2))
        fg = "text" if enabled else "faint"
        if icon is not None:
            size = min(rect.h - 2 * t.pad, rect.w // 3)
            ir = pygame.Rect(0, 0, size, size)
            if label:
                ir.midleft = (rect.x + t.pad, rect.centery)
                icon(self, ir, self.color(fg))
                self.text(label, (ir.right + t.pad, rect.centery), font, fg, max_w=rect.right - ir.right - 2 * t.pad)
            else:
                ir.center = rect.center
                icon(self, ir, self.color(fg))
        else:
            self.text(label, rect.center, font, fg, align="center", max_w=rect.w - t.pad)
        if enabled:
            self.hit(rect, fn)
        return rect

    def pill(self, rect, level: str, text: str, font: str = "small"):
        rect = pygame.Rect(rect)
        self.fill(rect, "panel2", rect.h // 2)
        d = rect.h // 2
        pygame.draw.circle(self.surface, self.color(LEVEL.get(level, "off")), (rect.x + d, rect.centery), d // 2)
        self.text(text, (rect.x + d * 2, rect.centery), font, "text", max_w=rect.w - d * 2 - d // 2)

    def checkbox(self, rect, on: bool):
        rect = pygame.Rect(rect)
        self.fill(rect, "accent" if on else "panel", self.theme.radius // 2)
        self.fill(rect, "line", self.theme.radius // 2, 2)
        if on:
            w = max(2, rect.w // 8)
            pts = [(rect.x + rect.w * 0.22, rect.y + rect.h * 0.52), (rect.x + rect.w * 0.42, rect.y + rect.h * 0.72),
                   (rect.x + rect.w * 0.78, rect.y + rect.h * 0.3)]
            pygame.draw.lines(self.surface, COLORS["white"], False, pts, w)

    def sparkline(self, rect, points: list, color="accent", lo=None, hi=None, span: float | None = None):
        """``points`` are (t, value); ``span`` seconds back from the newest, if given."""
        rect = pygame.Rect(rect)
        if span is not None and points:
            t_end = points[-1][0]
            points = [p for p in points if p[0] >= t_end - span]
        vals = [v for _, v in points if v is not None]
        if len(vals) < 2:
            return
        lo = min(vals) if lo is None else lo
        hi = max(vals) if hi is None else hi
        if hi - lo < 1e-9:
            hi, lo = hi + 0.5, lo - 0.5
        t0, t1 = points[0][0], points[-1][0]
        if t1 - t0 < 1e-9:
            return
        step = max(1, len(points) // max(1, rect.w))        # at most about one point per pixel
        pts = [(rect.x + (t - t0) / (t1 - t0) * rect.w, rect.bottom - (v - lo) / (hi - lo) * rect.h)
               for t, v in points[::step] if v is not None]
        if len(pts) >= 2:
            pygame.draw.lines(self.surface, self.color(color), False, pts, max(1, self.theme.touch // 30))


# ----------------------------------------------------------------------------
# Lists and grids (encoder focus + touch drag scrolling)
# ----------------------------------------------------------------------------
class ListView:
    """A vertically scrolling list. The encoder / d-pad move ``focus`` (skipping rows that
    ``selectable`` refuses); a finger drags the list and taps a row."""

    def __init__(self):
        self.focus = 0
        self.offset = 0.0
        self._follow = True             # keep the focused row in view (off while dragging)
        self.count = 0

    def move(self, delta: int, count: int | None = None, selectable=None):
        n = self.count if count is None else count
        if n <= 0:
            return
        i = self.focus
        step = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            j = i + step
            while 0 <= j < n and selectable is not None and not selectable(j):
                j += step
            if 0 <= j < n:
                i = j
        self.focus = i
        self._follow = True

    def scroll_by(self, dy: float):
        self.offset += dy
        self._follow = False

    def draw(self, ui: UI, rect, count: int, row_h: int, draw_row, on_tap=None, selectable=None):
        rect = pygame.Rect(rect)
        self.count = count
        if count and selectable is not None and not selectable(min(self.focus, count - 1)):
            self.move(1, count, selectable)
        self.focus = max(0, min(self.focus, count - 1)) if count else 0
        max_off = max(0, count * row_h - rect.h)
        if self._follow and count:
            top = self.focus * row_h
            if top < self.offset:
                self.offset = top
            elif top + row_h > self.offset + rect.h:
                self.offset = top + row_h - rect.h
        self.offset = max(0, min(self.offset, max_off))
        clip = ui.surface.get_clip()
        ui.surface.set_clip(rect.clip(clip) if clip else rect)
        first = int(self.offset // row_h)
        for i in range(first, min(count, first + rect.h // row_h + 2)):
            r = pygame.Rect(rect.x, rect.y + i * row_h - round(self.offset), rect.w, row_h)
            draw_row(ui, i, r, i == self.focus)
            if on_tap is not None and (selectable is None or selectable(i)):
                ui.hit(r.clip(rect), lambda i=i: self._tap(i, on_tap))
        ui.surface.set_clip(clip)
        if max_off > 0:                         # scroll position, thin bar on the right
            bar_h = max(ui.theme.pad * 2, rect.h * rect.h // (count * row_h))
            y = rect.y + (rect.h - bar_h) * self.offset / max_off
            ui.fill((rect.right - ui.theme.pad // 2, y, ui.theme.pad // 2, bar_h), "line", 2)
        ui.drag(rect, self)

    def _tap(self, i, on_tap):
        self.focus = i
        self._follow = True
        on_tap(i)


class GridView:
    """Tiles in rows; the encoder moves through them in order, the d-pad moves in 2D."""

    def __init__(self):
        self.focus = 0
        self.cols = 1
        self.count = 0

    def move(self, delta: int):
        if self.count:
            self.focus = max(0, min(self.count - 1, self.focus + delta))

    def move2d(self, dx: int, dy: int):
        self.move(dx + dy * self.cols)

    def layout(self, rect, count: int, min_w: int, min_h: int, max_cols: int = 4) -> list[pygame.Rect]:
        rect = pygame.Rect(rect)
        self.count = count
        self.focus = max(0, min(self.focus, count - 1)) if count else 0
        if not count:
            return []
        cols = max(1, min(max_cols, count, rect.w // max(1, min_w)))
        rows = math.ceil(count / cols)
        while rows > 1 and rect.h // rows < min_h and cols < count:
            cols += 1
            rows = math.ceil(count / cols)
        self.cols = cols
        gap = max(4, min_w // 12)
        w = (rect.w - gap * (cols - 1)) // cols
        h = (rect.h - gap * (rows - 1)) // rows
        return [pygame.Rect(rect.x + (i % cols) * (w + gap), rect.y + (i // cols) * (h + gap), w, h)
                for i in range(count)]


# ----------------------------------------------------------------------------
# Icons: drawn, not bitmaps, so they're sharp at any size. icon(ui, rect, color)
# ----------------------------------------------------------------------------
def _lw(r):
    return max(2, r.w // 14)


def icon_can(ui: UI, r, c):
    """Two differential lines: CAN-H steps up while CAN-L steps down."""
    w, lw = r.w, _lw(r)
    xs = [0, .18, .18, .42, .42, .58, .58, .82, .82, 1]
    hi = [.5, .5, .22, .22, .5, .5, .22, .22, .5, .5]
    lo = [.5, .5, .78, .78, .5, .5, .78, .78, .5, .5]
    for ys in (hi, lo):
        pygame.draw.lines(ui.surface, c, False, [(r.x + x * w, r.y + y * r.h) for x, y in zip(xs, ys)], lw)


def icon_meter(ui: UI, r, c):
    lw = _lw(r)
    body = pygame.Rect(r.x + r.w * .18, r.y, r.w * .64, r.h)
    pygame.draw.rect(ui.surface, c, body, lw, border_radius=r.w // 10)
    scr = pygame.Rect(body.x + body.w * .14, body.y + body.h * .1, body.w * .72, body.h * .3)
    pygame.draw.rect(ui.surface, c, scr, lw)
    pygame.draw.circle(ui.surface, c, (body.centerx, body.y + body.h * .66), body.w * .22, lw)
    pygame.draw.line(ui.surface, c, (body.centerx, body.y + body.h * .66),
                     (body.centerx + body.w * .14, body.y + body.h * .56), lw)


def icon_settings(ui: UI, r, c):
    cx, cy, rad = r.centerx, r.centery, r.w * .3
    lw = _lw(r)
    for k in range(8):
        a = k * math.pi / 4
        pygame.draw.line(ui.surface, c, (cx + math.cos(a) * rad, cy + math.sin(a) * rad),
                         (cx + math.cos(a) * r.w * .46, cy + math.sin(a) * r.w * .46), lw * 2)
    pygame.draw.circle(ui.surface, c, (cx, cy), rad, lw)
    pygame.draw.circle(ui.surface, c, (cx, cy), rad * .4, lw)


def icon_log(ui: UI, r, c):
    lw = _lw(r)
    page = pygame.Rect(r.x + r.w * .15, r.y + r.h * .05, r.w * .6, r.h * .9)
    pygame.draw.rect(ui.surface, c, page, lw, border_radius=r.w // 14)
    for k in range(3):
        y = page.y + page.h * (.25 + k * .2)
        pygame.draw.line(ui.surface, c, (page.x + page.w * .2, y), (page.right - page.w * .2, y), lw)
    pygame.draw.circle(ui.surface, COLORS["rec"], (r.x + r.w * .78, r.y + r.h * .78), r.w * .16)


def icon_codes(ui: UI, r, c):
    lw = _lw(r)
    pts = [(r.centerx, r.y + r.h * .08), (r.right - r.w * .05, r.bottom - r.h * .1),
           (r.x + r.w * .05, r.bottom - r.h * .1)]
    pygame.draw.polygon(ui.surface, c, pts, lw)
    pygame.draw.line(ui.surface, c, (r.centerx, r.y + r.h * .38), (r.centerx, r.y + r.h * .64), lw + 1)
    pygame.draw.circle(ui.surface, c, (r.centerx, r.y + r.h * .76), lw)


def icon_live(ui: UI, r, c):
    pts = [(r.x, r.y + r.h * .7), (r.x + r.w * .25, r.y + r.h * .7), (r.x + r.w * .4, r.y + r.h * .2),
           (r.x + r.w * .58, r.y + r.h * .85), (r.x + r.w * .72, r.y + r.h * .45), (r.right, r.y + r.h * .45)]
    pygame.draw.lines(ui.surface, c, False, pts, _lw(r))


def icon_connect(ui: UI, r, c):
    lw = _lw(r)
    plug = pygame.Rect(r.x + r.w * .3, r.y + r.h * .3, r.w * .4, r.h * .32)
    pygame.draw.rect(ui.surface, c, plug, lw, border_radius=r.w // 12)
    for x in (.4, .6):
        pygame.draw.line(ui.surface, c, (r.x + r.w * x, r.y + r.h * .08), (r.x + r.w * x, plug.y), lw)
    pygame.draw.line(ui.surface, c, (r.centerx, plug.bottom), (r.centerx, r.bottom), lw)


def icon_bus(ui: UI, r, c):
    lw = _lw(r)
    y = r.y + r.h * .5
    pygame.draw.line(ui.surface, c, (r.x, y), (r.right, y), lw)
    for k, x in enumerate((.15, .5, .85)):
        top = k % 2 == 0
        node = pygame.Rect(0, 0, r.w * .24, r.h * .24)
        node.center = (r.x + r.w * x, r.y + r.h * (.2 if top else .8))
        pygame.draw.line(ui.surface, c, (node.centerx, y), node.center, lw)
        pygame.draw.rect(ui.surface, c, node, 0, border_radius=r.w // 16)


def icon_back(ui: UI, r, c):
    lw = _lw(r) + 1
    pygame.draw.lines(ui.surface, c, False, [(r.x + r.w * .62, r.y + r.h * .2), (r.x + r.w * .3, r.centery),
                                             (r.x + r.w * .62, r.y + r.h * .8)], lw)


def icon_home(ui: UI, r, c):
    lw = _lw(r)
    roof = [(r.x + r.w * .1, r.y + r.h * .5), (r.centerx, r.y + r.h * .12), (r.right - r.w * .1, r.y + r.h * .5)]
    pygame.draw.lines(ui.surface, c, False, roof, lw + 1)
    pygame.draw.rect(ui.surface, c, (r.x + r.w * .22, r.y + r.h * .46, r.w * .56, r.h * .44), lw)


ICONS = {"can": icon_can, "meter": icon_meter, "settings": icon_settings, "log": icon_log,
         "codes": icon_codes, "live": icon_live, "connect": icon_connect, "bus": icon_bus,
         "back": icon_back, "home": icon_home}
