"""GUI smoke test (runs headless with QT_QPA_PLATFORM=offscreen). Skipped if Qt is missing."""

import time

import pytest

qt = pytest.importorskip("canopener.gui.qt", exc_type=ImportError)


@pytest.fixture(autouse=True)
def simulators_listed():
    """Most tests here pick a simulator as the source: list them (they're hidden by default)."""
    from canopener.core.settings import Settings
    Settings().set("show_simulators", True)


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


def _wait(app, cond, secs=15):
    end = time.time() + secs
    while not cond() and time.time() < end:
        pump(app, 0.1)
    return cond()


def test_obd_tab_reads_simulator(app, tmp_path, monkeypatch):
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow
    from canopener.gui.tab_obd import P_FREEZE, P_LIVE

    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
    w.connect()
    pump(app, 0.4)
    o = w.obd
    assert w.tabs.currentWidget() is o and o.check_btn.isEnabled()

    def column(table, c):
        return [table.item(r, c).text() for r in range(table.rowCount())]

    # Check codes: every module's codes, the MIL, and the freeze frame behind the first code.
    o.check_codes()
    assert _wait(app, lambda: not o._jobs and o.codes.rowCount() == 5)
    assert column(o.codes, 2) == ["P0133", "P0420", "P0171", "P0420", "P0730"]
    assert column(o.codes, 0)[-1] == "TCM (7E9)" and "Bank 1 Sensor 1" in column(o.codes, 3)[0]
    assert "ON" in o.mil_label.text() and o.ready_summary.text().startswith("NOT READY: MIL ON")
    o.select_code(0)                                     # P0133: O2 B1S1, and it set the freeze frame
    assert o.show_live_btn.isEnabled(), o.code_detail.text()
    assert o.show_freeze_btn.isEnabled(), o.code_detail.text()
    o.select_code(4)                                     # P0730: no standard sensor value
    assert not o.show_live_btn.isEnabled() and not o.show_freeze_btn.isEnabled()
    o.select_code(0)
    o._show_code_freeze()
    assert o.pages.currentIndex() == P_FREEZE and "Caused by" in column(o.freeze, 1)

    # Show in Live Data: finds the live values (none read yet), plots the code's sensor, streams.
    o._show_code_live()
    assert _wait(app, lambda: o._streaming and o.live.plotted)
    assert o.pages.currentIndex() == P_LIVE
    assert [o.model.rows[k].sig.name for k in o.live.plotted] == ["O2 B1S2: voltage"] or \
        all(o.model.rows[k].sig.pid in (0x14, 0x06, 0x15) for k in o.live.plotted)
    pump(app, 1.0)
    assert len(o.live.order) >= 20 and "Module" == o.live.table.horizontalHeaderItem(7).text()
    key = o.live.plotted[0]
    assert len(o.model.rows[key].points) >= 2            # it's being polled: history for the chart
    o.live.hide_plots.setChecked(True)
    assert not o.live.chart_pane.isVisible()
    o.live.only_checked.setChecked(True)                 # hide unticked: only the ticked row, only it polled
    pump(app, 0.4)
    assert [o.live.table.isRowHidden(i) for i in range(len(o.live.order))].count(False) == len(o.live.plotted)
    assert o._poll == list(dict.fromkeys((o.model.rows[k].sa, o.model.rows[k].sig.pid) for k in o.live.plotted))
    o.live.only_checked.setChecked(False)
    o.toggle_stream()
    assert not o._streaming

    # Clear codes: declined = nothing sent; confirmed = cleared and read back.
    answers = [qt.QtWidgets.QMessageBox.StandardButton.No]
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "warning", lambda *a, **k: answers[0])
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    o._clear_codes()
    pump(app, 0.3)
    assert not w.ctx.connection.simulator.cleared
    answers[0] = qt.QtWidgets.QMessageBox.StandardButton.Yes
    o._clear_codes()
    assert _wait(app, lambda: not o._jobs and w.ctx.connection.simulator.cleared)
    pump(app, 0.2)
    assert column(o.codes, 2) == ["P0420"] and column(o.codes, 1) == ["permanent"]
    assert "off" in o.mil_label.text()

    # Mode 08: find the EVAP test and run it.
    o._read_tests()
    assert _wait(app, lambda: not o._jobs and o.test_pick.count())
    assert "Evaporative system leak test" in o.test_pick.currentText()
    o._run_test()
    assert _wait(app, lambda: not o._jobs and o.test_log.count())
    assert w.ctx.connection.simulator.evap_test_running and "started" in o.test_log.item(0).text()
    w.disconnect()
    w.close()


def test_log_tool_records_obd(app, tmp_path):
    """Recording on a car: the OBD-II tab polls for the capture's whole length, markers show on
    its chart, the capture keeps an OBD-II report, and opening its log brings the values back."""
    from canopener.core import config, connection
    from canopener.gui.main_window import MainWindow

    config.set_data_dir(str(tmp_path / "data"))
    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
    w.connect()
    pump(app, 0.4)
    o, log = w.obd, w.log_panel
    log.start()
    assert log.recording
    assert _wait(app, lambda: o._streaming and len(o.live.order) >= 20)
    log.refresh()
    assert "OBD-II: polling" in log.detail_label.text()
    w.show_tab(o.live)
    log.handle_action("timestamp")
    pump(app, 1.0)
    assert o.live.chart.markers                          # the Log tool's marker on the OBD-II chart
    log.stop()
    assert not o._streaming                              # the recording's polling ends with it
    assert _wait(app, lambda: not o._jobs, 5)
    folder = log.save(parent_dir=str(tmp_path / "caps"), quiet=True)
    import os
    report = open(os.path.join(folder, "obd_report.txt")).read()
    assert "P0133" in report and "Engine speed" in report and "Readiness ECM (7E8): MIL ON" in report
    summary = open(os.path.join(folder, "summary.txt")).read()
    assert "OBD-II codes read in this capture" in summary and "ECM (7E8): stored code(s) P0133, P0420" in summary
    header = open(os.path.join(folder, "live_data.csv")).readline()
    assert "Engine speed (ECM (7E8)) [rpm]" in header
    # Reopen the log: the OBD-II values and codes come back from the recorded frames.
    w.open_log(os.path.join(folder, "capture.log"))
    pump(app, 0.5)
    assert w.ctx.session.offline and len(o.live.order) >= 20
    assert "P0133" in [o.codes.item(r, 2).text() for r in range(o.codes.rowCount())]
    assert any(len(o.model.rows[k].points) > 3 for k in o.live.order)
    w.close()


def test_uds_dialog_reads_module(app, tmp_path):
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow
    from canopener.gui.uds_dialog import UdsDialog

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
    w.connect()
    pump(app, 0.4)
    dlg = UdsDialog(w.ctx)
    assert dlg.read_btn.isEnabled()
    dlg._read()
    pump(app, 2.0)
    text = dlg.out.toPlainText()
    assert "C0561-00" in text and "P0730-00" in text
    assert "1HGCM82633A004352" in text          # the F190 data identifier (VIN)

    # Clear and reset ask first, then report.
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(qt.QtWidgets.QMessageBox, "warning", lambda *a, **k: qt.QtWidgets.QMessageBox.StandardButton.Yes)
    dlg._clear()
    pump(app, 1.0)
    assert "Clear codes: done" in dlg.out.toPlainText()
    assert w.ctx.connection.simulator.uds_dtcs == []
    dlg._reset()
    pump(app, 1.0)
    assert w.ctx.connection.simulator.resets == 1
    mp.undo()
    w.disconnect()
    w.close()


def test_transmit_dialog_gating(app, tmp_path):
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow
    from canopener.gui.transmit_dialog import TransmitDialog

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 0.4)
    dlg = TransmitDialog(w.ctx, w.transmitter)
    assert not dlg.send_btn.isEnabled()          # off by default
    dlg.arm.setChecked(True)
    assert dlg.send_btn.isEnabled()              # armed + connected + not listen-only
    assert w.transmitter.enabled
    w.disconnect()

    # A fresh connection must start with transmit mode off again.
    w.listen_only.setChecked(True)
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 0.4)
    assert not w.transmitter.enabled
    dlg2 = TransmitDialog(w.ctx, w.transmitter)
    dlg2.arm.setChecked(True)
    assert not dlg2.send_btn.isEnabled()         # listen-only overrides even when armed
    w.disconnect()
    w.close()


def test_window_fits_small_laptop(app, tmp_path):
    """The window must stay narrow enough to fit (and so maximise / snap on) a 1366-wide laptop,
    both empty AND once content has loaded. Two non-wrapping rows used to pin the minimum width
    wide: the Fault Codes controls (~1458 empty) and the per-ECU lamp strip (~1459 after a scan
    populated several ECUs' lamps). Both now wrap."""
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    app.processEvents()
    assert w.minimumSizeHint().width() <= 1200           # empty

    # Populate: connect to the sim, scan so lamps + codes fill, select a row so the detail loads.
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 1.5)
    w.ctx.run_preset("full")
    pump(app, 5.0)
    if w.codes.table.rowCount():
        w.codes.table.selectRow(0)
    app.processEvents()
    assert w.lamps.minimumSizeHint().width() <= 700      # lamp strip wraps, no longer ~1068
    assert w.minimumSizeHint().width() <= 1200           # still fits after content loaded

    # It must actually accept a laptop-sized geometry (not get clamped back up by a minimum).
    w.resize(1280, 720)
    app.processEvents()
    assert w.width() <= 1280 and w.height() <= 720

    # A 1280x720 screen leaves ~1280x650 under the taskbar and title bar. Every tab (plugin tabs
    # too), with the Log panel open, must fit that with room for a bigger desktop font.
    assert w.log_panel.isVisible()
    for i in range(w.tabs.count()):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
        m = w.minimumSizeHint()
        assert m.width() <= 1200 and m.height() <= 600, (w.tabs.tabText(i), m.width(), m.height())

    # Dialogs too.
    from canopener.gui.export_dialog import ExportDialog
    from canopener.gui.request_dialog import RequestDialog
    from canopener.gui.settings_dialog import UnitsDialog
    from canopener.gui.transmit_dialog import TransmitDialog
    from canopener.gui.uds_dialog import UdsDialog
    for d in (TransmitDialog(w.ctx, w.transmitter, w), RequestDialog(w.ctx, w), UdsDialog(w.ctx, w),
              ExportDialog(w.ctx, w), UnitsDialog({}, w)):
        d.show()
        app.processEvents()
        m = d.minimumSizeHint()
        assert d.width() <= 1280 and d.height() <= 650 and m.width() <= 1200 and m.height() <= 600, \
            (type(d).__name__, d.width(), d.height())
        d.close()
    w.disconnect()
    w.close()


def test_window_fits_a_real_1280x720_laptop_screen(tmp_path):
    """Run the app on an emulated screen the size of a 1280x720 laptop's usable area (taskbar and
    title bar taken off) with a saved oversized geometry, load content, and check the window
    really fits. Separate process: the screen size is set when Qt starts."""
    import json
    import subprocess
    import sys
    cfg = tmp_path / "screen.json"
    cfg.write_text(json.dumps({"synchronousWindowSystem": True, "windowFrameMargins": False, "screens": [
        {"name": "laptop", "x": 0, "y": 0, "width": 1280, "height": 650, "logicalDpi": 96,
         "logicalBaseDpi": 96, "dpr": 1}]}))
    script = r"""
import sys, time
from canopener.gui.qt import QtCore, QtWidgets
app = QtWidgets.QApplication([])
QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, sys.argv[1])
from canopener.core import connection
from canopener.gui.main_window import MainWindow
w = MainWindow(); w.show()
w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR)); w.connect()
end = time.time() + 1.5
while time.time() < end: app.processEvents(); time.sleep(0.02)
w.ctx.run_preset("full")
end = time.time() + 5
while time.time() < end: app.processEvents(); time.sleep(0.02)
avail = app.primaryScreen().availableGeometry()
bad = []
for i in range(w.tabs.count()):
    w.tabs.setCurrentIndex(i); app.processEvents(); w._fit_to_screen(); app.processEvents()
    if not avail.contains(w.frameGeometry()):
        bad.append((w.tabs.tabText(i), w.frameGeometry().width(), w.frameGeometry().height()))
print("BAD", bad) if bad else print("OK")
w.disconnect()
"""
    env = dict(__import__("os").environ, QT_QPA_PLATFORM=f"offscreen:configfile={cfg}")
    r = subprocess.run([sys.executable, "-c", script, str(tmp_path / "cfg")], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.stdout.strip().endswith("OK"), r.stdout + r.stderr


def test_live_data_view_toggles(app, tmp_path):
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 1.5)
    live = w.live
    w.tabs.setCurrentWidget(live)
    live.sample()
    rows = live.table.rowCount()
    assert rows > 5

    def shown():
        return [i for i in range(rows) if not live.table.isRowHidden(i)]

    # Hide unticked: only the ticked rows stay; a hint when nothing is ticked.
    assert live.only_checked.text() == "Hide unticked"
    live.only_checked.setChecked(True)
    assert live.only_checked.text() == "Show unticked" and shown() == []
    assert "Nothing is ticked" in live.empty.text() and not live.empty.isHidden()
    assert live.focus_spn(190)                         # plotting a signal brings it back
    assert len(shown()) == 1 and live.empty.isHidden()
    live.table.item(shown()[0], 0).setCheckState(qt.Qt.CheckState.Unchecked)
    assert shown() == []                               # unticking removes it again
    live.only_checked.setChecked(False)
    assert len(shown()) == rows

    # Hide plots: the chart (and its controls) go, the list gets the room.
    assert live.chart_pane.isVisible()
    live.hide_plots.setChecked(True)
    assert not live.chart_pane.isVisible() and live.hide_plots.text() == "Show plots"
    w.disconnect()
    w.close()

    # Both are remembered for next time.
    w2 = MainWindow()
    w2.show()
    assert w2.live.hide_plots.isChecked() and not w2.live.chart_pane.isVisible()
    assert not w2.live.only_checked.isChecked()
    w2.close()


def test_connect_brings_up_a_down_interface(app, tmp_path, monkeypatch):
    from canopener.core import interfaces
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    w.bitrate.setCurrentIndex(w.bitrate.findData(250000))
    w.listen_only.setChecked(False)

    calls = []
    monkeypatch.setattr(interfaces, "list_interfaces",
                        lambda *a, **k: [interfaces.CanInterface("can0", "down")])
    monkeypatch.setattr(interfaces, "bring_up",
                        lambda name, bitrate, lo, **k: (calls.append((name, bitrate, lo)) or (True, "ok")))
    assert w._ensure_up("can0") is True
    assert calls == [("can0", 250000, False)]          # auto-brought-up at the selected bitrate

    # The listen-only choice is carried through to bring-up.
    calls.clear()
    w.listen_only.setChecked(True)
    assert w._ensure_up("can0") is True and calls == [("can0", 250000, True)]

    # Already up: no bring-up attempted.
    calls.clear()
    monkeypatch.setattr(interfaces, "list_interfaces",
                        lambda *a, **k: [interfaces.CanInterface("can0", "up")])
    assert w._ensure_up("can0") is True and calls == []

    # vcan is never touched (the simulator manages it).
    assert w._ensure_up("vcan0") is True
    w.close()


def test_adapter_under_another_name_and_quiet_startup(app, tmp_path, monkeypatch):
    from canopener.core import interfaces
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    # Saved choice is can0 but the adapter came up as can1 (vcan0 is ignored): pick can1.
    monkeypatch.setattr(interfaces, "list_interfaces", lambda *a, **k: [
        interfaces.CanInterface("can1", "down"), interfaces.CanInterface("vcan0", "up")])
    w._refresh_interfaces(preferred="can0")
    assert w.iface.currentText() == "can1"

    # Connect on startup with nothing plugged in: a status message, no connection, no dialog.
    monkeypatch.setattr(interfaces, "list_interfaces", lambda *a, **k: [])
    monkeypatch.setattr(interfaces, "usb_adapters", lambda *a, **k: [])
    monkeypatch.setattr(w, "connect", lambda *a, **k: pytest.fail("should not connect"))
    w._connect_on_startup()
    assert "No CAN adapter found" in w.statusBar().currentMessage()
    w.close()


def test_interface_list_shows_only_connected_and_rechecks_on_open(app, tmp_path, monkeypatch):
    from canopener.core import interfaces
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    present = []
    monkeypatch.setattr(interfaces, "list_interfaces", lambda *a, **k: list(present))
    monkeypatch.setattr(interfaces, "usb_adapters", lambda *a, **k: [])
    w = MainWindow()
    w._refresh_interfaces(preferred="can0")
    # Nothing plugged in: an empty list, but the box still reads can0.
    assert w.iface.count() == 0 and w.iface.currentText() == "can0"

    # Plugged in afterwards: opening the list finds it, without touching the chosen bitrate.
    present[:] = [interfaces.CanInterface("can0", "up", bitrate=250000)]
    w.bitrate.setCurrentIndex(w.bitrate.findData(500000))
    w.iface._on_popup()                              # what showPopup() runs first
    assert [w.iface.itemText(i) for i in range(w.iface.count())] == ["can0"]
    assert w.iface.currentText() == "can0" and w.bitrate.currentData() == 500000

    # Unplugged again: it drops out of the list.
    present.clear()
    w.iface._on_popup()
    assert w.iface.count() == 0 and w.iface.currentText() == "can0"
    assert "No CAN adapter found" in w.statusBar().currentMessage()
    w.close()


def test_connection_sidebar_lamps_and_signal_finder(app, tmp_path, monkeypatch):
    from canopener.core import connection, interfaces, link
    from canopener.gui import connection_panel
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    monkeypatch.setattr(interfaces, "list_interfaces", lambda *a, **k: [interfaces.CanInterface("can0", "up")])
    monkeypatch.setattr(interfaces, "usb_adapters", lambda *a, **k: [])
    w = MainWindow()
    w.show()
    # The connection controls live in the Connection sidebar now, not the toolbar.
    assert w.conn_panel.isAncestorOf(w.source) and w.conn_panel.isAncestorOf(w.connect_btn)
    cues = []
    monkeypatch.setattr(w.conn_panel, "cue", lambda name, **k: cues.append((name, k.get("link_status"))))

    # Simulator: blue adapter lamp, then green bus lamp once traffic flows.
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 1.0)
    assert w.adapter_lamp.level == link.SIM and w.bus_lamp.level == link.OK
    assert w.conn_panel.bus_lamp.level == link.OK
    # Traffic stops (probe knocked off): amber lamp and a "lost" cue, once.
    w.ctx.session.last_rx_t = 0.0
    w.ctx.connection.simulator.stop()
    pump(app, 2.6)
    assert w.bus_lamp.level == link.WAIT and cues == [("lost", True)]
    w.disconnect()
    monkeypatch.undo()

    # Its own checkbox, under the lamps (shown for every source): off = no traffic chimes, while
    # the signal finder's lock-on chime keeps its own setting.
    from canopener.core import sound
    played = []
    monkeypatch.setattr(sound, "play", lambda name, **k: played.append(name) or True)
    p = w.conn_panel
    assert p.link_sound.isChecked() and not p.link_sound.isHidden()
    p.link_sound.setChecked(False)
    assert w.settings.value("sound/link_cues") in (False, "false")
    p.cue("lost", link_status=True)
    p.cue("found")
    assert played == ["found"]
    p.link_sound.setChecked(True)
    p.cue("back", link_status=True)
    assert played == ["found", "back"]
    monkeypatch.setattr(w.conn_panel, "cue", lambda name, **k: cues.append((name, k.get("link_status"))))
    monkeypatch.setattr(interfaces, "list_interfaces", lambda *a, **k: [interfaces.CanInterface("can0", "up")])
    monkeypatch.setattr(interfaces, "usb_adapters", lambda *a, **k: [])

    # Signal finder: Auto without passwordless access asks first (declined: nothing starts).
    w.source.setCurrentIndex(w.source.findData(connection.SOCKETCAN))
    w.iface.setCurrentText("can0")
    w.bitrate.setCurrentIndex(w.bitrate.findData(connection_panel.AUTO_BITRATE))
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "question",
                        lambda *a, **k: qt.QtWidgets.QMessageBox.StandardButton.Cancel)
    assert not w.conn_panel.start_finder(quiet_check=lambda name: False)

    # With access: it runs (a fake finder here), locks, chimes, sets the bitrate and connects.
    started = []

    class FakeFinder:
        def __init__(self, name, order, on_event, listen_only):
            started.append((name, order, listen_only))

        def start(self):
            pass

        def stop(self):
            pass
    monkeypatch.setattr(link, "SignalFinder", FakeFinder)
    connects = []
    monkeypatch.setattr(w, "connect", lambda *a, **k: connects.append(1))
    w.conn_panel.connect_when_found.setChecked(True)
    assert w.conn_panel.start_finder(quiet_check=lambda name: True)
    assert started[0][0] == "can0" and len(started[0][1]) == len(interfaces.BITRATES)
    assert not w.connect_btn.isEnabled() and w.conn_panel.finding
    pump(app, 0.3)
    assert "finding" in w.bus_lamp.text()
    w.conn_panel.handle_event(link.FinderEvent("searching", "250k: nothing heard", 250000))
    assert w.conn_panel.history.count() == 1
    w.conn_panel.handle_event(link.FinderEvent("locked", "Locked on 500k", 500000, 40, 0))
    assert not w.conn_panel.finding and w.connect_btn.isEnabled()
    assert w.bitrate.currentData() == 500000 and "LOCKED" in w.conn_panel.finder_state.text()
    assert cues[-1] == ("found", None) and connects == [1]           # the finder's chime, not a link cue
    assert int(w.settings.value("finder/last_bitrate")) == 500000
    w.close()


def test_obd2_is_its_own_profile(app, tmp_path):
    from canopener.core import connection, profiles
    from canopener.gui.main_window import MainWindow

    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path))
    w = MainWindow()
    w.show()
    tabs = w.tabs

    def shown():
        return {tabs.widget(i).title for i in range(tabs.count()) if tabs.isTabVisible(i)}
    # A J1939 profile: no OBD-II tab.
    assert "OBD-II (Auto)" not in shown() and "Fault Codes" in shown() and w.lamps.isVisible()
    # The OBD-II profile: one row of tabs (the scan tool's pages, Bus Monitor last); no J1939 lamps.
    w.profile.setCurrentIndex(w.profile.findData("obd2"))
    assert shown() == {"OBD-II (Auto)"} and not tabs.tabBar().isVisible()
    pages = [w.obd.pages.tabText(i) for i in range(w.obd.pages.count())]
    assert pages == ["Trouble Codes", "Live Data", "Freeze Frame", "Readiness", "On-Board Tests", "Test Results",
                     "Vehicle Info", "Bus Monitor"]
    assert tabs.currentWidget() is w.obd and not w.lamps.isVisible()
    w.show_tab(w.monitor)                                 # View > Bus Monitor still gets there
    assert w.obd.pages.currentWidget() is w.monitor
    assert not any(a.isEnabled() for a in w._j1939_actions)
    # Connecting the J1939 simulator brings the J1939 screens back (and vice versa).
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    assert profiles.protocol() == profiles.J1939 and "Fault Codes" in shown()
    assert "Bus Monitor" in shown() and tabs.tabBar().isVisible() and w.obd.pages.indexOf(w.monitor) < 0
    w.disconnect()
    w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
    w.connect()
    assert profiles.protocol() == profiles.OBD2 and "OBD-II (Auto)" in shown()
    w.disconnect()
    w.close()
    # Remembered next time.
    w2 = MainWindow()
    assert w2.profile.currentData() == "obd2" and "Fault Codes" not in {
        w2.tabs.widget(i).title for i in range(w2.tabs.count()) if w2.tabs.isTabVisible(i)}
    w2.close()


def test_manage_profiles_dialog(app, monkeypatch):
    from canopener.core import profiles
    from canopener.gui.main_window import MainWindow
    from canopener.gui.profiles_dialog import ProfilesDialog

    w = MainWindow()

    def picker():
        return [w.profile.itemData(i) for i in range(1, w.profile.count())]
    assert picker()[0] == "generic" and "sim-telehandler" in picker()
    w.profile.setCurrentIndex(w.profile.findData("sim-telehandler"))
    dlg = ProfilesDialog(w._all_profiles, "sim-telehandler", w)
    ids = dlg.order()
    i = ids.index("obd2")
    dlg.list.setCurrentRow(i)
    for _ in range(i):
        dlg._move(-1)                                   # OBD-II to the top
    row = dlg.order().index("sim-telehandler")
    dlg.list.item(row).setCheckState(qt.Qt.CheckState.Unchecked)
    assert "unticked" in dlg.current_note.text()        # it's the one in use: warned
    dlg.accept()
    w._load_profiles()
    assert picker()[0] == "obd2" and "sim-telehandler" not in picker()
    assert w.profile.currentData() == ""                # the hidden profile was in use: back to (none)
    assert profiles.list_prefs()[1] == {"sim-telehandler"}
    dlg = ProfilesDialog(w._all_profiles, "", w)        # still listed there, unticked, to turn back on
    dlg._reset()
    dlg._check_all(True)
    dlg.accept()
    w._load_profiles()
    assert picker()[0] == "generic" and "sim-telehandler" in picker()
    w.close()


def test_update_dialog_and_restart(app, monkeypatch):
    from canopener.core import updates
    from canopener.gui.main_window import MainWindow
    from canopener.gui.update_dialog import UpdateDialog

    status = updates.UpdateStatus(True, "An update is available (version 0.1.5): 2 change(s).", latest="0.1.5",
                                  behind=2, commits=["OBD-II freeze frame fixes", "Faster live data"],
                                  changed=["canopener/obd/pids.py", "deploy/linux/70-canopener.rules"])
    monkeypatch.setattr(updates, "check", lambda *a, **k: status)
    applied = []
    monkeypatch.setattr(updates, "apply", lambda *a, **k: applied.append(1) or (True, "Updated. Restart CANopener."))
    w = MainWindow()
    restarts = []
    dlg = UpdateDialog(lambda: restarts.append(1), w)
    pump(app, 0.5)
    assert "0.1.5" in dlg.headline.text() and "Faster live data" in dlg.details.toPlainText()
    assert "run the installer once" in dlg.details.toPlainText()            # deploy/ changed
    assert dlg.install_btn.isEnabled() and dlg.restart_btn.isHidden()
    dlg.install()
    pump(app, 0.5)
    assert applied and dlg.headline.text() == "Update installed." and not dlg.restart_btn.isHidden()
    assert dlg.install_btn.isHidden()
    dlg._restart()
    assert restarts == [1]

    # Up to date: nothing to install.
    monkeypatch.setattr(updates, "check", lambda *a, **k: updates.UpdateStatus(True, "CANopener 0.1.4 is up to date."))
    dlg = UpdateDialog(lambda: None, w)
    pump(app, 0.5)
    assert not dlg.install_btn.isEnabled() and "up to date" in dlg.headline.text()

    # The startup check is opt-in (off by default), and only speaks up when there's an update.
    assert not w.update_on_start.isChecked()
    w.update_on_start.setChecked(True)
    assert w.settings.value("update/check_on_start") in (True, "true")
    monkeypatch.setattr(updates, "check", lambda *a, **k: status)
    w._startup_update_check()
    pump(app, 0.5)
    assert "0.1.5" in w.update_action.text() and w._update_status is status

    # Restart: a desktop app starts a fresh copy; under the Pi's systemd service it exits with
    # the code the service restarts on.
    started, exits = [], []
    monkeypatch.setattr(qt.QtCore.QProcess, "startDetached", lambda *a: started.append(a) or True)
    monkeypatch.setattr(qt.QtWidgets.QApplication, "quit", lambda *a: exits.append(0))
    monkeypatch.setattr(qt.QtWidgets.QApplication, "exit", lambda *a: exits.append(a[-1]))
    monkeypatch.setattr(updates, "under_systemd", lambda: False)
    w.show()
    assert w.restart_app() and started and started[0][1][:2] == ["-m", "canopener"] and exits == [0]
    from canopener.core import config
    assert started[0][2] == config.install_dir()        # not the home folder, where ~/canopener would shadow it
    w2 = MainWindow()
    w2.show()
    monkeypatch.setattr(updates, "under_systemd", lambda: True)
    assert w2.restart_app() and exits[-1] == updates.RESTART_EXIT_CODE


def test_obd_quiet_gateway_port_is_not_a_dropout(app, monkeypatch):
    from canopener.core import connection, link
    from canopener.gui.main_window import MainWindow

    w = MainWindow()
    w.show()
    cues = []
    monkeypatch.setattr(w.conn_panel, "cue", lambda name, **k: cues.append(name))
    w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
    w.connect()
    o = w.obd
    o.check_codes()
    assert _wait(app, lambda: not o._jobs)
    # The simulated car is gatewayed: silent unless asked. Modules answered, so quiet = fine.
    assert _wait(app, lambda: w.bus_lamp.level == link.IDLE, 6)
    assert "gateway" in w.bus_lamp.text() and cues == []
    o.start_stream()                                     # (finds the PIDs first)
    o.stop_stream()                                      # Stop before it got going: cancels the start
    assert _wait(app, lambda: not o._jobs)
    pump(app, 0.3)
    assert not o._streaming and o.stream_btn.text() == "▶ Stream"
    o.start_stream()
    assert _wait(app, lambda: o._streaming and w.bus_lamp.level == link.OK, 6)
    o.stop_stream()
    assert _wait(app, lambda: w.bus_lamp.level == link.IDLE, 6)
    assert cues == []                                    # stopping the stream isn't "traffic lost"
    w.disconnect()
    w.close()


def test_table_columns_fit_contents_and_stay_where_dragged(app):
    from canopener.gui.widgets import FIT_MAX, FIT_PAD, setup_table

    t = qt.QtWidgets.QTableWidget()
    setup_table(t, ["Module", "Parameter", "Value"])
    t.resize(900, 300)
    t.show()
    t.setRowCount(2)
    for r, cells in enumerate((["ECM (7E8)", "Engine speed", "2150 rpm"],
                               ["ECM (7E8)", "Caused by", "P0133  O2 Sensor Circuit Slow Response (Bank 1 Sensor 1)"])):
        for c, text in enumerate(cells):
            t.setItem(r, c, qt.QtWidgets.QTableWidgetItem(text))
    pump(app, 0.1)
    fm = t.fontMetrics()
    assert t.columnWidth(1) < 300                                  # fits "Engine speed", not stretched
    assert t.columnWidth(1) >= fm.horizontalAdvance("Engine speed") + FIT_PAD      # with breathing room
    assert t.horizontalHeader().stretchLastSection()               # the last column takes the slack
    assert t.columnWidth(0) + t.columnWidth(1) + t.columnWidth(2) >= t.viewport().width() - 2
    # A long cell is capped, so it can't push everything else off.
    t.setRowCount(3)
    t.setItem(2, 0, qt.QtWidgets.QTableWidgetItem("x" * 300))
    pump(app, 0.1)
    assert t.columnWidth(0) <= FIT_MAX
    # Dragged by the user: their width stays, even when the table refills.
    t.setProperty("user_sized", True)
    t.setColumnWidth(1, 333)
    t.setRowCount(5)
    pump(app, 0.1)
    assert t.columnWidth(1) == 333
    t.close()


def test_trim_long_capture_on_save(app, tmp_path, monkeypatch):
    import os

    from canopener.core import capture as cap
    from canopener.gui import trim_dialog
    from canopener.gui.main_window import MainWindow

    def long_capture(name):
        folder = tmp_path / name / ".recording_x"
        folder.mkdir(parents=True)
        t0 = 1_700_000_000.0
        with open(folder / cap.CAPTURE_LOG, "w") as f:
            for s in range(3601):
                f.write(f"({t0 + s:.6f}) can0 18FEF100#00112233445566{s % 256:02X}\n")
        c = cap.Capture("Unit 14", plan=["shuts down", "restarts"])
        c.folder, c.root = str(folder), str(tmp_path / name)
        c.started, c.stopped, c.state = t0, t0 + 3600, cap.STOPPED
        c.tag(0, t0 + 3000)
        c.tag(1, t0 + 3030)
        return c, t0

    w = MainWindow()
    log = w.log_panel

    # The dialog: remembered margins, a preview of what's kept, settings saved on the way out.
    c, t0 = long_capture("a")
    log._set_capture(c)
    dlg = trim_dialog.TrimDialog(c, w.settings)
    assert dlg.margins == (300, 120) and "Keeps 07:30.0 of 1:00:00.0" in dlg.preview.text()
    dlg.before.setValue(1)
    assert "Keeps 03:30.0" in dlg.preview.text()            # 1 min + 30 s between tags + 2 min
    dlg._finish(trim_dialog.TRIM)
    assert float(w.settings.value("log/trim_before")) == 1.0

    # Save on a long capture offers the trim: Trim and save.
    results = [trim_dialog.TRIM]
    monkeypatch.setattr(trim_dialog.TrimDialog, "exec", lambda self: results[0])
    folder = log.save()
    assert folder and c.state == cap.SAVED and (c.started, c.stopped) == (t0 + 2940, t0 + 3150)
    assert "Trimmed" in open(os.path.join(folder, cap.SUMMARY_TXT)).read()

    # Save everything: untrimmed. Cancel: nothing saved.
    c, t0 = long_capture("b")
    log._set_capture(c)
    results[0] = trim_dialog.SAVE_ALL
    assert log.save() and (c.started, c.stopped) == (t0, t0 + 3600)
    c, t0 = long_capture("c")
    log._set_capture(c)
    results[0] = 0
    assert log.save() is None and c.state == cap.STOPPED and c.started == t0

    # Pressing Start again saves the last capture quietly: no dialog mid-job.
    monkeypatch.setattr(trim_dialog.TrimDialog, "exec", lambda self: pytest.fail("no prompt on a quiet save"))
    assert log.save(quiet=True) and c.started == t0

    # Trim… on a saved capture trims it and re-saves, so its files match.
    results[0] = trim_dialog.TRIM
    monkeypatch.setattr(trim_dialog.TrimDialog, "exec", lambda self: results[0])
    assert log.trim_saved_btn.isEnabled()
    log.trim()
    assert c.state == cap.SAVED and c.started == t0 + 2940
    assert "Trimmed" in open(os.path.join(c.folder, cap.SUMMARY_TXT)).read()
    w.close()


def test_settings_menu_switches_to_the_handheld_ui(app, tmp_path, monkeypatch):
    from canopener import frontends
    from canopener.core.settings import Settings
    from canopener.gui.main_window import MainWindow

    Q = qt.QtWidgets.QMessageBox
    monkeypatch.setattr(frontends, "available", lambda ui: (True, ""))
    answer = [Q.StandardButton.No]
    monkeypatch.setattr(Q, "question", lambda *a, **k: answer[0])
    w = MainWindow()
    w.show()
    w.handheld_action.setChecked(True)                  # asked, and said No: nothing changes
    assert not w.handheld_action.isChecked() and w.switch_to is None and Settings().get("ui") == ""
    answer[0] = Q.StandardButton.Yes
    w.handheld_action.setChecked(True)
    assert w.switch_to == "device" and Settings().get("ui") == "device" and not w.isVisible()


def test_codes_tab_clears_j1939_codes_after_asking(app, monkeypatch):
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow
    Q = qt.QtWidgets.QMessageBox
    answers, shown = [Q.StandardButton.No], []
    monkeypatch.setattr(Q, "warning", lambda *a, **k: answers[0] if len(a) > 3 else shown.append(a[2]))
    monkeypatch.setattr(Q, "information", lambda *a, **k: shown.append(a[2]))
    w = MainWindow()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 1.5)
    w.codes.clear_codes("active")                     # said No: nothing sent
    assert not w.ctx.busy and w.ctx.session.tx_count == 0
    answers[0] = Q.StandardButton.Yes
    w.codes.ecu.addItem("00 Engine #1", 0x00)          # (normally filled by a read)
    w.codes.ecu.setCurrentIndex(w.codes.ecu.findData(0x00))
    w.codes.clear_codes("active")
    end = time.time() + 15
    while time.time() < end and (w.ctx.busy or not shown):
        pump(app, 0.2)
    assert shown and "00 Engine #1: DM11 ACK" in shown[-1]
    assert w.ctx.result is not None and not any(r.spn == 3719 for r in w.ctx.result.rows)
    w.disconnect()
    w.close()


def test_python_can_adapter_from_the_interface_box(app, monkeypatch):
    pytest.importorskip("can")
    from canopener.can.pythoncan import PythonCanBackend
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow
    from canopener.sim import MachineSimulator
    sim = MachineSimulator(PythonCanBackend("virtual:gui1"), seed=1)
    sim.start()
    try:
        w = MainWindow()
        w.show()
        w.source.setCurrentIndex(w.source.findData(connection.SOCKETCAN))
        w._refresh_interfaces(preferred="virtual:gui1")
        assert w.iface.currentText() == "virtual:gui1"            # kept, not swapped for a Linux interface
        w.bitrate.setCurrentIndex(w.bitrate.findData(250000))
        w.connect()
        assert w.ctx.live and "python-can" in w.conn_label.text()
        pump(app, 1.0)
        assert w.ctx.session.rx_count > 10
        w.disconnect()
        w.close()
    finally:
        sim.stop()


def test_simulators_are_hidden_until_switched_on(app):
    from canopener.core import connection
    from canopener.core.settings import Settings
    from canopener.gui.main_window import MainWindow
    Settings().set("show_simulators", False)
    w = MainWindow()
    kinds = lambda: [w.source.itemData(i) for i in range(w.source.count())]   # noqa: E731
    assert kinds() == [connection.SOCKETCAN, connection.REPLAY] and not w.simulators_action.isChecked()
    w.simulators_action.setChecked(True)                    # Connection > Show simulators
    assert connection.OBD_SIM in kinds() and Settings().get("show_simulators") is True
    w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
    w.simulators_action.setChecked(False)                   # hidden again: back to the adapter
    assert connection.OBD_SIM not in kinds() and w.source.currentData() == connection.SOCKETCAN
    assert not w.iface.isHidden()                           # the adapter's controls came back
    w.show_simulators(True, save=False)                     # canopener --sim: this run only
    assert connection.SIMULATOR in kinds() and Settings().get("show_simulators") is False
    w.close()


def test_labeling_from_the_gui(app, tmp_path, monkeypatch):
    """Recover a signal the profile lacks: label the simulator's boom position on the generic
    profile, name its module, find it with 'Find what changed', then Save profile as."""
    from canopener.core import connection, labeling, profiles
    from canopener.gui.changes_dialog import ChangesDialog
    from canopener.gui.label_dialog import LabelDialog, name_module
    from canopener.gui.labels_dialog import LabelsDialog
    from canopener.gui.main_window import MainWindow
    from canopener.j1939.addresses import sa_name

    answers = iter([("Sim truck", True), ("Boom ECU", True), ("Sim truck B", True)])
    monkeypatch.setattr(qt.QtWidgets.QInputDialog, "getText", lambda *a, **k: next(answers))
    w = MainWindow()
    w.profile.setCurrentIndex(w.profile.findData("generic"))
    w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
    w.connect()
    pump(app, 1.5)

    pid = w.ctx.label_target()                      # generic is built in: asks for a name, makes one
    assert profiles.active().id == pid and profiles.active().extends == "generic"
    dlg = LabelDialog(w.ctx, pid, {"pgn": 0xFF10, "sa": 0x27, "start": 1, "length": 1})
    dlg.name.setText("Boom (mine)")
    dlg.size.setCurrentIndex(dlg.size.findData(2))
    dlg.scale.setText("0.1")
    dlg.unit.setCurrentText("deg")
    pump(app, 0.5)
    assert "°" in dlg.value_label.text() and not dlg.error.text()
    dlg.accept()
    assert dlg.saved is not None
    pump(app, 0.6)
    w.live.sample()
    row = next(r for r in w.live.rows.values() if r.sig.name == "Boom (mine)")
    assert row.value is not None and 11 <= row.value <= 92        # 512 +- 400 counts x 0.1

    assert w.ctx.label_target() == pid             # now the profile in use is ours: no question
    assert name_module(w.ctx, 0x27, w)
    assert sa_name(0x27, with_hex=False) == "Boom ECU"
    w.network.refresh(force=True)

    pump(app, 2.5)
    ch = ChangesDialog(w.ctx, lambda d: None)
    now = ch.now()
    ch.a.set_range(now - 4.5, now - 3.0)
    ch.b.set_range(now - 1.5, now)
    ch.find()
    assert ch.summary.text()
    ch.close()

    labels = LabelsDialog(w.ctx, lambda *a: None)
    assert labels.signals.rowCount() == 1 and labels.modules.rowCount() == 1
    labels.close()

    w._save_profile_as()
    assert profiles.active().name == "Sim truck B" and profiles.active().id != pid
    assert {s.name for s, _ in labeling.own_signals(profiles.active().id)} == {"Boom (mine)"}
    w.disconnect()
    w.close()


def test_canopen_in_the_same_screens(app, monkeypatch):
    """A CANopen truck in the J1939 screens: codes, network (node info, NMT), machine info, live
    data, Bus Monitor and labeling an 11-bit PDO."""
    from canopener.canopen import nmt
    from canopener.core import connection, labeling, profiles
    from canopener.core.canopen import CanopenCode, node_src
    from canopener.gui.label_dialog import LabelDialog
    from canopener.gui.main_window import MainWindow

    Yes = qt.QtWidgets.QMessageBox.StandardButton.Yes
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "warning", lambda *a, **k: Yes)
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(qt.QtWidgets.QInputDialog, "getText", lambda *a, **k: ("My truck", True))
    w = MainWindow()
    w.show()
    w.profile.setCurrentIndex(w.profile.findData("generic"))
    w.source.setCurrentIndex(w.source.findData(connection.CANOPEN_SIM))
    w.connect()
    assert profiles.active().id == "sim-canopen"           # a CANopen profile, picked for the sim
    pump(app, 2.5)
    assert w.traffic_label.text().endswith("3 CANopen node(s)") and not w.lamps.isVisible()
    assert not w.codes.preset_buttons["emissions"].isVisible() and not w.network.claim_btn.isVisible()

    assert w.ctx.run_preset("full")
    pump(app, 4.0)
    assert w.ctx.result is not None and not w.ctx.busy and w.ctx.result.j1939_skipped
    co_rows = [r for r in w.codes._items if isinstance(r, CanopenCode)]
    assert ("Active", 0x01, 0x4200) in {(c.status, c.node, c.code) for c in co_rows}
    t = w.codes.table                      # (selectRow would toggle if an earlier test left Ctrl "held")
    flags = qt.QtCore.QItemSelectionModel.SelectionFlag
    t.selectionModel().select(t.model().index(w.codes._items.index(co_rows[0]), 0),
                              flags.ClearAndSelect | flags.Rows)
    assert "CANopen EMCY" in w.codes.detail.toPlainText()
    w.codes.ecu.setCurrentIndex(w.codes.ecu.findData(node_src(0x01)))
    assert {c.node for c in w.codes.co_rows} == {0x01} and not w.codes.rows
    w.codes.ecu.setCurrentIndex(0)

    titles = [w.info.tree.topLevelItem(i).text(0) for i in range(w.info.tree.topLevelItemCount())]
    assert any("Traction controller (sim)  (CANopen)" in t for t in titles)

    w.network.refresh(force=True)
    w.network.tree.setCurrentItem(w.network.items[("node", 0x02)])
    assert w.network.selected_node() == 0x02 and w.network.info_btn.isEnabled()
    w.network.send_nmt(nmt.STOP)
    pump(app, 0.8)
    assert w.ctx.connection.simulator.nmt_log[-1] == (nmt.STOP, 0x02)
    w.network.send_nmt(nmt.START, every=True)
    w.network.read_node_info()
    pump(app, 1.5)

    w.live.sample()
    assert any(r.sig.name == "Motor speed" for r in w.live.rows.values())
    w.monitor.refresh()
    labels = {w.monitor.by_id.data(w.monitor.by_id.index(i, 2)) for i in range(len(w.monitor.by_id.order))}
    assert "TPDO1 node 01" in labels

    w.profile.setCurrentIndex(w.profile.findData("generic"))       # a J1939 profile: no CANopen anywhere
    w.network.refresh(force=True)
    assert not any(isinstance(k, tuple) and k[0] == "node" for k in w.network.items)
    assert not w.network.nmt_btn.isVisible() and w.lamps.isVisible()
    w._last_detect = 0
    w._check_detection(w.ctx.session)               # ... but it notices, and offers Generic CANopen
    assert w._detected.id == "generic-canopen" and "CANopen bus (3 node(s))" in w.detect_label.text()
    w._use_detected()
    assert profiles.active().id == "generic-canopen"

    pid = w.ctx.label_target()                      # generic-canopen is built in: a profile of our own
    dlg = LabelDialog(w.ctx, pid, {"id": 0x182, "start": 0, "length": 2})
    dlg.name.setText("Pump (mine)")
    pump(app, 0.5)
    assert not dlg.error.text() and dlg.value_label.text()
    dlg.accept()
    assert labeling.own_signals(pid)[0][1]["id"] == "0x182"
    w.disconnect()
    w.close()
