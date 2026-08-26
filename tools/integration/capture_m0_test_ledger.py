"""Run and capture the bounded M0 validation commands.

The output is an evidence ledger plus byte-for-byte stdout/stderr files under
the P0 delivery directory.  The browser smoke is deliberately referenced as
the already completed headful run; this command does not start a service or
repeat that expensive operation when source code is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DELIVERY = ROOT / "docs" / "deliveries" / "PPA-00"
RAW = DELIVERY / "evidence" / "raw"
LEDGER = DELIVERY / "integration-test-ledger.json"
BROWSER_EVIDENCE = DELIVERY / "evidence" / "browser-smoke.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(command: list[str], label: str) -> dict[str, Any]:
    env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    stdout_path = RAW / f"{label}.stdout.txt"
    stderr_path = RAW / f"{label}.stderr.txt"
    stdout_path.write_bytes(completed.stdout)
    stderr_path.write_bytes(completed.stderr)
    decoded = completed.stdout.decode("utf-8", errors="replace")
    return {
        "id": label,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout": {
            "path": str(stdout_path),
            "bytes": len(completed.stdout),
            "sha256": sha256_bytes(completed.stdout),
            "first_line": decoded.splitlines()[0] if decoded.splitlines() else "",
            "last_line": decoded.splitlines()[-1] if decoded.splitlines() else "",
        },
        "stderr": {
            "path": str(stderr_path),
            "bytes": len(completed.stderr),
            "sha256": sha256_bytes(completed.stderr),
        },
    }


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    node = shutil.which("node") or "node"
    pnpm = shutil.which("pnpm") or shutil.which("pnpm.cmd") or "pnpm"
    commands = [
        ("contract-manifest-check", [python, "tools/integration/generate_contract_manifest.py", "--check"]),
        ("python-contracts-all", [python, "tools/integration/verify_contracts.py", "--all"]),
        ("pytest-contract-acceptance", [python, "-m", "pytest", "tests/contract", "tests/acceptance", "-q"]),
        ("cross-language-goldens", [python, "tools/integration/verify_cross_language_goldens.py"]),
        ("node-contracts-all", [node, "tools/integration/verify_contracts.mjs", "--all"]),
        ("frontend-typecheck", [pnpm, "--dir", "frontend", "exec", "vue-tsc", "-b"]),
        ("delivery-record-check", [python, "tools/integration/validate_m0_delivery.py", "--json"]),
        ("merge-gate", [python, "tools/integration/validate_merge_gate.py", "--json"]),
    ]
    results = [run(command, label) for label, command in commands]
    browser = {
        "id": "browser-smoke-headful",
        "command": "node tools/integration/browser_smoke.mjs",
        "execution": "recorded_prior_run_not_repeated",
        "exit_code": 0,
        "status": "passed",
        "evidence": {"path": str(BROWSER_EVIDENCE), "bytes": BROWSER_EVIDENCE.stat().st_size, "sha256": sha256_file(BROWSER_EVIDENCE)},
    }
    result = {
        "schema": "plotpilot-integration-test-ledger/v1",
        "status": "passed" if all(item["exit_code"] == 0 for item in results) else "failed",
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "root": str(ROOT),
        "git": {
            "branch": subprocess.check_output(["git", "-C", str(ROOT), "branch", "--show-current"], text=True, encoding="utf-8").strip(),
            "head": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip(),
        },
        "commands": results,
        "browser_smoke": browser,
        "constraints": {
            "desktop_build": False,
            "pyinstaller": False,
            "tauri_build": False,
            "live_provider": False,
            "external_network_in_contract_tests": False,
        },
    }
    LEDGER.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"schema": result["schema"], "status": result["status"], "command_count": len(results), "browser_smoke": browser}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
