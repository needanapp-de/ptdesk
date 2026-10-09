"""The main window against a simulated printer: connect, show the status, print."""

import os
import time
from collections.abc import Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fake_printer import FakePTouch
from PySide6.QtWidgets import QApplication, QMessageBox

from ptdesk.config import Config

ADDRESS = "12:34:56:78:9A:BC"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Message boxes would block the test: record them instead."""
    shown: list[str] = []

    def record(answer: QMessageBox.StandardButton) -> Callable[..., QMessageBox.StandardButton]:
        def box(_parent: object, _title: str, text: str, *_args: object) -> QMessageBox.StandardButton:
            shown.append(text)
            return answer

        return box

    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, record(QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", record(QMessageBox.StandardButton.No))
    return shown


@pytest.fixture
def printer(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> FakePTouch:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # never touch the real settings
    fake = FakePTouch()
    monkeypatch.setattr("ptdesk.gui.worker.RfcommTransport", lambda address, name="": fake)
    return fake


@pytest.fixture
def window(app: QApplication, printer: FakePTouch, messages: list[str]):
    from ptdesk.gui.main_window import MainWindow
    from ptdesk.gui.worker import PrinterWorker

    worker = PrinterWorker()
    win = MainWindow(worker, Config(printer_address=ADDRESS, auto_connect=False))
    win.show()
    yield win
    worker.shutdown()
    win.dirty = False
    win.close()


def wait_for(app: QApplication, condition: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def connect(app: QApplication, win) -> None:
    win._connect_to(ADDRESS, user=True)
    wait_for(app, lambda: win.printer_info is not None and win.printer_status is not None)
    app.processEvents()


def test_connect_shows_printer_and_tape(app: QApplication, window, messages: list[str]) -> None:
    connect(app, window)
    assert "PT-P300BT" in window.status_label.text()
    assert "12 mm Band" in window.status_label.text()
    assert "12 mm Band erkannt" in window.tape_box.text()
    assert window.print_button.isEnabled()
    assert window.connect_button.text() == "Trennen"
    assert messages == []


def test_tape_from_printer_adapts_the_label(app: QApplication, window, printer: FakePTouch) -> None:
    printer.width = 9
    connect(app, window)
    wait_for(app, lambda: window.label.tape_mm == 9)
    assert "9 mm Band erkannt" in window.tape_box.text()


def test_print_two_lines_with_copies(app: QApplication, window, printer: FakePTouch, messages: list[str]) -> None:
    connect(app, window)
    window.label.elements[0].text = "Zeile 1\nZeile 2"
    window._changed()
    window.copies_spin.setValue(2)
    done: list[str] = []
    window.worker.print_finished.connect(lambda: done.append("ok"))
    window.worker.print_failed.connect(done.append)

    window.print_label()
    wait_for(app, lambda: done)

    assert done == ["ok"], messages
    assert len(printer.pages) == 2
    first, second = printer.page_settings
    assert first["first_page"] and not second["first_page"]
    assert first["advanced"] == second["advanced"] == 0x08  # feed the last label out
    assert first["mode"] == 0  # no cut marks
    assert printer.pages[0] == printer.pages[1]
    assert any(any(line) for line in printer.pages[0])  # something is printed
    wait_for(app, lambda: not window.printing and window.print_button.isEnabled())


def test_printer_error_blocks_printing(app: QApplication, window, printer: FakePTouch, messages: list[str]) -> None:
    printer.error2 = 0x10
    connect(app, window)
    assert "Deckel offen" in window.status_label.text()

    window.print_label()
    assert messages and "Deckel offen" in messages[-1]
    assert printer.pages == []


def test_error_while_printing_is_reported(app: QApplication, window, printer: FakePTouch) -> None:
    connect(app, window)
    printer.fail_print = 0x10
    failed: list[str] = []
    window.worker.print_failed.connect(failed.append)

    window.print_label()
    wait_for(app, lambda: failed)

    assert "Deckel offen" in failed[0]
    wait_for(app, lambda: not window.printing)
