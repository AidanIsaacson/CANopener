"""Retake the README screenshots (docs/images/) from the real app, offscreen, against the
built-in simulators:

    python scripts/readme_screenshots.py            # writes docs/images/*.png

Each shot is cropped to the part of the window it shows off. The signal finder's attempts are
fed in by hand (there's no real adapter to search), everything else is the app as it runs.
"""
import os
import sys
import tempfile
import time

# Set up a throwaway, offscreen environment *before* importing Qt / the app (hence the E402s).
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()           # throwaway captures and settings
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "docs", "images")

from canopener.core import capture as cap  # noqa: E402
from canopener.core import connection, link  # noqa: E402
from canopener.gui import log_panel as _lp  # noqa: E402
from canopener.gui.main_window import MainWindow  # noqa: E402
from canopener.gui.qt import QtCore, QtWidgets  # noqa: E402
from canopener.gui.trim_dialog import TrimDialog  # noqa: E402
from canopener.gui.widgets import fit_columns  # noqa: E402

app = QtWidgets.QApplication([])
app.setStyle("Fusion")
QtCore.QSettings.setPath(QtCore.QSettings.Format.NativeFormat, QtCore.QSettings.Scope.UserScope, tempfile.mkdtemp())

_lp.LogPanel.check_pending = lambda self: None                                  # no recovery prompt
_lp.LogPanel.confirm_unsaved = lambda self: True                                # close without asking
QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: None)


def pump(s):
    end = time.time() + s
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait(cond, secs=20):
    end = time.time() + secs
    while not cond() and time.time() < end:
        pump(0.1)


def save(widget, name, rect=None):
    img = widget.grab(rect) if rect is not None else widget.grab()
    img.save(os.path.join(OUT, name))
    print("saved", name, img.width(), "x", img.height())


def area(win, *widgets, pad=4):
    """The window rectangle covering these widgets (a crop of just the part that matters)."""
    r = None
    for wdg in widgets:
        g = QtCore.QRect(wdg.mapTo(win, QtCore.QPoint(0, 0)), wdg.size())
        r = g if r is None else r.united(g)
    return r.adjusted(-pad, -pad, pad, pad)


def window():
    w = MainWindow()
    w.resize(1180, 640)
    w.show_simulators(True, save=False)          # the screenshots run against the simulators
    w.show()
    pump(0.3)
    return w


# 1. J1939 Fault Codes after a full scan, telehandler simulator, lamps lit.
w = window()
w.log_panel.hide()
w.source.setCurrentIndex(w.source.findData(connection.SIMULATOR))
w.connect()
pump(6)                                          # warm up until the intermittent code shows
w.ctx.run_preset("full")
wait(lambda: w.ctx.result is not None and not w.ctx.busy)
pump(0.5)
w.codes.table.selectRow(0)
w.conn_panel.hide()
pump(0.3)
save(w, "j1939-fault-codes.png", area(w, w.lamps, w.tabs))

# 2. Recording a job: three timestamps on the live chart, Log panel open.
w.conn_panel.hide()
w.log_panel.show()
w.tabs.setCurrentWidget(w.live)
pump(0.5)
log = w.log_panel
log.name.setText("Unit 14 - shuts down after warm-up")
for label in ("Key on, start", "Shuts down", "Key cycled, restarts"):
    log.capture.add_planned(label)
log._sync()
picked = []
for name in ("Engine speed", "Oil pressure", "Coolant temp"):
    key = next((k for k in w.live.order if w.live.rows[k].sig.name == name), None)
    if key:
        picked.append(key)
        w.live.model.select(key)
w.live._rebuild_table()
w.live._sync_chart()
w.live.only_checked.setChecked(True)             # just the three ticked rows: the chart gets the room
w.live.findChild(QtWidgets.QSplitter).setSizes([130, 420])
log.start()
for gap in (4, 9, 7):
    pump(gap)
    log.handle_action("timestamp")
pump(5)
w.live.refresh(force=True)
pump(0.5)
save(w, "log-timestamps.png", area(w, w.live, log))
log.stop()
log.capture.discard()
w.disconnect()
w.close()

# 3. Signal finder: locked on, with its history (the attempts are shown as they'd appear).
w = window()
w.log_panel.hide()
link.adapter_status = lambda *a, **k: link.Status(link.OK, "Adapter can0", "can0 is present (up).")
link.bus_status = lambda *a, **k: link.Status(link.OK, "Bus: traffic", "Good CAN traffic from the machine.")
p = w.conn_panel
w.source.setCurrentIndex(w.source.findData(connection.SOCKETCAN))
w.iface.setCurrentText("can0")
w.bitrate.setCurrentIndex(w.bitrate.findData(0))
class _Finder:
    def stop(self):
        pass


p.locked.disconnect()                    # don't actually connect to can0 when it "locks"
p.cue = lambda *a, **k: None
p.finder = _Finder()
p._set_running(True)
for ev in (link.FinderEvent("searching", "250k: nothing heard", 250000),
           link.FinderEvent("searching", "500k: nothing heard", 500000),
           link.FinderEvent("searching", "250k: errors only (wrong bitrate or wiring?)", 250000),
           link.FinderEvent("heard", "Hearing traffic at 250k; checking it's steady", 250000)):
    p.handle_event(ev)
p.handle_event(link.FinderEvent("locked", "Locked on 250k: 412 good frame(s) in 1 s", 250000, 412, 0))
p.show()
p.setFloating(True)
p.resize(390, 720)
pump(0.5)
save(p, "signal-finder.png")
w.close()

# 4/5. OBD-II: trouble codes, then a code's sensors in Live Data.
w = window()
w.log_panel.hide()
w.conn_panel.hide()
w.source.setCurrentIndex(w.source.findData(connection.OBD_SIM))
w.connect()
pump(0.5)
o = w.obd
o.check_codes()
wait(lambda: not o._jobs and o.codes.rowCount() >= 5)
row = next(i for i, (_, _, code) in enumerate(o._codes) if code == "P0171")    # lean bank 1: trims + MAF
o.select_code(row)
w.resize(960, 520)
pump(0.5)
save(o.pages.widget(0), "obd-trouble-codes.png")
o._show_code_live()
wait(lambda: o._streaming and o.live.plotted)
o.live.only_checked.setChecked(True)
o.live.window.setCurrentIndex(0)                 # 30 s chart
o.live.findChild(QtWidgets.QSplitter).setSizes([150, 360])
fit_columns(o.live.table)
pump(20)
w.obd.live.refresh(force=True)
pump(0.3)
w.resize(980, 640)
pump(0.3)
save(o.live, "code-to-live-data.png")
o.stop_stream()
w.disconnect()
w.close()

# 6. The trim offer, for an hour-long capture with its fault tagged near the end.
folder = tempfile.mkdtemp()
t0 = time.time() - 4000
with open(os.path.join(folder, cap.CAPTURE_LOG), "w") as f:
    for s in range(0, 3600, 2):
        f.write(f"({t0 + s:.6f}) can0 18FEF100#0011223344556677\n" * 400)
c = cap.Capture("Unit 14", plan=["Shuts down", "Key cycled", "Restarts"])
c.folder, c.started, c.stopped, c.state = folder, t0, t0 + 3632, cap.STOPPED
for i, s in enumerate((3410, 3441, 3470)):
    c.tag(i, t0 + s)
w = window()
d = TrimDialog(c, w.settings, parent=w)
d.show()
pump(0.3)
save(d, "trim-dialog.png")
w.close()
