import csv
import json
import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# CONFIGURATION (Pass the KEY names into os.getenv)
CLIENT_ID = os.getenv("WHOOP_CLIENT_ID")
CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET")
AUTH_CODE = os.getenv("WHOOP_AUTH_CODE")
REDIRECT_URI = "https://google.com"

# DATA STORAGE
CSV_FILE_PATH = "data/raw/whoop_vitals_continuous.csv"
TOKEN_FILE = ".whoop_tokens.json"

# OFFICIAL WHOOP V1 ENDPOINTS
BASE_URL = "https://api.prod.whoop.com/developer/v2"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


def save_tokens(tokens):
    """Save tokens to local JSON file to persist session."""
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)


def load_stored_tokens():
    """Load cached tokens if available."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return None


def get_initial_token():
    """Exchange AUTH_CODE for Access & Refresh tokens."""
    payload = {
        "grant_type": "authorization_code",
        "code": AUTH_CODE,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }

    print("⏳ Exchanging Auth Code for Access Token...")
    response = requests.post(TOKEN_URL, data=payload)

    if response.status_code != 200:
        print(f"❌ Error getting token ({response.status_code}): {response.text}")
        print("💡 Solution: AUTH_CODE may have expired or was already used. Generate a fresh one.")
        exit(1)

    tokens = response.json()
    save_tokens(tokens)
    print("✅ Authorization successful!")
    return tokens


def refresh_access_token(current_tokens):
    """Obtain a new access token using rotating refresh tokens."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": current_tokens["refresh_token"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "offline read:recovery read:cycles read:sleep",
    }

    print("🔄 Refreshing access token...")
    response = requests.post(TOKEN_URL, data=payload)

    if response.status_code != 200:
        print(f"❌ Error refreshing token ({response.status_code}): {response.text}")
        exit(1)

    new_tokens = response.json()
    save_tokens(new_tokens)
    return new_tokens


def get_valid_access_token(tokens):
    """Ensure token validity, refreshing if within 5 minutes of expiration."""
    if time.time() > (tokens["expires_at"] - 300):
        return refresh_access_token(tokens)
    return tokens


def fetch_latest_data(access_token):
    """Fetch Recovery and Cycle data using official V1 payload schemas."""
    headers = {"Authorization": f"Bearer {access_token}"}

    data_packet = {
        "timestamp": int(time.time()),
        "hrv": None,
        "resting_hr": None,
        "recovery_score": None,
        "current_strain": None,
    }

    # 1. GET RECOVERY (Fetch up to 10 entries to skip pending/null records)
    try:
        rec_res = requests.get(f"{BASE_URL}/recovery?limit=10", headers=headers)
        if rec_res.status_code == 200:
            records = rec_res.json().get("records", [])
            for rec in records:
                score = rec.get("score")
                # Ensure record is scored and score object exists
                if score and rec.get("score_state") == "SCORED":
                    data_packet["hrv"] = score.get("hrv_rmssd_milli")
                    data_packet["resting_hr"] = score.get("resting_heart_rate")
                    data_packet["recovery_score"] = score.get("recovery_score")
                    break
        else:
            print(f"⚠️ Recovery API returned status {rec_res.status_code}: {rec_res.text}")
    except Exception as e:
        print(f"⚠️ Recovery fetch error: {e}")

    # 2. GET CYCLE (Strain)
    try:
        cycle_res = requests.get(f"{BASE_URL}/cycle?limit=5", headers=headers)
        if cycle_res.status_code == 200:
            records = cycle_res.json().get("records", [])
            for cycle in records:
                score = cycle.get("score")
                if score:
                    data_packet["current_strain"] = score.get("strain")
                    break
        else:
            print(f"⚠️ Cycle API returned status {cycle_res.status_code}: {cycle_res.text}")
    except Exception as e:
        print(f"⚠️ Cycle fetch error: {e}")

    return data_packet


def initialize_csv():
    """Ensure destination CSV structure exists."""
    if not os.path.exists(CSV_FILE_PATH):
        os.makedirs(os.path.dirname(CSV_FILE_PATH), exist_ok=True)
        with open(CSV_FILE_PATH, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "timestamp",
                    "hrv_rmssd",
                    "resting_hr",
                    "recovery_score",
                    "current_strain",
                ]
            )
        print(f"📁 Created data file: {CSV_FILE_PATH}")


def main():
    print("🚀 Starting WHOOP 5.0 Data Harvester (24/7 Mode)")
    initialize_csv()

    tokens = load_stored_tokens()
    if not tokens:
        if not AUTH_CODE:
            print("❌ No Auth Code found in .env.")
            return
        tokens = get_initial_token()

    print("📡 Polling WHOOP Cloud API every 60 seconds...")

    while True:
        try:
            tokens = get_valid_access_token(tokens)
            data = fetch_latest_data(tokens["access_token"])

            with open(CSV_FILE_PATH, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        data["timestamp"],
                        data["hrv"],
                        data["resting_hr"],
                        data["recovery_score"],
                        data["current_strain"],
                    ]
                )

            t_str = datetime.now().strftime("%H:%M:%S")
            print(f"[{t_str}] 💾 Saved: Recovery {data['recovery_score']}% | Strain {data['current_strain']}")

            time.sleep(60)

        except KeyboardInterrupt:
            print("\n🛑 Stopping Harvester.")
            break
        except Exception as e:
            print(f"❌ Unexpected Error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()