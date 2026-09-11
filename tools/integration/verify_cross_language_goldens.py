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

from verify_contracts import (  # type: ignore  # noqa: E402
    verify_goldens,
    verify_macro_planning_host,
    verify_v2_public_surface,
)


def main() -> int:
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(ROOT / "tools" / "integration" / "verify_contracts.mjs"), "--all"],
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
    python_v2 = verify_v2_public_surface()
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
    node_v2 = node.get("v2")
    if node_v2 != python_v2:
        raise SystemExit(f"v2 cross-language mismatch:\npython={python_v2}\nnode={node_v2}")
    python_macro = verify_macro_planning_host()
    node_macro = node.get("macro_planning_host")
    if node_macro != python_macro:
        raise SystemExit(
            f"macro-planning cross-language mismatch:\npython={python_macro}\nnode={node_macro}"
        )
    integer_evidence = {
        key: python_macro.get(key)
        for key in (
            "canonical_integer_fields",
            "integer_vector_count",
            "integer_vector_accepted",
            "integer_vector_rejected",
            "integer_vector_source_sha256",
            "integer_vector_result_digest",
        )
    }
    if integer_evidence != {
        "canonical_integer_fields": 8,
        "integer_vector_count": 34,
        "integer_vector_accepted": 19,
        "integer_vector_rejected": 15,
        "integer_vector_source_sha256": "2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b",
        "integer_vector_result_digest": "d4cfe7ec27c052badd2f67723fa5a4123becd60b6c1be11ce37b07b58f00f13d",
    }:
        raise SystemExit(f"macro-planning integer vector evidence drift: {integer_evidence}")
    print(json.dumps({"status": "ok", "python": actual, "node": expected, "v2": python_v2, "macro_planning_host": python_macro}, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
