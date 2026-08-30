from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "first-party-plugins" / "prompt-skill-runtime" / "src"))
sys.path.insert(0, str(ROOT / "backend"))
