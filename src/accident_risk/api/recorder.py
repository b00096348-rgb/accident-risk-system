"""Turn the live inference stream into persisted sessions, tracks and alerts.

The recorder sits between ingestion and the database. It never raises into the
ingestion path: a storage failure must not stop a frame from being processed,
because losing telemetry is worse than losing a history row.
"""

from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Any

from accident_risk.api.db import Database
from accident_risk.schemas import PredictionResult, SensorFrame

LOGGER = logging.getLogger("accident_risk")

# One track point per second is enough for history and the live map; raw frames
# arrive far faster than that and belong in the research pipeline, not SQLite.
POINT_INTERVAL_SECONDS = 1.0
# Do not raise a second high-risk alert for the same driver inside this window.
ALERT_COOLDOWN_SECONDS = 60.0


class SessionRecorder:
    """Persist track points and raise alerts for frames belonging to open sessions."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self._lock = RLock()
        self._sessions: dict[str, int] = {}
        self._last_point: dict[str, float] = {}
        self._last_alert: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    def register(self, stream_id: str, session_id: int) -> None:
        """Begin recording frames for a stream."""
        with self._lock:
            self._sessions[stream_id] = session_id
            self._last_point.pop(stream_id, None)
            self._last_alert.pop(stream_id, None)
            self._last_seen.pop(stream_id, None)

    def release(self, stream_id: str) -> None:
        """Stop recording frames for a stream."""
        with self._lock:
            for store in (self._sessions, self._last_point, self._last_alert, self._last_seen):
                store.pop(stream_id, None)

    def session_for(self, stream_id: str) -> int | None:
        """Return the open session recording this stream, if any."""
        with self._lock:
            return self._sessions.get(stream_id)

    def observe(self, frame: SensorFrame, prediction: PredictionResult | None) -> None:
        """Record a frame against its session. Never raises into ingestion."""
        try:
            self._observe(frame, prediction)
        except Exception:  # noqa: BLE001 - storage must never break acquisition
            LOGGER.exception("recorder_failed", extra={"context": {"stream": frame.stream_id}})

    def _observe(self, frame: SensorFrame, prediction: PredictionResult | None) -> None:
        """Append a throttled track point and raise alerts when thresholds are crossed."""
        with self._lock:
            session_id = self._sessions.get(frame.stream_id)
            if session_id is None:
                return
            now = frame.timestamp
            self._last_seen[frame.stream_id] = now
            due = now - self._last_point.get(frame.stream_id, 0.0) >= POINT_INTERVAL_SECONDS
            if due:
                self._last_point[frame.stream_id] = now
            alert_due = now - self._last_alert.get(frame.stream_id, 0.0) >= ALERT_COOLDOWN_SECONDS

        if due:
            self.db.add_point(
                session_id,
                {
                    "ts": now,
                    "risk_index": prediction.overall_risk_index if prediction else None,
                    "risk_level": prediction.risk_level if prediction else None,
                    # SensorFrame carries m/s; the interface shows km/h.
                    "speed_kmh": frame.vehicle_speed * 3.6
                    if frame.vehicle_speed is not None
                    else None,
                    "rpm": frame.rpm,
                    "engine_load": frame.engine_load,
                    "throttle": frame.throttle_position,
                    "heart_rate": frame.heart_rate,
                    "latitude": frame.gps_latitude,
                    "longitude": frame.gps_longitude,
                },
            )

        if prediction is None or prediction.overall_risk_index is None or not alert_due:
            return
        thresholds = self.db.get_settings()
        if prediction.overall_risk_index < thresholds["risk_high_threshold"]:
            return
        session = self.db.get_session(session_id)
        if session is None:
            return
        with self._lock:
            self._last_alert[frame.stream_id] = now
        self.db.create_alert(
            session["user_id"],
            "high_risk",
            session_id=session_id,
            risk_index=prediction.overall_risk_index,
            latitude=frame.gps_latitude,
            longitude=frame.gps_longitude,
        )

    def report_collision(
        self, stream_id: str, latitude: float | None, longitude: float | None
    ) -> dict[str, Any] | None:
        """Raise a pending collision alert the driver may still cancel (FR10, FR11)."""
        session_id = self.session_for(stream_id)
        if session_id is None:
            return None
        session = self.db.get_session(session_id)
        if session is None:
            return None
        return self.db.create_alert(
            session["user_id"],
            "collision",
            session_id=session_id,
            latitude=latitude,
            longitude=longitude,
            status="pending",
        )

    def summarise(self, session_id: int) -> dict[str, Any]:
        """Compute the stored session summary from its track points (FR13.7)."""
        points = self.db.session_points(session_id)
        session = self.db.get_session(session_id)
        if session is None:
            return {}
        speeds = [p["speed_kmh"] for p in points if p["speed_kmh"] is not None]
        risks = [p["risk_index"] for p in points if p["risk_index"] is not None]
        hearts = [p["heart_rate"] for p in points if p["heart_rate"] is not None]
        started = session["started_at"]
        ended = session["ended_at"] or (points[-1]["ts"] if points else started)
        levels: dict[str, int] = {}
        for point in points:
            if point["risk_level"]:
                levels[point["risk_level"]] = levels.get(point["risk_level"], 0) + 1
        # Points are ~1 s apart, so counting them approximates seconds.
        idling = sum(1 for p in points if p["speed_kmh"] is not None and p["speed_kmh"] < 1)
        distance_km = sum(speeds) / 3600.0 if speeds else 0.0
        gaps = self.db.session_gaps(session_id)
        tolerance = self.db.get_settings()["session_gap_tolerance_seconds"]
        longest_gap = max(
            ((g["ended_at"] or ended) - g["started_at"] for g in gaps), default=0.0
        )
        return {
            "duration_seconds": round(max(ended - started, 0.0), 1),
            "distance_km": round(distance_km, 2),
            "max_speed_kmh": round(max(speeds), 1) if speeds else None,
            "mean_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
            "idling_seconds": idling,
            "mean_risk_index": round(sum(risks) / len(risks), 3) if risks else None,
            "max_risk_index": round(max(risks), 3) if risks else None,
            "risk_level_seconds": levels,
            "mean_heart_rate": round(sum(hearts) / len(hearts), 1) if hearts else None,
            "points": len(points),
            "gap_count": len(gaps),
            "longest_gap_seconds": round(longest_gap, 1),
            "complete": longest_gap <= tolerance,
            "alerts": len(self.db.list_alerts(user_id=session["user_id"], limit=1000)),
        }

    def finish(self, session_id: int, stream_id: str, reason: str) -> dict[str, Any]:
        """Close a session, store its summary and flag it incomplete if gaps were long."""
        summary = self.summarise(session_id)
        self.db.save_summary(session_id, summary)
        self.db.end_session(session_id, reason, complete=bool(summary.get("complete", True)))
        self.release(stream_id)
        return summary

    def stale_streams(self, timeout_seconds: float) -> list[str]:
        """Return streams that have not produced a frame within the timeout (FR13.6)."""
        now = time.time()
        with self._lock:
            return [
                stream
                for stream, seen in self._last_seen.items()
                if now - seen > timeout_seconds
            ]
