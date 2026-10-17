"""Retired heuristic-label training entry point.

The former experiment labeled its own inputs, backfilled future observations,
randomly split neighboring rows, and selected models against the test set.
Those artifacts cannot substantiate accident prediction and are not loaded by
live serving. Use the mapped, grouped training pipeline with independent labels.
"""


def train_and_evaluate_fusion() -> None:
    raise RuntimeError(
        "Heuristic fusion training is disabled. Use scripts/train_vehicle.py or "
        "scripts/train_physiology.py with an inspected mapping, independent labels, "
        "and group-separated data. Existing fusion artifacts are left untouched."
    )


if __name__ == "__main__":
    train_and_evaluate_fusion()
