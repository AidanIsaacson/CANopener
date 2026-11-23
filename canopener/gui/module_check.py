"""The missing-module check (``core.roster``) as a banner: what's on the bus against what the
profile says the machine has. The Network tab always shows it; the Codes tab only when a module is
missing or went quiet, since that's where a bare 'communication error' code sends people."""

from ..core import roster
from . import themes
from .qt import QtGui, QtWidgets
from .widgets import STANDARD_STATUS_COLORS

EDGE = {"bad": "bad", "ok": "ok", "banner": "wait"}         # banner kind -> status colour of its edge


class ModuleCheck(QtWidgets.QFrame):
    def __init__(self, only_problems: bool = False, link: str = "", parent=None):
        super().__init__(parent)
        self.only_problems = only_problems
        self.link = link                    # e.g. "Network tab": a link after the summary
        self.kind = "banner"
        self.roster: roster.Roster | None = None
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        self.label = QtWidgets.QLabel()
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)
        self.linkActivated = self.label.linkActivated
        self.refresh_theme()
        self.hide()

    def refresh_theme(self):
        bg, fg = themes.tint(self.kind)
        role = EDGE[self.kind]
        edge = themes.color(role) or QtGui.QColor(STANDARD_STATUS_COLORS[role])
        self.setStyleSheet(f"QFrame {{ background: {bg.name()}; border: 1px solid {edge.name()}; "
                           f"border-radius: 4px; }} QLabel {{ border: none; color: {fg.name()}; }}")

    def show_roster(self, r: roster.Roster | None):
        """Show ``r`` (None, or nothing to say: hidden)."""
        self.roster = r
        if r is None or (not r.modules and not r.bus_quiet) or (self.only_problems and not r.problems):
            self.hide()
            return
        kind = "bad" if r.problems else "ok" if r.has_list and not r.listening and not r.bus_quiet else "banner"
        if kind != self.kind:
            self.kind = kind
            self.refresh_theme()
        text = "<b>Modules:</b> " + _html(r.summary())
        if self.link:
            text += f" <a href='modules'>{_html(self.link)}</a>"
        hint = "" if self.only_problems else r.hint()
        if hint:
            text += "<br><small>" + _html(hint) + "</small>"
        if text != self.label.text():
            self.label.setText(text)
        self.show()


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
