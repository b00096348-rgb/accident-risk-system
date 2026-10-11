import os
import threading
from pathlib import Path

import joblib
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from accident_risk.sources.live_buffer import LiveBuffer

app = FastAPI(title="Accident Risk System - Realtime Hardware Pipeline")
live_buffer = LiveBuffer()

state_lock = threading.Lock()
live_hardware_state = {}

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def serve_dashboard():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(
            index_path,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    return {"status": "active", "message": "Accident Risk System API running"}


@app.get("/static/{file_path:path}")
def serve_static_no_cache(file_path: str):
    file_full_path = STATIC_DIR / file_path
    if file_full_path.exists() and file_full_path.is_file():
        return FileResponse(
            file_full_path,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    return {"error": "File not found"}


@app.post("/ingest")
@app.post("/api/v1/ingest")
async def ingest_hardware_data(request: Request):
    global live_hardware_state
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            payload["stream_id"] = "hardware"
            with state_lock:
                live_hardware_state.update(payload)
            live_buffer.push(payload)
            return {"status": "success", "ingested": True}
        return {"status": "error", "message": "Payload must be a JSON object"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/v1/predict/live")
def get_live_prediction():
    global live_hardware_state

    with state_lock:
        latest_sample = dict(live_hardware_state)

    if not latest_sample:
        latest_sample = live_buffer.get_latest_snapshot("hardware") or {}

    if not latest_sample:
        obd_path = Path("data/raw/obd_entries.csv")
        whoop_path = Path("data/raw/whoop_ble_30s_vitals.csv")

        if obd_path.exists() and whoop_path.exists():
            try:
                df_obd = pd.read_csv(obd_path)
                df_whoop = pd.read_csv(whoop_path)
                if not df_obd.empty and not df_whoop.empty:
                    df_obd["datetime"] = pd.to_datetime(df_obd["timestamp"])
                    df_whoop["datetime"] = pd.to_datetime(df_whoop["datetime"])
                    df_merged = (
                        pd.merge_asof(
                            df_obd.sort_values("datetime"),
                            df_whoop.sort_values("datetime"),
                            on="datetime",
                            direction="backward",
                        )
                        .bfill()
                        .fillna(0)
                    )
                    latest_sample = df_merged.iloc[-1].to_dict()
            except Exception:
                pass

    if not latest_sample:
        return {"status": "waiting_for_hardware_stream"}

    model_path = "artifacts/fusion_model/fusion_model.joblib"
    scaler_path = "artifacts/fusion_model/scaler.joblib"

    if not os.path.exists(model_path) or not os.path.exists(scaler_path):
        return {"status": "model_not_trained", "message": "Run scripts/train_fusion.py first."}

    model = joblib.load(model_path)
    scaler = joblib.load(scaler_path)

    features = [
        "speed", "rpm", "throttle_position", "engine_load_value",
        "acceleration", "mean_hr_30s", "rmssd_30s", "sdnn_30s", "pnn50_30s"
    ]

    sample_data = {feat: float(latest_sample.get(feat, 0.0)) for feat in features}
    
    # Extract instant heart rate if available
    instant_hr = float(latest_sample.get("heart_rate", sample_data["mean_hr_30s"]))

    df_in = pd.DataFrame([sample_data])[features]
    scaled = scaler.transform(df_in)

    prob = float(model.predict_proba(scaled)[0][1])

    real_speed = float(sample_data["speed"])
    real_rpm = float(sample_data["rpm"])
    real_throttle = float(sample_data["throttle_position"])
    real_load = float(sample_data["engine_load_value"])
    real_accel = float(sample_data["acceleration"])

    lat = float(latest_sample.get("latitude", 25.2775))
    lon = float(latest_sample.get("longitude", 55.3418))

    risk_pct = int(round(prob * 100))

    return {
        "status": "active",
        "risk": risk_pct,
        "risk_score": round(prob, 4),
        "risk_level": "HIGH" if prob > 0.6 else ("MODERATE" if prob > 0.35 else "LOW"),
        "position": {
            "lat": lat,
            "lon": lon,
            "name": "Live Vehicle GPS"
        },
        "location": {"latitude": lat, "longitude": lon},
        "speed": int(round(real_speed)),
        "rpm": int(round(real_rpm)),
        "throttle": int(round(real_throttle)),
        "engine_load": int(round(real_load)),
        "load": int(round(real_load)),
        "acceleration": round(real_accel, 2),
        "ax": round(real_accel, 2),
        "ay": 0.0,
        "az": 9.81,
        "heart": int(round(instant_hr)) if instant_hr > 0 else 72,
        "event": "Live Hardware Telematics Active",
        "live_vitals": {
            "hr": int(round(instant_hr)) if instant_hr > 0 else 72,
            "rmssd": sample_data["rmssd_30s"],
            "sdnn": sample_data["sdnn_30s"],
            "pnn50": sample_data["pnn50_30s"]
        },
        "live_telematics": {
            "speed_kmh": real_speed,
            "rpm": real_rpm,
            "throttle": real_throttle,
            "acceleration": real_accel
        }
    }