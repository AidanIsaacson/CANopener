"""Colour themes for the desktop app (Settings > Colour theme), made from the same palettes as the
handheld's themes (core/themes.py): Element Zero, Graphite, Daylight, Basic and any of the user's
``~/.config/canopener/themes/*.json``. *Standard* leaves Qt's own look (it follows the system).

A theme is a Fusion style with a palette built from the theme's colours, and ``tint()`` for the
few places that colour a row or a banner (fault codes, OBD results, the Log tool's next
timestamp): pale backgrounds with dark text in the standard look, the theme's own colours in a
theme. The choice is kept in QSettings (``view/theme``).
"""

from ..core import themes
from .qt import QtGui, QtWidgets

STANDARD = "standard"
SETTING = "view/theme"

# the standard look's highlights (pale, so dark text on them stays readable in any system theme)
_PALE = {"bad": "#ffd6d6", "bad2": "#ffc9c9", "warn": "#ffe8cc", "stored": "#fff3bf", "caution": "#ffec99",
         "info": "#d0ebff", "ok": "#b2f2bb", "muted": "#e9ecef", "banner": "#fff3bf"}
# what each highlight is, in a theme's palette
_ROLE = {"bad": "bad", "bad2": "bad", "warn": "warn", "stored": "wait", "caution": "wait", "info": "accent",
         "ok": "ok", "muted": "off", "banner": "wait"}
_DARK_TEXT = "#212529"

_look: themes.Look | None = None            # None = Standard
_original: tuple | None = None              # the app's own style name and palette, to go back to


def choices() -> list[tuple[str, str]]:
    """(label, id): Standard, then every theme the handheld has."""
    return [("Standard (follows the system)", STANDARD)] + themes.available()


def current() -> str:
    return _look.id if _look is not None else STANDARD


def is_dark() -> bool:
    return _look is not None and themes.luminance(_look.colors["bg"]) < 0.4


def _q(rgb) -> QtGui.QColor:
    return QtGui.QColor(*rgb[:3])


def palette(look: themes.Look) -> QtGui.QPalette:
    c = look.colors
    dark = themes.luminance(c["bg"]) < 0.4
    window = c["panel"] if dark else c["bg"]
    base = c["bg"] if dark else c["panel"]
    P, G = QtGui.QPalette.ColorRole, QtGui.QPalette.ColorGroup
    pal = QtGui.QPalette()
    roles = {
        P.Window: window, P.WindowText: c["text"], P.Base: base, P.AlternateBase: themes.mix(base, window, 0.6),
        P.Text: c["text"], P.Button: c["panel2"], P.ButtonText: c["text"], P.BrightText: c["bad"],
        P.Highlight: c["accent"], P.HighlightedText: c["on_accent"], P.ToolTipBase: c["panel2"],
        P.ToolTipText: c["text"], P.PlaceholderText: c["faint"], P.Link: c["accent"], P.LinkVisited: c["accent"],
        P.Light: themes.mix(c["panel2"], c["text"], 0.2), P.Midlight: themes.mix(c["panel2"], c["text"], 0.1),
        P.Mid: c["line"], P.Dark: themes.mix(window, (0, 0, 0), 0.5), P.Shadow: c["shadow"],
    }
    for role, rgb in roles.items():
        pal.setColor(role, _q(rgb))
    for role in (P.WindowText, P.Text, P.ButtonText):
        pal.setColor(G.Disabled, role, _q(c["faint"]))
    pal.setColor(G.Disabled, P.Highlight, _q(c["line"]))
    return pal


def apply(app: QtWidgets.QApplication, theme_id: str | None):
    """Switch the whole app to ``theme_id`` (STANDARD or a core.themes id), straight away."""
    global _look, _original
    if _original is None:
        _original = (app.style().name(), QtGui.QPalette(app.palette()))
    from . import widgets
    if not theme_id or theme_id == STANDARD:
        _look = None
        app.setStyle(_original[0])
        app.setPalette(_original[1])
        widgets.STATUS_COLORS.update(widgets.STANDARD_STATUS_COLORS)
    else:
        _look = themes.load(theme_id)
        app.setStyle("Fusion")
        app.setPalette(palette(_look))
        c = _look.colors
        widgets.STATUS_COLORS.update({k: _hex(c[k]) for k in widgets.STATUS_COLORS})
    for w in app.allWidgets():          # a widget with its own style sheet keeps its old palette until
        if w.styleSheet():              # the sheet is set again
            w.setStyleSheet(w.styleSheet())


def tint(kind: str) -> tuple[QtGui.QColor, QtGui.QColor]:
    """(background, text) for a highlighted row or banner: bad, bad2, warn, stored, caution, info,
    ok, muted, banner."""
    if _look is None:
        return QtGui.QColor(_PALE.get(kind, "#ffffff")), QtGui.QColor(_DARK_TEXT)
    c = _look.colors
    base = c["bg"] if is_dark() else c["panel"]
    return _q(themes.mix(base, c[_ROLE.get(kind, "off")], 0.28 if is_dark() else 0.2)), _q(c["text"])


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb[:3])


def fill_style(kind: str) -> str:
    """Style-sheet colours for a solid button or banner of a status colour (ok, bad, wait, off,
    sim, idle) or the accent: its background and a text colour that reads on it."""
    if _look is None:
        from .widgets import STANDARD_STATUS_COLORS
        return f"background: {STANDARD_STATUS_COLORS.get(kind, '#1c7ed6')}; color: white;"
    bg = _look.colors.get(kind, _look.colors["accent"])
    fg = _look.colors["on_accent"] if kind == "accent" else max(
        ((255, 255, 255), (8, 10, 12)), key=lambda t: themes.contrast(t, bg))
    return f"background: {_hex(bg)}; color: {_hex(fg)};"


def color(name: str) -> QtGui.QColor | None:
    """A theme colour by role (``"bad"``, ``"accent"`` ...), or None in the standard look."""
    return _q(_look.colors[name]) if _look is not None and name in _look.colors else None
