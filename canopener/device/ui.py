"""Drawing for the handheld: sizes, colours, text, buttons, lists, icons.

Immediate mode: each frame a screen draws itself and registers what can be tapped (``UI.hit``)
and dragged (``UI.drag``) as it goes, so the touch areas are always exactly what's on screen.
Sizes come from the panel's real pixel density: a touch target is 10 mm whatever the screen
(DESIGN.md: gloved hands), so the same layout works on an 800x480 7", a 1280x720 7" and a
1280x800 10".
"""

import math
import time

import pygame

from ..core import themes

# The Basic theme's palette, for code that wants a colour without a UI to hand. Screens use
# ``ui.color(name)``, which follows the chosen theme (core/themes.py).
COLORS = {
    "bg": (16, 19, 24), "panel": (30, 35, 43), "panel2": (42, 49, 60), "line": (64, 72, 86),
    "text": (232, 236, 241), "dim": (140, 150, 165), "faint": (90, 98, 112), "accent": (64, 156, 255),
    "focus": (255, 196, 0), "rec": (240, 62, 62), "on": (46, 160, 90), "warn": (240, 160, 40),
    "ok": (47, 182, 96), "wait": (240, 170, 30), "bad": (230, 60, 60), "off": (110, 118, 130),
    "sim": (70, 140, 240), "idle": (110, 100, 220), "black": (0, 0, 0), "white": (255, 255, 255),
    # the finish: solid colours (no per-pixel alpha), so they cost what any fill costs
    "shadow": (7, 9, 12), "edge": (62, 71, 86), "edge_accent": (122, 188, 255), "edge_bad": (250, 120, 120),
    "accent_dim": (28, 52, 84), "panel_hi": (36, 42, 52),
}
ON = {"accent": "on_accent", "bad": "on_bad"}       # text colour on a filled button of that colour
EDGES = {"panel": "edge", "panel2": "edge", "accent": "edge_accent", "bad": "edge_bad", "panel_hi": "edge"}
LEVEL = {"ok": "ok", "wait": "wait", "bad": "bad", "off": "off", "sim": "sim", "idle": "idle"}


FONT_ROLE = {"small": "text", "body": "text", "big": "heading", "title": "heading", "huge": "numbers"}


class Theme:
    """Sizes for this panel (from its pixel density) and the look (themes.Look: colours, style,
    fonts) they're drawn in."""

    def __init__(self, size: tuple[int, int], diagonal_in: float = 7.0, look: themes.Look | None = None):
        w, h = size
        self.size = size
        self.look = look or themes.load(themes.DEFAULT)
        self.colors = self.look.colors
        self.style = self.look.style
        self.px_per_mm = math.hypot(w, h) / (max(diagonal_in, 3.0) * 25.4)
        self.touch = max(40, round(10 * self.px_per_mm))        # 10 mm: the smallest touch target
        self.pad = max(4, round(self.touch * 0.16))
        self.radius = max(4, round(self.touch * self.style["radius"])) if self.style["radius"] else 0
        self.row = self.touch
        self.font_px = {
            "small": round(self.touch * 0.36), "body": round(self.touch * 0.46),
            "big": round(self.touch * 0.62), "title": round(self.touch * 0.8),
            "huge": round(self.touch * 1.3),
        }
        self._files = {role: themes.first_font(self.look.fonts.get(role) or []) for role in themes.FONT_ROLES}
        self._fonts: dict = {}

    def font(self, name: str, px: int | None = None) -> pygame.font.Font:
        px = px or self.font_px.get(name, self.font_px["body"])
        file = self._files.get(FONT_ROLE.get(name, "text"))
        key = (file, px)
        f = self._fonts.get(key)
        if f is None:
            size = max(6, round(px * self.style["font_scale"] * (_FILE_SCALE if file else 1.0)))
            try:
                f = pygame.font.Font(file, size)
            except (OSError, pygame.error):         # a broken font file: the built-in one
                f = pygame.font.Font(None, px)
            self._fonts[key] = f
        return f


# Pygame's built-in font is small for its pixel size (capitals ~2/3 the height of most fonts'), and
# the layouts are sized for it: an installed font is brought down to a little over the same height.
_FILE_SCALE = 0.72


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
        return self.hit_rect_at(pos)[1]

    def hit_rect_at(self, pos):
        """(rect, fn) of the top-most tap area at ``pos``, or (None, None)."""
        for rect, fn in reversed(self.hits):
            if rect.collidepoint(pos):
                return rect, fn
        return None, None

    def drag_at(self, pos):
        for rect, s in reversed(self.drags):
            if rect.collidepoint(pos):
                return s
        return None

    # --- primitives ---
    def color(self, c):
        if isinstance(c, str):
            return self.theme.colors.get(c) or COLORS.get(c, c)
        return c

    @property
    def style(self) -> dict:
        return self.theme.style

    def fill(self, rect, color, radius: int = 0, width: int = 0):
        pygame.draw.rect(self.surface, self.color(color), rect, width, border_radius=radius)

    def panel(self, rect, color="panel", radius: int | None = None):
        """A flat surface (a list row, a value tile): the fill, and an outline if the theme has one."""
        rect = pygame.Rect(rect)
        radius = self.theme.radius if radius is None else radius
        self.fill(rect, color, radius)
        if self.style["border"]:
            self.fill(rect, "line", radius, 1)
        return rect

    def focus_ring(self, rect, radius: int | None = None, width: int | None = None):
        """What the encoder / d-pad is on."""
        t = self.theme
        self.fill(rect, "focus", t.radius if radius is None else radius, width or max(2, t.pad // 3))

    def card(self, rect, color="panel", radius: int | None = None, shadow: bool = True):
        """A raised panel: a drop shadow, the fill and a lighter top edge (or an outline), as the
        theme says. Plain draws, no alpha blending, so it's as cheap on a Pi as a flat fill."""
        rect = pygame.Rect(rect)
        radius = self.theme.radius if radius is None else radius
        st = self.style
        if shadow and st["shadow"]:
            d = max(2, self.theme.pad // 3)
            self.fill(rect.move(0, d), "shadow", radius)
        self.fill(rect, color, radius)
        if st["border"] and color in ("panel", "panel2", "panel_hi"):
            self.fill(rect, "line", radius, 1)
        edge = EDGES.get(color) if isinstance(color, str) and st["edge"] else None
        if edge and rect.w > 2 * radius:
            pygame.draw.line(self.surface, self.color(edge), (rect.x + radius, rect.y),
                             (rect.right - radius - 1, rect.y), max(1, self.theme.touch // 50))
        return rect

    def glow(self, rect):
        """Light up what the finger is on (drawn over everything, only while pressed)."""
        rect = pygame.Rect(rect)
        lit = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(lit, (*self.color("press"), 46), lit.get_rect(), border_radius=self.theme.radius)
        self.surface.blit(lit, rect.topleft)
        self.fill(rect, "accent", self.theme.radius, 2)

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
        self.card(rect, bg if enabled else "panel", t.radius, shadow=enabled)
        if focused:
            self.fill(rect, "focus", t.radius, max(2, t.pad // 2))
        fg = ON.get(bg, "text") if enabled else "faint"
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
        c = self.color(LEVEL.get(level, "off"))
        halo = themes.mix(self.color("panel2"), c, 0.3)
        pygame.draw.circle(self.surface, halo, (rect.x + d, rect.centery), d * .62)
        pygame.draw.circle(self.surface, c, (rect.x + d, rect.centery), d // 2)
        self.text(text, (rect.x + d * 2, rect.centery), font, "text", max_w=rect.w - d * 2 - d // 2)

    def checkbox(self, rect, on: bool):
        rect = pygame.Rect(rect)
        r = max(2, self.theme.radius // 2)
        self.fill(rect, "accent" if on else "panel", r)
        if not on:
            self.fill(rect, "line", r, 2)
        if on:
            w = max(2, rect.w // 8)
            pts = [(rect.x + rect.w * 0.22, rect.y + rect.h * 0.52), (rect.x + rect.w * 0.42, rect.y + rect.h * 0.72),
                   (rect.x + rect.w * 0.78, rect.y + rect.h * 0.3)]
            pygame.draw.lines(self.surface, self.color("on_accent"), False, pts, w)

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
        if len(pts) < 2:
            return
        c = self.color(color)
        w = max(1, self.theme.touch // 30)
        if self.style["trace_fill"]:            # shade down to the bottom, a dot on the newest value
            shade = themes.mix(self.color("panel"), c, 0.16)
            pygame.draw.polygon(self.surface, shade, pts + [(pts[-1][0], rect.bottom), (pts[0][0], rect.bottom)])
            pygame.draw.lines(self.surface, c, False, pts, w + 1)
            pygame.draw.circle(self.surface, c, pts[-1], w * 2.5)
        else:
            pygame.draw.lines(self.surface, c, False, pts, w)


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
            w = max(3, ui.theme.pad // 2)
            ui.fill((rect.right - w, rect.y, w, rect.h), "panel", w // 2)
            ui.fill((rect.right - w, y, w, bar_h), "line", w // 2)
        ui.drag(rect, self)

    def _tap(self, i, on_tap):
        self.focus = i
        self._follow = True
        on_tap(i)


class GridView:
    """Tiles in rows; the encoder moves through them in order, the d-pad moves in 2D.

    Double-tapping a tile (``tap``) enlarges it: it takes most of the space and the others stack
    beside it (below it on a tall screen). Double-tapping it again, or ``unzoom``, puts the grid
    back; double-tapping another tile enlarges that one instead."""

    DOUBLE_TAP = 0.45       # seconds between two taps on the same tile

    def __init__(self, clock=time.monotonic):
        self.focus = 0
        self.cols = 1
        self.count = 0
        self.zoom: int | None = None        # the enlarged tile's index
        self.clock = clock
        self._tapped: tuple[int, float] | None = None

    def tap(self, i: int) -> bool:
        """A tap on tile ``i``: focus it; the second of a double tap also (un)enlarges it. True
        when the zoom changed."""
        now = self.clock()
        self.focus = i
        if self._tapped is not None and self._tapped[0] == i and now - self._tapped[1] <= self.DOUBLE_TAP:
            self._tapped = None
            self.zoom = None if self.zoom == i else i
            return True
        self._tapped = (i, now)
        return False

    def unzoom(self) -> bool:
        """Back to the plain grid. True if a tile was enlarged."""
        was, self.zoom = self.zoom is not None, None
        return was

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
            self.zoom = None
            return []
        gap = max(4, min_w // 12)
        if self.zoom is not None and (self.zoom >= count or count < 2):
            self.zoom = None
        if self.zoom is not None:
            return self._zoomed(rect, count, min_w, min_h, gap)
        cols = max(1, min(max_cols, count, rect.w // max(1, min_w)))
        rows = math.ceil(count / cols)
        while rows > 1 and rect.h // rows < min_h and cols < count:
            cols += 1
            rows = math.ceil(count / cols)
        self.cols = cols
        w = (rect.w - gap * (cols - 1)) // cols
        h = (rect.h - gap * (rows - 1)) // rows
        return [pygame.Rect(rect.x + (i % cols) * (w + gap), rect.y + (i // cols) * (h + gap), w, h)
                for i in range(count)]

    def _zoomed(self, rect, count: int, min_w: int, min_h: int, gap: int) -> list[pygame.Rect]:
        """One big tile and the rest stacked: in a strip down the right side, or along the bottom
        when the space is taller than it is wide. The strip grows a second line when one line
        would squash its tiles, but never takes more than half the space."""
        n = count - 1
        self.cols = 1                       # the d-pad steps through them in order
        small_w, small_h = max(1, round(min_w * .8)), max(1, round(min_h * .6))
        if rect.w >= rect.h:
            lines = max(1, math.ceil(n / max(1, (rect.h + gap) // (small_h + gap))))
            col_w = max(small_w, round(rect.w * .27))
            lines = max(1, min(lines, (rect.w // 2) // max(1, col_w)))
            strip = lines * col_w + (lines - 1) * gap
            per = math.ceil(n / lines)
            h = (rect.h - gap * (per - 1)) // per
            big = pygame.Rect(rect.x, rect.y, rect.w - strip - gap, rect.h)
            small = [pygame.Rect(big.right + gap + (j // per) * (col_w + gap), rect.y + (j % per) * (h + gap),
                                 col_w, h) for j in range(n)]
        else:
            lines = max(1, math.ceil(n / max(1, (rect.w + gap) // (small_w + gap))))
            row_h = max(small_h, round(rect.h * .2))
            lines = max(1, min(lines, (rect.h // 2) // max(1, row_h)))
            strip = lines * row_h + (lines - 1) * gap
            per = math.ceil(n / lines)
            w = (rect.w - gap * (per - 1)) // per
            big = pygame.Rect(rect.x, rect.y, rect.w, rect.h - strip - gap)
            small = [pygame.Rect(rect.x + (j % per) * (w + gap), big.bottom + gap + (j // per) * (row_h + gap),
                                 w, row_h) for j in range(n)]
        out = small[:self.zoom] + [big] + small[self.zoom:]
        return out


# ----------------------------------------------------------------------------
# Icons: drawn, not bitmaps, so they're sharp at any size. icon(ui, rect, color)
#
# Each is drawn once at three times the size and scaled down smoothly (anti-aliased edges), then
# kept, so a frame only blits it. Two sets: "classic" and "line" (rounded strokes on a 24-unit
# grid), picked by the theme's ``icons`` style. A plugin's own icon functions draw directly.
# ----------------------------------------------------------------------------
_SS = 3
_icon_cache: dict = {}


class _Pen:
    """What an icon is drawn with: the big surface, and the UI's colours."""

    def __init__(self, ui: UI, surface: pygame.Surface):
        self.surface = surface
        self.theme = ui.theme
        self._ui = ui

    def color(self, c):
        return self._ui.color(c)


def draw_icon(ui: UI, fn, r, c):
    """``fn(pen, rect, color)`` drawn smooth into ``r`` (cached per size and colour)."""
    r = pygame.Rect(r)
    if r.w < 2 or r.h < 2:
        return
    c = tuple(c[:3])
    key = (fn, r.size, c, ui.color("rec"), ui.color("bg"))
    s = _icon_cache.get(key)
    if s is None:
        if len(_icon_cache) > 400:
            _icon_cache.clear()
        big = pygame.Surface((r.w * _SS, r.h * _SS), pygame.SRCALPHA)
        big.fill((*c, 0))                       # transparent, but the icon's colour at the edges
        fn(_Pen(ui, big), big.get_rect(), c)
        s = _icon_cache[key] = pygame.transform.smoothscale(big, r.size)
    ui.surface.blit(s, r)


def _icon(classic, line=None):
    def icon(ui: UI, r, c):
        fn = line if line is not None and ui.style["icons"] == "line" else classic
        draw_icon(ui, fn, r, c)
    icon.__doc__ = classic.__doc__
    return icon


def _lw(r):
    return max(2, r.w // 14)


# --- classic -------------------------------------------------------------------
def _can(ui, r, c):
    """Two differential lines: CAN-H steps up while CAN-L steps down."""
    w, lw = r.w, _lw(r)
    xs = [0, .18, .18, .42, .42, .58, .58, .82, .82, 1]
    hi = [.5, .5, .22, .22, .5, .5, .22, .22, .5, .5]
    lo = [.5, .5, .78, .78, .5, .5, .78, .78, .5, .5]
    for ys in (hi, lo):
        pygame.draw.lines(ui.surface, c, False, [(r.x + x * w, r.y + y * r.h) for x, y in zip(xs, ys)], lw)


def _meter(ui, r, c):
    lw = _lw(r)
    body = pygame.Rect(r.x + r.w * .18, r.y, r.w * .64, r.h)
    pygame.draw.rect(ui.surface, c, body, lw, border_radius=r.w // 10)
    scr = pygame.Rect(body.x + body.w * .14, body.y + body.h * .1, body.w * .72, body.h * .3)
    pygame.draw.rect(ui.surface, c, scr, lw)
    pygame.draw.circle(ui.surface, c, (body.centerx, body.y + body.h * .66), body.w * .22, lw)
    pygame.draw.line(ui.surface, c, (body.centerx, body.y + body.h * .66),
                     (body.centerx + body.w * .14, body.y + body.h * .56), lw)


def _settings(ui, r, c):
    cx, cy, rad = r.centerx, r.centery, r.w * .3
    lw = _lw(r)
    for k in range(8):
        a = k * math.pi / 4
        pygame.draw.line(ui.surface, c, (cx + math.cos(a) * rad, cy + math.sin(a) * rad),
                         (cx + math.cos(a) * r.w * .46, cy + math.sin(a) * r.w * .46), lw * 2)
    pygame.draw.circle(ui.surface, c, (cx, cy), rad, lw)
    pygame.draw.circle(ui.surface, c, (cx, cy), rad * .4, lw)


def _log(ui, r, c):
    lw = _lw(r)
    page = pygame.Rect(r.x + r.w * .15, r.y + r.h * .05, r.w * .6, r.h * .9)
    pygame.draw.rect(ui.surface, c, page, lw, border_radius=r.w // 14)
    for k in range(3):
        y = page.y + page.h * (.25 + k * .2)
        pygame.draw.line(ui.surface, c, (page.x + page.w * .2, y), (page.right - page.w * .2, y), lw)
    pygame.draw.circle(ui.surface, ui.color("rec"), (r.x + r.w * .78, r.y + r.h * .78), r.w * .16)


def _codes(ui, r, c):
    lw = _lw(r)
    pts = [(r.centerx, r.y + r.h * .08), (r.right - r.w * .05, r.bottom - r.h * .1),
           (r.x + r.w * .05, r.bottom - r.h * .1)]
    pygame.draw.polygon(ui.surface, c, pts, lw)
    pygame.draw.line(ui.surface, c, (r.centerx, r.y + r.h * .38), (r.centerx, r.y + r.h * .64), lw + 1)
    pygame.draw.circle(ui.surface, c, (r.centerx, r.y + r.h * .76), lw)


def _live(ui, r, c):
    pts = [(r.x, r.y + r.h * .7), (r.x + r.w * .25, r.y + r.h * .7), (r.x + r.w * .4, r.y + r.h * .2),
           (r.x + r.w * .58, r.y + r.h * .85), (r.x + r.w * .72, r.y + r.h * .45), (r.right, r.y + r.h * .45)]
    pygame.draw.lines(ui.surface, c, False, pts, _lw(r))


def _connect(ui, r, c):
    lw = _lw(r)
    plug = pygame.Rect(r.x + r.w * .3, r.y + r.h * .3, r.w * .4, r.h * .32)
    pygame.draw.rect(ui.surface, c, plug, lw, border_radius=r.w // 12)
    for x in (.4, .6):
        pygame.draw.line(ui.surface, c, (r.x + r.w * x, r.y + r.h * .08), (r.x + r.w * x, plug.y), lw)
    pygame.draw.line(ui.surface, c, (r.centerx, plug.bottom), (r.centerx, r.bottom), lw)


def _bus(ui, r, c):
    lw = _lw(r)
    y = r.y + r.h * .5
    pygame.draw.line(ui.surface, c, (r.x, y), (r.right, y), lw)
    for k, x in enumerate((.15, .5, .85)):
        top = k % 2 == 0
        node = pygame.Rect(0, 0, r.w * .24, r.h * .24)
        node.center = (r.x + r.w * x, r.y + r.h * (.2 if top else .8))
        pygame.draw.line(ui.surface, c, (node.centerx, y), node.center, lw)
        pygame.draw.rect(ui.surface, c, node, 0, border_radius=r.w // 16)


def _back(ui, r, c):
    lw = _lw(r) + 1
    pygame.draw.lines(ui.surface, c, False, [(r.x + r.w * .62, r.y + r.h * .2), (r.x + r.w * .3, r.centery),
                                             (r.x + r.w * .62, r.y + r.h * .8)], lw)


def _home(ui, r, c):
    lw = _lw(r)
    roof = [(r.x + r.w * .1, r.y + r.h * .5), (r.centerx, r.y + r.h * .12), (r.right - r.w * .1, r.y + r.h * .5)]
    pygame.draw.lines(ui.surface, c, False, roof, lw + 1)
    pygame.draw.rect(ui.surface, c, (r.x + r.w * .22, r.y + r.h * .46, r.w * .56, r.h * .44), lw)


def _games(ui, r, c):
    """A wire bend joining two bus bars: Harness."""
    lw = _lw(r) + 1
    for x in (.08, .92):
        pygame.draw.line(ui.surface, c, (r.x + r.w * x, r.y + r.h * .1), (r.x + r.w * x, r.bottom - r.h * .1), lw + 2)
    pts = [(r.x + r.w * .08, r.y + r.h * .3), (r.centerx, r.y + r.h * .3), (r.centerx, r.y + r.h * .7),
           (r.right - r.w * .08, r.y + r.h * .7)]
    pygame.draw.lines(ui.surface, c, False, pts, lw)
    pygame.draw.circle(ui.surface, c, (r.centerx, r.y + r.h * .3), lw * 1.2)
    pygame.draw.circle(ui.surface, c, (r.centerx, r.y + r.h * .7), lw * 1.2)


# --- line: rounded strokes on a 24 x 24 grid ------------------------------------
class _Grid:
    def __init__(self, ui, r, c, weight: float = 2.0):
        self.surface, self.c = ui.surface, c
        self.k = min(r.w, r.h) / 24
        self.ox, self.oy = r.x + (r.w - 24 * self.k) / 2, r.y + (r.h - 24 * self.k) / 2
        self.w = weight * self.k

    def p(self, x, y):
        return self.ox + x * self.k, self.oy + y * self.k

    def stroke(self, pts, closed=False, color=None):
        """A polyline with round ends and joins."""
        c = color or self.c
        pts = [self.p(*q) for q in pts] + ([self.p(*pts[0])] if closed else [])
        pygame.draw.lines(self.surface, c, False, pts, max(1, round(self.w)))
        for q in pts:
            pygame.draw.circle(self.surface, c, q, self.w / 2)

    def rect(self, x0, y0, x1, y1, radius, fill=False, color=None):
        r = pygame.Rect(*self.p(x0, y0), (x1 - x0) * self.k, (y1 - y0) * self.k)
        if fill:
            pygame.draw.rect(self.surface, color or self.c, r, 0, border_radius=round(radius * self.k))
        else:
            r = r.inflate(self.w, self.w)         # the stroke centred on the outline, like stroke()
            pygame.draw.rect(self.surface, color or self.c, r, max(1, round(self.w)),
                             border_radius=round(radius * self.k + self.w / 2))

    def circle(self, x, y, rad, fill=False, color=None):
        if fill:
            pygame.draw.circle(self.surface, color or self.c, self.p(x, y), rad * self.k)
        else:
            pygame.draw.circle(self.surface, color or self.c, self.p(x, y), rad * self.k + self.w / 2,
                               max(1, round(self.w)))


def _can_line(ui, r, c):
    g = _Grid(ui, r, c)
    xs = [2, 6, 6, 11, 11, 14, 14, 19, 19, 22]
    g.stroke(list(zip(xs, [12, 12, 5, 5, 12, 12, 5, 5, 12, 12])))
    g.stroke(list(zip(xs, [12, 12, 19, 19, 12, 12, 19, 19, 12, 12])))


def _meter_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.rect(5.5, 2.5, 18.5, 21.5, 2.5)
    g.rect(8.5, 5.5, 15.5, 9.5, 1)
    g.circle(12, 15.5, 2.6)
    g.stroke([(12, 15.5), (13.6, 14.0)])


def _settings_line(ui, r, c):
    g = _Grid(ui, r, c, 1.9)
    pts = []
    for k in range(8):
        a0 = k * 45
        for rad, da in ((7.4, -15), (10.2, -8), (10.2, 8), (7.4, 15)):
            a = math.radians(a0 + da)
            pts.append((12 + rad * math.cos(a), 12 + rad * math.sin(a)))
    g.stroke(pts, closed=True)
    g.circle(12, 12, 2.6)


def _log_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.stroke([(5, 3), (13.5, 3), (18, 7.5), (18, 12)])
    g.stroke([(5, 3), (5, 21), (12, 21)])
    g.stroke([(13.5, 3), (13.5, 7.5), (18, 7.5)])
    g.stroke([(8.5, 11), (14, 11)])
    g.stroke([(8.5, 15), (11.5, 15)])
    g.circle(17.5, 18.5, 5.4, fill=True, color=(*c, 0))      # knock out a ring round the dot
    g.circle(17.5, 18.5, 3.8, fill=True, color=ui.color("rec"))


def _codes_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.stroke([(12, 3.5), (21.5, 20), (2.5, 20)], closed=True)
    g.stroke([(12, 9.5), (12, 13.5)])
    g.circle(12, 16.8, 1.35, fill=True)


def _live_line(ui, r, c):
    _Grid(ui, r, c).stroke([(2, 13), (6.5, 13), (9, 6), (13, 19), (16, 10), (18, 13), (22, 13)])


def _connect_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.rect(7, 8, 17, 15, 2.5)
    g.stroke([(10, 3), (10, 8)])
    g.stroke([(14, 3), (14, 8)])
    g.stroke([(12, 15), (12, 21.5)])


def _bus_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.stroke([(2, 12), (22, 12)])
    for x, y in ((5, 5.5), (12, 18.5), (19, 5.5)):
        g.stroke([(x, 12), (x, y)])
        g.rect(x - 2.6, y - 2.6, x + 2.6, y + 2.6, 1.2, fill=True)


def _back_line(ui, r, c):
    _Grid(ui, r, c, 2.4).stroke([(15, 5), (8, 12), (15, 19)])


def _home_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.stroke([(3, 11), (12, 3.5), (21, 11)])
    g.stroke([(5.5, 9.5), (5.5, 20.5), (18.5, 20.5), (18.5, 9.5)])
    g.stroke([(10, 20.5), (10, 15), (14, 15), (14, 20.5)])


def _games_line(ui, r, c):
    g = _Grid(ui, r, c)
    g.stroke([(3, 3.5), (3, 20.5)], color=c)
    g.stroke([(21, 3.5), (21, 20.5)], color=c)
    g.stroke([(3, 9), (12, 9), (12, 15), (21, 15)])
    g.circle(12, 9, 1.6, fill=True)
    g.circle(12, 15, 1.6, fill=True)


icon_can = _icon(_can, _can_line)
icon_meter = _icon(_meter, _meter_line)
icon_settings = _icon(_settings, _settings_line)
icon_log = _icon(_log, _log_line)
icon_codes = _icon(_codes, _codes_line)
icon_live = _icon(_live, _live_line)
icon_connect = _icon(_connect, _connect_line)
icon_bus = _icon(_bus, _bus_line)
icon_back = _icon(_back, _back_line)
icon_home = _icon(_home, _home_line)
icon_games = _icon(_games, _games_line)

ICONS = {"can": icon_can, "meter": icon_meter, "settings": icon_settings, "log": icon_log,
         "codes": icon_codes, "live": icon_live, "connect": icon_connect, "bus": icon_bus,
         "back": icon_back, "home": icon_home, "games": icon_games}
