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
