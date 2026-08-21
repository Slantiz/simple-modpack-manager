#!/usr/bin/env python3
"""Zero-install launcher: `py modman.py <command> ...`

Adds ``src/`` to the import path and runs the CLI. If you prefer a real
``modman`` command, run ``pip install -e .`` instead (see pyproject.toml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from modman.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
