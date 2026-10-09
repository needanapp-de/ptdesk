"""Printer communication on a dedicated asyncio thread.

The blocking Bluetooth socket and the D-Bus/WinRT discovery must not stall Qt's event loop.
All results are delivered to the GUI through Qt signals.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from typing import Any

from PIL import Image
from PySide6.QtCore import QObject, Signal

from ptdesk.protocol.client import PrintCancelled, PrinterError, PrintOptions, PTouchClient
from ptdesk.protocol.status import ParseError
from ptdesk.transport.base import TransportError
from ptdesk.transport.discovery import scan
from ptdesk.transport.rfcomm import RfcommTransport

log = logging.getLogger(__name__)

_EXPECTED_ERRORS = (PrinterError, TransportError, ParseError)


def _message(e: BaseException) -> str:
    if isinstance(e, _EXPECTED_ERRORS):
        return str(e)
    return f"Unerwarteter Fehler: {type(e).__name__}: {e}"


class PrinterWorker(QObject):
    scan_finished = Signal(list)  # list[FoundPrinter]
    scan_failed = Signal(str)
    connecting = Signal(str)
    connected = Signal(object)  # PrinterInfo
    connect_failed = Signal(str)
    disconnected = Signal()
    status = Signal(object)  # Status
    print_progress = Signal(str, float)  # stage of the current job, fraction
    print_job = Signal(int, int)  # index of the job that starts, number of jobs
    print_job_done = Signal(object)  # tag of the finished job
    print_finished = Signal()
    print_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="printer-worker", daemon=True)
        self._thread.start()
        self._client: PTouchClient | None = None
        self._cancel: asyncio.Event | None = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_crash)
        return future

    @staticmethod
    def _log_crash(future: concurrent.futures.Future[Any]) -> None:
        if not future.cancelled() and future.exception():
            log.error("Worker task crashed", exc_info=future.exception())

    # --- API for the GUI thread --------------------------------------------

    def scan(self, timeout: float = 8.0) -> None:
        self._submit(self._scan(timeout))

    def connect_printer(self, address: str, name: str = "") -> None:
        self._submit(self._connect(address, name))

    def disconnect_printer(self) -> None:
        self._submit(self._disconnect())

    def refresh_status(self) -> None:
        self._submit(self._refresh_status())

    def print_jobs(self, jobs: list[tuple[Image.Image, int, Any]], tape_mm: float, options: PrintOptions) -> None:
        """Print several labels one after another: ``(image, copies, tag)``; stops at the first error.

        All but the last job are chain printed, so the labels of a serial print hang together and
        only the last one is fed out (if ``options.feed_last``).
        """
        self._submit(self._print([(image.copy(), copies, tag) for image, copies, tag in jobs], tape_mm, options))

    def cancel_print(self) -> None:
        cancel = self._cancel
        if cancel is not None:
            self._loop.call_soon_threadsafe(cancel.set)

    def shutdown(self) -> None:
        try:
            self._submit(self._disconnect()).result(timeout=8)
        except (concurrent.futures.TimeoutError, RuntimeError):
            log.warning("Disconnect on shutdown timed out")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    # --- coroutines on the worker loop -------------------------------------

    async def _scan(self, timeout: float) -> None:
        try:
            self.scan_finished.emit(await scan(timeout))
        except Exception as e:
            self.scan_failed.emit(_message(e))

    async def _connect(self, address: str, name: str) -> None:
        await self._disconnect()
        try:
            transport = RfcommTransport(address, name)
        except TransportError as e:
            self.connect_failed.emit(str(e))
            return
        self.connecting.emit(transport.name)
        client = PTouchClient(transport)
        client.on_disconnect = lambda: self._handle_disconnect(client)
        client.on_status = self.status.emit
        try:
            info = await client.connect()
        except Exception as e:
            self.connect_failed.emit(_message(e))
            return

        self._client = client
        self.connected.emit(info)
        if client.status is not None:
            self.status.emit(client.status)
        client.start_polling()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    def _handle_disconnect(self, client: PTouchClient) -> None:
        # ignore late callbacks of a client that was already replaced
        if self._client is client or self._client is None:
            self._client = None
            self.disconnected.emit()

    async def _refresh_status(self) -> None:
        client = self._client
        if client is None or not client.is_connected or client.printing:
            return
        try:
            await client.request_status()
        except _EXPECTED_ERRORS as e:
            log.warning("Status request failed: %s", e)

    async def _print(self, jobs: list[tuple[Image.Image, int, Any]], tape_mm: float, options: PrintOptions) -> None:
        client = self._client
        if client is None or not client.is_connected:
            self.print_failed.emit("Kein Drucker verbunden")
            return

        self._cancel = asyncio.Event()
        try:
            for i, (image, copies, tag) in enumerate(jobs):
                self.print_job.emit(i, len(jobs))
                last = i == len(jobs) - 1
                job_options = options if last else PrintOptions(options.margin_mm, False, options.cut_marks)
                await client.print_pages(
                    [image] * copies,
                    tape_mm,
                    job_options,
                    progress=lambda stage, fraction: self.print_progress.emit(stage, fraction),
                    cancel=self._cancel,
                )
                self.print_job_done.emit(tag)
                if self._cancel.is_set() and not last:
                    raise PrintCancelled()
        except PrintCancelled as e:
            self.print_failed.emit(str(e))
        except Exception as e:
            log.exception("Print failed")
            self.print_failed.emit(_message(e))
        else:
            self.print_finished.emit()
        finally:
            self._cancel = None

        if client.is_connected:
            await self._refresh_status()
            client.start_polling()
