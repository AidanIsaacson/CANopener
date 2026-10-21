"""A layout that arranges widgets left-to-right and wraps to the next line when it runs out of
width, like words in a paragraph. Qt has no built-in one.

We use it for control rows (scan buttons, filters) so a window can be made narrow enough to fit a
small laptop screen: a QHBoxLayout refuses to shrink below the sum of its children's widths, which
pins the whole window's minimum width wide and stops it maximising / snapping. A flow row's
minimum width is just its widest single child, so it wraps instead of blocking the resize.

Adapted from Qt's canonical FlowLayout example; works on PySide6 and PyQt6.
"""

from .qt import QtCore, QtWidgets


class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: list[QtWidgets.QLayoutItem] = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QtCore.QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        line_height = 0
        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()
            if x > rect.x() + m.left() and x + w > right:
                x = rect.x() + m.left()
                y = y + line_height + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), item.sizeHint()))
            x = x + w + self._spacing
            line_height = max(line_height, h)
        return y + line_height + m.bottom() - rect.y()
