"""Validate the M0 delivery records without starting product services.

This is a read-only release gate for the tracked P0 evidence.  It checks the
content-addressed contract inventory, the ten recorded browser observations,
the safety constraints, and the symbolic M0-OPEN manifest.  Runtime and
browser execution remain separate commands recorded in the integration test
ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    import validate_merge_gate as merge_gate
except ModuleNotFoundError:  # pragma: no cover - package-style imports
    from . import validate_merge_gate as merge_gate


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
DELIVERY = ROOT / "docs" / "deliveries" / "PPA-00"
CONTRACT_MANIFEST = ROOT / "contracts" / "manifest-v1.json"
CONTRACT_GOLDEN = DELIVERY / "contract-golden-manifest.json"
PARITY = DELIVERY / "parity-ledger.json"
M0_OPEN = DELIVERY / "m0-open-manifest.json"
EVIDENCE = DELIVERY / "evidence" / "browser-smoke.json"
CLOSURE_INDEX = ROOT / "docs" / "contracts" / "finding-closure-v1.json"
CLOSURE_EVIDENCE = DELIVERY / "evidence" / "finding-closure.json"
P0_BRANCH = "codex/ppa-00-integration"
BASE_SHA = "1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0"
MATRIX_PATH = merge_gate.DEFAULT_MATRIX
RELEASE_LABEL = "M0-OPEN-R3"

_ALLOWED_FONT_URLS = {
    "https://fonts.loli.net/css2?family=Inter:wght@400;500;600;700&display=swap",
    "https://fonts.loli.net/css2?family=JetBrains+Mono:wght@400;500&display=swap",
    "https://fonts.loli.net/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap",
}
_ALLOWED_FONT_ERROR = "Failed to load resource: net::ERR_FAILED"
_ALLOWED_TAURI_WARNING = re.compile(
    r"^\[API\] Tauri IPC 调用失败: TypeError: Cannot read properties of undefined \(reading 'invoke'\)"
    r"(?:\n    at invoke \(http://127\.0\.0\.1:3000/node_modules/\.vite/deps/@tauri-apps_api_core\.js\?v=[a-z0-9]+:\d+:\d+\))?"
    r"(?:\n    at initApiClient \(http://127\.0\.0\.1:3000/src/api/config\.ts:\d+:\d+\))?"
    r"(?:\n    at async bootstrap \(http://127\.0\.0\.1:3000/src/main\.ts:\d+:\d+\))?$",
    re.IGNORECASE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is forbidden: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def resolve_record(record: dict[str, Any]) -> Path:
    path = Path(str(record["path"]))
    if not path.is_absolute():
        path = ROOT / path
    return path


def check_records(records: list[dict[str, Any]], label: str, *, allow_missing: bool = False) -> None:
    for record in records:
        path = resolve_record(record)
        if not path.is_file():
            if allow_missing:
                continue
            raise AssertionError(f"{label} file missing: {path}")
        if path.stat().st_size != int(record["bytes"]):
            raise AssertionError(f"{label} size drift: {path}")
        if sha256(path) != record["sha256"]:
            raise AssertionError(f"{label} hash drift: {path}")


def content_addressed_records(value: Any) -> list[dict[str, Any]]:
    """Collect every nested evidence record carrying path/bytes/hash."""

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


def contract_inventory() -> dict[str, int]:
    negative = [
        read_json(path)
        for path in sorted((CONTRACTS / "corpus" / "negative" / "84.13").glob("*.json"))
    ]
    return {
        "schema_count": len(list((CONTRACTS / "json-schema").glob("*.schema.json"))),
        "positive_fixture_count": len(list((CONTRACTS / "examples" / "fixtures").glob("*.json"))),
        "combination_example_count": len(list((CONTRACTS / "examples").glob("*.json"))),
        "negative_group_count": len(negative),
        "negative_case_count": sum(len(item.get("negative", [])) for item in negative),
    }


def flow_id(flow: dict[str, Any]) -> str:
    value = flow.get("flow_id", flow.get("id"))
    if not isinstance(value, str) or not value:
        raise AssertionError("browser flow id is missing")
    return value


def _flow_screenshots(flow: dict[str, Any]) -> list[Any]:
    value = flow.get("screenshots", flow.get("screenshot"))
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def _flow_has_expected_actual(flow: dict[str, Any]) -> bool:
    expected_actual = flow.get("expected_actual", flow.get("expected_vs_actual"))
    if isinstance(expected_actual, list) and expected_actual:
        return True
    if isinstance(flow.get("expected"), (str, dict, list)) and isinstance(flow.get("actual"), (str, dict, list)):
        return True
    assertions = flow.get("assertions")
    return isinstance(assertions, list) and any(
        isinstance(item, dict) and {"expected", "actual"}.issubset(item)
        for item in assertions
    )


def _validate_browser_error_gate(evidence: dict[str, Any]) -> None:
    """Require the smoke producer's strict, classified browser error gate."""

    strict = evidence.get("strict_browser_gate")
    if not isinstance(strict, dict) or strict.get("passed") is not True:
        raise AssertionError("browser strict error gate did not pass")
    if int(strict.get("failure_count", 0) or 0) != 0 or strict.get("failures"):
        raise AssertionError("browser strict error gate contains failures")
    policy = evidence.get("allowlist_policy")
    if not isinstance(policy, list):
        raise AssertionError("browser allowlist policy is missing")
    policy_ids = {
        item.get("id") for item in policy if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if policy_ids != {"blocked-font-css", "tauri-browser-fallback"}:
        raise AssertionError("browser allowlist policy is not the exact M0 policy")

    http_failures = evidence.get("http_failures")
    if not isinstance(http_failures, list):
        raise AssertionError("browser HTTP failure classification is missing")
    if any(isinstance(item, dict) and int(item.get("status", 0) or 0) >= 400 for item in http_failures):
        raise AssertionError("browser evidence contains an HTTP status >= 400")
    if http_failures:
        raise AssertionError("browser evidence contains an unclassified HTTP failure")

    page_errors = evidence.get("page_errors")
    if not isinstance(page_errors, list) or page_errors:
        raise AssertionError("browser evidence contains a page error")
    console_errors = evidence.get("console_errors")
    if not isinstance(console_errors, list) or console_errors:
        raise AssertionError("browser evidence contains an unallowlisted console error/warning")
    console_events = evidence.get("console_events")
    console_allowlisted = evidence.get("console_allowlisted")
    if not isinstance(console_events, list) or not isinstance(console_allowlisted, list):
        raise AssertionError("browser console classification is missing")
    if len(console_events) != len(console_allowlisted):
        raise AssertionError("every error/warning console event must be explicitly allowlisted")
    for event in console_allowlisted:
        if not isinstance(event, dict) or event.get("allowlist_id") not in policy_ids:
            raise AssertionError("browser console event has an unknown allowlist id")
        if event.get("allowlist_id") == "tauri-browser-fallback":
            if event.get("type") != "warning" or not _ALLOWED_TAURI_WARNING.fullmatch(str(event.get("text", ""))):
                raise AssertionError("browser Tauri fallback is not an exact documented warning")
        elif event.get("type") != "error" or event.get("text") != _ALLOWED_FONT_ERROR:
            raise AssertionError("browser font allowlist entry is not exact")

    blocked_external = evidence.get("blocked_external_requests")
    if not isinstance(blocked_external, list):
        raise AssertionError("browser external-request classification is missing")
    font_requests = 0
    for request in blocked_external:
        if not isinstance(request, dict) or request.get("allowlisted") is not True:
            raise AssertionError("browser evidence contains an unallowlisted external request")
        if request.get("allowlist_id") != "blocked-font-css" or request.get("method") != "GET":
            raise AssertionError("browser external allowlist is broader than the font CSS rule")
        if request.get("resource_type") != "stylesheet" or request.get("url") not in _ALLOWED_FONT_URLS:
            raise AssertionError("browser external allowlist contains a non-font stylesheet")
        font_requests += 1
    font_events = sum(
        1 for event in console_allowlisted
        if isinstance(event, dict) and event.get("allowlist_id") == "blocked-font-css"
    )
    if font_events > font_requests:
        raise AssertionError("browser font console allowlist is not bound to an aborted request")


def _validate_independent_writing_support_subflows(flows: list[dict[str, Any]]) -> None:
    """Check that FLOW-07 and FLOW-08 are separate executable UI records."""

    subflows: dict[str, dict[str, Any]] = {}
    for parent in flows:
        children = parent.get("subflows", [])
        if children is None:
            children = []
        if not isinstance(children, list):
            raise AssertionError(f"browser subflows are not a list: {flow_id(parent)}")
        for child in children:
            if not isinstance(child, dict):
                raise AssertionError(f"browser subflow is not an object: {flow_id(parent)}")
            child_id = flow_id(child)
            if child_id in subflows:
                raise AssertionError(f"duplicate browser subflow: {child_id}")
            subflows[child_id] = child
    required = {"FLOW-07-foreshadow-ledger", "FLOW-08-story-evolution-bible"}
    if set(subflows) != required:
        raise AssertionError("browser evidence must contain exactly the two explicit FLOW-07/FLOW-08 subflows")

    foreshadow = subflows["FLOW-07-foreshadow-ledger"]
    bible = subflows["FLOW-08-story-evolution-bible"]
    for child, label in ((foreshadow, "FLOW-07"), (bible, "FLOW-08")):
        if child.get("status") not in {"passed", "exercised"} or child.get("exercised") is not True:
            raise AssertionError(f"{label} subflow was not exercised")
        actions = child.get("ui_actions", child.get("actions"))
        if not isinstance(actions, list) or not actions:
            raise AssertionError(f"{label} has no independent UI action record")
        trace = child.get("api_trace")
        if not isinstance(trace, list) or not trace:
            raise AssertionError(f"{label} has no independent API trace")
        if not _flow_has_expected_actual(child):
            raise AssertionError(f"{label} has no independent expected/actual assertion")
        screenshots = _flow_screenshots(child)
        if len(screenshots) != 1 or not isinstance(screenshots[0], (str, dict)):
            raise AssertionError(f"{label} must have exactly one independent screenshot")
        if not any(isinstance(action, dict) and action.get("action") == "click" and action.get("target") == "写作支撑" for action in actions):
            raise AssertionError(f"{label} does not record the writing-support group click")
    f_targets = {item.get("target") for item in foreshadow["ui_actions"] if isinstance(item, dict)}
    b_targets = {item.get("target") for item in bible["ui_actions"] if isinstance(item, dict)}
    if "伏笔账本" not in f_targets or "故事演进" not in b_targets:
        raise AssertionError("FLOW-07/FLOW-08 do not record their distinct tab clicks")
    f_urls = "\n".join(str(item.get("url", "")) for item in foreshadow["api_trace"] if isinstance(item, dict))
    b_urls = "\n".join(str(item.get("url", "")) for item in bible["api_trace"] if isinstance(item, dict))
    if "foreshadow-ledger" not in f_urls:
        raise AssertionError("FLOW-07 trace lacks the foreshadow-ledger API")
    if "story-evolution" not in b_urls and "/bible" not in b_urls:
        raise AssertionError("FLOW-08 trace lacks the story-evolution/Bible API")
    f_paths = {str(item.get("path")) if isinstance(item, dict) else str(item) for item in _flow_screenshots(foreshadow)}
    b_paths = {str(item.get("path")) if isinstance(item, dict) else str(item) for item in _flow_screenshots(bible)}
    f_hashes = {str(item.get("sha256")) for item in _flow_screenshots(foreshadow) if isinstance(item, dict)}
    b_hashes = {str(item.get("sha256")) for item in _flow_screenshots(bible) if isinstance(item, dict)}
    if f_paths & b_paths or (f_hashes and b_hashes and f_hashes & b_hashes):
        raise AssertionError("FLOW-07 and FLOW-08 share screenshot evidence")


def validate_browser_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Validate the executable ten-flow record, not a surface inventory."""

    if evidence.get("status") != "passed":
        raise AssertionError("browser evidence is not a passed run")
    flows = evidence.get("flows")
    if not isinstance(flows, list) or len(flows) != 10:
        raise AssertionError("browser evidence must contain ten flows")
    ids = [flow_id(flow) for flow in flows if isinstance(flow, dict)]
    if len(ids) != 10 or len(set(ids)) != 10:
        raise AssertionError("browser evidence flow IDs must be unique")
    if set(ids) != {
        "home-create-surface", "wizard-generation", "workbench-shell",
        "workbench-chapter-tree", "chapter-edit-save", "generation-pause-cancel",
        "sse-disconnect-recovery", "workbench-writing-support",
        "checkpoint-recovery", "workbench-export-menu",
    }:
        raise AssertionError("browser evidence top-level flows are not the ten M0 records")
    for flow in flows:
        if not isinstance(flow, dict):
            raise AssertionError("browser evidence flow must be an object")
        current_id = flow_id(flow)
        if flow.get("status") not in {"passed", "exercised"} or flow.get("exercised", True) is not True:
            raise AssertionError(f"browser flow was not exercised successfully: {current_id}")
        actions = flow.get("ui_actions", flow.get("actions"))
        if not isinstance(actions, list) or not actions:
            raise AssertionError(f"browser flow has no UI action record: {current_id}")
        trace = flow.get("api_trace")
        if not isinstance(trace, list) or not trace:
            raise AssertionError(f"browser flow has no API trace: {current_id}")
        if not _flow_has_expected_actual(flow):
            raise AssertionError(f"browser flow has no expected/actual assertion: {current_id}")
        if not _flow_screenshots(flow):
            raise AssertionError(f"browser flow has no screenshot: {current_id}")

    _validate_independent_writing_support_subflows(flows)
    _validate_browser_error_gate(evidence)

    trace = evidence.get("api_trace")
    if not isinstance(trace, list) or not trace:
        raise AssertionError("browser evidence has no aggregate API trace")
    trace_text = "\n".join(str(item.get("url", "")) for item in trace if isinstance(item, dict))
    required_routes = {
        "create": "/api/v1/novels/",
        "wizard_sse": "/api/v1/bible/novels/",
        "chapter_generation": "/api/v1/novels/",
        "autopilot": "/api/v1/autopilot/",
        "export": "/api/v1/export/novel/",
    }
    if required_routes["create"] not in trace_text:
        raise AssertionError("browser trace lacks UI book creation request")
    if required_routes["wizard_sse"] not in trace_text or "generate-stream" not in trace_text:
        raise AssertionError("browser trace lacks wizard generation SSE")
    if "generate-chapter-stream" not in trace_text:
        raise AssertionError("browser trace lacks chapter generation SSE")
    if required_routes["autopilot"] not in trace_text:
        raise AssertionError("browser trace lacks autopilot execution/recovery requests")
    if required_routes["export"] not in trace_text:
        raise AssertionError("browser trace lacks UI export request")

    constraints = evidence.get("constraints")
    if not isinstance(constraints, dict):
        raise AssertionError("browser constraints are missing")
    if constraints.get("generation_pipeline_invoked") is not True:
        raise AssertionError("browser generation pipeline was not invoked")
    if constraints.get("non_empty_chapter_body_saved") is not True:
        raise AssertionError("browser did not save a non-empty chapter body")
    if constraints.get("temporary_data_removed_after_run") is not True:
        raise AssertionError("browser temporary data was not removed")
    if constraints.get("fake_provider_used") is not True and constraints.get("fake_provider") is not True:
        raise AssertionError("browser run did not prove deterministic fake Provider use")
    if constraints.get("real_provider_used", False) is not False:
        raise AssertionError("browser run used a live provider")
    if constraints.get("external_network_used", False) is not False:
        raise AssertionError("browser run used external network")
    if constraints.get("sse_disconnect_recovery") is not True and constraints.get("sse_recovery_exercised") is not True:
        raise AssertionError("browser run did not exercise SSE disconnect/recovery")
    if constraints.get("ui_download_captured") is not True and constraints.get("export_download_captured") is not True:
        raise AssertionError("browser run did not capture a UI download")
    for key in ("forbidden_generation_calls", "unexpected_external_calls"):
        calls = evidence.get(key, [])
        if calls:
            raise AssertionError(f"browser evidence has unexpected calls: {key}")
    data_evidence = evidence.get("data_evidence")
    if not isinstance(data_evidence, dict) or int(data_evidence.get("non_empty_chapter_count", 0) or 0) < 1:
        raise AssertionError("browser data evidence does not prove non-empty chapter persistence")
    export = evidence.get("export_observation")
    if not isinstance(export, dict) or int(export.get("status", 0)) != 200 or int(export.get("byte_length", 0)) <= 0:
        raise AssertionError("browser export observation is not a non-empty HTTP 200 download")

    script = (ROOT / "tools" / "integration" / "browser_smoke.mjs").read_text(encoding="utf-8")
    forbidden_source_patterns = (
        r"jsonFetch\([^\n]*?/api/v1/novels/",
        r"/chapters/[^\n]*/ensure",
        r"collectExportObservation",
    )
    for pattern in forbidden_source_patterns:
        if re.search(pattern, script):
            raise AssertionError(f"browser script contains a direct-action shortcut: {pattern}")
    return {"flow_count": len(flows), "api_trace_entries": len(trace)}


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, encoding="utf-8").strip()


def validate() -> dict[str, Any]:
    # Do not infer this state from m0-open-manifest.json.  The manifest is a
    # recorded claim; the matrix, filesystem and Git refs are the live gate.
    creation_gate = merge_gate.verify_creation_gate(
        matrix_path=MATRIX_PATH,
        root=ROOT,
    )
    if creation_gate["closed"] is not True:
        raise AssertionError("P1-P6 creation gate is not closed")

    contract = read_json(CONTRACT_GOLDEN)
    contract_manifest = read_json(CONTRACT_MANIFEST)
    closure = read_json(CLOSURE_INDEX)
    closure_evidence = read_json(CLOSURE_EVIDENCE)
    if contract["schema"] != "plotpilot-contract-golden-delivery/v1":
        raise AssertionError("unexpected contract-golden delivery schema")
    if contract["source"]["sha256"] != "e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b":
        raise AssertionError("formal design identity drift")
    if contract["generated_from"]["contract_manifest"]["sha256"] != sha256(CONTRACT_MANIFEST):
        raise AssertionError("contract manifest hash drift")
    expected_inventory = contract_inventory()
    if contract["inventory"] != expected_inventory:
        raise AssertionError(f"contract inventory drift: {contract['inventory']!r}")
    if len(contract_manifest.get("schemas", [])) != expected_inventory["schema_count"]:
        raise AssertionError("contracts/manifest-v1.json schema inventory drift")
    if closure.get("schema") != "plotpilot-finding-closure/v1" or closure.get("status") != "closed" or closure.get("finding_count") != 25 or closure.get("ids_exactly_once") is not True:
        raise AssertionError("finding closure inventory drift")
    if closure_evidence.get("schema") != "plotpilot-finding-closure-evidence/v1" or closure_evidence.get("status") != "passed" or closure_evidence.get("exit_code") != 0:
        raise AssertionError("finding closure evidence is not passed")
    closure_record = closure_evidence.get("closure_index")
    if not isinstance(closure_record, dict) or closure_record.get("path") != "docs/contracts/finding-closure-v1.json":
        raise AssertionError("finding closure evidence binding drift")
    check_records(content_addressed_records(closure_evidence), "finding closure")
    check_records(contract["artifacts"]["contract_files"], "contract")
    check_records(contract["artifacts"]["contract_docs"], "contract doc")
    check_records(contract["artifacts"]["sdk"], "SDK")
    check_records(contract["artifacts"]["tooling"], "tool")
    if len(contract["negative_groups"]) != expected_inventory["negative_group_count"] or sum(item["negative_case_count"] for item in contract["negative_groups"]) != expected_inventory["negative_case_count"]:
        raise AssertionError("negative group inventory drift")

    parity = read_json(PARITY)
    evidence = read_json(EVIDENCE)
    if parity["schema"] != "plotpilot-parity-ledger/v1" or parity["status"] != "baseline_recorded":
        raise AssertionError("parity ledger is not a recorded baseline")
    if len(parity["main_flows"]) != 10 or len(parity["features"]) != 27:
        raise AssertionError("parity ledger must contain ten main flows and 27 baseline features")
    browser_summary = validate_browser_evidence(evidence)
    if parity["evidence_run"]["sha256"] != sha256(EVIDENCE):
        raise AssertionError("parity evidence hash drift")
    screenshot_records = parity["evidence_run"]["screenshot_files"]
    if len(screenshot_records) != 11:
        raise AssertionError("expected nine top-level screenshots plus two independent FLOW-07/FLOW-08 screenshots")
    check_records(screenshot_records, "screenshot")
    for flow in parity["main_flows"]:
        if flow.get("status") != "exercised" or flow["baseline_evidence"].get("executed") is not True:
            raise AssertionError(f"main flow was not exercised: {flow.get('flow_id')}")
        if not flow["baseline_evidence"]["screenshots"]:
            raise AssertionError(f"main flow has no screenshot evidence: {flow['flow_id']}")
        if not flow["baseline_evidence"].get("ui_actions"):
            raise AssertionError(f"main flow has no UI action evidence: {flow['flow_id']}")
        if not flow["baseline_evidence"].get("expected_actual") and not flow["baseline_evidence"]["behavior_assertions"]:
            raise AssertionError(f"main flow has no behavior assertions: {flow['flow_id']}")

    identity = read_json(DELIVERY / "evidence" / "m0.2-identity-runtime.json")
    if identity["captured_branch"] != P0_BRANCH or identity["baseline"]["commit"] != BASE_SHA:
        raise AssertionError("M0.2 identity drift")
    if identity["git_controls"]["donor_local_push"] != "DISABLED":
        raise AssertionError("donor-local push fence is open")

    m0 = read_json(M0_OPEN)
    if m0.get("schema") != "plotpilot-m0-open-manifest/v1" or m0.get("status") != "open":
        raise AssertionError("M0-open manifest is not open")
    if m0.get("commit_ref") != RELEASE_LABEL or m0.get("tag") != RELEASE_LABEL or m0.get("self_hash_excluded") is not True:
        raise AssertionError("M0-open symbolic identity is invalid")
    if set(m0["gates"]) != {f"M0.{index}" for index in range(1, 8)} or any(item["status"] != "passed" for item in m0["gates"].values()):
        raise AssertionError("one or more M0 gates are not passed")
    declared_gate = m0.get("constraints", {}).get("p1_p6_creation_gate")
    live_gate_state = "closed" if creation_gate["closed"] else "open"
    if declared_gate != live_gate_state:
        raise AssertionError(
            f"M0 manifest creation-gate claim disagrees with live gate: "
            f"declared={declared_gate!r}, live={live_gate_state!r}"
        )
    if git("branch", "--show-current") != P0_BRANCH:
        raise AssertionError("current branch is not P0 integration branch")
    if git("remote", "get-url", "--push", "donor-local") != "DISABLED":
        raise AssertionError("donor-local push URL drift")
    return {
        "schema": "p0-m0-delivery-validation/v1",
        "passed": True,
        "contract_schemas": 48,
        "negative_groups": expected_inventory["negative_group_count"],
        "negative_cases": expected_inventory["negative_case_count"],
        "parity_main_flows": 10,
        "parity_features": 27,
        "screenshots": len(screenshot_records),
        "browser_api_trace_entries": browser_summary["api_trace_entries"],
        "branch": git("branch", "--show-current"),
        "base_sha": BASE_SHA,
        "donor_local_push": git("remote", "get-url", "--push", "donor-local"),
        "p1_p6_creation_gate": live_gate_state,
        "creation_gate": creation_gate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    print(json.dumps(validate(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
