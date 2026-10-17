"""Actual-observation freshness and validation for dashboard ingestion."""

import time

from fastapi.testclient import TestClient

from accident_risk.api.main import create_app
from accident_risk.inference.engine import InferenceEngine


def test_empty_partial_stale_and_zero():
    client = TestClient(create_app(InferenceEngine()))
    empty = client.get("/api/v1/predict/live").json()
    assert empty["status"] == "waiting_for_hardware_stream"
    assert empty["heart"] is None and empty["risk_score"] is None
    now = time.time()
    packet = dict(
        timestamp=now,
        source="test",
        stream_id="hardware",
        vehicle_speed=0,
        heart_rate=82,
        sensor_timestamps={"heart_rate": now - 10},
    )
    assert client.post("/ingest", json=packet).status_code == 200
    data = client.get("/api/v1/predict/live").json()
    assert data["speed"] == 0 and data["heart"] == 82
    assert data["location"]["latitude"] is None and data["rpm"] is None
    assert data["risk_score"] is None
    # Re-sending an old measurement must not renew it.
    stale = dict(
        timestamp=now + 0.01,
        stream_id="old",
        source="test",
        heart_rate=82,
        sensor_timestamps={"heart_rate": now - 40},
    )
    assert client.post("/ingest", json=stale).status_code == 200
    data = client.get("/api/v1/predict/live?stream_id=old").json()
    assert data["heart"] is None and data["status"] == "stale"
    assert client.get("/api/v1/predict/live").status_code == 400


def test_invalid_packets_and_reset():
    client = TestClient(create_app(InferenceEngine()))
    packet = dict(timestamp=time.time(), source="test", stream_id="hardware")
    for update in (
        {"spo2": 110},
        {"gps_latitude": 200},
        {"vehicle_speed": -1},
        {"timestamp": time.time() + 100},
        {"speed": 40},
    ):
        assert client.post("/ingest", json=packet | update).status_code == 422
    assert client.post("/ingest", json=packet | {"vehicle_speed": 10}).status_code == 200
    assert client.get("/api/v1/predict/live").json()["speed"] == 36
    client.post("/session/stop")
    assert client.get("/api/v1/predict/live").json()["speed"] is None
    client.post("/session/start")
    assert client.get("/api/v1/predict/live").json()["status"] == "waiting_for_hardware_stream"
