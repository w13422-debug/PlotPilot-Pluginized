from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for plugin in ("export-suite", "chapter-workflow", "autopilot", "quality-suite"):
    source = ROOT / "first-party-plugins" / plugin / "backend" / "src"
    if source.is_dir():
        sys.path.insert(0, str(source))
