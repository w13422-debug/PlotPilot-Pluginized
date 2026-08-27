"""Run a fresh, read-only M0-OPEN-R4 release review.

This review is deliberately separate from the recorded integration ledger. It
reruns deterministic contract/delivery/merge checks, verifies the content
addressed records and safety boundaries, and writes a small evidence record.
It never starts PlotPilot, touches the donor, creates Git refs, or changes
product data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DELIVERY = ROOT / "docs" / "deliveries" / "PPA-00"
RAW = DELIVERY / "evidence" / "raw"
REVIEW_PATH = DELIVERY / "evidence" / "fresh-readonly-review.json"
BASE_SHA = "1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0"
BRANCH = "codex/ppa-00-integration"
RELEASE_LABEL = "M0-OPEN-R4"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is forbidden: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *args],
        text=True,
        encoding="utf-8",
        errors="strict",
    ).strip()


def run(command: list[str], label: str) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    RAW.mkdir(parents=True, exist_ok=True)
    stdout_path = RAW / f"fresh-review-{label}.stdout.txt"
    stderr_path = RAW / f"fresh-review-{label}.stderr.txt"
    stdout_path.write_bytes(completed.stdout)
    stderr_path.write_bytes(completed.stderr)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    return {
        "id": label,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout": {
            "path": stdout_path.relative_to(ROOT).as_posix(),
            "bytes": len(completed.stdout),
            "sha256": sha256_bytes(completed.stdout),
            "first_line": stdout.splitlines()[0] if stdout.splitlines() else "",
            "last_line": stdout.splitlines()[-1] if stdout.splitlines() else "",
        },
        "stderr": {
            "path": stderr_path.relative_to(ROOT).as_posix(),
            "bytes": len(completed.stderr),
            "sha256": sha256_bytes(completed.stderr),
        },
        "_stdout": stdout,
    }


def resolve(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


def check_records(records: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for record in records:
        path = resolve(str(record["path"]))
        if not path.is_file():
            failures.append({"label": label, "path": str(path), "reason": "missing"})
            continue
        if path.stat().st_size != int(record["bytes"]):
            failures.append({"label": label, "path": str(path), "reason": "size"})
        actual = sha256_file(path)
        if actual != record["sha256"]:
            failures.append({
                "label": label,
                "path": str(path),
                "reason": "sha256",
                "expected": record["sha256"],
                "actual": actual,
            })
    return failures


def content_addressed_records(value: Any) -> list[dict[str, Any]]:
    """Collect every evidence record carrying a path/bytes/hash triple."""

    records: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("path"), str) and "bytes" in node and "sha256" in node:
                records.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return records


def child_gate_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return structured failures for every child gate that did not pass.

    The child commands are the executable gates of this review.  Their result
    metadata must therefore be part of the review verdict, not merely a
    record in the emitted evidence.  Treat missing or malformed status fields
    as failures as well; a fresh review must never infer success from an
    incomplete child result.
    """

    failures: list[dict[str, Any]] = []
    for result in results:
        exit_code = result.get("exit_code")
        status = result.get("status")
        if exit_code == 0 and status == "passed":
            continue
        failures.append(
            {
                "command_id": result.get("id"),
                "command": result.get("command"),
                "exit_code": exit_code,
                "status": status,
                "reason": "child gate requires exit_code=0 and status=passed",
            }
        )
    return failures


def main() -> int:
    python = sys.executable
    commands = [
        ([python, "tools/integration/verify_contracts.py", "--all"], "python-contracts-all"),
        ([python, "tools/integration/verify_cross_language_goldens.py"], "cross-language-goldens"),
        (["node", "tools/integration/verify_contracts.mjs", "--all"], "node-contracts-all"),
        ([python, "tools/integration/validate_m0_delivery.py", "--json"], "delivery-record-check"),
        ([python, "tools/integration/validate_merge_gate.py", "--json"], "merge-gate"),
    ]
    results = [run(command, label) for command, label in commands]
    child_failures = child_gate_failures(results)
    m0 = read_json(DELIVERY / "m0-open-manifest.json")
    contract = read_json(DELIVERY / "contract-golden-manifest.json")
    parity = read_json(DELIVERY / "parity-ledger.json")
    browser = read_json(DELIVERY / "evidence" / "browser-smoke.json")
    closure = read_json(ROOT / "docs" / "contracts" / "finding-closure-v1.json")
    closure_evidence = read_json(DELIVERY / "evidence" / "finding-closure.json")
    state = read_json(ROOT / "coordination" / "PPA-00" / "state.json")
    failures: list[str] = []
    merge_result = next(item for item in results if item.get("id") == "merge-gate")
    try:
        parsed_merge = json.loads(str(merge_result.get("_stdout", "")))
    except (TypeError, json.JSONDecodeError):
        parsed_merge = {}
        failures.append("merge-gate returned invalid JSON")
    if not isinstance(parsed_merge, dict):
        merge = {}
        failures.append("merge-gate JSON result is not an object")
    else:
        merge = parsed_merge

    record_failures: list[dict[str, Any]] = []
    for key, label in (
        ("contract_files", "contract"),
        ("contract_docs", "contract-doc"),
        ("sdk", "sdk"),
        ("tooling", "tooling"),
    ):
        record_failures.extend(check_records(contract["artifacts"][key], label))
    record_failures.extend(check_records(parity["evidence_run"]["screenshot_files"], "screenshot"))
    record_failures.extend(check_records(content_addressed_records(closure_evidence), "finding-closure"))

    if state.get("schema") != "ppa-project-state/v1":
        failures.append("state schema drift")
    if set(state.get("milestones", {})) != {f"M0.{index}" for index in range(1, 8)}:
        failures.append("state milestone set drift")
    if any(value != "passed" for value in state.get("milestones", {}).values()):
        failures.append("state milestone not passed")
    if m0["verification"]["test_ledger"]["sha256"] != sha256_file(DELIVERY / "integration-test-ledger.json"):
        failures.append("M0 test ledger hash mismatch")
    if m0["verification"]["contract_manifest"]["sha256"] != sha256_file(ROOT / "contracts" / "manifest-v1.json"):
        failures.append("M0 contract manifest hash mismatch")
    if m0["verification"]["parity_ledger"]["sha256"] != sha256_file(DELIVERY / "parity-ledger.json"):
        failures.append("M0 parity ledger hash mismatch")
    if m0.get("status") != "open" or m0.get("commit_ref") != RELEASE_LABEL or m0.get("tag") != RELEASE_LABEL:
        failures.append("M0 symbolic identity drift")
    if set(m0.get("gates", {})) != {f"M0.{index}" for index in range(1, 8)}:
        failures.append("M0 gate set drift")
    if any(value.get("status") != "passed" for value in m0.get("gates", {}).values()):
        failures.append("M0 gate not passed")
    if contract.get("inventory") != {
        "combination_example_count": 4,
        "negative_case_count": 105,
        "negative_group_count": 14,
        "positive_fixture_count": 35,
        "schema_count": 48,
    }:
        failures.append("contract inventory drift")
    if len(parity.get("main_flows", [])) != 10 or len(parity.get("features", [])) != 27:
        failures.append("parity inventory drift")
    subflow_map = {
        child.get("flow_id", child.get("id")): child
        for flow in browser.get("flows", [])
        for child in (flow.get("subflows", []) if isinstance(flow, dict) else [])
        if isinstance(child, dict)
    }
    required_subflows = {"FLOW-07-foreshadow-ledger", "FLOW-08-story-evolution-bible"}
    if set(subflow_map) != required_subflows:
        failures.append("FLOW-07/FLOW-08 subflow inventory drift")
    for subflow_id in required_subflows:
        child = subflow_map.get(subflow_id, {})
        if child.get("status") not in {"passed", "exercised"} or child.get("exercised") is not True:
            failures.append(f"{subflow_id} was not exercised")
        if not child.get("ui_actions") or not child.get("api_trace") or not child.get("screenshots"):
            failures.append(f"{subflow_id} lacks independent UI/trace/screenshot evidence")
    if (
        len(browser.get("flows", [])) != 10
        or len(browser.get("api_trace", [])) <= 0
        or browser.get("page_errors")
        or browser.get("forbidden_generation_calls")
        or browser.get("unexpected_external_calls")
        or browser.get("constraints", {}).get("non_empty_chapter_body_saved") is not True
        or browser.get("constraints", {}).get("fake_provider_used") is not True
        or browser.get("constraints", {}).get("real_provider_used") is not False
        or browser.get("constraints", {}).get("external_network_used") is not False
        or browser.get("constraints", {}).get("sse_disconnect_recovery") is not True
    ):
        failures.append("browser safety/evidence drift")
    if closure.get("schema") != "plotpilot-finding-closure/v1" or closure.get("status") != "closed" or closure.get("finding_count") != 25 or closure.get("ids_exactly_once") is not True:
        failures.append("finding closure inventory drift")
    closure_index = closure_evidence.get("closure_index")
    closure_path = ROOT / "docs" / "contracts" / "finding-closure-v1.json"
    if not isinstance(closure_index, dict) or closure_index.get("path") != "docs/contracts/finding-closure-v1.json":
        failures.append("finding closure evidence binding drift")
    elif closure_index.get("sha256") != sha256_file(closure_path) or closure_index.get("bytes") != closure_path.stat().st_size:
        failures.append("finding closure content address drift")
    if merge.get("out_of_set_paths") or not merge.get("p1_p6_absent") or merge.get("donor_local_push") != "DISABLED":
        failures.append("write-set or creation gate drift")

    banned = re.compile(r"\b(?:stub|TODO|FIXME)\b|not implemented|假数据|假按钮", re.IGNORECASE)
    marker_hits: list[dict[str, Any]] = []
    for root in (
        ROOT / "contracts",
        ROOT / "backend" / "plotpilot_plugin_sdk",
        ROOT / "backend" / "plotpilot_core" / "bootstrap",
        ROOT / "frontend" / "src" / "contracts",
        ROOT / "tests" / "contract",
        ROOT / "tests" / "acceptance",
    ):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".ts", ".json", ".md"}:
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if banned.search(line):
                    marker_hits.append({"path": path.relative_to(ROOT).as_posix(), "line": line_number})
    if marker_hits:
        failures.append("stub marker found")

    tag_present = subprocess.run(
        ["git", "-C", str(ROOT), "show-ref", "--tags", "--verify", "refs/tags/M0-OPEN"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    review_failures = bool(failures or record_failures or child_failures)
    findings: list[dict[str, Any]] = [
        {
            "severity": "blocker",
            "id": "F-REVIEW-CHILD-GATE",
            "command_id": failure["command_id"],
            "command": failure["command"],
            "exit_code": failure["exit_code"],
            "status": failure["status"],
            "evidence": [failure],
            "required_action": "修复 child gate 后重新运行 fresh review",
        }
        for failure in child_failures
    ]
    if failures or record_failures:
        findings.append(
            {
                "severity": "blocker",
                "id": "F-REVIEW-CHECKS",
                "evidence": failures + record_failures,
                "required_action": "修复后重新运行 fresh review",
            }
        )
    review = {
        "schema": "plotpilot-m0-fresh-readonly-review/v1",
        "review_task_id": "PPA-M0-FRESH-REVIEW-R4",
        "reviewer": "P0 main control fallback (delegated Sol reviewer returned not_found)",
        "mode": "fresh_read_only_structured_review",
        "reviewed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "root": str(ROOT),
        "observed_head": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "base_sha": BASE_SHA,
        "commands": [{key: value for key, value in item.items() if key != "_stdout"} for item in results],
        "observations": {
            "m0_gates": {key: value["status"] for key, value in m0["gates"].items()},
            "contract_schemas": len(list((ROOT / "contracts" / "json-schema").glob("*.schema.json"))),
            "negative_groups": len(list((ROOT / "contracts" / "corpus" / "negative" / "84.13").glob("*.json"))),
            "negative_cases": contract["inventory"]["negative_case_count"],
            "parity_main_flows": len(parity["main_flows"]),
            "parity_features": len(parity["features"]),
            "screenshots": len(parity["evidence_run"]["screenshot_files"]),
            "browser_subflows": sorted(subflow_map),
            "browser_api_trace_entries": len(browser["api_trace"]),
            "state_json_valid": True,
            "m0_ledger_hash_matches": m0["verification"]["test_ledger"]["sha256"] == sha256_file(DELIVERY / "integration-test-ledger.json"),
            "contract_record_hashes_match": not record_failures,
            "stub_marker_hits": marker_hits,
            "tag_present_at_review": tag_present,
            "child_gate_failures": child_failures,
        },
        "findings": findings,
        "m0_gate_matrix": {key: {"status": value["status"], "evidence": value["evidence"]} for key, value in m0["gates"].items()},
        "write_set_check": {
            "passed": not merge.get("out_of_set_paths", []),
            "out_of_set_paths": merge.get("out_of_set_paths", []),
            "changed_paths_count": len(merge.get("changed_paths", [])),
        },
        "p1_p6_gate": {
            "passed": bool(merge.get("p1_p6_absent", False)),
            "creation_gate": merge.get("creation_gate", {}),
            "tag_expected_after_commit": True,
        },
        "verdict": "passed_pre_commit" if not review_failures else "failed",
        "limitations": [
            "review is pre-commit; exact M0-OPEN-R4 tag target is verified in the final close step",
            "browser smoke was executed as the current R3 headful evidence; this read-only review consumes its exact recorded hash",
        ],
    }
    REVIEW_PATH.write_text(json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "path": str(REVIEW_PATH),
        "sha256": sha256_file(REVIEW_PATH),
        "verdict": review["verdict"],
        "findings": len(review["findings"]),
        "commands": [(item["id"], item["exit_code"]) for item in results],
        "tag_present_at_review": tag_present,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if review["verdict"] == "passed_pre_commit" else 1


if __name__ == "__main__":
    raise SystemExit(main())
