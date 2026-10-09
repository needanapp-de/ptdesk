"""In-memory transport that behaves like a PT-P300BT: parses the raster commands and answers like the real one."""

import asyncio

from ptdesk.protocol.raster import unpackbits
from ptdesk.transport.base import Transport

# reply of a real PT-P300BT with 12 mm laminated tape (white, black text), no error
REAL_STATUS = bytes.fromhex(
    "80 20 42 30 72 30 00 00 00 00 0c 01 00 00 00 00 00 00 00 00 00 00 00 00 01 08 00 00 00 00 00 00"
)


def status(status_type: int = 0, phase: int = 0, width: int = 12, error1: int = 0, error2: int = 0) -> bytes:
    raw = bytearray(REAL_STATUS)
    raw[8], raw[9], raw[10], raw[18], raw[19] = error1, error2, width, status_type, phase
    return bytes(raw)


class ProtocolError(Exception):
    pass


class FakePTouch(Transport):
    def __init__(self, address: str = "12:34:56:78:9A:BC", name: str = "", *, width: int = 12) -> None:
        super().__init__()
        self.address = address
        self.width = width
        self.error2 = 0  # e.g. 0x10 = cover open, reported in the status
        self.fail_print = 0  # error2 bits reported as an error status after the job
        self.connected = False
        self.pages: list[list[bytes]] = []  # finished pages: their raster lines (16 bytes each)
        self.page_settings: list[dict] = []
        self.status_requests = 0
        self._buf = bytearray()
        self._lines: list[bytes] = []
        self._settings: dict = {}
        self._job_pages = 0
        self._next = 0.0

    @property
    def name(self) -> str:
        return "PT-FAKE"

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            self._emit_disconnect()

    def _send(self, data: bytes, later: bool = False) -> None:
        """Reply; ``later`` replies arrive after a short delay, strictly in the order they were sent."""
        loop = asyncio.get_running_loop()
        self._next = max(loop.time(), self._next) + (0.005 if later else 0.0)
        loop.call_at(self._next, self._emit_fragments, data)

    def _emit_fragments(self, data: bytes) -> None:
        for i in range(0, len(data), 7):  # arrive in fragments like over RFCOMM
            self._emit_data(data[i : i + 7])

    async def write(self, data: bytes) -> None:
        if not self.connected:
            raise AssertionError("write while disconnected")
        self._buf += data
        self._parse()

    def _parse(self) -> None:
        buf = self._buf
        while buf:
            b = buf[0]
            need = 1
            if b == 0x00:
                pass
            elif b == 0x1B:
                if len(buf) < 2:
                    return
                if buf[1] == ord("@"):
                    need = 2
                    self._settings = {}
                elif buf[1] == ord("i"):
                    if len(buf) < 3:
                        return
                    cmd = buf[2]
                    sizes = {ord("S"): 3, ord("a"): 4, ord("z"): 13, ord("K"): 4, ord("M"): 4, ord("d"): 5, ord("!"): 4}
                    if cmd not in sizes:
                        raise ProtocolError(f"unknown ESC i {cmd:#x}")
                    need = sizes[cmd]
                    if len(buf) < need:
                        return
                    args = bytes(buf[3:need])
                    if cmd == ord("S"):
                        self.status_requests += 1
                        self._send(status(width=self.width, error2=self.error2))
                    elif cmd == ord("z"):
                        self._settings["print_info"] = args
                        self._settings["lines"] = int.from_bytes(args[4:8], "little")
                        self._settings["first_page"] = args[8] == 0
                    elif cmd == ord("K"):
                        self._settings["advanced"] = args[0]
                    elif cmd == ord("M"):
                        self._settings["mode"] = args[0]
                    elif cmd == ord("d"):
                        self._settings["margin"] = int.from_bytes(args, "little")
                else:
                    raise ProtocolError(f"unknown ESC {buf[1]:#x}")
            elif b == ord("M"):
                need = 2
                if len(buf) < need:
                    return
                if buf[1] != 0x02:
                    raise ProtocolError("only TIFF compression is used")
            elif b == ord("G"):
                if len(buf) < 3:
                    return
                need = 3 + (buf[1] | buf[2] << 8)
                if len(buf) < need:
                    return
                line = unpackbits(bytes(buf[3:need]))
                if len(line) != 16:
                    raise ProtocolError(f"raster line with {len(line)} bytes")
                self._lines.append(line)
            elif b == ord("Z"):
                self._lines.append(bytes(16))
            elif b in (0x0C, 0x1A):
                if self._settings.get("lines") != len(self._lines):
                    raise ProtocolError(f"ESC i z announced {self._settings.get('lines')} lines, got {len(self._lines)}")
                self.pages.append(self._lines)
                self.page_settings.append(dict(self._settings))
                self._lines = []
                self._page_printed(last=b == 0x1A)
            else:
                raise ProtocolError(f"unexpected byte {b:#x}")
            del buf[:need]

    def _page_printed(self, last: bool) -> None:
        """Status reports like the real printer: phase "printing", one "completed" per page, phase "ready"."""
        if self._job_pages == 0:
            self._send(status(status_type=0x06, phase=1, width=self.width), later=True)
        self._job_pages += 1
        if self.fail_print:
            self._send(status(status_type=0x02, phase=1, width=self.width, error2=self.fail_print), later=True)
            return
        self._send(status(status_type=0x01, phase=1, width=self.width), later=True)
        if last:
            self._job_pages = 0
            self._send(status(status_type=0x06, phase=0, width=self.width), later=True)
