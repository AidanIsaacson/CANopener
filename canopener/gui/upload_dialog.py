"""Log > Upload settings…: where saved captures are sent (core/upload.py)."""

import threading

from ..core import upload
from .qt import QtWidgets, Signal

TOKEN_KEPT = "(saved: leave empty to keep it)"


class UploadDialog(QtWidgets.QDialog):
    _tested = Signal(bool, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Upload settings")
        self.settings = settings
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel("Saved captures can be sent to your upload server (or wherever a plugin sends "
                                 "them), to read somewhere else. The server's setup is in "
                                 "deploy/upload-server/README.md.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QtWidgets.QFormLayout()
        self.target = QtWidgets.QComboBox()
        for u in upload.uploaders():
            self.target.addItem(u.name, u.id)
        i = self.target.findData(settings.get("upload.target"))
        self.target.setCurrentIndex(max(0, i))
        self.target.currentIndexChanged.connect(self._sync)
        form.addRow("Send to", self.target)
        self.url = QtWidgets.QLineEdit(settings.get("upload.url") or "")
        self.url.setPlaceholderText("https://logs.example.com")
        form.addRow("Server address", self.url)
        self.token = QtWidgets.QLineEdit()
        self.token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText(TOKEN_KEPT if upload.load_token() else "the upload token from the server's .env")
        form.addRow("Upload token", self.token)
        self.on_save = QtWidgets.QCheckBox("Upload every capture when it's saved")
        self.on_save.setChecked(bool(settings.get("upload.on_save")))
        form.addRow("", self.on_save)
        lay.addLayout(form)
        tl = QtWidgets.QHBoxLayout()
        self.test_btn = QtWidgets.QPushButton("Test")
        self.test_btn.setToolTip("Check the address and the token (sends nothing)")
        self.test_btn.clicked.connect(self.test)
        tl.addWidget(self.test_btn)
        self.result = QtWidgets.QLabel()
        self.result.setWordWrap(True)
        tl.addWidget(self.result, 1)
        lay.addLayout(tl)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._tested.connect(self._show_test)
        self._sync()
        self.resize(520, self.sizeHint().height())

    def _is_server(self) -> bool:
        return self.target.currentData() == upload.HttpUploader.id

    def _sync(self):
        for w in (self.url, self.token):
            w.setEnabled(self._is_server())

    def apply(self):
        self.settings.update({"upload.target": self.target.currentData(), "upload.url": self.url.text().strip(),
                              "upload.on_save": self.on_save.isChecked()})
        if self.token.text().strip():
            upload.save_token(self.token.text())

    def test(self):
        self.apply()
        u = upload.get_uploader(self.target.currentData())
        self.test_btn.setEnabled(False)
        self.result.setText("Checking…")

        def work():
            try:
                self._tested.emit(True, u.check(self.settings))
            except upload.UploadError as e:
                self._tested.emit(False, str(e))
        threading.Thread(target=work, name="upload-test", daemon=True).start()

    def _show_test(self, ok: bool, text: str):
        self.test_btn.setEnabled(True)
        self.result.setText(("✓ " if ok else "✗ ") + text)

    def accept(self):
        self.apply()
        super().accept()
