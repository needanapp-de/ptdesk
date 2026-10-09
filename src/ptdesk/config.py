"""Persistent settings shared by GUI and CLI (JSON in the user's config directory)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ptdesk" / "config.json"


@dataclass
class Config:
    # calibration: shifts the print to compensate a constant offset (mm)
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    # last used printer
    printer_address: str = ""
    printer_name: str = ""
    auto_connect: bool = True
    # defaults for new labels
    tape_mm: float = 12.0
    length_mm: float = 50.0
    margin_mm: float = 2.5
    # feed the last label out to the cutter; off = chain printing (saves about 25 mm of tape per job,
    # the last label must be fed with a double press on the power button)
    feed_last: bool = True
    cut_marks: bool = False
    last_directory: str = ""

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("Cannot read %s: %s", path, e)
            return cls()
        if not isinstance(data, dict):
            log.warning("Ignoring %s: not a JSON object", path)
            return cls()
        config = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            value, default = data[f.name], getattr(config, f.name)
            if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if type(value) is type(default):
                setattr(config, f.name, value)
        return config

    def save(self) -> None:
        path = config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning("Cannot write %s: %s", path, e)
