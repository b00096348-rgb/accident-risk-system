"""Local research ingestion API; a future Android/BLE gateway sends SensorFrame JSON."""

import logging
import time
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from accident_risk.api.auth import AuthService
from accident_risk.api.db import Database
from accident_risk.api.recorder import SessionRecorder
from accident_risk.api.routes_app import create_router
from accident_risk.inference.engine import InferenceEngine
from accident_risk.logging_utils import configure_logging
from accident_risk.models.fusion import RiskFusion
from accident_risk.models.physiology_model import PhysiologyModel
from accident_risk.models.vehicle_model import VehicleModel
from accident_risk.processing.map_context import MapContextResolver
from accident_risk.schemas import SENSOR_FIELDS, PredictionResult, SensorFrame
from accident_risk.settings import EnvironmentSettings, PipelineSettings, load_config
from accident_risk.sources.live_buffer import LiveBuffer


class Batch(BaseModel):
    """Bounded request size, with independent accepted/rejected results."""

    frames: list[SensorFrame] = Field(min_length=1, max_length=1000)


def bootstrap_administrator(database: Database, env: EnvironmentSettings) -> None:
    """Seed the first administrator so a fresh deployment can be signed into.

    Runs only when the store holds no users at all. Set
    ACCIDENT_RISK_BOOTSTRAP_ADMIN_PHONE to choose the number.
    """
    if database.list_users():
        return
    phone = env.bootstrap_admin_phone
    if not phone:
        return
    try:
        database.create_user(phone, env.bootstrap_admin_name, "administrator")
    except ValueError:  # pragma: no cover - a malformed number is reported at login
        logging.getLogger("accident_risk").warning("bootstrap_admin_phone is not a valid number")


def create_app(
    engine: InferenceEngine | None = None, database_path: Path | str | None = None
) -> FastAPI:
    """Create isolated session state; serve with a single worker to retain in-memory state."""
    env = EnvironmentSettings()
    if engine is None:
        config = load_config(env.config) if env.config.exists() else {}
        configure_logging(config.get("logging", {}).get("level", "INFO"))
        vehicle = VehicleModel.load(env.vehicle_model) if env.vehicle_model else None
        physiology = PhysiologyModel.load(env.physiology_model) if env.physiology_model else None
        engine = InferenceEngine(
            vehicle,
            physiology,
            settings=None
            if vehicle or physiology
            else PipelineSettings(**config.get("pipeline", {})),
            fusion=RiskFusion(**config.get("fusion", {})),
        )
    application = FastAPI(title="Accident-risk research API", version="0.1.0")
    buffer = LiveBuffer()
    lock = RLock()
    map_resolver = MapContextResolver()
    application.state.engine = engine
    application.state.active = True
    static_dir = Path(__file__).with_name("static")
    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    database = Database(database_path if database_path is not None else env.database)
    auth = AuthService(database, expose_codes=env.expose_login_codes)
    recorder = SessionRecorder(database)
    bootstrap_administrator(database, env)
    application.state.db = database
    application.state.auth = auth
    application.state.recorder = recorder
    application.include_router(create_router(database, auth, recorder, engine))

    @application.get("/app", include_in_schema=False)
    @application.get("/app/{path:path}", include_in_schema=False)
    def application_shell(path: str = "") -> FileResponse:
        """Serve the role-aware driver and operator interface for any client route."""
        return FileResponse(
            static_dir / "app" / "index.html", headers={"Cache-Control": "no-store"}
        )

    @application.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """Serve the approachable live monitoring dashboard."""
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    def ingest_frame(frame: SensorFrame) -> PredictionResult | None:
        """Consume immediately under the session lock; slow modalities never gate ingestion."""
        if not application.state.active:
            raise HTTPException(409, "Session is stopped; POST /session/start")
        try:
            if frame.timestamp > time.time() + 1:
                raise ValueError("Timestamp is in the future; use Unix seconds UTC")

            if frame.gps_latitude is not None and frame.gps_longitude is not None:
                in_roundabout = map_resolver.is_in_roundabout(
                    frame.gps_latitude, frame.gps_longitude
                )
                frame = frame.model_copy(update={"is_roundabout": in_roundabout})

            buffer.push(frame)
            result = None
            for pending in buffer:
                result = engine.process(pending)
            prediction = result or engine.latest.get(frame.stream_id)
            # Persist the frame against its session if one is recording. This
            # never raises: storage problems must not stop acquisition.
            recorder.observe(frame, prediction)
            return prediction
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.get("/api/v1/predict/live")
    def live_dashboard(stream_id: str | None = Query(None)) -> dict:
        """Display actual observations with the same per-channel expiry as inference."""
        with lock:
            sync = engine.pipeline.synchronizer
            streams = list(sync.values)
            if stream_id is None:
                if len(streams) > 1:
                    raise HTTPException(400, "Specify stream_id for multiple streams")
                stream_id = streams[0] if streams else "hardware"
            now = time.time()
            snapshot = sync.snapshot(stream_id, now)
            fields = snapshot.model_dump()
            fresh = any(fields[key] is not None for key in SENSOR_FIELDS)
            status = (
                "active"
                if fresh
                else ("stale" if stream_id in sync.values else "waiting_for_hardware_stream")
            )
            if not application.state.active:
                status = "stopped"
                fields = {key: None for key in SENSOR_FIELDS}
            prediction = engine.latest.get(stream_id)
            score = None
            prediction_fresh = (
                prediction is not None
                and 0 <= now - prediction.timestamp <= engine.settings.default_max_age_seconds
            )
            if fresh and application.state.active and prediction_fresh:
                score = prediction.overall_risk_index
            result = {
                "status": status,
                "stream_id": stream_id,
                "received_at": sync.last.get(stream_id),
                "sensor_age_seconds": sync.ages(stream_id, now),
                "risk_score": score,
                "risk_status": "Research model output"
                if score is not None
                else (
                    "No compatible model loaded"
                    if not engine.models
                    else "Waiting for fresh model inputs"
                ),
                "speed": fields["vehicle_speed"] * 3.6
                if fields["vehicle_speed"] is not None
                else None,
                "rpm": fields["rpm"],
                "throttle": fields["throttle_position"],
                "engine_load": fields["engine_load"],
                "heart": fields["heart_rate"],
                "spo2": fields["spo2"],
                "quality": fields["physiology_signal_quality"],
                "heading": fields["gps_heading"],
                "location": {
                    "latitude": fields["gps_latitude"],
                    "longitude": fields["gps_longitude"],
                },
            }
            for key in (
                "accel_x",
                "accel_y",
                "accel_z",
                "gyro_x",
                "gyro_y",
                "gyro_z",
                "steering_angle",
                "tire_pressure_fl",
                "tire_pressure_fr",
                "tire_pressure_rl",
                "tire_pressure_rr",
            ):
                result[key] = fields[key]
            return result

    @application.get("/health")
    def health() -> dict:
        """Expose process/session/model readiness separately."""
        return {
            "status": "ok",
            "session_id": engine.session_id,
            "active": application.state.active,
            "models_loaded": len(engine.models),
            "calibrated": False,
        }

    @application.post("/api/v1/ingest", response_model=PredictionResult | None)
    @application.post("/ingest", response_model=PredictionResult | None)
    def ingest(frame: SensorFrame) -> PredictionResult | None:
        """Accept a timestamped partial sensor update."""
        with lock:
            return ingest_frame(frame)

    @application.post("/ingest/batch")
    def ingest_batch(batch: Batch) -> dict:
        """Structural errors reject the request; temporal errors are reported per item.

        Valid earlier items are retained if a later item is out of order.
        """
        with lock:
            if not application.state.active:
                raise HTTPException(409, "Session is stopped")
            results = []
            for i, frame in enumerate(batch.frames):
                try:
                    prediction = ingest_frame(frame)
                    results.append({"index": i, "accepted": True, "prediction": prediction})
                except HTTPException as exc:
                    results.append({"index": i, "accepted": False, "error": exc.detail})
            return {"results": results}

    @application.get("/prediction/latest", response_model=PredictionResult | None)
    def latest(stream_id: str | None = Query(None)) -> PredictionResult | None:
        """Select a stream explicitly when more than one has predictions."""
        with lock:
            if stream_id:
                return engine.latest.get(stream_id)
            if len(engine.latest) > 1:
                raise HTTPException(400, "Specify stream_id for multiple streams")
            return next(iter(engine.latest.values()), None)

    @application.post("/reset")
    def reset() -> dict:
        """Clear session buffers and previous predictions."""
        nonlocal buffer
        with lock:
            engine.reset()
            buffer = LiveBuffer()
            if not application.state.active:
                buffer.stop()
            return {"session_id": engine.session_id}

    @application.post("/session/start")
    def start() -> dict:
        """Begin a fresh independent acquisition session."""
        with lock:
            application.state.active = True
            reset()
            return {"session_id": engine.session_id, "active": True}

    @application.post("/session/stop")
    def stop() -> dict:
        """Stop accepting packets while retaining the last prediction for inspection."""
        with lock:
            application.state.active = False
            buffer.stop()
            return {"session_id": engine.session_id, "active": False}

    return application


app = create_app()
