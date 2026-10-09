"""Brother P-touch raster commands and print job encoding.

Sources: Brother "Raster Command Reference PT-E550W/P750W/P710BT" v1.02 (print data structure,
command layouts) and the byte sequence of the official app for the PT-P300BT (recorded by
stecman, gist ee1fd9a8b1b6f0fdd170ee87ba2ddafd): 64 x 00, ESC @, ESC i a 01, ESC i z, ESC i K,
ESC i M, ESC i d, M 02, G/Z raster lines, 1A. ``ESC i a 01`` is also a constant in the app's
native library.

Every raster line covers the full print head (``head_pins`` bits, MSB first). The pixels across
the tape go to the pins ``pin_offset .. pin_offset + printable_px``; the first pixel row of the
label (its top edge) is the first of these pins.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntFlag

from PIL import Image, ImageOps

INVALIDATE = b"\x00" * 64
INITIALIZE = b"\x1b@"
STATUS_REQUEST = b"\x1biS"
RASTER_MODE = b"\x1bia\x01"
COMPRESSION_TIFF = b"M\x02"
ZERO_RASTER = b"Z"
PRINT_PAGE = b"\x0c"  # end of a page that is not the last
PRINT_LAST_PAGE = b"\x1a"  # end of the last page, with feeding


class PrintInfoFlag(IntFlag):
    MEDIA_TYPE = 0x02
    WIDTH = 0x04
    LENGTH = 0x08
    QUALITY = 0x40
    RECOVER = 0x80


class ModeFlag(IntFlag):
    AUTO_CUT = 0x40  # the PT-P300BT has no cutter; it prints a cut mark between labels instead
    MIRROR = 0x80


class AdvancedFlag(IntFlag):
    NO_CHAIN_PRINTING = 0x08  # feed the last label out to the cutter


def status_request() -> bytes:
    return INVALIDATE + INITIALIZE + STATUS_REQUEST


def print_information(media_type: int, width_mm: int, raster_lines: int, first_page: bool) -> bytes:
    """``ESC i z``: what the official app sends for the PT-P300BT (valid fields C4h)."""
    flags = PrintInfoFlag.RECOVER | PrintInfoFlag.QUALITY | PrintInfoFlag.WIDTH
    return b"\x1biz" + struct.pack(
        "<BBBBIBB", flags, media_type & 0xFF, width_mm & 0xFF, 0, raster_lines, 0 if first_page else 1, 0
    )


def various_mode(flags: ModeFlag) -> bytes:
    return b"\x1biM" + bytes((int(flags),))


def advanced_mode(flags: AdvancedFlag) -> bytes:
    return b"\x1biK" + bytes((int(flags),))


def margin(dots: int) -> bytes:
    """``ESC i d``: blank feed before and after the printed area, in dots."""
    return b"\x1bid" + struct.pack("<H", dots)


def packbits(data: bytes) -> bytes:
    """TIFF PackBits: runs of 2..128 equal bytes as (1 - n, byte), the rest as (n - 1, literal bytes)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and run < 128 and data[i + run] == data[i]:
            run += 1
        if run >= 2:
            out += bytes((257 - run, data[i]))
            i += run
            continue
        start = i
        i += 1
        while i < n and i - start < 128 and not (i + 1 < n and data[i] == data[i + 1]):
            i += 1
        out.append(i - start - 1)
        out += data[start:i]
    return bytes(out)


def unpackbits(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        header = data[i]
        i += 1
        if header < 128:
            out += data[i : i + header + 1]
            i += header + 1
        elif header > 128:
            out += bytes((data[i],)) * (257 - header)
            i += 1
    return bytes(out)


def raster_line(line: bytes) -> bytes:
    if not any(line):
        return ZERO_RASTER
    compressed = packbits(line)
    return b"G" + struct.pack("<H", len(compressed)) + compressed


def raster_lines(image: Image.Image, head_pins: int, pin_offset: int) -> list[bytes]:
    """One ``head_pins``-bit line per column of ``image`` (black = 1).

    ``image`` is the printable area: width along the tape, height across it (mode "1", 0 = black).
    """
    if head_pins % 8:
        raise ValueError("head_pins must be a multiple of 8")
    if pin_offset < 0 or pin_offset + image.height > head_pins:
        raise ValueError(f"{image.height} px from pin {pin_offset} do not fit {head_pins} pins")
    # transpose: column x of the label becomes line x, its top pixel the first bit of the line
    ink = ImageOps.invert(image.convert("L")).transpose(Image.Transpose.TRANSPOSE).convert("1")
    head = Image.new("1", (head_pins, ink.height), 0)
    head.paste(ink, (pin_offset, 0))
    data = head.tobytes()
    size = head_pins // 8
    return [data[i : i + size] for i in range(0, len(data), size)]


@dataclass(frozen=True)
class JobSettings:
    media_type: int
    width_mm: int
    head_pins: int
    pin_offset: int
    margin_dots: int
    cut_marks: bool = False  # ESC i M auto cut: cut mark between the labels
    feed_last: bool = True  # no chain printing: feed the last label out to the cutter


def page_commands(settings: JobSettings, lines: int, first_page: bool) -> bytes:
    mode = ModeFlag.AUTO_CUT if settings.cut_marks else ModeFlag(0)
    advanced = AdvancedFlag.NO_CHAIN_PRINTING if settings.feed_last else AdvancedFlag(0)
    return (
        RASTER_MODE
        + print_information(settings.media_type, settings.width_mm, lines, first_page)
        + advanced_mode(advanced)
        + various_mode(mode)
        + margin(settings.margin_dots)
        + COMPRESSION_TIFF
    )


def encode_job(pages: list[Image.Image], settings: JobSettings) -> bytes:
    """A complete print job: one page per image (copies are repeated pages)."""
    if not pages:
        raise ValueError("no pages")
    out = bytearray(INVALIDATE + INITIALIZE)
    for index, image in enumerate(pages):
        lines = raster_lines(image, settings.head_pins, settings.pin_offset)
        if not lines:
            raise ValueError("empty page")
        out += page_commands(settings, len(lines), first_page=index == 0)
        for line in lines:
            out += raster_line(line)
        out += PRINT_LAST_PAGE if index == len(pages) - 1 else PRINT_PAGE
    return bytes(out)
