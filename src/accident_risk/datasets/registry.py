"""Extend the dataset registry without changing training or inference."""

from pathlib import Path
from typing import Any

from accident_risk.datasets.base import MappedTabularAdapter
from accident_risk.datasets.physiology import WhoopVitalsDataset
from accident_risk.datasets.polidriving import POLIDrivingAdapter
from accident_risk.datasets.vzcrash import VZCrashAdapter

REGISTRY: dict[str, type[MappedTabularAdapter]] = {
    "vzcrash": VZCrashAdapter,
    "polidriving": POLIDrivingAdapter,
}


def register(name: str, adapter: type[MappedTabularAdapter]) -> None:
    """Register an explicitly named adapter; refuse accidental replacement."""
    if name in REGISTRY:
        raise ValueError(f"Dataset already registered: {name}")
    REGISTRY[name] = adapter


def get_adapter(
    name: str, path: str | Path | None = None, mapping: dict[str, Any] | None = None
) -> MappedTabularAdapter:
    """Resolve a registered dataset with informative unknown-name errors."""
    if name not in REGISTRY:
        raise ValueError(
            f"Unknown dataset {name!r}; available: {sorted(REGISTRY)}. Register an adapter or use --prepared."
        )
    return REGISTRY[name](path, mapping)


DATASET_REGISTRY = {
    # ... existing datasets
    "whoop_vitals": WhoopVitalsDataset,
}