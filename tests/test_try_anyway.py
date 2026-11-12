"""Try anyway: live values from a vehicle that lists none (like the ISO 9141-2 forklift whose
engine computer answers 0100 with an all-zero mask), and the vehicle protocol in captures."""

import time

import pytest

from canopener.core.connection import ELM_SIM, open_connection
from canopener.core.obd import ObdClient
from canopener.core.settings import Settings
from canopener.sim.obd import ObdSimulator


@pytest.fixture
def lists_nothing(monkeypatch):
    """Every module answers 'supported PIDs' with an empty mask, but still answers the PIDs."""
    monkeypatch.setattr(ObdSimulator, "_support",
                        classmethod(lambda cls, head, pids, base: [head + b"\0\0\0\0"] if base == 0 else []))


def _wait(cond, tick=None, seconds=20.0):
    end = time.time() + seconds
    while time.time() < end:
        if tick:
            tick()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_core_try_unlisted(lists_nothing):
    conn = open_connection(ELM_SIM, "sim:iso9141")
    try:
        c = ObdClient(conn.session)
        assert c.live_pids() == []                          # what the forklift said
        found = c.try_unlisted([0x0C, 0x05, 0x4F])           # 0x4F: nobody answers it
        pids = {pid for _, pid in found}
        assert {0x0C, 0x05} <= pids and 0x4F not in pids
    finally:
        conn.close()


def test_handheld_offers_try_anyway_and_records_the_protocol(lists_nothing, tmp_path):
    from canopener.device.context import DeviceContext
    s = Settings()
    s.update({"profile": "generic-obd2", "elm.sim_vehicle": "iso9141"})
    ctx = DeviceContext(s, start_meters=False)
    try:
        ctx.connect("elm-sim")
        assert ctx.wait_idle(20) and ctx.live and ctx.obd_streamer is not None
        ctx.toggle_obd_stream()
        assert _wait(lambda: ctx.obd_none_listed, ctx.tick)
        assert "Try anyway" in ctx.current_toast() and not ctx.obd_streamer.running
        ctx.try_obd_anyway()
        assert _wait(lambda: ctx.obd_streamer.running, ctx.tick, 30)
        assert not ctx.obd_none_listed and "answered" in ctx.current_toast()
        assert _wait(lambda: any(r.sig.pid == 0x0C for r in ctx.obd_live.rows.values()),
                     lambda: (ctx.tick(), ctx.obd_live.sample(ctx.session)))
        assert ctx.start_log()
        time.sleep(0.5)
        ctx.stop_log()
        meta = ctx.capture.meta
        assert meta["vehicle_protocol"].startswith("ISO 9141-2") and meta["adapter_chip"]
    finally:
        ctx.shutdown()


def test_desktop_try_anyway_button(lists_nothing, tmp_path):
    qt = pytest.importorskip("canopener.gui.qt", exc_type=ImportError)
    from canopener.core import connection
    from canopener.gui.main_window import MainWindow
    app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    s = Settings()
    s.update({"show_simulators": True, "elm.sim_vehicle": "iso9141"})
    qt.QtCore.QSettings.setDefaultFormat(qt.QtCore.QSettings.Format.IniFormat)
    qt.QtCore.QSettings.setPath(qt.QtCore.QSettings.Format.IniFormat, qt.QtCore.QSettings.Scope.UserScope,
                                str(tmp_path / "qs"))
    w = MainWindow()
    w.uploads.stop()
    w.show()
    w.source.setCurrentIndex(w.source.findData(connection.ELM_SIM))
    w.connect()
    assert _wait(lambda: w.ctx.live, lambda: pump(0.1))
    o = w.obd
    assert o.try_btn.isHidden()
    o.read_live()
    assert _wait(lambda: not o.try_btn.isHidden(), lambda: pump(0.1), 30)
    o.try_btn.click()
    assert _wait(lambda: o.try_btn.isHidden() and not o._jobs, lambda: pump(0.1), 30)
    pump(0.5)
    assert any(r.sig.pid == 0x0C for r in o.model.rows.values())
    w.close()
