"""Stream real hardware observations to the shared API; no simulated defaults.

This gateway does not write or modify data/raw.
"""

from accident_risk.sources.hardware import main

if __name__ == "__main__":
    main()
