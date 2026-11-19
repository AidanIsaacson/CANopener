"""Control mapping: every physical input, live, and what it does. Fix wiring mistakes in software.

One row per input that's installed: the button ADC's stacks (A1-A3, grouped by input, with the
voltage each reads and the button it decodes), or the buttons on GPIO, then the encoder. A lit dot
is a button down right now. Select a button to give it another job (F1 and F3 wired the wrong
way round? swap them here), to ignore it, or to take its voltage as its level. Select a stack's
input row to switch the whole stack off while it isn't wired.

While **Testing** is on (it is when the page opens), the hardware only lights up here: a stack
sending presses on its own can't drive the screens, and touch still works everything. The model is
``device/input.py`` (``ButtonAdc``, ``stack_actions``, ``stack_levels``); the mapping lives in
``device.button_map`` / ``device.button_tuning`` / ``device.stacks_off`` / ``device.keys`` /
``device.encoder_reverse`` (settings.json).
"""

import time
from collections import deque

from ...core import bootconfig
from .. import input as inp
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm

LIT_S = 0.4                 # the encoder's dot stays lit this long after a step
SLOW_PASS_MS = 40           # a pass longer than this makes presses feel late
LOG_ROWS = 6
HELP = ["Every button and the encoder, live. A lit dot is a button down right now.",
        "", "While Testing is on, presses only light up here (they don't do anything), so you can test "
            "freely. Touch still works everything. Tap Testing to use the buttons normally.",
        "", "Select a button to change what it does (wired F1 and F3 the wrong way round? Give each the "
            "other's job), to ignore it, or to take the voltage it reads now as its level (hold it down "
            "with one hand while you tap).",
        "", "With the button ADC, each input row shows the voltage it reads: about 3.3 V with nothing "
            "pressed. A voltage that wanders with nothing pressed means the stack isn't wired, or its "
            "pull-up is missing. The handheld ignores a stack that sends presses on its own; select its "
            "input row to switch it off until it's wired.",
        "", "Direction reverses the encoder, if A and B are swapped."]

GPIO_KEY_NAMES = {"F1": "f1", "F2": "f2", "F3": "f3", "F4": "f4", "F5": "f5", "UP": "up", "DOWN": "down",
                  "LEFT": "left", "RIGHT": "right", "SELECT": "return"}


def status(ctx) -> str:
    return "Test and assign buttons"


class ControlMapScreen(Screen):
    title = "Control mapping"

    def __init__(self, app):
        super().__init__(app)
        self.testing = True
        self.keys_down: set[str] = set()
        self.key_log: deque = deque(maxlen=inp.LOG_KEPT)   # (time, key name, "down" / "up")
        self.steps = 0
        self.stepped_at = -1e9
        self.rows = RowList(self._rows)

    # --- what the app hands over while testing ---
    def capture(self, kind: str, *what):
        now = time.monotonic()
        if kind == "key":
            name, down = what
            (self.keys_down.add if down else self.keys_down.discard)(name)
            self.key_log.append((now, name, "down" if down else "up"))
        elif kind == "encoder":
            self.steps += what[0]
            self.stepped_at = now

    def on_leave(self):
        self.keys_down.clear()

    def softkeys(self):
        return [SoftKey("Testing", self._toggle_testing, active=self.testing),
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, "Control mapping", HELP))),
                SoftKey("Reset", self._reset)]

    def _toggle_testing(self):
        self.testing = not self.testing
        self.keys_down.clear()
        self.ctx.toast("Testing: the buttons only light up here (touch works everything)" if self.testing else
                       "Testing off: the buttons work normally")

    # --- rows ---
    def _rows(self):
        hw = self.ctx.hardware
        rows = [Row("Testing" if self.testing else "Testing off", kind="info",
                    level=lambda: "sim" if self.testing else "idle",
                    hint="presses only light up here; touch works everything" if self.testing
                    else "the buttons work normally: tap Testing to try them safely")]
        adc = hw.available("button_adc")
        if adc:
            rows += self._stack_rows()
        if hw.state("controls") != "no" and not adc:
            rows += self._gpio_rows()
        if not adc and hw.state("controls") == "no":
            rows.append(Row("No buttons installed", kind="info", level=lambda: "off",
                            hint="System > Installed options: Buttons and encoder, and the button ADC"))
        rows += self._encoder_rows()
        rows += self._log_rows()
        return rows

    def _stack_rows(self):
        b = self.app.buttons
        hw = self.ctx.hardware
        if b is None:
            st = hw.status("button_adc")
            return [Row("Button ADC", kind="header"),
                    Row(st.text, kind="info", level=lambda: "bad", hint=st.detail or "looking for it every 5 s")]
        rows = [Row(f"Presses in the last 5 s: {b.presses()}", kind="info",
                    level=lambda: "bad" if b.flooding else "idle",
                    hint="a big number with nothing pressed is a stack wired wrong" if not b.flooding else
                    "ignored: " + ", ".join(f"A{c}" for c in sorted(b.flooding)))]
        if b.pass_s is not None:
            ms = b.pass_s * 1000
            rows.append(Row(f"Reads every stack every {max(ms, b.period * 1000):.0f} ms", kind="info",
                            level=lambda: "bad" if b.slow or ms > SLOW_PASS_MS else "ok",
                            hint=("slow inputs: " + ", ".join(f"A{c} at {b.rates[c]} a second" for c in b.slow)
                                  + " (install.sh --device fixes it)") if b.slow else
                            "a press shows within two reads"))
        for ch in sorted(b.stacks):
            lad = b.stacks[ch]
            rows.append(Row(f"A{ch}: {inp.STACK_NAMES.get(ch, '')}", kind="header"))
            rows.append(Row(f"Input A{ch}", lambda ch=ch, lad=lad: self._input_text(b, ch, lad),
                            select=lambda ch=ch: self._toggle_stack(ch),
                            level=lambda ch=ch, lad=lad: self._input_level(b, ch, lad),
                            hint="on: select to switch this stack off" if ch not in b.muted
                            else "off: select to switch it back on"))
            tuning = self.ctx.settings.get("device.button_tuning") or {}
            for i, level in enumerate(lad.levels):
                pos = i + 1
                tuned = inp.pos_key(ch, pos) in tuning
                rows.append(Row(f"Button {pos}", lambda a=lad.actions[i]: self._action_text(a),
                                select=lambda ch=ch, pos=pos: self._edit_position(ch, pos),
                                level=lambda lad=lad, i=i: "ok" if lad.pressed == i else "idle",
                                hint=f"{level:.2f} V" + (" (taken here)" if tuned else "")))
        return rows

    def _input_text(self, b, ch, lad) -> str:
        v = lad.volts
        if v is None:
            return "no reading"
        state = ("off" if ch in b.muted else "ignored: presses on its own" if ch in b.flooding else
                 f"button {lad.pressed + 1}" if lad.pressed is not None else
                 "nothing pressed" if v >= lad.rest else "between buttons")
        return f"{v:.2f} V, {state}"

    @staticmethod
    def _input_level(b, ch, lad) -> str:
        if ch in b.muted:
            return "off"
        if ch in b.flooding:
            return "bad"
        return "ok" if lad.pressed is not None else "idle"

    def _gpio_rows(self):
        km = self.app.keymap
        rows = [Row("Buttons on GPIO", kind="header")]
        for gpio, _code, label in bootconfig.GPIO_KEYS:
            name = GPIO_KEY_NAMES[label]
            rows.append(Row(f"{label} (GPIO{gpio})", lambda name=name: self._action_text(km.keys.get(name, "")),
                            select=lambda name=name, label=label: self._edit_key(name, label),
                            level=lambda name=name: "ok" if name in self.keys_down else "idle"))
        others = sorted(k for k in self.keys_down if k not in GPIO_KEY_NAMES.values())
        if others:
            rows.append(Row("Other keys down", ", ".join(others), kind="info"))
        return rows

    def _encoder_rows(self):
        enc = self.app.encoder
        s = self.ctx.settings
        where = "not looked for" if enc is None else (enc.error or enc.path or "looking")
        return [Row("Encoder", kind="header"),
                Row("Turn it", lambda: f"{self.steps:+d} steps" if self.testing else "turns the focus",
                    level=lambda: "ok" if time.monotonic() - self.stepped_at < LIT_S else "idle",
                    select=self._zero_steps, hint=where),
                Row("Direction", lambda: "reversed" if s.get("device.encoder_reverse") else "normal",
                    select=lambda: s.set("device.encoder_reverse", not s.get("device.encoder_reverse")),
                    hint="clockwise should move down the list")]

    def _log_rows(self):
        now = time.monotonic()
        entries = []
        b = self.app.buttons
        if b is not None:
            for t, ch, pos, kind in list(b.log):
                acts = b.stacks[ch].actions if ch in b.stacks else []
                a = acts[pos - 1] if pos - 1 < len(acts) else ""
                entries.append((t, f"A{ch} button {pos} {kind}", self._action_text(a)))
        for t, name, kind in list(self.key_log):
            entries.append((t, f"{name} {kind}", self._action_text(self.app.keymap.keys.get(name, ""))))
        entries.sort(key=lambda e: e[0])
        rows = [Row("Last presses", kind="header")]
        if not entries:
            return rows + [Row("Nothing yet: press a button", kind="info")]
        for t, what, does in entries[-LOG_ROWS:][::-1]:
            rows.append(Row(what, f"{now - t:.1f} s ago", kind="info", hint=does))
        return rows

    # --- names ---
    def _action_text(self, a: str) -> str:
        if not a or a == "none":
            return "nothing (ignored)"
        if a == inp.BUTTON5:
            return f"Button 5: {inp.ACTION_NAMES.get(self.app.keymap.button5, '')}"
        return inp.ACTION_NAMES.get(a, a)

    def _choices(self) -> list:
        return [(self._action_text(a), a) for a in inp.ASSIGNABLE] + [("Nothing (ignore it)", "")]

    # --- changes ---
    def _toggle_stack(self, ch: int):
        s = self.ctx.settings
        off = [int(c) for c in (s.get("device.stacks_off") or [])]
        if ch in off:
            off.remove(ch)
            self.ctx.toast(f"A{ch} on again")
        else:
            off.append(ch)
            self.ctx.toast(f"A{ch} off: its buttons do nothing until you switch it back on here")
        s.set("device.stacks_off", sorted(off))

    def _edit_position(self, ch: int, pos: int):
        key = inp.pos_key(ch, pos)
        opts = [(f"Does: {label}", ("map", a)) for label, a in self._choices()]
        opts += [("Take its level now (hold the button down)", ("take", None)),
                 ("Back to the usual level", ("untune", None))]
        choose(self.app, f"A{ch} button {pos}", opts, None, lambda v: self._apply_position(key, ch, pos, *v))

    def _apply_position(self, key: str, ch: int, pos: int, what: str, action):
        s = self.ctx.settings
        if what == "map":
            m = dict(s.get("device.button_map") or {})
            default = inp.stack_actions()[ch][pos - 1]
            if action == default:
                m.pop(key, None)
            else:
                m[key] = action
            s.set("device.button_map", m)
            self.ctx.toast(f"A{ch} button {pos}: {self._action_text(action)}")
            return
        tuning = dict(s.get("device.button_tuning") or {})
        if what == "untune":
            tuning.pop(key, None)
            s.set("device.button_tuning", tuning)
            self.ctx.toast(f"A{ch} button {pos}: the usual level again")
            return
        b = self.app.buttons
        lad = b.stacks.get(ch) if b is not None else None
        v = lad.volts if lad is not None else None
        if v is None or v >= 3.0:
            self.ctx.toast(f"A{ch} reads {'nothing' if v is None else f'{v:.2f} V'}: hold the button down while "
                           "you tap, and try again")
            return
        others = [x for i, x in enumerate(inp.stack_levels(s.get("device.button_levels"), ch, tuning)) if i != pos - 1]
        if any(abs(x - v) < inp.MIN_GAP * 2 for x in others):
            self.ctx.toast(f"{v:.2f} V is too close to another button's level on A{ch}: check that button's resistor")
            return
        tuning[key] = round(v, 3)
        s.set("device.button_tuning", tuning)
        self.ctx.toast(f"A{ch} button {pos}: level {v:.2f} V")

    def _edit_key(self, name: str, label: str):
        def pick(action):
            s = self.ctx.settings
            keys = dict(s.get("device.keys") or {})
            if inp.DEFAULT_KEYS.get(name) == action:
                keys.pop(name, None)
            else:
                keys[name] = action or "none"
            s.set("device.keys", keys)
            self.ctx.toast(f"{label}: {self._action_text(action)}")
        choose(self.app, f"{label} does", self._choices(), None, pick)

    def _zero_steps(self):
        self.steps = 0

    def _reset(self):
        def go():
            s = self.ctx.settings
            for key, value in (("device.button_map", {}), ("device.button_tuning", {}), ("device.stacks_off", []),
                               ("device.encoder_reverse", False)):
                s.set(key, value)
            keys = {k: v for k, v in (s.get("device.keys") or {}).items() if k not in GPIO_KEY_NAMES.values()}
            s.set("device.keys", keys)
            self.ctx.toast("Control mapping back to the defaults")
        confirm(self.app, "Reset the control mapping?", "Every button back to its usual job and level, every stack "
                "on, the encoder the usual way round.", "Reset", go, danger=False)

    # --- input / drawing ---
    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
