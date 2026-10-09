"""Shared pieces for serial printing in the property editors: label context, table icon, column button."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QInputDialog, QMenu, QToolButton, QWidget

from ptdesk.render import merge
from ptdesk.render.label import Element, Label


@dataclass
class ElementContext:
    """The label around the edited element: its table columns and the row shown in the preview."""

    label: Callable[[], Label] = Label
    record: Callable[[], Mapping[str, str]] = dict

    @property
    def sample(self) -> dict[str, str]:
        return self.label().sample

    def columns(self) -> list[str]:
        return merge.column_names(self.label())

    def unique_column(self, base: str) -> str:
        return merge.unique_column(self.label(), base)

    def resolve(self, element: Element) -> Element:
        """The element with the placeholders filled from the preview row (unknown ones stay)."""
        if not any(merge.PLACEHOLDER.search(s.get()) for s in merge.slots(element)):
            return element
        return merge.fill_element(element, self.record())[0]

    def check(self, element: Element) -> str | None:
        """Problem of the element as previewed; placeholders without a value are not an error."""
        resolved = self.resolve(element)
        if any(merge.PLACEHOLDER.search(s.get()) for s in merge.slots(resolved)):
            return None
        return resolved.validate()


@lru_cache(maxsize=1)
def table_icon() -> QIcon:
    icon = QIcon()
    color = QColor("#2f6fbf")
    for size in (16, 24, 32, 48):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        line = max(1, size // 16)
        m = max(1, size // 8)
        rect = QRect(m, m + size // 8, size - 2 * m - line, size - 2 * m - size // 4 - line)
        p.fillRect(QRect(rect.x(), rect.y(), rect.width() + line, max(2, size // 5)), color)
        p.setPen(QPen(color, line))
        p.drawRect(rect)
        for i in (1, 2):
            x = rect.x() + rect.width() * i // 3
            p.drawLine(x, rect.y(), x, rect.bottom())
        for i in (1, 2):
            y = rect.y() + max(2, size // 5) + (rect.height() - max(2, size // 5)) * i // 3
            p.drawLine(rect.x(), y, rect.right(), y)
        p.end()
        icon.addPixmap(pixmap)
    return icon


class ColumnButton(QToolButton):
    """Menu button that inserts a ``{column}`` placeholder into a text field."""

    def __init__(
        self, context: Callable[[], ElementContext], insert: Callable[[str], None], base: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._insert = insert
        self._base = base
        self.setText("Tabellenspalte einfügen")
        self.setIcon(table_icon())
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolTip(
            "Für den Seriendruck: {Spalte} wird beim Drucken durch den Wert aus der Tabelle ersetzt, "
            "z. B. „Raum {Raum}“."
        )
        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._fill_menu)
        self.setMenu(self._menu)

    def _fill_menu(self) -> None:
        self._menu.clear()
        names = self._context().columns()
        for name in names:
            self._menu.addAction(name, lambda n=name: self._insert("{" + n + "}"))
        if names:
            self._menu.addSeparator()
        self._menu.addAction("Neue Spalte …", self._new_column)

    def _new_column(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Neue Tabellenspalte", "Name der Spalte:", text=self._context().unique_column(self._base)
        )
        name = " ".join(name.replace("{", "").replace("}", "").split())
        if ok and name:
            self._insert("{" + name + "}")
