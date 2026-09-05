#!/usr/bin/env python3
"""Compatibility launcher for the packaged ``zc360d`` daemon."""

import sys
from pathlib import Path

source = Path(__file__).resolve().parent / "src"
if str(source) not in sys.path:
    sys.path.insert(0, str(source))

from zc360.daemon import main


if __name__ == "__main__":
    main()
