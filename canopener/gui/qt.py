"""Qt binding shim: PySide6 (pip) or PyQt6 (``apt install python3-pyqt6`` on Raspberry Pi OS).

Always use fully scoped enums (``Qt.AlignmentFlag.AlignLeft``) so both bindings work.
"""

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal

    QT_API = "PySide6"
except ImportError as pyside_error:      # pragma: no cover - depends on what's installed
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtCore import Qt
        from PyQt6.QtCore import pyqtSignal as Signal
    except ImportError:
        # Report the PySide6 problem: it's usually a missing system library (e.g. libEGL).
        raise pyside_error from None

    QT_API = "PyQt6"

__all__ = ["QtCore", "QtGui", "QtWidgets", "Qt", "Signal", "QT_API"]
