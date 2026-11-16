#!/usr/bin/env python3
"""Thin forwarder from runbatch.py to `calibpipe submit`."""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to sys.path if running from checkout
_src = Path(__file__).resolve().parent.parent / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from calibpipe.cli import main

if __name__ == "__main__":
    sys.exit(main(["submit", *sys.argv[1:]]))
