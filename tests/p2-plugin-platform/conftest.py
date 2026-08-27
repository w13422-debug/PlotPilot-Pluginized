"""Make the P2 test slice self-contained without changing root configuration."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
backend = str(ROOT / "backend")
if backend not in sys.path:
    sys.path.insert(0, backend)
