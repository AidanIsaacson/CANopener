"""Diagnostics on the handheld (System > Installed options): run the checks for one part or for
everything fitted, read the output, save it, copy it to a USB stick.

The checks themselves are ``core.diagnostics`` (shared with ``canopener-cli hardware --diagnose``);
they run on a worker thread, so a slow command never freezes the screen.
"""

import os

import pygame

from ... import __version__
from ...core import diagnostics, hardware, storage
from .. import input as inp
from ..ui import ListView
from .base import Row, RowList, Screen, SoftKey, choose


def run(app, option_ids: list[str], title: str):
    """Run the checks on a worker thread, then show the report."""
    ctx = app.ctx

    def work():
        results = diagnostics.run(option_ids, ctx.hardware)
        return diagnostics.report(results, ctx.hardware, __version__)
    ctx.run_job("Running diagnostics", work, lambda text: app.push(ReportScreen(app, title, text)))


def fitted(hw) -> list[str]:
    """The options set to Installed or Use if found."""
    return [o.id for o in hardware.OPTIONS if hw.state(o.id) != hardware.NO]


class DiagnosticsScreen(Screen):
    title = "Diagnostics"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _rows(self):
        hw = self.ctx.hardware
        rows = [Row("Run checks", kind="header"),
                Row("Everything fitted", lambda: f"{len(fitted(hw))} part(s)", select=self.run_fitted,
                    hint="every part set to Installed or Use if found")]
        for opt in hardware.OPTIONS:
            st = hw.status(opt.id)
            rows.append(Row(opt.name, select=lambda opt=opt: run(self.app, [opt.id], opt.name),
                            level=lambda opt=opt: hw.status(opt.id).level,
                            hint=f"{st.text}: {st.detail}" if st.detail else st.text))
        reports = diagnostics.saved_reports()
        rows.append(Row("Saved reports", kind="header"))
        if not reports:
            rows.append(Row("None saved yet", kind="info", hint=f"Save puts them in "
                            f"{os.path.join('~', 'canopener', diagnostics.DIAG_DIR)}"))
        for path in reversed(reports[-10:]):
            rows.append(Row(os.path.basename(path), select=lambda path=path: self._open(path), kind="menu"))
        return rows

    def run_fitted(self):
        ids = fitted(self.ctx.hardware)
        if not ids:
            self.ctx.toast("Nothing is set to Installed yet")
            return
        run(self.app, ids, "Everything fitted")

    def _open(self, path):
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError as e:
            self.ctx.toast(f"Can't open it: {e}")
            return
        self.app.push(ReportScreen(self.app, os.path.basename(path), text, path=path))

    def softkeys(self):
        return [SoftKey("Run all", self.run_fitted)]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class ReportScreen(Screen):
    """A diagnostics report to scroll through, with Save and Copy to USB."""

    def __init__(self, app, title: str, text: str, path: str | None = None):
        super().__init__(app)
        self.title = title
        self.text = text
        self.path = path            # where it's saved (None: not yet)
        self.view = ListView()
        self._wrapped: list[tuple[str, str]] | None = None
        self._width = 0

    def save(self) -> str | None:
        if self.path is None:
            try:
                self.path = diagnostics.save(self.text)
            except OSError as e:
                self.ctx.toast(f"Couldn't save: {e}")
                return None
            self.ctx.toast(f"Saved: {self.path.replace(os.path.expanduser('~'), '~')}")
        else:
            self.ctx.toast(f"Already saved: {os.path.basename(self.path)}")
        return self.path

    def copy_to_usb(self):
        path = self.save()
        if path is None:
            return
        drives = storage.removable_drives()
        if not drives:
            self.ctx.toast("No USB stick found. Plug one in and try again.")
            return
        ctx = self.ctx

        def copy(dest):
            ctx.run_job("Copying to USB", lambda: storage.export_items([path], dest),
                        lambda r: ctx.toast(f"Copied to {r[0]}"))
        choose(self.app, "Copy the report to", [(d.describe(), d.path) for d in drives], None, copy)

    def softkeys(self):
        return [SoftKey("Saved" if self.path else "Save", self.save, active=self.path is not None),
                SoftKey("Copy to USB", self.copy_to_usb)]

    def on_action(self, action, n=1):
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n * 3 if action == inp.ROTATE else (-3 if action == inp.UP else 3))
            return True
        if action in (inp.LEFT, inp.RIGHT):
            page = max(1, len(self._wrapped or []) // 20)
            self.view.move(-page if action == inp.LEFT else page)
            return True
        return False

    def _wrap(self, ui, width: int) -> list[tuple[str, str]]:
        """(line, colour) per screen line: headings stand out, the reasons are dimmer."""
        if self._wrapped is None or width != self._width:
            out = []
            for line in self.text.splitlines():
                color = ("accent" if line.startswith(("---", "===")) or (out and out[-1][0].startswith("===")) else
                         "dim" if line.startswith("#") else "text")
                for w in ui.wrap(line, "small", width) or [""]:
                    out.append((w, color))
            self._wrapped, self._width = out, width
        return self._wrapped

    def draw(self, ui, rect):
        t = ui.theme
        area = pygame.Rect(rect.x + t.pad, rect.y, rect.w - 2 * t.pad, rect.h)
        lines = self._wrap(ui, area.w - t.pad)
        lh = t.font("small").get_linesize() + 1

        def draw_row(ui, i, r, focused):
            text, color = lines[i]
            ui.text(text, (r.x, r.centery), "small", color)

        self.view.draw(ui, area, len(lines), lh, draw_row)
