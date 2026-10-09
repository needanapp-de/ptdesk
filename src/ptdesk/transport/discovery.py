"""Find Brother P-touch printers nearby (Bluetooth Classic).

Linux asks BlueZ over D-Bus for an inquiry scan; Windows lists the devices Windows knows (paired
ones and those found by its own scan) through WinRT.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

from ptdesk.transport.base import TransportError
from ptdesk.transport.rfcomm import normalize_address

log = logging.getLogger(__name__)

SERIAL_PORT_UUID = "00001101-0000-1000-8000-00805f9b34fb"


@dataclass
class FoundPrinter:
    address: str
    name: str
    paired: bool | None = None
    rssi: int | None = None

    @property
    def is_ptouch(self) -> bool:
        return is_ptouch_name(self.name)


def is_ptouch_name(name: str) -> bool:
    """P-touch printers advertise as ``PT-<model><serial digits>``, e.g. ``PT-P300BT1234``."""
    return name.upper().startswith("PT-")


async def scan(timeout: float = 8.0) -> list[FoundPrinter]:
    """P-touch printers first, then by signal strength."""
    if sys.platform.startswith("linux"):
        found = await _scan_bluez(timeout)
    elif sys.platform == "win32":
        found = await _scan_windows(timeout)
    else:
        raise TransportError("Die Druckersuche gibt es nur unter Linux und Windows.")
    found.sort(key=lambda p: (not p.is_ptouch, -(p.rssi if p.rssi is not None else -999), p.name))
    return found


# --- Linux: BlueZ over D-Bus ---------------------------------------------------

async def _scan_bluez(timeout: float) -> list[FoundPrinter]:
    from dbus_fast import BusType, Message, MessageType, Variant, unpack_variants
    from dbus_fast.aio import MessageBus

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except (OSError, ValueError) as e:
        raise TransportError(f"Bluetooth-Dienst (BlueZ) nicht erreichbar: {e}") from e

    async def call(path: str, interface: str, member: str, signature: str = "", body: list | None = None):
        reply = await bus.call(
            Message(
                destination="org.bluez", path=path, interface=interface, member=member,
                signature=signature, body=body or [],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise TransportError(f"Bluetooth: {reply.error_name} {' '.join(map(str, reply.body))}".strip())
        return reply.body

    async def managed_objects() -> dict:
        body = await call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return unpack_variants(body[0])

    try:
        objects = await managed_objects()
        adapters = [p for p, ifaces in objects.items() if "org.bluez.Adapter1" in ifaces]
        powered = [p for p in adapters if objects[p]["org.bluez.Adapter1"].get("Powered")]
        if not adapters:
            raise TransportError("Kein Bluetooth-Adapter gefunden.")
        if not powered:
            raise TransportError("Bluetooth ist ausgeschaltet.")
        adapter = powered[0]

        await call(
            adapter, "org.bluez.Adapter1", "SetDiscoveryFilter", "a{sv}", [{"Transport": Variant("s", "bredr")}]
        )
        started = False
        try:
            await call(adapter, "org.bluez.Adapter1", "StartDiscovery")
            started = True
        except TransportError as e:
            log.warning("StartDiscovery failed, listing known devices only: %s", e)
        await asyncio.sleep(timeout)
        if started:
            try:
                await call(adapter, "org.bluez.Adapter1", "StopDiscovery")
            except TransportError as e:
                log.warning("StopDiscovery failed: %s", e)
        objects = await managed_objects()
    finally:
        bus.disconnect()

    printers = []
    for path, ifaces in objects.items():
        device = ifaces.get("org.bluez.Device1")
        if device is None or not str(path).startswith(adapter + "/"):
            continue
        name = str(device.get("Name") or device.get("Alias") or "")
        uuids = {str(u).lower() for u in device.get("UUIDs") or []}
        if not is_ptouch_name(name) and SERIAL_PORT_UUID not in uuids:
            continue
        try:
            address = normalize_address(str(device.get("Address", "")))
        except TransportError:
            continue
        rssi = device.get("RSSI")
        printers.append(
            FoundPrinter(address, name, bool(device.get("Paired")), int(rssi) if isinstance(rssi, int) else None)
        )
    return printers


# --- Windows: WinRT device enumeration -------------------------------------------

async def _scan_windows(timeout: float) -> list[FoundPrinter]:
    try:
        from winrt.windows.devices.bluetooth import BluetoothDevice
        from winrt.windows.devices.enumeration import DeviceInformation
    except ImportError as e:
        raise TransportError(f"Windows-Bluetooth-Schnittstelle fehlt: {e}") from e

    printers: dict[str, FoundPrinter] = {}
    for paired in (True, False):
        selector = BluetoothDevice.get_device_selector_from_pairing_state(paired)
        try:
            infos = await asyncio.wait_for(DeviceInformation.find_all_async_aqs_filter(selector), timeout)
        except (OSError, TimeoutError) as e:
            log.warning("Bluetooth enumeration (paired=%s) failed: %s", paired, e)
            continue
        for info in infos:
            try:
                device = await BluetoothDevice.from_id_async(info.id)
            except OSError as e:
                log.warning("Cannot open %s: %s", info.name, e)
                continue
            if device is None:
                continue
            raw = device.bluetooth_address
            address = ":".join(f"{(raw >> shift) & 0xFF:02X}" for shift in range(40, -8, -8))
            name = device.name or info.name or ""
            if not is_ptouch_name(name):
                continue
            printers.setdefault(address, FoundPrinter(address, name, paired))
    return list(printers.values())
