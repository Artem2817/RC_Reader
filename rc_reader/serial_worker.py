from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic
from typing import Any

from .plugin_loader import find_plugin, load_plugins
from .profiles import PROTOCOL_BY_NAME, PROTOCOL_SPECS, SCAN_BAUDRATES, ProtocolSpec
from .protocols import (
    CRSFParser,
    FPortParser,
    GHSTParser,
    IBUSParser,
    MAVLinkParser,
    SBUSParser,
    SUMDParser,
    SpektrumParser,
)


UART_FORMATS = ("8N1", "8N2", "8E1", "8E2", "8O1", "8O2")


def parse_uart_format(value: str) -> tuple[int, str, int]:
    """Return pyserial-compatible bytesize, parity and stop bits."""
    normalized = value.strip().upper()
    if normalized not in UART_FORMATS:
        raise ValueError(f"Unsupported UART format: {value}")
    return int(normalized[0]), normalized[1], int(normalized[2])


def create_parser(protocol: str):
    parser_factories = {
        "CRSF": CRSFParser,
        "SBUS": SBUSParser,
        "F.PORT": FPortParser,
        "GHST": GHSTParser,
        "IBUS": IBUSParser,
        "SUMD": SUMDParser,
        "DSM2 1024": lambda: SpektrumParser(1024),
        "DSMX 2048": lambda: SpektrumParser(2048),
        "MAVLINK": MAVLinkParser,
    }
    normalized = protocol.strip().upper()
    try:
        return parser_factories[normalized]()
    except KeyError as exc:
        plugin = find_plugin(normalized)
        if plugin is not None:
            return plugin.factory()
        raise ValueError(f"Unsupported protocol: {protocol}") from exc


@dataclass(frozen=True, slots=True)
class SerialConfig:
    port: str
    baudrate: int
    protocol: str
    uart_format: str


class SerialWorker:
    """Read a serial port in a background thread and emit events to a queue."""

    def __init__(
        self,
        config: SerialConfig,
        events: Queue[tuple[str, Any]],
        event_prefix: str = "",
    ) -> None:
        self.config = config
        self.events = events
        self.event_prefix = event_prefix
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._serial: Any = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="serial-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.cancel_read()
            except (AttributeError, OSError):
                pass

    def _emit(self, kind: str, payload: Any = None) -> None:
        self.events.put((f"{self.event_prefix}{kind}", payload))

    def _run(self) -> None:
        try:
            import serial

            parser = create_parser(self.config.protocol)
            bytesize, parity, stopbits = parse_uart_format(self.config.uart_format)

            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                timeout=0.1,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self._emit("connected")
            last_stats = monotonic()

            while not self._stop_event.is_set():
                waiting = self._serial.in_waiting
                data = self._serial.read(max(1, min(waiting, 4096)))
                if data:
                    for frame in parser.feed(data):
                        self._emit("frame", frame)
                    drain_packets = getattr(parser, "drain_packets", None)
                    if drain_packets is not None:
                        for packet in drain_packets():
                            self._emit("packet", packet)

                now = monotonic()
                if now - last_stats >= 0.25:
                    self._emit("stats", parser.stats.snapshot())
                    last_stats = now

            self._emit("stats", parser.stats.snapshot())
        except Exception as exc:
            self._emit("error", str(exc))
        finally:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self._emit("disconnected")


class SerialOutputWorker:
    """Own a serial TX port and send encoded frames supplied by the UI."""

    def __init__(self, config: SerialConfig, events: Queue[tuple[str, Any]]) -> None:
        self.config = config
        self.events = events
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._serial: Any = None
        self._outbound: Queue[bytes] = Queue(maxsize=500)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="serial-output", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def send(self, data: bytes) -> None:
        if not data or not self.running:
            return
        try:
            self._outbound.put_nowait(data)
        except Full:
            try:
                self._outbound.get_nowait()
                self._outbound.put_nowait(data)
            except (Empty, Full):
                pass

    def _run(self) -> None:
        try:
            import serial

            bytesize, parity, stopbits = parse_uart_format(self.config.uart_format)
            self._serial = serial.Serial(
                port=self.config.port,
                baudrate=self.config.baudrate,
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                timeout=0.1,
                write_timeout=0.5,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self.events.put(("output_connected", None))
            while not self._stop_event.is_set():
                try:
                    data = self._outbound.get(timeout=0.1)
                except Empty:
                    continue
                self._serial.write(data)
        except Exception as exc:
            self.events.put(("output_error", str(exc)))
        finally:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self.events.put(("output_disconnected", None))


class ScanWorker:
    """Try every parser at every baud/UART combination until RC frames arrive."""

    DWELL_SECONDS = 0.45

    def __init__(
        self,
        port: str,
        events: Queue[tuple[str, Any]],
        preferred_baudrate: int | None = None,
        preferred_uart_format: str | None = None,
    ) -> None:
        self.port = port
        self.events = events
        self.preferred_baudrate = preferred_baudrate
        self.preferred_uart_format = preferred_uart_format
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._serial: Any = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="protocol-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._serial is not None:
            try:
                self._serial.cancel_read()
            except (AttributeError, OSError):
                pass

    def _run(self) -> None:
        try:
            import serial

            attempts = self._build_attempts()
            observations: list[tuple[float, ProtocolSpec]] = []
            for index, (baudrate, uart_format) in enumerate(attempts, start=1):
                if self._stop_event.is_set():
                    break
                self.events.put(
                    (
                        "scan_progress",
                        (baudrate, uart_format, index, len(attempts)),
                    )
                )
                try:
                    bytesize, parity, stopbits = parse_uart_format(uart_format)
                    self._serial = serial.Serial(
                        port=self.port,
                        baudrate=baudrate,
                        bytesize=bytesize,
                        parity=parity,
                        stopbits=stopbits,
                        timeout=0.05,
                        xonxoff=False,
                        rtscts=False,
                        dsrdtr=False,
                    )
                    self._serial.reset_input_buffer()
                    parser_specs = PROTOCOL_SPECS + tuple(
                        plugin.spec for plugin in load_plugins()
                    )
                    parsers = {
                        spec.name: create_parser(spec.name)
                        for spec in parser_specs
                    }
                    deadline = monotonic() + self.DWELL_SECONDS
                    while monotonic() < deadline and not self._stop_event.is_set():
                        waiting = self._serial.in_waiting
                        data = self._serial.read(max(1, min(waiting, 4096)))
                        if data:
                            for parser in parsers.values():
                                parser.feed(data)
                        found_protocol = self._find_confident_protocol(parsers)
                        if found_protocol is not None:
                            profile = PROTOCOL_BY_NAME.get(found_protocol)
                            if profile is None:
                                plugin = find_plugin(found_protocol)
                                profile = plugin.spec if plugin is not None else ProtocolSpec(
                                    found_protocol, baudrate, uart_format
                                )
                            confidence = self._confidence(
                                found_protocol, parsers[found_protocol].stats
                            )
                            self.events.put(
                                (
                                    "scan_found",
                                    (
                                        ProtocolSpec(
                                            found_protocol,
                                            baudrate,
                                            uart_format,
                                            profile.wiring_note,
                                        ),
                                        confidence,
                                    ),
                                )
                            )
                            return
                    for protocol, parser in parsers.items():
                        if parser.stats.channel_frames:
                            profile = PROTOCOL_BY_NAME.get(protocol)
                            observations.append(
                                (
                                    self._confidence(protocol, parser.stats),
                                    ProtocolSpec(
                                        protocol,
                                        baudrate,
                                        uart_format,
                                        profile.wiring_note if profile else "",
                                    ),
                                )
                            )
                except (OSError, serial.SerialException, ValueError):
                    pass
                finally:
                    if self._serial is not None:
                        try:
                            self._serial.close()
                        except Exception:
                            pass
                        self._serial = None
            observations.sort(key=lambda item: item[0], reverse=True)
            self.events.put(("scan_done", tuple(observations[:3])))
        except Exception as exc:
            self.events.put(("scan_error", str(exc)))
        finally:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

    def _build_attempts(self) -> list[tuple[int, str]]:
        baudrates: list[int] = []
        if self.preferred_baudrate and self.preferred_baudrate > 0:
            baudrates.append(self.preferred_baudrate)
        for baudrate in SCAN_BAUDRATES:
            if baudrate not in baudrates:
                baudrates.append(baudrate)

        uart_formats: list[str] = []
        preferred_format = (self.preferred_uart_format or "").strip().upper()
        if preferred_format in UART_FORMATS:
            uart_formats.append(preferred_format)
        uart_formats.extend(
            uart_format
            for uart_format in UART_FORMATS
            if uart_format not in uart_formats
        )

        attempts: list[tuple[int, str]] = []

        def add_attempt(baudrate: int, uart_format: str) -> None:
            attempt = (baudrate, uart_format)
            if attempt not in attempts:
                attempts.append(attempt)

        if self.preferred_baudrate and preferred_format:
            add_attempt(self.preferred_baudrate, preferred_format)
        for spec in PROTOCOL_SPECS:
            add_attempt(spec.baudrate, spec.uart_format)
        for baudrate in baudrates:
            for uart_format in uart_formats:
                add_attempt(baudrate, uart_format)
        return attempts

    @staticmethod
    def _find_confident_protocol(parsers: dict[str, Any]) -> str | None:
        confident: list[tuple[int, int, str]] = []
        for protocol, parser in parsers.items():
            stats = parser.stats
            if protocol.startswith("DSM"):
                # DSM has no checksum. Demand a longer, mostly clean run to
                # avoid recognizing random bytes from another UART setting.
                enough = (
                    stats.channel_frames >= 8
                    and stats.invalid_frames <= stats.channel_frames * 2 + 10
                )
            else:
                enough = stats.channel_frames >= 3
            if enough:
                confident.append(
                    (stats.channel_frames, -stats.invalid_frames, protocol)
                )
        if not confident:
            return None
        confident.sort(reverse=True)
        return confident[0][2]

    @staticmethod
    def _confidence(protocol: str, stats: Any) -> float:
        frames = stats.channel_frames
        errors = stats.invalid_frames
        if protocol.startswith("DSM"):
            score = 35 + frames * 5 - min(30, errors * 0.5)
        else:
            score = 50 + frames * 15 - min(25, errors * 0.5)
        return max(1.0, min(99.9, score))
