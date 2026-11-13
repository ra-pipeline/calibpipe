#!/usr/bin/env python3
"""CLI runner shim for updating regression test reference files."""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to sys.path if running from repository checkout
_src = Path(__file__).resolve().parent.parent / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from calibpipe.update_ref import main

if __name__ == "__main__":
    sys.exit(main())
