"""Looks: colours, fonts and finish for the handheld (System > Display > Theme), and the palettes
the desktop app's colour themes are made from (gui/themes.py).

A theme is a palette (named colours: ``bg``, ``panel``, ``text``, ``accent``, ``ok``, ...), a few
style switches (corner size, shadows or outlines, which icon set, how the home tiles look) and the
fonts to use. Four are built in; more can be added as JSON files in
``~/.config/canopener/themes/``, each starting from a built-in one and changing what it likes::

    {"name": "Orange", "base": "graphite", "colors": {"accent": "#ff7a1a"}}

Every key is listed in docs/DEVICE.md ("Themes"). ``device.accent`` (System > Display > Accent
colour) swaps the accent of whichever theme is chosen.

Standard library only (core/): device/ui.py turns a ``Look`` into fonts and drawing, and
gui/themes.py into a Qt palette. Fonts are found by name through fontconfig (``fc-match``), so a theme can
name any installed font; if none of its fonts is installed, Pygame's built-in one is used.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from . import config

DEFAULT = "element-zero"
ALIASES = {"classic": "basic", "night": "element-zero"}     # earlier names, still understood

# Every colour a theme has. A user theme names only the ones it changes.
COLOR_KEYS = (
    "bg", "bar", "panel", "panel2", "panel_hi", "line", "text", "dim", "faint",
    "accent", "accent_dim", "edge_accent", "on_accent", "focus", "press",
    "ok", "wait", "bad", "on_bad", "warn", "off", "sim", "idle", "rec", "on",
    "shadow", "edge", "edge_bad", "black", "white",
)

STYLE_DEFAULTS = {
    "radius": 0.14,             # corner radius, as a fraction of a touch target (10 mm)
    "shadow": True,             # a drop shadow under raised panels (buttons, tiles)
    "edge": True,               # a lighter line along the top of raised panels
    "border": False,            # a 1-pixel outline round panels, list rows and tiles
    "icons": "classic",         # classic / line (rounded strokes)
    "tile": "disc",             # home tiles: disc (icon centred on a disc) / chip (left-aligned)
    "trace_fill": False,        # shade under live-data traces, with a dot on the newest value
    "font_scale": 1.0,          # all text bigger or smaller
}
STYLE_CHOICES = {"icons": ("classic", "line"), "tile": ("disc", "chip")}

# Font roles: "text" (small, body), "heading" (big, title), "numbers" (huge readouts). Each is a
# list tried in order: "Family" or "Family:weight" (fontconfig names: regular, medium, semibold,
# bold, ...) or a path to a .ttf / .otf. Empty = Pygame's built-in font.
FONT_ROLES = ("text", "heading", "numbers")

_BASIC = {
    "name": "Basic",
    "colors": {
        "bg": (16, 19, 24), "bar": (30, 35, 43), "panel": (30, 35, 43), "panel2": (42, 49, 60),
        "panel_hi": (36, 42, 52), "line": (64, 72, 86), "text": (232, 236, 241), "dim": (140, 150, 165),
        "faint": (90, 98, 112), "accent": (64, 156, 255), "accent_dim": (28, 52, 84),
        "edge_accent": (122, 188, 255), "on_accent": (232, 236, 241), "focus": (255, 196, 0),
        "press": (255, 255, 255), "ok": (47, 182, 96), "wait": (240, 170, 30), "bad": (230, 60, 60),
        "on_bad": (255, 255, 255), "warn": (240, 160, 40), "off": (110, 118, 130), "sim": (70, 140, 240),
        "idle": (110, 100, 220), "rec": (240, 62, 62), "on": (46, 160, 90), "shadow": (7, 9, 12),
        "edge": (62, 71, 86), "edge_bad": (250, 120, 120), "black": (0, 0, 0), "white": (255, 255, 255),
    },
    "style": {},
    "fonts": {},
}

_SANS = ["Inter:medium", "DejaVu Sans", "Liberation Sans"]
_SANS_HEAD = ["Inter:semibold", "DejaVu Sans:bold", "Liberation Sans:bold"]
_SANS_NUM = ["Inter Display:semibold", "Inter:semibold", "DejaVu Sans:bold", "Liberation Sans:bold"]
_MODERN = {"radius": 0.18, "shadow": False, "edge": False, "border": True, "icons": "line", "tile": "chip",
           "trace_fill": True}

BUILTIN: dict[str, dict] = {
    # Amber on black, like a glowing hologram; easy on eyes adjusted to a dark cab.
    "element-zero": {
        "name": "Element Zero",
        "colors": {
            "bg": "#000000", "bar": "#0b0702", "panel": "#130d05", "panel2": "#1f1608", "panel_hi": "#191107",
            "line": "#3a2a12", "text": "#ffb547", "dim": "#b77d2c", "faint": "#6e4b1b",
            "accent": "#ff9e1b", "accent_dim": "#3a2507", "edge_accent": "#ffc56b", "on_accent": "#0b0702",
            "focus": "#ffe2a8", "press": "#ffb547", "ok": "#8fbf3a", "wait": "#e6a531", "bad": "#ff4a2b",
            "on_bad": "#0b0702", "warn": "#ff7a1a", "off": "#6e5a40", "sim": "#c79036", "idle": "#a8743a",
            "rec": "#ff4a2b", "on": "#8fbf3a", "shadow": "#000000", "edge": "#3a2a12", "edge_bad": "#ff7f66",
            "white": "#ffe2a8",
        },
        "style": dict(_MODERN),
        "fonts": {"text": _SANS, "heading": _SANS_HEAD, "numbers": _SANS_NUM},
    },
    # Quieter dark: flat panels with a fine outline, teal accent, proper type.
    "graphite": {
        "name": "Graphite",
        "colors": {
            "bg": "#0c0e12", "bar": "#121419", "panel": "#181b21", "panel2": "#22262e", "panel_hi": "#1d2027",
            "line": "#2e333d", "text": "#eef0f3", "dim": "#9aa2b0", "faint": "#5f6775",
            "accent": "#2cc6b0", "accent_dim": "#123a36", "edge_accent": "#7fe3d4", "on_accent": "#06110f",
            "focus": "#ffc233", "press": "#ffffff", "ok": "#3ccf7a", "wait": "#f2b233", "bad": "#ff5a52",
            "on_bad": "#ffffff", "warn": "#f5a524", "off": "#6b7280", "sim": "#5b9cff", "idle": "#9a8cff",
            "rec": "#ff4d4d", "on": "#3ccf7a", "shadow": "#050607", "edge": "#2e333d", "edge_bad": "#ff8f88",
        },
        "style": dict(_MODERN),
        "fonts": {"text": _SANS, "heading": _SANS_HEAD, "numbers": _SANS_NUM},
    },
    # Light, for sunlight: dark text on white, strong colours, a black focus ring.
    "daylight": {
        "name": "Daylight",
        "colors": {
            "bg": "#e6e9ee", "bar": "#d7dce3", "panel": "#ffffff", "panel2": "#eef1f5", "panel_hi": "#f7f9fb",
            "line": "#b9c1cc", "text": "#0b0f14", "dim": "#444c58", "faint": "#6f7886",
            "accent": "#0057c2", "accent_dim": "#d4e3f7", "edge_accent": "#4d8fe0", "on_accent": "#ffffff",
            "focus": "#0b0f14", "press": "#0057c2", "ok": "#12823f", "wait": "#b86e00", "bad": "#c81e1e",
            "on_bad": "#ffffff", "warn": "#b35c00", "off": "#7a8494", "sim": "#2463d1", "idle": "#5b45c9",
            "rec": "#d01c1c", "on": "#12823f", "shadow": "#c3cad4", "edge": "#ffffff", "edge_bad": "#e86a6a",
            "black": "#000000", "white": "#ffffff",
        },
        "style": dict(_MODERN, shadow=True),
        "fonts": {"text": ["Inter:semibold", "DejaVu Sans:bold", "Liberation Sans:bold"],
                  "heading": ["Inter:bold", "DejaVu Sans:bold", "Liberation Sans:bold"], "numbers": _SANS_NUM},
    },
    # The original look: Pygame's own font, raised panels with shadows, a blue accent.
    "basic": _BASIC,
}

# System > Display > Accent colour ("" = the theme's own).
ACCENTS = [("Theme's own", ""), ("Blue", "#3d8bff"), ("Teal", "#2cc6b0"), ("Green", "#3cbf5c"),
           ("Orange", "#ff8a1f"), ("Red", "#f0443a"), ("Pink", "#f0559a"), ("Purple", "#9a6bff")]


@dataclass
class Look:
    """A theme, resolved: every colour as (r, g, b), every style key, the font lists."""
    id: str
    name: str
    colors: dict
    style: dict
    fonts: dict
    problems: list = field(default_factory=list)     # what was wrong with a theme file (shown once)


def themes_dir() -> str:
    return os.path.join(config.config_dir(), "themes")


def parse_color(value) -> tuple[int, int, int]:
    """"#rgb", "#rrggbb" or [r, g, b] -> (r, g, b). Raises ValueError."""
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        rgb = tuple(int(v) for v in value[:3])
    elif isinstance(value, str) and value.startswith("#") and len(value) in (4, 7):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    else:
        raise ValueError(f"not a colour: {value!r} (use \"#rrggbb\")")
    if not all(0 <= v <= 255 for v in rgb):
        raise ValueError(f"not a colour: {value!r}")
    return rgb


def mix(a, b, t: float) -> tuple[int, int, int]:
    """``a`` blended ``t`` of the way towards ``b``."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def luminance(c) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _user_files() -> dict[str, str]:
    """Theme id -> JSON file, for the files in themes_dir() (built-in names can't be replaced)."""
    d = themes_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return {}
    out = {}
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext.lower() == ".json" and stem not in BUILTIN and stem not in ALIASES:
            out[stem] = os.path.join(d, n)
    return out


def _read(path: str) -> tuple[dict, str]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {}, f"{os.path.basename(path)}: {e}"
    if not isinstance(data, dict):
        return {}, f"{os.path.basename(path)}: not a JSON object"
    return data, ""


def available() -> list[tuple[str, str]]:
    """(label, id) for every theme: the built-in ones, then the user's files."""
    out = [(t["name"], tid) for tid, t in BUILTIN.items()]
    for tid, path in _user_files().items():
        data, _ = _read(path)
        out.append((str(data.get("name") or tid), tid))
    return out


def load(theme_id: str | None = None, accent: str = "") -> Look:
    """The theme ``theme_id`` (unknown -> the default), with ``accent`` ("#rrggbb") swapped in."""
    problems: list[str] = []
    tid = ALIASES.get(theme_id or DEFAULT, theme_id or DEFAULT)
    spec = BUILTIN.get(tid)
    if spec is None:
        path = _user_files().get(tid)
        if path is None:
            if theme_id:
                problems.append(f"Theme '{theme_id}' not found: using {BUILTIN[DEFAULT]['name']}")
            tid, spec = DEFAULT, BUILTIN[DEFAULT]
        else:
            spec, err = _read(path)
            if err:
                problems.append(err)
            spec = dict(spec, name=spec.get("name") or tid)
    base_id = spec.get("base", DEFAULT) if tid not in BUILTIN else None
    base_id = ALIASES.get(base_id, base_id)
    if base_id is not None and base_id not in BUILTIN:
        problems.append(f"{spec.get('name')}: unknown base '{base_id}', using {DEFAULT}")
        base_id = DEFAULT
    base = BUILTIN[base_id] if base_id else {"colors": {}, "style": {}, "fonts": {}}

    colors = {k: parse_color(v) for k, v in _BASIC["colors"].items()}
    for src in (base.get("colors", {}), spec.get("colors", {})):
        if not isinstance(src, dict):
            problems.append(f"{spec.get('name')}: \"colors\" must be an object")
            continue
        for k, v in src.items():
            if k not in COLOR_KEYS:
                problems.append(f"{spec.get('name')}: unknown colour '{k}'")
                continue
            try:
                colors[k] = parse_color(v)
            except ValueError as e:
                problems.append(f"{spec.get('name')}: {k}: {e}")

    style = dict(STYLE_DEFAULTS)
    for src in (base.get("style", {}), spec.get("style", {})):
        for k, v in (src.items() if isinstance(src, dict) else ()):
            if k not in STYLE_DEFAULTS:
                problems.append(f"{spec.get('name')}: unknown style '{k}'")
            elif k in STYLE_CHOICES and v not in STYLE_CHOICES[k]:
                problems.append(f"{spec.get('name')}: style {k} is one of {', '.join(STYLE_CHOICES[k])}")
            elif isinstance(STYLE_DEFAULTS[k], bool) and not isinstance(v, bool):
                problems.append(f"{spec.get('name')}: style {k} is true or false")
            elif isinstance(STYLE_DEFAULTS[k], float) and not isinstance(v, (int, float)):
                problems.append(f"{spec.get('name')}: style {k} is a number")
            else:
                style[k] = v
    style["radius"] = min(0.5, max(0.0, float(style["radius"])))
    style["font_scale"] = min(1.5, max(0.6, float(style["font_scale"])))

    fonts = {r: [] for r in FONT_ROLES}
    for src in (base.get("fonts", {}), spec.get("fonts", {})):
        for k, v in (src.items() if isinstance(src, dict) else ()):
            if k not in FONT_ROLES:
                problems.append(f"{spec.get('name')}: unknown font role '{k}' ({', '.join(FONT_ROLES)})")
            else:
                fonts[k] = [v] if isinstance(v, str) else [str(x) for x in v]

    if accent:
        try:
            set_accent(colors, parse_color(accent))
        except ValueError as e:
            problems.append(f"Accent colour: {e}")
    return Look(tid, str(spec.get("name") or tid), colors, style, fonts, problems)


def set_accent(colors: dict, accent: tuple):
    """Swap the accent, and the colours worked out from it, in a resolved palette."""
    colors["accent"] = accent
    light = luminance(colors["bg"]) > 0.4
    colors["accent_dim"] = mix(colors["panel"], accent, 0.18 if light else 0.25)
    colors["edge_accent"] = mix(accent, (255, 255, 255), 0.35)
    colors["on_accent"] = max(((255, 255, 255), (8, 10, 12)), key=lambda c: contrast(c, accent))


# ----------------------------------------------------------------------------
# Fonts: a name -> a file, through fontconfig
# ----------------------------------------------------------------------------
_font_files: dict[str, str | None] = {}


def font_file(spec: str) -> str | None:
    """The file for "Family" / "Family:weight" / a path, or None if it isn't installed."""
    if spec in _font_files:
        return _font_files[spec]
    path = None
    if os.sep in spec or spec.lower().endswith((".ttf", ".otf")):
        p = os.path.expanduser(spec)
        path = p if os.path.isfile(p) else None
    elif shutil.which("fc-match"):
        family = spec.split(":")[0].strip().lower()
        try:
            out = subprocess.run(["fc-match", "-f", "%{family}\n%{file}", spec], capture_output=True,
                                 text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        names, _, file = out.partition("\n")
        # fc-match always answers with something: only take it if it's the family asked for
        if file and family in (n.strip().lower() for n in names.split(",")) and os.path.isfile(file):
            path = file
    _font_files[spec] = path
    return path


def first_font(candidates: list[str]) -> str | None:
    for spec in candidates:
        f = font_file(spec)
        if f:
            return f
    return None
