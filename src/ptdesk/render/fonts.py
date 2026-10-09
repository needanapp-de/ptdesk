"""Font discovery for Pillow (system fonts on Linux/Windows plus bundled DejaVu Sans).

Variable fonts (one file, several weights) are registered once per named
instance, e.g. "Ubuntu" -> {"Regular": (file, "Regular"), "Bold": (file, "Bold")}.
"""

from __future__ import annotations

import logging
import os
import sys
from functools import cache
from pathlib import Path
from typing import NamedTuple

from PIL import ImageFont

log = logging.getLogger(__name__)

DEFAULT_FAMILY = "DejaVu Sans"
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "fonts"
_REGULAR_STYLES = ("Regular", "Book", "Normal", "Roman", "Medium")


class FontFace(NamedTuple):
    path: Path
    variation: str | None  # named instance of a variable font


_BUNDLED = {
    "Book": FontFace(_BUNDLED_DIR / "DejaVuSans.ttf", None),
    "Bold": FontFace(_BUNDLED_DIR / "DejaVuSans-Bold.ttf", None),
}


def _font_dirs() -> list[Path]:
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    if sys.platform == "darwin":
        return [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), data_home / "fonts", Path.home() / ".fonts"]


def _variation_names(font: ImageFont.FreeTypeFont) -> list[str]:
    try:
        names = font.get_variation_names()
    except (OSError, AttributeError):
        return []
    return [n.decode("utf-8", "replace") if isinstance(n, bytes) else str(n) for n in names]


@cache
def font_families() -> dict[str, dict[str, FontFace]]:
    """Map family name -> {style name: face}."""
    families: dict[str, dict[str, FontFace]] = {DEFAULT_FAMILY: dict(_BUNDLED)}
    seen: set[Path] = set()
    for base in _font_dirs():
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in (".ttf", ".otf"):
                continue
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            try:
                font = ImageFont.truetype(str(real), 10)
                family, style = font.getname()
            except OSError:
                continue
            if not family:
                continue
            styles = families.setdefault(family, {})
            variations = _variation_names(font)
            if variations:
                for name in variations:
                    styles.setdefault(name, FontFace(real, name))
            elif style:
                # static files win over instances of a variable font
                if style not in styles or styles[style].variation is not None:
                    styles[style] = FontFace(real, None)
    log.debug("Found %d font families", len(families))
    return families


def family_names() -> list[str]:
    return sorted(font_families(), key=str.casefold)


def font_face(family: str, bold: bool = False) -> FontFace:
    styles = font_families().get(family) or font_families()[DEFAULT_FAMILY]
    for style in ("Bold",) if bold else _REGULAR_STYLES:
        if style in styles:
            return styles[style]

    def plain(style: str) -> bool:
        return not any(s in style for s in ("Italic", "Oblique", "Condensed"))

    if bold:
        for style, face in styles.items():
            if "Bold" in style and plain(style) and "Semi" not in style and "Extra" not in style:
                return face
    for style, face in styles.items():
        if plain(style):
            return face
    return next(iter(styles.values()))


@cache
def load_font(family: str, bold: bool, size_px: int) -> ImageFont.FreeTypeFont:
    face = font_face(family, bold)
    try:
        font = ImageFont.truetype(str(face.path), max(size_px, 1))
        if face.variation:
            font.set_variation_by_name(face.variation)
        return font
    except OSError:
        log.warning("Cannot load font %s, using bundled default", face.path)
        fallback = _BUNDLED["Bold" if bold else "Book"]
        return ImageFont.truetype(str(fallback.path), max(size_px, 1))
