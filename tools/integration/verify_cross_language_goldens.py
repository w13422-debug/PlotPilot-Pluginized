"""Cross-check Python SDK output with the independent Node verifier."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from verify_contracts import verify_goldens  # type: ignore  # noqa: E402


def main() -> int:
    completed = subprocess.run(
        ["node", str(ROOT / "tools" / "integration" / "verify_contracts.mjs"), "--all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        return completed.returncode
    node = json.loads(completed.stdout)
    python = verify_goldens()
    expected = {
        "package_hash": node["package"]["package_hash"],
        "release_id": node["package"]["release_id"],
        "skill_package_hash": node["skill"]["skill_package_hash"],
        "skill_release_id": node["skill"]["skill_release_id"],
        "request_key": node["snapshot"]["request_key"],
        "snapshot_hash": node["snapshot"]["snapshot_hash"],
        "backup_hash": node["backup"]["backup_hash"],
    }
    actual = {key: python[key] for key in ("package_hash", "skill_package_hash", "request_key", "snapshot_hash", "backup_hash")}
    # The Python golden summary does not repeat release IDs; read them from
    # the expected vectors so this comparison covers both package identities.
    package_expected = json.loads((ROOT / "contracts/golden/package/expected.json").read_text(encoding="utf-8"))
    skill_expected = json.loads((ROOT / "contracts/golden/skill/expected.json").read_text(encoding="utf-8"))
    actual.update({"release_id": package_expected["release_id"], "skill_release_id": skill_expected["skill_release_id"]})
    if actual != expected:
        raise SystemExit(f"cross-language mismatch:\npython={actual}\nnode={expected}")
    print(json.dumps({"status": "ok", "python": actual, "node": expected}, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
