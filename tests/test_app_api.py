"""Contract tests for the product API: login, role gating and the session flow."""

import pytest
from fastapi.testclient import TestClient

from accident_risk.api.main import create_app
from accident_risk.inference.engine import InferenceEngine


@pytest.fixture
def app(tmp_path, models):
    return create_app(InferenceEngine(*models), database_path=tmp_path / "app.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def sign_in(client, phone):
    """Complete the two-step phone login and return an auth header."""
    requested = client.post("/app/api/auth/request-code", json={"phone": phone}).json()
    verified = client.post(
        "/app/api/auth/verify", json={"phone": phone, "code": requested["code"]}
    )
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['token']}"}


@pytest.fixture
def people(app):
    database = app.state.db
    database.create_user("+971501110001", "Driver One", "driver")
    database.create_user("+971501110002", "Manager One", "account_manager")
    database.create_user("+971501110003", "Admin One", "administrator")
    return database


def test_interface_is_served(client):
    page = client.get("/app")
    assert page.status_code == 200
    assert "RoadSense" in page.text
    assert client.get("/static/app/app.js").status_code == 200
    assert client.get("/static/app/app.css").status_code == 200


def test_original_dashboard_still_works(client):
    """The research page and ingestion API must be unaffected by the new routes."""
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["status"] == "ok"


def test_everything_requires_authentication(client, people):
    for path in ("/app/api/me", "/app/api/sessions", "/app/api/live", "/app/api/users"):
        assert client.get(path).status_code == 401, path


def test_wrong_code_is_refused(client, people):
    client.post("/app/api/auth/request-code", json={"phone": "+971501110001"})
    response = client.post(
        "/app/api/auth/verify", json={"phone": "+971501110001", "code": "000000"}
    )
    assert response.status_code == 401


def test_driver_cannot_reach_manager_or_admin_routes(client, people):
    headers = sign_in(client, "+971501110001")
    assert client.get("/app/api/me", headers=headers).json()["role"] == "driver"
    assert client.get("/app/api/live", headers=headers).status_code == 403
    assert client.get("/app/api/drivers", headers=headers).status_code == 403
    assert client.get("/app/api/users", headers=headers).status_code == 403
    assert client.put(
        "/app/api/settings", headers=headers, json={"values": {"risk_high_threshold": 0.6}}
    ).status_code == 403


def test_manager_cannot_reach_admin_routes(client, people):
    headers = sign_in(client, "+971501110002")
    assert client.get("/app/api/live", headers=headers).status_code == 200
    assert client.get("/app/api/drivers", headers=headers).status_code == 200
    assert client.get("/app/api/users", headers=headers).status_code == 403
    assert client.post(
        "/app/api/devices", headers=headers, json={"kind": "adapter", "identifier": "X"}
    ).status_code == 403


def test_administrator_inherits_manager_routes(client, people):
    headers = sign_in(client, "+971501110003")
    for path in ("/app/api/live", "/app/api/drivers", "/app/api/devices", "/app/api/users"):
        assert client.get(path, headers=headers).status_code == 200, path


def test_session_lifecycle_and_ingestion(client, people):
    headers = sign_in(client, "+971501110001")
    started = client.post("/app/api/driver/session/start", headers=headers, json={})
    assert started.status_code == 200
    stream_id = started.json()["stream_id"]

    # A second session for the same driver is refused.
    assert client.post(
        "/app/api/driver/session/start", headers=headers, json={}
    ).status_code == 409

    for step in range(6):
        frame = {
            "timestamp": 1_000_000 + step,
            "source": "synthetic_test",
            "stream_id": stream_id,
            "vehicle_speed": 10 + step,
            "accel_x": step,
            "accel_y": 0,
            "accel_z": 9.8,
            "heart_rate": 70,
            "physiology_signal_quality": 1,
            "gps_latitude": 25.3,
            "gps_longitude": 55.4,
        }
        assert client.post("/ingest", json=frame).status_code == 200

    state = client.get("/app/api/driver/state", headers=headers).json()
    assert state["session"]["stream_id"] == stream_id

    stopped = client.post("/app/api/driver/session/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["summary"]["points"] > 0

    detail = client.get(
        f"/app/api/sessions/{started.json()['id']}", headers=headers
    ).json()
    assert len(detail["points"]) > 0
    assert client.post("/app/api/driver/session/stop", headers=headers).status_code == 404


def test_drivers_only_see_their_own_sessions(client, people):
    driver = sign_in(client, "+971501110001")
    manager = sign_in(client, "+971501110002")
    created = client.post("/app/api/driver/session/start", headers=driver, json={}).json()

    other = people.create_user("+971501110009", "Driver Two", "driver")
    hidden = people.start_session(other["id"], "other-stream")

    assert [s["id"] for s in client.get("/app/api/sessions", headers=driver).json()] == [
        created["id"]
    ]
    assert client.get(
        f"/app/api/sessions/{hidden['id']}", headers=driver
    ).status_code == 403
    assert client.get(f"/app/api/sessions/{hidden['id']}", headers=manager).status_code == 200


def test_manager_enrols_a_driver_with_a_kit(client, people):
    admin = sign_in(client, "+971501110003")
    adapter = client.post(
        "/app/api/devices", headers=admin, json={"kind": "adapter", "identifier": "ELM-010"}
    ).json()

    manager = sign_in(client, "+971501110002")
    enrolled = client.post(
        "/app/api/drivers",
        headers=manager,
        json={"phone": "+971501110020", "name": "New Driver", "adapter_id": adapter["id"]},
    )
    assert enrolled.status_code == 201
    assert enrolled.json()["kit"][0]["identifier"] == "ELM-010"
    # The new driver can sign in immediately.
    assert client.get(
        "/app/api/me", headers=sign_in(client, "+971501110020")
    ).json()["role"] == "driver"


def test_threshold_updates_are_validated(client, people):
    headers = sign_in(client, "+971501110003")
    ok = client.put(
        "/app/api/settings", headers=headers, json={"values": {"risk_high_threshold": 0.6}}
    )
    assert ok.status_code == 200 and ok.json()["risk_high_threshold"] == 0.6
    bad = client.put(
        "/app/api/settings", headers=headers, json={"values": {"risk_medium_threshold": 0.95}}
    )
    assert bad.status_code == 422
    assert client.get("/app/api/settings", headers=headers).json()["risk_high_threshold"] == 0.6


def test_administrator_cannot_lock_themselves_out(client, people):
    headers = sign_in(client, "+971501110003")
    me = client.get("/app/api/me", headers=headers).json()
    assert client.patch(
        f"/app/api/users/{me['id']}", headers=headers, json={"active": False}
    ).status_code == 409
    assert client.patch(
        f"/app/api/users/{me['id']}", headers=headers, json={"role": "driver"}
    ).status_code == 409


def test_collision_alert_can_be_cancelled_by_the_driver(client, people):
    headers = sign_in(client, "+971501110001")
    started = client.post("/app/api/driver/session/start", headers=headers, json={}).json()
    raised = client.post(
        "/app/api/driver/collision",
        headers=headers,
        json={"stream_id": started["stream_id"], "latitude": 25.3, "longitude": 55.4},
    )
    assert raised.status_code == 200 and raised.json()["status"] == "pending"

    state = client.get("/app/api/driver/state", headers=headers).json()
    assert state["pending_collision"]["id"] == raised.json()["id"]

    cancelled = client.post(f"/app/api/alerts/{raised.json()['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_one_driver_cannot_act_on_another_drivers_alert(client, people):
    first = sign_in(client, "+971501110001")
    started = client.post("/app/api/driver/session/start", headers=first, json={}).json()
    alert = client.post(
        "/app/api/driver/collision", headers=first, json={"stream_id": started["stream_id"]}
    ).json()

    people.create_user("+971501110030", "Driver Three", "driver")
    second = sign_in(client, "+971501110030")
    assert client.post(
        f"/app/api/alerts/{alert['id']}/cancel", headers=second
    ).status_code == 403
    assert client.get("/app/api/alerts", headers=second).json() == []


def test_logout_invalidates_the_token(client, people):
    headers = sign_in(client, "+971501110001")
    assert client.post("/app/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/app/api/me", headers=headers).status_code == 401
