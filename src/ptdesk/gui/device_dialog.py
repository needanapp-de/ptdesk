"""Dialog to find and pick a printer."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ptdesk.gui.worker import PrinterWorker
from ptdesk.transport.base import TransportError
from ptdesk.transport.discovery import FoundPrinter
from ptdesk.transport.rfcomm import normalize_address

HINT = (
    "Drucker einschalten und in die Nähe stellen. Ist er mit dem Handy verbunden, dort zuerst trennen. "
    "Die Suche dauert einige Sekunden."
)
HINT_WINDOWS = (
    " Fehlt er unter Windows in der Liste, ihn zuerst in den Bluetooth-Einstellungen koppeln "
    "(„Gerät hinzufügen“ → Bluetooth → PT-P300BT…)."
)


class DeviceDialog(QDialog):
    def __init__(self, worker: PrinterWorker, auto_connect: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drucker verbinden")
        self.setMinimumWidth(460)
        self._worker = worker
        self._printers: list[FoundPrinter] = []

        layout = QVBoxLayout(self)
        hint = QLabel(HINT + (HINT_WINDOWS if sys.platform == "win32" else ""))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _: self.accept())
        self._list.currentRowChanged.connect(lambda _: self._on_row())
        layout.addWidget(self._list, 1)

        self._status = QLabel()
        layout.addWidget(self._status)

        row = QHBoxLayout()
        row.addWidget(QLabel("oder Adresse:"))
        self._address = QLineEdit()
        self._address.setPlaceholderText("z. B. 12:34:56:78:9A:BC")
        self._address.textChanged.connect(lambda _: self._update_buttons())
        row.addWidget(self._address, 1)
        layout.addLayout(row)

        self.auto_connect = QCheckBox("Beim Start automatisch mit diesem Drucker verbinden")
        self.auto_connect.setChecked(auto_connect)
        layout.addWidget(self.auto_connect)

        buttons = QDialogButtonBox()
        self._rescan = QPushButton("Erneut suchen")
        self._rescan.clicked.connect(self.start_scan)
        buttons.addButton(self._rescan, QDialogButtonBox.ButtonRole.ActionRole)
        self._ok = buttons.addButton("Verbinden", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Abbrechen", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        worker.scan_finished.connect(self._on_results)
        worker.scan_failed.connect(self._on_failed)
        self.finished.connect(self._disconnect_signals)

        self._update_buttons()
        self.start_scan()

    def selected(self) -> FoundPrinter | None:
        """The chosen printer: from the list, or the typed address."""
        row = self._list.currentRow()
        if 0 <= row < len(self._printers):
            return self._printers[row]
        try:
            return FoundPrinter(normalize_address(self._address.text()), "")
        except TransportError:
            return None

    def start_scan(self) -> None:
        self._rescan.setEnabled(False)
        self._status.setText("Suche läuft …")
        self._worker.scan(8.0)

    def _on_results(self, printers: list[FoundPrinter]) -> None:
        self._printers = printers
        self._list.clear()
        for p in printers:
            notes = []
            if p.paired:
                notes.append("gekoppelt")
            if not p.is_ptouch:
                notes.append("kein P-touch, evtl. nicht unterstützt")
            note = f"   ({', '.join(notes)})" if notes else ""
            item = QListWidgetItem(f"{p.name or 'Unbenannt'}   ·   {p.address}{note}")
            item.setData(Qt.ItemDataRole.UserRole, p.address)
            self._list.addItem(item)
        if printers:
            self._list.setCurrentRow(0)
            self._status.setText(f"{len(printers)} Gerät(e) gefunden.")
        else:
            self._status.setText("Kein Drucker gefunden. Ist er eingeschaltet und nicht mit dem Handy verbunden?")
        self._rescan.setEnabled(True)
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        self._status.setText(message)
        self._rescan.setEnabled(True)

    def _on_row(self) -> None:
        printer = self._printers[self._list.currentRow()] if 0 <= self._list.currentRow() < len(self._printers) else None
        if printer is not None:
            self._address.blockSignals(True)
            self._address.clear()
            self._address.blockSignals(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        if self._address.text().strip():
            self._list.blockSignals(True)
            self._list.setCurrentRow(-1)
            self._list.blockSignals(False)
        self._ok.setEnabled(self.selected() is not None)

    def _disconnect_signals(self) -> None:
        self._worker.scan_finished.disconnect(self._on_results)
        self._worker.scan_failed.disconnect(self._on_failed)
