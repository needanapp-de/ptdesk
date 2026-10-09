"""Property editor for the selected label element."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ptdesk.gui.merge_widgets import ColumnButton, ElementContext
from ptdesk.render.fonts import DEFAULT_FAMILY, family_names
from ptdesk.render.label import (
    BARCODE_TYPES,
    BarcodeElement,
    Element,
    ImageElement,
    RectElement,
    TextElement,
)


def mm_spin(minimum: float, maximum: float, step: float = 0.5) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    spin.setDecimals(1)
    spin.setSuffix(" mm")
    spin.setKeyboardTracking(False)
    return spin


def combo(items: list[tuple[str, Any]]) -> QComboBox:
    box = QComboBox()
    for text, data in items:
        box.addItem(text, data)
    return box


def select_data(box: QComboBox, value: Any) -> None:
    index = box.findData(value)
    box.setCurrentIndex(max(index, 0))


class PropertyPanel(QWidget):
    changed = Signal()  # an attribute of the element was edited
    replace_image_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._element: Element | None = None
        self._loading = False
        self._loaders: dict[type[Element], Callable[[Any], None]] = {}
        self._pages: dict[type[Element], int] = {}
        self._context = ElementContext()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._title = QLabel("Kein Element ausgewählt")
        title_font = QFont(self._title.font())
        title_font.setBold(True)
        self._title.setFont(title_font)
        layout.addWidget(self._title)

        self._hint = QLabel("Element links hinzufügen oder in der Vorschau anklicken.")
        self._hint.setMinimumWidth(0)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._geometry = QGroupBox("Position und Größe")
        form = QFormLayout(self._geometry)
        self._x = mm_spin(-100, 200)
        self._y = mm_spin(-100, 500)
        self._w = mm_spin(1, 200)
        self._h = mm_spin(1, 500)
        for label, spin, attr in (("X", self._x, "x"), ("Y", self._y, "y"), ("Breite", self._w, "width"), ("Höhe", self._h, "height")):
            form.addRow(label, spin)
            spin.valueChanged.connect(lambda v, a=attr: self._set(a, round(v, 2)))
        layout.addWidget(self._geometry)

        self._stack = QStackedWidget()
        self._stack.addWidget(QWidget())
        self._build_text_page()
        self._build_barcode_page()
        self._build_image_page()
        self._build_rect_page()
        layout.addWidget(self._stack)

        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #d03030;")
        layout.addWidget(self._error)
        layout.addStretch(1)

        self.set_element(None)

    # --- public ------------------------------------------------------------

    def set_context(self, context: ElementContext) -> None:
        """Gives the editors access to the label's table columns and the previewed table row."""
        self._context = context

    def set_element(self, element: Element | None) -> None:
        self._element = element
        has = element is not None
        self._geometry.setVisible(has)
        self._hint.setVisible(not has)
        self._title.setText(element.TITLE if element else "Kein Element ausgewählt")
        if element is None:
            self._stack.setCurrentIndex(0)
            self._error.clear()
            return
        self._stack.setCurrentIndex(self._pages[type(element)])
        self._loading = True
        try:
            self._loaders[type(element)](element)
        finally:
            self._loading = False
        self.refresh_geometry()

    def refresh_geometry(self) -> None:
        element = self._element
        if element is None:
            return
        self._loading = True
        try:
            for spin, value in ((self._x, element.x), (self._y, element.y), (self._w, element.width), (self._h, element.height)):
                spin.setValue(value)
        finally:
            self._loading = False
        self._refresh_error()

    # --- helpers -----------------------------------------------------------

    def _set(self, attr: str, value: Any) -> None:
        if self._loading or self._element is None or getattr(self._element, attr) == value:
            return
        setattr(self._element, attr, value)
        self._refresh_error()
        self.changed.emit()

    def _refresh_error(self) -> None:
        error = self._context.check(self._element) if self._element else None
        self._error.setText(error or "")

    def _add_page(self, cls: type[Element], page: QWidget, loader: Callable[[Any], None]) -> None:
        self._pages[cls] = self._stack.addWidget(page)
        self._loaders[cls] = loader

    # --- pages -------------------------------------------------------------

    def _build_text_page(self) -> None:
        page = QGroupBox("Eigenschaften")
        form = QFormLayout(page)

        text = QPlainTextEdit()
        text.setFixedHeight(80)
        text.textChanged.connect(lambda: self._set("text", text.toPlainText()))
        form.addRow(text)

        def insert_into_text(placeholder: str) -> None:
            text.insertPlainText(placeholder)
            text.setFocus()

        form.addRow(ColumnButton(lambda: self._context, insert_into_text, "Text"))

        font = QComboBox()
        font.setEditable(True)
        font.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        font.addItems(family_names())
        font.currentTextChanged.connect(lambda v: self._set("font", v or DEFAULT_FAMILY))
        form.addRow("Schrift", font)

        size = QDoubleSpinBox()
        size.setRange(4, 300)
        size.setSuffix(" pt")
        size.setDecimals(1)
        size.setKeyboardTracking(False)
        size.valueChanged.connect(lambda v: self._set("size_pt", v))
        form.addRow("Größe", size)

        bold = QCheckBox("Fett")
        bold.toggled.connect(lambda v: self._set("bold", v))
        form.addRow(bold)

        align = combo([("Links", "left"), ("Zentriert", "center"), ("Rechts", "right")])
        align.currentIndexChanged.connect(lambda _: self._set("align", align.currentData()))
        form.addRow("Ausrichtung", align)

        valign = combo([("Oben", "top"), ("Mitte", "middle"), ("Unten", "bottom")])
        valign.currentIndexChanged.connect(lambda _: self._set("valign", valign.currentData()))
        form.addRow("Vertikal", valign)

        wrap = QCheckBox("Automatisch umbrechen")
        wrap.toggled.connect(lambda v: self._set("wrap", v))
        form.addRow(wrap)

        fit = QCheckBox("Verkleinern, bis Text ins Feld passt")
        fit.toggled.connect(lambda v: self._set("fit", v))
        form.addRow(fit)

        def load(e: TextElement) -> None:
            if text.toPlainText() != e.text:
                text.setPlainText(e.text)
            font.setCurrentText(e.font)
            size.setValue(e.size_pt)
            bold.setChecked(e.bold)
            select_data(align, e.align)
            select_data(valign, e.valign)
            wrap.setChecked(e.wrap)
            fit.setChecked(e.fit)

        self._add_page(TextElement, page, load)

    def _build_barcode_page(self) -> None:
        page = QGroupBox("Eigenschaften")
        form = QFormLayout(page)

        data = QLineEdit()
        data.textChanged.connect(lambda v: self._set("data", v))
        form.addRow("Inhalt", data)

        def insert_into_data(placeholder: str) -> None:
            data.insert(placeholder)
            data.setFocus()

        form.addRow(ColumnButton(lambda: self._context, insert_into_data, "Code"))

        kind = combo([(name, key) for key, name in BARCODE_TYPES.items()])
        kind.currentIndexChanged.connect(lambda _: self._set("symbology", kind.currentData()))
        form.addRow("Typ", kind)

        show_text = QCheckBox("Klartext darunter")
        show_text.toggled.connect(lambda v: self._set("show_text", v))
        form.addRow(show_text)

        text_size = QDoubleSpinBox()
        text_size.setRange(4, 48)
        text_size.setSuffix(" pt")
        text_size.setKeyboardTracking(False)
        text_size.valueChanged.connect(lambda v: self._set("text_size_pt", v))
        form.addRow("Textgröße", text_size)

        def load(e: BarcodeElement) -> None:
            if data.text() != e.data:
                data.setText(e.data)
            select_data(kind, e.symbology)
            show_text.setChecked(e.show_text)
            text_size.setValue(e.text_size_pt)

        self._add_page(BarcodeElement, page, load)

    def _build_image_page(self) -> None:
        page = QGroupBox("Eigenschaften")
        form = QFormLayout(page)

        name = QLabel()
        name.setWordWrap(True)
        form.addRow("Datei", name)

        replace = QPushButton("Bild ersetzen …")
        replace.clicked.connect(self.replace_image_requested)
        form.addRow(replace)

        aspect = QPushButton("Seitenverhältnis des Bildes übernehmen")
        form.addRow(aspect)

        dither = QCheckBox("Rastern (gut für Fotos)")
        form.addRow(dither)

        threshold = QSpinBox()
        threshold.setRange(1, 254)
        threshold.setToolTip("Helligkeit, ab der ein Pixel weiß bleibt")
        threshold.valueChanged.connect(lambda v: self._set("threshold", v))
        form.addRow("Schwellwert", threshold)

        invert = QCheckBox("Invertieren")
        invert.toggled.connect(lambda v: self._set("invert", v))
        form.addRow(invert)

        def on_dither(value: bool) -> None:
            threshold.setEnabled(not value)
            self._set("dither", value)

        dither.toggled.connect(on_dither)

        def keep_aspect() -> None:
            e = self._element
            if isinstance(e, ImageElement) and (size := e.source_size()):
                self._set("height", round(e.width * size[1] / size[0], 1))
                self.refresh_geometry()

        aspect.clicked.connect(keep_aspect)

        def load(e: ImageElement) -> None:
            name.setText(e.name or "–")
            dither.setChecked(e.dither)
            threshold.setValue(e.threshold)
            threshold.setEnabled(not e.dither)
            invert.setChecked(e.invert)

        self._add_page(ImageElement, page, load)

    def _build_rect_page(self) -> None:
        page = QGroupBox("Eigenschaften")
        form = QFormLayout(page)

        line = QDoubleSpinBox()
        line.setRange(0.1, 20)
        line.setSingleStep(0.1)
        line.setSuffix(" mm")
        line.setKeyboardTracking(False)
        line.valueChanged.connect(lambda v: self._set("line_mm", round(v, 2)))
        form.addRow("Linienstärke", line)

        filled = QCheckBox("Gefüllt (für Linien: Höhe klein wählen)")
        filled.toggled.connect(lambda v: (self._set("filled", v), line.setEnabled(not v)))
        form.addRow(filled)

        def load(e: RectElement) -> None:
            line.setValue(e.line_mm)
            filled.setChecked(e.filled)
            line.setEnabled(not e.filled)

        self._add_page(RectElement, page, load)

