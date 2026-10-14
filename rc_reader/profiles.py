from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProtocolSpec:
    name: str
    baudrate: int
    uart_format: str
    wiring_note: str = ""


PROTOCOL_SPECS = (
    ProtocolSpec("CRSF", 420000, "8N1", "RX адаптера ← TX приёмника"),
    ProtocolSpec("GHST", 420000, "8N1", "Однопроводный half-duplex; для чтения подключить линию к RX"),
    ProtocolSpec("SBUS", 100000, "8E2", "Обычно требуется аппаратная инверсия сигнала"),
    ProtocolSpec("F.Port", 115200, "8N1", "Однопроводный инвертированный half-duplex"),
    ProtocolSpec("iBUS", 115200, "8N1", "Неинвертированный UART"),
    ProtocolSpec("SUMD", 115200, "8N1", "Неинвертированный UART"),
    ProtocolSpec("DSM2 1024", 125000, "8N1", "Spektrum satellite: питание и логика 3.3 В"),
    ProtocolSpec("DSMX 2048", 125000, "8N1", "Spektrum satellite: питание и логика 3.3 В"),
    ProtocolSpec("MAVLink", 115200, "8N1", "Ищет сообщения RC_CHANNELS/RC_CHANNELS_RAW"),
)

PROTOCOL_BY_NAME = {spec.name: spec for spec in PROTOCOL_SPECS}

# The exhaustive scanner tests every protocol at every baud/UART combination.
# Ordering keeps the most likely RC rates near the front, while still covering
# common standard and non-standard serial rates.
SCAN_BAUDRATES = (
    420000,
    416666,
    115200,
    100000,
    125000,
    400000,
    250000,
    230400,
    200000,
    128000,
    76800,
    57600,
    38400,
    19200,
    9600,
    4800,
    460800,
    500000,
    921600,
    1000000,
    1500000,
    2000000,
)
