"""The CAN mode: connection, fault codes, live data, the bus, and the data log, as pages on a
left rail. Left / right on the d-pad changes page; the encoder moves within it.

Everything works for J1939 machines and OBD-II cars (the active profile decides), and only
reads: J1939 requests and OBD-II / UDS reads. Nothing here clears codes or changes a vehicle.
"""

import time

import pygame

from ...core import interfaces, profiles
from ...core.connection import SOCKETCAN
from ...j1939.addresses import sa_name
from ...obd import describe_dtc, pids_for_dtc
from .. import input as inp
from .. import ui as icons
from ..context import BITRATES, SOURCES, bitrate_label, source_label
from ..ui import ListView
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose
from .live import LivePage
from .log import LogScreen


def status(ctx) -> str:
    if ctx.finding:
        return "Finding signal..."
    if ctx.connection is None:
        return "Not connected"
    return ctx.bus.text.replace("Bus: ", "")


# ----------------------------------------------------------------------------
class ConnectPage(Screen):
    title = "Connect"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _rows(self):
        ctx, s = self.ctx, self.ctx.settings
        kind = s.get("can.source")
        busy = ctx.connection is not None or ctx.finding
        rows = [Row("Connection", kind="header")]

        def pick(title, key, options):
            if busy:
                ctx.toast("Disconnect first")
                return
            choose(self.app, title, options, s.get(key), lambda v: s.set(key, v))

        rows.append(Row("Source", lambda: source_label(s.get("can.source")),
                        select=lambda: pick("Source", "can.source", [(lb, k) for k, lb in SOURCES])))
        if kind == SOCKETCAN:
            names = [i.name for i in interfaces.list_interfaces(details=False)]
            cur = s.get("can.iface")
            opts = [(n, n) for n in dict.fromkeys(names + [cur, "can0", "vcan0"])]
            rows.append(Row("Interface", cur, select=lambda: pick("Interface", "can.iface", opts),
                            hint="" if cur in names else "not present"))
            rows.append(Row("Bitrate", lambda: bitrate_label(int(s.get("can.bitrate") or 0)),
                            select=lambda: pick("Bitrate", "can.bitrate", [(bitrate_label(b), b) for b in BITRATES])))
            rows.append(Row("Listen-only (never transmit)", select=lambda: self._toggle("can.listen_only"),
                            check=lambda: s.get("can.listen_only")))
        found = profiles.arrange(profiles.available_profiles()[0])
        rows.append(Row("Profile", ctx.profile_name, select=lambda: choose(
            self.app, "Machine profile", [(p.name, p.id) for p in found], s.get("profile"), self._profile)))
        rows.append(Row("Connect at power-on", select=lambda: self._toggle("can.connect_on_start"),
                        check=lambda: s.get("can.connect_on_start")))
        rows.append(Row("Status", kind="header"))
        rows.append(Row(ctx.adapter.text, kind="info", level=lambda: ctx.adapter.level, hint=ctx.adapter.detail))
        rows.append(Row(ctx.bus.text, kind="info", level=lambda: ctx.bus.level, hint=ctx.bus.detail))
        if ctx.connection is not None:
            sess = ctx.session
            rows.append(Row(ctx.describe_connection(), kind="info",
                            hint=f"{ctx.frames_per_second():.0f} frames/s, {len(sess.ecus)} module(s), "
                                 f"{sess.rx_count:,} frames"))
        for line in list(ctx.finder_log)[-4:]:
            rows.append(Row(line, kind="info"))
        return rows

    def _toggle(self, key):
        s = self.ctx.settings
        if key == "can.listen_only" and self.ctx.connection is not None:
            self.ctx.toast("Disconnect first")
            return
        s.set(key, not s.get(key))

    def _profile(self, pid):
        self.ctx.apply_profile(pid)
        self.ctx.toast(f"Profile: {self.ctx.profile_name()}")

    def softkeys(self):
        ctx = self.ctx
        connected = ctx.connection is not None
        keys = [SoftKey("Disconnect" if connected else "Connect",
                        ctx.disconnect if connected else ctx.connect, enabled=not ctx.busy and not ctx.finding)]
        if ctx.settings.get("can.source") == SOCKETCAN:
            keys.append(SoftKey("Stop finding" if ctx.finding else "Find signal",
                                ctx.stop_finder if ctx.finding else ctx.start_finder,
                                active=ctx.finding, enabled=not ctx.busy))
        return keys

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


# ----------------------------------------------------------------------------
class CodesPage(Screen):
    title = "Codes"

    def __init__(self, app):
        super().__init__(app)
        self.view = ListView()
        self.items: list = []

    @property
    def obd(self) -> bool:
        return self.ctx.protocol == profiles.OBD2

    def _items(self):
        ctx = self.ctx
        if self.obd:
            out = []
            for ecu, kinds in (ctx.obd_codes or {}).items():
                for kind, codes in kinds.items():
                    out += [("obd", ecu, kind, code) for code in codes]
            return out
        r = ctx.result
        return [("j1939", row) for row in r.rows] if r else []

    def focused(self):
        return self.items[self.view.focus] if 0 <= self.view.focus < len(self.items) else None

    def _has_live(self, item) -> bool:
        if item is None:
            return False
        if item[0] == "obd":
            return bool(pids_for_dtc(item[3]))
        from ...j1939.signals import all_signals
        return any(s.spn == item[1].spn for s in all_signals())

    def softkeys(self):
        ctx = self.ctx
        can = ctx.live and not ctx.busy
        live = SoftKey("Live data", self.show_live, enabled=self._has_live(self.focused()))
        if self.obd:
            return [SoftKey("Read codes", lambda: ctx.read_codes(), enabled=can), None, None, live]
        return [SoftKey("Active", lambda: ctx.read_codes("active"), enabled=can),
                SoftKey("Stored", lambda: ctx.read_codes("stored"), enabled=can),
                SoftKey("Full scan", lambda: ctx.read_codes("full"), enabled=can), live]

    def show_live(self):
        item = self.focused()
        if not self._has_live(item):
            return
        ctx = self.ctx
        if item[0] == "obd":
            _, ecu, _, code = item
            pids = pids_for_dtc(code)
            ctx.focus_pids = list(pids)
            st = ctx.obd_streamer
            if st is not None:
                todo = list(dict.fromkeys(list(st.todo if st.running else []) + [(ecu, p) for p in pids]))
                st.stop()
                st.start(todo)
        else:
            ctx.focus_spn = item[1].spn
        self.parent.show_page(LivePage)

    def details(self, i):
        item = self.items[i]
        if item[0] == "obd":
            _, ecu, kind, code = item
            lines = [f"{code}: {describe_dtc(code)}", "", f"Module: {self.ctx.obd_label(ecu)}", f"Kind: {kind}"]
            title = code
        else:
            r = item[1]
            lines = [f"SPN {r.spn_label}: {r.spn_text}", f"FMI {r.fmi}: {r.fmi_text}", "",
                     f"Status: {r.status}", f"Module: {sa_name(r.sa)}", f"Occurrences: {r.oc_text}",
                     f"Lamps: {r.lamps}", f"Seen {r.seen}x, last {time.strftime('%H:%M:%S', time.localtime(r.last))}"]
            title = f"SPN {r.spn} FMI {r.fmi}"
        keys = [None, None, SoftKey("Live data", lambda: (self.app.pop(), self.show_live()))] \
            if self._has_live(item) else []
        self.app.push(InfoDialog(self.app, title, lines, keys))

    def on_action(self, action, n=1):
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1), len(self.items))
        elif action == inp.SELECT and self.items:
            self.details(self.view.focus)
        else:
            return False
        return True

    def draw(self, ui, rect):
        t = ui.theme
        ctx = self.ctx
        self.items = self._items()
        top = rect.y
        if not self.obd and ctx.session is not None:
            lamps = [(sa, lp) for sa, lp in ctx.session.current_lamps().items() if lp.any_on]
            if lamps:
                text = "Lamps on: " + "; ".join(f"{sa_name(sa, with_hex=False)}: {lp}" for sa, lp in lamps)
                ui.text(text, (rect.x + t.pad, rect.y + t.touch * .3), "body", "warn", max_w=rect.w - 2 * t.pad)
                top += round(t.touch * .6)
        area = pygame.Rect(rect.x, top, rect.w, rect.bottom - top)
        if not self.items:
            if not ctx.live:
                msg = "Connect first (Connect page)."
            elif self.obd:
                msg = "Press Read codes: every module is asked for stored, pending and permanent codes."
            elif ctx.result is None:
                msg = "Press Active for the codes on now, Stored for history, or Full scan for everything."
            else:
                msg = f"{ctx.result.title}: no codes. " + (
                    "Listen-only: only codes the machine broadcasts by itself." if ctx.result.listen_only else "")
            ui.paragraph(msg, area.inflate(-t.pad * 4, -t.pad * 4), "body", "dim")
            return

        def draw_row(ui, i, r, focused):
            item = self.items[i]
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            ui.fill(inner, "panel", t.radius)
            if focused:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            if item[0] == "obd":
                _, ecu, kind, code = item
                head, sub = f"{code}  {describe_dtc(code)}", f"{kind.capitalize()} - {ctx.obd_label(ecu)}"
                color = "bad" if kind != "pending" else "warn"
            else:
                row = item[1]
                head = f"SPN {row.spn} FMI {row.fmi}  {row.spn_text}"
                sub = f"{row.status} - {sa_name(row.sa)} - {row.fmi_text} - OC {row.oc_text}"
                color = "bad" if row.status.lower().startswith("active") else "warn"
            pygame.draw.rect(ui.surface, ui.color(color), (inner.x, inner.y, t.pad // 2, inner.h))
            ui.text(head, (inner.x + t.pad * 1.5, inner.y + inner.h * .34), "body", "text", max_w=inner.w - 2 * t.pad)
            ui.text(sub, (inner.x + t.pad * 1.5, inner.y + inner.h * .74), "small", "dim", max_w=inner.w - 2 * t.pad)

        self.view.draw(ui, area, len(self.items), round(t.row * 1.2), draw_row, self.details)


# ----------------------------------------------------------------------------
class BusPage(Screen):
    """Who's on the bus (modules, frames, rate), or every CAN ID with its latest bytes."""

    title = "Bus"

    def __init__(self, app):
        super().__init__(app)
        self.view = ListView()
        self.monitor = False
        self.ids: dict = {}           # (id, extended) -> [count, data, previous data, last t]
        self._seq = 0
        self._session = None
        self.lines: list = []

    def softkeys(self):
        return [SoftKey("Modules", lambda: setattr(self, "monitor", False), active=not self.monitor),
                SoftKey("CAN IDs", lambda: setattr(self, "monitor", True), active=self.monitor),
                SoftKey("Clear", self.ids.clear, enabled=self.monitor)]

    def on_action(self, action, n=1):
        if action in (inp.UP, inp.DOWN, inp.ROTATE):
            self.view.move(n if action == inp.ROTATE else (-1 if action == inp.UP else 1), len(self.lines))
            return True
        if action == inp.SELECT:
            self.monitor = not self.monitor
            return True
        return False

    def _feed(self, s):
        if s is not self._session:
            self._session, self._seq = s, s.frame_count if s is not None else 0
            self.ids.clear()
        if s is None:
            return
        self._seq, frames = s.frames_since(self._seq)
        for fr in frames[-5000:]:
            if fr.error:
                continue
            e = self.ids.get((fr.can_id, fr.extended))
            if e is None:
                self.ids[(fr.can_id, fr.extended)] = [1, bytes(fr.data), b"", fr.timestamp]
            else:
                e[0] += 1
                if e[1] != fr.data:
                    e[2], e[1] = e[1], bytes(fr.data)
                e[3] = fr.timestamp

    def _lines(self):
        ctx, s = self.ctx, self.ctx.session
        now = time.time()
        if self.monitor:
            out = []
            for (cid, ext), (count, data, prev, last) in sorted(self.ids.items()):
                ident = f"{cid:08X}" if ext else f"{cid:03X}"
                changed = "".join("^ " if i < len(prev) and prev[i] != b else "  " for i, b in enumerate(data))
                out.append((f"{ident}  {data.hex(' ').upper()}", f"x{count}   {now - last:.1f} s ago   {changed}"))
            return out
        if s is None:
            return []
        if ctx.protocol == profiles.OBD2:
            sn = ctx.obd_live.sniffer
            return [(sn.label(ecu), f"answers on {ecu:X}") for ecu in sorted(sn.modules())]
        rates: dict[int, float] = {}
        for (sa, _pgn), st in s.stats_copy().items():
            rates[sa] = rates.get(sa, 0.0) + st.rate
        out = []
        for e in s.ecu_list():
            ago = now - e.last_seen if not s.offline else 0
            name = f"   NAME: {e.name.summary()}" if e.name else ""
            out.append((sa_name(e.sa), f"{e.frames:,} frames, {rates.get(e.sa, 0):.0f}/s, "
                                       f"last {ago:.1f} s ago{name}"))
        return out

    def draw(self, ui, rect):
        t = ui.theme
        self._feed(self.ctx.session)
        self.lines = self._lines()
        if not self.lines:
            ui.paragraph("Nothing heard yet." if self.ctx.live else "Connect first (Connect page).",
                         rect.inflate(-t.pad * 4, -t.pad * 4), "body", "dim")
            return

        def draw_row(ui, i, r, focused):
            head, sub = self.lines[i]
            inner = r.inflate(-t.pad // 2, -t.pad // 3)
            ui.fill(inner, "panel", t.radius)
            if focused:
                ui.fill(inner, "focus", t.radius, max(2, t.pad // 3))
            ui.text(head, (inner.x + t.pad, inner.y + inner.h * .34), "body", "text", max_w=inner.w - 2 * t.pad)
            ui.text(sub, (inner.x + t.pad, inner.y + inner.h * .74), "small", "dim", max_w=inner.w - 2 * t.pad)

        self.view.draw(ui, rect, len(self.lines), round(t.row * 1.15), draw_row)


# ----------------------------------------------------------------------------
PAGES = [(ConnectPage, "Connect", icons.icon_connect), (CodesPage, "Codes", icons.icon_codes),
         (LivePage, "Live", icons.icon_live), (BusPage, "Bus", icons.icon_bus), (LogScreen, "Log", icons.icon_log)]


class CanScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.pages = [cls(app) for cls, _, _ in PAGES]
        for p in self.pages:
            p.parent = self
        self.index = 0 if self.ctx.connection is None else 1

    @property
    def title(self) -> str:
        return f"CAN · {self.page.title}"

    @property
    def page(self) -> Screen:
        return self.pages[self.index]

    def show_page(self, cls):
        for i, p in enumerate(self.pages):
            if isinstance(p, cls):
                self._go(i)

    def _go(self, i):
        if i != self.index:
            self.page.on_leave()
            self.index = i % len(self.pages)
            self.page.on_enter()

    def on_enter(self):
        self.page.on_enter()

    def softkeys(self):
        return self.page.softkeys()

    def on_action(self, action, n=1):
        if action in (inp.LEFT, inp.RIGHT):
            self._go((self.index + (-1 if action == inp.LEFT else 1)) % len(self.pages))
            return True
        return self.page.on_action(action, n)

    def draw(self, ui, rect):
        t = ui.theme
        rail_w = round(t.touch * 1.6)
        n = len(self.pages)
        h = min(round(t.touch * 1.3), rect.h // n)
        for i, (_cls, label, icon) in enumerate(PAGES):
            r = pygame.Rect(rect.x, rect.y + i * h, rail_w, h - t.pad // 2)
            on = i == self.index
            ui.fill(r, "accent" if on else "panel", t.radius)
            size = round(min(r.h * .5, r.w * .4))
            ir = pygame.Rect(0, 0, size, size)
            ir.midtop = (r.centerx, r.y + r.h * .1)
            icon(ui, ir, ui.color("text" if on else "dim"))
            ui.text(label, (r.centerx, r.bottom - r.h * .2), "small", "text" if on else "dim", align="center")
            ui.hit(r, lambda i=i: self._go(i))
        page = pygame.Rect(rect.x + rail_w + t.pad, rect.y, rect.w - rail_w - t.pad, rect.h)
        self.page.draw(ui, page)
