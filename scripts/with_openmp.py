"""Run a Python command with the installed macOS OpenMP runtime, without global changes.

Examples: python scripts/with_openmp.py -m pytest -q
          python scripts/with_openmp.py scripts/serve.py
The optional torch package is inspected on disk, never imported into XGBoost.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Scope the runtime search path to the requested child Python process."""
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    environment = os.environ.copy()
    if sys.platform == "darwin" and not Path("/opt/homebrew/opt/libomp/lib/libomp.dylib").exists():
        spec = importlib.util.find_spec("torch")
        if spec and spec.origin:
            library = Path(spec.origin).parent / "lib"
            if (library / "libomp.dylib").exists():
                existing = environment.get("DYLD_LIBRARY_PATH")
                environment["DYLD_LIBRARY_PATH"] = str(library) + (
                    ":" + existing if existing else ""
                )
    return subprocess.call([sys.executable, *sys.argv[1:]], env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
