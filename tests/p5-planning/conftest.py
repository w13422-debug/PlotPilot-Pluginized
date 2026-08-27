from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for source in (ROOT / "first-party-plugins/project-planner/src", ROOT / "first-party-plugins/story-state/src"):
    sys.path.insert(0, str(source))
