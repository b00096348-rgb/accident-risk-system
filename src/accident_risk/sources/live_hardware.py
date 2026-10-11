import asyncio
import threading
import time

import numpy as np
import obd
from bleak import BleakClient, BleakScanner

from accident_risk.sources.live_buffer import LiveBuffer

HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


class HardwareFrame(dict):
    """Dictionary subclass providing stream_id metadata expected by LiveBuffer."""

    def __init__(self, data: dict, stream_id: str = "hardware"):
        super().__init__(data)
        self.stream_id = stream_id


class RealtimeHardwareSource:
    def __init__(self, live_buffer: LiveBuffer, obd_port: str = "/dev/cu.OBDII"):
        self.buffer = live_buffer
        self.obd_port = obd_port
        self.running = False

        self._lock = threading.Lock()
        self.bpm_buffer = []
        self.rr_buffer = []

        self.latest_vitals = {
            "mean_hr_30s": 70.0,
            "rmssd_30s": 50.0,
            "sdnn_30s": 45.0,
            "pnn50_30s": 15.0,
        }
        self.latest_obd = {
            "speed": 0.0,
            "rpm": 0.0,
            "throttle_position": 0.0,
            "engine_load_value": 0.0,
            "acceleration": 0.0,
        }

    def _obd_loop(self):
        conn = None
        try:
            conn = obd.OBD(portstr=self.obd_port, baudrate=38400, timeout=2.0)
        except Exception:
            pass

        prev_speed_mps, prev_time = None, None

        while self.running:
            if conn and conn.is_connected():
                try:
                    now = time.time()
                    s = conn.query(obd.commands.SPEED)
                    r = conn.query(obd.commands.RPM)
                    t = conn.query(obd.commands.THROTTLE_POS)
                    engine_load = conn.query(obd.commands.ENGINE_LOAD)

                    speed = s.value.magnitude if not s.is_null() else 0.0
                    rpm = r.value.magnitude if not r.is_null() else 0.0
                    throttle = t.value.magnitude if not t.is_null() else 0.0
                    load = (
                        engine_load.value.magnitude
                        if not engine_load.is_null()
                        else 0.0
                    )

                    speed_mps = speed * (1000.0 / 3600.0)
                    accel = 0.0
                    if prev_speed_mps is not None and prev_time is not None:
                        dt = now - prev_time
                        if dt > 0:
                            accel = (speed_mps - prev_speed_mps) / dt

                    prev_speed_mps, prev_time = speed_mps, now

                    with self._lock:
                        self.latest_obd = {
                            "speed": round(speed, 2),
                            "rpm": round(rpm, 2),
                            "throttle_position": round(throttle, 2),
                            "engine_load_value": round(load, 2),
                            "acceleration": round(accel, 3),
                        }
                except Exception:
                    pass

            now = time.time()
            with self._lock:
                obd_copy = dict(self.latest_obd)
                vitals_copy = dict(self.latest_vitals)

            payload = {"timestamp": now, **obd_copy, **vitals_copy}
            self.buffer.push(HardwareFrame(payload, stream_id="hardware"))
            time.sleep(0.2)

        if conn:
            try:
                conn.close()
            except Exception:
                pass

    def _parse_ble(self, sender, data: bytearray):
        flags = data[0]
        offset = 2 if (flags & 0x01) == 0 else 3
        bpm = data[1] if (flags & 0x01) == 0 else ((data[2] << 8) | data[1])

        with self._lock:
            if bpm > 0:
                self.bpm_buffer.append(bpm)

            if flags & 0x10:
                while offset + 1 < len(data):
                    rr_raw = (data[offset + 1] << 8) | data[offset]
                    rr_ms = (rr_raw / 1024.0) * 1000.0
                    if 300.0 <= rr_ms <= 2000.0:
                        self.rr_buffer.append(rr_ms)
                    offset += 2

    async def _ble_loop(self):
        try:
            devices = await BleakScanner.discover()
            whoop_dev = next(
                (d for d in devices if d.name and "WHOOP" in d.name.upper()),
                None,
            )
            if not whoop_dev:
                return

            async with BleakClient(whoop_dev.address) as client:
                await client.start_notify(
                    HEART_RATE_MEASUREMENT_UUID, self._parse_ble
                )
                while self.running:
                    await asyncio.sleep(15)
                    with self._lock:
                        if self.bpm_buffer:
                            mean_hr = float(np.mean(self.bpm_buffer))
                            rmssd, sdnn, pnn50 = 50.0, 45.0, 15.0
                            if len(self.rr_buffer) > 1:
                                sdnn = float(np.std(self.rr_buffer))
                                diffs = np.abs(np.diff(self.rr_buffer))
                                rmssd = float(np.sqrt(np.mean(diffs**2)))
                                pnn50 = float(
                                    (np.sum(diffs > 50.0) / len(diffs)) * 100.0
                                )

                            self.latest_vitals = {
                                "mean_hr_30s": round(mean_hr, 2),
                                "rmssd_30s": round(rmssd, 2),
                                "sdnn_30s": round(sdnn, 2),
                                "pnn50_30s": round(pnn50, 2),
                            }
                            self.bpm_buffer.clear()
                            self.rr_buffer.clear()
        except Exception:
            pass

    def start(self):
        self.running = True
        threading.Thread(target=self._obd_loop, daemon=True).start()
        threading.Thread(
            target=lambda: asyncio.run(self._ble_loop()), daemon=True
        ).start()