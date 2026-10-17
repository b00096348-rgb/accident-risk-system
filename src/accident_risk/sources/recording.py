"""Append real observations to per-run CSV files independently of API availability."""

import csv
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from accident_risk.schemas import SENSOR_FIELDS, SensorFrame

CSV_FIELDS = ("timestamp", "stream_id", "source", *SENSOR_FIELDS, "sensor_timestamps")


class CSVRecorder:
    """Create a unique session folder and flush each measurement row to disk.

    Each source has its own CSV with canonical SensorFrame names and SI units.
    Empty cells mean missing observations. Sensor times are a JSON object so
    cached readings retain their original measurement times for later replay.
    """

    def __init__(self, directory: str | Path) -> None:
        root = Path(directory).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        prefix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-")
        self.directory = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
        self._files: set[str] = set()
        self._lock = Lock()

    def write(self, frame: SensorFrame) -> Path:
        """Append one validated packet, closing the file before returning."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", frame.source):
            raise ValueError("Recording source must be a simple file-safe name")
        row = frame.model_dump()
        row["sensor_timestamps"] = json.dumps(frame.sensor_timestamps, sort_keys=True)
        path = self.directory / f"{frame.source}.csv"
        with self._lock:
            new = frame.source not in self._files
            with path.open("x" if new else "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                if new:
                    writer.writeheader()
                    self._files.add(frame.source)
                writer.writerow(row)
                handle.flush()
        return path
