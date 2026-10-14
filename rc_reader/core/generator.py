from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from queue import Full, Queue
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any

from .protocol_registry import REGISTRY
from ..serial_worker import parse_uart_format


@dataclass(slots=True)
class GeneratorProfile:
    name: str
    protocol: str
    baudrate: int
    uart_format: str
    frame_rate: float
    channels: list[int]
    enabled: list[bool]
    mode: str = "Manual"
    active_channel: int = 0
    step_size: int = 50
    sequence: list[dict[str, Any]] | None = None

    def normalized(self) -> "GeneratorProfile":
        if self.protocol not in REGISTRY or not REGISTRY[self.protocol].config.tx:
            raise ValueError(f"TX is not supported for {self.protocol}")
        config = REGISTRY[self.protocol].config
        self.baudrate = max(1200, min(4_000_000, int(self.baudrate)))
        self.frame_rate = max(float(config.tx_rate_min), min(float(config.tx_rate_max), float(self.frame_rate)))
        self.channels = [max(750, min(2250, int(value))) for value in self.channels[:32]]
        if not self.channels:
            self.channels = [1500] * 16
        self.enabled = (list(self.enabled) + [True] * 32)[: len(self.channels)]
        self.active_channel = max(0, min(len(self.channels) - 1, int(self.active_channel)))
        self.step_size = max(1, min(500, int(self.step_size)))
        return self


def save_profile(path: str | Path, profile: GeneratorProfile) -> None:
    target = Path(path)
    target.write_text(json.dumps(asdict(profile.normalized()), indent=2), encoding="utf-8")


def load_profile(path: str | Path) -> GeneratorProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Generator profile must be a JSON object")
    allowed = set(GeneratorProfile.__dataclass_fields__)
    return GeneratorProfile(**{key: value for key, value in data.items() if key in allowed}).normalized()


class RCGenerator:
    """Deterministic periodic binary transmitter independent from GUI timing."""

    def __init__(self, events: Queue[tuple[str, Any]]) -> None:
        self.events = events
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._serial: Any = None
        self._profile: GeneratorProfile | None = None
        self._sweep_direction = 1
        self._last_motion = 0.0
        self._sequence_index = 0
        self._sequence_started = 0.0
        self.sent_packets = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, port: str, profile: GeneratorProfile) -> None:
        if self.running:
            return
        self._profile = profile.normalized()
        self.sent_packets = 0
        self._stop.clear()
        self._thread = Thread(target=self._run, args=(port,), name="rc-generator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.cancel_write()
            except (AttributeError, OSError):
                pass

    def update_channel(self, index: int, value: int) -> None:
        with self._lock:
            if self._profile and 0 <= index < len(self._profile.channels):
                self._profile.channels[index] = max(750, min(2250, int(value)))

    def set_mode(self, mode: str, active_channel: int = 0, step_size: int = 50) -> None:
        with self._lock:
            if self._profile:
                self._profile.mode = mode
                self._profile.active_channel = active_channel
                self._profile.step_size = step_size
                self._last_motion = 0.0

    def _emit(self, kind: str, payload: Any = None) -> None:
        try:
            self.events.put_nowait((kind, payload))
        except Full:
            pass

    def _run(self, port: str) -> None:
        try:
            import serial

            assert self._profile is not None
            bits, parity, stopbits = parse_uart_format(self._profile.uart_format)
            self._serial = serial.Serial(
                port=port,
                baudrate=self._profile.baudrate,
                bytesize=bits,
                parity=parity,
                stopbits=stopbits,
                timeout=0.05,
                write_timeout=0.5,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            self._emit("tx_connected")
            next_deadline = monotonic()
            while not self._stop.is_set():
                now = monotonic()
                if now < next_deadline:
                    self._stop.wait(min(0.01, next_deadline - now))
                    continue
                with self._lock:
                    self._advance_motion(now)
                    profile = self._profile
                    assert profile is not None
                    channels = tuple(value if enabled else 1500 for value, enabled in zip(profile.channels, profile.enabled))
                    frame_rate = profile.frame_rate
                    frame = REGISTRY[profile.protocol].encode(channels)
                self._serial.write(frame)
                self.sent_packets += 1
                self._emit("tx_frame", (monotonic(), frame, self.sent_packets))
                next_deadline += 1.0 / frame_rate
                if next_deadline < monotonic() - 0.25:
                    next_deadline = monotonic()
        except Exception as exc:
            self._emit("tx_error", str(exc))
        finally:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self._emit("tx_disconnected")

    def _advance_motion(self, now: float) -> None:
        assert self._profile is not None
        profile = self._profile
        channel = max(0, min(len(profile.channels) - 1, profile.active_channel))
        if profile.mode == "Sweep":
            elapsed = max(0.0, now - (self._last_motion or now))
            value = profile.channels[channel] + self._sweep_direction * max(1, round(elapsed * 500))
            if value >= 2000:
                value, self._sweep_direction = 2000, -1
            elif value <= 1000:
                value, self._sweep_direction = 1000, 1
            profile.channels[channel] = value
            self._last_motion = now
        elif profile.mode == "Step" and now - self._last_motion >= 0.5:
            value = profile.channels[channel] + self._sweep_direction * profile.step_size
            if value >= 2000:
                value, self._sweep_direction = 2000, -1
            elif value <= 1000:
                value, self._sweep_direction = 1000, 1
            profile.channels[channel] = value
            self._last_motion = now
        elif profile.mode == "Sequence" and profile.sequence:
            item = profile.sequence[self._sequence_index % len(profile.sequence)]
            if not self._sequence_started:
                self._sequence_started = now
            if now - self._sequence_started >= max(0.01, float(item.get("duration", 1.0))):
                self._sequence_index = (self._sequence_index + 1) % len(profile.sequence)
                self._sequence_started = now
                item = profile.sequence[self._sequence_index]
            values = item.get("channels", [])
            for index, value in enumerate(values[: len(profile.channels)]):
                profile.channels[index] = max(750, min(2250, int(value)))
