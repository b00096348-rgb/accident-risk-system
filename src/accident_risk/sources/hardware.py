"""Optional hardware gateway. Send only observed, SI-unit SensorFrames to the API."""

import argparse
import asyncio
import json
import logging
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from accident_risk.schemas import SENSOR_FIELDS, SensorFrame
from accident_risk.sources.recording import CSVRecorder

LOG = logging.getLogger(__name__)
HEART_RATE_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


class Publisher:
    """Serialize modalities while preserving original observation times."""

    def __init__(self, url: str, stream_id: str, recorder: CSVRecorder | None = None) -> None:
        self.recorder = recorder
        self.url = url
        self.stream_id = stream_id
        self.lock = threading.Lock()

    def send(self, source: str, values: dict, observed: float | dict) -> bool:
        """Report failures; never substitute zero or refresh cached sensor ages."""
        values = {key: value for key, value in values.items() if value is not None}
        if not values:
            return False
        try:
            with self.lock:
                times = observed if isinstance(observed, dict) else dict.fromkeys(values, observed)
                frame = SensorFrame(
                    timestamp=time.time(),
                    stream_id=self.stream_id,
                    source=source,
                    sensor_timestamps=times,
                    **values,
                )
                if self.recorder is not None:
                    try:
                        self.recorder.write(frame)
                    except Exception as exc:
                        LOG.error("CSV RECORDING FAILED for %s: %s", source, exc)
                request = Request(
                    self.url,
                    data=frame.model_dump_json().encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=3) as response:
                    response.read()
            LOG.info("Sent %s: %s", source, values)
            return True
        except Exception as exc:
            LOG.warning("%s packet not delivered: %s", source, exc)
            return False


def parse_heart_rate(data: bytes) -> tuple[int | None, list[float]]:
    """Decode the standard BLE Heart Rate Measurement flags, including energy expended."""
    if len(data) < 2:
        raise ValueError("Truncated heart-rate measurement")
    flags = data[0]
    width = 2 if flags & 1 else 1
    offset = 1 + width
    if len(data) < offset:
        raise ValueError("Truncated heart-rate value")
    bpm = int.from_bytes(data[1:offset], "little")
    if flags & 8:
        offset += 2
    if len(data) < offset or (flags & 16 and (len(data) - offset) % 2):
        raise ValueError("Truncated BLE optional fields")
    rr = []
    if flags & 16:
        for index in range(offset, len(data), 2):
            interval = int.from_bytes(data[index : index + 2], "little") * 1000 / 1024
            if 0 < interval <= 10000:
                rr.append(interval)
    # Sensor-contact-supported and contact-not-detected: no usable measurement.
    if flags & 4 and not flags & 2:
        return None, []
    return bpm if 0 < bpm <= 300 else None, rr


def phone_values(data: dict) -> tuple[dict, dict]:
    """Accept canonical SensorFrame JSON or Sensor Logger's explicit ns timestamps.

    Sensor Logger uses Unix nanoseconds in each entry's `time`. Direct JSON must
    provide Unix seconds and canonical sensor names. No IP geolocation fallback.
    """
    if "payload" not in data:
        frame = SensorFrame.model_validate(data)
        values = {
            name: getattr(frame, name) for name in SENSOR_FIELDS if getattr(frame, name) is not None
        }
        times = {name: frame.sensor_timestamps.get(name, frame.timestamp) for name in values}
        return values, times
    names = {
        "location": {"latitude": "gps_latitude", "longitude": "gps_longitude"},
        "gyroscope": {axis: f"gyro_{axis}" for axis in "xyz"},
        "accelerometer": {axis: f"accel_{axis}" for axis in "xyz"},
    }
    values, times = {}, {}
    if not isinstance(data["payload"], list):
        raise ValueError("payload must be a list")
    for entry in data["payload"]:
        if entry.get("name") not in names:
            continue
        stamp = entry.get("time")
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (float, int))
            or not math.isfinite(stamp)
        ):
            raise ValueError("Sensor Logger entry requires Unix nanoseconds in time")
        observed = stamp / 1e9
        if observed <= 0 or observed > time.time() + 1:
            raise ValueError("Invalid phone observation time")
        for source, target in names[entry["name"]].items():
            value = entry.get("values", {}).get(source)
            if value is not None and observed >= times.get(target, -1):
                values[target], times[target] = value, observed
    if not values:
        raise ValueError("No supported phone measurements")
    SensorFrame(
        timestamp=time.time(),
        source="phone",
        stream_id="validation",
        sensor_timestamps=times,
        **values,
    )
    return values, times


def phone_server(publisher: Publisher, host: str, port: int) -> ThreadingHTTPServer:
    """Create a dedicated phone receiver, separate from the dashboard port."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/ingest":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 1_000_000:
                    raise ValueError("Invalid payload size")
                values, times = phone_values(json.loads(self.rfile.read(length)))
                ok = publisher.send("phone", values, times)
                self.send_response(200 if ok else 502)
                self.end_headers()
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                self.send_error(422, str(exc))

        def log_message(self, format: str, *args) -> None:
            LOG.debug(format, *args)

    return ThreadingHTTPServer((host, port), Handler)


def obd_loop(publisher: Publisher, port: str, stop: threading.Event) -> None:
    """Read supported OBD channels; reconnect without fabricating missing readings."""
    import obd

    channels = [
        (obd.commands.SPEED, "vehicle_speed", "m/s"),
        (obd.commands.RPM, "rpm", "rpm"),
        (obd.commands.THROTTLE_POS, "throttle_position", "percent"),
        (obd.commands.ENGINE_LOAD, "engine_load", "percent"),
    ]
    while not stop.is_set():
        connection = None
        try:
            connection = obd.OBD(portstr=port, baudrate=38400, fast=False, timeout=2)
            if not connection.is_connected():
                raise ConnectionError(f"No ECU connection on {port}")
            while not stop.is_set() and connection.is_connected():
                values, times = {}, {}
                for command, field, unit in channels:
                    response = connection.query(command)
                    if not response.is_null():
                        values[field] = float(response.value.to(unit).magnitude)
                        times[field] = time.time()
                if values:
                    publisher.send("obd", values, times)
                stop.wait(0.5)
        except Exception as exc:
            LOG.warning("OBD unavailable: %s", exc)
        finally:
            if connection is not None:
                connection.close()
        stop.wait(5)


async def wearable_loop(publisher: Publisher, stop: threading.Event) -> None:
    """Publish actual BLE HR and RR only; window features belong to the shared pipeline."""
    from bleak import BleakClient, BleakScanner

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    def notification(sender, data: bytearray) -> None:
        try:
            bpm, rr = parse_heart_rate(bytes(data))
            values = {}
            if bpm is not None:
                values["heart_rate"] = bpm
            # The most recent RR in this notification; never derive RR from BPM.
            if rr:
                values["rr_interval_ms"] = rr[-1]
            if values:
                queue.put_nowait((values, time.time()))
        except (ValueError, asyncio.QueueFull) as exc:
            LOG.warning("BLE packet rejected: %s", exc)

    while not stop.is_set():
        try:
            devices = await BleakScanner.discover()
            device = next((d for d in devices if "WHOOP" in (d.name or "").upper()), None)
            if device is None:
                raise ConnectionError("WHOOP not found; enable heart-rate broadcast")
            async with BleakClient(device.address) as client:
                await client.start_notify(HEART_RATE_UUID, notification)
                while client.is_connected and not stop.is_set():
                    try:
                        values, observed = await asyncio.wait_for(queue.get(), timeout=1)
                        await asyncio.to_thread(publisher.send, "wearable", values, observed)
                    except TimeoutError:
                        continue
        except Exception as exc:
            LOG.warning("Wearable unavailable: %s", exc)
        while not queue.empty():
            queue.get_nowait()
        for _ in range(5):
            if stop.is_set():
                return
            await asyncio.sleep(1)


def main(default_obd: bool = False, default_whoop: bool = False) -> None:
    """Run the selected collectors without starting another dashboard or requiring a model."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000/ingest")
    parser.add_argument("--stream-id", default="hardware")
    parser.add_argument("--obd-port", default="/dev/cu.OBDII" if default_obd else None)
    parser.add_argument("--whoop", action="store_true", default=default_whoop)
    parser.add_argument("--phone-port", type=int, help="Optional phone receiver, e.g. 8003")
    parser.add_argument("--phone-host", default="127.0.0.1")
    parser.add_argument(
        "--record-dir",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "data" / "recordings",
        help="CSV parent directory; each run creates a new session folder (default: data/recordings)",
    )
    parser.add_argument("--no-record", action="store_true", help="Explicitly disable CSV recording")
    args = parser.parse_args()
    if not (args.obd_port or args.whoop or args.phone_port):
        parser.error("Select --obd-port, --whoop, or --phone-port")
    from urllib.parse import urlparse

    if args.phone_port == (urlparse(args.api).port or 80):
        parser.error("Phone receiver and dashboard must use different ports")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    recorder = None if args.no_record else CSVRecorder(args.record_dir)
    if recorder:
        LOG.info("CSV recording enabled: %s (one file per source)", recorder.directory)
    else:
        LOG.warning("CSV recording disabled by --no-record")
    publisher = Publisher(args.api, args.stream_id, recorder)
    stop = threading.Event()
    threads = []
    server = None
    if args.obd_port:
        threads.append(
            threading.Thread(target=obd_loop, args=(publisher, args.obd_port, stop), daemon=True)
        )
    if args.whoop:
        threads.append(
            threading.Thread(
                target=lambda: asyncio.run(wearable_loop(publisher, stop)), daemon=True
            )
        )
    if args.phone_port:
        server = phone_server(publisher, args.phone_host, args.phone_port)
        threads.append(threading.Thread(target=server.serve_forever, daemon=True))
        LOG.info("Phone receiver: http://%s:%s/ingest", args.phone_host, args.phone_port)
    try:
        for thread in threads:
            thread.start()
        while all(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if server:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=4)
