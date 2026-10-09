"""Label preview: shows the real 1-bit rendering and lets the user move/resize elements.

The tape runs horizontally. The hatched stripes at the top and bottom are outside the print head's
reach, the ones at the left and right end are the blank feed margins.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QWidget

from ptdesk.render.label import Element, Label, printable_band

MARGIN = 28
HANDLE = 9
GRID_MM = 0.5
FINE_GRID_MM = 0.1
MIN_SIZE_MM = 1.0


@dataclass
class _Drag:
    mode: str  # "move" | "resize"
    start: QPointF  # mm
    geometry: tuple[float, float, float, float]


def _snap(value: float, grid: float) -> float:
    return round(round(value / grid) * grid, 2)


class LabelCanvas(QWidget):
    selection_changed = Signal(int)
    geometry_changed = Signal(int)
    delete_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._label = Label()
        self._view = self._label  # geometry shown (with automatic length: the fitted copy)
        self._preview: QImage | None = None
        self._dpi = 180
        self._printable_px = 64
        self._selected = -1
        self._drag: _Drag | None = None

    # --- state -------------------------------------------------------------

    def set_label(self, label: Label) -> None:
        self._label = label
        self._view = label
        self._drag = None
        self.set_selected(-1)

    def set_view(self, view: Label) -> None:
        """Geometry to display; same elements in the same order as the edited label."""
        self._view = view if len(view.elements) == len(self._label.elements) else self._label
        self.update()

    def set_preview(self, image: Image.Image) -> None:
        gray = image.convert("L")
        self._preview = QImage(gray.tobytes(), gray.width, gray.height, gray.width, QImage.Format.Format_Grayscale8).copy()
        self.update()

    def set_printer_geometry(self, dpi: int, printable_px: int) -> None:
        self._dpi, self._printable_px = dpi, printable_px
        self.update()

    def selected(self) -> int:
        return self._selected

    def set_selected(self, index: int) -> None:
        index = index if 0 <= index < len(self._label.elements) else -1
        if index != self._selected:
            self._selected = index
            self.selection_changed.emit(index)
        self.update()

    def _element(self, index: int) -> Element | None:
        return self._label.elements[index] if 0 <= index < len(self._label.elements) else None

    # --- coordinates -------------------------------------------------------

    def _scale(self) -> float:
        """Widget pixels per millimetre."""
        w = max(self.width() - 2 * MARGIN, 10)
        h = max(self.height() - 2 * MARGIN, 10)
        return min(w / self._view.length_mm, h / self._view.tape_mm)

    def _origin(self) -> QPointF:
        s = self._scale()
        return QPointF(
            (self.width() - self._view.length_mm * s) / 2,
            (self.height() - self._view.tape_mm * s) / 2,
        )

    def _to_widget(self, x: float, y: float, w: float, h: float) -> QRectF:
        s, o = self._scale(), self._origin()
        return QRectF(o.x() + x * s, o.y() + y * s, w * s, h * s)

    def _to_mm(self, pos: QPointF) -> QPointF:
        s, o = self._scale(), self._origin()
        return QPointF((pos.x() - o.x()) / s, (pos.y() - o.y()) / s)

    def _element_rect(self, element: Element) -> QRectF:
        shown = self._shown(element)
        return self._to_widget(shown.x, shown.y, shown.width, shown.height)

    def _shown(self, element: Element) -> Element:
        try:
            return self._view.elements[self._label.elements.index(element)]
        except (ValueError, IndexError):
            return element

    def _handle_rect(self, element: Element) -> QRectF:
        r = self._element_rect(element)
        return QRectF(r.right() - HANDLE / 2, r.bottom() - HANDLE / 2, HANDLE, HANDLE)

    def _hit(self, pos: QPointF) -> int:
        for i in range(len(self._label.elements) - 1, -1, -1):
            if self._element_rect(self._label.elements[i]).adjusted(-2, -2, 2, 2).contains(pos):
                return i
        return -1

    # --- painting ----------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window).darker(118))

        paper = self._to_widget(0, 0, self._view.length_mm, self._view.tape_mm)
        p.fillRect(paper.translated(3, 3), QColor(0, 0, 0, 70))
        p.fillRect(paper, Qt.GlobalColor.white)
        if self._preview is not None:
            p.drawImage(paper, self._preview)

        self._paint_unprintable(p, paper)

        for i, element in enumerate(self._label.elements):
            rect = self._element_rect(element)
            if i == self._selected:
                continue
            color = QColor(220, 50, 50) if element.validate() else QColor(120, 140, 170)
            p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)

        selected = self._element(self._selected)
        if selected is not None:
            accent = self.palette().color(QPalette.ColorRole.Highlight)
            p.setPen(QPen(accent, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._element_rect(selected))
            p.setBrush(accent)
            p.drawRect(self._handle_rect(selected))

        p.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        p.drawText(
            QRectF(paper.left(), paper.bottom() + 4, paper.width(), MARGIN - 6),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            f"{self._view.length_mm:g} mm lang · {self._view.tape_mm:g} mm Band"
            + (" · Länge automatisch" if self._label.auto_length else ""),
        )

    def _paint_unprintable(self, p: QPainter, paper: QRectF) -> None:
        px_per_mm = self._dpi / 25.4
        s = self._scale()
        tape_px = round(self._view.tape_mm * px_per_mm)
        top, bottom = printable_band(tape_px, self._printable_px)
        band = QBrush(QColor(220, 60, 60, 110), Qt.BrushStyle.BDiagPattern)
        if top > 0:
            p.fillRect(QRectF(paper.left(), paper.top(), paper.width(), top / px_per_mm * s), band)
        if bottom < tape_px:
            y = paper.top() + bottom / px_per_mm * s
            p.fillRect(QRectF(paper.left(), y, paper.width(), paper.bottom() - y), band)
        margin = min(self._view.margin_mm, self._view.length_mm / 2) * s
        feed = QBrush(QColor(120, 120, 120, 90), Qt.BrushStyle.FDiagPattern)
        p.fillRect(QRectF(paper.left(), paper.top(), margin, paper.height()), feed)
        p.fillRect(QRectF(paper.right() - margin, paper.top(), margin, paper.height()), feed)

    # --- interaction -------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        pos = event.position()
        selected = self._element(self._selected)
        if selected is not None and self._handle_rect(selected).adjusted(-3, -3, 3, 3).contains(pos):
            mode, index = "resize", self._selected
        else:
            mode, index = "move", self._hit(pos)
            self.set_selected(index)

        element = self._element(index)
        if element is not None:
            self._drag = _Drag(mode, self._to_mm(pos), (element.x, element.y, element.width, element.height))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        element = self._element(self._selected)
        if self._drag is None or element is None:
            self._update_cursor(pos)
            return

        grid = FINE_GRID_MM if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else GRID_MM
        current = self._to_mm(pos)
        dx, dy = current.x() - self._drag.start.x(), current.y() - self._drag.start.y()
        x, y, w, h = self._drag.geometry

        if self._drag.mode == "move":
            element.x, element.y = _snap(x + dx, grid), _snap(y + dy, grid)
        else:
            new_w = max(MIN_SIZE_MM, _snap(w + dx, grid))
            new_h = max(MIN_SIZE_MM, _snap(h + dy, grid))
            element.width, element.height = new_w, new_h

        self.geometry_changed.emit(self._selected)
        self.update()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._drag = None

    def _update_cursor(self, pos: QPointF) -> None:
        selected = self._element(self._selected)
        if selected is not None and self._handle_rect(selected).adjusted(-3, -3, 3, 3).contains(pos):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif self._hit(pos) >= 0:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        element = self._element(self._selected)
        if element is None:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit()
            return

        step = FINE_GRID_MM if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else GRID_MM
        moves = {
            Qt.Key.Key_Left: (-step, 0),
            Qt.Key.Key_Right: (step, 0),
            Qt.Key.Key_Up: (0, -step),
            Qt.Key.Key_Down: (0, step),
        }
        if key not in moves:
            super().keyPressEvent(event)
            return
        dx, dy = moves[key]
        element.x, element.y = round(element.x + dx, 2), round(element.y + dy, 2)
        self.geometry_changed.emit(self._selected)
        self.update()
