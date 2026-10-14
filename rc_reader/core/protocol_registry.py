from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..encoder import encode_channels
from ..plugin_loader import load_plugins
from ..serial_worker import create_parser


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    name: str
    baudrate: int
    data_bits: int = 8
    parity: str = "N"
    stop_bits: int = 1
    inverted: bool = False
    half_duplex: bool = False
    frame_format: str = ""
    channel_count: str = ""
    channel_range: str = ""
    frame_rate: str = ""
    rx: bool = True
    tx: bool = False
    custom_baudrate: bool = False
    tx_rate_min: int = 1
    tx_rate_max: int = 500
    note: str = ""

    @property
    def uart_format(self) -> str:
        return f"{self.data_bits}{self.parity}{self.stop_bits}"


class ProtocolAdapter:
    """Uniform facade over the project's proven streaming parsers/encoders."""

    def __init__(self, config: ProtocolConfig, parser_name: str | None = None) -> None:
        self.config = config
        self.parser_name = parser_name or config.name

    def new_decoder(self) -> Any:
        if not self.config.rx:
            raise ValueError(f"{self.config.name} does not support reception")
        return create_parser(self.parser_name)

    def decode(self, decoder: Any, data: bytes) -> list[Any]:
        return decoder.feed(data)

    def encode(self, channels_us: tuple[int, ...] | list[int]) -> bytes:
        if not self.config.tx:
            raise ValueError(f"{self.config.name} does not support transmission")
        return encode_channels(self.parser_name, channels_us)

    def validate(self, frame: bytes) -> bool:
        if not frame or not self.config.rx:
            return False
        decoder = self.new_decoder()
        return bool(decoder.feed(frame))

    def detect(self, data: bytes, minimum_frames: int = 3) -> float:
        if not self.config.rx:
            return 0.0
        decoder = self.new_decoder()
        decoder.feed(data)
        stats = decoder.stats
        if stats.channel_frames < minimum_frames:
            return 0.0
        penalty = min(35.0, stats.invalid_frames * 1.5)
        return max(1.0, min(99.9, 50.0 + stats.channel_frames * 12.0 - penalty))

    def get_protocol_config(self) -> ProtocolConfig:
        return self.config


def _c(
    name: str,
    baudrate: int,
    uart: str,
    *,
    inverted: bool = False,
    half_duplex: bool = False,
    frame: str,
    channels: str,
    channel_range: str,
    rate: str,
    tx: bool = False,
    custom: bool = False,
    tx_rates: tuple[int, int] = (1, 500),
    note: str = "",
) -> ProtocolConfig:
    return ProtocolConfig(
        name=name,
        baudrate=baudrate,
        data_bits=int(uart[0]),
        parity=uart[1],
        stop_bits=int(uart[2]),
        inverted=inverted,
        half_duplex=half_duplex,
        frame_format=frame,
        channel_count=channels,
        channel_range=channel_range,
        frame_rate=rate,
        tx=tx,
        custom_baudrate=custom,
        tx_rate_min=tx_rates[0],
        tx_rate_max=tx_rates[1],
        note=note,
    )


_ADAPTERS = (
    ProtocolAdapter(_c("CRSF", 420000, "8N1", frame="length + type + payload + CRC8", channels="16/32 subset", channel_range="172–1811", rate="up to 500 Hz", tx=True, custom=True, tx_rates=(25, 500), note="ELRS uses the CRSF wire protocol")),
    ProtocolAdapter(_c("SBUS", 100000, "8E2", inverted=True, frame="25 bytes", channels="16 + 2 digital", channel_range="172–1811", rate="~70/140 Hz", tx=True, tx_rates=(50, 150), note="Requires a hardware inverter with most USB-UART adapters")),
    ProtocolAdapter(_c("SBUS2", 100000, "8E2", inverted=True, frame="25-byte RC frame", channels="16 + 2 digital", channel_range="172–1811", rate="~70/140 Hz", tx=True, tx_rates=(50, 150), note="Telemetry slots are not transmitted"), "SBUS"),
    ProtocolAdapter(_c("iBUS", 115200, "8N1", frame="32 bytes + checksum", channels="14 (18 extended RX)", channel_range="1000–2000 µs", rate="~100 Hz", tx=True, custom=True, tx_rates=(25, 150))),
    ProtocolAdapter(_c("SUMD", 115200, "8N1", frame="header + channels + CRC16", channels="up to 32", channel_range="8000–16000", rate="~100 Hz", tx=True, custom=True, tx_rates=(25, 200))),
    ProtocolAdapter(_c("F.Port", 115200, "8N1", inverted=True, half_duplex=True, frame="escaped variable frame + checksum", channels="16 + 2 digital", channel_range="172–1811", rate="~100 Hz", note="RX only; external inversion and half-duplex interface may be required")),
    ProtocolAdapter(_c("GHST", 420000, "8N1", half_duplex=True, frame="length + type + payload + CRC8", channels="16", channel_range="12-bit", rate="variable", custom=True, note="RX only")),
    ProtocolAdapter(_c("DSM2 1024", 125000, "8N1", frame="16-byte satellite", channels="7 per packet", channel_range="10-bit", rate="11/22 ms", custom=True, note="RX only; 3.3 V logic")),
    ProtocolAdapter(_c("DSMX 2048", 125000, "8N1", frame="16-byte satellite", channels="up to 12", channel_range="11-bit", rate="11/22 ms", custom=True, note="RX only; 3.3 V logic")),
    ProtocolAdapter(_c("MAVLink", 115200, "8N1", frame="MAVLink 1/2 RC messages", channels="up to 18", channel_range="µs/scaled", rate="source-defined", custom=True, note="RX only")),
)


REGISTRY: dict[str, ProtocolAdapter] = {adapter.config.name: adapter for adapter in _ADAPTERS}
for _plugin in load_plugins():
    _spec = _plugin.spec
    _uart = _spec.uart_format if len(_spec.uart_format) == 3 else "8N1"
    REGISTRY.setdefault(
        _spec.name,
        ProtocolAdapter(
            ProtocolConfig(
                name=_spec.name,
                baudrate=_spec.baudrate,
                data_bits=int(_uart[0]),
                parity=_uart[1],
                stop_bits=int(_uart[2]),
                frame_format="External parser plugin",
                channel_count="plugin-defined",
                channel_range="plugin-defined",
                frame_rate="plugin-defined",
                rx=True,
                tx=False,
                custom_baudrate=True,
                note=_spec.wiring_note,
            ),
            _spec.name,
        ),
    )


def protocol_names(*, rx: bool | None = None, tx: bool | None = None) -> tuple[str, ...]:
    return tuple(
        name
        for name, adapter in REGISTRY.items()
        if (rx is None or adapter.config.rx is rx)
        and (tx is None or adapter.config.tx is tx)
    )
