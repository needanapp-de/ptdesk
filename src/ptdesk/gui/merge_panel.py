"""Table view for serial printing: one row per label with its check state and problems."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ptdesk.gui.merge_widgets import table_icon
from ptdesk.render.merge import MergedRow
from ptdesk.table import Table

FIXED_COLUMNS = ("Drucken", "Zeile", "Status")
OK_COLOR = QColor("#208020")
ERROR_COLOR = QColor("#c03030")
MUTED_COLOR = QColor("#808080")


def _row_key(record: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(record.items()))


class MergePanel(QWidget):
    row_selected = Signal(int)  # index into the table rows, -1 = none
    checks_changed = Signal()
    reload_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: Table | None = None
        self._results: list[MergedRow] = []
        self._messages: list[str] = []
        # remembered by row content, so they survive reloading the edited file
        self._printed: set[tuple[tuple[str, str], ...]] = set()
        self._unchecked: set[tuple[tuple[str, str], ...]] = set()
        self._seen: set[tuple[tuple[str, str], ...]] = set()
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)

        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(table_icon().pixmap(20, 20))
        top.addWidget(icon)
        self._title = QLabel()
        font = QFont(self._title.font())
        font.setBold(True)
        self._title.setFont(font)
        top.addWidget(self._title)
        self._summary = QLabel()
        top.addWidget(self._summary, 1)
        for text, tip, slot in (
            ("Alle", "Alle fehlerfreien Zeilen drucken", lambda: self._check_all(True)),
            ("Keine", "Keine Zeile drucken", lambda: self._check_all(False)),
            ("Neu laden", "Tabelle neu einlesen (passiert auch automatisch beim Speichern in Excel)", self.reload_requested),
            ("Schließen", "Seriendruck beenden", self.close_requested),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            top.addWidget(button)
        layout.addLayout(top)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "background: rgba(220,160,0,0.13); border: 1px solid rgba(220,160,0,0.6); border-radius: 4px; padding: 4px;"
        )
        self._banner.setVisible(False)
        layout.addWidget(self._banner)

        self._table = QTableWidget()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.currentCellChanged.connect(lambda row, *_: self.row_selected.emit(row))
        layout.addWidget(self._table, 1)

    # --- public ------------------------------------------------------------

    def set_table(self, table: Table, keep_state: bool = False) -> None:
        """Show a (re)loaded table; with ``keep_state`` printed/unchecked rows stay marked."""
        if not keep_state:
            self._printed.clear()
            self._unchecked.clear()
            self._seen.clear()
        current = self._table.currentRow()
        self._data = table
        self._results = []
        self._updating = True
        try:
            self._table.clear()
            headers = list(FIXED_COLUMNS) + table.columns
            self._table.setColumnCount(len(headers))
            self._table.setHorizontalHeaderLabels(headers)
            self._table.setRowCount(len(table.rows))
            for row, (record, line) in enumerate(zip(table.rows, table.lines, strict=True)):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._table.setItem(row, 0, check)
                number = QTableWidgetItem(str(line))
                number.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table.setItem(row, 1, number)
                self._table.setItem(row, 2, QTableWidgetItem())
                for col, name in enumerate(table.columns, start=len(FIXED_COLUMNS)):
                    value = record.get(name, "")
                    item = QTableWidgetItem(value.replace("\n", " ⏎ "))
                    item.setToolTip(value)
                    self._table.setItem(row, col, item)
            self._table.resizeColumnsToContents()
            for col in range(self._table.columnCount()):
                self._table.setColumnWidth(col, min(self._table.columnWidth(col) + 8, 240))
        finally:
            self._updating = False
        self._title.setText(table.path.name if table.path else "Tabelle")
        if table.rows:
            self._table.setCurrentCell(min(max(current, 0), len(table.rows) - 1), 1)

    def set_results(self, results: list[MergedRow], messages: list[str]) -> None:
        self._results, self._messages = results, messages
        self._banner.setText("<br>".join(messages))
        self._banner.setVisible(bool(messages))
        if self._data is None:
            return
        self._updating = True
        try:
            for result in results:
                key = _row_key(self._data.rows[result.index])
                if key not in self._seen:  # first time: untouched example rows stay unchecked
                    self._seen.add(key)
                    if result.is_example:
                        self._unchecked.add(key)
                self._update_row(result, key)
        finally:
            self._updating = False
        self._table.resizeColumnToContents(2)
        self._table.setColumnWidth(2, min(self._table.columnWidth(2) + 8, 320))
        self._update_summary()

    def checked(self) -> list[MergedRow]:
        """Rows that will be printed, in table order."""
        return [r for r in self._results if self._is_checked(r)]

    def mark_printed(self, index: int) -> None:
        if self._data is None or not 0 <= index < len(self._data.rows):
            return
        key = _row_key(self._data.rows[index])
        self._printed.add(key)
        self._unchecked.add(key)
        if index < len(self._results):
            self._updating = True
            try:
                self._update_row(self._results[index], key)
            finally:
                self._updating = False
        self._update_summary()
        self.checks_changed.emit()

    def current_index(self) -> int:
        return self._table.currentRow()

    # --- internals ---------------------------------------------------------

    def _is_checked(self, result: MergedRow) -> bool:
        if self._data is None or not result.ok or result.copies <= 0:
            return False
        return _row_key(self._data.rows[result.index]) not in self._unchecked

    def _update_row(self, result: MergedRow, key: tuple[tuple[str, str], ...]) -> None:
        row = result.index
        check = self._table.item(row, 0)
        status = self._table.item(row, 2)
        printable = result.ok and result.copies > 0
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if printable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        check.setFlags(flags)
        check.setCheckState(Qt.CheckState.Checked if self._is_checked(result) else Qt.CheckState.Unchecked)

        if result.errors:
            text, color = "⚠ " + result.errors[0], ERROR_COLOR
            tip = "\n".join(result.errors)
        elif key in self._printed:
            text, color, tip = "✓ gedruckt", MUTED_COLOR, "Wurde schon gedruckt. Zum erneuten Druck anhaken."
        elif result.copies <= 0:
            text, color, tip = "übersprungen (Anzahl 0)", MUTED_COLOR, ""
        elif result.is_example and key in self._unchecked:
            text, color = "Beispielzeile", MUTED_COLOR
            tip = "Unveränderte Beispielzeile aus der erstellten Tabelle. Zum Drucken anhaken."
        else:
            text = "OK" if result.copies == 1 else f"OK · {result.copies}×"
            color, tip = OK_COLOR, ""
        status.setText(text)
        status.setForeground(color)
        status.setToolTip(tip)

    def _update_summary(self) -> None:
        rows = self.checked()
        labels = sum(r.copies for r in rows)
        errors = sum(1 for r in self._results if r.errors)
        parts = [
            f"{len(self._results)} Zeile{'n' if len(self._results) != 1 else ''}",
            f"{len(rows)} zum Drucken ({labels} Etikett{'en' if labels != 1 else ''})",
        ]
        if errors:
            parts.append(f"<span style='color:#c03030'>{errors} mit Fehlern</span>")
        self._summary.setText(" · ".join(parts))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 0 or self._data is None or item.row() >= len(self._results):
            return
        result = self._results[item.row()]
        key = _row_key(self._data.rows[result.index])
        if item.checkState() == Qt.CheckState.Checked:
            self._unchecked.discard(key)
        else:
            self._unchecked.add(key)
        self._updating = True
        try:
            self._update_row(result, key)
        finally:
            self._updating = False
        self._update_summary()
        self.checks_changed.emit()

    def _check_all(self, checked: bool) -> None:
        if self._data is None:
            return
        for result in self._results:
            key = _row_key(self._data.rows[result.index])
            if checked:
                self._unchecked.discard(key)
            else:
                self._unchecked.add(key)
        self.set_results(self._results, self._messages)
        self.checks_changed.emit()
