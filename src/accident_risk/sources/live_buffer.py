"""Bounded thread-safe acquisition queue; no barrier across sensor modalities."""

from collections import deque
from collections.abc import Iterator
from threading import RLock
from typing import Any

from accident_risk.schemas import SENSOR_FIELDS
from accident_risk.sources.base import DataSource


class LiveBuffer(DataSource):
    """Nonblocking drain iterator; overflow is explicit, never silent data loss.

    The latest-observation cache is diagnostic. The shared inference synchronizer
    applies staleness; raw queued frames retain original observations.
    """

    def __init__(self, capacity: int = 10000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._lock = RLock()
        self._queue: deque = deque()
        self._latest: dict[str, dict[str, tuple[float, float]]] = {}
        self._running = True

    def start(self) -> None:
        """Accept updates."""
        with self._lock:
            self._running = True

    def stop(self) -> None:
        """Reject new updates while allowing queued observations to drain."""
        with self._lock:
            self._running = False

    def push(self, frame: Any) -> None:
        """Enqueue immediately, supporting both SensorFrame models and dictionary frames."""
        with self._lock:
            if not self._running:
                raise RuntimeError("Live session is stopped")
            if len(self._queue) >= self.capacity:
                raise BufferError(
                    "Live buffer full; consumer must drain or apply backpressure")

            is_dict = isinstance(frame, dict)
            stream_id = getattr(frame, "stream_id", None) or (
                frame.get("stream_id", "default") if is_dict else "default"
            )
            latest = self._latest.setdefault(stream_id, {})

            timestamp = getattr(frame, "timestamp", None) or (
                frame.get("timestamp", 0.0) if is_dict else 0.0
            )

            sensor_ts = {}
            if hasattr(frame, "sensor_timestamps"):
                sensor_ts = getattr(frame, "sensor_timestamps", {}) or {}
            elif is_dict:
                sensor_ts = frame.get("sensor_timestamps", {}) or {}

            # Evaluate both standard schema fields and dynamic dictionary keys
            keys_to_check = set(SENSOR_FIELDS).union(
                frame.keys()) if is_dict else SENSOR_FIELDS

            for name in keys_to_check:
                if name in ("timestamp", "stream_id", "source", "sensor_timestamps"):
                    continue
                value = frame.get(name) if is_dict else getattr(
                    frame, name, None)
                observed = sensor_ts.get(name, timestamp)
                if value is not None and (name not in latest or observed >= latest[name][1]):
                    latest[name] = (value, observed)

            self._queue.append(frame)

    def ages(self, stream_id: str, now: float) -> dict[str, float]:
        """Return ages in seconds for observations at or before `now`."""
        with self._lock:
            return {
                key: now - t for key, (_, t) in self._latest.get(stream_id, {}).items() if t <= now
            }

    def __iter__(self) -> Iterator:
        """Drain what is available; never block waiting for HR or SpO2."""
        while True:
            with self._lock:
                if not self._queue:
                    return
                frame = self._queue.popleft()
            yield frame
