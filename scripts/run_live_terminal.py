import asyncio
import datetime
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import joblib
import numpy as np
import obd
import pandas as pd
import requests
from bleak import BleakClient, BleakScanner

# Configuration & Constants
OBD_PORT = "/dev/cu.OBDII"
HEART_RATE_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
MODEL_PATH = "artifacts/fusion_model/fusion_model.joblib"
SCALER_PATH = "artifacts/fusion_model/scaler.joblib"
HTTP_PORT = 8000

# Shared Thread-Safe States
state_lock = threading.Lock()
latest_vitals = {
    "mean_hr_30s": 72.0,
    "rmssd_30s": 45.0,
    "sdnn_30s": 40.0,
    "pnn50_30s": 15.0,
    "instant_hr": 72
}

phone_lock = threading.Lock()
latest_phone_data = {
    "lat": 0.0,
    "lon": 0.0,
    "gyro_x": 0.0,
    "gyro_y": 0.0,
    "gyro_z": 0.0,
    "has_fix": False
}


# ==========================================
# Phone Ingest Server (Galaxy A26 Receiver)
# ==========================================
def parse_phone_payload(data):
    """Parse incoming GPS & Gyroscope data from Galaxy A26 sensor payload."""
    lat, lon = None, None
    gx, gy, gz = 0.0, 0.0, 0.0

    # 1. Sensor Logger App Payload Structure
    if "payload" in data and isinstance(data["payload"], list):
        for entry in data["payload"]:
            name = entry.get("name")
            vals = entry.get("values", {})
            if name == "location":
                lat = vals.get("latitude")
                lon = vals.get("longitude")
            elif name == "gyroscope":
                gx = vals.get("x", 0.0)
                gy = vals.get("y", 0.0)
                gz = vals.get("z", 0.0)

    # 2. Direct JSON Stream Payload Structure
    else:
        lat = data.get("latitude") or data.get("lat") or data.get("gps_latitude")
        lon = data.get("longitude") or data.get("lon") or data.get("gps_longitude")
        gx = data.get("gyro_x", 0.0)
        gy = data.get("gyro_y", 0.0)
        gz = data.get("gyro_z", 0.0)

    if lat is not None and lon is not None:
        with phone_lock:
            latest_phone_data["lat"] = round(float(lat), 6)
            latest_phone_data["lon"] = round(float(lon), 6)
            latest_phone_data["gyro_x"] = round(float(gx), 4)
            latest_phone_data["gyro_y"] = round(float(gy), 4)
            latest_phone_data["gyro_z"] = round(float(gz), 4)
            latest_phone_data["has_fix"] = True


class PhoneSensorHTTPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/ingest":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode("utf-8"))
                parse_phone_payload(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"received"}')
            except Exception:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress HTTP server output logs to keep terminal stream clean
        return


def start_phone_ingest_thread():
    """Start HTTP server thread to listen for Galaxy A26 phone telemetry."""
    def run_server():
        server_address = ("0.0.0.0", HTTP_PORT)
        httpd = HTTPServer(server_address, PhoneSensorHTTPHandler)
        httpd.serve_forever()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()


# Fallback IP Geolocation if Phone Stream has not connected yet
def fetch_ip_location():
    try:
        resp = requests.get("http://ip-api.com/json/", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return round(data["lat"], 6), round(data["lon"], 6)
    except Exception:
        pass
    return 0.0, 0.0


# ==========================================
# WHOOP BLE Vitals Background Thread
# ==========================================
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
            whoop_dev = next((d for d in devices if d.name and "WHOOP" in d.name.upper()), None)
            if not whoop_dev:
                print("⚠️ WHOOP band not detected over BLE. (Using baseline resting vitals)")
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
        flags = data[0]
        offset = 2 if (flags & 0x01) == 0 else 3
        bpm = data[1] if (flags & 0x01) == 0 else ((data[2] << 8) | data[1])
        if bpm > 0:
            self.bpm_buffer.append(bpm)
            with state_lock:
                latest_vitals["instant_hr"] = bpm

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
            latest_vitals["mean_hr_30s"] = round(mean_hr, 2)
            latest_vitals["rmssd_30s"] = round(rmssd, 2)
            latest_vitals["sdnn_30s"] = round(sdnn, 2)
            latest_vitals["pnn50_30s"] = round(pnn50, 2)

        self.bpm_buffer.clear()
        self.rr_buffer.clear()


# ==========================================
# Main Terminal Inference Engine
# ==========================================
def main():
    if not os.path.exists(MODEL_PATH) or not os.path.exists(SCALER_PATH):
        print("❌ Model files not found! Please run training script first.")
        sys.exit(1)

    print("🧠 Loading XGBoost Inference Engine & Scaler...")
    model = joblib.load(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)

    print("📱 Starting Galaxy A26 Phone Ingest Server on port 8000...")
    start_phone_ingest_thread()

    print("🔍 Searching for WHOOP BLE Band...")
    whoop_thread = WhoopBLEThread()
    whoop_thread.start()

    print(f"🔌 Connecting to Vehicle ECU on {OBD_PORT} at 38400 baud...")
    connection = obd.OBD(portstr=OBD_PORT, baudrate=38400, fast=False, timeout=2.0)
    print(f"ECU Status: {connection.status()}")

    feature_cols = [
        "speed", "rpm", "throttle_position", "engine_load_value",
        "acceleration", "mean_hr_30s", "rmssd_30s", "sdnn_30s", "pnn50_30s"
    ]

    prev_speed_mps = None
    prev_time = None

    print("\n" + "=" * 95)
    print(" 🚀 REAL-TIME MULTIMODAL ACCIDENT RISK INFERENCE (GALAXY A26 GPS & GYRO NODE)")
    print("=" * 95 + "\n")

    try:
        while True:
            t_now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            current_time = time.time()

            # 1. OBD-II ECU Telematics
            speed_kmh, rpm, throttle, load = 0.0, 0.0, 0.0, 0.0
            if connection.is_connected():
                s_res = connection.query(obd.commands.SPEED)
                r_res = connection.query(obd.commands.RPM)
                t_res = connection.query(obd.commands.THROTTLE_POS)
                l_res = connection.query(obd.commands.ENGINE_LOAD)

                speed_kmh = s_res.value.magnitude if not s_res.is_null() else 0.0
                rpm = r_res.value.magnitude if not r_res.is_null() else 0.0
                throttle = t_res.value.magnitude if not t_res.is_null() else 0.0
                load = l_res.value.magnitude if not l_res.is_null() else 0.0

            # 2. Acceleration Calculation
            speed_mps = speed_kmh * (1000.0 / 3600.0)
            accel = 0.0
            if prev_speed_mps is not None and prev_time is not None:
                dt = current_time - prev_time
                if dt > 0:
                    accel = (speed_mps - prev_speed_mps) / dt
            prev_speed_mps = speed_mps
            prev_time = current_time

            # 3. Galaxy A26 GPS & Gyroscope Snapshot
            with phone_lock:
                phone_snapshot = dict(latest_phone_data)

            if phone_snapshot["has_fix"]:
                lat, lon = phone_snapshot["lat"], phone_snapshot["lon"]
            else:
                lat, lon = fetch_ip_location()

            gx, gy, gz = phone_snapshot["gyro_x"], phone_snapshot["gyro_y"], phone_snapshot["gyro_z"]

            # 4. Snapshot of Current Vitals
            with state_lock:
                vitals = dict(latest_vitals)

            # 5. Feature Matrix & XGBoost Inference
            sample_dict = {
                "speed": round(speed_kmh, 2),
                "rpm": round(rpm, 2),
                "throttle_position": round(throttle, 2),
                "engine_load_value": round(load, 2),
                "acceleration": round(accel, 3),
                "mean_hr_30s": vitals["mean_hr_30s"],
                "rmssd_30s": vitals["rmssd_30s"],
                "sdnn_30s": vitals["sdnn_30s"],
                "pnn50_30s": vitals["pnn50_30s"]
            }

            df_input = pd.DataFrame([sample_dict])[feature_cols]
            scaled_input = scaler.transform(df_input)
            prob = float(model.predict_proba(scaled_input)[0][1])

            risk_pct = round(prob * 100, 1)
            risk_label = "HIGH" if prob > 0.6 else ("MODERATE" if prob > 0.35 else "LOW")

            # Terminal Color Banners
            color_code = "\033[91m" if risk_label == "HIGH" else ("\033[93m" if risk_label == "MODERATE" else "\033[92m")
            reset_code = "\033[0m"

            # 6. Stream Live Multimodal Terminal Output
            print(
                f"[{t_now}] 📍 Lat:{lat:.6f}, Lon:{lon:.6f} | "
                f"🌀 Gyro Z:{gz:+5.2f} | "
                f"🚗 Speed:{speed_kmh:4.1f}km/h | RPM:{rpm:4.0f} | Accel:{accel:5.2f}m/s² | "
                f"🫀 HR:{vitals['instant_hr']:3.0f}bpm | HRV:{vitals['rmssd_30s']:4.1f}ms | "
                f"🎯 Risk: {color_code}{risk_pct:5.1f}% ({risk_label}){reset_code}"
            )

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\n🛑 Stopped live inference stream.")
    finally:
        if connection.is_connected():
            connection.close()
        print("Serial connections closed cleanly.")


if __name__ == "__main__":
    main()