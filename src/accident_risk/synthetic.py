"""SYNTHETIC SOFTWARE TEST FIXTURES ONLY. No scientific evaluation evidence."""

from pathlib import Path
from typing import Any

import pandas as pd


def write_synthetic_fixture(directory: str | Path) -> dict[str, Any]:
    """Write deliberately invented TEST columns and their mapping outside data/raw.

    These names do not describe VZCrash or POLIDriving. Each participant has
    independent synthetic normal/crash events, allowing group-disjoint tests.
    """
    directory = Path(directory)
    if "raw" in directory.parts:
        raise ValueError("Synthetic fixtures must not be written into real raw datasets")
    input_path = directory / "input"
    input_path.mkdir(parents=True, exist_ok=True)
    rows = []
    for participant in range(10):
        for event in range(2):
            for tick in range(8):
                rows.append(
                    {
                        "test_epoch_s": 1700000000 + participant * 100 + event * 10 + tick,
                        "test_event": f"test-{participant}-{event}",
                        "test_person": f"test-{participant}",
                        "test_speed_mps": 10 + tick * (event + 1),
                        "test_accel_x": event * 3 + tick * 0.1,
                        "test_accel_y": 0,
                        "test_accel_z": 9.80665,
                        "test_annotation": "test_positive" if event else "test_negative",
                    }
                )
    pd.DataFrame(rows).to_csv(input_path / "SYNTHETIC_ONLY.csv", index=False)
    return {
        "random_seed": 42,
        "paths": {"artifacts": str(directory / "models")},
        "pipeline": {
            "vehicle_window_seconds": 3,
            "physiology_window_seconds": 6,
            "stride_seconds": 1,
        },
        "dataset": {
            "name": "vzcrash",
            "path": str(input_path),
            "mapping": {
                "confirmed": True,
                "files": ["SYNTHETIC_ONLY.csv"],
                "timestamp_column": "test_epoch_s",
                "timestamp_unit": "s",
                "stream_column": "test_event",
                "columns": {
                    "vehicle_speed": "test_speed_mps",
                    "accel_x": "test_accel_x",
                    "accel_y": "test_accel_y",
                    "accel_z": "test_accel_z",
                },
                "units": {
                    "vehicle_speed": "m/s",
                    "accel_x": "m/s^2",
                    "accel_y": "m/s^2",
                    "accel_z": "m/s^2",
                },
                "groups": {"participant": "test_person", "event": "test_event"},
                "label_column": "test_annotation",
                "label_map": {"test_positive": "crash", "test_negative": "normal"},
                "label_interpretation": "SYNTHETIC SOFTWARE TEST ONLY; not actual VZCrash schema or outcomes",
            },
        },
        "training": {
            "models": ["logistic_regression", "random_forest", "xgboost"],
            "target": "event_detection_score",
            "score_labels": ["crash"],
            "class_weight": "balanced",
            "selection_metric": "pr_auc",
            "test_size": 0.2,
            "validation_size": 0.2,
        },
        "synthetic_test_only": True,
    }
