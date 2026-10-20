"""Machine Info tab: component / software / ECU IDs, VIN, hours, fuel, readiness."""

from ..j1939.addresses import sa_name
from .qt import QtWidgets


class InfoTab(QtWidgets.QWidget):
    title = "Machine Info"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.read_btn = QtWidgets.QPushButton("Read Machine Info")
        self.read_btn.setMinimumHeight(44)
        self.read_btn.clicked.connect(lambda: ctx.run_preset("info"))
        bar.addWidget(self.read_btn)
        bar.addStretch(1)
        self.note = QtWidgets.QLabel("Also filled in by a Full Scan.")
        self.note.setStyleSheet("color: gray")
        bar.addWidget(self.note)
        lay.addLayout(bar)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Item", "Value"])
        self.tree.setColumnWidth(0, 260)
        lay.addWidget(self.tree)

        ctx.result_ready.connect(self.show_result)
        ctx.busy_changed.connect(lambda b: self.read_btn.setEnabled(not b and ctx.live))
        ctx.session_changed.connect(lambda s: self.read_btn.setEnabled(ctx.live and not ctx.busy))
        self.read_btn.setEnabled(False)

    def show_result(self, result):
        info = result.info()
        claims = {sa: d for (p, sa), (t, d) in result.responses.items() if p == 0xEE00}
        readiness = result.readiness()
        if not info and not claims and not readiness:
            return          # e.g. a codes-only read: keep what we had
        self.tree.clear()
        groups = {}

        def group(sa):
            if sa not in groups:
                g = groups[sa] = QtWidgets.QTreeWidgetItem(self.tree, [sa_name(sa), ""])
                g.setExpanded(True)
            return groups[sa]

        for it in info:
            QtWidgets.QTreeWidgetItem(group(it.sa), [it.label, it.text])
        for sa, r in readiness.items():
            QtWidgets.QTreeWidgetItem(group(sa), ["Readiness (DM5)",
                                                  f"{r.active} active, {r.previously_active} previously "
                                                  f"active, OBD: {r.obd_text}"])
        session = self.ctx.session
        if session is not None:
            for e in session.ecu_list():
                if e.sa in claims and e.name:
                    QtWidgets.QTreeWidgetItem(group(e.sa), ["Address claim NAME",
                                                            f"{e.name.summary()} (0x{e.name.raw:016X})"])
        for sa in sorted(groups):
            self.tree.addTopLevelItem(groups[sa])
        missing = [label for _, label, outcome in result.response_summary()
                   if outcome.startswith(("no response", "not in log"))]
        self.note.setText(("Not answered: " + ", ".join(missing)) if missing else "All requests answered.")
