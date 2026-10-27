"""Network tab: every ECU on the bus, its address-claim NAME, and the PGNs it sends."""

import time

from ..j1939.addresses import sa_name
from ..j1939.constants import PGN_ADDRESS_CLAIM, pgn_label
from ..j1939.fields import hexs
from .qt import Qt, QtGui, QtWidgets

COLS = ["Address / PGN", "Name / label", "Frames", "Rate", "DLC", "Latest data / NAME", "Last seen"]


class NetworkTab(QtWidgets.QWidget):
    title = "Network"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.items: dict = {}
        lay = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.claim_btn = QtWidgets.QPushButton("Request address claims")
        self.claim_btn.setToolTip("Asks every ECU to announce itself (PGN 60928). Read-only.")
        self.claim_btn.clicked.connect(lambda: ctx.request_pgn(PGN_ADDRESS_CLAIM))
        bar.addWidget(self.claim_btn)
        expand = QtWidgets.QPushButton("Expand all")
        bar.addWidget(expand)
        collapse = QtWidgets.QPushButton("Collapse all")
        bar.addWidget(collapse)
        self.name_btn = QtWidgets.QPushButton("Name this module…")
        self.name_btn.setToolTip("Give the selected address your own name (saved in your profile; "
                                 "shows everywhere the address does)")
        self.name_btn.clicked.connect(self.name_selected)
        bar.addWidget(self.name_btn)
        bar.addStretch(1)
        self.summary = QtWidgets.QLabel("")
        bar.addWidget(self.summary)
        lay.addLayout(bar)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(True)
        for c, w in enumerate((150, 260, 70, 70, 40, 330, 80)):
            self.tree.setColumnWidth(c, w)
        self.tree.itemSelectionChanged.connect(self._selection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        act = QtGui.QAction("Name this module…", self.tree)
        act.triggered.connect(self.name_selected)
        self.tree.addAction(act)
        expand.clicked.connect(self.tree.expandAll)
        collapse.clicked.connect(self.tree.collapseAll)
        lay.addWidget(self.tree)
        ctx.session_changed.connect(self._session_changed)
        ctx.profile_changed.connect(lambda: self._session_changed(ctx.session))
        self._session_changed(None)

    def selected_sa(self) -> int | None:
        it = self.tree.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        return None if it is None else it.data(0, Qt.ItemDataRole.UserRole)

    def _selection(self):
        self.name_btn.setEnabled(self.selected_sa() is not None)

    def name_selected(self):
        sa = self.selected_sa()
        if sa is not None:
            from .label_dialog import name_module
            name_module(self.ctx, sa, self)

    def _session_changed(self, session):
        self.tree.clear()
        self.items.clear()
        self.claim_btn.setEnabled(bool(self.ctx.live and not session.listen_only))
        self.refresh(force=True)
        self._selection()

    def refresh(self, force: bool = False):
        s = self.ctx.session
        if s is None or (not force and not self.isVisible()):
            return
        now = time.time()
        ref = max((st.last for st in s.stats_copy().values()), default=now) if s.offline else now
        stats = s.stats_copy()
        with s.lock:
            latest = dict(s.latest)
        for e in s.ecu_list():
            top = self.items.get(e.sa)
            if top is None:
                top = self.items[e.sa] = QtWidgets.QTreeWidgetItem(self.tree)
                top.setData(0, Qt.ItemDataRole.UserRole, e.sa)
            top.setText(0, f"{e.sa:02X}  ({e.sa})")
            top.setText(1, sa_name(e.sa, with_hex=False))
            top.setText(2, str(e.frames))
            top.setText(5, e.name.summary() if e.name else "no address claim seen")
            top.setText(6, f"{ref - e.last_seen:.1f}s ago")
            for (sa, pgn), st in stats.items():
                if sa != e.sa:
                    continue
                key = (sa, pgn)
                child = self.items.get(key)
                if child is None:
                    child = self.items[key] = QtWidgets.QTreeWidgetItem(top)
                    child.setText(0, f"{pgn:05X} ({pgn})")
                    top.sortChildren(0, Qt.SortOrder.AscendingOrder)
                child.setText(1, pgn_label(pgn))
                child.setText(2, str(st.count))
                child.setText(3, f"{st.rate:.1f}/s" if st.rate else "")
                child.setText(4, str(st.dlc))
                d = latest.get((pgn, sa))
                child.setText(5, hexs(d[1][:32]) + (" ..." if d and len(d[1]) > 32 else "") if d else "")
                child.setText(6, f"{ref - st.last:.1f}s ago")
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        self.summary.setText(f"{len(s.ecus)} source address(es)")
        self._selection()
