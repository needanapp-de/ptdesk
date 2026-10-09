"""Render the app icon to packaging/ptdesk.png and packaging/ptdesk.ico (used by PyInstaller)."""

import os
import sys
from io import BytesIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtGui import QGuiApplication

from ptdesk.gui.app import app_icon

HERE = Path(__file__).resolve().parent


def main() -> None:
    _app = QGuiApplication(sys.argv)
    icon = app_icon()
    images = []
    for size in (16, 32, 48, 64, 128, 256):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(QSize(size, size)).save(buffer, "PNG")
        buffer.close()
        images.append(Image.open(BytesIO(bytes(data))).convert("RGBA"))

    images[-1].save(HERE / "ptdesk.png")
    images[-1].save(HERE / "ptdesk.ico", sizes=[(im.width, im.height) for im in images])
    print("Icon written to", HERE)


if __name__ == "__main__":
    main()
