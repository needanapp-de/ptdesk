"""Transport interface: a byte pipe to the printer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class TransportError(Exception):
    """Connection-level problem (user-facing message)."""


class Transport(ABC):
    def __init__(self) -> None:
        self.on_data: Callable[[bytes], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    def _emit_data(self, data: bytes) -> None:
        if self.on_data:
            self.on_data(data)

    def _emit_disconnect(self) -> None:
        if self.on_disconnect:
            self.on_disconnect()
