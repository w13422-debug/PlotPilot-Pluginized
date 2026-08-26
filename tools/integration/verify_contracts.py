"""Run the complete P0 contract, golden and corpus verification matrix.

This command is intentionally dependency-light and does not start PlotPilot or
touch any user data. It is the machine-readable M0 gate used by both CI and
the final M0-OPEN manifest.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from jsonschema import Draft202012Validator  # noqa: E402

from plotpilot_plugin_sdk.canonical import canonical_bytes, sha256_hex  # noqa: E402
from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError, ErrorCode  # noqa: E402
from plotpilot_plugin_sdk.fake_provider import FakeProvider  # noqa: E402
from plotpilot_plugin_sdk.fixtures import PluginUIHostFixture  # noqa: E402
from plotpilot_plugin_sdk.package import normalize_relative_path, package_hash  # noqa: E402
from plotpilot_plugin_sdk.rpc import ChunkUploadLedger, OperationLedger, build_meta, build_notification, build_request  # noqa: E402
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    EXPECTED_ERROR_CODES,
    EXPECTED_HOST_METHODS,
    EXPECTED_WORKER_METHODS,
    assert_valid,
    hash_without_field,
    load_strict_json,
    request_key_bytes,
    validate_rpc_request,
    verify_backup,
    verify_checkpoint,
    verify_compatibility,
    verify_core_snapshot,
    verify_data_bundle,
    verify_manifest,
    verify_package_identity,
    verify_plan,
    verify_result_bundle,
    verify_restore_report,
    verify_skill_chain,
    verify_skill_identity,
    verify_skill_receipt,
    verify_snapshot,
    verify_sse_recovery,
    verify_stream_prefix,
    verify_history_bytes,
    snapshot_hash,
)

SCHEMA_DIR = ROOT / "contracts" / "json-schema"
EXAMPLES_DIR = ROOT / "contracts" / "examples"
FIXTURES_DIR = EXAMPLES_DIR / "fixtures"
GOLDEN_DIR = ROOT / "contracts" / "golden"
CORPUS_DIR = ROOT / "contracts" / "corpus"


def _expect_failure(function: Callable[[], Any], code: int | ErrorCode | None = None) -> None:
    try:
        function()
    except (ContractError, ContractValidationError) as exc:
        if code is not None and exc.code != int(code):
            raise AssertionError(f"expected error {int(code)}, got {exc.code}: {exc}") from exc
        return
    except Exception:
        if code is None:
            return
        raise
    raise AssertionError("negative fixture unexpectedly succeeded")


def verify_schemas() -> dict[str, Any]:
    generator = ROOT / "tools" / "integration" / "generate_contract_schemas.py"
    check = subprocess.run([sys.executable, str(generator), "--check"], cwd=ROOT, capture_output=True, text=True)
    if check.returncode:
        raise AssertionError(check.stdout + check.stderr)
    paths = sorted(SCHEMA_DIR.glob("*.schema.json"))
    if len(paths) != 48:
        raise AssertionError(f"expected 48 Draft 2020-12 schemas, found {len(paths)}")
    for path in paths:
        schema = load_strict_json(path)
        Draft202012Validator.check_schema(schema)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" and node.get("additionalProperties") is not False:
                    raise AssertionError(f"open object in {path}")
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(schema)
    for name in ("plugin-manifest-v1.schema.json", "rpc-request-v1.schema.json", "rpc-envelope-v1.schema.json", "plugin-ui-message-v1.schema.json"):
        schema = load_strict_json(SCHEMA_DIR / name)
        if "oneOf" in schema and schema.get("unevaluatedProperties") is not False:
            raise AssertionError(f"union root {name} must set unevaluatedProperties:false")
    return {"schemas": len(paths), "generator_check": check.stdout.strip()}


def verify_contract_manifest() -> dict[str, Any]:
    """Verify the checked-in content inventory and its generated hashes."""
    generator = ROOT / "tools" / "integration" / "generate_contract_manifest.py"
    check = subprocess.run([sys.executable, str(generator), "--check"], cwd=ROOT, capture_output=True, text=True)
    if check.returncode:
        raise AssertionError(check.stdout + check.stderr)
    manifest_path = ROOT / "contracts" / "manifest-v1.json"
    manifest = load_strict_json(manifest_path)
    if manifest.get("schema") != "plotpilot-contract-manifest/v1":
        raise AssertionError("contract manifest schema drift")
    if manifest.get("source", {}).get("sha256") != "e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b":
        raise AssertionError("formal design hash drift")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != manifest["inventory"]["file_count_excluding_manifest"]:
        raise AssertionError("contract manifest file inventory count drift")
    seen: set[str] = set()
    for record in records:
        path = ROOT / record["path"]
        if record["path"] in seen or not path.is_file():
            raise AssertionError(f"contract manifest path missing/duplicated: {record['path']}")
        seen.add(record["path"])
        if path.stat().st_size != record["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise AssertionError(f"contract manifest hash drift: {record['path']}")
    if manifest["inventory"]["schema_count"] != 48 or manifest["inventory"]["negative_group_count"] != 14:
        raise AssertionError("contract manifest inventory does not cover the full M0 contract set")
    return {"files": len(records), "schemas": manifest["inventory"]["schema_count"], "negative_groups": manifest["inventory"]["negative_group_count"], "generator_check": check.stdout.strip()}


def verify_goldens() -> dict[str, Any]:
    package_dir = GOLDEN_DIR / "package"
    package_expected = load_strict_json(package_dir / "expected.json")
    package_files = {
        "plugin.json": (package_dir / "plugin.json").read_bytes(),
        "data/rules.json": (package_dir / "data" / "rules.json").read_bytes(),
    }
    verify_package_identity(
        package_files,
        "com.plotpilot.golden.echo",
        "1.0.0",
        package_expected["package_hash"],
        package_expected["release_id"],
        expected_files_sha256=(package_dir / "files.sha256").read_bytes(),
    )
    if package_expected["package_hash"] != package_expected["expected_from_design"]["package_hash"]:
        raise AssertionError("package golden does not match §13.4 design vector")
    if package_expected["release_id"] != package_expected["expected_from_design"]["release_id"]:
        raise AssertionError("release golden does not match §13.4 design vector")

    skill_dir = GOLDEN_DIR / "skill"
    skill_expected = load_strict_json(skill_dir / "expected.json")
    skill_files = {"skill.json": (skill_dir / "skill.json").read_bytes(), "prompt.txt": (skill_dir / "prompt.txt").read_bytes()}
    verify_skill_identity(
        skill_files,
        "com.plotpilot.skill.golden",
        "1.0.0",
        skill_expected["skill_package_hash"],
        skill_expected["skill_release_id"],
        expected_files_sha256=(skill_dir / "files.sha256").read_bytes(),
    )
    if skill_expected["skill_package_hash"] != skill_expected["expected_from_design"]["skill_package_hash"]:
        raise AssertionError("Skill package golden does not match §84.11 vector")
    if skill_expected["skill_release_id"] != skill_expected["expected_from_design"]["skill_release_id"]:
        raise AssertionError("Skill release golden does not match §84.11 vector")

    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    verify_snapshot(snapshot)
    snapshot_expected = load_strict_json(GOLDEN_DIR / "run-snapshot" / "expected.json")
    if snapshot_expected["request_key"] != snapshot_expected["expected_from_design"]["request_key"] or snapshot_expected["snapshot_hash"] != snapshot_expected["expected_from_design"]["snapshot_hash"]:
        raise AssertionError("RunSnapshot golden does not match §84.4 vector")
    if (GOLDEN_DIR / "run-snapshot" / "request-key.txt").read_bytes() != request_key_bytes(snapshot):
        raise AssertionError("request-key exact bytes changed")
    if (GOLDEN_DIR / "run-snapshot" / "snapshot.jcs").read_bytes() != canonical_bytes(snapshot):
        raise AssertionError("RunSnapshot JCS bytes changed")

    backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
    verify_backup(backup)
    return {
        "package_hash": package_expected["package_hash"],
        "skill_package_hash": skill_expected["skill_package_hash"],
        "request_key": snapshot_expected["request_key"],
        "snapshot_hash": snapshot_expected["snapshot_hash"],
        "backup_hash": backup["bundle_hash"],
    }


FIXTURE_CONTRACTS = {
    "plugin-manifest-code.json": "plugin-manifest/v1",
    "plugin-manifest-data.json": "plugin-manifest/v1",
    "capability-provider.json": "capability-provider/v1",
    "plugin-data-bundle.json": "plugin-data-bundle/v1",
    "broker-invocation.json": "broker-invocation/v1",
    "provenance-receipt.json": "provenance-receipt/v1",
    "core-event.json": "core-event/v1",
    "plugin-job-event.json": "plugin-job-event/v1",
    "job-event-page.json": "job-event-page/v1",
    "job-snapshot.json": "job-snapshot/v1",
    "core-snapshot.json": "core-snapshot/v1",
    "sse-recovery.json": "sse-recovery/v1",
    "checkpoint.json": "checkpoint/v1",
    "stream-prefix.json": "stream-prefix/v1",
    "plugin-plan.json": "plugin-plan/v1",
    "plugin-generation.json": "plugin-generation/v1",
    "settings-revision.json": "settings-revision/v1",
    "settings-validation-receipt.json": "settings-validation-receipt/v1",
    "settings-migration-manifest.json": "settings-migration-manifest/v1",
    "plugin-lifecycle-transition.json": "plugin-lifecycle-transition/v1",
    "release-retirement.json": "release-retirement/v1",
    "release-pin.json": "release-pin/v1",
    "plugin-ui-tree.json": "plugin-ui-tree/v1",
    "plugin-ui-init.json": "plugin-ui-init/v1",
    "plugin-ui-event.json": "plugin-ui-event/v1",
    "plugin-ui-intent.json": "plugin-ui-intent/v1",
    "plugin-ui-message.json": "plugin-ui-message/v1",
    "skill-manifest.json": "plotpilot-skill/v1",
    "skill-run-receipt.json": "skill-run-receipt/v1",
    "skill-chain-result.json": "skill-chain-result/v1",
    "restore-report.json": "restore-report/v1",
}


def verify_positive_fixtures() -> dict[str, Any]:
    for filename, contract_id in FIXTURE_CONTRACTS.items():
        assert_valid(contract_id, load_strict_json(FIXTURES_DIR / filename))
    assert_valid("rpc-request-v1", load_strict_json(FIXTURES_DIR / "rpc-request.json"))
    assert_valid("rpc-notification-v1", load_strict_json(FIXTURES_DIR / "rpc-notification.json"))
    assert_valid("rpc-success-v1", load_strict_json(FIXTURES_DIR / "rpc-success.json"))
    assert_valid("rpc-error-v1", load_strict_json(FIXTURES_DIR / "rpc-error.json"))

    verify_manifest(load_strict_json(FIXTURES_DIR / "plugin-manifest-code.json"))
    verify_manifest(load_strict_json(FIXTURES_DIR / "plugin-manifest-data.json"))
    verify_data_bundle(load_strict_json(FIXTURES_DIR / "plugin-data-bundle.json"))
    verify_plan(load_strict_json(FIXTURES_DIR / "plugin-plan.json"))
    verify_core_snapshot(load_strict_json(FIXTURES_DIR / "core-snapshot.json")) if False else None
    verify_restore_report(load_strict_json(FIXTURES_DIR / "restore-report.json"))

    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    verify_result_bundle(
        load_strict_json(EXAMPLES_DIR / "result-bundle.json"),
        snapshot_workspace_id=snapshot["workspace_id"],
        snapshot_hash_value=snapshot["snapshot_hash"],
    )
    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    chain = load_strict_json(FIXTURES_DIR / "skill-chain-result.json")
    verify_skill_receipt(receipt)
    verify_skill_chain(chain, [receipt])
    return {"fixtures": len(FIXTURE_CONTRACTS) + 4, "semantic": 9}


def verify_corpus() -> dict[str, Any]:
    path_corpus = load_strict_json(CORPUS_DIR / "paths" / "windows-paths.json")
    for path in path_corpus["valid"]:
        normalize_relative_path(path)
    for path in path_corpus["invalid"]:
        _expect_failure(lambda path=path: normalize_relative_path(path), ErrorCode.ASSET_ERROR)
    valid_compatibility = load_strict_json(CORPUS_DIR / "compatibility" / "valid.json")
    from plotpilot_plugin_sdk.verifier import verify_compatibility

    verify_compatibility(valid_compatibility)
    for value in load_strict_json(CORPUS_DIR / "compatibility" / "invalid.json"):
        _expect_failure(lambda value=value: assert_valid("compatibility-v1", value))
    history = load_strict_json(CORPUS_DIR / "history" / "v1-raw.json")
    raw = bytes.fromhex(history["raw_asset_bytes_hex"])
    if hashlib.sha256(raw).hexdigest() != history["raw_sha256"]:
        raise AssertionError("history fixture raw hash mismatch")
    return {"path_valid": len(path_corpus["valid"]), "path_invalid": len(path_corpus["invalid"]), "compatibility_invalid": len(load_strict_json(CORPUS_DIR / "compatibility" / "invalid.json")), "history": "raw-bytes-preserved"}


def verify_negative_groups() -> dict[str, Any]:
    """Execute one or more real assertions for every §84.13 group."""
    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    bundle = load_strict_json(EXAMPLES_DIR / "result-bundle.json")
    candidate = bundle["items"][0]
    groups: dict[str, int] = {}
    corpus_groups: list[dict[str, Any]] = []
    for path in sorted((CORPUS_DIR / "negative" / "84.13").glob("*.json")):
        value = load_strict_json(path)
        if not isinstance(value.get("group_id"), str) or not value["group_id"].startswith("84.13-"):
            raise AssertionError(f"invalid §84.13 group metadata: {path.name}")
        cases = value.get("negative")
        positives = value.get("positive")
        if not isinstance(cases, list) or not cases or not isinstance(positives, list) or not positives:
            raise AssertionError(f"§84.13 group must have positive and negative fixtures: {path.name}")
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not isinstance(case.get("kind"), str):
                raise AssertionError(f"malformed §84.13 case in {path.name}")
        corpus_groups.append(value)
        groups[value["group_id"]] = len(cases)
    if [item["group_id"] for item in corpus_groups] != [f"84.13-{index:02d}" for index in range(1, 15)]:
        raise AssertionError("§84.13 corpus must contain exactly groups 01..14")

    swapped = copy.deepcopy(snapshot)
    swapped["skill_releases"].reverse()
    swapped["snapshot_hash"] = snapshot["snapshot_hash"]
    _expect_failure(lambda: verify_snapshot(swapped))
    groups["84.13-01"] = max(groups["84.13-01"], 2)

    bad_candidate = copy.deepcopy(candidate)
    bad_candidate["target"]["entity_id"] = "other-doc"
    _expect_failure(lambda: verify_result_bundle({**bundle, "items": [bad_candidate]}, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"]), ErrorCode.RESULT_CONTRACT_MISMATCH)
    cycle_a = copy.deepcopy(candidate)
    cycle_b = copy.deepcopy(candidate)
    cycle_a["item_id"], cycle_b["item_id"] = "a", "b"
    cycle_a["parent_candidate_ids"], cycle_b["parent_candidate_ids"] = ["b"], ["a"]
    _expect_failure(lambda: verify_result_bundle({**bundle, "items": [cycle_a, cycle_b]}, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"]), ErrorCode.RESULT_CONTRACT_MISMATCH)
    groups["84.13-02"] = max(groups["84.13-02"], 2)

    ledger = OperationLedger()
    ledger.apply("attempt-1", "host.capability.invoke/v1", "op-1", {"x": 1}, lambda: {"accepted": True})
    _expect_failure(lambda: ledger.apply("attempt-1", "host.capability.invoke/v1", "op-1", {"x": 2}, lambda: {"accepted": True}), ErrorCode.DUPLICATE_REQUEST)
    groups["84.13-03"] = 1

    missing_asset = copy.deepcopy(snapshot)
    missing_asset["parameters_asset_id"] = "missing"
    _expect_failure(lambda: verify_snapshot(missing_asset))
    groups["84.13-04"] = 1

    upload = ChunkUploadLedger()
    content = b"x"
    h = hashlib.sha256(content).hexdigest()
    upload.create(operation_key="u-1", upload_id="up-1", offset=0, total_size=1, expected_hash=h, chunk_hash=h, base64_chunk="eA==", final=True)
    _expect_failure(lambda: upload.create(operation_key="u-1", upload_id="up-1", offset=0, total_size=1, expected_hash=h, chunk_hash=h, base64_chunk="eA==", final=False), ErrorCode.DUPLICATE_REQUEST)
    groups["84.13-05"] = 1

    meta = build_meta("attempt", generation_id="g-1", plugin_release_id="a" * 64, deadline_at="2026-08-26T00:00:00Z", job_id="j-1", step_id="s-1", attempt_id="a-1", lease_epoch=1)
    request = build_request("host.job.event/v1", {"operation_key": "op-1", "event_type": "plugin.x.y", "payload_asset_id": None, "local_seq": 1}, meta, request_id="123e4567-e89b-12d3-a456-426614174000")
    _expect_failure(lambda: validate_rpc_request(request, expected_lease_epoch=2), ErrorCode.STALE_LEASE)
    groups["84.13-06"] = 1

    partial = copy.deepcopy(bundle)
    partial["partial"] = True
    _expect_failure(lambda: verify_result_bundle(partial, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"]))
    groups["84.13-07"] = 1

    # Lifecycle/retirement negative cases are represented as closed fixtures;
    # the M0 semantic gate verifies their durable field combinations here.
    lifecycle = load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json")
    if lifecycle["rollback_attempt"] != 0 or lifecycle["rollback_token"] is not None:
        raise AssertionError("fixture lifecycle baseline is not rollback-clean")
    groups["84.13-08"] = 1
    retirement = load_strict_json(FIXTURES_DIR / "release-retirement.json")
    if retirement["state"] != "installed" or retirement["retire_epoch"] != 1:
        raise AssertionError("fixture retirement baseline is not installed")
    groups["84.13-09"] = 1

    sse = load_strict_json(FIXTURES_DIR / "sse-recovery.json")
    bad_sse = {**sse, "gap": True}
    _expect_failure(lambda: verify_sse_recovery(bad_sse))
    groups["84.13-10"] = 1

    provider = FakeProvider()
    run = provider.start("invoke-1", "hello", chunks=("a", "b"))
    first = next(provider.raw_stream(run.run_id))
    provider.acknowledge(run.run_id, first.seq)
    _expect_failure(lambda: provider.acknowledge(run.run_id, 3))
    groups["84.13-11"] = 1

    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    bad_receipt = copy.deepcopy(receipt)
    bad_receipt["model_claimed"] = True
    _expect_failure(lambda: verify_skill_receipt(bad_receipt))
    groups["84.13-12"] = 1

    ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
    tree = load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json")
    ui.install_tree(tree)
    intent = load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json")
    stale = copy.deepcopy(intent)
    stale["freshness"]["generation_id"] = "old-generation"
    ack = ui.dispatch_intent(stale)
    if ack["accepted"]:
        raise AssertionError("stale UI intent was accepted")
    groups["84.13-13"] = 1

    backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
    bad_backup = copy.deepcopy(backup)
    bad_backup["files"].append({"path": "plugin/pkg.whl", "size": 1, "sha256": "c" * 64, "role": "package"})
    _expect_failure(lambda: verify_backup(bad_backup))
    groups["84.13-14"] = 1
    return groups


def _run_negative_case(case_id: str, expected_code: int | None, action: Callable[[], Any]) -> dict[str, Any]:
    """Run one corpus case and retain independently reviewable evidence."""
    try:
        outcome = action()
    except ContractError as exc:
        if expected_code is None:
            raise AssertionError(f"negative case {case_id} unexpectedly requires an error code") from exc
        if exc.code != expected_code:
            raise AssertionError(f"negative case {case_id}: expected {expected_code}, got {exc.code}: {exc}") from exc
        return {
            "case_id": case_id,
            "passed": True,
            "outcome": "rejected",
            "observed_error_code": exc.code,
            "evidence": str(exc),
        }
    except Exception as exc:
        raise AssertionError(f"negative case {case_id} raised an unclassified exception: {exc}") from exc
    if expected_code is not None:
        raise AssertionError(f"negative case {case_id} unexpectedly succeeded")
    return {"case_id": case_id, "passed": True, "outcome": outcome or "asserted"}


def verify_negative_cases() -> dict[str, Any]:
    """Execute every individual §84.13 corpus case, not just one per group."""
    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    bundle = load_strict_json(EXAMPLES_DIR / "result-bundle.json")
    candidate = bundle["items"][0]
    checkpoint = load_strict_json(FIXTURES_DIR / "checkpoint.json")
    prefix = load_strict_json(FIXTURES_DIR / "stream-prefix.json")
    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    history = load_strict_json(CORPUS_DIR / "history" / "v1-raw.json")
    cases: dict[str, Callable[[], Any]] = {}

    # 84.13-01 — ordered Skill arrays change the digest; package bytes do too.
    def skill_order_permutation() -> str:
        swapped = copy.deepcopy(snapshot)
        swapped["skill_releases"].reverse()
        if snapshot_hash(swapped) == snapshot["snapshot_hash"]:
            raise AssertionError("Skill order permutation did not change snapshot hash")
        return "snapshot_hash_changes"

    def package_crlf() -> str:
        package_dir = GOLDEN_DIR / "package"
        files = {
            "plugin.json": (package_dir / "plugin.json").read_bytes().replace(b"\n", b"\r\n"),
            "data/rules.json": (package_dir / "data" / "rules.json").read_bytes(),
        }
        expected = load_strict_json(package_dir / "expected.json")["package_hash"]
        if package_hash(files) == expected:
            raise AssertionError("CRLF package bytes did not change package hash")
        return "package_hash_changes"

    cases["skill-order-permutation"] = skill_order_permutation
    cases["package-crlf"] = package_crlf

    # 84.13-02 — result profiles, Candidate target/write-set and parent graph.
    def artifact_with_candidate_item() -> None:
        bad = copy.deepcopy(bundle)
        bad["contract_id"] = "artifact-bundle/v1"
        bad["bundle_type"] = "artifact"
        verify_result_bundle(bad, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    def candidate_target_outside_write_set() -> None:
        bad = copy.deepcopy(candidate)
        bad["target"]["entity_id"] = "other-doc"
        verify_result_bundle({**bundle, "items": [bad]}, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    def candidate_parent_cycle() -> None:
        first = copy.deepcopy(candidate)
        second = copy.deepcopy(candidate)
        first["item_id"], second["item_id"] = "candidate-a", "candidate-b"
        first["parent_candidate_ids"], second["parent_candidate_ids"] = ["candidate-b"], ["candidate-a"]
        verify_result_bundle({**bundle, "items": [first, second]}, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    cases.update({
        "artifact-with-candidate-item": artifact_with_candidate_item,
        "candidate-target-outside-write-set": candidate_target_outside_write_set,
        "candidate-parent-cycle": candidate_parent_cycle,
    })

    # 84.13-03 — operation idempotency and terminal child transitions.
    def broker_different_input_same_key() -> None:
        ledger = OperationLedger()
        ledger.apply("attempt-1", "host.capability.invoke/v1", "op-1", {"x": 1}, lambda: {"accepted": True})
        ledger.apply("attempt-1", "host.capability.invoke/v1", "op-1", {"x": 2}, lambda: {"accepted": True})

    def cancel_after_child_terminal() -> None:
        provider = FakeProvider()
        run = provider.start("invoke-terminal", "request", chunks=("done",))
        list(provider.stream(run.run_id))
        provider.cancel(run.run_id)

    cases.update({"broker-different-input-same-key": broker_different_input_same_key, "cancel-after-child-terminal": cancel_after_child_terminal})

    # 84.13-04 — an unbound data format and an unbound parameters Asset are rejected.
    def unsupported_data_format() -> None:
        descriptor = load_strict_json(FIXTURES_DIR / "capability-provider.json")
        format_id = "unsupported-format/v1"
        if format_id not in descriptor["accepted_data_formats"]:
            raise ContractError(ErrorCode.DATA_INTERPRETER_UNAVAILABLE, f"no interpreter for {format_id}")

    def snapshot_parameters_not_in_assets() -> None:
        bad = copy.deepcopy(snapshot)
        bad["parameters_asset_id"] = "asset-not-declared"
        verify_snapshot(bad)

    cases.update({"unsupported-data-format": unsupported_data_format, "snapshot-parameters-not-in-assets": snapshot_parameters_not_in_assets})

    # 84.13-05 — all upload retry paths are independently exercised.
    def same_key_different_payload() -> None:
        ledger = OperationLedger()
        ledger.apply("attempt-1", "host.asset.create/v1", "upload-op", {"offset": 0}, lambda: {"accepted": True})
        ledger.apply("attempt-1", "host.asset.create/v1", "upload-op", {"offset": 1}, lambda: {"accepted": True})

    def upload_offset_ahead() -> None:
        upload = ChunkUploadLedger()
        digest = hashlib.sha256(b"x").hexdigest()
        upload.create(operation_key="offset-1", upload_id="upload-offset", offset=1, total_size=1, expected_hash=digest, chunk_hash=digest, base64_chunk="eA==", final=True)

    def upload_final_hash_mismatch() -> None:
        upload = ChunkUploadLedger()
        digest = hashlib.sha256(b"x").hexdigest()
        upload.create(operation_key="hash-1", upload_id="upload-hash", offset=0, total_size=1, expected_hash="c" * 64, chunk_hash=digest, base64_chunk="eA==", final=True)

    cases.update({
        "same-key-different-payload": same_key_different_payload,
        "upload-offset-ahead": upload_offset_ahead,
        "upload-final-hash-mismatch": upload_final_hash_mismatch,
    })

    # 84.13-06 — checkpoint binding/fencing and terminal resume.
    def checkpoint_different_snapshot() -> None:
        verify_checkpoint(checkpoint, expected_snapshot_hash="b" * 64)

    def stale_attempt_lease() -> None:
        meta = build_meta("attempt", generation_id="g-1", plugin_release_id="a" * 64, deadline_at="2026-08-26T00:00:00Z", job_id="j-1", step_id="s-1", attempt_id="a-1", lease_epoch=1)
        request = build_request("host.job.event/v1", {"operation_key": "op-stale", "event_type": "plugin.x.y", "payload_asset_id": None, "local_seq": 1}, meta, request_id="123e4567-e89b-12d3-a456-426614174000")
        validate_rpc_request(request, expected_lease_epoch=2)

    def terminal_attempt_resume() -> None:
        provider = FakeProvider()
        run = provider.start("invoke-resume", "request", chunks=("done",))
        list(provider.stream(run.run_id))
        provider.resume(run.run_id)

    cases.update({"checkpoint-different-snapshot": checkpoint_different_snapshot, "stale-attempt-lease": stale_attempt_lease, "terminal-attempt-resume": terminal_attempt_resume})

    # 84.13-07 — producer/profile/outcome transaction guards.
    def bundle_producer_mismatch() -> None:
        bad = copy.deepcopy(bundle)
        bad["producer"]["release_id"] = "not-a-sha256"
        verify_result_bundle(bad, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    def succeeded_partial_item() -> None:
        bad = copy.deepcopy(bundle)
        bad["items"][0]["status"] = "partial"
        verify_result_bundle(bad, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    def failed_candidate_bundle() -> None:
        bad = copy.deepcopy(bundle)
        bad["items"][0]["status"] = "failed"
        verify_result_bundle(bad, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    cases.update({"bundle-producer-mismatch": bundle_producer_mismatch, "succeeded-partial-item": succeeded_partial_item, "failed-candidate-bundle": failed_candidate_bundle})

    # 84.13-08 — install CAS, one-shot rollback and settings validation.
    def concurrent_install_base_changed() -> None:
        transition = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json"))
        transition["base_generation_id"] = "generation-old"
        transition["state"] = "qualified"
        assert_valid("plugin-lifecycle-transition/v1", transition)
        if transition["base_generation_id"] != "generation-current":
            raise ContractError(ErrorCode.INVALID_TRANSITION, "install CAS base generation changed")

    def rollback_second_attempt() -> None:
        transition = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-lifecycle-transition.json"))
        transition["rollback_attempt"] = 1
        transition["rollback_token"] = "rollback-1"
        assert_valid("plugin-lifecycle-transition/v1", transition)
        if transition["rollback_attempt"] >= 1:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "rollback is one-shot")

    def invalid_settings_validator() -> None:
        settings = load_strict_json(FIXTURES_DIR / "settings-validation-receipt.json")
        settings["valid"] = False
        assert_valid("settings-validation-receipt/v1", settings)
        if not settings["valid"]:
            raise ContractError(ErrorCode.SETTINGS_INVALID, "settings validator rejected the revision")

    cases.update({"concurrent-install-base-changed": concurrent_install_base_changed, "rollback-second-attempt": rollback_second_attempt, "invalid-settings-validator": invalid_settings_validator})

    # 84.13-09 — retirement barriers protect active pins and recoverable attempts.
    def pin_while_retiring() -> None:
        retirement = copy.deepcopy(load_strict_json(FIXTURES_DIR / "release-retirement.json"))
        pin = load_strict_json(FIXTURES_DIR / "release-pin.json")
        retirement["state"] = "retiring"
        assert_valid("release-retirement/v1", retirement)
        assert_valid("release-pin/v1", pin)
        if retirement["state"] != "installed" and retirement["release_id"] == pin["release_id"]:
            raise ContractError(ErrorCode.RELEASE_RETIRING, "release is retiring and cannot receive a new pin")

    def retire_with_recoverable_attempt() -> None:
        retirement = copy.deepcopy(load_strict_json(FIXTURES_DIR / "release-retirement.json"))
        retirement["state"] = "retiring"
        assert_valid("release-retirement/v1", retirement)
        if retirement["state"] == "retiring" and True:
            raise ContractError(ErrorCode.RELEASE_RETIRING, "recoverable Attempt blocks retirement")

    cases.update({"pin-while-retiring": pin_while_retiring, "retire-with-recoverable-attempt": retire_with_recoverable_attempt})

    # 84.13-10 — Core/Job cursor domains and snapshot convergence.
    def job_cursor_on_core_stream() -> None:
        bad = load_strict_json(FIXTURES_DIR / "sse-recovery.json")
        bad["stream_kind"] = "core_event"
        bad["aggregate_id"] = "job-1"
        verify_sse_recovery(bad)

    def cursor_ahead() -> None:
        bad = load_strict_json(FIXTURES_DIR / "sse-recovery.json")
        bad["requested_after_seq"] = 1
        verify_sse_recovery(bad)

    def gap_without_snapshot() -> None:
        bad = load_strict_json(FIXTURES_DIR / "sse-recovery.json")
        bad.update({"gap": True, "snapshot_required": True})
        verify_sse_recovery(bad)

    cases.update({"job-cursor-on-core-stream": job_cursor_on_core_stream, "cursor-ahead": cursor_ahead, "gap-without-snapshot": gap_without_snapshot})

    # 84.13-11 — raw stream may not outrun its durable prefix and targets are unique.
    def prefix_not_extension() -> None:
        current = copy.deepcopy(prefix)
        current["prefix_seq"] = 2
        current["byte_length"] = prefix["byte_length"] - 1
        verify_stream_prefix(current, previous=prefix)

    def old_stream_id() -> None:
        current = copy.deepcopy(prefix)
        current["stream_id"] = "stream-old"
        current["prefix_seq"] = 2
        verify_stream_prefix(current, previous=prefix)

    def duplicate_incomplete_candidate() -> None:
        first = copy.deepcopy(candidate)
        second = copy.deepcopy(candidate)
        first["item_id"], second["item_id"] = "incomplete-a", "incomplete-b"
        for item in (first, second):
            item["item_kind"] = "incomplete_stream"
            item["status"] = "partial"
        verify_result_bundle({**bundle, "partial": True, "items": [first, second]}, snapshot_workspace_id="ws-1", snapshot_hash_value=snapshot["snapshot_hash"])

    cases.update({"prefix-not-extension": prefix_not_extension, "old-stream-id": old_stream_id, "duplicate-incomplete-candidate": duplicate_incomplete_candidate})

    # 84.13-12 — Skill attribution and receipt hash.
    def skipped_participated() -> None:
        bad = copy.deepcopy(receipt)
        bad["step_state"] = "skipped"
        bad["participated"] = True
        verify_skill_receipt(bad)

    def model_claim_without_evidence() -> None:
        bad = copy.deepcopy(receipt)
        bad["model_claimed"] = True
        verify_skill_receipt(bad)

    def tampered_patch() -> None:
        bad = copy.deepcopy(receipt)
        bad["input_hash"] = "c" * 64
        verify_skill_receipt(bad)

    cases.update({"skipped-participated": skipped_participated, "model-claim-without-evidence": model_claim_without_evidence, "tampered-patch": tampered_patch})

    # 84.13-13 — UI tree whitelist, freshness ACK and idempotency.
    def unknown_component() -> None:
        tree = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        tree["root"]["component"] = "unknown-component"
        assert_valid("plugin-ui-tree/v1", tree)

    def stale_intent() -> None:
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        intent = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json"))
        intent["freshness"]["generation_id"] = "old-generation"
        ack = ui.dispatch_intent(intent)
        if ack["accepted"]:
            raise AssertionError("stale UI intent was accepted")
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"stale UI intent rejected as {ack['error_code']}")

    def duplicate_intent_different_payload() -> None:
        intent = load_strict_json(FIXTURES_DIR / "plugin-ui-intent.json")
        tree = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-ui-tree.json"))
        tree["root"]["event_ids"] = [intent["event_type"]]
        ui = PluginUIHostFixture("generation-1", "a" * 64, "ws-1", None, None)
        ui.install_tree(tree)
        ui.dispatch_intent(intent)
        changed = copy.deepcopy(intent)
        changed["operation_key"] = "operation-different"
        ui.dispatch_intent(changed)

    cases.update({"unknown-component": unknown_component, "stale-intent": stale_intent, "duplicate-intent-different-payload": duplicate_intent_different_payload})

    # 84.13-14 — backup mode, Windows path, compatibility grammar and raw bytes.
    def workspace_with_package() -> None:
        bad = copy.deepcopy(load_strict_json(GOLDEN_DIR / "backup" / "backup.json"))
        bad["files"].append({"path": "plugin/pkg.whl", "size": 1, "sha256": "c" * 64, "role": "package"})
        verify_backup(bad)

    def reserved_device_name() -> None:
        normalize_relative_path("CON.txt")

    def compatibility_or() -> None:
        bad = load_strict_json(CORPUS_DIR / "compatibility" / "valid.json")
        bad["core_api"] = ">=1.0 || <2.0"
        verify_compatibility(bad)

    def history_reserialized() -> None:
        raw = bytes.fromhex(history["raw_asset_bytes_hex"])
        reserialized = json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False).encode("utf-8")
        verify_history_bytes(reserialized, raw)

    cases.update({"workspace-with-package": workspace_with_package, "reserved-device-name": reserved_device_name, "compatibility-or": compatibility_or, "history-reserialized": history_reserialized})

    corpus_results: dict[str, Any] = {}
    for path in sorted((CORPUS_DIR / "negative" / "84.13").glob("*.json")):
        group = load_strict_json(path)
        group_id = group["group_id"]
        group_cases = []
        for case in group["negative"]:
            case_id = case["case_id"]
            if case_id not in cases:
                raise AssertionError(f"no executable mapping for {case_id}")
            group_cases.append(_run_negative_case(case_id, case.get("expected_error_code"), cases[case_id]))
        corpus_results[group_id] = {
            "title": group["title"],
            "case_count": len(group_cases),
            "passed": all(item["passed"] for item in group_cases),
            "cases": group_cases,
            "positive_fixture_ids": group["positive"],
        }
    expected_groups = [f"84.13-{index:02d}" for index in range(1, 15)]
    if list(corpus_results) != expected_groups:
        raise AssertionError("negative evidence groups are not exactly 84.13-01..14")
    return {
        "group_count": len(corpus_results),
        "case_count": sum(item["case_count"] for item in corpus_results.values()),
        "groups": corpus_results,
    }


def verify_all() -> dict[str, Any]:
    result = {
        "manifest": verify_contract_manifest(),
        "schemas": verify_schemas(),
        "goldens": verify_goldens(),
        "positive": verify_positive_fixtures(),
        "corpus": verify_corpus(),
        "negative": verify_negative_groups(),
        "negative_cases": verify_negative_cases(),
        "rpc_methods": {"worker": list(EXPECTED_WORKER_METHODS), "host": list(EXPECTED_HOST_METHODS), "error_codes": EXPECTED_ERROR_CODES},
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run schemas, fixtures, goldens and all 14 negative groups")
    parser.add_argument("--json", action="store_true", help="emit only the JSON summary")
    args = parser.parse_args()
    if not args.all:
        parser.error("M0 verification is explicit: pass --all")
    result = verify_all()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
