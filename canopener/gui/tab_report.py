"""Report tab: the full text report for the last scan, with save / copy."""

import datetime
import os

from ..can.logfile import write_log
from ..core import config, report
from .qt import QtWidgets
from .widgets import mono_font


class ReportTab(QtWidgets.QWidget):
    title = "Report"

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save report...")
        save.clicked.connect(self.save)
        bar.addWidget(save)
        copy = QtWidgets.QPushButton("Copy to clipboard")
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.text.toPlainText()))
        bar.addWidget(copy)
        self.include_log = QtWidgets.QCheckBox("Also save the raw CAN log alongside")
        self.include_log.setChecked(True)
        bar.addWidget(self.include_log)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono_font())
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setPlaceholderText("Run a scan (or open a log file) to build a report.")
        lay.addWidget(self.text)
        ctx.result_ready.connect(lambda r: self.render())
        ctx.units_changed.connect(lambda _: self.render())
        ctx.profile_changed.connect(self.render)

    def render(self):
        res = self.ctx.result
        if res is None:
            self.text.clear()
            return
        self.text.setPlainText(report.build_report(res, self.ctx.session, full=True, system=self.ctx.units))

    def save(self):
        res = self.ctx.result
        if res is None:
            QtWidgets.QMessageBox.information(self, "Save", "Nothing to save yet - run a scan first.")
            return
        d = config.ensure_dir(os.path.join(config.data_dir(), "reports"))
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save report", os.path.join(d, f"report_{stamp}.txt"),
                                                        "Text (*.txt);;All files (*)")
        if not path:
            return
        with open(path, "w") as f:
            f.write(self.text.toPlainText())
        saved = [path]
        s = self.ctx.session
        if self.include_log.isChecked() and s is not None and s.raw:
            logpath = os.path.splitext(path)[0] + ".log"
            with s.lock:
                frames = list(s.raw)
            channel = s.backend.channel if s.backend is not None and s.backend.kind == "socketcan" else "can0"
            write_log(logpath, frames, channel)
            saved.append(logpath)
        QtWidgets.QMessageBox.information(self, "Saved", "Saved:\n" + "\n".join(saved))

