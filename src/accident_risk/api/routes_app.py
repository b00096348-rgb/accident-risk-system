"""Product API behind the operator and driver interface.

Mounted at ``/app/api`` alongside the existing research ingestion endpoints,
which are left untouched. Every route here requires authentication; role gating
follows the hierarchy in :mod:`accident_risk.api.db` (FR2).
"""

import time
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from accident_risk.api.auth import AuthError, AuthService, Principal, bearer_from_header
from accident_risk.api.db import Database, role_allows
from accident_risk.api.recorder import SessionRecorder
from accident_risk.inference.engine import InferenceEngine

# A session with no frames for this long is closed automatically (FR13.6).
SESSION_TIMEOUT_SECONDS = 180.0


class CodeRequest(BaseModel):
    """Step one of login: identify the user by phone number."""

    phone: str = Field(min_length=5, max_length=20)


class VerifyRequest(BaseModel):
    """Step two of login: exchange the single-use code for a token."""

    phone: str = Field(min_length=5, max_length=20)
    code: str = Field(min_length=4, max_length=10)


class StartSessionRequest(BaseModel):
    """Open a driving session; the stream_id links it to ingested frames."""

    stream_id: str | None = Field(None, min_length=1, max_length=120)


class CollisionRequest(BaseModel):
    """Report a probable collision detected on the device."""

    stream_id: str
    latitude: float | None = None
    longitude: float | None = None


class DriverRequest(BaseModel):
    """Enrol a driver and optionally assign their kit (FR16)."""

    phone: str = Field(min_length=5, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    adapter_id: int | None = None
    wearable_id: int | None = None


class UserRequest(BaseModel):
    """Create a user of any role (FR19)."""

    phone: str = Field(min_length=5, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    role: str


class UserPatch(BaseModel):
    """Change a user's name, role or active flag."""

    name: str | None = Field(None, min_length=1, max_length=120)
    role: str | None = None
    active: bool | None = None


class DeviceRequest(BaseModel):
    """Add a device to the inventory (FR17)."""

    kind: str
    identifier: str = Field(min_length=1, max_length=120)
    note: str = ""


class DevicePatch(BaseModel):
    """Change a device's status or assignment."""

    status: str | None = None
    assigned_to: int | None = None
    clear_assignment: bool = False


class SettingsRequest(BaseModel):
    """Update configurable risk thresholds (FR18)."""

    values: dict[str, float]


def risk_level_for(index: float | None, thresholds: dict[str, float]) -> str | None:
    """Map a risk index onto the administrator-configured bands."""
    if index is None:
        return None
    if index >= thresholds["risk_critical_threshold"]:
        return "CRITICAL"
    if index >= thresholds["risk_high_threshold"]:
        return "HIGH"
    if index >= thresholds["risk_medium_threshold"]:
        return "MEDIUM"
    return "LOW"


def create_router(
    database: Database,
    auth: AuthService,
    recorder: SessionRecorder,
    engine: InferenceEngine,
) -> APIRouter:
    """Build the product API over the given store, auth service and engine."""
    router = APIRouter(prefix="/app/api", tags=["application"])

    # ------------------------------------------------------------ security

    def get_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        """Resolve the bearer token on the request into a principal."""
        try:
            return auth.resolve(bearer_from_header(authorization))
        except AuthError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    def require(minimum: str):
        """Build a dependency that admits only roles at or above `minimum`."""

        def dependency(
            principal: Annotated[Principal, Depends(get_principal)],
        ) -> Principal:
            if not role_allows(principal.role, minimum):
                raise HTTPException(403, "Your role does not permit this action")
            return principal

        return dependency

    any_user = Depends(get_principal)
    manager = Depends(require("account_manager"))
    admin = Depends(require("administrator"))

    def visible_session(session_id: int, principal: Principal) -> dict[str, Any]:
        """Fetch a session the principal is allowed to see (FR13.8)."""
        session = database.get_session(session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if not role_allows(principal.role, "account_manager") and (
            session["user_id"] != principal.user_id
        ):
            raise HTTPException(403, "You can only review your own sessions")
        database.log_access(principal.user_id, principal.role, f"session:{session_id}")
        return session

    def close_stale_sessions() -> None:
        """Auto-close sessions whose stream has gone silent past the timeout."""
        for stream_id in recorder.stale_streams(SESSION_TIMEOUT_SECONDS):
            session_id = recorder.session_for(stream_id)
            if session_id is not None:
                recorder.finish(session_id, stream_id, "timeout")

    # ---------------------------------------------------------------- auth

    @router.post("/auth/request-code")
    def request_code(payload: CodeRequest) -> dict[str, Any]:
        """Send a single-use login code to a registered phone number (FR1)."""
        try:
            return auth.request_code(payload.phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/auth/verify")
    def verify(payload: VerifyRequest) -> dict[str, Any]:
        """Exchange a login code for a bearer token."""
        try:
            return auth.verify_code(payload.phone, payload.code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except AuthError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @router.post("/auth/logout")
    def logout(authorization: Annotated[str | None, Header()] = None) -> dict[str, bool]:
        """Invalidate the caller's token."""
        token = bearer_from_header(authorization)
        if token:
            auth.revoke(token)
        return {"ok": True}

    @router.get("/me")
    def me(principal: Principal = any_user) -> dict[str, Any]:
        """Return the signed-in user and the kit assigned to them."""
        return {
            "id": principal.user_id,
            "name": principal.name,
            "phone": principal.phone,
            "role": principal.role,
            "kit": database.kit_for_user(principal.user_id),
            "thresholds": database.get_settings(),
        }

    # -------------------------------------------------------------- driver

    @router.get("/driver/state")
    def driver_state(principal: Principal = any_user) -> dict[str, Any]:
        """Connection status, running session and latest risk for the caller."""
        close_stale_sessions()
        session = database.active_session(principal.user_id)
        thresholds = database.get_settings()
        snapshot: dict[str, Any] = {}
        prediction = None
        ages: dict[str, float] = {}
        if session is not None:
            stream_id = session["stream_id"]
            now = time.time()
            synchronizer = engine.pipeline.synchronizer
            if stream_id in synchronizer.values:
                snapshot = synchronizer.snapshot(stream_id, now).model_dump()
                ages = synchronizer.ages(stream_id, now)
            prediction = engine.latest.get(stream_id)
        index = prediction.overall_risk_index if prediction else None
        return {
            "session": session,
            "vehicle_connected": snapshot.get("vehicle_speed") is not None
            or snapshot.get("rpm") is not None,
            "wearable_connected": snapshot.get("heart_rate") is not None,
            "uploading": bool(session) and bool(snapshot),
            "risk_index": index,
            "risk_level": risk_level_for(index, thresholds),
            "speed_kmh": snapshot.get("vehicle_speed") * 3.6
            if snapshot.get("vehicle_speed") is not None
            else None,
            "rpm": snapshot.get("rpm"),
            "engine_load": snapshot.get("engine_load"),
            "throttle": snapshot.get("throttle_position"),
            "heart_rate": snapshot.get("heart_rate"),
            "latitude": snapshot.get("gps_latitude"),
            "longitude": snapshot.get("gps_longitude"),
            "sensor_age_seconds": ages,
            "pending_collision": next(
                (
                    a
                    for a in database.list_alerts(user_id=principal.user_id, kind="collision",
                                                  limit=5)
                    if a["status"] == "pending"
                ),
                None,
            ),
            "models_loaded": len(engine.models),
        }

    @router.post("/driver/session/start")
    def start_session(
        payload: StartSessionRequest, principal: Principal = any_user
    ) -> dict[str, Any]:
        """Open a driving session for the caller (FR13.1)."""
        if database.active_session(principal.user_id) is not None:
            raise HTTPException(409, "A session is already running")
        stream_id = payload.stream_id or f"u{principal.user_id}-{uuid4().hex[:12]}"
        try:
            session = database.start_session(principal.user_id, stream_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        recorder.register(stream_id, session["id"])
        return session

    @router.post("/driver/session/stop")
    def stop_session(principal: Principal = any_user) -> dict[str, Any]:
        """Close the caller's running session and store its summary (FR13.6, FR13.7)."""
        session = database.active_session(principal.user_id)
        if session is None:
            raise HTTPException(404, "No session is running")
        summary = recorder.finish(session["id"], session["stream_id"], "driver_stopped")
        return {"session": database.get_session(session["id"]), "summary": summary}

    @router.post("/driver/collision")
    def report_collision(
        payload: CollisionRequest, principal: Principal = any_user
    ) -> dict[str, Any]:
        """Raise a cancellable collision alert for the caller's session."""
        session = database.active_session(principal.user_id)
        if session is None or session["stream_id"] != payload.stream_id:
            raise HTTPException(409, "That stream has no running session")
        alert = recorder.report_collision(
            payload.stream_id, payload.latitude, payload.longitude
        )
        if alert is None:
            raise HTTPException(409, "That stream is not being recorded")
        return alert

    # -------------------------------------------------------------- alerts

    @router.get("/alerts")
    def alerts(
        principal: Principal = any_user,
        kind: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """List alerts. Drivers see only their own; managers see every driver's."""
        scope = None if role_allows(principal.role, "account_manager") else principal.user_id
        database.log_access(principal.user_id, principal.role, "alerts")
        return database.list_alerts(user_id=scope, kind=kind, limit=limit)

    @router.post("/alerts/{alert_id}/{action}")
    def act_on_alert(
        alert_id: int, action: str, principal: Principal = any_user
    ) -> dict[str, Any]:
        """Confirm or cancel an alert (FR11). Drivers may act only on their own."""
        if action not in {"confirm", "cancel"}:
            raise HTTPException(404, "Unknown action")
        existing = next(
            (a for a in database.list_alerts(limit=500) if a["id"] == alert_id), None
        )
        if existing is None:
            raise HTTPException(404, "Alert not found")
        if not role_allows(principal.role, "account_manager") and (
            existing["user_id"] != principal.user_id
        ):
            raise HTTPException(403, "You can only act on your own alerts")
        status = "confirmed" if action == "confirm" else "cancelled"
        updated = database.set_alert_status(alert_id, status, principal.user_id)
        if updated is None:
            raise HTTPException(404, "Alert not found")
        return updated

    # ------------------------------------------------------------ sessions

    @router.get("/sessions")
    def sessions(
        principal: Principal = any_user,
        driver_id: int | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """List sessions. Drivers are restricted to their own (FR13.8, FR14)."""
        if role_allows(principal.role, "account_manager"):
            scope = driver_id
        else:
            scope = principal.user_id
        database.log_access(principal.user_id, principal.role, "sessions")
        return database.list_sessions(user_id=scope, limit=limit)

    @router.get("/sessions/{session_id}")
    def session_detail(session_id: int, principal: Principal = any_user) -> dict[str, Any]:
        """Return one session with its track, gaps and summary."""
        session = visible_session(session_id, principal)
        return {
            "session": session,
            "points": database.session_points(session_id),
            "gaps": database.session_gaps(session_id),
        }

    # ---------------------------------------------------- live monitoring

    @router.get("/live")
    def live(principal: Principal = manager) -> dict[str, Any]:
        """Every active session with its latest position, speed and risk (FR15)."""
        close_stale_sessions()
        thresholds = database.get_settings()
        synchronizer = engine.pipeline.synchronizer
        now = time.time()
        drivers = []
        for session in database.list_sessions(limit=500):
            if session["ended_at"] is not None:
                continue
            stream_id = session["stream_id"]
            fields: dict[str, Any] = {}
            if stream_id in synchronizer.values:
                fields = synchronizer.snapshot(stream_id, now).model_dump()
            prediction = engine.latest.get(stream_id)
            index = prediction.overall_risk_index if prediction else None
            drivers.append(
                {
                    "session_id": session["id"],
                    "driver_id": session["user_id"],
                    "driver_name": session["driver_name"],
                    "stream_id": stream_id,
                    "started_at": session["started_at"],
                    "risk_index": index,
                    "risk_level": risk_level_for(index, thresholds),
                    "speed_kmh": fields.get("vehicle_speed") * 3.6
                    if fields.get("vehicle_speed") is not None
                    else None,
                    "heart_rate": fields.get("heart_rate"),
                    "latitude": fields.get("gps_latitude"),
                    "longitude": fields.get("gps_longitude"),
                    "last_seen": synchronizer.last.get(stream_id),
                    "receiving": bool(fields) and any(v is not None for v in fields.values()),
                }
            )
        database.log_access(principal.user_id, principal.role, "live")
        return {"drivers": drivers, "thresholds": thresholds, "server_time": now}

    # ------------------------------------------------- driver enrolment

    @router.get("/drivers")
    def list_drivers(principal: Principal = manager) -> list[dict[str, Any]]:
        """List enrolled drivers with their assigned kit."""
        drivers = database.list_users(role="driver")
        for driver in drivers:
            driver["kit"] = database.kit_for_user(driver["id"])
        return drivers

    @router.post("/drivers", status_code=201)
    def enrol_driver(payload: DriverRequest, principal: Principal = manager) -> dict[str, Any]:
        """Enrol a driver and assign their kit in one step (FR16)."""
        try:
            driver = database.create_user(payload.phone, payload.name, "driver")
            for device_id in (payload.adapter_id, payload.wearable_id):
                if device_id is not None:
                    database.update_device(device_id, assigned_to=driver["id"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        driver["kit"] = database.kit_for_user(driver["id"])
        return driver

    # ----------------------------------------------------- administration

    @router.get("/users")
    def list_users(principal: Principal = admin) -> list[dict[str, Any]]:
        """List every user account (FR19)."""
        return database.list_users()

    @router.post("/users", status_code=201)
    def create_user(payload: UserRequest, principal: Principal = admin) -> dict[str, Any]:
        """Create a user of any role (FR19)."""
        try:
            return database.create_user(payload.phone, payload.name, payload.role)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.patch("/users/{user_id}")
    def patch_user(
        user_id: int, payload: UserPatch, principal: Principal = admin
    ) -> dict[str, Any]:
        """Change a user's name, role or active flag."""
        if user_id == principal.user_id and payload.active is False:
            raise HTTPException(409, "You cannot deactivate your own account")
        if user_id == principal.user_id and payload.role not in (None, "administrator"):
            raise HTTPException(409, "You cannot remove your own administrator role")
        try:
            user = database.update_user(
                user_id, name=payload.name, role=payload.role, active=payload.active
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if user is None:
            raise HTTPException(404, "User not found")
        return user

    @router.get("/devices")
    def list_devices(principal: Principal = manager) -> list[dict[str, Any]]:
        """List the device inventory (FR17)."""
        return database.list_devices()

    @router.post("/devices", status_code=201)
    def create_device(payload: DeviceRequest, principal: Principal = admin) -> dict[str, Any]:
        """Add an adapter or wearable to the inventory (FR17)."""
        try:
            return database.create_device(payload.kind, payload.identifier, payload.note)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.patch("/devices/{device_id}")
    def patch_device(
        device_id: int, payload: DevicePatch, principal: Principal = admin
    ) -> dict[str, Any]:
        """Reassign a device or change its status (FR17)."""
        try:
            device = database.update_device(
                device_id,
                status=payload.status,
                assigned_to=payload.assigned_to,
                clear_assignment=payload.clear_assignment,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if device is None:
            raise HTTPException(404, "Device not found")
        return device

    @router.get("/settings")
    def get_settings(principal: Principal = manager) -> dict[str, float]:
        """Read the configurable risk thresholds (FR18)."""
        return database.get_settings()

    @router.put("/settings")
    def put_settings(payload: SettingsRequest, principal: Principal = admin) -> dict[str, float]:
        """Update the risk thresholds, rejecting out-of-order values (FR18)."""
        try:
            return database.update_settings(payload.values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/access-log")
    def access_log(
        principal: Principal = admin, limit: int = Query(100, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        """Return recent data access events (NFR11)."""
        return database.recent_access(limit)

    return router
