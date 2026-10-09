"""The 32-byte status reply of Brother P-touch printers (answer to ``ESC i S`` and sent on its own
while printing)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

STATUS_SIZE = 32
PRINT_HEAD_MARK = 0x80


class ParseError(Exception):
    pass


class StatusType(IntEnum):
    REPLY = 0x00
    PRINTING_COMPLETED = 0x01
    ERROR = 0x02
    TURNED_OFF = 0x04
    NOTIFICATION = 0x05
    PHASE_CHANGE = 0x06


class PhaseType(IntEnum):
    EDITING = 0x00  # ready to receive data
    PRINTING = 0x01


# error information 1 (byte 8) and 2 (byte 9), bit -> message; from Brother's raster command
# references for the PT-P710BT and the PT-P900 series (there is none for the PT-P300BT)
ERRORS_1 = {
    0x01: "Kein Band eingelegt",
    0x04: "Schneidmesser klemmt",
    0x08: "Batterien schwach",
    0x40: "Netzteil mit zu hoher Spannung",
}
ERRORS_2 = {
    0x01: "Falsches Band eingelegt",
    0x02: "Erweiterungspuffer voll",
    0x04: "Übertragungsfehler",
    0x08: "Empfangspuffer voll",
    0x10: "Deckel offen",
    0x20: "Druckkopf überhitzt",
    0x80: "Systemfehler",
}

MEDIA_TYPES = {
    0x00: "kein Band",
    0x01: "laminiertes Band",
    0x03: "nicht laminiertes Band",
    0x11: "Schrumpfschlauch 2:1",
    0x17: "Schrumpfschlauch 3:1",
    0xFF: "inkompatibles Band",
}

TAPE_COLORS = {
    0x01: "weiß", 0x02: "sonstige", 0x03: "klar", 0x04: "rot", 0x05: "blau", 0x06: "gelb", 0x07: "grün",
    0x08: "schwarz", 0x09: "klar (weiße Schrift)", 0x20: "mattweiß", 0x21: "matt klar", 0x22: "matt silber",
    0x23: "satin gold", 0x24: "satin silber", 0x30: "blau (D)", 0x31: "rot (D)", 0x40: "fluoreszierend orange",
    0x41: "fluoreszierend gelb", 0x50: "beerenpink", 0x51: "hellgrau", 0x52: "limettengrün", 0x60: "gelb (F)",
    0x61: "pink (F)", 0x62: "blau (F)", 0x70: "weiß (Schrumpfschlauch)", 0x90: "weiß (Flex ID)",
    0x91: "gelb (Flex ID)", 0xF0: "Reinigungsband", 0xF1: "Schablone", 0xFF: "inkompatibel",
}
TEXT_COLORS = {
    0x01: "weiß", 0x02: "sonstige", 0x04: "rot", 0x05: "blau", 0x08: "schwarz", 0x0A: "gold", 0x62: "blau (F)",
    0xF0: "Reinigungsband", 0xF1: "Schablone", 0xFF: "inkompatibel",
}


@dataclass(frozen=True)
class Status:
    series_code: int
    model_code: int
    error1: int
    error2: int
    media_width_mm: int
    media_type: int
    status_type: int
    phase_type: int
    phase_number: int
    notification: int
    tape_color: int
    text_color: int
    raw: bytes

    @property
    def errors(self) -> list[str]:
        out = [text for bit, text in ERRORS_1.items() if self.error1 & bit]
        out += [text for bit, text in ERRORS_2.items() if self.error2 & bit]
        unknown = (self.error1 & ~sum(ERRORS_1)) | ((self.error2 & ~sum(ERRORS_2)) << 8)
        if unknown:
            out.append(f"Unbekannter Fehler (0x{unknown:04x})")
        return out

    @property
    def has_error(self) -> bool:
        return bool(self.error1 or self.error2) or self.status_type == StatusType.ERROR

    @property
    def tape_present(self) -> bool:
        return self.media_width_mm > 0 and self.media_type not in (0x00, 0xFF)

    @property
    def media_name(self) -> str:
        return MEDIA_TYPES.get(self.media_type, f"Band 0x{self.media_type:02x}")

    @property
    def tape_color_name(self) -> str:
        return TAPE_COLORS.get(self.tape_color, f"0x{self.tape_color:02x}")

    @property
    def text_color_name(self) -> str:
        return TEXT_COLORS.get(self.text_color, f"0x{self.text_color:02x}")


def parse_status(data: bytes) -> Status:
    if len(data) != STATUS_SIZE:
        raise ParseError(f"Status hat {len(data)} statt {STATUS_SIZE} Byte")
    if data[0] != PRINT_HEAD_MARK or data[1] != STATUS_SIZE or data[2] != ord("B"):
        raise ParseError(f"Unerwarteter Statuskopf: {data[:3].hex(' ')}")
    return Status(
        series_code=data[3],
        model_code=data[4],
        error1=data[8],
        error2=data[9],
        media_width_mm=data[10],
        media_type=data[11],
        status_type=data[18],
        phase_type=data[19],
        phase_number=(data[20] << 8) | data[21],
        notification=data[22],
        tape_color=data[24],
        text_color=data[25],
        raw=bytes(data),
    )


class StatusReader:
    """Collects the byte stream into 32-byte status replies (they may arrive split or merged)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def clear(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        out = []
        while True:
            start = self._buf.find(bytes((PRINT_HEAD_MARK, STATUS_SIZE)))
            if start < 0:
                # keep a trailing 0x80 that may be the start of the next reply
                del self._buf[: max(0, len(self._buf) - 1)]
                return out
            del self._buf[:start]
            if len(self._buf) < STATUS_SIZE:
                return out
            out.append(bytes(self._buf[:STATUS_SIZE]))
            del self._buf[:STATUS_SIZE]
