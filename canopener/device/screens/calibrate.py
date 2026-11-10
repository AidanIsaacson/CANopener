"""Meter calibration on the handheld (System > Meters > Calibration).

``CalibrationScreen`` lists the voltage channels with their saved correction and starts a run;
``CalRunScreen`` walks through ``core.calibration.CalSession`` one channel after the other, so the
user goes zero, voltage, save, next channel without going back to a menu. Full checks each
channel against a second meter (hold the reading, then accept it or turn the encoder to match);
Quick takes the supply voltage once and does the rest by itself, with a beep at each step.
"""

from collections import deque

import pygame

from ...core import calibration as calib
from .. import input as inp
from .base import Row, RowList, Screen, SoftKey, choose, confirm


def channel_labels(channels) -> list[tuple[object, str]]:
    """(channel, "CH1") for each calibratable channel, numbered in the order the meter lists them."""
    return [(ch, f"CH{i + 1}") for i, ch in enumerate(channels)]


def summary(ctx) -> str:
    chans = calib.calibratable(ctx.hub.channels())
    if not chans:
        return ""
    done = sum(1 for c in chans if ctx.calibration.get(c.id) is not None)
    return f"{done} of {len(chans)} calibrated"


class CalibrationScreen(Screen):
    title = "Calibration"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _channels(self):
        return channel_labels(calib.calibratable(self.ctx.hub.channels()))

    def _rows(self):
        ctx = self.ctx
        chans = self._channels()
        if not chans:
            return [Row("No meter channels", kind="info",
                        hint="Switch a meter on in System > Meters (or the simulated one, to try this)."),
                    Row("Meters", select=self._meters, kind="menu")]
        rows = [
            Row("Calibrate", kind="header"),
            Row("Quick: all channels", select=lambda: self.start(calib.QUICK),
                hint="type the supply voltage once, then touch COM and the supply with each probe"),
            Row("Full: all channels", select=lambda: self.start(calib.FULL),
                hint="check each channel against a second meter; adjust with the encoder"),
            Row("Channels", kind="header"),
        ]
        for ch, label in chans:
            rows.append(Row(label, lambda ch=ch: self._value(ch),
                            select=lambda ch=ch, label=label: self._channel(ch, label), hint=self._hint(ch)))
        if ctx.calibration.channels():
            rows.append(Row("Reset all channels", select=self._reset_all, hint="back to the raw readings"))
        return rows

    def _value(self, ch) -> str:
        c = self.ctx.calibration.get(ch.id)
        return f"gain {c.gain:.4f}" if c else "not calibrated"

    def _hint(self, ch) -> str:
        c = self.ctx.calibration.get(ch.id)
        if c is None:
            return ch.name
        parts = [ch.name, f"zero {c.offset:+.3f} V"]
        if c.ref is not None:
            parts.append(f"set at {c.ref:.2f} V")
        if c.when:
            parts.append(c.when)
        return " · ".join(parts)

    def _meters(self):
        from .settings import SettingsScreen
        self.app.push(SettingsScreen(self.app, section="Meters"))

    def start(self, mode: str, only=None):
        chans = self._channels()
        if only is not None:
            chans = [(ch, label) for ch, label in chans if ch.id == only]
        if chans:
            self.app.push(CalRunScreen(self.app, chans, mode))

    def _channel(self, ch, label):
        def picked(what):
            if what == "cal":
                self.start(calib.FULL, only=ch.id)
            elif what == "quick":
                self.start(calib.QUICK, only=ch.id)
            elif what == "reset":
                self.ctx.calibration.reset(ch.id)
                self.ctx.toast(f"{label} back to uncalibrated")
        options = [("Calibrate (full)", "cal"), ("Calibrate (quick)", "quick")]
        if self.ctx.calibration.get(ch.id) is not None:
            options.append(("Reset to uncalibrated", "reset"))
        choose(self.app, f"{label} · {ch.name}", options, None, picked)

    def _reset_all(self):
        confirm(self.app, "Reset all channels?", "Every channel goes back to its raw reading (no zero offset, "
                "gain 1). Calibrate again afterwards.", "Reset", lambda: (self.ctx.calibration.reset(),
                                                                         self.ctx.toast("Calibration reset")))

    def softkeys(self):
        if not self._channels():
            return []
        return [SoftKey("Quick", lambda: self.start(calib.QUICK)), SoftKey("Full", lambda: self.start(calib.FULL))]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class CalRunScreen(Screen):
    """One run of the calibration flow. Readings arrive on the sources' threads (raw, before any
    correction) and are handed to the session once a frame, so the session itself stays
    single-threaded."""

    def __init__(self, app, channels: list, mode: str):
        super().__init__(app)
        self.chans = channels
        self.names = {ch.id: ch.name for ch, _ in channels}
        self.title = "Quick" if mode == calib.QUICK else "Full"
        self.session = calib.CalSession(self.ctx.calibration, [(ch.id, label) for ch, label in channels], mode,
                                        ref=self.ctx.settings.get(calib.REF_SETTING))
        self._q: deque = deque(maxlen=500)
        self._ids = set(self.names)

    # --- readings ---
    def _raw(self, sample):
        if sample.channel in self._ids:
            self._q.append(sample)

    def on_enter(self):
        self.ctx.hub.add_listener(self._raw, raw=True)

    def on_leave(self):
        self.ctx.hub.remove_listener(self._raw)

    def pump(self):
        """Feed the waiting readings, move time-driven steps on, play the sounds."""
        s = self.session
        cur = s.current.channel_id if s.current else None
        while self._q:
            sample = self._q.popleft()
            if sample.channel == cur:
                s.feed(sample.t, sample.value)
        s.update()
        for ev in s.pop_events():
            self.ctx.cue(ev)

    # --- input ---
    def _select(self):
        s = self.session
        if s.step == calib.DONE:
            self._leave()
            return
        if s.step == calib.REF:
            self.ctx.settings.set(calib.REF_SETTING, s.ref)
        s.select()
        for ev in s.pop_events():
            self.ctx.cue(ev)

    def _leave(self):
        saved = [c.label for c in self.session.saved]
        if self.session.step != calib.DONE:
            self.ctx.toast(f"Calibration stopped: {', '.join(saved)} saved" if saved
                           else "Calibration stopped: nothing changed")
        self.app.pop(self)

    def softkeys(self):
        s = self.session
        label = s.current.label if s.current else ""
        step = SoftKey(f"Step {s.step_size:g} V", s.cycle_step)
        stop = SoftKey("Stop", self._leave)
        if s.step == calib.REF:
            return [step, None, stop, SoftKey("Start", self._select)]
        if s.step == calib.ZERO:
            return [SoftKey(f"Skip {label}", s.skip), None, stop,
                    SoftKey("Zero", self._select) if s.mode == calib.FULL else None]
        if s.step == calib.SPAN:
            main = SoftKey("Hold", self._select) if s.mode == calib.FULL else SoftKey("Use it", self._select)
            return [SoftKey(f"Skip {label}", s.skip), SoftKey("Redo zero", s.redo), stop, main]
        if s.step == calib.CONFIRM:
            return [SoftKey("Retake", s.back), SoftKey("Adjust", s.choose_adjust), stop,
                    SoftKey("Looks right", lambda: (setattr(s, "confirm_focus", 0), self._select()))]
        if s.step == calib.ADJUST:
            return [step, SoftKey("Retake", lambda: (s.back(), s.back())), stop, SoftKey("Save", self._select)]
        if s.step == calib.SAVED:
            return [None, None, stop, SoftKey("Next", self._select)]
        return [None, None, None, SoftKey("Done", self._leave)]

    def on_action(self, action, n=1):
        s = self.session
        if action == inp.BACK:
            if not s.back():
                self._leave()
            return True
        if action == inp.SELECT:
            self._select()
            return True
        if s.step in (calib.REF, calib.ADJUST):
            if action == inp.ROTATE:
                s.adjust(n)
            elif action in (inp.RIGHT, inp.UP):
                s.adjust(1)
            elif action in (inp.LEFT, inp.DOWN):
                s.adjust(-1)
            else:
                return False
            return True
        if s.step == calib.CONFIRM and action in (inp.ROTATE, inp.LEFT, inp.RIGHT, inp.UP, inp.DOWN):
            if action != inp.ROTATE or n % 2:
                s.toggle_confirm()
            return True
        return action in (inp.ROTATE, inp.UP, inp.DOWN, inp.LEFT, inp.RIGHT)     # nothing to move here

    # --- drawing ---
    def _texts(self) -> tuple[str, str]:
        s = self.session
        c = s.current
        label = c.label if c else ""
        quick = s.mode == calib.QUICK
        if s.step == calib.REF:
            return ("Supply voltage", "Measure the supply with your second meter and set the same value here "
                    "(turn the encoder). Every channel is calibrated against it.")
        if s.step == calib.ZERO:
            how = "It's taken by itself once the reading settles." if quick else "Push when the reading settles."
            return (f"{label}: touch the probe to COM", f"Hold {label}'s probe on the COM lead (or on the supply's "
                    f"minus, with COM clipped there). {how}")
        if s.step == calib.SPAN:
            if quick:
                how = f"It's saved by itself once it reads close to {s.ref:.2f} V."
            else:
                how = "Push to hold the reading, then check it against your second meter."
            return f"{label}: touch the supply", f"{label} on the supply's plus, COM on its minus. {how}"
        if s.step == calib.CONFIRM:
            return (f"{label} reads {s.value:.3f} V", "Measure the supply with your second meter. "
                    "Does it match? Turn the encoder to choose, push to go.")
        if s.step == calib.ADJUST:
            return (f"{label}: match your meter", f"Turn the encoder until this reads what your second meter "
                    f"does ({s.step_size:g} V a click; soft key 1 changes the step). Push to save.")
        if s.step == calib.SAVED:
            nxt = next((ch.label for ch in s.channels[s.index + 1:] if ch.state == "pending"), None)
            cal = c.cal.describe() if c and c.cal else ""
            return f"{label} saved", cal + (f". Next: {nxt}" if nxt else "")
        lines = []
        for ch in s.channels:
            lines.append(f"{ch.label}: {ch.cal.describe()}" if ch.state == "saved" else f"{ch.label}: {ch.state}")
        return "Calibration done", "\n".join(lines)

    def _readout(self):
        """(big number text, steadiness level, caption) for the step."""
        s = self.session
        if s.step == calib.REF:
            return f"{s.ref:.3f} V", None, "supply voltage"
        if s.step in (calib.ZERO, calib.SPAN):
            v = s.shown(s.live)
            if v is None:
                return "--- V", "bad", "no reading"
            return f"{v:.3f} V", ("ok" if s.steady.steady else "wait"), ("settled" if s.steady.steady else "settling")
        if s.step in (calib.CONFIRM, calib.ADJUST):
            return f"{s.value:.3f} V", None, "held reading" if s.step == calib.CONFIRM else "adjusting"
        if s.step == calib.SAVED and s.current and s.current.cal and s.current.cal.ref is not None:
            return f"{s.current.cal.ref:.3f} V", "ok", "saved"
        return "", None, ""

    def draw(self, ui, rect):
        self.pump()
        t, s = ui.theme, self.session
        # channel progress along the top
        h = round(t.touch * .62)
        n = max(1, len(s.channels))
        w = min(round(t.touch * 3.2), (rect.w - t.pad * (n - 1)) // n)
        x = rect.x
        for i, ch in enumerate(s.channels):
            level = {"saved": "ok", "current": "wait", "skipped": "off"}.get(ch.state, "off")
            text = ch.label + (" saved" if ch.state == "saved" else " skipped" if ch.state == "skipped" else "")
            r = pygame.Rect(x, rect.y, w, h)
            ui.pill(r, level, text)
            if i == s.index and s.step != calib.DONE:
                ui.fill(r, "focus", h // 2, max(2, t.pad // 4))
            x += w + t.pad
        y = rect.y + h + t.pad
        heading, body = self._texts()
        ui.text(heading, (rect.x + t.pad, y), "big", "text", valign="top", max_w=rect.w - 2 * t.pad)
        y += t.font("big").get_linesize() + t.pad // 2
        if s.step == calib.DONE:
            ui.paragraph(body, pygame.Rect(rect.x + t.pad, y, rect.w - 2 * t.pad, rect.bottom - y), "body", "text")
            return
        used = ui.paragraph(body, pygame.Rect(rect.x + t.pad, y, rect.w - 2 * t.pad, t.font("body").get_linesize() * 3),
                            "body", "dim")
        y += used + t.pad
        # the big number, in a card
        big, level, caption = self._readout()
        bottom = rect.bottom - (t.touch + t.pad * 2 if s.step == calib.CONFIRM else t.pad)
        if s.problem:
            bottom -= t.font("body").get_linesize() * 2 + t.pad
        card = pygame.Rect(rect.x + t.pad, y, rect.w - 2 * t.pad, max(t.touch, bottom - y))
        ui.card(card, "panel")
        if s.step == calib.ADJUST:
            ui.fill(card, "focus", t.radius, max(2, t.pad // 3))
        ui.text(big, (card.centerx, card.centery - t.pad), "huge", "text", align="center", max_w=card.w - 2 * t.pad)
        if caption:
            cap = pygame.Rect(0, 0, min(card.w - 2 * t.pad, round(t.touch * 3)), round(t.touch * .55))
            cap.midbottom = (card.centerx, card.bottom - t.pad // 2)
            if level:
                ui.pill(cap, level, caption)
            else:
                ui.text(caption, cap.center, "small", "dim", align="center")
        y = card.bottom + t.pad
        if s.problem:
            ui.paragraph(s.problem, pygame.Rect(rect.x + t.pad, y, rect.w - 2 * t.pad,
                                                t.font("body").get_linesize() * 2), "body", "warn")
            y += t.font("body").get_linesize() * 2 + t.pad
        if s.step == calib.CONFIRM:
            bw = (rect.w - 3 * t.pad) // 2
            ok = pygame.Rect(rect.x + t.pad, rect.bottom - t.touch - t.pad, bw, t.touch)
            adj = pygame.Rect(ok.right + t.pad, ok.y, bw, t.touch)
            ui.button(ok, "Looks right", lambda: (setattr(s, "confirm_focus", 0), self._select()),
                      focused=s.confirm_focus == 0, color="accent")
            ui.button(adj, "Adjust", s.choose_adjust, focused=s.confirm_focus == 1)
