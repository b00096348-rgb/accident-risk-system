# Live dashboard and hardware setup

Run from the repository root, with `.venv` activated. Install optional hardware and development dependencies using `python -m pip install -e '.[hardware,dev]'`.

Terminal 1 runs the dashboard:

```sh
python scripts/serve.py
```

Open http://127.0.0.1:8000. Live sensors is the default; Demo values stays entirely in the browser and never posts fabricated packets to the API.

Terminal 2 runs selected collectors. Replace the serial-port argument with your actual paired adapter path:

```sh
python scripts/run_live_terminal.py --obd-port /dev/cu.OBDII --whoop
```

For one device alone, use `python scripts/test_obd.py --obd-port /dev/cu.OBDII` or `python scripts/stream_whoop_ble_csv.py`. The latter filename is retained for compatibility but now streams observations only; it no longer writes CSVs or cached HRV defaults. Collectors do not write into data/raw. They do not require model files. Dependencies are imported only when their collector is selected. OBD and WHOOP reconnect attempts are logged; failed API delivery is reported and never replaced with fake data.

## Optional phone

The phone receiver is a separate, explicitly enabled port. On a trusted local network:

```sh
python scripts/run_live_terminal.py --phone-host 0.0.0.0 --phone-port 8003
```

Point the phone at `http://YOUR_MAC_LAN_IP:8003/ingest`, not localhost on the phone. This receiver is unauthenticated; do not expose it to the internet. It forwards to the same dashboard API on 8000. It may be combined with `--obd-port` and `--whoop` in one command. All collectors should use the same `--stream-id` for one trip. Do not run duplicate collectors for the same sensor.

Accepted phone formats: canonical SensorFrame JSON with Unix-second timestamps, or the existing Sensor Logger payload convention with `payload` entries containing `name`, Unix-nanosecond `time`, and `values`. Supported entry names are location, accelerometer, and gyroscope. Confirm the phone application's units and timestamp export format before use. Location is degrees, acceleration is m/s², and angular velocity is rad/s. No location is inferred from an IP address; zero latitude/longitude is a valid observation.

## Units and freshness

The API now enforces the existing SensorFrame contract: `timestamp`, `source`, and `stream_id` are required, and unknown field names are rejected. `vehicle_speed` is m/s, `engine_load` is percent, GPS fields are `gps_latitude` and `gps_longitude`. The gateway converts OBD speed to m/s; only the browser converts it to km/h. Old ad-hoc payloads with `speed`, `engine_load_value`, or date strings must be migrated.

Preserve original measurement times in `sensor_timestamps` when forwarding a reading. A newly delivered old packet does not make a sensor fresh. Per-channel expiry is controlled by configs/base.yaml (by default 2 seconds for vehicle/IMU, 5 for GPS, 30 for HR, and 60 for SpO2/HRV). Missing readings render as dashes. BLE HR broadcasts alone do not establish SpO2 or signal quality. The current BLE collector forwards the latest actual RR interval in each notification, not every interval in a multi-interval batch; no full RR-series fidelity is claimed.

## Model status

Telemetry works without a model. Models must be produced by the grouped, mapped training pipeline and explicitly configured with ACCIDENT_RISK_VEHICLE_MODEL or ACCIDENT_RISK_PHYSIOLOGY_MODEL. Training, replay, and live ingestion now share the same FeaturePipeline and saved preprocessing contract. Models are loaded once at server startup. Never load an untrusted joblib artifact.

The old train_fusion.py heuristic-label experiment is disabled: it generated targets from input thresholds, backfilled future measurements, randomly split neighboring samples, and selected a model on the test set. Existing artifacts remain untouched but are no longer loaded by the live dashboard. Independent labels, appropriate group splits, and real held-out evaluation are still required before making performance claims. A research score is not a crash probability.

## Validation limits

Offline tests cover schema rejection, original-time expiry, missing/zero readings, BLE packet parsing, mocked OBD unit conversion, and API delivery. These do not verify a real car, serial adapter, wearable broadcast, or phone export. Test the actual device connection while parked before collecting a drive.

## macOS OpenMP and tests

This Mac does not have a system libomp installation. The installed PyTorch package includes a runtime that passed the XGBoost tests when placed on the child process's library search path. Use the included launcher for XGBoost commands or the complete test suite:

```sh
python scripts/with_openmp.py -m pytest -q
python scripts/with_openmp.py scripts/serve.py
```

The launcher does not import PyTorch or change shell/system settings. XGBoost and PyTorch computation still run in separate processes. If the bundled runtime is unavailable, install libomp using your package manager. The ordinary `python scripts/serve.py` command works without a model and for models that do not require XGBoost.
