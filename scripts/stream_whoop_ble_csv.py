import asyncio
import csv
import os
import time
from datetime import datetime

import numpy as np
import requests
from bleak import BleakClient, BleakScanner

CSV_FILE_PATH = "data/raw/whoop_ble_30s_vitals.csv"
API_INGEST_URL = "http://127.0.0.1:8000/ingest"
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Data Buffers
bpm_buffer = []
rr_buffer = []
latest_rmssd = 0.0
latest_sdnn = 0.0
latest_pnn50 = 0.0


def initialize_csv():
    """Ensure destination directory and CSV headers exist."""
    os.makedirs(os.path.dirname(CSV_FILE_PATH), exist_ok=True)
    if not os.path.exists(CSV_FILE_PATH):
        with open(CSV_FILE_PATH, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "timestamp",
                    "datetime",
                    "mean_hr_30s",
                    "std_hr_30s",
                    "min_hr_30s",
                    "max_hr_30s",
                    "range_hr_30s",
                    "median_hr_30s",
                    "mean_rr_30s",
                    "sdnn_30s",
                    "rmssd_30s",
                    "pnn50_30s",
                    "pnn20_30s",
                ]
            )
        print(f"📁 Initialized maximal feature dataset at: {CSV_FILE_PATH}")


def send_instant_vitals(instant_bpm: int):
    """Post instant 1 Hz heart rate to FastAPI LiveBuffer every second."""
    dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    api_payload = {
        "timestamp": dt_str,
        "heart_rate": instant_bpm,
        "mean_hr_30s": instant_bpm,
        "rmssd_30s": latest_rmssd,
        "sdnn_30s": latest_sdnn,
        "pnn50_30s": latest_pnn50
    }
    try:
        requests.post(API_INGEST_URL, json=api_payload, timeout=0.1)
    except Exception:
        pass


def parse_ble_payload(sender, data: bytearray):
    """Parses incoming BLE notification packets into buffers and streams instant HR."""
    global bpm_buffer, rr_buffer

    flags = data[0]
    hr_format = flags & 0x01

    if hr_format == 0:
        bpm = data[1]
        offset = 2
    else:
        bpm = (data[2] << 8) | data[1]
        offset = 3

    if bpm > 0:
        bpm_buffer.append(bpm)
        # Stream instant 1 Hz pulse directly to API
        send_instant_vitals(bpm)

    # Parse R-R intervals if present (bit 4)
    if flags & 0x10:
        while offset + 1 < len(data):
            rr_raw = (data[offset + 1] << 8) | data[offset]
            rr_ms = (rr_raw / 1024.0) * 1000.0
            if 300.0 <= rr_ms <= 2000.0:
                rr_buffer.append(rr_ms)
            offset += 2


def write_30s_window_to_csv():
    """Computes HRV mathematical features across 30s buffer and saves to CSV."""
    global bpm_buffer, rr_buffer, latest_rmssd, latest_sdnn, latest_pnn50

    if len(bpm_buffer) == 0:
        print("⚠️ Waiting for BLE samples...")
        return

    now = int(time.time())
    dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    mean_hr = round(float(np.mean(bpm_buffer)), 2)
    std_hr = round(float(np.std(bpm_buffer)), 2)
    min_hr = round(float(np.min(bpm_buffer)), 2)
    max_hr = round(float(np.max(bpm_buffer)), 2)
    range_hr = round(max_hr - min_hr, 2)
    median_hr = round(float(np.median(bpm_buffer)), 2)

    if len(rr_buffer) > 1:
        mean_rr = round(float(np.mean(rr_buffer)), 2)
        sdnn = round(float(np.std(rr_buffer)), 2)
        rr_diffs = np.abs(np.diff(rr_buffer))
        rmssd = round(float(np.sqrt(np.mean(rr_diffs**2))), 2)
        pnn50 = round(float((np.sum(rr_diffs > 50.0) / len(rr_diffs)) * 100.0), 2)
        pnn20 = round(float((np.sum(rr_diffs > 20.0) / len(rr_diffs)) * 100.0), 2)
    else:
        mean_rr, sdnn, rmssd, pnn50, pnn20 = 0.0, 0.0, 0.0, 0.0, 0.0

    # Cache latest HRV for instant 1 Hz streaming
    latest_rmssd, latest_sdnn, latest_pnn50 = rmssd, sdnn, pnn50

    with open(CSV_FILE_PATH, mode="a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                now, dt_str, mean_hr, std_hr, min_hr, max_hr,
                range_hr, median_hr, mean_rr, sdnn, rmssd, pnn50, pnn20
            ]
        )

    print(
        f"[{dt_str}] 💾 30s Window logged to CSV | Mean HR: {mean_hr} BPM | "
        f"RMSSD: {rmssd} ms | pNN50: {pnn50}%"
    )

    bpm_buffer.clear()
    rr_buffer.clear()


async def main():
    initialize_csv()
    print("🔍 Scanning for WHOOP device over BLE...")

    devices = await BleakScanner.discover()
    whoop_device = None

    for d in devices:
        if d.name and "WHOOP" in d.name.upper():
            whoop_device = d
            break

    if not whoop_device:
        print("❌ WHOOP band not found. Ensure 'Broadcast Heart Rate' is ON in WHOOP App.")
        return

    print(f"✅ Found WHOOP: {whoop_device.name} [{whoop_device.address}]")

    async with BleakClient(whoop_device.address) as client:
        print("⚡ Connected! Subscribing to 1 Hz BLE stream...")
        await client.start_notify(HEART_RATE_MEASUREMENT_UUID, parse_ble_payload)
        print("📡 Streaming active. Instant HR streaming live to API...")

        while True:
            await asyncio.sleep(30)
            write_30s_window_to_csv()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped BLE Harvester.")