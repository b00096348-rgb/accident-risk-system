"""Deterministic features from unique past sensor observations, with stable names."""

import math
import numpy as np

from accident_risk.processing.windows import Window


def series(window: Window, field: str) -> tuple[np.ndarray, np.ndarray]:
    """Deduplicate carried readings by original sensor timestamp, excluding pre-window data."""
    readings = {}
    for frame in window.frames:
        value = getattr(frame, field)
        observed = frame.sensor_timestamps.get(field, frame.timestamp)
        if value is not None and window.start <= observed <= window.end:
            readings[observed] = value
    times = np.array(sorted(readings), dtype=float)
    return times, np.array([readings[t] for t in times], dtype=float)


def statistics(name: str, times: np.ndarray, values: np.ndarray) -> dict[str, float]:
    """Population standard deviation and least-squares slope per second; NaN if absent."""
    keys = ("mean", "std", "min", "max", "delta", "slope")
    if len(values) == 0:
        return {f"{name}_{key}": float("nan") for key in keys}
    centered = times - times.mean()
    slope = (
        float(np.dot(centered, values - values.mean()) / np.dot(centered, centered))
        if np.dot(centered, centered) > 0
        else float("nan")
    )
    return dict(
        zip(
            (f"{name}_{key}" for key in keys),
            [
                float(values.mean()),
                float(values.std()),
                float(values.min()),
                float(values.max()),
                float(values[-1] - values[0]),
                slope,
            ],
            strict=True,
        )
    )


def vector_series(window: Window, prefix: str) -> tuple[np.ndarray, np.ndarray]:
    """Only form vectors from genuinely simultaneous axis observations."""
    axes = [dict(zip(*series(window, f"{prefix}_{axis}"), strict=True)) for axis in "xyz"]
    common = sorted(set(axes[0]) & set(axes[1]) & set(axes[2]))
    return np.array(common), np.array([[a[t] for a in axes] for t in common]).reshape(-1, 3)


def vehicle_features(
    window: Window, longitudinal_axis: str | None = None, lateral_axis: str | None = None
) -> dict[str, float]:
    """Extract kinematics and OBD summaries without assuming a mounting orientation.

    Vehicle speed takes priority at each observation time; GPS fills missing times.
    Raw acceleration includes gravity unless the upstream device has removed it.
    """
    gps = dict(zip(*series(window, "gps_speed"), strict=True))
    gps.update(dict(zip(*series(window, "vehicle_speed"), strict=True)))
    times = np.array(sorted(gps))
    features = statistics("speed", times, np.array([gps[t] for t in times]))
    for name in (
        "accel_x",
        "accel_y",
        "accel_z",
        "gyro_x",
        "gyro_y",
        "gyro_z",
        "rpm",
        "throttle_position",
        "engine_load",
    ):
        features.update(statistics(name, *series(window, name)))
    for prefix, name in (("accel", "acceleration_magnitude"), ("gyro", "gyroscope_magnitude")):
        t, vectors = vector_series(window, prefix)
        features.update(statistics(name, t, np.linalg.norm(vectors, axis=1)))
        if prefix == "accel":
            jerk = (
                np.linalg.norm(np.diff(vectors, axis=0) / np.diff(t)[:, None], axis=1)
                if len(t) > 1
                else np.array([])
            )
            features.update(statistics("jerk", t[1:], jerk))
    for name, axis in (
        ("longitudinal_acceleration", longitudinal_axis),
        ("lateral_acceleration", lateral_axis),
    ):
        features.update(
            statistics(name, *series(window, axis))
            if axis
            else statistics(name, np.array([]), np.array([]))
        )
    features["gyro_variance"] = features["gyroscope_magnitude_std"] ** 2
    # z-axis angular-rate delta is not physical yaw without known mounting.
    features["device_z_rotation_rate_change"] = features["gyro_z_delta"]

    # --- ROUNDABOUT CONTEXT LOGIC ---
    latest_frame = window.frames[-1] if window.frames else None
    in_roundabout = (
        1.0 if (latest_frame and getattr(latest_frame, "is_roundabout", False)) else 0.0
    )
    features["is_roundabout"] = in_roundabout

    # If jerk is low (< 4 m/s^3) during a roundabout turn, flag for false-positive suppression
    max_jerk = features.get("jerk_max", 0.0)
    features["roundabout_risk_suppressor"] = (
        1.0 if (in_roundabout == 1.0 and (math.isnan(max_jerk) or max_jerk < 4.0)) else 0.0
    )

    return features
