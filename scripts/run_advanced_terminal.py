import asyncio
import datetime
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import joblib
import numpy as np
import obd
import pandas as pd
from bleak import BleakClient, BleakScanner

# Path Configuration
FUSION_MODEL_PATH = "artifacts/fusion_model/fusion_model.joblib"
FUSION_SCALER_PATH = "artifacts/fusion_model/scaler.joblib"
BENCHMARK_MODEL_PATH = "artifacts/benchmark_models/vzcrash_xgb.joblib"
ANOMALY_MODEL_PATH = "artifacts/benchmark_models/unsupervised_iso.joblib"
BENCHMARK_SCALER_PATH = "artifacts/benchmark_models/benchmark_scaler.joblib"

OBD_PORT = "/dev/cu.OBDII"
HEART_RATE_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
CLASS_MAP = {0: "CRASH (Class 0)", 1: "NEAR-MISS (Class 1)", 2: "NORMAL (Class 2)"}

# Maximum allowed delay between live hardware packets (seconds)
HARDWARE_TIMEOUT_SEC = 5.0

# Thread-Safe Global State Store
state_lock = threading.Lock()

live_state = {
    # Android Phone Stream
    "lat": 0.0,
    "lon": 0.0,
    "accel_mag": 0.0,
    "gyro_mag": 0.0,
    "last_android_time": 0.0,
    # WHOOP BLE Vitals Stream
    "instant_hr": 0,
    "mean_hr_30s": 72.0,
    "rmssd_30s": 45.0,
    "sdnn_30s": 40.0,
    "pnn50_30s": 15.0,
    "last_whoop_time": 0.0,
    # OBD-II ECU Telematics Stream
    "speed": 0.0,
    "rpm": 0.0,
    "throttle": 0.0,
    "load": 0.0,
    "accel_obd": 0.0,
    "last_obd_time": 0.0,
}


# =========================================================
# 1. Robust Background HTTP Receiver for Sensor Logger App
# =========================================================
class AndroidIngestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        now = time.time()

        try:
            payload = json.loads(body.decode("utf-8"))

            # Normalize array vs object payloads from different apps
            if isinstance(payload, list):
                frames = payload
            elif (
                isinstance(payload, dict)
                and "payload" in payload
                and isinstance(payload["payload"], list)
            ):
                frames = payload["payload"]
            elif isinstance(payload, dict):
                frames = [payload]
            else:
                frames = []

            with state_lock:
                for frame in frames:
                    if not isinstance(frame, dict):
                        continue

                    # Structure A: Sensor Logger format {"name": "location", "values": {"latitude": ...}}
                    sensor_name = str(frame.get("name", "")).lower()
                    values = (
                        frame.get("values", {})
                        if isinstance(frame.get("values"), dict)
                        else {}
                    )

                    if sensor_name in ("location", "gps"):
                        lat = values.get("latitude") or values.get("lat")
                        lon = values.get("longitude") or values.get("lon")
                        if (
                            lat is not None
                            and lon is not None
                            and float(lat) != 0.0
                        ):
                            live_state["lat"] = float(lat)
                            live_state["lon"] = float(lon)
                            live_state["last_android_time"] = now

                    elif sensor_name in (
                        "accelerometer",
                        "accel",
                        "accelerometeruncalibrated",
                    ):
                        x = values.get("x", 0.0)
                        y = values.get("y", 0.0)
                        z = values.get("z", 0.0)
                        live_state["accel_mag"] = math.sqrt(
                            float(x) ** 2 + float(y) ** 2 + float(z) ** 2
                        )
                        live_state["last_android_time"] = now

                    elif sensor_name in (
                        "gyroscope",
                        "gyro",
                        "gyroscopeuncalibrated",
                    ):
                        x = values.get("x", 0.0)
                        y = values.get("y", 0.0)
                        z = values.get("z", 0.0)
                        live_state["gyro_mag"] = math.sqrt(
                            float(x) ** 2 + float(y) ** 2 + float(z) ** 2
                        )
                        live_state["last_android_time"] = now

                    # Structure B: Flat JSON format {"latitude": 25.32, "accel_x": 0.1}
                    lat = (
                        frame.get("latitude")
                        or frame.get("gps_latitude")
                        or frame.get("lat")
                    )
                    lon = (
                        frame.get("longitude")
                        or frame.get("gps_longitude")
                        or frame.get("lon")
                    )
                    if (
                        lat is not None
                        and lon is not None
                        and float(lat) != 0.0
                    ):
                        live_state["lat"] = float(lat)
                        live_state["lon"] = float(lon)
                        live_state["last_android_time"] = now

                    ax = frame.get("accel_x") or frame.get("ax")
                    ay = frame.get("accel_y") or frame.get("ay")
                    az = frame.get("accel_z") or frame.get("az")
                    if ax is not None and ay is not None and az is not None:
                        live_state["accel_mag"] = math.sqrt(
                            float(ax) ** 2 + float(ay) ** 2 + float(az) ** 2
                        )
                        live_state["last_android_time"] = now

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except Exception:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        return  # Keep console output clean


def start_android_receiver():
    """Run background HTTP server on port 8000 for Android Sensor Logger payloads."""
    server = HTTPServer(("0.0.0.0", 8000), AndroidIngestHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


# =========================================================
# 2. Background Thread for WHOOP BLE Vitals
# =========================================================
class WhoopBLEThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.bpm_buffer = []
        self.rr_buffer = []

    def run(self):
        asyncio.run(self.ble_main())

    async def ble_main(self):
        try:
            devices = await BleakScanner.discover()
            whoop_dev = next(
                (d for d in devices if d.name and "WHOOP" in d.name.upper()),
                None,
            )
            if not whoop_dev:
                print("❌ WHOOP band not detected over BLE.")
                return

            print(f"✅ Connected to WHOOP BLE Band: {whoop_dev.name}")
            async with BleakClient(whoop_dev.address) as client:
                await client.start_notify(HEART_RATE_UUID, self.parse_ble)
                while True:
                    await asyncio.sleep(15)
                    self.compute_vitals()
        except Exception as e:
            print(f"⚠️ WHOOP BLE Connection error: {e}")

    def parse_ble(self, sender, data: bytearray):
        if not data:
            return
        flags = data[0]
        offset = 2 if (flags & 0x01) == 0 else 3
        bpm = data[1] if (flags & 0x01) == 0 else ((data[2] << 8) | data[1])
        now = time.time()

        if bpm > 0:
            self.bpm_buffer.append(bpm)
            with state_lock:
                live_state["instant_hr"] = bpm
                live_state["last_whoop_time"] = now

        if flags & 0x10:
            while offset + 1 < len(data):
                rr_raw = (data[offset + 1] << 8) | data[offset]
                rr_ms = (rr_raw / 1024.0) * 1000.0
                if 300.0 <= rr_ms <= 2000.0:
                    self.rr_buffer.append(rr_ms)
                offset += 2

    def compute_vitals(self):
        if not self.bpm_buffer:
            return
        mean_hr = float(np.mean(self.bpm_buffer))
        rmssd, sdnn, pnn50 = 45.0, 40.0, 15.0
        if len(self.rr_buffer) > 1:
            sdnn = float(np.std(self.rr_buffer))
            diffs = np.abs(np.diff(self.rr_buffer))
            rmssd = float(np.sqrt(np.mean(diffs**2)))
            pnn50 = float((np.sum(diffs > 50.0) / len(diffs)) * 100.0)

        with state_lock:
            live_state["mean_hr_30s"] = round(mean_hr, 2)
            live_state["rmssd_30s"] = round(rmssd, 2)
            live_state["sdnn_30s"] = round(sdnn, 2)
            live_state["pnn50_30s"] = round(pnn50, 2)

        self.bpm_buffer.clear()
        self.rr_buffer.clear()


# =========================================================
# 3. Main Real-Time Engine with Live Connection Gate
# =========================================================
def main():
    if not os.path.exists(BENCHMARK_MODEL_PATH) or not os.path.exists(
        FUSION_MODEL_PATH
    ):
        print(
            "❌ Model files missing! Run 'python scripts/train_fusion.py' and 'python scripts/train_vzcrash_3class.py' first."
        )
        sys.exit(1)

    print(
        "🧠 Loading Model Artifacts (Fusion, VZCrash Benchmark, & Anomaly Detector)..."
    )
    fusion_model = joblib.load(FUSION_MODEL_PATH)
    fusion_scaler = joblib.load(FUSION_SCALER_PATH)

    benchmark_model = joblib.load(BENCHMARK_MODEL_PATH)
    anomaly_model = joblib.load(ANOMALY_MODEL_PATH)
    benchmark_scaler = joblib.load(BENCHMARK_SCALER_PATH)

    print("📡 Listening for Android Sensor Logger on port 8000...")
    start_android_receiver()

    print("🔍 Searching for WHOOP BLE Band...")
    whoop_thread = WhoopBLEThread()
    whoop_thread.start()

    print(f"🔌 Connecting to Vehicle ECU on {OBD_PORT} at 38400 baud...")
    connection = obd.OBD(
        portstr=OBD_PORT, baudrate=38400, fast=False, timeout=2.0
    )
    print(f"ECU Status: {connection.status()}")

    fusion_cols = [
        "speed",
        "rpm",
        "throttle_position",
        "engine_load_value",
        "acceleration",
        "mean_hr_30s",
        "rmssd_30s",
        "sdnn_30s",
        "pnn50_30s",
    ]
    prev_speed_mps, prev_time = None, None

    print("\n" + "=" * 100)
    print(
        " 🚀 REAL-TIME MULTIMODAL INFERENCE STREAM (HARDWARE HANDSHAKE GATE ACTIVE)"
    )
    print("=" * 100 + "\n")

    try:
        while True:
            t_now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            current_time = time.time()

            # A. OBD-II ECU Sampling
            speed_kmh, rpm, throttle, load = 0.0, 0.0, 0.0, 0.0
            obd_responsive = False

            if connection.is_connected():
                s_res = connection.query(obd.commands.SPEED)
                r_res = connection.query(obd.commands.RPM)
                t_res = connection.query(obd.commands.THROTTLE_POS)
                l_res = connection.query(obd.commands.ENGINE_LOAD)

                if not r_res.is_null():
                    rpm = r_res.value.magnitude
                    speed_kmh = (
                        s_res.value.magnitude if not s_res.is_null() else 0.0
                    )
                    throttle = (
                        t_res.value.magnitude if not t_res.is_null() else 0.0
                    )
                    load = (
                        l_res.value.magnitude if not l_res.is_null() else 0.0
                    )

                    obd_responsive = True
                    with state_lock:
                        live_state["last_obd_time"] = current_time

            # B. Acceleration Calculation
            speed_mps = speed_kmh * (1000.0 / 3600.0)
            accel_obd = 0.0
            if prev_speed_mps is not None and prev_time is not None:
                dt = current_time - prev_time
                if dt > 0:
                    accel_obd = (speed_mps - prev_speed_mps) / dt
            prev_speed_mps = speed_mps
            prev_time = current_time

            # C. Read Thread-Safe State & Hardware Ages
            with state_lock:
                lat = live_state["lat"]
                lon = live_state["lon"]
                accel_mag = (
                    live_state["accel_mag"]
                    if live_state["accel_mag"] > 0
                    else abs(accel_obd)
                )
                gyro_mag = live_state["gyro_mag"]
                hr = live_state["instant_hr"]
                mean_hr = live_state["mean_hr_30s"]
                rmssd = live_state["rmssd_30s"]
                sdnn = live_state["sdnn_30s"]
                pnn50 = live_state["pnn50_30s"]

                android_active = (
                    current_time - live_state["last_android_time"]
                ) <= HARDWARE_TIMEOUT_SEC
                whoop_active = (
                    (current_time - live_state["last_whoop_time"])
                    <= HARDWARE_TIMEOUT_SEC
                    and hr > 0
                )
                obd_active = (
                    (current_time - live_state["last_obd_time"])
                    <= HARDWARE_TIMEOUT_SEC
                    and obd_responsive
                )

            # D. Hardware Connection Gate Validation
            if not (obd_active and whoop_active and android_active):
                status_obd = "🟢 OK" if obd_active else "🔴 DISCONNECTED"
                status_whoop = "🟢 OK" if whoop_active else "🔴 DISCONNECTED"
                status_android = (
                    "🟢 OK" if android_active else "🔴 DISCONNECTED"
                )

                print(
                    f"[{t_now}] ⚠️ INFERENCE PAUSED | Hardware Status: "
                    f"OBD-II: {status_obd} | WHOOP: {status_whoop} | Android Sensor: {status_android}"
                )
                time.sleep(1.0)
                continue

            # E. All Hardware Verified -> Model Inference
            fusion_df = pd.DataFrame(
                [
                    {
                        "speed": round(speed_kmh, 2),
                        "rpm": round(rpm, 2),
                        "throttle_position": round(throttle, 2),
                        "engine_load_value": round(load, 2),
                        "acceleration": round(accel_obd, 3),
                        "mean_hr_30s": mean_hr,
                        "rmssd_30s": rmssd,
                        "sdnn_30s": sdnn,
                        "pnn50_30s": pnn50,
                    }
                ]
            )[fusion_cols]

            fusion_prob = float(
                fusion_model.predict_proba(
                    fusion_scaler.transform(fusion_df)
                )[0][1]
            )

            # F. VZCrash Benchmark Prediction
            benchmark_sample = np.array(
                [[accel_mag, gyro_mag, speed_kmh, abs(accel_obd)]]
            )
            benchmark_scaled = benchmark_scaler.transform(benchmark_sample)
            class_pred = int(benchmark_model.predict(benchmark_scaled)[0])
            class_label = CLASS_MAP[class_pred]

            # G. Unsupervised Anomaly Prediction
            is_anomaly = anomaly_model.predict(benchmark_scaled)[0] == -1

            # Independent Colors
            if class_pred == 0:
                vz_color = "\033[91m"  # Red for Crash
            elif class_pred == 1:
                vz_color = "\033[93m"  # Yellow for Near-Miss
            else:
                vz_color = "\033[92m"  # Green for Normal

            anomaly_color = "\033[91m" if is_anomaly else "\033[92m"
            reset = "\033[0m"

            print(
                f"[{t_now}] 📍 Lat:{lat:.6f}, Lon:{lon:.6f} | "
                f"🚗 Speed:{speed_kmh:4.1f}km/h | RPM:{rpm:4.0f} | "
                f"🫀 HR:{hr:3.0f}bpm | "
                f"🎯 Fusion Risk:{fusion_prob*100:5.1f}% | "
                f"🏆 VZCrash:{vz_color}{class_label}{reset} | "
                f"🔍 Anomaly:{anomaly_color}{'YES' if is_anomaly else 'NO'}{reset}"
            )

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\n🛑 Stopped live inference stream.")
    finally:
        if connection.is_connected():
            connection.close()


if __name__ == "__main__":
    main()