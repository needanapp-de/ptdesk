"""Printer and tape metadata.

The values come from the printer definition in Brother's "P-touch Design&Print 2" app
(``assets/ptd/bspp30ad.json`` for the PT-P300BT) and were checked against the status reply of a
real PT-P300BT (series 0x30, model 0x72).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tape:
    width_mm: float
    status_width: int  # media width byte in the status reply (whole millimetres)
    printable_px: int  # pins the printer uses for this tape
    pin_offset: int  # head pins left blank before the printable area

    @property
    def name(self) -> str:
        return f"{self.width_mm:g} mm"


@dataclass(frozen=True)
class PrinterModel:
    name: str
    series_code: int
    model_code: int
    dpi: int
    head_pins: int
    tapes: tuple[Tape, ...]
    min_length_mm: float
    max_length_mm: float
    min_margin_mm: float
    max_copies: int

    @property
    def px_per_mm(self) -> float:
        return self.dpi / 25.4

    def tape(self, width_mm: float) -> Tape | None:
        return next((t for t in self.tapes if t.width_mm == width_mm), None)

    def tape_by_status(self, status_width: int) -> Tape | None:
        return next((t for t in self.tapes if t.status_width == status_width), None)


PT_P300BT = PrinterModel(
    name="PT-P300BT",
    series_code=0x30,
    model_code=0x72,
    dpi=180,
    head_pins=128,
    tapes=(
        Tape(3.5, 4, 18, 55),
        Tape(6, 6, 32, 48),
        Tape(9, 9, 50, 39),
        Tape(12, 12, 64, 32),
    ),
    min_length_mm=25.0,
    max_length_mm=500.0,
    min_margin_mm=2.4,
    max_copies=200,
)

MODELS = (PT_P300BT,)
DEFAULT_MODEL = PT_P300BT
DEFAULT_TAPE_MM = 12.0


def model_by_code(series_code: int, model_code: int) -> PrinterModel | None:
    return next((m for m in MODELS if (m.series_code, m.model_code) == (series_code, model_code)), None)
