import csv
import datetime
import os
import time

import obd
import requests

# macOS CoreLocation integration
try:
    import CoreLocation
    from Foundation import NSDate, NSRunLoop
    HAS_CORELOCATION = True
except ImportError:
    HAS_CORELOCATION = False

# Persistent Location Manager instance & Cache
_LOCATION_MANAGER = None
_CACHED_LAT = 25.2775
_CACHED_LON = 55.3418
_LAST_IP_FETCH = 0.0

if HAS_CORELOCATION:
    try:
        _LOCATION_MANAGER = CoreLocation.CLLocationManager.alloc().init()
        _LOCATION_MANAGER.startUpdatingLocation()
    except Exception:
        _LOCATION_MANAGER = None


def fetch_ip_location():
    """Fetch location via IP geolocation at most once every 30 seconds to prevent rate limits."""
    global _CACHED_LAT, _CACHED_LON, _LAST_IP_FETCH
    now = time.time()
    if now - _LAST_IP_FETCH > 30.0:
        _LAST_IP_FETCH = now
        try:
            res = requests.get("http://ip-api.com/json/", timeout=2.0).json()
            if res.get("status") == "success":
                _CACHED_LAT = round(float(res["lat"]), 6)
                _CACHED_LON = round(float(res["lon"]), 6)
        except Exception:
            pass
    return _CACHED_LAT, _CACHED_LON


def get_live_gps():
    """Fetch live coordinates from CoreLocation or cached IP location."""
    global _CACHED_LAT, _CACHED_LON
    if _LOCATION_MANAGER is not None:
        try:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
            loc = _LOCATION_MANAGER.location()
            if loc:
                coord = loc.coordinate()
                lat = round(float(coord.latitude), 6)
                lon = round(float(coord.longitude), 6)
                if lat != 0.0 and lon != 0.0:
                    _CACHED_LAT, _CACHED_LON = lat, lon
                    return lat, lon
        except Exception:
            pass
    return fetch_ip_location()


# Configuration
PORT = "/dev/cu.OBDII"
API_INGEST_URL = "http://127.0.0.1:8000/ingest"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(SCRIPT_DIR, "..", "data", "raw")
os.makedirs(RAW_DIR, exist_ok=True)
CSV_FILE = os.path.join(RAW_DIR, "obd_entries.csv")

FIELDNAMES = [
    "timestamp",
    "speed",
    "rpm",
    "throttle_position",
    "engine_load_value",
    "engine_temperature",
    "acceleration"
]

print("Connecting to OBD-II adapter at:", PORT)

connection = obd.OBD(
    portstr=PORT,
    baudrate=38400,
    fast=False,
    timeout=2.0
)

print("Connection status:", connection.status())

if connection.is_connected():
    print(f"✅ ECU Connected! Logging to '{CSV_FILE}' & streaming to API...\n")
    file_exists = os.path.isfile(CSV_FILE)
    prev_speed_mps = None
    prev_time = None

    try:
        with open(CSV_FILE, mode='a', newline='') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
            if not file_exists:
                writer.writeheader()
                csv_file.flush()

            while True:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                current_time = time.time()

                # 1. Query OBD Sensors
                speed_resp = connection.query(obd.commands.SPEED)
                rpm_resp = connection.query(obd.commands.RPM)
                throttle_resp = connection.query(obd.commands.THROTTLE_POS)
                load_resp = connection.query(obd.commands.ENGINE_LOAD)
                temp_resp = connection.query(obd.commands.COOLANT_TEMP)

                speed_kmh = speed_resp.value.magnitude if not speed_resp.is_null() else 0.0
                rpm = rpm_resp.value.magnitude if not rpm_resp.is_null() else 0.0
                throttle_position = throttle_resp.value.magnitude if not throttle_resp.is_null() else 0.0
                engine_load_value = load_resp.value.magnitude if not load_resp.is_null() else 0.0
                engine_temperature = temp_resp.value.magnitude if not temp_resp.is_null() else 0.0

                # 2. Acceleration calculation
                speed_mps = speed_kmh * (1000.0 / 3600.0)
                acceleration = 0.0
                if prev_speed_mps is not None and prev_time is not None:
                    dt = current_time - prev_time
                    if dt > 0:
                        acceleration = (speed_mps - prev_speed_mps) / dt

                prev_speed_mps = speed_mps
                prev_time = current_time

                # 3. Write to local CSV
                row_data = {
                    "timestamp": timestamp,
                    "speed": round(speed_kmh, 2),
                    "rpm": round(rpm, 2),
                    "throttle_position": round(throttle_position, 2),
                    "engine_load_value": round(engine_load_value, 2),
                    "engine_temperature": round(engine_temperature, 2),
                    "acceleration": round(acceleration, 3)
                }
                writer.writerow(row_data)
                csv_file.flush()

                # 4. Fetch Live GPS
                lat, lon = get_live_gps()

                # 5. Push live payload to FastAPI /ingest
                api_payload = {
                    "timestamp": timestamp,
                    "speed": round(speed_kmh, 2),
                    "rpm": round(rpm, 2),
                    "throttle_position": round(throttle_position, 2),
                    "engine_load_value": round(engine_load_value, 2),
                    "engine_temperature": round(engine_temperature, 2),
                    "acceleration": round(acceleration, 3),
                    "latitude": lat,
                    "longitude": lon
                }
                try:
                    requests.post(API_INGEST_URL, json=api_payload, timeout=0.2)
                except Exception:
                    pass

                print(
                    f"[LIVE ECU -> CSV & API] {timestamp} | "
                    f"Speed: {speed_kmh:.1f} km/h | "
                    f"RPM: {rpm:.0f} | "
                    f"Lat: {lat} | Lon: {lon}"
                )

                time.sleep(0.2)

    except KeyboardInterrupt:
        print(f"\nStopped collection. Data saved cleanly to {CSV_FILE}.")
    finally:
        connection.close()
        print("Serial connection closed.")
else:
    print("❌ Failed to connect to car ECU.")