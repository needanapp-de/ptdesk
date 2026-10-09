"""Main window: label settings, element list, preview canvas, properties and printing."""

from __future__ import annotations

import copy
import html
import json
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ptdesk import __version__
from ptdesk.config import Config
from ptdesk.gui.canvas import LabelCanvas
from ptdesk.gui.device_dialog import DeviceDialog
from ptdesk.gui.merge_panel import MergePanel
from ptdesk.gui.merge_widgets import ElementContext, table_icon
from ptdesk.gui.properties import PropertyPanel, combo, mm_spin, select_data
from ptdesk.gui.worker import PrinterWorker
from ptdesk.models import DEFAULT_MODEL, PrinterModel, Tape
from ptdesk.protocol.client import PrinterInfo, PrintOptions
from ptdesk.protocol.status import Status
from ptdesk.render import merge
from ptdesk.render.label import (
    BarcodeElement,
    Element,
    ImageElement,
    Label,
    RectElement,
    TextElement,
    change_tape,
    fitted_length,
    print_image,
    render,
    testpage_label,
    text_label,
)
from ptdesk.table import Table, TableError, read_table, write_template

IMAGE_FILTER = "Bilder (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)"
TEMPLATE_FILTER = "ptdesk-Vorlagen (*.json)"
TABLE_FILTER = "Tabellen (*.xlsx *.xlsm *.csv *.txt);;Alle Dateien (*)"
XLSX_FILTER = "Excel-Tabelle (*.xlsx)"
CSV_FILTER = "CSV, Semikolon-getrennt (*.csv)"


class MainWindow(QMainWindow):
    def __init__(self, worker: PrinterWorker, config: Config) -> None:
        super().__init__()
        self.worker = worker
        self.config = config
        self.printer_info: PrinterInfo | None = None
        self.printer_status: Status | None = None
        self.connecting = False
        self.printing = False
        self._user_initiated_connect = False
        self._known_tape: float | None = None  # tape width last reported by the printer

        self.label = self._new_label()
        self.path: Path | None = None
        self.dirty = False

        # serial printing
        self.table: Table | None = None
        self._table_example: dict[str, str] = {}
        self.merged: list[merge.MergedRow] = []
        self.preview_row = -1
        self._job = (0, 1)
        self._jobs_done = 0
        self._job_labels = 0
        self._reload_pending = False
        self._reload_attempts = 0
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(lambda _path: self._reload_timer.start())
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(800)  # Excel & co. write the file in several steps
        self._reload_timer.timeout.connect(lambda: self.reload_table(quiet=True))
        self._merge_timer = QTimer(self)
        self._merge_timer.setSingleShot(True)
        self._merge_timer.setInterval(250)
        self._merge_timer.timeout.connect(self._recompute_merge)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render)

        self._build_ui()
        self._build_menus()
        self._connect_worker()
        self._load_label_into_ui()
        self._update_printer_ui()
        self._update_tape_ui()
        self._update_merge_ui()
        self.resize(1280, 800)
        self.canvas.setFocus()

        if config.auto_connect and config.printer_address:
            QTimer.singleShot(300, lambda: self._connect_to(config.printer_address, user=False))

    # --- model helpers -----------------------------------------------------

    def _new_label(self) -> Label:
        tape = self.model.tape(self.config.tape_mm) or self.model.tapes[-1]
        label = text_label(
            "Mein Etikett", self.config.length_mm, tape.width_mm, self._printable_mm(tape), self.config.margin_mm
        )
        label.auto_length = True
        return label

    @property
    def model(self) -> PrinterModel:
        if self.printer_info and self.printer_info.model:
            return self.printer_info.model
        return DEFAULT_MODEL

    def _printable_mm(self, tape: Tape) -> float:
        return tape.printable_px / self.model.px_per_mm

    def _label_tape(self) -> Tape:
        """The model's tape for the label's width (the widest one if the printer cannot use that width)."""
        return self.model.tape(self.label.tape_mm) or self.model.tapes[-1]

    def _inserted_tape(self) -> Tape | None:
        status = self.printer_status
        if self.printer_info is None or status is None or not status.tape_present:
            return None
        return self.model.tape_by_status(status.media_width_mm)

    def _fitted(self, label: Label) -> Label:
        return fitted_length(label, self.model.dpi, self.model.min_length_mm, self.model.max_length_mm)

    # --- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = QToolBar("Drucker")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.connect_button = QPushButton("Drucker verbinden …")
        self.connect_button.clicked.connect(self._on_connect_button)
        toolbar.addWidget(self.connect_button)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setContentsMargins(12, 0, 12, 0)
        toolbar.addWidget(self.status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())

        self.canvas = LabelCanvas()
        self.canvas.selection_changed.connect(self._on_canvas_selection)
        self.canvas.geometry_changed.connect(self._on_canvas_geometry)
        self.canvas.delete_requested.connect(self.delete_element)
        self.merge_panel = MergePanel()
        self.merge_panel.row_selected.connect(self._on_merge_row)
        self.merge_panel.checks_changed.connect(self._update_merge_ui)
        self.merge_panel.reload_requested.connect(self.reload_table)
        self.merge_panel.close_requested.connect(self.close_table)
        self.merge_panel.setVisible(False)
        center = QSplitter(Qt.Orientation.Vertical)
        center.addWidget(self.canvas)
        center.addWidget(self.merge_panel)
        center.setStretchFactor(0, 3)
        center.setStretchFactor(1, 2)
        splitter.addWidget(center)

        self.properties = PropertyPanel()
        self.properties.set_context(ElementContext(lambda: self.label, self._preview_record))
        self.properties.changed.connect(self._on_property_changed)
        self.properties.replace_image_requested.connect(self.replace_image)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.properties)
        scroll.setMinimumWidth(320)
        splitter.addWidget(scroll)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([280, 660, 340])
        self.setCentralWidget(splitter)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(260)
        layout = QVBoxLayout(panel)

        # tape and length
        group = QGroupBox("Band und Länge")
        form = QFormLayout(group)
        self.tape_box = QLabel()
        self.tape_box.setWordWrap(True)
        self.tape_box.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(self.tape_box)
        self.tape_combo = combo([(t.name, t.width_mm) for t in DEFAULT_MODEL.tapes])
        self.tape_combo.setToolTip("Breite des eingelegten Bandes. Ist ein Drucker verbunden, kommt sie vom Drucker.")
        self.tape_combo.currentIndexChanged.connect(self._on_tape)
        form.addRow("Bandbreite", self.tape_combo)
        self.length_spin = mm_spin(DEFAULT_MODEL.min_length_mm, DEFAULT_MODEL.max_length_mm, 1.0)
        self.length_spin.valueChanged.connect(self._on_length)
        self.auto_length = QCheckBox("Länge an den Text anpassen")
        self.auto_length.setToolTip(
            "Texte werden so groß, wie die Bandbreite erlaubt, und das Etikett so lang wie nötig "
            "(gilt für Texte mit „Verkleinern, bis Text ins Feld passt“ ohne automatischen Umbruch)."
        )
        self.auto_length.toggled.connect(self._on_auto_length)
        form.addRow("Länge", self.length_spin)
        form.addRow(self.auto_length)
        self.margin_spin = mm_spin(DEFAULT_MODEL.min_margin_mm, 50, 0.5)
        self.margin_spin.setToolTip("Unbedruckter Vorschub am Anfang und am Ende des Etiketts")
        self.margin_spin.valueChanged.connect(self._on_margin)
        form.addRow("Rand", self.margin_spin)
        layout.addWidget(group)

        # elements
        group = QGroupBox("Elemente")
        v = QVBoxLayout(group)
        grid = QGridLayout()
        adders = [
            ("Text", self.add_text),
            ("Barcode", lambda: self.add_element(BarcodeElement())),
            ("Bild …", self.add_image),
            ("Rahmen / Linie", lambda: self.add_element(RectElement())),
        ]
        for i, (text, slot) in enumerate(adders):
            button = QPushButton(f"+ {text}")
            button.clicked.connect(slot)
            grid.addWidget(button, i // 2, i % 2)
        v.addLayout(grid)
        self.element_list = QListWidget()
        self.element_list.currentRowChanged.connect(self._on_list_selection)
        v.addWidget(self.element_list, 1)
        row = QHBoxLayout()
        for text, tip, slot in (
            ("Kopie", "Duplizieren", self.duplicate_element),
            ("▲", "Nach hinten", lambda: self.move_element(-1)),
            ("▼", "Nach vorne", lambda: self.move_element(1)),
            ("Löschen", "Element löschen (Entf)", self.delete_element),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        v.addLayout(row)
        layout.addWidget(group, 1)

        # serial printing
        group = QGroupBox("Seriendruck aus Tabelle")
        v = QVBoxLayout(group)
        self.merge_info = QLabel()
        self.merge_info.setWordWrap(True)
        self.merge_info.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.merge_info)
        row = QHBoxLayout()
        button = QPushButton("Tabelle erstellen …")
        button.setToolTip("Excel-Tabelle mit den passenden Spalten für dieses Etikett speichern")
        button.clicked.connect(self.export_table)
        row.addWidget(button)
        button = QPushButton("Tabelle öffnen …")
        button.setIcon(table_icon())
        button.setToolTip("Ausgefüllte Tabelle laden: jede Zeile ergibt ein Etikett")
        button.clicked.connect(lambda: self.open_table())
        row.addWidget(button)
        v.addLayout(row)
        layout.addWidget(group)

        # printing
        group = QGroupBox("Drucken")
        v = QVBoxLayout(group)
        row = QHBoxLayout()
        row.addWidget(QLabel("Kopien"))
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, DEFAULT_MODEL.max_copies)
        self.copies_spin.valueChanged.connect(lambda _: self._schedule_merge())
        row.addWidget(self.copies_spin, 1)
        v.addLayout(row)
        self.feed_check = QCheckBox("Letztes Etikett ausgeben")
        self.feed_check.setToolTip(
            "An: Das letzte Etikett wird bis zum Messer vorgeschoben und kann gleich abgeschnitten werden.\n"
            "Aus (Kettendruck): spart etwa 25 mm Band pro Druck, das letzte Etikett bleibt aber im Drucker. "
            "Zum Ausgeben zweimal kurz die Ein-Taste drücken."
        )
        self.feed_check.setChecked(self.config.feed_last)
        self.feed_check.toggled.connect(lambda v: self._set_config("feed_last", v))
        v.addWidget(self.feed_check)
        self.cut_marks_check = QCheckBox("Schnittmarken zwischen den Kopien")
        self.cut_marks_check.setChecked(self.config.cut_marks)
        self.cut_marks_check.toggled.connect(lambda v: self._set_config("cut_marks", v))
        v.addWidget(self.cut_marks_check)
        self.print_button = QPushButton("Drucken")
        self.print_button.setMinimumHeight(40)
        font = self.print_button.font()
        font.setBold(True)
        self.print_button.setFont(font)
        self.print_button.clicked.connect(self.print_label)
        v.addWidget(self.print_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        v.addWidget(self.progress)
        self.cancel_button = QPushButton("Abbrechen")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.worker.cancel_print)
        v.addWidget(self.cancel_button)
        layout.addWidget(group)

        return panel

    def _set_config(self, attr: str, value: object) -> None:
        setattr(self.config, attr, value)
        self.config.save()

    def _offset_spin(self, value: float, attr: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-5, 5)
        spin.setDecimals(2)
        spin.setSingleStep(0.25)
        spin.setSuffix(" mm")
        spin.setKeyboardTracking(False)
        spin.setValue(value)

        def changed(v: float) -> None:
            setattr(self.config, attr, round(v, 3))
            self.config.save()

        spin.valueChanged.connect(changed)
        return spin

    def _build_menus(self) -> None:
        def action(menu, text, slot, shortcut=None) -> QAction:  # noqa: ANN001
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        m = self.menuBar().addMenu("&Datei")
        action(m, "Neu", self.new_label, QKeySequence.StandardKey.New)
        action(m, "Öffnen …", self.open_label, QKeySequence.StandardKey.Open)
        action(m, "Speichern", self.save_label, QKeySequence.StandardKey.Save)
        action(m, "Speichern unter …", self.save_label_as, QKeySequence.StandardKey.SaveAs)
        m.addSeparator()
        action(m, "Als PNG exportieren …", self.export_png)
        m.addSeparator()
        action(m, "Beenden", self.close, QKeySequence.StandardKey.Quit)

        m = self.menuBar().addMenu("D&rucker")
        action(m, "Verbinden …", self.show_device_dialog)
        self.disconnect_action = action(m, "Trennen", self.worker.disconnect_printer)
        self.refresh_action = action(m, "Status aktualisieren", self.worker.refresh_status)
        m.addSeparator()
        self.print_action = action(m, "Drucken", self.print_label, QKeySequence.StandardKey.Print)
        self.testpage_action = action(m, "Testetikett drucken", self.print_testpage)
        m.addSeparator()
        action(m, "Druckversatz …", self.show_offset_dialog)

        m = self.menuBar().addMenu("&Seriendruck")
        action(m, "Tabelle erstellen …", self.export_table)
        action(m, "Tabelle öffnen …", lambda: self.open_table())
        self.reload_table_action = action(m, "Tabelle neu laden", lambda: self.reload_table(), QKeySequence.StandardKey.Refresh)
        self.close_table_action = action(m, "Tabelle schließen", self.close_table)

        m = self.menuBar().addMenu("&Hilfe")
        action(m, "Über ptdesk", self.show_about)

    def _connect_worker(self) -> None:
        w = self.worker
        w.connecting.connect(self._on_connecting)
        w.connected.connect(self._on_connected)
        w.connect_failed.connect(self._on_connect_failed)
        w.disconnected.connect(self._on_disconnected)
        w.status.connect(self._on_status)
        w.print_progress.connect(self._on_print_progress)
        w.print_job.connect(self._on_print_job)
        w.print_job_done.connect(self._on_print_job_done)
        w.print_finished.connect(self._on_print_finished)
        w.print_failed.connect(self._on_print_failed)

    # --- label <-> UI ------------------------------------------------------

    def _load_label_into_ui(self) -> None:
        if self.model.tape(self.label.tape_mm) is None:  # e.g. a template for a wider tape
            tape = self.model.tapes[-1]
            # the printable band of an unknown tape: about three quarters of its width, like on 6 to 12 mm tape
            change_tape(self.label, tape.width_mm, self.label.tape_mm * 0.75, self._printable_mm(tape))
        self.label.margin_mm = max(self.label.margin_mm, self.model.min_margin_mm)
        self.label.length_mm = min(max(self.label.length_mm, self.model.min_length_mm), self.model.max_length_mm)
        for widget, value in (
            (self.length_spin, self.label.length_mm),
            (self.margin_spin, self.label.margin_mm),
        ):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.tape_combo.blockSignals(True)
        select_data(self.tape_combo, self.label.tape_mm)
        self.tape_combo.blockSignals(False)
        self.auto_length.blockSignals(True)
        self.auto_length.setChecked(self.label.auto_length)
        self.auto_length.blockSignals(False)
        self.length_spin.setEnabled(not self.label.auto_length)
        self.canvas.set_label(self.label)
        self._refresh_list()
        self.properties.set_element(None)
        self._schedule_render()
        self._schedule_merge()
        self._update_title()

    def _refresh_list(self) -> None:
        current = self.canvas.selected()
        self.element_list.blockSignals(True)
        self.element_list.clear()
        context = ElementContext(lambda: self.label, self._preview_record)
        for element in self.label.elements:
            text = element.summary()
            if context.check(element):
                text = "⚠ " + text
            self.element_list.addItem(text)
        self.element_list.setCurrentRow(current)
        self.element_list.blockSignals(False)

    def _schedule_render(self) -> None:
        self._render_timer.start()

    def _render(self) -> None:
        view = self._fitted(self._preview_label())
        if self.label.auto_length:
            if self.length_spin.value() != view.length_mm:
                self.length_spin.blockSignals(True)
                self.length_spin.setValue(view.length_mm)
                self.length_spin.blockSignals(False)
            # show the fitted text widths in the editor; they depend on the content only, not on the old width
            resized = False
            for element, shown in zip(self.label.elements, view.elements, strict=True):
                if isinstance(element, TextElement) and element.width != shown.width:
                    element.width, resized = shown.width, True
            if resized:
                self.properties.refresh_geometry()
        self.canvas.set_printer_geometry(self.model.dpi, self._label_tape().printable_px)
        self.canvas.set_view(view)
        self.canvas.set_preview(render(view, self.model.dpi))

    def _preview_record(self) -> dict[str, str]:
        """Values for the {column} placeholders in the preview: selected table row, else the example."""
        if self.table is not None and 0 <= self.preview_row < len(self.table.rows):
            return self.table.rows[self.preview_row]
        return self.label.sample

    def _preview_label(self) -> Label:
        if not merge.has_placeholders(self.label):
            return self.label
        return merge.fill_label(self.label, self._preview_record(), strict=False)[0]

    def _changed(self, refresh_list: bool = False) -> None:
        if not self.dirty:
            self.dirty = True
            self._update_title()
        if refresh_list:
            self._refresh_list()
        self._schedule_render()
        self._schedule_merge()

    def _update_title(self) -> None:
        name = self.path.name if self.path else "Neues Etikett"
        self.setWindowTitle(f"{name}{' *' if self.dirty else ''} – ptdesk")

    # --- label settings ----------------------------------------------------

    def _on_tape(self, _index: int) -> None:
        width = self.tape_combo.currentData()
        if width is None or width == self.label.tape_mm:
            return
        self._switch_tape(width)
        self._changed()

    def _switch_tape(self, width: float) -> None:
        old = self._label_tape()
        new = self.model.tape(width)
        if new is None:
            return
        change_tape(self.label, width, self._printable_mm(old), self._printable_mm(new))
        self.config.tape_mm = width
        self.config.save()
        self.tape_combo.blockSignals(True)
        select_data(self.tape_combo, width)
        self.tape_combo.blockSignals(False)
        self.properties.refresh_geometry()
        self.canvas.update()

    def _on_length(self, value: float) -> None:
        self.label.length_mm = round(value, 1)
        self.config.length_mm = self.label.length_mm
        self.config.save()
        self.canvas.update()
        self._changed()

    def _on_auto_length(self, value: bool) -> None:
        self.label.auto_length = value
        self.length_spin.setEnabled(not value)
        if not value:  # keep the length the content had
            self.label.length_mm = round(self.length_spin.value(), 1)
        self._changed()

    def _on_margin(self, value: float) -> None:
        self.label.margin_mm = round(value, 2)
        self.config.margin_mm = self.label.margin_mm
        self.config.save()
        self.canvas.update()
        self._changed()

    # --- elements ----------------------------------------------------------

    def _selected_element(self) -> Element | None:
        i = self.canvas.selected()
        return self.label.elements[i] if 0 <= i < len(self.label.elements) else None

    def add_text(self) -> None:
        printable = self._printable_mm(self._label_tape())
        top = max(0.0, (self.label.tape_mm - printable) / 2)
        right = max((e.x + e.width for e in self.label.elements), default=self.label.margin_mm)
        element = TextElement(
            x=round(right + 1, 1), y=round(top, 2), width=30, height=round(printable, 2), text="Text",
            size_pt=48, align="center", valign="middle", wrap=False, fit=True,
        )
        self.label.elements.append(element)
        self._changed(refresh_list=True)
        self.canvas.set_selected(len(self.label.elements) - 1)

    def add_element(self, element: Element) -> None:
        # keep new elements inside the printable band
        printable = self._printable_mm(self._label_tape())
        top = max(0.0, (self.label.tape_mm - printable) / 2)
        element.height = round(min(element.height, printable), 2)
        element.width = round(min(element.width, max(self.label.length_mm - 2 * self.label.margin_mm, 1)), 2)
        # cascade along the tape so new elements don't hide each other
        step = 3 * (len(self.label.elements) % 5)
        element.x = round(min(self.label.margin_mm + step, max(self.label.length_mm - element.width, 0)), 1)
        element.y = round(top, 2)
        self.label.elements.append(element)
        self._changed(refresh_list=True)
        self.canvas.set_selected(len(self.label.elements) - 1)

    def add_image(self) -> None:
        path = self._ask_image()
        if not path:
            return
        try:
            element = ImageElement.from_file(path)
        except OSError as e:
            QMessageBox.warning(self, "Bild", f"Bild kann nicht geladen werden:\n{e}")
            return
        self._fit_image(element)
        self.add_element(element)

    def replace_image(self) -> None:
        element = self._selected_element()
        if not isinstance(element, ImageElement):
            return
        path = self._ask_image()
        if not path:
            return
        try:
            new = ImageElement.from_file(path)
        except OSError as e:
            QMessageBox.warning(self, "Bild", f"Bild kann nicht geladen werden:\n{e}")
            return
        element.name, element.png_base64 = new.name, new.png_base64
        self.properties.set_element(element)
        self._changed(refresh_list=True)

    def _ask_image(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "Bild auswählen", self.config.last_directory, IMAGE_FILTER)
        if path:
            self.config.last_directory = str(Path(path).parent)
            self.config.save()
        return path

    def _fit_image(self, element: ImageElement) -> None:
        size = element.source_size() or (1, 1)
        max_w = self.label.length_mm - 2 * self.label.margin_mm
        max_h = self._printable_mm(self._label_tape())
        scale = min(max_w / size[0], max_h / size[1])
        element.width = round(size[0] * scale, 1)
        element.height = round(size[1] * scale, 1)

    def delete_element(self) -> None:
        i = self.canvas.selected()
        if not 0 <= i < len(self.label.elements):
            return
        del self.label.elements[i]
        self.canvas.set_selected(min(i, len(self.label.elements) - 1))
        self._changed(refresh_list=True)

    def duplicate_element(self) -> None:
        element = self._selected_element()
        if element is None:
            return
        clone = copy.deepcopy(element)
        clone.x, clone.y = round(clone.x + 2, 1), round(clone.y + 2, 1)
        self.label.elements.append(clone)
        self._changed(refresh_list=True)
        self.canvas.set_selected(len(self.label.elements) - 1)

    def move_element(self, direction: int) -> None:
        i = self.canvas.selected()
        j = i + direction
        if not (0 <= i < len(self.label.elements) and 0 <= j < len(self.label.elements)):
            return
        elements = self.label.elements
        elements[i], elements[j] = elements[j], elements[i]
        self.canvas.set_selected(j)
        self._changed(refresh_list=True)

    def _on_canvas_selection(self, index: int) -> None:
        self.element_list.blockSignals(True)
        self.element_list.setCurrentRow(index)
        self.element_list.blockSignals(False)
        self.properties.set_element(self._selected_element())

    def _on_list_selection(self, row: int) -> None:
        self.canvas.set_selected(row)

    def _on_canvas_geometry(self, _index: int) -> None:
        self.properties.refresh_geometry()
        self._changed()

    def _on_property_changed(self) -> None:
        i = self.canvas.selected()
        element = self._selected_element()
        if element is not None and (item := self.element_list.item(i)) is not None:
            context = ElementContext(lambda: self.label, self._preview_record)
            item.setText(("⚠ " if context.check(element) else "") + element.summary())
        self.canvas.update()
        self._changed()

    # --- files -------------------------------------------------------------

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Ungespeicherte Änderungen",
            "Das Etikett wurde geändert. Änderungen speichern?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_label()
        return answer == QMessageBox.StandardButton.Discard

    def new_label(self) -> None:
        if not self._confirm_discard():
            return
        self.label, self.path, self.dirty = self._new_label(), None, False
        self._load_label_into_ui()
        self._apply_inserted_tape()

    def open_label(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Vorlage öffnen", self.config.last_directory, TEMPLATE_FILTER)
        if not path:
            return
        try:
            label = Label.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self, "Öffnen", f"Vorlage kann nicht geöffnet werden:\n{e}")
            return
        self.config.last_directory = str(Path(path).parent)
        self.config.save()
        self.label, self.path, self.dirty = label, Path(path), False
        self._load_label_into_ui()
        self._apply_inserted_tape()

    def save_label(self) -> bool:
        if self.path is None:
            return self.save_label_as()
        merge.prune_sample(self.label)
        try:
            self.path.write_text(json.dumps(self.label.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Speichern", f"Speichern fehlgeschlagen:\n{e}")
            return False
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage(f"Gespeichert: {self.path}", 4000)
        return True

    def save_label_as(self) -> bool:
        start = str(self.path or Path(self.config.last_directory or Path.home()) / "etikett.json")
        path, _ = QFileDialog.getSaveFileName(self, "Vorlage speichern", start, TEMPLATE_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".json"
        self.path = Path(path)
        self.config.last_directory = str(self.path.parent)
        self.config.save()
        return self.save_label()

    def export_png(self) -> None:
        start = str(Path(self.config.last_directory or Path.home()) / "etikett.png")
        path, _ = QFileDialog.getSaveFileName(self, "Als PNG exportieren", start, "PNG-Bild (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            render(self._fitted(self._preview_label()), self.model.dpi).save(path, dpi=(self.model.dpi, self.model.dpi))
        except OSError as e:
            QMessageBox.warning(self, "Export", f"Export fehlgeschlagen:\n{e}")
            return
        self.statusBar().showMessage(f"Exportiert: {path}", 4000)

    # --- printer connection ------------------------------------------------

    def _on_connect_button(self) -> None:
        if self.printer_info is not None:
            self.worker.disconnect_printer()
        else:
            self.show_device_dialog()

    def show_device_dialog(self) -> None:
        dialog = DeviceDialog(self.worker, self.config.auto_connect, self)
        if dialog.exec() != DeviceDialog.DialogCode.Accepted or (printer := dialog.selected()) is None:
            return
        self.config.printer_address = printer.address
        self.config.printer_name = printer.name
        self.config.auto_connect = dialog.auto_connect.isChecked()
        self.config.save()
        self._connect_to(printer.address, user=True, name=printer.name)

    def _connect_to(self, address: str, user: bool, name: str = "") -> None:
        self._user_initiated_connect = user
        self.connecting = True
        self._update_printer_ui()
        self.worker.connect_printer(address, name or self.config.printer_name)

    def _on_connecting(self, name: str) -> None:
        self.connecting = True
        self.status_label.setText(f"<span style='color:#d0a000'>●</span> Verbinde mit {name} …")
        self._update_printer_ui(status=False)

    def _on_connected(self, info: PrinterInfo) -> None:
        self.connecting = False
        self.printer_info = info
        self._known_tape = None
        self.statusBar().showMessage(f"Verbunden mit {info.model_name}", 4000)
        if info.model is None:
            QMessageBox.warning(
                self, "Drucker", f"{info.model_name} wird von ptdesk nicht unterstützt. Drucken ist deaktiviert."
            )
        self._update_printer_ui()
        self._update_tape_ui()
        self._schedule_render()

    def _on_connect_failed(self, message: str) -> None:
        self.connecting = False
        self._update_printer_ui()
        if self._user_initiated_connect:
            QMessageBox.warning(self, "Verbindung fehlgeschlagen", message)
        else:
            self.statusBar().showMessage(f"Automatisches Verbinden fehlgeschlagen: {message}", 10000)

    def _on_disconnected(self) -> None:
        was_printing = self.printing
        self.printer_info = None
        self.printer_status = None
        self._known_tape = None
        self.connecting = False
        self._update_printer_ui()
        self._update_tape_ui()
        if not was_printing:
            self.statusBar().showMessage("Drucker getrennt", 4000)

    def _on_status(self, status: Status) -> None:
        previous = self.printer_status
        self.printer_status = status
        if previous is not None and status.errors and status.errors != previous.errors:
            self.statusBar().showMessage("Drucker meldet: " + ", ".join(status.errors), 8000)
        self._update_printer_ui()
        tape = self._inserted_tape()
        width = tape.width_mm if tape else None
        if width != self._known_tape:
            self._known_tape = width
            self._apply_inserted_tape()
        else:
            self._update_tape_ui()

    # --- tape --------------------------------------------------------------

    def _apply_inserted_tape(self) -> None:
        """Take the tape width from the printer."""
        tape = self._inserted_tape()
        if tape is not None and not self.printing and tape.width_mm != self.label.tape_mm:
            self._switch_tape(tape.width_mm)
            self.statusBar().showMessage(f"Etikett an das eingelegte {tape.name} Band angepasst", 8000)
            if self.path is not None:
                self._changed()
            else:
                self._schedule_render()
        self._update_tape_ui()

    def _update_tape_ui(self) -> None:
        tape = self._inserted_tape()
        status = self.printer_status
        self.tape_combo.setEnabled(tape is None)
        ok = "background: rgba(32,160,32,0.13); border: 1px solid rgba(32,160,32,0.55);"
        warn = "background: rgba(220,160,0,0.13); border: 1px solid rgba(220,160,0,0.6);"
        style = "border-radius: 4px; padding: 6px;"
        if tape is not None and status is not None:
            self.tape_box.setText(
                f"<b>✓ {tape.name} Band erkannt</b><br>{html.escape(status.media_name)}, "
                f"{html.escape(status.tape_color_name)} mit {html.escape(status.text_color_name)}er Schrift"
            )
            self.tape_box.setToolTip("Die Bandbreite kommt vom Drucker.")
            self.tape_box.setStyleSheet(ok + style)
        elif self.printer_info is not None and status is not None:
            if status.tape_present:
                text = f"<b>Band mit {status.media_width_mm} mm wird nicht unterstützt</b>"
            else:
                text = "<b>Kein Band eingelegt</b>"
            self.tape_box.setText(text)
            self.tape_box.setToolTip("")
            self.tape_box.setStyleSheet(warn + style)
        else:
            self.tape_box.setText("Die Bandbreite wird übernommen, sobald ein Drucker verbunden ist.")
            self.tape_box.setToolTip("")
            self.tape_box.setStyleSheet("color: palette(mid);")

    def show_offset_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Druckversatz")
        layout = QVBoxLayout(dialog)
        text = QLabel(
            "Nur nötig, wenn der Druck bei <b>allen</b> Etiketten deutlich in dieselbe Richtung verschoben sitzt."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        x = self._offset_spin(self.config.offset_x_mm, "offset_x_mm")
        y = self._offset_spin(self.config.offset_y_mm, "offset_y_mm")
        form.addRow("Richtung Etikettenende", x)
        form.addRow("Nach unten", y)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        reset = buttons.addButton("Auf 0 setzen", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(lambda: (x.setValue(0), y.setValue(0)))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _update_printer_ui(self, status: bool = True) -> None:
        connected = self.printer_info is not None
        can_print = connected and self.printer_info is not None and self.printer_info.model is not None and not self.printing
        self.print_button.setEnabled(can_print)
        self.print_action.setEnabled(can_print)
        self.testpage_action.setEnabled(can_print)
        self.disconnect_action.setEnabled(connected)
        self.refresh_action.setEnabled(connected and not self.printing)
        self.connect_button.setEnabled(not self.connecting and not self.printing)
        self.connect_button.setText("Trennen" if connected else "Drucker verbinden …")
        if status:
            self.status_label.setText(self._status_html())

    def _status_html(self) -> str:
        if self.connecting:
            return "<span style='color:#d0a000'>●</span> Verbinde …"
        info = self.printer_info
        if info is None:
            return "<span style='color:gray'>●</span> Kein Drucker verbunden"

        parts = [f"<b>{html.escape(info.model_name)}</b>"]
        status = self.printer_status
        if status is not None and status.tape_present:
            parts.append(f"{status.media_width_mm} mm Band, {html.escape(status.tape_color_name)}")
        text = "<span style='color:#20a020'>●</span> " + " · ".join(parts)
        problems = list(status.errors) if status else []
        if status is not None and not status.tape_present and "Kein Band eingelegt" not in problems:
            problems.append("Kein Band eingelegt")
        if problems:
            text += " · <span style='color:#d03030'><b>" + html.escape(", ".join(problems)) + "</b></span>"
        return text

    # --- serial printing ---------------------------------------------------

    def _schedule_merge(self) -> None:
        if self.table is not None:
            self._merge_timer.start()
        self._update_merge_ui()

    def _update_merge_ui(self) -> None:
        if self.table is not None:
            rows = self.merge_panel.checked()
            labels = sum(r.copies for r in rows)
            name = html.escape(self.table.path.name if self.table.path else "Tabelle")
            self.merge_info.setText(f"<b>{name}</b>: {len(rows)} von {len(self.table.rows)} Zeilen ausgewählt")
            self.print_button.setText(f"{labels} Etikett{'en' if labels != 1 else ''} drucken")
        else:
            columns = merge.column_names(self.label)
            if columns:
                self.merge_info.setText("Tabellenspalten: " + html.escape(", ".join(columns)))
            else:
                self.merge_info.setText(
                    "<span style='color:gray'>Werte aus einer Excel-Tabelle drucken: beim Text oder Barcode "
                    "„Tabellenspalte einfügen“ wählen oder {Spalte} eintippen.</span>"
                )
            self.print_button.setText("Drucken")
        self.reload_table_action.setEnabled(self.table is not None)
        self.close_table_action.setEnabled(self.table is not None)

    def _recompute_merge(self) -> None:
        self._merge_timer.stop()
        if self.table is None:
            return
        self.merged = merge.merge_rows(
            self.label, self.table.rows, self.table.lines, self.copies_spin.value(), self._table_example
        )
        messages = [html.escape(w) for w in self.table.warnings]
        if not merge.has_placeholders(self.label):
            messages.insert(
                0,
                "Das Etikett enthält keine Tabellenfelder, jede Zeile ergibt dasselbe Etikett. Beim Text oder "
                "Barcode „Tabellenspalte einfügen“ wählen oder {Spalte} eintippen.",
            )
        elif missing := merge.missing_columns(self.label, self.table.columns):
            messages.insert(
                0,
                "In der Tabelle fehlen die Spalten <b>" + html.escape(", ".join(missing)) + "</b>. "
                "Spaltennamen in der Tabelle oder im Etikett angleichen oder die Tabelle neu erstellen.",
            )
        self.merge_panel.set_results(self.merged, messages)
        self._update_merge_ui()

    def _on_merge_row(self, index: int) -> None:
        self.preview_row = index
        self._schedule_render()
        self._refresh_list()
        self.properties.refresh_geometry()

    def export_table(self) -> None:
        if not merge.column_names(self.label):
            QMessageBox.information(
                self,
                "Tabelle erstellen",
                "Das Etikett hat noch keine Tabellenfelder.\n\n"
                "Beim Text oder Barcode „Tabellenspalte einfügen“ wählen oder {Spaltenname} direkt eintippen, "
                "z. B. „{Name}“ in der ersten und „{Abteilung}“ in der zweiten Zeile.\n\n"
                "Jedes Tabellenfeld wird eine Spalte, jede Zeile der Tabelle ein Etikett.",
            )
            return
        stem = self.path.stem if self.path else "etiketten"
        start = str(Path(self.config.last_directory or Path.home()) / f"{stem}-tabelle.xlsx")
        path, selected = QFileDialog.getSaveFileName(
            self, "Tabelle für den Seriendruck erstellen", start, f"{XLSX_FILTER};;{CSV_FILTER}"
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() not in (".xlsx", ".csv"):
            target = target.with_name(target.name + (".csv" if selected == CSV_FILTER else ".xlsx"))
        merge.prune_sample(self.label)
        try:
            write_template(target, self.label, self.path.name if self.path else "")
        except (OSError, TableError) as e:
            QMessageBox.warning(self, "Tabelle erstellen", f"Die Tabelle konnte nicht gespeichert werden:\n{e}")
            return
        self.config.last_directory = str(target.parent)
        self.config.save()
        answer = QMessageBox.question(
            self,
            "Tabelle erstellt",
            f"„{target.name}“ ist gespeichert: eine Spalte pro Tabellenfeld, Zeile 2 ist ein Beispiel.\n\n"
            "Jetzt mit Excel oder LibreOffice öffnen? ptdesk zeigt die Tabelle unter der Vorschau an und "
            "lädt sie neu, sobald du sie dort speicherst.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._load_table(target, ask_template=False)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        else:
            self.statusBar().showMessage(f"Tabelle gespeichert: {target}", 6000)

    def open_table(self, path: Path | None = None) -> None:
        if path is None:
            name, _ = QFileDialog.getOpenFileName(
                self, "Tabelle für den Seriendruck öffnen", self.config.last_directory, TABLE_FILTER
            )
            if not name:
                return
            path = Path(name)
        self.config.last_directory = str(path.parent)
        self.config.save()
        self._load_table(path, ask_template=True)

    def _load_table(self, path: Path, ask_template: bool) -> bool:
        try:
            table = read_table(path)
        except (OSError, TableError) as e:
            QMessageBox.warning(self, "Tabelle öffnen", f"„{path.name}“ kann nicht gelesen werden:\n{e}")
            return False
        if ask_template and table.template is not None:
            self._offer_table_template(table)
        self._show_table(table, keep_state=False)
        return True

    def _offer_table_template(self, table: Table) -> None:
        """A table created by ptdesk carries its label: offer to load it."""
        assert table.template is not None
        embedded = Label.from_dict(table.template)
        merge.prune_sample(self.label)  # the embedded label was saved pruned as well
        if embedded.to_dict() == self.label.to_dict():
            return
        name = Path(table.template_name).name if table.template_name else ""
        if merge.has_placeholders(self.label):
            box = QMessageBox(
                QMessageBox.Icon.Question,
                "Tabelle öffnen",
                f"Die Tabelle wurde für das Etikett „{name or 'ohne Namen'}“ erstellt, das gerade nicht geöffnet "
                "ist. Welches Etikett soll verwendet werden?",
                parent=self,
            )
            use = box.addButton("Etikett aus der Tabelle", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Aktuelles Etikett", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not use:
                return
        if not self._confirm_discard():
            return
        self.path = None
        if name and table.path is not None and (candidate := table.path.parent / name).is_file():
            try:
                if Label.from_dict(json.loads(candidate.read_text(encoding="utf-8"))).to_dict() == embedded.to_dict():
                    self.path = candidate
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.label, self.dirty = embedded, False
        self._load_label_into_ui()
        self._apply_inserted_tape()
        self.statusBar().showMessage(f"Etikett „{name or 'ohne Namen'}“ aus der Tabelle übernommen", 6000)

    def _show_table(self, table: Table, keep_state: bool) -> None:
        self.table = table
        self._table_example = merge.example_values(Label.from_dict(table.template)) if table.template else {}
        if not keep_state or not 0 <= self.preview_row < len(table.rows):
            self.preview_row = 0 if table.rows else -1
        if self._watcher.files():
            self._watcher.removePaths(self._watcher.files())
        if table.path is not None:
            self._watcher.addPath(str(table.path))
        self.merge_panel.set_table(table, keep_state=keep_state)
        self.merge_panel.setVisible(True)
        self._recompute_merge()
        self._schedule_render()
        self._refresh_list()
        self.properties.refresh_geometry()

    def reload_table(self, quiet: bool = False) -> None:
        if self.table is None or self.table.path is None:
            return
        if self.printing:  # row numbers must not change while rows are being printed
            self._reload_pending = True
            return
        path = self.table.path
        if not path.exists():  # some programs replace the file when saving
            if quiet and self._reload_attempts < 5:
                self._reload_attempts += 1
                self._reload_timer.start()
            elif not quiet:
                QMessageBox.warning(self, "Tabelle neu laden", f"„{path}“ gibt es nicht mehr.")
            return
        self._reload_attempts = 0
        if str(path) not in self._watcher.files():
            self._watcher.addPath(str(path))
        try:
            table = read_table(path)
        except (OSError, TableError) as e:
            if quiet:
                self.statusBar().showMessage(f"Tabelle konnte nicht neu geladen werden: {e}", 8000)
            else:
                QMessageBox.warning(self, "Tabelle neu laden", f"„{path.name}“ kann nicht gelesen werden:\n{e}")
            return
        self._show_table(table, keep_state=True)
        self.statusBar().showMessage(f"Tabelle neu geladen: {len(table.rows)} Zeilen", 4000)

    def close_table(self) -> None:
        self.table = None
        self.merged = []
        self.preview_row = -1
        self._table_example = {}
        if self._watcher.files():
            self._watcher.removePaths(self._watcher.files())
        self.merge_panel.setVisible(False)
        self._update_merge_ui()
        self._schedule_render()
        self._refresh_list()
        self.properties.refresh_geometry()

    # --- printing ----------------------------------------------------------

    def print_label(self) -> None:
        if self.table is not None:
            self._print_table()
            return
        label = self.label
        if merge.has_placeholders(label):
            sample = merge.lookup(label.sample)
            if missing := [c for c in merge.column_names(label) if merge.key(c) not in sample]:
                QMessageBox.information(
                    self,
                    "Drucken",
                    "Das Etikett holt Werte aus einer Tabelle (" + ", ".join(missing) + ").\n\n"
                    "Für den Seriendruck zuerst „Tabelle öffnen …“ wählen oder bei diesen Feldern wieder feste "
                    "Werte eintragen.",
                )
                return
            label = merge.fill_label(label, label.sample, strict=False)[0]  # prints what the preview shows
        self._print(label, self.copies_spin.value())

    def print_testpage(self) -> None:
        tape = self._label_tape()
        label = testpage_label(40, tape.width_mm, self._printable_mm(tape), self.label.margin_mm)
        self._print(label, 1)

    def _can_start_print(self) -> bool:
        if self.printing:
            return False
        if self.printer_info is None:
            self.show_device_dialog()
            return False
        return True

    def _confirm_printer_state(self, labels: int) -> bool:
        """Fail closed: printer errors and a tape that does not match the label stop the print."""
        status = self.printer_status
        if status is None:
            QMessageBox.warning(self, "Drucken", "Der Druckerstatus ist unbekannt. Bitte neu verbinden.")
            return False
        if status.errors:
            QMessageBox.warning(self, "Drucken", "Der Drucker meldet: " + ", ".join(status.errors))
            return False
        tape = self._inserted_tape()
        if tape is None:
            QMessageBox.warning(self, "Drucken", "Es ist kein passendes Band eingelegt.")
            return False
        if tape.width_mm != self.label.tape_mm:
            answer = QMessageBox.question(
                self,
                "Drucken",
                f"Eingelegt ist {tape.name} Band, das Etikett ist für {self.label.tape_mm:g} mm angelegt.\n\n"
                f"Etikett auf {tape.name} umstellen? Danach die Vorschau prüfen und erneut drucken.",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._switch_tape(tape.width_mm)
                self._changed()
            return False
        if labels > self.model.max_copies:
            QMessageBox.warning(self, "Drucken", f"Höchstens {self.model.max_copies} Etiketten pro Zeile.")
            return False
        return True

    def _print_image(self, label: Label) -> Image.Image | None:
        """The print image of a filled label, or None (after a message) if it cannot be printed."""
        label = self._fitted(label)
        label.margin_mm = max(label.margin_mm, self.model.min_margin_mm)
        if not self.model.min_length_mm <= label.length_mm <= self.model.max_length_mm:
            QMessageBox.warning(
                self, "Drucken",
                f"Das Etikett ist {label.length_mm:g} mm lang, möglich sind "
                f"{self.model.min_length_mm:g} bis {self.model.max_length_mm:g} mm.",
            )
            return None
        return print_image(
            label, self._label_tape().printable_px, self.model.dpi, self.config.offset_x_mm, self.config.offset_y_mm
        )

    def _print(self, label: Label, copies: int) -> None:
        if not self._can_start_print():
            return
        errors = [e.validate() for e in label.elements if e.validate()]
        if errors:
            QMessageBox.warning(self, "Drucken", "Bitte zuerst korrigieren:\n\n" + "\n".join(errors))
            return
        if not self._confirm_printer_state(copies):
            return
        image = self._print_image(label)
        if image is not None:
            self._start_jobs([(image, copies, None)], label)

    def _print_table(self) -> None:
        if not self._can_start_print():
            return
        if self._merge_timer.isActive():
            self._recompute_merge()
        rows = self.merge_panel.checked()
        if not rows:
            QMessageBox.information(
                self,
                "Seriendruck",
                "Keine Zeile zum Drucken ausgewählt. Zeilen mit Fehlern lassen sich erst drucken, wenn sie in "
                "der Tabelle korrigiert sind.",
            )
            return
        labels = sum(r.copies for r in rows)
        if not self._confirm_printer_state(max(r.copies for r in rows)):
            return
        answer = QMessageBox.question(
            self,
            "Seriendruck",
            f"{labels} Etikett{'en' if labels != 1 else ''} aus {len(rows)} Tabellenzeile"
            f"{'n' if len(rows) != 1 else ''} drucken?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        jobs = self._render_jobs(rows)
        if jobs is not None:
            self._start_jobs(jobs, self.label)

    def _render_jobs(self, rows: list[merge.MergedRow]) -> list[tuple[Image.Image, int, int]] | None:
        dialog = None
        if len(rows) > 20:
            dialog = QProgressDialog("Etiketten werden vorbereitet …", "Abbrechen", 0, len(rows), self)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(300)
        jobs = []
        for i, row in enumerate(rows):
            if dialog is not None:
                dialog.setValue(i)  # modal: also keeps the window responsive
                if dialog.wasCanceled():
                    return None
            image = self._print_image(row.label)
            if image is None:
                return None
            jobs.append((image, row.copies, row.index))
        if dialog is not None:
            dialog.setValue(len(rows))
        return jobs

    def _start_jobs(self, jobs: list[tuple[Image.Image, int, int | None]], label: Label) -> None:
        self._job = (0, len(jobs))
        self._jobs_done = 0
        self._job_labels = sum(copies for _, copies, _ in jobs)
        self._set_printing(True)
        options = PrintOptions(
            margin_mm=max(label.margin_mm, self.model.min_margin_mm),
            feed_last=self.feed_check.isChecked(),
            cut_marks=self.cut_marks_check.isChecked(),
        )
        self.worker.print_jobs(jobs, self.label.tape_mm, options)

    def _set_printing(self, printing: bool) -> None:
        self.printing = printing
        self.progress.setVisible(printing)
        self.progress.setValue(0)
        self.progress.setFormat("Übertragung …")
        self.cancel_button.setVisible(printing)
        self._update_printer_ui()
        if not printing and self._reload_pending:
            self._reload_pending = False
            QTimer.singleShot(0, lambda: self.reload_table(quiet=True))

    def _on_print_job(self, index: int, total: int) -> None:
        self._job = (index, total)

    def _on_print_job_done(self, tag: object) -> None:
        self._jobs_done += 1
        if isinstance(tag, int):
            self.merge_panel.mark_printed(tag)

    def _on_print_progress(self, stage: str, fraction: float) -> None:
        index, total = self._job
        part = fraction * 0.4 if stage == "transfer" else 0.4 + fraction * 0.6
        self.progress.setValue(round((index + part) / max(total, 1) * 100))
        what = "Übertragung" if stage == "transfer" else "Druck"
        prefix = f"Zeile {index + 1} von {total} · " if total > 1 else ""
        self.progress.setFormat(f"{prefix}{what} … %p %")

    def _on_print_finished(self) -> None:
        self._set_printing(False)
        n = self._job_labels
        hint = "" if self.feed_check.isChecked() else " · Letztes Etikett: zweimal kurz die Ein-Taste drücken"
        self.statusBar().showMessage(f"{n} Etikett{'en' if n != 1 else ''} gedruckt{hint}", 10000)

    def _on_print_failed(self, message: str) -> None:
        self._set_printing(False)
        cancelled = message == "Druck abgebrochen"
        total = self._job[1]
        if total > 1:
            detail = f"{self._jobs_done} von {total} Zeilen gedruckt"
            if cancelled:
                self.statusBar().showMessage(f"{message}, {detail}", 8000)
                return
            message += f"\n\n{detail}. Sie sind in der Tabelle als gedruckt markiert, „Drucken“ macht mit den übrigen weiter."
        if cancelled:
            self.statusBar().showMessage(message, 6000)
        else:
            QMessageBox.warning(self, "Druckfehler", message)

    # --- misc --------------------------------------------------------------

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "Über ptdesk",
            f"<b>ptdesk {__version__}</b><br>Etiketten für den Brother P-touch CUBE (PT-P300BT) per Bluetooth "
            "drucken.<br><br>Inoffizielles Projekt, nicht mit Brother verbunden. Brother und P-touch sind Marken "
            "der Brother Industries, Ltd.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.printing:
            answer = QMessageBox.question(self, "Beenden", "Es wird gerade gedruckt. Trotzdem beenden?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._confirm_discard():
            event.ignore()
            return
        self.config.save()
        event.accept()
