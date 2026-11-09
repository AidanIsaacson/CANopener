"""CAN > Connect > Bluetooth adapters: pair, rename, test and forget Bluetooth OBD adapters, with no
keyboard. The work is ``core.bluetooth`` through ``DeviceContext.bt_*`` (worker-thread jobs).

The PIN is picked from the common ones and names from a short list built from the adapter's own
name ("vLinker MC (truck)" ...). Names typed on the desktop or with ``canopener-cli bluetooth
rename`` show up here too: they're shared settings.
"""

from ..context import PINS
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm

PLACES = ["car", "truck", "work truck", "shop", "spare"]
KIND_SHORT = {"serial": "serial port", "classic": "Classic", "ble": "Low Energy: can't use"}


def name_choices(base: str) -> list[tuple[str, str]]:
    """Names to pick from on a handheld: the adapter's own, then '<short> (car)' and the like."""
    short = " ".join((base or "OBD adapter").replace("-", " ").split()[:2]) or "OBD adapter"
    names = [base] if base else []
    names += [f"{short} ({place})" for place in PLACES]
    return [(n, n) for n in dict.fromkeys(names)]


class BluetoothScreen(Screen):
    title = "Bluetooth adapters"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def on_enter(self):
        if self.ctx.bt_state is None and not self.ctx.busy:
            self.ctx.bt_refresh()

    def _rows(self):
        ctx = self.ctx
        rows = [Row("Bluetooth", kind="header")]
        state = ctx.bt_state
        if state is None:
            rows.append(Row("Checking…" if ctx.busy else "Not checked yet", kind="info"))
        else:
            problem = state.problem()
            rows.append(Row("Bluetooth is on" if not problem else "Bluetooth isn't ready", kind="info",
                            level=lambda: "bad" if problem else "ok", hint=problem))
            if state.present and not state.powered and not state.blocked:
                rows.append(Row("Turn on Bluetooth", kind="action", select=ctx.bt_power_on))
        rows.append(Row("Your adapters", kind="header"))
        picked = (ctx.settings.get("elm.adapter") or "").upper()
        choices = ctx.bt_choices()
        if not choices:
            rows.append(Row("None yet: scan below, then pair", kind="info"))
        for addr, label in choices:
            rows.append(Row(label, "in use" if addr == picked else "", hint=addr,
                            select=lambda a=addr: self._adapter_menu(a)))
        rows.append(Row("Pair a new adapter", kind="header"))
        rows.append(Row("Scan for adapters", kind="action", select=ctx.bt_scan,
                        hint="plug it into the vehicle first (some wake only with the key on)"))
        for d in ctx.bt_found:
            hint = d.address + ("  OBD adapter?" if d.looks_like_obd else "")
            rows.append(Row(d.name or "(no name)", KIND_SHORT.get(d.kind, ""), hint=hint,
                            select=lambda d=d: self._pair(d)))
        if ctx.bt_scanned and not ctx.bt_found:
            rows.append(Row("Nothing new found", kind="info"))
        return rows

    # --- actions ---
    def _adapter_menu(self, addr: str):
        ctx = self.ctx
        label = ctx.bt_label(addr)

        def pick(what):
            if what == "use":
                ctx.bt_use(addr, label)
                ctx.toast(f"Using {label}")
                self.app.pop(self)
            elif what == "rename":
                base = next((d.name for d in ctx.bt_paired if d.address == addr), "") or label
                choose(self.app, "Name it", name_choices(base), label, lambda n: ctx.bt_rename(addr, n))
            elif what == "test":
                ctx.bt_test(addr, lambda lines: self.app.push(InfoDialog(self.app, label, lines)))
            elif what == "forget":
                confirm(self.app, f"Forget {label}?", f"Unpairs {addr} from this unit. You can pair it again later.",
                        "Forget", lambda: ctx.bt_forget(addr))
        choose(self.app, label, [("Use this adapter", "use"), ("Rename", "rename"), ("Test", "test"),
                                 ("Forget (unpair)", "forget")], None, pick)

    def _pair(self, dev):
        if not dev.usable:
            self.app.push(InfoDialog(self.app, dev.name or dev.address, [
                "This entry is the adapter's Bluetooth Low Energy side, which CANopener can't use.",
                "", "Pick the same adapter's Classic entry (often the same name, marked serial port or Classic)."]))
            return
        choose(self.app, "PIN, if it asks", [(p + ("  (most adapters)" if i == 0 else ""), p)
                                             for i, p in enumerate(PINS)], PINS[0],
               lambda pin: self.ctx.bt_pair(dev.address, pin))

    def softkeys(self):
        return [SoftKey("Scan", self.ctx.bt_scan, enabled=not self.ctx.busy),
                SoftKey("Refresh", self.ctx.bt_refresh, enabled=not self.ctx.busy)]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
