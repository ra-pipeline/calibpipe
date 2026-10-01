#!/usr/bin/env python3
"""Legacy entrypoint for calibPipeIF.py forwarding to calibpipe.driver."""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to sys.path if running from checkout
_src = Path(__file__).resolve().parent.parent / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from calibpipe.driver import main

if __name__ == "__main__":
    main(sys.argv[1:])
