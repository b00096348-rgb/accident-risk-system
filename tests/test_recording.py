"""Runtime recording preserves observations, including when the API is offline."""

import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor

from accident_risk.schemas import SensorFrame
from accident_risk.sources.hardware import Publisher
from accident_risk.sources.recording import CSV_FIELDS, CSVRecorder


def test_rows_are_readable_immediately_and_missing_is_not_zero(tmp_path):
    recorder = CSVRecorder(tmp_path)
    now = time.time()
    frame = SensorFrame(
        timestamp=now,
        source="obd",
        stream_id="trip",
        vehicle_speed=0,
        sensor_timestamps={"vehicle_speed": now - 1},
    )
    path = recorder.write(frame)
    with path.open() as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert reader.fieldnames == list(CSV_FIELDS)
    assert len(rows) == 1
    assert float(rows[0]["vehicle_speed"]) == 0
    assert rows[0]["heart_rate"] == "" and rows[0]["rpm"] == ""
    assert json.loads(rows[0]["sensor_timestamps"]) == {"vehicle_speed": now - 1}
    recorder.write(frame)
    with path.open() as handle:
        assert len(list(csv.DictReader(handle))) == 2
    second = CSVRecorder(tmp_path)
    assert second.directory != recorder.directory
    assert path.exists()


def test_api_failure_does_not_lose_recording_and_bad_data_is_not_saved(tmp_path, monkeypatch):
    from accident_risk.sources import hardware

    def offline(*args, **kwargs):
        raise ConnectionError("dashboard offline")

    monkeypatch.setattr(hardware, "urlopen", offline)
    recorder = CSVRecorder(tmp_path)
    publisher = Publisher("http://127.0.0.1:8000/ingest", "trip", recorder)
    assert not publisher.send("wearable", {"heart_rate": 80}, time.time())
    path = recorder.directory / "wearable.csv"
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1 and float(rows[0]["heart_rate"]) == 80
    assert not publisher.send("wearable", {"heart_rate": 999}, time.time())
    with path.open() as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_parallel_sources_and_single_header(tmp_path):
    recorder = CSVRecorder(tmp_path)
    frames = [
        SensorFrame(timestamp=time.time(), stream_id="trip", source="obd", rpm=i) for i in range(40)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(recorder.write, frames))
    with (recorder.directory / "obd.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 40
    assert {float(row["rpm"]) for row in rows} == set(range(40))
    recorder.write(
        SensorFrame(timestamp=time.time(), stream_id="trip", source="phone", gps_latitude=0)
    )
    assert (recorder.directory / "phone.csv").exists()


def test_recording_error_is_reported_without_stopping_delivery(monkeypatch, caplog):
    from accident_risk.sources import hardware

    class BrokenRecorder:
        def write(self, frame):
            raise OSError("disk full")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"null"

    monkeypatch.setattr(hardware, "urlopen", lambda *a, **k: Response())
    publisher = Publisher("http://127.0.0.1:8000/ingest", "trip", BrokenRecorder())
    assert publisher.send("obd", {"rpm": 1000}, time.time())
    assert "CSV RECORDING FAILED" in caplog.text
