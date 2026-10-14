from __future__ import annotations

import csv
import json
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from time import monotonic
from typing import Any, Iterable

from .protocols import ChannelFrame


def frame_to_dict(frame: ChannelFrame, elapsed: float) -> dict[str, Any]:
    return {
        "type": "frame",
        "t": round(elapsed, 6),
        "protocol": frame.protocol,
        "channels": list(frame.channels),
        "microseconds": list(frame.microseconds),
        "raw": frame.raw_frame.hex(),
        "failsafe": frame.failsafe,
        "frame_lost": frame.frame_lost,
        "digital_17": frame.digital_channel_17,
        "digital_18": frame.digital_channel_18,
        "channel_min": frame.channel_min,
        "channel_max": frame.channel_max,
        "percentages": list(frame.percent_override) if frame.percent_override else None,
        "metadata": list(frame.metadata),
        "frame_name": frame.frame_name,
    }


def frame_from_dict(record: dict[str, Any]) -> ChannelFrame:
    microseconds = tuple(int(value) for value in record.get("microseconds", ()))
    percentages = record.get("percentages")
    return ChannelFrame(
        protocol=str(record["protocol"]),
        channels=tuple(int(value) for value in record["channels"]),
        raw_frame=bytes.fromhex(record.get("raw", "")),
        failsafe=bool(record.get("failsafe", False)),
        frame_lost=bool(record.get("frame_lost", False)),
        digital_channel_17=bool(record.get("digital_17", False)),
        digital_channel_18=bool(record.get("digital_18", False)),
        channel_min=int(record.get("channel_min", 0)),
        channel_max=int(record.get("channel_max", 2047)),
        microseconds_override=microseconds or None,
        percent_override=(
            tuple(float(value) for value in percentages)
            if percentages is not None
            else None
        ),
        metadata=tuple(
            (str(key), str(value)) for key, value in record.get("metadata", ())
        ),
        frame_name=str(record.get("frame_name", "")),
    )


class SessionRecorder:
    def __init__(self) -> None:
        self._file = None
        self._started_at = 0.0
        self.path: Path | None = None
        self.frame_count = 0

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, path: str | Path) -> None:
        self.stop()
        self.path = Path(path)
        self._file = self.path.open("w", encoding="utf-8", buffering=64 * 1024)
        self._file.write(
            json.dumps(
                {"type": "header", "format": "rc-reader-log", "version": 1},
                ensure_ascii=False,
            )
            + "\n"
        )
        self._started_at = monotonic()
        self.frame_count = 0

    def record(self, frame: ChannelFrame) -> None:
        if self._file is None:
            return
        elapsed = monotonic() - self._started_at
        self._file.write(
            json.dumps(frame_to_dict(frame, elapsed), ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        self.frame_count += 1
        if self.frame_count % 100 == 0:
            self._file.flush()

    def stop(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None


class ReplayWorker:
    def __init__(
        self,
        path: str | Path,
        events: Queue[tuple[str, Any]],
        speed: float = 1.0,
    ) -> None:
        self.path = Path(path)
        self.events = events
        self.speed = max(0.1, speed)
        self._stop_event = Event()
        self._thread: Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="session-replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        try:
            started = monotonic()
            with self.path.open("r", encoding="utf-8") as source:
                for line in source:
                    if self._stop_event.is_set():
                        break
                    record = json.loads(line)
                    if record.get("type") != "frame":
                        continue
                    target = float(record.get("t", 0)) / self.speed
                    delay = target - (monotonic() - started)
                    if delay > 0 and self._stop_event.wait(delay):
                        break
                    self.events.put(("replay_frame", frame_from_dict(record)))
            self.events.put(("replay_done", None))
        except Exception as exc:
            self.events.put(("replay_error", str(exc)))


def export_csv(
    path: str | Path,
    frames: Iterable[tuple[float, ChannelFrame]],
    max_channels: int = 32,
) -> None:
    columns = ["time_s", "protocol", "frame", "failsafe", "frame_lost"]
    for index in range(1, max_channels + 1):
        columns.extend((f"ch{index}_raw", f"ch{index}_us"))

    with Path(path).open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns)
        writer.writeheader()
        for elapsed, frame in frames:
            row: dict[str, Any] = {
                "time_s": f"{elapsed:.6f}",
                "protocol": frame.protocol,
                "frame": frame.frame_name,
                "failsafe": int(frame.failsafe),
                "frame_lost": int(frame.frame_lost),
            }
            for index, (raw, microseconds) in enumerate(
                zip(frame.channels, frame.microseconds), start=1
            ):
                if index > max_channels:
                    break
                row[f"ch{index}_raw"] = raw
                row[f"ch{index}_us"] = microseconds
            writer.writerow(row)

