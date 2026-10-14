from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PortInfo:
    device: str
    label: str
    adapter: str
    vid: int | None = None
    pid: int | None = None


_KNOWN = {
    0x1A86: "CH340/CH341",
    0x10C4: "CP210x",
    0x0403: "FTDI",
}


def list_serial_ports() -> tuple[PortInfo, ...]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return ()
    result: list[PortInfo] = []
    for item in list_ports.comports():
        adapter = _KNOWN.get(item.vid, "USB CDC/UART" if item.vid else "Serial port")
        description = item.description or adapter
        label = f"{item.device}  ·  {description}"
        result.append(PortInfo(item.device, label, adapter, item.vid, item.pid))
    return tuple(sorted(result, key=lambda port: port.device))


def transmission_warning(protocol: str, adapter: str) -> str:
    from .protocol_registry import REGISTRY

    config = REGISTRY[protocol].config
    if config.inverted and adapter in {"CH340/CH341", "CP210x", "FTDI", "USB CDC/UART", "Serial port"}:
        return (
            f"{protocol} requires an inverted UART signal. {adapter} normally cannot "
            "produce it directly; use a hardware inverter or an adapter with inversion support."
        )
    if config.half_duplex:
        return f"{protocol} uses half-duplex wiring; verify line direction and electrical interface."
    return ""
