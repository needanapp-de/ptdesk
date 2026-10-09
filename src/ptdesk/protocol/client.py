"""Async client for Brother P-touch printers: status requests and print jobs.

The printer answers ``ESC i S`` with a 32-byte status and sends statuses on its own while printing
(phase "printing", "printing completed", phase "ready"; a status of type "error" on problems).
It must not be asked for its status during a print job, and the connection must stay open until
the job is reported complete, otherwise the print can be aborted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from ptdesk.models import PrinterModel, Tape, model_by_code
from ptdesk.protocol import raster
from ptdesk.protocol.status import (
    ParseError,
    PhaseType,
    Status,
    StatusReader,
    StatusType,
    parse_status,
)
from ptdesk.transport.base import Transport, TransportError

log = logging.getLogger(__name__)

STATUS_TIMEOUT = 3.0
STATUS_TRIES = 3
CHUNK_SIZE = 512  # transfer granularity for progress reports and cancelling
MIN_PRINT_SPEED_MM_S = 8.0  # conservative; the PT-P300BT prints about 20 mm/s
JOB_TIMEOUT_EXTRA = 20.0
SETTLE_TIME = 3.0  # quiet time after "ready" before a job counts as done when completions are missing


class PrinterError(Exception):
    """Printer problem with a user-facing (German) message."""


class PrintCancelled(PrinterError):
    def __init__(self) -> None:
        super().__init__("Druck abgebrochen")


@dataclass
class PrinterInfo:
    model: PrinterModel | None
    series_code: int
    model_code: int

    @property
    def model_name(self) -> str:
        return self.model.name if self.model else f"Unbekannt (Serie 0x{self.series_code:02x}, Modell 0x{self.model_code:02x})"


# stage is "transfer" (sending data) or "print" (printer working), fraction 0..1
ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class PrintOptions:
    margin_mm: float
    feed_last: bool = True  # feed the last label out to the cutter (else chain printing)
    cut_marks: bool = False  # print a cut mark between the copies


class PTouchClient:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.info: PrinterInfo | None = None
        self.status: Status | None = None
        self.printing = False

        self.on_disconnect: Callable[[], None] | None = None
        self.on_status: Callable[[Status], None] | None = None

        self._reader = StatusReader()
        self._lock = asyncio.Lock()
        self._reply: asyncio.Future[Status] | None = None
        self._events: asyncio.Queue[Status] = asyncio.Queue()
        self._poll_task: asyncio.Task[None] | None = None

        transport.on_data = self._handle_data
        transport.on_disconnect = self._handle_disconnect

    @property
    def is_connected(self) -> bool:
        return self.transport.is_connected and self.info is not None

    @property
    def model(self) -> PrinterModel | None:
        return self.info.model if self.info else None

    def tape(self) -> Tape | None:
        """The inserted tape, if the printer reports one this model knows."""
        if self.model is None or self.status is None or not self.status.tape_present:
            return None
        return self.model.tape_by_status(self.status.media_width_mm)

    # --- connection --------------------------------------------------------

    async def connect(self) -> PrinterInfo:
        self._reader.clear()
        await self.transport.connect()
        try:
            status = await self.request_status()
        except BaseException:
            await self.transport.disconnect()
            raise
        self.info = PrinterInfo(model_by_code(status.series_code, status.model_code), status.series_code, status.model_code)
        log.info("Printer: %s, status %s", self.info.model_name, status.raw.hex(" "))
        return self.info

    async def disconnect(self) -> None:
        self.stop_polling()
        self.info = None
        await self.transport.disconnect()

    # --- status ------------------------------------------------------------

    async def request_status(self, timeout: float = STATUS_TIMEOUT, tries: int = STATUS_TRIES) -> Status:
        if self.printing:
            raise PrinterError("Während des Drucks ist keine Statusabfrage möglich")
        async with self._lock:
            for attempt in range(tries):
                self._reply = asyncio.get_running_loop().create_future()
                try:
                    await self._write(raster.status_request())
                    status = await asyncio.wait_for(self._reply, timeout)
                except TimeoutError:
                    log.warning("No status reply (%d/%d)", attempt + 1, tries)
                    continue
                finally:
                    self._reply = None
                return status
        raise PrinterError("Der Drucker antwortet nicht auf die Statusabfrage")

    def start_polling(self, interval: float = 10.0, max_fails: int = 3) -> None:
        self.stop_polling()
        self._poll_task = asyncio.create_task(self._poll_loop(interval, max_fails))

    def stop_polling(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _poll_loop(self, interval: float, max_fails: int) -> None:
        fails = 0
        while True:
            await asyncio.sleep(interval)
            if self.printing or self._lock.locked():
                continue
            try:
                await self.request_status(tries=1)
                fails = 0
            except PrinterError as e:
                fails += 1
                log.warning("Status poll failed (%d/%d): %s", fails, max_fails, e)
                if fails >= max_fails:
                    log.error("Printer stopped answering, disconnecting")
                    self._poll_task = None
                    await self.transport.disconnect()
                    self._handle_disconnect()
                    return

    async def _write(self, data: bytes) -> None:
        if not self.transport.is_connected:
            raise PrinterError("Drucker ist nicht verbunden")
        try:
            await self.transport.write(data)
        except TransportError as e:
            raise PrinterError(str(e)) from e

    def _handle_data(self, chunk: bytes) -> None:
        for raw in self._reader.feed(chunk):
            try:
                status = parse_status(raw)
            except ParseError as e:
                log.warning("Ignoring invalid status: %s", e)
                continue
            log.debug("<< status type %d phase %d err %02x%02x", status.status_type, status.phase_type, status.error1, status.error2)
            self.status = status
            if status.status_type == StatusType.REPLY and self._reply is not None and not self._reply.done():
                self._reply.set_result(status)
            elif self.printing:
                self._events.put_nowait(status)
            if self.on_status:
                self.on_status(status)

    def _handle_disconnect(self) -> None:
        self.stop_polling()
        was_connected = self.info is not None
        self.info = None
        if self._reply is not None and not self._reply.done():
            self._reply.set_exception(PrinterError("Verbindung zum Drucker verloren"))
        if was_connected and self.on_disconnect:
            self.on_disconnect()

    # --- printing ----------------------------------------------------------

    def check_ready(self, tape_mm: float) -> Tape:
        """The inserted tape if a label for ``tape_mm`` can be printed now, else a PrinterError."""
        if not self.is_connected or self.model is None:
            raise PrinterError("Kein unterstützter Drucker verbunden")
        status = self.status
        if status is None:
            raise PrinterError("Druckerstatus unbekannt")
        if status.errors:
            raise PrinterError("Drucker meldet: " + ", ".join(status.errors))
        tape = self.tape()
        if tape is None:
            raise PrinterError("Kein passendes Band eingelegt")
        if tape.width_mm != tape_mm:
            raise PrinterError(f"Eingelegt ist {tape.name} Band, das Etikett ist für {tape_mm:g} mm angelegt")
        return tape

    async def print_pages(
        self,
        pages: list[Image.Image],
        tape_mm: float,
        options: PrintOptions,
        *,
        progress: ProgressCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        """Print ``pages`` (from ``label.print_image``) as one job, one label each."""
        if not pages:
            raise PrinterError("Nichts zu drucken")
        model = self.model
        tape = self.check_ready(tape_mm)
        assert model is not None and self.status is not None
        if len(pages) > model.max_copies:
            raise PrinterError(f"Höchstens {model.max_copies} Etiketten pro Auftrag")
        for page in pages:
            if page.height != tape.printable_px:
                raise PrinterError("Das Druckbild passt nicht zum eingelegten Band")
        margin_mm = max(options.margin_mm, model.min_margin_mm)
        length_mm = sum(p.width for p in pages) / model.px_per_mm + 2 * margin_mm * len(pages)
        if any(p.width / model.px_per_mm + 2 * margin_mm > model.max_length_mm for p in pages):
            raise PrinterError(f"Ein Etikett darf höchstens {model.max_length_mm:g} mm lang sein")

        settings = raster.JobSettings(
            media_type=self.status.media_type,
            width_mm=self.status.media_width_mm,
            head_pins=model.head_pins,
            pin_offset=tape.pin_offset,
            margin_dots=round(margin_mm * model.px_per_mm),
            cut_marks=options.cut_marks,
            feed_last=options.feed_last,
        )
        job = raster.encode_job(pages, settings)

        def report(stage: str, fraction: float) -> None:
            if progress:
                progress(stage, max(0.0, min(1.0, fraction)))

        async with self._lock:
            self.stop_polling()
            self.printing = True
            self._events = asyncio.Queue()
            try:
                await self._send_job(job, report, cancel)
                timeout = JOB_TIMEOUT_EXTRA + length_mm / MIN_PRINT_SPEED_MM_S
                await self._wait_until_printed(len(pages), timeout, report)
            finally:
                self.printing = False

    async def _send_job(self, job: bytes, report: ProgressCallback, cancel: asyncio.Event | None) -> None:
        for offset in range(0, len(job), CHUNK_SIZE):
            if cancel is not None and cancel.is_set():
                # invalidate + initialize puts the printer back into the receiving state (raster reference);
                # whether it still prints the part of a page it already has is untested
                await self._write(raster.INVALIDATE * 2 + raster.INITIALIZE)
                raise PrintCancelled()
            await self._write(job[offset : offset + CHUNK_SIZE])
            report("transfer", (offset + CHUNK_SIZE) / len(job))
            self._raise_reported_error()

    def _raise_reported_error(self) -> None:
        while not self._events.empty():
            status = self._events.get_nowait()
            if status.status_type == StatusType.ERROR or status.errors:
                raise PrinterError("Drucker meldet: " + (", ".join(status.errors) or "Fehler"))

    async def _wait_until_printed(self, pages: int, timeout: float, report: ProgressCallback) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        completed = 0
        ready_after_completion = False
        report("print", 0.0)
        while completed < pages:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PrinterError("Der Drucker meldet nicht, dass der Druck fertig ist")
            wait = min(remaining, SETTLE_TIME) if ready_after_completion else remaining
            try:
                status = await asyncio.wait_for(self._events.get(), wait)
            except TimeoutError:
                if ready_after_completion:
                    log.info("Printer ready after %d of %d completion reports, assuming done", completed, pages)
                    break
                continue
            if status.status_type == StatusType.ERROR or status.errors:
                raise PrinterError("Drucker meldet: " + (", ".join(status.errors) or "Fehler"))
            if status.status_type == StatusType.PRINTING_COMPLETED:
                completed += 1
                ready_after_completion = False
                report("print", completed / pages)
            elif status.status_type == StatusType.PHASE_CHANGE:
                ready_after_completion = completed > 0 and status.phase_type == PhaseType.EDITING
            elif status.status_type == StatusType.TURNED_OFF:
                raise PrinterError("Der Drucker hat sich ausgeschaltet")
        # the phase change back to "ready" follows the last completion report
        try:
            while True:
                status = await asyncio.wait_for(self._events.get(), 1.5)
                if status.status_type == StatusType.ERROR or status.errors:
                    raise PrinterError("Drucker meldet: " + (", ".join(status.errors) or "Fehler"))
                if status.status_type == StatusType.PHASE_CHANGE and status.phase_type == PhaseType.EDITING:
                    break
        except TimeoutError:
            pass
        report("print", 1.0)
