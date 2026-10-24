"""Validated configuration with relative YAML inheritance and environment overrides."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PipelineSettings(BaseModel):
    """Timing in seconds; staleness is independent for every sensor."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    vehicle_window_seconds: float = Field(10, gt=0)
    physiology_window_seconds: float = Field(60, gt=0)
    stride_seconds: float = Field(1, gt=0)
    min_samples: int = Field(2, ge=1)
    prediction_horizon_seconds: float = Field(0, ge=0)
    default_max_age_seconds: float = Field(2, gt=0)
    max_age_seconds: dict[str, float] = Field(
        default_factory=lambda: {
            "heart_rate": 30,
            "spo2": 60,
            "hrv_rmssd": 60,
            "rr_interval_ms": 5,
            "physiology_signal_quality": 60,
            "gps_speed": 5,
            "gps_latitude": 5,
            "gps_longitude": 5,
        }
    )
    physiology_quality_threshold: float = Field(0.5, ge=0, le=1)
    spo2_drop_threshold: float = Field(3, gt=0)
    longitudinal_axis: str | None = None
    lateral_axis: str | None = None
    quality_fields: list[str] = Field(
        default_factory=lambda: ["accel_x", "accel_y", "accel_z", "vehicle_speed"]
    )

    @model_validator(mode="after")
    def validate_sensors(self) -> "PipelineSettings":
        """Reject misspelled channels and invalid per-sensor limits."""
        from accident_risk.schemas import SENSOR_FIELDS

        for axis in (self.longitudinal_axis, self.lateral_axis):
            if axis is not None and axis not in {"accel_x", "accel_y", "accel_z"}:
                raise ValueError("Mounting axes must name accelerometer fields")
        if any(
            k not in SENSOR_FIELDS or not 0 < v < float("inf")
            for k, v in self.max_age_seconds.items()
        ):
            raise ValueError("Invalid sensor staleness settings")
        if not self.quality_fields or set(self.quality_fields) - set(SENSOR_FIELDS):
            raise ValueError("quality_fields must name at least one known sensor")
        return self


class EnvironmentSettings(BaseSettings):
    """Runtime paths may be supplied via ACCIDENT_RISK_* or .env."""

    model_config = SettingsConfigDict(env_prefix="ACCIDENT_RISK_", env_file=".env", extra="ignore")
    config: Path = Path("configs/base.yaml")
    vehicle_model: Path | None = None
    physiology_model: Path | None = None
    database: Path = Path("data/app.sqlite3")
    bootstrap_admin_phone: str | None = None
    bootstrap_admin_name: str = "Administrator"
    expose_login_codes: bool = True


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings, replacing scalar/list values."""
    result = dict(base)
    for key, value in override.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def load_config(path: str | Path, _seen: set[Path] | None = None) -> dict[str, Any]:
    """Load YAML, resolving `extends` relative to its file; reject cycles."""
    path = Path(path).resolve()
    seen = set() if _seen is None else _seen
    if path in seen:
        raise ValueError("Configuration inheritance cycle")
    seen.add(path)
    config = yaml.safe_load(path.read_text()) or {}
    parent = config.pop("extends", None)
    if parent:
        config = merge(load_config(path.parent / parent, seen), config)
    PipelineSettings(**config.get("pipeline", {}))
    return config
