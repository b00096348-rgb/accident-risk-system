# Android capture app

The phone is the hub. This app owns the two Bluetooth links, the phone's own
sensors, and the buffer that survives a drive through a dead zone. The screens
the driver sees are the same `/app` pages the browser serves, loaded in a
WebView, so the interface is written once.

## Why a native app and not a web page

A browser cannot open a Bluetooth Classic serial connection, and the ELM327
speaks SPP. Web Bluetooth covers BLE only, which would get the wearable and
leave the car unreachable. Nor can the Python pipeline move onto the phone:
xgboost and scipy are not reliably buildable under Termux, torch is not
available at all, and Termux cannot reach the adapter over SPP either. So the
split is fixed: **phone senses, server scores.**

## Building it

Open `android/` in Android Studio (Giraffe or newer) and press Run. Gradle
fetches the dependencies on first build. No SDK components beyond the standard
API 34 platform are needed.

From the command line, with `ANDROID_HOME` set:

```bash
cd android && ./gradlew assembleDebug
```

## First run

The app asks for the server address. Anything reachable from the phone works:

| Where the server is | Address to enter |
| --- | --- |
| Laptop on the same Wi-Fi | `http://192.168.x.x:8000` |
| Android emulator, server on the host | `http://10.0.2.2:8000` (the default) |
| A hosted deployment | `https://your-host` |

Start the server with `--host 0.0.0.0` for anything other than the emulator, or
the phone cannot reach it.

The app then loads `/app` and the driver signs in there with their phone number,
exactly as in the browser. Grant Bluetooth, location and notification
permissions when asked; capture cannot run without them.

Pair the ELM327 and the wearable in Android's own Bluetooth settings first. The
app finds them among bonded devices by name fragment -- `OBD` and `WHOOP` by
default, changeable in `SettingsStore`.

## What happens during a drive

Pressing **Start session** in the web UI creates the session server-side and
hands its `stream_id` to the native layer through a JavaScript bridge. That is
what keeps the two halves in step: there is no stream name to match by hand.

`CaptureService` then runs as a foreground service with a persistent
notification and a partial wake lock. This matters more than it sounds. Android
suspends ordinary background work, which is exactly why the 28 September Car
Scanner recording lost twenty minutes in the middle of a drive. A foreground
service with a wake lock does not get suspended that way.

Every 200 ms the service assembles a frame from whatever is currently fresh:

| Source | Fields | Note |
| --- | --- | --- |
| ELM327 | speed, RPM, throttle, engine load, coolant, STFT, LTFT | Speed and RPM every pass; the rest every eighth |
| Wearable | heart rate, RR interval | Standard BLE Heart Rate Service, so Polar and Garmin work too |
| Phone IMU | accelerometer, gyroscope | |
| Phone GPS | latitude, longitude, speed, heading | |

Only seven PIDs are polled, deliberately. The adapter answers one request at a
time over one serial link, so every extra channel steals sample rate from the
ones the model actually uses.

Frames go to an on-disk SQLite queue first and leave it only once the server has
accepted them. With signal they upload every two seconds; without it they pile
up and go out through `/ingest/batch` when the connection returns. Kill the app,
lose signal for twenty minutes, restart the phone -- the session still completes.
The queue is capped at 200,000 frames and drops oldest-first beyond that.

If the app restarts while a session is open, the page re-arms capture on its
next poll rather than leaving a session recording nothing.

## Units

The server rejects anything that is not SI, and a silent mismatch would poison
the training data rather than fail loudly. The app converts at the edge:

- OBD reports km/h; `vehicle_speed` is published in m/s.
- Android reports bearing in degrees; `gps_heading` is published in radians.
- Acceleration (m/s^2) and rotation (rad/s) are already correct.
- A null field means no new reading, never zero. Nulls are omitted from the JSON
  so the server can age each channel independently.

## Known limits

- One session per phone at a time, by design.
- No on-device risk model. Without a reachable server the drive is recorded in
  full and scored on upload, but the driver sees no live risk. Moving inference
  onto the phone means exporting the model to ONNX Runtime Mobile and mirroring
  the feature pipeline in Kotlin -- and that mirror has to be held identical to
  the Python one, which is a real maintenance hazard rather than a free win.
- Manufacturer battery managers (Samsung, Xiaomi, Huawei) can still kill a
  foreground service. Set the app to **Unrestricted** battery usage and keep it
  out of the sleeping-apps list.
- `usesCleartextTraffic` is enabled so plain-HTTP testing against a laptop
  works. Turn it off once the server is on HTTPS.
