"""Stream real hardware observations to the shared API; no simulated defaults.

Readings are saved to per-run CSVs under data/recordings while streaming.
Existing recordings and data/raw are left untouched.
"""

from accident_risk.sources.hardware import main

if __name__ == "__main__":
    main()
