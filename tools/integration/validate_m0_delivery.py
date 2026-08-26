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
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DELIVERY = ROOT / "docs" / "deliveries" / "PPA-00"
CONTRACT_MANIFEST = ROOT / "contracts" / "manifest-v1.json"
CONTRACT_GOLDEN = DELIVERY / "contract-golden-manifest.json"
PARITY = DELIVERY / "parity-ledger.json"
M0_OPEN = DELIVERY / "m0-open-manifest.json"
EVIDENCE = DELIVERY / "evidence" / "browser-smoke.json"
P0_BRANCH = "codex/ppa-00-integration"
BASE_SHA = "1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0"


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


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, encoding="utf-8").strip()


def validate() -> dict[str, Any]:
    contract = read_json(CONTRACT_GOLDEN)
    contract_manifest = read_json(CONTRACT_MANIFEST)
    if contract["schema"] != "plotpilot-contract-golden-delivery/v1":
        raise AssertionError("unexpected contract-golden delivery schema")
    if contract["source"]["sha256"] != "e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b":
        raise AssertionError("formal design identity drift")
    if contract["generated_from"]["contract_manifest"]["sha256"] != sha256(CONTRACT_MANIFEST):
        raise AssertionError("contract manifest hash drift")
    if contract["inventory"] != {
        "combination_example_count": 4,
        "negative_case_count": 39,
        "negative_group_count": 14,
        "positive_fixture_count": 35,
        "schema_count": 48,
    }:
        raise AssertionError(f"contract inventory drift: {contract['inventory']!r}")
    if len(contract_manifest.get("schemas", [])) != 48:
        raise AssertionError("contracts/manifest-v1.json does not list 48 schemas")
    check_records(contract["artifacts"]["contract_files"], "contract")
    check_records(contract["artifacts"]["contract_docs"], "contract doc")
    check_records(contract["artifacts"]["sdk"], "SDK")
    check_records(contract["artifacts"]["tooling"], "tool")
    if len(contract["negative_groups"]) != 14 or sum(item["negative_case_count"] for item in contract["negative_groups"]) != 39:
        raise AssertionError("negative group inventory drift")

    parity = read_json(PARITY)
    evidence = read_json(EVIDENCE)
    if parity["schema"] != "plotpilot-parity-ledger/v1" or parity["status"] != "baseline_recorded":
        raise AssertionError("parity ledger is not a recorded baseline")
    if len(parity["main_flows"]) != 10 or len(parity["features"]) != 27:
        raise AssertionError("parity ledger must contain ten main flows and 27 baseline features")
    if len(evidence["flows"]) != 10 or evidence["status"] != "passed":
        raise AssertionError("browser evidence is not a ten-flow pass")
    if evidence["constraints"]["generation_pipeline_invoked"] or evidence["constraints"]["non_empty_chapter_body_saved"]:
        raise AssertionError("browser safety constraint was violated")
    if evidence["constraints"]["temporary_data_removed_after_run"] is not True:
        raise AssertionError("temporary browser data was not removed")
    if evidence["forbidden_generation_calls"] or evidence["page_errors"]:
        raise AssertionError("browser evidence contains forbidden calls or page errors")
    if parity["evidence_run"]["sha256"] != sha256(EVIDENCE):
        raise AssertionError("parity evidence hash drift")
    screenshot_records = parity["evidence_run"]["screenshot_files"]
    if len(screenshot_records) != 10:
        raise AssertionError("expected ten screenshot records")
    check_records(screenshot_records, "screenshot")
    for flow in parity["main_flows"]:
        if not flow["baseline_evidence"]["screenshots"]:
            raise AssertionError(f"main flow has no screenshot evidence: {flow['flow_id']}")
        if not flow["baseline_evidence"]["behavior_assertions"]:
            raise AssertionError(f"main flow has no behavior assertions: {flow['flow_id']}")

    identity = read_json(DELIVERY / "evidence" / "m0.2-identity-runtime.json")
    if identity["captured_branch"] != P0_BRANCH or identity["baseline"]["commit"] != BASE_SHA:
        raise AssertionError("M0.2 identity drift")
    if identity["git_controls"]["donor_local_push"] != "DISABLED":
        raise AssertionError("donor-local push fence is open")

    m0 = read_json(M0_OPEN)
    if m0.get("schema") != "plotpilot-m0-open-manifest/v1" or m0.get("status") != "open":
        raise AssertionError("M0-open manifest is not open")
    if m0.get("commit_ref") != "M0-OPEN" or m0.get("tag") != "M0-OPEN" or m0.get("self_hash_excluded") is not True:
        raise AssertionError("M0-open symbolic identity is invalid")
    if set(m0["gates"]) != {f"M0.{index}" for index in range(1, 8)} or any(item["status"] != "passed" for item in m0["gates"].values()):
        raise AssertionError("one or more M0 gates are not passed")
    if git("branch", "--show-current") != P0_BRANCH:
        raise AssertionError("current branch is not P0 integration branch")
    if git("remote", "get-url", "--push", "donor-local") != "DISABLED":
        raise AssertionError("donor-local push URL drift")
    return {
        "schema": "p0-m0-delivery-validation/v1",
        "passed": True,
        "contract_schemas": 48,
        "negative_groups": 14,
        "negative_cases": 39,
        "parity_main_flows": 10,
        "parity_features": 27,
        "screenshots": 10,
        "browser_api_trace_entries": evidence["api_trace"].__len__(),
        "branch": git("branch", "--show-current"),
        "base_sha": BASE_SHA,
        "donor_local_push": git("remote", "get-url", "--push", "donor-local"),
        "p1_p6_creation_gate": "closed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.parse_args()
    print(json.dumps(validate(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
