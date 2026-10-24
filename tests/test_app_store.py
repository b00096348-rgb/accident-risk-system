"""Store and authentication tests. Standard library only; no FastAPI needed."""

import time

import pytest

from accident_risk.api.auth import AuthError, AuthService
from accident_risk.api.db import Database, normalise_phone, role_allows


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "store.sqlite3")


@pytest.fixture
def auth(db):
    return AuthService(db)


@pytest.fixture
def people(db):
    return {
        "driver": db.create_user("+971501110001", "Driver One", "driver"),
        "manager": db.create_user("+971501110002", "Manager One", "account_manager"),
        "admin": db.create_user("+971501110003", "Admin One", "administrator"),
    }


# ----------------------------------------------------------------- basics


@pytest.mark.parametrize(
    "raw,expected",
    [("+971 50 123 4567", "+971501234567"), ("971501234567", "+971501234567")],
)
def test_phone_normalises(raw, expected):
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "abc", "+" + "9" * 20])
def test_phone_rejects_nonsense(raw):
    with pytest.raises(ValueError):
        normalise_phone(raw)


def test_role_hierarchy_is_ordered():
    assert role_allows("administrator", "account_manager")
    assert role_allows("account_manager", "driver")
    assert not role_allows("driver", "account_manager")
    assert not role_allows("account_manager", "administrator")
    assert not role_allows("unknown", "driver")


def test_duplicate_phone_is_rejected(db, people):
    with pytest.raises(ValueError):
        db.create_user("+971 50 111 0001", "Someone Else", "driver")


# ------------------------------------------------------------------- auth


def test_login_round_trip(auth, people):
    requested = auth.request_code("+971501110001")
    result = auth.verify_code("+971501110001", requested["code"])
    assert auth.resolve(result["token"]).role == "driver"


def test_unregistered_number_does_not_leak(auth, people):
    assert "code" not in auth.request_code("+971509999999")


def test_code_is_single_use(auth, people):
    requested = auth.request_code("+971501110001")
    auth.verify_code("+971501110001", requested["code"])
    with pytest.raises(AuthError):
        auth.verify_code("+971501110001", requested["code"])


def test_lockout_after_repeated_failures(auth, people):
    """Regression: the attempt counter must survive the raise that follows it."""
    auth.request_code("+971501110001")
    for _ in range(5):
        with pytest.raises(AuthError):
            auth.verify_code("+971501110001", "999999")
    with pytest.raises(AuthError) as excinfo:
        auth.verify_code("+971501110001", "999999")
    assert excinfo.value.status == 429


def test_expired_code_is_refused(auth, db, people):
    requested = auth.request_code("+971501110001")
    with db.connect() as connection:
        connection.execute(
            "UPDATE auth_codes SET expires_at = ? WHERE phone = ?",
            (time.time() - 1, "+971501110001"),
        )
    with pytest.raises(AuthError):
        auth.verify_code("+971501110001", requested["code"])


def test_deactivating_a_user_invalidates_their_token(auth, db, people):
    requested = auth.request_code("+971501110001")
    token = auth.verify_code("+971501110001", requested["code"])["token"]
    db.update_user(people["driver"]["id"], active=False)
    with pytest.raises(AuthError) as excinfo:
        auth.resolve(token)
    assert excinfo.value.status == 403


def test_secrets_are_not_stored_in_clear_text(auth, db, people):
    requested = auth.request_code("+971501110001")
    token = auth.verify_code("+971501110001", requested["code"])["token"]
    blob = db.path.read_bytes()
    assert token.encode() not in blob
    assert requested["code"].encode() not in blob


# ---------------------------------------------------------------- devices


def test_kit_assignment_activates_and_clears(db, people):
    adapter = db.create_device("adapter", "ELM-001")
    db.create_device("wearable", "WHOOP-001")
    assigned = db.update_device(adapter["id"], assigned_to=people["driver"]["id"])
    assert assigned["status"] == "active"
    assert len(db.kit_for_user(people["driver"]["id"])) == 1
    db.update_device(adapter["id"], clear_assignment=True, status="spare")
    assert db.kit_for_user(people["driver"]["id"]) == []


def test_device_identifier_is_unique(db):
    db.create_device("adapter", "ELM-001")
    with pytest.raises(ValueError):
        db.create_device("adapter", "ELM-001")


# --------------------------------------------------------------- sessions


def test_session_lifecycle(db, people):
    driver = people["driver"]["id"]
    session = db.start_session(driver, "stream-a")
    assert db.active_session(driver)["id"] == session["id"]
    db.add_point(session["id"], {"ts": 100.0, "speed_kmh": 40, "risk_index": 0.2})
    db.save_summary(session["id"], {"distance_km": 1.2})
    db.end_session(session["id"], "driver_stopped")
    stored = db.get_session(session["id"])
    assert stored["ended_at"] is not None
    assert stored["summary"]["distance_km"] == 1.2
    assert db.active_session(driver) is None


def test_stream_id_cannot_be_reused(db, people):
    db.start_session(people["driver"]["id"], "stream-a")
    with pytest.raises(ValueError):
        db.start_session(people["manager"]["id"], "stream-a")


def test_sessions_scope_to_one_driver(db, people):
    db.start_session(people["driver"]["id"], "stream-a")
    db.start_session(people["manager"]["id"], "stream-b")
    assert len(db.list_sessions(user_id=people["driver"]["id"])) == 1
    assert len(db.list_sessions()) == 2


def test_gaps_are_recorded(db, people):
    session = db.start_session(people["driver"]["id"], "stream-a")
    gap = db.record_gap(session["id"], "wearable", 100.0)
    db.close_gap(gap, 118.0)
    recorded = db.session_gaps(session["id"])
    assert len(recorded) == 1
    assert recorded[0]["ended_at"] - recorded[0]["started_at"] == 18.0


# --------------------------------------------------------------- settings


def test_thresholds_must_stay_ordered(db):
    db.update_settings({"risk_high_threshold": 0.66})
    assert db.get_settings()["risk_high_threshold"] == 0.66
    for bad in (
        {"risk_medium_threshold": 0.9},
        {"risk_critical_threshold": 0.5},
        {"risk_high_threshold": 1.5},
        {"session_gap_tolerance_seconds": 0},
    ):
        with pytest.raises(ValueError):
            db.update_settings(bad)
    # A rejected update must not partially apply.
    assert db.get_settings()["risk_high_threshold"] == 0.66


def test_unknown_setting_is_rejected(db):
    with pytest.raises(ValueError):
        db.update_settings({"not_a_setting": 1})
