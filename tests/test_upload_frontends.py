"""Machine details and uploads from the desktop Log panel, the handheld's Data log and the CLI."""

import json
import os
import time

import pytest
from test_upload import _get, _saved_capture, server  # noqa: F401  (the fixture)

from canopener.core import capture as cap
from canopener.core import upload
from canopener.core.settings import Settings


def _wait(cond, seconds=8.0, tick=None):
    end = time.time() + seconds
    while time.time() < end:
        if tick:
            tick()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_desktop_machine_dialog_and_upload(tmp_path, server, monkeypatch):  # noqa: F811
    qt = pytest.importorskip("canopener.gui.qt", exc_type=ImportError)
    from canopener.core import connection
    from canopener.gui.machine_dialog import MachineDialog
    from canopener.gui.main_window import MainWindow
    app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])

    def pump(seconds=0.1):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    Settings().set("show_simulators", True)
    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path / "qs"))
    w = MainWindow()
    w.uploads.stop()                        # the test drives the queue itself
    w.show()
    lp = w.log_panel
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    assert w.ctx.run_preset("info")         # standard J1939 Requests: Component ID, VIN, ...
    pump(3.0)
    lp.handle_action("start_stop")
    pump(0.6)
    lp.handle_action("timestamp")
    pump(0.4)
    lp.handle_action("start_stop")

    def fill_in(dlg):                       # what a user does in the dialog, then Save
        eng = next(c for c in dlg.candidates() if c.fields.get("make") == "DEUTZ")
        dlg._use(eng)
        dlg.fields["unit"].setText("14")
        dlg.question.setPlainText("Does PGN 0xFF10 mean anything?")
        dlg.accept()
        return 1
    monkeypatch.setattr(MachineDialog, "exec", fill_in)
    lp.edit_machine()
    assert lp.capture.machine["serial"] == "SIM00012345" and lp.capture.machine["unit"] == "14"
    assert "DEUTZ TCD3.6 L4" in lp.machine_label.text()
    assert cap.recent_machines()[0]["serial"] == "SIM00012345"

    folder = lp.save(quiet=True)
    assert folder and lp.capture.state == cap.SAVED
    readme = open(os.path.join(folder, "README.md")).read()
    assert "| Serial / VIN | SIM00012345 |" in readme and "Does PGN 0xFF10 mean anything?" in readme
    assert "Component ID: Make: DEUTZ" in readme and "front_end" not in readme
    meta = json.load(open(os.path.join(folder, "capture.json")))["meta"]
    assert meta["front_end"].startswith("desktop")

    # Upload with nothing set up: the settings dialog is asked for, and nothing is queued
    asked = []
    lp.upload_settings_requested.disconnect()
    lp.upload_settings_requested.connect(lambda: asked.append(1))
    lp.upload()
    assert asked and lp.uploads.waiting() == 0
    # set up (as the Upload settings dialog's Save would), then Upload
    from canopener.gui.upload_dialog import UploadDialog
    d = UploadDialog(w.core_settings, w)
    d.url.setText(server[0])
    d.token.setText("up-token")
    d.accept()
    w.core_settings.set("upload.part_mb", 0.125)
    assert upload.load_token() == "up-token" and upload.target(w.core_settings) is not None
    lp.upload()
    assert "Waiting to upload" in lp.upload_label.text() and not lp.upload_btn.isEnabled()
    assert lp.uploads.run_once() == folder
    lp.refresh()
    assert lp.upload_label.text().startswith("Uploaded") and "https://logs.example.com/api/captures/" in \
        lp.upload_label.text()
    assert lp.upload_btn.text() == "⇪ Upload again"
    key = lp.uploads.status(folder).key
    m = json.loads(_get(f"{server[0]}/api/captures/{key}")[1])
    assert m["machine"]["make"] == "DEUTZ" and m["connection"]["front_end"].startswith("desktop")
    w.close()


def test_handheld_machine_and_upload(tmp_path, server, monkeypatch):  # noqa: F811
    pygame = pytest.importorskip("pygame")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.screens.base import ChoiceDialog
    from canopener.device.screens.log import LogScreen

    s = Settings()
    ctx = DeviceContext(s)
    try:
        app = DeviceApp(ctx, (800, 480), encoder=False)
        app.step([])
        app.push(LogScreen(app))
        labels = [k.label for k in app.softkey_slots() if k]
        assert labels[:3] == ["Start", "Plan", "Machine"]
        cap.remember_machine({"make": "Acme", "model": "LiftMaster 40", "serial": "AB123"})
        app.top.machine()
        assert isinstance(app.top, ChoiceDialog)
        i = next(n for n, (label, _) in enumerate(app.top.options) if "LiftMaster" in label)
        app.top._pick(i)
        assert ctx.capture.machine["serial"] == "AB123"

        from canopener.core import config
        monkeypatch.setattr(config, "_data_dir_override", str(tmp_path))     # logs_dir() = tmp_path/logs
        c = _saved_capture(tmp_path)
        app.step([])
        assert "Open" in [k.label for k in app.softkey_slots() if k]      # saved logs can be reopened
        app.top.recover()
        assert isinstance(app.top, ChoiceDialog)
        app.top._pick([v for _, v in app.top.options].index(c.folder))
        assert ctx.capture.folder == c.folder and ctx.capture.state == cap.SAVED
        app.step([])
        labels = [k.label for k in app.softkey_slots() if k]
        assert labels[:4] == ["New log", "Upload", "Machine", "More"]
        ctx.upload_log()
        assert "Nowhere to upload to yet" in ctx.current_toast()
        s.set("upload.url", server[0])
        upload.save_token("up-token")
        ctx.upload_log()
        assert "Queued" in ctx.current_toast()
        assert ctx.uploads.run_once() == c.folder
        ctx._upload_cache = None
        assert ctx.upload_status().state == upload.DONE
        app.step([])
        assert [k.label for k in app.softkey_slots() if k][1] == "Upload again"
        app.top.more()
        assert [label for label, _ in app.top.options][1] == "Copy to USB"
    finally:
        ctx.shutdown()
        pygame.quit()


def test_cli_upload_and_capture_machine(tmp_path, server, capsys):  # noqa: F811
    from canopener import cli
    c = _saved_capture(tmp_path)
    assert cli.main(["capture", c.folder, "--machine", "unit=7", "--question", "look at FF12"]) == 0
    r = cap.Capture.load(c.folder)
    assert r.machine["unit"] == "7" and r.question == "look at FF12"
    assert cli.main(["capture", c.folder, "--machine", "colour=red"]) == 2
    Settings().set("upload.url", server[0])
    upload.save_token("up-token")
    assert cli.main(["upload", "--test"]) == 0
    assert cli.main(["upload", c.folder]) == 0
    capsys.readouterr()
    assert cli.main(["upload", "--status", c.folder]) == 0
    out = capsys.readouterr().out
    assert "Uploaded" in out and "_Unit_14_" in out
    upload.save_token("nope")
    assert cli.main(["upload", "--test"]) == 1
