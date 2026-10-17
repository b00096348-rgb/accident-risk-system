"""Local research ingestion API; a future Android/BLE gateway sends SensorFrame JSON."""

import time
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from accident_risk.inference.engine import InferenceEngine
from accident_risk.logging_utils import configure_logging
from accident_risk.models.fusion import RiskFusion
from accident_risk.models.physiology_model import PhysiologyModel
from accident_risk.models.vehicle_model import VehicleModel
from accident_risk.schemas import SENSOR_FIELDS, PredictionResult, SensorFrame
from accident_risk.settings import EnvironmentSettings, PipelineSettings, load_config
from accident_risk.sources.live_buffer import LiveBuffer


class Batch(BaseModel):
    """Bounded request size, with independent accepted/rejected results."""

    frames: list[SensorFrame] = Field(min_length=1, max_length=1000)


def create_app(engine: InferenceEngine | None = None) -> FastAPI:
    """Create isolated session state; serve with a single worker to retain in-memory state."""
    if engine is None:
        env = EnvironmentSettings()
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
    app = FastAPI(title="Accident-risk research API", version="0.1.0")
    buffer = LiveBuffer()
    lock = RLock()
    app.state.engine = engine
    app.state.active = True
    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """Serve the approachable live monitoring dashboard."""
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    def ingest_frame(frame: SensorFrame) -> PredictionResult | None:
        """Consume immediately under the session lock; slow modalities never gate ingestion."""
        if not app.state.active:
            raise HTTPException(409, "Session is stopped; POST /session/start")
        try:
            if frame.timestamp > time.time() + 1:
                raise ValueError("Timestamp is in the future; use Unix seconds UTC")
            buffer.push(frame)
            result = None
            for pending in buffer:
                result = engine.process(pending)
            return result or engine.latest.get(frame.stream_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/predict/live")
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
            if not app.state.active:
                status = "stopped"
                fields = {key: None for key in SENSOR_FIELDS}
            prediction = engine.latest.get(stream_id)
            score = None
            # Never keep a score alive after its inputs expire or the session stops.
            prediction_fresh = (
                prediction is not None
                and 0 <= now - prediction.timestamp <= engine.settings.default_max_age_seconds
            )
            if fresh and app.state.active and prediction_fresh:
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

    @app.get("/health")
    def health() -> dict:
        """Expose process/session/model readiness separately."""
        return {
            "status": "ok",
            "session_id": engine.session_id,
            "active": app.state.active,
            "models_loaded": len(engine.models),
            "calibrated": False,
        }

    @app.post("/api/v1/ingest", response_model=PredictionResult | None)
    @app.post("/ingest", response_model=PredictionResult | None)
    def ingest(frame: SensorFrame) -> PredictionResult | None:
        """Accept a timestamped partial sensor update."""
        with lock:
            return ingest_frame(frame)

    @app.post("/ingest/batch")
    def ingest_batch(batch: Batch) -> dict:
        """Structural errors reject the request; temporal errors are reported per item.

        Valid earlier items are retained if a later item is out of order.
        """
        with lock:
            if not app.state.active:
                raise HTTPException(409, "Session is stopped")
            results = []
            for i, frame in enumerate(batch.frames):
                try:
                    prediction = ingest_frame(frame)
                    results.append({"index": i, "accepted": True, "prediction": prediction})
                except HTTPException as exc:
                    results.append({"index": i, "accepted": False, "error": exc.detail})
            return {"results": results}

    @app.get("/prediction/latest", response_model=PredictionResult | None)
    def latest(stream_id: str | None = Query(None)) -> PredictionResult | None:
        """Select a stream explicitly when more than one has predictions."""
        with lock:
            if stream_id:
                return engine.latest.get(stream_id)
            if len(engine.latest) > 1:
                raise HTTPException(400, "Specify stream_id for multiple streams")
            return next(iter(engine.latest.values()), None)

    @app.post("/reset")
    def reset() -> dict:
        """Clear session buffers and previous predictions."""
        nonlocal buffer
        with lock:
            engine.reset()
            buffer = LiveBuffer()
            if not app.state.active:
                buffer.stop()
            return {"session_id": engine.session_id}

    @app.post("/session/start")
    def start() -> dict:
        """Begin a fresh independent acquisition session."""
        with lock:
            app.state.active = True
            reset()
            return {"session_id": engine.session_id, "active": True}

    @app.post("/session/stop")
    def stop() -> dict:
        """Stop accepting packets while retaining the last prediction for inspection."""
        with lock:
            app.state.active = False
            buffer.stop()
            return {"session_id": engine.session_id, "active": False}

    return app


app = create_app()
