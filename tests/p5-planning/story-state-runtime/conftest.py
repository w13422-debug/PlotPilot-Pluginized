import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for source in (
    ROOT / "backend",
    ROOT / "first-party-plugins/prompt-skill-runtime/src",
    ROOT / "first-party-plugins/story-state/src",
):
    sys.path.insert(0, str(source))
