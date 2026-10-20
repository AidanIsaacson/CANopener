"""Desktop GUI entry point: ``canopener`` or ``python -m canopener``."""

import argparse
import os
import sys

from .. import __version__


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canopener", description="CANopener J1939 diagnostics (desktop GUI)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--sim", action="store_true", help="connect to the built-in simulator on start")
    ap.add_argument("--iface", help="connect to this SocketCAN interface on start (e.g. can0)")
    ap.add_argument("--log", help="open and decode a candump log file on start")
    ap.add_argument("--fullscreen", action="store_true", help="start full screen (touch panels)")
    ap.add_argument("--scale", type=float, help="UI scale factor, e.g. 1.5 for a small high-DPI touch screen")
    a = ap.parse_args(argv)

    if a.scale:
        os.environ["QT_SCALE_FACTOR"] = str(a.scale)
    try:
        from .qt import QtWidgets
    except ImportError:
        print("The desktop GUI needs PySide6 (pip install PySide6-Essentials) or PyQt6 "
              "(sudo apt install python3-pyqt6).\nThe command-line tool works without it: canopener-cli --help",
              file=sys.stderr)
        return 1
    from ..core import connection
    from .main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("CANopener")
    app.setOrganizationName("CANopener")
    app.setStyleSheet("QPushButton[big='true'] { font-weight: bold; font-size: 11pt; padding: 6px 14px; }")
    win = MainWindow()
    if a.fullscreen:
        win.showFullScreen()
    else:
        win.show()
    if a.log:
        win.open_log(a.log)
    elif a.sim:
        win.source.setCurrentIndex(win.source.findData(connection.SIMULATOR))
        win.connect()
    elif a.iface:
        win.source.setCurrentIndex(win.source.findData(connection.SOCKETCAN))
        win.iface.setCurrentText(a.iface)
        win.connect()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
