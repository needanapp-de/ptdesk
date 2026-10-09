# PyInstaller spec: builds dist/ptdesk/ with the GUI (ptdesk) and the CLI (ptdesk-cli).
# Usage: pyinstaller --noconfirm --clean packaging/ptdesk.spec   (run on the target OS)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).parent
icon = root / "packaging" / ("ptdesk.ico" if sys.platform == "win32" else "ptdesk.png")

datas = [(str(root / "src" / "ptdesk" / "fonts"), "ptdesk/fonts")]
binaries = []
hiddenimports = []

if sys.platform == "win32":
    # the WinRT projections are loaded lazily and are not found by the import analysis
    d, b, h = collect_all("winrt")
    datas += d
    binaries += b
    hiddenimports += h
else:
    # dbus-fast is partly compiled (Cython); make sure every submodule is bundled
    hiddenimports += collect_submodules("dbus_fast")

excludes = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore"]


def analysis(script):
    return Analysis(
        [str(root / "packaging" / script)],
        pathex=[str(root / "src")],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
    )


gui = analysis("ptdesk_gui.py")
cli = analysis("ptdesk_cli.py")

gui_exe = EXE(
    PYZ(gui.pure),
    gui.scripts,
    [],
    exclude_binaries=True,
    name="ptdesk",
    console=False,
    icon=str(icon) if icon.exists() else None,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="ptdesk-cli",
    console=True,
    icon=str(icon) if icon.exists() else None,
)

COLLECT(
    gui_exe,
    gui.binaries,
    gui.datas,
    cli_exe,
    cli.binaries,
    cli.datas,
    name="ptdesk",
)
