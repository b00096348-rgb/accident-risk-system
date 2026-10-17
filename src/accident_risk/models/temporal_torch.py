"""Secondary optional small 1D CNN; classical inference never imports torch."""

import sys

if sys.platform == "darwin" and "xgboost" in sys.modules:
    raise RuntimeError(
        "Run the optional PyTorch experiment in a separate process from XGBoost on macOS to avoid conflicting OpenMP runtimes"
    )

import torch
from torch import nn


def select_device() -> torch.device:
    """Prefer Apple MPS, then optional CUDA, and otherwise CPU."""
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


class TemporalCNN(nn.Module):
    """Window classifier: input [batch, channels, past_time], output class logits.

    Windows must end at the prediction cutoff. Padding is left-only, and missing
    samples must be imputed from the training fold plus explicit mask channels.
    No temporal training/benchmark superiority is claimed by this prototype.
    """

    def __init__(self, channels: int, classes: int, hidden: int = 32) -> None:
        super().__init__()
        if channels < 1 or classes < 2 or hidden < 1:
            raise ValueError("Positive channels/hidden and at least two classes required")
        self.network = nn.Sequential(
            nn.ConstantPad1d((4, 0), 0),
            nn.Conv1d(channels, hidden, 5),
            nn.ReLU(),
            nn.ConstantPad1d((2, 0), 0),
            nn.Conv1d(hidden, hidden, 3),
            nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(hidden, classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Compute logits on the caller's selected device; do not label them crash probabilities."""
        return self.network(x)


def create_temporal_model(channels: int, classes: int) -> TemporalCNN:
    """Create a model on the available device, falling back if device initialization fails."""
    model = TemporalCNN(channels, classes)
    try:
        return model.to(select_device())
    except (RuntimeError, NotImplementedError):
        return model.cpu()
