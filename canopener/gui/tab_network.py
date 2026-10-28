"""Network tab: every module on the bus, as the profile's protocol has it. J1939: ECUs with their
address-claim NAME and the PGNs they send. CANopen: nodes with their state, heartbeat and messages
(reading a node's identity, finding silent nodes, the confirm-first NMT commands) and any other
11-bit IDs. A "j1939+canopen" profile lists both."""

import time

from ..canopen import cob, nmt
from ..core import canopen, profiles
from ..j1939.addresses import sa_name
from ..j1939.constants import PGN_ADDRESS_CLAIM, pgn_label
from ..j1939.fields import hexs
from .flow import FlowLayout
from .qt import Qt, QtGui, QtWidgets

COLS = ["Address / message", "Name / label", "Frames", "Rate", "DLC", "Latest data / details", "Last seen"]
OTHER_IDS = "other-11-bit"


class NetworkTab(QtWidgets.QWidget):
    title = "Network"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.items: dict = {}
        lay = QtWidgets.QVBoxLayout(self)
        bar = FlowLayout(spacing=8)            # wraps on a narrow screen (see gui/flow.py)
        self.claim_btn = QtWidgets.QPushButton("Request address claims")
        self.claim_btn.setToolTip("Asks every J1939 ECU to announce itself (PGN 60928). Read-only.")
        self.claim_btn.clicked.connect(lambda: ctx.request_pgn(PGN_ADDRESS_CLAIM))
        bar.addWidget(self.claim_btn)
        self.find_btn = QtWidgets.QPushButton("Find CANopen nodes")
        self.find_btn.setToolTip("Asks every CANopen node ID (1-127) for its device type, to find nodes that "
                                 "send no heartbeat. Read-only.")
        self.find_btn.clicked.connect(self.find_nodes)
        bar.addWidget(self.find_btn)
        expand = QtWidgets.QPushButton("Expand all")
        bar.addWidget(expand)
        collapse = QtWidgets.QPushButton("Collapse all")
        bar.addWidget(collapse)
        self.name_btn = QtWidgets.QPushButton("Name this module…")
        self.name_btn.setToolTip("Give the selected address or node your own name (saved in your profile; "
                                 "shows everywhere it does)")
        self.name_btn.clicked.connect(self.name_selected)
        bar.addWidget(self.name_btn)
        self.info_btn = QtWidgets.QPushButton("Read node info")
        self.info_btn.setToolTip("Read the selected CANopen node's identity, name and versions (SDO, read-only)")
        self.info_btn.clicked.connect(self.read_node_info)
        bar.addWidget(self.info_btn)
        self.nmt_btn = QtWidgets.QToolButton()
        self.nmt_btn.setText("NMT…")
        self.nmt_btn.setToolTip("CANopen network commands: start, stop or reset the selected node (or every "
                                "node). These change the machine; each asks first.")
        self.nmt_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.nmt_btn)
        for cmd, (label, _) in nmt.COMMANDS.items():
            menu.addAction(label + "…", lambda c=cmd: self.send_nmt(c))
        menu.addSeparator()
        menu.addAction(nmt.COMMANDS[nmt.START][0] + " (every node)…", lambda: self.send_nmt(nmt.START, True))
        menu.addAction(nmt.COMMANDS[nmt.RESET_COMM][0] + " (every node)…",
                       lambda: self.send_nmt(nmt.RESET_COMM, True))
        self.nmt_btn.setMenu(menu)
        bar.addWidget(self.nmt_btn)
        self.summary = QtWidgets.QLabel("")
        bar.addWidget(self.summary)
        lay.addLayout(bar)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(True)
        for c, w in enumerate((170, 260, 70, 70, 40, 330, 80)):
            self.tree.setColumnWidth(c, w)
        self.tree.itemSelectionChanged.connect(self._selection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        for text, fn in (("Name this module…", self.name_selected), ("Read node info", self.read_node_info)):
            act = QtGui.QAction(text, self.tree)
            act.triggered.connect(fn)
            self.tree.addAction(act)
        expand.clicked.connect(self.tree.expandAll)
        collapse.clicked.connect(self.tree.collapseAll)
        lay.addWidget(self.tree)
        ctx.session_changed.connect(self._session_changed)
        ctx.profile_changed.connect(lambda: self._session_changed(ctx.session))
        ctx.busy_changed.connect(lambda _: self._selection())
        self._session_changed(None)

    # --- selection ---------------------------------------------------------------------------------
    def selected_sa(self) -> int | None:
        """The selected module: a J1939 address, or a CANopen node as ``canopen.node_src(n)``."""
        it = self.tree.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        src = None if it is None else it.data(0, Qt.ItemDataRole.UserRole)
        return src if isinstance(src, int) else None

    def selected_node(self) -> int | None:
        src = self.selected_sa()
        return src - canopen.NODE_SRC if src is not None and canopen.is_node(src) else None

    def _can_send(self) -> bool:
        s = self.ctx.session
        return bool(self.ctx.live and not s.listen_only and not self.ctx.busy)

    def _apply_protocol(self):
        j1939, co = profiles.speaks_j1939(), profiles.speaks_canopen()
        self.claim_btn.setVisible(j1939)
        for w in (self.find_btn, self.info_btn, self.nmt_btn):
            w.setVisible(co)

    def _selection(self):
        self.name_btn.setEnabled(self.selected_sa() is not None)
        node = self.selected_node() is not None
        self.info_btn.setEnabled(node and self._can_send())
        self.nmt_btn.setEnabled(self._can_send() and self.ctx.session.has_canopen)
        self.find_btn.setEnabled(self._can_send())

    def name_selected(self):
        sa = self.selected_sa()
        if sa is not None:
            from .label_dialog import name_module
            name_module(self.ctx, sa, self)

    # --- CANopen actions -----------------------------------------------------------------------------
    def find_nodes(self):
        s = self.ctx.session

        def work():
            client = canopen.CanopenClient(s)
            try:
                return client.find_nodes()
            finally:
                client.close()

        def done(found):
            self.refresh(force=True)
            self.ctx.status_message.emit("CANopen nodes answering: " + (", ".join(f"{n:02X}" for n in found)
                                                                        if found else "none"))
        self.ctx.run_task("Asking every CANopen node ID…", work, done)

    def read_node_info(self):
        node = self.selected_node()
        if node is None or not self._can_send():
            return
        s = self.ctx.session

        def work():
            client = canopen.CanopenClient(s)
            try:
                return client.node_info(node)
            except canopen.SdoError as e:
                return str(e)
            finally:
                client.close()

        def done(value):
            self.refresh(force=True)
            title = canopen.source_name(canopen.node_src(node))
            if isinstance(value, str):
                QtWidgets.QMessageBox.warning(self, "Read node info", f"{title}: {value}")
                return
            text = "\n".join(f"{k}: {v}" for k, v in value) or "It answered, but has none of the standard objects."
            QtWidgets.QMessageBox.information(self, "Read node info", f"{title}\n\n{text}\n\n(Also in Machine Info.)")
        self.ctx.run_task(f"Reading node {node:02X}…", work, done)

    def send_nmt(self, command: int, every: bool = False):
        node = nmt.ALL_NODES if every else self.selected_node()
        if node is None:
            QtWidgets.QMessageBox.information(self, "NMT", "Select a CANopen node first (or use an 'every node' "
                                              "command).")
            return
        if not self._can_send():
            QtWidgets.QMessageBox.information(self, "NMT", "Commands have to transmit: connect, and turn "
                                              "listen-only off in Connection.")
            return
        r = QtWidgets.QMessageBox.warning(self, "NMT command", canopen.warning_nmt(command, node),
                                          QtWidgets.QMessageBox.StandardButton.Yes
                                          | QtWidgets.QMessageBox.StandardButton.No,
                                          QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        client = canopen.CanopenClient(self.ctx.session)
        try:
            client.nmt(command, node)
            self.ctx.status_message.emit(f"Sent NMT {nmt.COMMANDS[command][0]}")
        except canopen.SdoError as e:
            QtWidgets.QMessageBox.warning(self, "NMT command", str(e))
        finally:
            client.close()

    # --- the tree --------------------------------------------------------------------------------------
    def _session_changed(self, session):
        self._apply_protocol()
        self.tree.clear()
        self.items.clear()
        self.claim_btn.setEnabled(bool(self.ctx.live and not session.listen_only))
        self.refresh(force=True)
        self._selection()

    def _top(self, key, src):
        top = self.items.get(key)
        if top is None:
            top = self.items[key] = QtWidgets.QTreeWidgetItem(self.tree)
            top.setData(0, Qt.ItemDataRole.UserRole, src)
        return top

    def _child(self, top, key, title):
        child = self.items.get(key)
        if child is None:
            child = self.items[key] = QtWidgets.QTreeWidgetItem(top)
            child.setText(0, title)
            top.sortChildren(0, Qt.SortOrder.AscendingOrder)
        return child

    def refresh(self, force: bool = False):
        s = self.ctx.session
        if s is None or (not force and not self.isVisible()):
            return
        now = time.time()
        stats = s.stats_copy()
        std = s.std_stats_copy()
        last = max([st.last for st in stats.values()] + [st.last for st in std.values()], default=now)
        ref = last if s.offline else now
        with s.lock:
            latest = dict(s.latest)
            latest_std = dict(s.latest_std)
        j1939, co = profiles.speaks_j1939(), profiles.speaks_canopen()
        for e in s.ecu_list() if j1939 else ():
            top = self._top(e.sa, e.sa)
            top.setText(0, f"{e.sa:02X}  ({e.sa})")
            top.setText(1, sa_name(e.sa, with_hex=False))
            top.setText(2, str(e.frames))
            top.setText(5, e.name.summary() if e.name else "no address claim seen")
            top.setText(6, f"{ref - e.last_seen:.1f}s ago")
            for (sa, pgn), st in stats.items():
                if sa != e.sa:
                    continue
                child = self._child(top, (sa, pgn), f"{pgn:05X} ({pgn})")
                child.setText(1, pgn_label(pgn))
                child.setText(2, str(st.count))
                child.setText(3, f"{st.rate:.1f}/s" if st.rate else "")
                child.setText(4, str(st.dlc))
                d = latest.get((pgn, sa))
                child.setText(5, hexs(d[1][:32]) + (" ..." if d and len(d[1]) > 32 else "") if d else "")
                child.setText(6, f"{ref - st.last:.1f}s ago")
        nodes = s.canopen_nodes() if co else []
        for info in nodes:
            src = canopen.node_src(info.node)
            top = self._top(("node", info.node), src)
            top.setText(0, f"Node {info.node:02X}  ({info.node})")
            top.setText(1, canopen.source_name(src, with_hex=False))
            top.setText(2, str(info.frames))
            top.setText(5, "CANopen · " + canopen.node_summary(info))
            top.setText(6, f"{ref - info.last_seen:.1f}s ago" if info.last_seen else "")
            for cid, cs in info.cobs.items():
                child = self._child(top, ("cob", cid), f"{cid:03X}")
                child.setText(1, cob.KIND_LABELS.get(cob.classify(cid)[0], ""))
                child.setText(2, str(cs.count))
                child.setText(3, f"{1 / cs.period:.1f}/s" if cs.period else "")
                child.setText(4, str(len(cs.data)))
                child.setText(5, hexs(cs.data))
                child.setText(6, f"{ref - cs.last:.1f}s ago")
        known = {n.node for n in nodes}
        other = {cid: st for cid, st in std.items() if cob.node_of(cid) not in known} if co else {}
        for key in [k for k in self.items if isinstance(k, tuple) and k[0] == "id" and k[1] not in other]:
            item = self.items.pop(key)          # its node turned up: it's listed under the node now
            item.parent().removeChild(item)
        if other:
            top = self._top(OTHER_IDS, None)
            top.setText(0, "11-bit IDs")
            top.setText(1, "Network messages, and IDs from no known node")
            top.setText(2, str(sum(st.count for st in other.values())))
            for cid, st in other.items():
                child = self._child(top, ("id", cid), f"{cid:03X}")
                child.setText(1, cob.label(cid))
                child.setText(2, str(st.count))
                child.setText(3, f"{st.rate:.1f}/s" if st.rate else "")
                child.setText(4, str(st.dlc))
                d = latest_std.get(cid)
                child.setText(5, hexs(d[1]) if d else "")
                child.setText(6, f"{ref - st.last:.1f}s ago")
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        parts = []
        if j1939:
            parts.append(f"{len(s.ecus)} source address(es)")
        if co:
            parts.append(f"{len(nodes)} CANopen node(s)")
        self.summary.setText(", ".join(parts))
        self._selection()
