from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import statistics
from time import monotonic

from .protocols import ChannelFrame


@dataclass(frozen=True, slots=True)
class Alert:
    timestamp: float
    level: str
    message: str


@dataclass(frozen=True, slots=True)
class ChannelMetrics:
    channel: int
    samples: int
    current_us: int
    minimum_us: int
    maximum_us: int
    mean_us: float
    jitter_us: float
    noise_us: float
    kind: str


class SignalAnalyzer:
    """Accumulates timing, channel quality and failsafe information."""

    def __init__(
        self,
        max_channels: int = 32,
        dropout_threshold_ms: float = 50.0,
        jitter_alert_ms: float = 5.0,
        lq_alert_percent: float = 30.0,
    ) -> None:
        self.max_channels = max_channels
        self.dropout_threshold_ms = dropout_threshold_ms
        self.jitter_alert_ms = jitter_alert_ms
        self.lq_alert_percent = lq_alert_percent
        self.reset()

    def reset(self) -> None:
        self.started_at = monotonic()
        self.last_frame_at: float | None = None
        self.frame_count = 0
        self.failsafe_count = 0
        self.frame_lost_count = 0
        self.dropout_count = 0
        self.longest_gap_ms = 0.0
        self.intervals_ms: deque[float] = deque(maxlen=10_000)
        self.protocol_counts: Counter[str] = Counter()
        self.channel_values: list[deque[int]] = [
            deque(maxlen=10_000) for _ in range(self.max_channels)
        ]
        self.channel_current = [1500] * self.max_channels
        self.alerts: deque[Alert] = deque(maxlen=1000)
        self._last_jitter_alert_at = 0.0
        self._last_lq_alert_at = 0.0

    def ingest(
        self, frame: ChannelFrame, timestamp: float | None = None
    ) -> tuple[Alert, ...]:
        now = monotonic() if timestamp is None else timestamp
        new_alerts: list[Alert] = []
        if self.last_frame_at is not None:
            gap_ms = max(0.0, (now - self.last_frame_at) * 1000)
            if gap_ms < 60_000:
                self.intervals_ms.append(gap_ms)
                self.longest_gap_ms = max(self.longest_gap_ms, gap_ms)
                if gap_ms >= self.dropout_threshold_ms:
                    self.dropout_count += 1
                    new_alerts.append(
                        Alert(now, "DROP", f"Пауза между кадрами {gap_ms:.1f} ms")
                    )
        self.last_frame_at = now
        self.frame_count += 1
        self.protocol_counts[frame.protocol] += 1

        if frame.failsafe:
            self.failsafe_count += 1
            new_alerts.append(Alert(now, "FAILSAFE", f"{frame.protocol}: FAILSAFE"))
        if frame.frame_lost:
            self.frame_lost_count += 1
            new_alerts.append(
                Alert(now, "LOST", f"{frame.protocol}: установлен Frame Lost")
            )

        for index, value in enumerate(frame.microseconds[: self.max_channels]):
            self.channel_current[index] = value
            self.channel_values[index].append(value)

        jitter = self.jitter_ms
        if (
            len(self.intervals_ms) >= 20
            and jitter >= self.jitter_alert_ms
            and now - self._last_jitter_alert_at >= 2.0
        ):
            self._last_jitter_alert_at = now
            new_alerts.append(Alert(now, "JITTER", f"Jitter кадров {jitter:.2f} ms"))

        metadata = dict(frame.metadata)
        lq_text = (
            metadata.get("Link Quality")
            or metadata.get("RX Link Quality")
            or metadata.get("Downlink LQ")
        )
        if lq_text:
            try:
                lq = float(lq_text.replace("%", "").strip())
                if lq < self.lq_alert_percent and now - self._last_lq_alert_at >= 2.0:
                    self._last_lq_alert_at = now
                    new_alerts.append(Alert(now, "LOW LQ", f"Link Quality {lq:.0f}%"))
            except ValueError:
                pass

        self.alerts.extend(new_alerts)
        return tuple(new_alerts)

    @property
    def mean_interval_ms(self) -> float:
        return statistics.fmean(self.intervals_ms) if self.intervals_ms else 0.0

    @property
    def rate_hz(self) -> float:
        mean = self.mean_interval_ms
        return 1000.0 / mean if mean > 0 else 0.0

    @property
    def jitter_ms(self) -> float:
        return (
            statistics.pstdev(self.intervals_ms)
            if len(self.intervals_ms) > 1
            else 0.0
        )

    def channel_metrics(self, index: int) -> ChannelMetrics:
        values = self.channel_values[index]
        if not values:
            return ChannelMetrics(index + 1, 0, 0, 0, 0, 0.0, 0.0, 0.0, "—")
        current = self.channel_current[index]
        minimum = min(values)
        maximum = max(values)
        mean = statistics.fmean(values)
        jitter = statistics.pstdev(values) if len(values) > 1 else 0.0
        centered = [value for value in values if 1450 <= value <= 1550]
        noise = statistics.pstdev(centered) if len(centered) > 1 else 0.0
        unique_rounded = len({round(value / 20) for value in values})
        span = maximum - minimum
        if span < 30:
            kind = "constant"
        elif unique_rounded <= 4:
            kind = "switch"
        else:
            kind = "analog"
        return ChannelMetrics(
            index + 1,
            len(values),
            current,
            minimum,
            maximum,
            mean,
            jitter,
            noise,
            kind,
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "duration_s": monotonic() - self.started_at,
            "frames": self.frame_count,
            "rate_hz": self.rate_hz,
            "mean_interval_ms": self.mean_interval_ms,
            "jitter_ms": self.jitter_ms,
            "dropouts": self.dropout_count,
            "longest_gap_ms": self.longest_gap_ms,
            "frame_lost": self.frame_lost_count,
            "failsafe": self.failsafe_count,
            "protocols": dict(self.protocol_counts),
        }


class CalibrationWizard:
    ROLES = ("Roll", "Pitch", "Throttle", "Yaw", "Aux 1", "Aux 2", "Aux 3", "Aux 4")

    def __init__(self, max_channels: int = 32) -> None:
        self.max_channels = max_channels
        self.mapping: dict[str, int] = {}
        self.active_role: str | None = None
        self.minimums = [10_000] * max_channels
        self.maximums = [0] * max_channels

    def begin(self, role: str) -> None:
        if role not in self.ROLES:
            raise ValueError(f"Unknown calibration role: {role}")
        self.active_role = role
        self.minimums = [10_000] * self.max_channels
        self.maximums = [0] * self.max_channels

    def ingest(self, frame: ChannelFrame) -> None:
        if self.active_role is None:
            return
        for index, value in enumerate(frame.microseconds[: self.max_channels]):
            self.minimums[index] = min(self.minimums[index], value)
            self.maximums[index] = max(self.maximums[index], value)

    def finish(self) -> tuple[str, int, int, int] | None:
        if self.active_role is None:
            return None
        used = set(self.mapping.values())
        spans = [
            (self.maximums[index] - self.minimums[index], index)
            for index in range(self.max_channels)
            if self.maximums[index] > 0 and index not in used
        ]
        role = self.active_role
        self.active_role = None
        if not spans:
            return None
        _span, index = max(spans)
        self.mapping[role] = index
        return role, index + 1, self.minimums[index], self.maximums[index]


class DualComparator:
    def __init__(self, max_channels: int = 32) -> None:
        self.max_channels = max_channels
        self.primary: tuple[float, ChannelFrame] | None = None
        self.secondary: tuple[float, ChannelFrame] | None = None

    def ingest(self, source: str, frame: ChannelFrame, timestamp: float | None = None) -> None:
        sample = (monotonic() if timestamp is None else timestamp, frame)
        if source == "primary":
            self.primary = sample
        else:
            self.secondary = sample

    def compare(self) -> tuple[float, tuple[int, ...]] | None:
        if self.primary is None or self.secondary is None:
            return None
        primary_time, primary = self.primary
        secondary_time, secondary = self.secondary
        count = min(
            self.max_channels,
            len(primary.microseconds),
            len(secondary.microseconds),
        )
        deltas = tuple(
            secondary.microseconds[index] - primary.microseconds[index]
            for index in range(count)
        )
        return (secondary_time - primary_time) * 1000, deltas
