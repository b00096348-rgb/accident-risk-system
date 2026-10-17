"""In-process offline API contract tests with synthetic measurements."""

from fastapi.testclient import TestClient

from accident_risk.api.main import create_app
from accident_risk.inference.engine import InferenceEngine


def test_api(models):
    with TestClient(create_app(InferenceEngine(*models))) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "RoadSense" in dashboard.text
        demo_script = client.get("/static/app.js")
        assert demo_script.status_code == 200
        assert client.get("/static/dashboard.css").status_code == 200
        assert "live-tab" in dashboard.text and "demo-tab" in dashboard.text
        assert "actual-map" in dashboard.text
        assert "OpenStreetMap" in dashboard.text
        assert "Tire pressure" in dashboard.text
        assert "Motion & gyro" in dashboard.text
        assert "tire_pressure_fl" in demo_script.text
        assert client.get("/docs").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/prediction/latest").json() is None
        payload = {
            "timestamp": 100,
            "source": "synthetic",
            "stream_id": "trip",
            "vehicle_speed": 10,
        }
        assert client.post("/ingest", json=payload).json() is None
        p = client.post("/ingest", json=payload | {"timestamp": 101}).json()
        assert p["event_detection_score"] is not None
        assert client.get("/prediction/latest").json()["timestamp"] == 101
        assert client.post("/ingest", json=payload | {"spo2": 400}).status_code == 422
        assert client.post("/ingest", json=payload).status_code == 422
        result = client.post(
            "/ingest/batch", json={"frames": [payload | {"timestamp": 102}, payload]}
        ).json()
        assert [r["accepted"] for r in result["results"]] == [True, False]
        assert client.post("/session/stop").status_code == 200
        assert client.post("/ingest", json=payload).status_code == 409
        assert client.post("/session/start").status_code == 200
        assert client.get("/prediction/latest").json() is None
        demo_frame = payload | {
            "timestamp": 103,
            "gps_latitude": 25.1869,
            "gps_longitude": 55.2671,
            "gps_speed": 12.5,
            "gps_heading": 1.2,
            "steering_angle": 0.12,
            "tire_pressure_fl": 235,
            "tire_pressure_fr": 236,
            "tire_pressure_rl": 238,
            "tire_pressure_rr": 237,
            "heart_rate": 74,
            "spo2": 98,
        }
        assert client.post("/ingest", json=demo_frame).status_code == 200
        assert client.post("/reset").status_code == 200
