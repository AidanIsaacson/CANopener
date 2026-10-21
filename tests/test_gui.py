"""GUI smoke test (runs headless with QT_QPA_PLATFORM=offscreen). Skipped if Qt is missing."""

import time

import pytest

qt = pytest.importorskip("canopener.gui.qt", exc_type=ImportError)


@pytest.fixture(scope="module")
def app():
    return qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def test_simulator_end_to_end(app, tmp_path, monkeypatch):
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    assert w.ctx.live
    pump(app, 1.5)
    assert w.ctx.run_preset("stored")
    pump(app, 4.5)
    assert w.ctx.result is not None and not w.ctx.busy
    assert w.codes.table.rowCount() >= 4
    w.codes.table.selectRow(1)
    picked = w.codes.selected()
    assert f"SPN {picked.spn}" in w.codes.detail.toPlainText()
    w.codes.history.setChecked(True)               # rebuilds the table; selection must survive
    assert w.codes.selected() == picked
    w.codes.ecu.setCurrentIndex(w.codes.ecu.findData(0x27))
    assert {r.sa for r in w.codes.rows} == {0x27}
    w.codes.ecu.setCurrentIndex(0)

    w.tabs.setCurrentWidget(w.live)
    assert w.live.focus_spn(190)                    # engine speed is plotted
    pump(app, 0.5)
    assert len(w.live.order) > 20 and w.live.plotted

    w.units.setCurrentIndex(w.units.findData("imperial"))
    pump(app, 0.3)
    assert "°F" in w.report.text.toPlainText()

    assert len(w.monitor.by_id.order) > 10
    w.disconnect()
    assert w.ctx.session is None
    w.close()


def test_open_log(app, tmp_path):
    from canopener.can.frame import CanFrame
    from canopener.can.logfile import write_log
    from canopener.gui.main_window import MainWindow
    from canopener.j1939.dtc import Dtc, encode_dm

    path = tmp_path / "x.log"
    frames = [CanFrame(0x18FECA00, encode_dm(0x04, 0xFF, [Dtc(110, 0, 2)]), True, 10.0 + i) for i in range(3)]
    frames += [CanFrame(0x18FEEE00, bytes([100 + i, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]), True, 10.5 + i)
               for i in range(3)]
    write_log(str(path), frames)
    w = MainWindow()
    w.open_log(str(path))
    pump(app, 0.3)
    assert w.ctx.session.offline
    assert w.codes.table.item(0, 3).text() == "Engine coolant temperature"
    row = next(r for r in w.live.rows.values() if r.sig.spn == 110)
    assert len(row.points) == 3 and row.hi == 62
    w.close()


def _key(app, w, key, text=""):
    target = qt.QtWidgets.QApplication.focusWidget() or w
    for t in (qt.QtCore.QEvent.Type.KeyPress, qt.QtCore.QEvent.Type.KeyRelease):
        qt.QtWidgets.QApplication.sendEvent(target, qt.QtGui.QKeyEvent(t, key, qt.Qt.KeyboardModifier.NoModifier, text))
    pump(app, 0.05)


def test_log_tool_keys_capture_and_export(app, tmp_path, monkeypatch):
    from canopener.core import capture as cap
    from canopener.core import config, connection
    from canopener.gui.export_dialog import ExportDialog
    from canopener.gui.main_window import MainWindow

    Key = qt.Qt.Key
    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path / "qs"))
    w = MainWindow()
    w.settings.setValue("data_dir", str(tmp_path / "data"))
    config.set_data_dir(str(tmp_path / "data"))
    monkeypatch.setattr(w, "isActiveWindow", lambda: True)      # offscreen windows never get focus
    w.show()
    lp = w.log_panel
    lp.show()
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 0.8)

    lp.templates.setCurrentText("Intermittent shutdown")
    lp._load_template()
    assert len(lp.capture.markers) == 9
    lp.name.setFocus()
    _key(app, w, Key.Key_Space, " ")                 # typing in a text box: no timestamp added
    assert len(lp.capture.markers) == 9
    _key(app, w, Key.Key_Escape)                     # Esc leaves the box ...
    assert not lp.name.hasFocus()
    _key(app, w, Key.Key_Space, " ")                 # ... and now idle Space adds a planned timestamp
    assert len(lp.capture.markers) == 10
    assert lp.owns_name_editor(qt.QtWidgets.QApplication.focusWidget())   # with its name box open
    _key(app, w, Key.Key_Return)                     # Enter finishes the name (doesn't start recording)
    assert lp.capture.state == cap.IDLE
    lp.capture.delete(9)
    lp._sync()

    _key(app, w, Key.Key_Return)                      # Enter = start
    assert lp.capture.state == cap.RECORDING
    _key(app, w, Key.Key_Return)                      # bumped again right away: debounced
    assert lp.capture.state == cap.RECORDING
    _key(app, w, Key.Key_Space, " ")
    _key(app, w, Key.Key_4, "4")
    _key(app, w, Key.Key_Space, " ")
    _key(app, w, Key.Key_Backspace)                   # undo the last Space
    assert [i for i, m in enumerate(lp.capture.markers) if m.tagged] == [0, 3]
    pump(app, 0.9)
    _key(app, w, Key.Key_Return)                      # stop
    assert lp.capture.state == cap.STOPPED and lp.save_row.isVisible()

    lp.capture.rename(1, "crank")
    lp._flash = (3, time.monotonic())                # a row is still flashing from the last tag ...
    lp.refresh()                                     # ... recolouring it must not undo the rename
    lp.table.item(2, 3).setText("noisy")             # an edit in the table is a real edit
    folder = lp.save()
    assert folder and lp.capture.state == cap.SAVED and folder in w.ctx.saved_paths
    summary = open(f"{folder}/summary.txt").read()
    assert "2 - crank" in summary and "(noisy)" in summary

    dlg = ExportDialog(w.ctx, w)
    assert dlg.selected() == [folder]
    dlg.to_folder.setChecked(True)
    dlg.folder.setText(str(tmp_path / "out"))
    dlg._export()
    for _ in range(100):
        pump(app, 0.05)
        if dlg._worker is None:
            break
    assert "Exported" in dlg.status.text()
    dlg.close()

    # remap start/stop to F9; Enter stops doing anything
    w.hotkeys.bindings["start_stop"] = "F9"
    _key(app, w, Key.Key_Return)
    assert lp.capture.state == cap.SAVED
    _key(app, w, Key.Key_F9)
    assert lp.capture.state == cap.RECORDING          # Start on a saved capture begins a new one
    lp.hide()                                         # Log tool closed: keys do nothing
    pump(app, 0.9)
    _key(app, w, Key.Key_F9)
    assert lp.capture.state == cap.RECORDING
    w.disconnect()                                    # disconnecting stops the capture
    assert lp.capture.state == cap.STOPPED
    lp.capture.discard()
    w.close()


def test_keybind_dialog_rejects_duplicates(app):
    from canopener.gui.hotkeys import Hotkeys, KeybindDialog

    settings = qt.QtCore.QSettings()
    hk = Hotkeys(settings, lambda: True)
    dlg = KeybindDialog(hk)
    dlg.edits["timestamp"].setKeySequence(qt.QtGui.QKeySequence("Return"))
    assert not dlg._check() and "Return" in dlg.warning.text()
    dlg._defaults()
    assert dlg._check()
    dlg.edits["start_stop"].setKeySequence(qt.QtGui.QKeySequence("F9"))
    dlg._accept()
    assert hk.bindings["start_stop"] == "F9" and Hotkeys(settings, lambda: True).bindings["start_stop"] == "F9"


def _type(app, text):
    for ch in text:
        target = qt.QtWidgets.QApplication.focusWidget()
        k = qt.Qt.Key.Key_Space if ch == " " else qt.Qt.Key(ord(ch.upper()))
        for t in (qt.QtCore.QEvent.Type.KeyPress, qt.QtCore.QEvent.Type.KeyRelease):
            qt.QtWidgets.QApplication.sendEvent(target, qt.QtGui.QKeyEvent(t, k, qt.Qt.KeyboardModifier.NoModifier, ch))
    pump(app, 0.05)


def test_shift_space_names_a_whole_plan_from_the_keyboard(app, tmp_path, monkeypatch):
    from canopener.core import capture as cap
    from canopener.gui.main_window import MainWindow

    Key, Shift = qt.Qt.Key, qt.Qt.KeyboardModifier.ShiftModifier
    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path / "qs"))
    w = MainWindow()
    monkeypatch.setattr(w, "isActiveWindow", lambda: True)
    w.show()
    lp = w.log_panel
    lp.show()
    lp.capture.markers = []
    lp._sync()
    w.live.table.setFocus()

    def shift_space():
        target = qt.QtWidgets.QApplication.focusWidget() or w
        for t in (qt.QtCore.QEvent.Type.KeyPress, qt.QtCore.QEvent.Type.KeyRelease):
            qt.QtWidgets.QApplication.sendEvent(target, qt.QtGui.QKeyEvent(t, Key.Key_Space, Shift, " "))
        pump(app, 0.05)

    shift_space()                                    # new timestamp, its name box open + selected
    editor = qt.QtWidgets.QApplication.focusWidget()
    assert lp.owns_name_editor(editor) and editor.selectedText() == "Timestamp 1"
    _type(app, "Key On")                             # typing replaces the placeholder
    shift_space()                                    # keeps "Key On", opens the next one
    _type(app, "Start")
    shift_space()
    _type(app, "Drive")
    _key(app, w, Key.Key_Return)                     # Enter: done naming
    assert [m.label for m in lp.capture.markers] == ["Key On", "Start", "Drive"]
    assert lp.capture.state == cap.IDLE
    assert [lp.table.item(r, 0).text() for r in range(3)] == ["1", "2", "3"]     # number column
    assert lp.table.item(0, 1).text() == "Key On"
    assert w.settings.value("log/plan") == ["Key On", "Start", "Drive"]

    shift_space()                                    # Esc cancels a name: the placeholder stays
    _type(app, "oops")
    _key(app, w, Key.Key_Escape)
    assert lp.capture.markers[3].label == "Timestamp 4"
    assert not lp.owns_name_editor(qt.QtWidgets.QApplication.focusWidget())

    lp.name.setFocus()                               # in the job-name box Shift+Space just types
    shift_space()
    assert len(lp.capture.markers) == 4
    w.close()


def test_profile_features_in_the_gui(app, tmp_path, monkeypatch):
    import json

    from canopener.core import connection, profiles, units
    from canopener.gui.main_window import MainWindow
    from canopener.gui.request_dialog import RequestDialog

    pdir = tmp_path / "config" / "canopener" / "profiles"
    pdir.mkdir(parents=True)
    (pdir / "test-loader.json").write_text(json.dumps({
        "name": "Test loader", "detect": {"addresses": [0, 39], "min_match": 2},
        "signals": [{"name": "Park brake", "pgn": "0xFF22", "sa": 39, "start": 0, "bit": 0, "bits": 1,
                     "enum": {"0": "Released", "1": "Applied"}, "group": "Brakes"}]}))
    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path / "qs"))
    w = MainWindow()
    w.show()
    w.profile.setCurrentIndex(w.profile.findData("generic"))
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 2.6)                                   # detection runs every 2 s
    assert w.detect_bar.isVisible() and "Test loader" in w.detect_label.text()
    w._use_detected()
    assert profiles.active().id == "test-loader" and not w.detect_bar.isVisible()

    w.tabs.setCurrentWidget(w.live)
    pump(app, 1.0)
    rows = {w.live.rows[k].sig.name: (i, k) for i, k in enumerate(w.live.order)}
    i, _ = rows["Park brake"]
    from canopener.gui.tab_live import C_GROUP, C_VALUE
    assert w.live.table.item(i, C_VALUE).text() in ("Released", "Applied")
    assert w.live.table.item(i, C_GROUP).text() == "Brakes"
    w.live.group.setCurrentText("Brakes")
    visible = [w.live.rows[k].sig.name for j, k in enumerate(w.live.order) if not w.live.table.isRowHidden(j)]
    assert visible == ["Park brake"]
    w.live.group.setCurrentIndex(0)

    units.set_custom({"pressure": "psi", "temperature": "°C"})
    w.units.setCurrentIndex(w.units.findData(units.CUSTOM))
    w.live.refresh(force=True)
    unit_cells = {w.live.rows[k].sig.name: w.live.table.item(j, 4).text() for j, k in enumerate(w.live.order)}
    assert unit_cells["Oil pressure"] == "psi" and unit_cells["Coolant temp"] == "°C"
    units.set_custom(units.PRESETS[units.METRIC])
    w.units.setCurrentIndex(w.units.findData(units.METRIC))

    dlg = RequestDialog(w.ctx, w)
    dlg.pgn.setText("0xFEEB")
    dlg._send()
    pump(app, 1.8)
    assert "Make: DEUTZ" in dlg.out.toPlainText()
    dlg.close()
    w.disconnect()
    profiles.apply(None)
    w.close()
