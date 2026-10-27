"""Screens, soft keys, row lists and dialogs: the pieces every page is built from.

A ``Screen`` draws into the content area and handles actions (``device/input.py``). The app
draws the chrome around it: the soft-key labels along the hardware buttons' edge, and the status
strip (back / home, title, connection lamps, REC, clock) on the other.
"""

from dataclasses import dataclass

import pygame

from .. import input as inp
from ..ui import UI, ListView


@dataclass
class SoftKey:
    label: str
    fn: object = None
    enabled: bool = True
    active: bool = False        # drawn highlighted (a mode that's on)


class Screen:
    title = ""
    modal = False               # dialogs: drawn over the screen underneath

    def __init__(self, app):
        self.app = app
        self.ctx = app.ctx

    def softkeys(self) -> list:
        return []

    def on_action(self, action: str, n: int = 1) -> bool:
        return False

    def draw(self, ui: UI, rect: pygame.Rect):
        pass

    def on_enter(self):
        pass

    def on_leave(self):
        pass


# ----------------------------------------------------------------------------
# Row lists (settings, connection setup, anything that's "label: value")
# ----------------------------------------------------------------------------
@dataclass
class Row:
    label: str
    value: object = ""          # text, or a function returning it
    select: object = None       # Select / tap
    adjust: object = None       # adjust(+1 / -1): left / right on the d-pad
    kind: str = "row"           # row / header / info (not selectable) / action (a button-like row)
    check: object = None        # function -> bool: draw a checkbox
    level: object = None        # function -> lamp level: draw a status dot
    hint: str = ""              # small second line

    def value_text(self) -> str:
        v = self.value() if callable(self.value) else self.value
        return "" if v is None else str(v)

    @property
    def selectable(self) -> bool:
        return self.kind in ("row", "action") and (self.select is not None or self.adjust is not None)


class RowList:
    def __init__(self, rows_fn):
        self.rows_fn = rows_fn
        self.view = ListView()
        self.rows: list[Row] = []

    def refresh(self) -> list[Row]:
        self.rows = self.rows_fn()
        return self.rows

    def focused(self) -> Row | None:
        if 0 <= self.view.focus < len(self.rows):
            return self.rows[self.view.focus]
        return None

    def on_action(self, action: str, n: int = 1) -> bool:
        rows = self.refresh()
        sel = lambda i: rows[i].selectable  # noqa: E731
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            delta = n if action == inp.ROTATE else (-1 if action == inp.UP else 1)
            self.view.move(delta, len(rows), sel)
            return True
        row = self.focused()
        if row is None or not row.selectable:
            return False
        if action == inp.SELECT and row.select is not None:
            row.select()
            return True
        if action in (inp.LEFT, inp.RIGHT) and row.adjust is not None:
            row.adjust(-1 if action == inp.LEFT else 1)
            return True
        return False

    def draw(self, ui: UI, rect):
        rows = self.refresh()
        t = ui.theme

        def draw_row(ui: UI, i, r, focused):
            row = rows[i]
            if row.kind == "header":
                ui.text(row.label.upper(), (r.x + t.pad, r.bottom - t.pad), "small", "accent", valign="center")
                return
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            if row.kind != "info":
                ui.fill(inner, "panel2" if row.kind == "action" else "panel", t.radius)
            if focused and row.selectable:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            x = inner.x + t.pad
            if row.check is not None:
                box = pygame.Rect(0, 0, t.touch * .5, t.touch * .5)
                box.midleft = (x, inner.centery)
                ui.checkbox(box, bool(row.check()))
                x = box.right + t.pad
            if row.level is not None:
                lvl = row.level()
                pygame.draw.circle(ui.surface, ui.color(lvl if lvl in ("ok", "wait", "bad", "sim", "idle") else "off"),
                                   (x + t.pad // 2, inner.centery), t.pad // 1.5)
                x += t.pad * 2
            value = row.value_text()
            vw = ui.theme.font("body").size(value)[0] if value else 0
            vw = min(vw, inner.w // 2)
            label_w = inner.right - x - vw - 3 * t.pad
            if row.hint:
                ui.text(row.label, (x, inner.y + inner.h * .34), "body", "text", max_w=label_w)
                ui.text(row.hint, (x, inner.y + inner.h * .74), "small", "dim", max_w=inner.right - x - t.pad)
            else:
                ui.text(row.label, (x, inner.centery), "body", "text" if row.kind != "info" else "dim",
                        max_w=label_w)
            if value:
                color = "accent" if row.adjust is not None or row.select is not None else "text"
                ui.text(value, (inner.right - t.pad, inner.centery if not row.hint else inner.y + inner.h * .34),
                        "body", color, align="right", max_w=inner.w // 2)

        def tap(i):
            row = rows[i]
            if row.select is not None:
                row.select()
            elif row.adjust is not None:
                row.adjust(1)

        self.view.draw(ui, rect, len(rows), t.row, draw_row, tap, lambda i: rows[i].selectable)


def cycle(options: list, current, delta: int):
    """The option ``delta`` steps from ``current`` (wrapping) in a list of (label, value)."""
    values = [v for _, v in options]
    i = values.index(current) if current in values else 0
    return values[(i + delta) % len(values)]


def label_of(options: list, value, default: str = "") -> str:
    return next((lb for lb, v in options if v == value), default or str(value))


# ----------------------------------------------------------------------------
# Dialogs
# ----------------------------------------------------------------------------
class Dialog(Screen):
    modal = True

    def panel(self, ui: UI, rect, w_frac=0.86, h_frac=0.86) -> pygame.Rect:
        shade = pygame.Surface(rect.size, pygame.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        ui.surface.blit(shade, rect.topleft)
        ui.hit(rect, lambda: None)                  # swallow taps outside the panel
        p = pygame.Rect(0, 0, rect.w * w_frac, rect.h * h_frac)
        p.center = rect.center
        ui.fill(p, "panel", ui.theme.radius * 2)
        ui.fill(p, "line", ui.theme.radius * 2, 2)
        return p


class ConfirmDialog(Dialog):
    """'Are you sure?' with No as the default (encoder push on arrival = No)."""

    def __init__(self, app, title: str, text: str, yes_label: str, on_yes, danger: bool = True):
        super().__init__(app)
        self.title, self.text, self.yes_label, self.on_yes, self.danger = title, text, yes_label, on_yes, danger
        self.focus = 0          # 0 = No, 1 = Yes

    def _answer(self, yes: bool):
        self.app.pop(self)
        if yes:
            self.on_yes()

    def softkeys(self):
        return [SoftKey("No", lambda: self._answer(False)), None, None,
                SoftKey(self.yes_label, lambda: self._answer(True))]

    def on_action(self, action, n=1):
        if action in (inp.ROTATE, inp.LEFT, inp.RIGHT, inp.UP, inp.DOWN):
            self.focus = 1 - self.focus if action != inp.ROTATE or n % 2 else self.focus
            return True
        if action == inp.SELECT:
            self._answer(self.focus == 1)
            return True
        if action == inp.BACK:
            self._answer(False)
            return True
        return False

    def draw(self, ui, rect):
        t = ui.theme
        long = len(self.text) > 200             # a full warning (clearing codes): use the whole screen
        p = self.panel(ui, rect, 0.94, 0.96) if long else self.panel(ui, rect, 0.8, 0.8)
        ui.text(self.title, (p.x + t.pad * 2, p.y + t.pad * 2), "big", "text", valign="top", max_w=p.w - 4 * t.pad)
        body = pygame.Rect(p.x + t.pad * 2, p.y + t.touch * 1.1, p.w - 4 * t.pad, p.h - t.touch * 2.6)
        font = "body"
        if len(ui.wrap(self.text, font, body.w)) * t.font(font).get_linesize() > body.h:
            font = "small"                      # never cut a warning short: smaller text instead
        ui.paragraph(self.text, body, font, "text")
        bw = (p.w - 5 * t.pad) // 2
        no = pygame.Rect(p.x + t.pad * 2, p.bottom - t.touch - t.pad * 2, bw, t.touch)
        yes = pygame.Rect(no.right + t.pad, no.y, bw, t.touch)
        ui.button(no, "No", lambda: self._answer(False), focused=self.focus == 0)
        ui.button(yes, self.yes_label, lambda: self._answer(True), focused=self.focus == 1,
                  color="bad" if self.danger else "accent")


class ChoiceDialog(Dialog):
    """Pick one of a list of (label, value). Back cancels."""

    def __init__(self, app, title: str, options: list, current, on_pick):
        super().__init__(app)
        self.title, self.options, self.on_pick = title, options, on_pick
        self.view = ListView()
        values = [v for _, v in options]
        self.view.focus = values.index(current) if current in values else 0
        self.current = current

    def _pick(self, i: int):
        self.app.pop(self)
        self.on_pick(self.options[i][1])

    def softkeys(self):
        return [SoftKey("Cancel", lambda: self.app.pop(self))]

    def on_action(self, action, n=1):
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1), len(self.options))
            return True
        if action == inp.SELECT and self.options:
            self._pick(self.view.focus)
            return True
        if action == inp.BACK:
            self.app.pop(self)
            return True
        return False

    def draw(self, ui, rect):
        t = ui.theme
        p = self.panel(ui, rect, 0.7, 0.92)
        ui.text(self.title, (p.x + t.pad * 2, p.y + t.touch * .5), "big", "text", max_w=p.w - 4 * t.pad)
        area = pygame.Rect(p.x + t.pad, p.y + t.touch, p.w - 2 * t.pad, p.h - t.touch - t.pad)

        def draw_row(ui, i, r, focused):
            label, value = self.options[i]
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            ui.fill(inner, "accent" if value == self.current else "panel2", t.radius)
            if focused:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            ui.text(label, (inner.x + t.pad, inner.centery), "body", "text", max_w=inner.w - 2 * t.pad)

        self.view.draw(ui, area, len(self.options), t.row, draw_row, self._pick)


class InfoDialog(Dialog):
    """Text to read (a fault code's details, about, an error), with optional extra soft keys."""

    def __init__(self, app, title: str, lines: list[str], keys: list | None = None):
        super().__init__(app)
        self.title, self.lines, self.keys = title, lines, keys or []
        self.view = ListView()

    def softkeys(self):
        return [SoftKey("Close", lambda: self.app.pop(self))] + list(self.keys)

    def on_action(self, action, n=1):
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1))
            return True
        if action in (inp.BACK, inp.SELECT):
            self.app.pop(self)
            return True
        return False

    def draw(self, ui, rect):
        t = ui.theme
        p = self.panel(ui, rect, 0.92, 0.94)
        ui.text(self.title, (p.x + t.pad * 2, p.y + t.touch * .5), "big", "text", max_w=p.w - 4 * t.pad)
        area = pygame.Rect(p.x + t.pad * 2, p.y + t.touch, p.w - 4 * t.pad, p.h - t.touch - t.pad)
        wrapped = [w for line in self.lines for w in (ui.wrap(line, "body", area.w - t.pad) or [""])]
        lh = t.font("body").get_linesize() + 2

        def draw_row(ui, i, r, focused):
            ui.text(wrapped[i], (r.x, r.centery), "body", "text")

        self.view.draw(ui, area, len(wrapped), lh, draw_row)


def choose(app, title: str, options: list, current, on_pick):
    app.push(ChoiceDialog(app, title, options, current, on_pick))


def confirm(app, title: str, text: str, yes_label: str, on_yes, danger: bool = True):
    app.push(ConfirmDialog(app, title, text, yes_label, on_yes, danger))
