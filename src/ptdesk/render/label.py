"""Label model (in millimetres) and rendering to a 1-bit image at printer resolution.

A label is a piece of tape: x runs along the tape (``length_mm``), y across it (``tape_mm``).
Only a band in the middle of the tape is printable, and the feed margin at both ends stays blank.
"""

from __future__ import annotations

import base64
import copy
import io
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from typing import Any, ClassVar

import barcode
from barcode.errors import BarcodeError
from PIL import Image, ImageDraw, ImageOps

from ptdesk.render.fonts import DEFAULT_FAMILY, load_font

DEFAULT_DPI = 180
FORMAT_VERSION = 1


@dataclass
class RenderContext:
    dpi: int = DEFAULT_DPI

    @property
    def px_per_mm(self) -> float:
        return self.dpi / 25.4

    def px(self, mm: float) -> int:
        return round(mm * self.px_per_mm)

    def pt_to_px(self, pt: float) -> int:
        return max(1, round(pt * self.dpi / 72))


@dataclass(kw_only=True)
class Element:
    TYPE: ClassVar[str] = ""
    TITLE: ClassVar[str] = ""

    x: float = 2.0
    y: float = 2.0
    width: float = 20.0
    height: float = 10.0

    def box(self, ctx: RenderContext) -> tuple[int, int, int, int]:
        x0, y0 = ctx.px(self.x), ctx.px(self.y)
        return x0, y0, x0 + max(ctx.px(self.width), 1), y0 + max(ctx.px(self.height), 1)

    def summary(self) -> str:
        return self.TITLE

    def validate(self) -> str | None:
        """Error message if the element cannot be rendered, else None."""
        return None

    def draw(self, canvas: Image.Image, ctx: RenderContext) -> None:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE, **asdict(self)}


def _draw_error_box(canvas: Image.Image, box: tuple[int, int, int, int]) -> None:
    d = ImageDraw.Draw(canvas)
    d.rectangle(box, outline=0, width=2)
    d.line(box, fill=0, width=2)
    d.line((box[0], box[3], box[2], box[1]), fill=0, width=2)


@dataclass(kw_only=True)
class TextElement(Element):
    TYPE: ClassVar[str] = "text"
    TITLE: ClassVar[str] = "Text"

    width: float = 40.0
    height: float = 8.0
    text: str = "Text"
    font: str = DEFAULT_FAMILY
    size_pt: float = 12.0
    bold: bool = False
    align: str = "left"  # left | center | right
    valign: str = "top"  # top | middle | bottom
    wrap: bool = True
    fit: bool = False  # shrink font until the text fits the box

    def summary(self) -> str:
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return f"Text: {first[:30]}"

    def _layout(self, size_px: int, box_w: int) -> tuple[Any, list[str], int]:
        font = load_font(self.font, self.bold, size_px)
        ascent, descent = font.getmetrics()
        lines: list[str] = []
        for paragraph in self.text.split("\n"):
            lines += _wrap(paragraph, font, box_w) if self.wrap else [paragraph]
        return font, lines, ascent + descent

    def _fitted(self, box_w: int, box_h: int, ctx: RenderContext) -> tuple[Any, list[str], int]:
        size_px = ctx.pt_to_px(self.size_pt)
        font, lines, pitch = self._layout(size_px, box_w)
        if self.fit and not self._fits(font, lines, pitch, box_w, box_h):
            # largest size below the configured one that still fits
            best = self._layout(4, box_w)
            lo, hi = 5, size_px - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = self._layout(mid, box_w)
                if self._fits(*candidate, box_w, box_h):
                    best, lo = candidate, mid + 1
                else:
                    hi = mid - 1
            font, lines, pitch = best
        return font, lines, pitch

    def content_width_mm(self, ctx: RenderContext) -> float:
        """Width of the longest line when the box is as wide as needed (font size limited by the height)."""
        _x0, y0, _x1, y1 = self.box(ctx)
        font, lines, _pitch = self._fitted(1_000_000, y1 - y0, ctx)
        return max((font.getlength(line) for line in lines), default=0) / ctx.px_per_mm

    def draw(self, canvas: Image.Image, ctx: RenderContext) -> None:
        x0, y0, x1, y1 = self.box(ctx)
        box_w, box_h = x1 - x0, y1 - y0
        font, lines, pitch = self._fitted(box_w, box_h, ctx)

        text_h = pitch * len(lines)
        if self.valign == "middle":
            y = y0 + (box_h - text_h) // 2
        elif self.valign == "bottom":
            y = y1 - text_h
        else:
            y = y0

        d = ImageDraw.Draw(canvas)
        for line in lines:
            w = font.getlength(line)
            if self.align == "center":
                x = x0 + (box_w - w) / 2
            elif self.align == "right":
                x = x1 - w
            else:
                x = x0
            d.text((x, y), line, font=font, fill=0, anchor="la")
            y += pitch

    @staticmethod
    def _fits(font: Any, lines: list[str], pitch: int, box_w: int, box_h: int) -> bool:
        widest = max((font.getlength(line) for line in lines), default=0)
        return widest <= box_w and pitch * len(lines) <= box_h


def _wrap(paragraph: str, font: Any, max_width: int) -> list[str]:
    words = paragraph.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and font.getlength(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


BARCODE_TYPES = {
    "code128": "Code 128",
    "ean13": "EAN-13",
    "ean8": "EAN-8",
    "code39": "Code 39",
}


@dataclass(kw_only=True)
class BarcodeElement(Element):
    TYPE: ClassVar[str] = "barcode"
    TITLE: ClassVar[str] = "Barcode"

    width: float = 40.0
    height: float = 12.0
    data: str = "12345678"
    symbology: str = "code128"
    show_text: bool = True
    text_size_pt: float = 8.0

    def summary(self) -> str:
        return f"{BARCODE_TYPES.get(self.symbology, 'Barcode')}: {self.data[:24]}"

    def _build(self) -> tuple[str, str]:
        code = barcode.get_barcode_class(self.symbology)(self.data, writer=None)
        bits = "".join(code.build())
        text = code.get_fullcode() if self.symbology.startswith("ean") else self.data
        return bits, text

    def validate(self) -> str | None:
        if not self.data:
            return "Barcode ist leer"
        try:
            self._build()
        except (BarcodeError, ValueError, KeyError) as e:
            return f"Ungültiger {BARCODE_TYPES.get(self.symbology, 'Barcode')}: {e}"
        return None

    def draw(self, canvas: Image.Image, ctx: RenderContext) -> None:
        x0, y0, x1, y1 = self.box(ctx)
        if self.validate():
            _draw_error_box(canvas, (x0, y0, x1, y1))
            return
        bits, text = self._build()

        bars_bottom = y1
        font = None
        if self.show_text:
            font = load_font(DEFAULT_FAMILY, False, ctx.pt_to_px(self.text_size_pt))
            ascent, descent = font.getmetrics()
            bars_bottom = y1 - ascent - descent - 1

        module = max(1, (x1 - x0) // len(bits))
        ox = x0 + (x1 - x0 - module * len(bits)) // 2
        d = ImageDraw.Draw(canvas)
        start = None
        for i, bit in enumerate(bits + "0"):
            if bit == "1" and start is None:
                start = i
            elif bit != "1" and start is not None:
                d.rectangle((ox + start * module, y0, ox + i * module - 1, bars_bottom - 1), fill=0)
                start = None

        if font is not None:
            d.text(((x0 + x1) / 2, bars_bottom + 1), text, font=font, fill=0, anchor="ma")


@dataclass(kw_only=True)
class ImageElement(Element):
    TYPE: ClassVar[str] = "image"
    TITLE: ClassVar[str] = "Bild"

    width: float = 20.0
    height: float = 20.0
    name: str = ""
    png_base64: str = ""  # embedded so templates stay self-contained
    dither: bool = True
    threshold: int = 128
    invert: bool = False

    @classmethod
    def from_file(cls, path: str, **kwargs: Any) -> ImageElement:
        with Image.open(path) as img:
            img.load()
            buf = io.BytesIO()
            _flatten(img).save(buf, "PNG")
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        return cls(name=name, png_base64=base64.b64encode(buf.getvalue()).decode("ascii"), **kwargs)

    def summary(self) -> str:
        return f"Bild: {self.name or '(leer)'}"

    def validate(self) -> str | None:
        if not self.png_base64:
            return "Kein Bild geladen"
        try:
            _decode_image(self.png_base64)
        except (OSError, ValueError) as e:
            return f"Bild kann nicht gelesen werden: {e}"
        return None

    def source_size(self) -> tuple[int, int] | None:
        try:
            return _decode_image(self.png_base64).size
        except (OSError, ValueError):
            return None

    def draw(self, canvas: Image.Image, ctx: RenderContext) -> None:
        x0, y0, x1, y1 = self.box(ctx)
        if self.validate():
            _draw_error_box(canvas, (x0, y0, x1, y1))
            return
        img = _decode_image(self.png_base64)
        img = ImageOps.contain(img, (x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
        if self.invert:
            img = ImageOps.invert(img)
        if self.dither:
            mono = img.convert("1")  # Floyd-Steinberg
        else:
            mono = img.point(lambda v: 255 if v >= self.threshold else 0, mode="1")
        ox = x0 + (x1 - x0 - img.width) // 2
        oy = y0 + (y1 - y0 - img.height) // 2
        canvas.paste(mono.convert("L"), (ox, oy))


def _flatten(img: Image.Image) -> Image.Image:
    """Grayscale with transparent areas turned white."""
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        img = Image.alpha_composite(background, rgba)
    return img.convert("L")


@lru_cache(maxsize=32)
def _decode_image(png_base64: str) -> Image.Image:
    with Image.open(io.BytesIO(base64.b64decode(png_base64))) as img:
        img.load()
        return _flatten(img)


@dataclass(kw_only=True)
class RectElement(Element):
    TYPE: ClassVar[str] = "rect"
    TITLE: ClassVar[str] = "Rahmen / Linie"

    width: float = 30.0
    height: float = 10.0
    line_mm: float = 0.5
    filled: bool = False

    def summary(self) -> str:
        return "Fläche" if self.filled else "Rahmen"

    def draw(self, canvas: Image.Image, ctx: RenderContext) -> None:
        x0, y0, x1, y1 = self.box(ctx)
        d = ImageDraw.Draw(canvas)
        if self.filled:
            d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=0)
        else:
            d.rectangle((x0, y0, x1 - 1, y1 - 1), outline=0, width=max(1, ctx.px(self.line_mm)))


ELEMENT_TYPES: dict[str, type[Element]] = {
    cls.TYPE: cls for cls in (TextElement, BarcodeElement, ImageElement, RectElement)
}


def element_from_dict(data: dict[str, Any]) -> Element:
    cls = ELEMENT_TYPES[data["type"]]
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Label:
    length_mm: float = 50.0
    tape_mm: float = 12.0
    margin_mm: float = 2.5  # blank feed at the start and the end of the label
    auto_length: bool = False  # length follows the content (see fit_length)
    elements: list[Element] = field(default_factory=list)
    # example values of the table columns ({column} placeholders), shown in the preview without a table
    sample: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "format": "ptdesk-label",
            "version": FORMAT_VERSION,
            "length_mm": self.length_mm,
            "tape_mm": self.tape_mm,
            "margin_mm": self.margin_mm,
            "auto_length": self.auto_length,
            "elements": [e.to_dict() for e in self.elements],
        }
        if self.sample:
            data["sample"] = dict(self.sample)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Label:
        if data.get("format") != "ptdesk-label":
            raise ValueError("Keine ptdesk-Etikettenvorlage")
        return cls(
            length_mm=float(data.get("length_mm", 50)),
            tape_mm=float(data.get("tape_mm", 12)),
            margin_mm=float(data.get("margin_mm", 2.5)),
            auto_length=bool(data.get("auto_length", False)),
            elements=[element_from_dict(e) for e in data.get("elements", []) if e.get("type") in ELEMENT_TYPES],
            sample={str(k): str(v) for k, v in (data.get("sample") or {}).items()},
        )


AUTO_LENGTH_PADDING_MM = 0.5


def fitted_length(label: Label, dpi: int = DEFAULT_DPI, min_mm: float = 0.0, max_mm: float = 10_000.0) -> Label:
    """``label`` itself, or with ``auto_length`` a copy whose length (and text box widths) fit the content.

    Text boxes that shrink their font to fit get exactly as wide as their longest line at the
    largest size the box height allows; the label then ends one margin after the rightmost element.
    """
    if not label.auto_length:
        return label
    ctx = RenderContext(dpi)
    out = copy.copy(label)
    out.elements = []
    right = label.margin_mm
    for element in label.elements:
        element = copy.copy(element)
        if isinstance(element, TextElement) and element.fit and not element.wrap and element.text.strip():
            element.width = round(element.content_width_mm(ctx) + AUTO_LENGTH_PADDING_MM, 2)
        out.elements.append(element)
        right = max(right, element.x + element.width)
    natural = right + label.margin_mm
    out.length_mm = round(min(max(natural, min_mm), max_mm), 1)
    if out.length_mm > natural:  # shorter than the minimum: center the content
        shift_mm = (out.length_mm - natural) / 2
        for element in out.elements:
            element.x = round(element.x + shift_mm, 2)
    return out


def render(label: Label, dpi: int = DEFAULT_DPI) -> Image.Image:
    """Render the full label (mode "1", 0 = black) at ``dpi``: width along the tape, height across it."""
    ctx = RenderContext(dpi)
    canvas = Image.new("L", (max(ctx.px(label.length_mm), 1), max(ctx.px(label.tape_mm), 1)), 255)
    for element in label.elements:
        element.draw(canvas, ctx)
    return canvas.point(lambda v: 255 if v >= 128 else 0, mode="1")


def printable_band(tape_px: int, printable_px: int) -> tuple[int, int]:
    """Rows ``(start, end)`` across the tape that the printer reaches (centered on the tape)."""
    if tape_px <= printable_px:
        return 0, tape_px
    start = (tape_px - printable_px) // 2
    return start, start + printable_px


def printable_columns(label: Label, dpi: int = DEFAULT_DPI) -> tuple[int, int]:
    """Columns ``(start, end)`` along the tape between the feed margins."""
    ctx = RenderContext(dpi)
    length = max(ctx.px(label.length_mm), 1)
    margin = min(ctx.px(label.margin_mm), length // 2)
    return margin, length - margin


def shift(image: Image.Image, dx_px: int, dy_px: int) -> Image.Image:
    """Move the content by (dx, dy) pixels, keeping the size; uncovered areas become white.

    Used to compensate a constant offset of the print (positive dx = towards the end of the
    label, positive dy = further down).
    """
    if dx_px == 0 and dy_px == 0:
        return image
    out = Image.new(image.mode, image.size, 255 if image.mode in ("1", "L") else "white")
    out.paste(image, (dx_px, dy_px))
    return out


def print_image(
    label: Label, printable_px: int, dpi: int = DEFAULT_DPI, offset_x_mm: float = 0.0, offset_y_mm: float = 0.0
) -> Image.Image:
    """Exactly what is printed: rendered, shifted by the calibration offset, cut to the printable area.

    Width = number of raster lines (along the tape), height = ``printable_px`` (across the tape).
    ``label`` must already have its final length (see :func:`fitted_length`).
    """
    ctx = RenderContext(dpi)
    image = shift(render(label, dpi), ctx.px(offset_x_mm), ctx.px(offset_y_mm))
    top, bottom = printable_band(image.height, printable_px)
    left, right = printable_columns(label, dpi)
    band = image.crop((left, top, right, bottom))
    if band.height < printable_px:  # tape narrower than the printable band: pad evenly
        padded = Image.new("1", (band.width, printable_px), 1)
        padded.paste(band, (0, (printable_px - band.height) // 2))
        band = padded
    return band


def change_tape(label: Label, tape_mm: float, old_printable_mm: float, new_printable_mm: float) -> None:
    """Switch ``label`` to another tape width, scaling the elements from the old printable band to the new one.

    Positions across the tape and heights scale with the band; images also keep their aspect ratio.
    """
    if tape_mm == label.tape_mm and old_printable_mm == new_printable_mm:
        return
    old_top = max(0.0, (label.tape_mm - old_printable_mm) / 2)
    new_top = max(0.0, (tape_mm - new_printable_mm) / 2)
    factor = new_printable_mm / old_printable_mm if old_printable_mm > 0 else 1.0
    for element in label.elements:
        element.y = round(new_top + (element.y - old_top) * factor, 2)
        element.height = round(max(element.height * factor, 0.5), 2)
        if isinstance(element, ImageElement):
            element.width = round(max(element.width * factor, 0.5), 2)
    label.tape_mm = tape_mm


def _printable_box(length_mm: float, tape_mm: float, printable_mm: float, margin_mm: float) -> tuple[float, float, float, float]:
    top = max(0.0, (tape_mm - printable_mm) / 2)
    return margin_mm, top, max(length_mm - 2 * margin_mm, 1), min(printable_mm, tape_mm)


def image_label(path: str, length_mm: float, tape_mm: float, printable_mm: float, margin_mm: float = 2.5) -> Label:
    """A label that shows one image file as large as the printable area allows."""
    x, y, w, h = _printable_box(length_mm, tape_mm, printable_mm, margin_mm)
    element = ImageElement.from_file(path, x=x, y=y, width=w, height=h)
    return Label(length_mm=length_mm, tape_mm=tape_mm, margin_mm=margin_mm, elements=[element])


def text_label(text: str, length_mm: float, tape_mm: float, printable_mm: float, margin_mm: float = 2.5) -> Label:
    """A label with one text (several lines allowed) filling the printable area."""
    x, y, w, h = _printable_box(length_mm, tape_mm, printable_mm, margin_mm)
    element = TextElement(
        x=x, y=y, width=w, height=h, text=text, size_pt=48, align="center", valign="middle", wrap=False, fit=True
    )
    return Label(length_mm=length_mm, tape_mm=tape_mm, margin_mm=margin_mm, elements=[element])


def testpage_label(length_mm: float, tape_mm: float, printable_mm: float, margin_mm: float = 2.5) -> Label:
    """Test pattern: frame along the printable area and a text that shows the orientation."""
    x, y, w, h = _printable_box(length_mm, tape_mm, printable_mm, margin_mm)
    return Label(
        length_mm=length_mm,
        tape_mm=tape_mm,
        margin_mm=margin_mm,
        elements=[
            RectElement(x=x, y=y, width=w, height=h, line_mm=0.3),
            TextElement(
                x=x + 1, y=y + 0.6, width=w - 2, height=h - 1.2, text=f"ptdesk Test {tape_mm:g} mm",
                size_pt=30, align="left", valign="middle", wrap=False, fit=True,
            ),
        ],
    )
