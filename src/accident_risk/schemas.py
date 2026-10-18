"""Wire schema: timestamps are finite Unix seconds UTC; sensors use documented units."""

# pylint: disable=no-member,not-an-iterable

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Finite = Annotated[float, Field(allow_inf_nan=False)]
Percent = Annotated[Finite, Field(ge=0, le=100)]
Score = Annotated[Finite, Field(ge=0, le=1)]


class SensorFrame(BaseModel):
    """An asynchronous observation. Null means no new reading, never zero.

    Use a unique stream per trip/event and stable units/orientation. Optional
    sensor_timestamps preserve original observation times after causal filling.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    timestamp: Annotated[Finite, Field(ge=0, description="Unix seconds, UTC")]
    stream_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    accel_x: Finite | None = Field(None, description="m/s^2, device x axis")
    accel_y: Finite | None = Field(None, description="m/s^2, device y axis")
    accel_z: Finite | None = Field(None, description="m/s^2, device z axis")
    gyro_x: Finite | None = Field(None, description="rad/s")
    gyro_y: Finite | None = Field(None, description="rad/s")
    gyro_z: Finite | None = Field(None, description="rad/s")
    gps_latitude: Annotated[Finite, Field(ge=-90, le=90)] | None = None
    gps_longitude: Annotated[Finite, Field(ge=-180, le=180)] | None = None
    gps_speed: Annotated[Finite, Field(ge=0)] | None = Field(None, description="m/s")
    gps_heading: Annotated[Finite, Field(ge=0, lt=6.283185307179586)] | None = Field(
        None, description="radians clockwise from true north"
    )
    vehicle_speed: Annotated[Finite, Field(ge=0)] | None = Field(None, description="m/s")
    rpm: Annotated[Finite, Field(ge=0)] | None = Field(None, description="rev/min")
    throttle_position: Percent | None = None
    engine_load: Percent | None = None
    steering_angle: Annotated[
        Finite, Field(ge=-12.566370614359172, le=12.566370614359172)
    ] | None = Field(None, description="radians; positive direction must be documented")
    tire_pressure_fl: Annotated[Finite, Field(gt=0, le=1000)] | None = Field(
        None, description="kPa, front-left"
    )
    tire_pressure_fr: Annotated[Finite, Field(gt=0, le=1000)] | None = Field(
        None, description="kPa, front-right"
    )
    tire_pressure_rl: Annotated[Finite, Field(gt=0, le=1000)] | None = Field(
        None, description="kPa, rear-left"
    )
    tire_pressure_rr: Annotated[Finite, Field(gt=0, le=1000)] | None = Field(
        None, description="kPa, rear-right"
    )
    heart_rate: Annotated[Finite, Field(gt=0, le=300)] | None = Field(None, description="bpm")
    spo2: Percent | None = Field(None, description="percent")
    hrv_rmssd: Annotated[Finite, Field(ge=0)] | None = Field(
        None, description="ms, device-reported"
    )
    rr_interval_ms: Annotated[Finite, Field(gt=0, le=10000)] | None = Field(
        None, description="Actual RR/IBI interval, ms; never derived from BPM"
    )
    physiology_signal_quality: Score | None = None
    is_roundabout: bool | None = Field(None, description="True if vehicle is inside a roundabout")
    sensor_timestamps: dict[str, Annotated[Finite, Field(ge=0)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_observation_times(self) -> "SensorFrame":
        """Disallow unknown sensor times, absent readings, and future observations."""
        for name, timestamp in self.sensor_timestamps.items():
            if name not in SENSOR_FIELDS or getattr(self, name) is None:
                raise ValueError(f"Observation timestamp requires a present sensor: {name}")
            if timestamp > self.timestamp:
                raise ValueError(f"Future observation for {name}")
        return self


SENSOR_FIELDS = tuple(
    name
    for name in SensorFrame.model_fields
    if name not in {"timestamp", "stream_id", "source", "sensor_timestamps"}
)
PHYSIOLOGY_FIELDS = (
    "heart_rate",
    "spo2",
    "hrv_rmssd",
    "rr_interval_ms",
    "physiology_signal_quality",
)


class PredictionResult(BaseModel):
    """Uncalibrated model scores and configurable presentation index; not medical advice."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    timestamp: Annotated[Finite, Field(ge=0, description="Unix seconds, UTC")]
    stream_id: str
    event_detection_score: Score | None = None
    vehicle_risk_score: Score | None = None
    driver_state_risk: Score | None = None
    overall_risk_index: Score | None = None
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None
    data_quality: Score
    model_version: str
    quality_flags: list[str] = Field(default_factory=list)
    sensor_age_seconds: dict[str, float] = Field(default_factory=dict)
    inference_latency_ms: float = Field(ge=0)
    