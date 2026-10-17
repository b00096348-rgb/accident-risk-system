"""Offline hardware-boundary tests; no radio or ECU required."""

import time

import pytest

from accident_risk.sources.hardware import parse_heart_rate, phone_values


def test_ble_flags_energy_and_truncation():
    # HR=80, two energy bytes, RR=1024 ticks (one second).
    assert parse_heart_rate(bytes([24, 80, 99, 0, 0, 4])) == (80, [1000.0])
    assert parse_heart_rate(bytes([1, 4, 1])) == (260, [])
    assert parse_heart_rate(bytes([4, 80])) == (None, [])
    for data in (b"", bytes([1, 80]), bytes([24, 80]), bytes([16, 80, 4])):
        with pytest.raises(ValueError):
            parse_heart_rate(data)


def test_phone_partial_zero_coordinates_and_original_time():
    now = time.time() - 3
    values, times = phone_values(
        {
            "payload": [
                {
                    "name": "location",
                    "time": int(now * 1e9),
                    "values": {"latitude": 0, "longitude": 0},
                },
                {"name": "gyroscope", "time": int(now * 1e9), "values": {"z": 0.2}},
            ]
        }
    )
    assert values == {"gps_latitude": 0, "gps_longitude": 0, "gyro_z": 0.2}
    assert times["gyro_z"] == pytest.approx(now)
    with pytest.raises(ValueError):
        phone_values({"payload": [{"name": "gyroscope", "values": {"z": 0.2}}]})


def test_phone_rejects_invalid_units_and_missing_timestamp():
    with pytest.raises(ValueError):
        phone_values({"latitude": 25})
    with pytest.raises(ValueError):
        phone_values(
            {
                "timestamp": time.time(),
                "stream_id": "hardware",
                "source": "phone",
                "gps_latitude": 200,
            }
        )


def test_obd_omits_failed_queries_and_converts_units(monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace

    from accident_risk.sources.hardware import obd_loop

    stop = threading.Event()
    conversions = []

    class Value:
        def to(self, unit):
            conversions.append(unit)
            return SimpleNamespace(magnitude=0)

    class Connection:
        def is_connected(self):
            return True

        def query(self, command):
            return SimpleNamespace(is_null=lambda: command != "speed", value=Value())

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "obd",
        SimpleNamespace(
            OBD=lambda **kwargs: Connection(),
            commands=SimpleNamespace(
                SPEED="speed", RPM="rpm", THROTTLE_POS="throttle", ENGINE_LOAD="load"
            ),
        ),
    )

    class Sink:
        def send(self, source, values, times):
            assert values == {"vehicle_speed": 0}
            assert set(times) == {"vehicle_speed"}
            stop.set()

    obd_loop(Sink(), "fake-port", stop)
    assert conversions == ["m/s"]


def test_publisher_posts_canonical_payload(monkeypatch):
    import json

    from accident_risk.sources import hardware

    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"null"

    def send(request, timeout):
        captured.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(hardware, "urlopen", send)
    publisher = hardware.Publisher("http://127.0.0.1:8000/ingest", "hardware")
    old = time.time() - 20
    assert publisher.send("wearable", {"heart_rate": 80, "spo2": None}, old)
    assert captured[0]["sensor_timestamps"] == {"heart_rate": old}
    assert captured[0]["stream_id"] == "hardware"
    assert not publisher.send("wearable", {"heart_rate": 999}, old)
    assert len(captured) == 1
