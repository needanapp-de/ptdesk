"""Bluetooth Classic transport: an RFCOMM socket (Serial Port Profile) to the printer.

Python's socket module speaks RFCOMM natively on Linux (BlueZ) and, since Python 3.9, on Windows.
The blocking socket runs in threads: one reader thread pushes incoming bytes into the event loop,
writes run in the default executor. Asyncio's own socket support is avoided on purpose because the
Windows proactor loop does not handle Bluetooth sockets.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading

from ptdesk.transport.base import Transport, TransportError

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = 1  # read from the PT-P300BT's SDP record ("Serial Port", RFCOMM channel 1)
_ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_IO_TIMEOUT = 30.0
_READ_SIZE = 256


def normalize_address(address: str) -> str:
    """``AA:BB:CC:DD:EE:FF`` in upper case; raises TransportError for anything else."""
    value = address.strip().upper().replace("-", ":")
    if not _ADDRESS.match(value):
        raise TransportError(f"Ungültige Bluetooth-Adresse: {address!r}")
    return value


def bluetooth_available() -> bool:
    return hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")


class RfcommTransport(Transport):
    def __init__(
        self, address: str, name: str = "", channel: int = DEFAULT_CHANNEL, connect_timeout: float = 30.0
    ) -> None:
        super().__init__()
        self.address = normalize_address(address)
        self._name = name
        if not 1 <= channel <= 30:
            raise TransportError(f"Ungültiger RFCOMM-Kanal: {channel}")
        self._channel = channel
        self._connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._closing = False
        self._write_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name or self.address

    @property
    def is_connected(self) -> bool:
        return self._sock is not None and not self._closing

    async def connect(self) -> None:
        if not bluetooth_available():
            raise TransportError("Diese Python-Installation unterstützt kein Bluetooth (RFCOMM).")
        if self._sock is not None:
            return
        self._closing = False
        try:
            sock = await asyncio.to_thread(self._open)
        except TimeoutError as e:
            raise TransportError(
                f"{self.name} antwortet nicht. Ist der Drucker eingeschaltet, in der Nähe und nicht mit "
                "einem anderen Gerät verbunden?"
            ) from e
        except OSError as e:
            raise TransportError(f"Verbindung zu {self.name} fehlgeschlagen: {e.strerror or e}") from e

        self._sock = sock
        loop = asyncio.get_running_loop()
        self._reader = threading.Thread(target=self._read_loop, args=(sock, loop), name="rfcomm-reader", daemon=True)
        self._reader.start()
        log.info("Connected to %s (%s, channel %d)", self.name, self.address, self._channel)

    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.settimeout(self._connect_timeout)
            sock.connect((self.address, self._channel))
            sock.settimeout(_IO_TIMEOUT)
        except BaseException:
            sock.close()
            raise
        return sock

    def _read_loop(self, sock: socket.socket, loop: asyncio.AbstractEventLoop) -> None:
        while not self._closing:
            try:
                data = sock.recv(_READ_SIZE)
            except TimeoutError:
                continue  # idle printer; keep listening
            except OSError as e:
                if not self._closing:
                    log.warning("Connection to %s lost: %s", self.name, e)
                break
            if not data:
                if not self._closing:
                    log.info("%s closed the connection", self.name)
                break
            loop.call_soon_threadsafe(self._emit_data, data)
        if not self._closing:
            loop.call_soon_threadsafe(self._connection_lost, sock)

    def _connection_lost(self, sock: socket.socket) -> None:
        if self._sock is not sock:
            return
        self._sock = None
        self._close_socket(sock)
        self._emit_disconnect()

    async def disconnect(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        self._closing = True
        self._close_socket(sock)
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            await asyncio.to_thread(reader.join, 5)

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError as e:
            log.warning("Closing the socket failed: %s", e)

    async def write(self, data: bytes) -> None:
        sock = self._sock
        if sock is None or self._closing:
            raise TransportError("Drucker ist nicht verbunden")
        async with self._write_lock:
            try:
                await asyncio.to_thread(sock.sendall, data)
            except TimeoutError as e:
                raise TransportError("Der Drucker nimmt keine Daten mehr an (Zeitüberschreitung)") from e
            except OSError as e:
                raise TransportError(f"Senden an den Drucker fehlgeschlagen: {e.strerror or e}") from e
