from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode  # noqa: E402
from plotpilot_plugin_sdk.package import build_files_sha256  # noqa: E402
from verify_contracts import (  # noqa: E402
    FIXTURES_DIR,
    GOLDEN_DIR,
    hash_without_field,
    load_strict_json,
    verify_backup,
    verify_capability_descriptor,
    verify_checkpoint,
    verify_core_snapshot,
    verify_data_bundle,
    verify_job_snapshot,
    verify_manifest,
    verify_package_manifest,
    verify_positive_fixtures,
    verify_provenance_receipt,
    verify_result_bundle,
    verify_settings_validation_receipt,
    verify_skill_chain,
    verify_skill_receipt,
)


def _assert_rejected(action, code: ErrorCode) -> None:
    with pytest.raises(ContractError) as caught:
        action()
    assert caught.value.code == int(code)


def test_all_self_hash_positive_fixtures_are_strict_and_tamper_negative() -> None:
    assert verify_positive_fixtures()["self_hashes"] == 9

    self_hash_cases = (
        ("plugin-data-bundle.json", "bundle_hash", "plugin-data-bundle/v1", verify_data_bundle),
        ("core-snapshot.json", "snapshot_hash", "core-snapshot/v1", verify_core_snapshot),
        ("job-snapshot.json", "snapshot_hash", "job-snapshot/v1", verify_job_snapshot),
        ("checkpoint.json", "checkpoint_hash", "checkpoint/v1", verify_checkpoint),
        ("settings-validation-receipt.json", "receipt_hash", "settings-validation-receipt/v1", verify_settings_validation_receipt),
        ("provenance-receipt.json", "receipt_hash", "provenance-receipt/v1", verify_provenance_receipt),
    )
    for filename, field, prefix, verifier in self_hash_cases:
        value = load_strict_json(FIXTURES_DIR / filename)
        verifier(value)
        assert value[field] == hash_without_field(value, field, prefix)
        tampered = copy.deepcopy(value)
        tampered[field] = "0" * 64
        _assert_rejected(lambda tampered=tampered, verifier=verifier: verifier(tampered), ErrorCode.RESULT_CONTRACT_MISMATCH)

    backup = load_strict_json(GOLDEN_DIR / "backup" / "backup.json")
    verify_backup(backup)
    tampered_backup = copy.deepcopy(backup)
    tampered_backup["bundle_hash"] = "0" * 64
    _assert_rejected(lambda: verify_backup(tampered_backup), ErrorCode.RESULT_CONTRACT_MISMATCH)

    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    chain = load_strict_json(FIXTURES_DIR / "skill-chain-result.json")
    verify_skill_receipt(receipt)
    verify_skill_chain(chain, [receipt])
    tampered_receipt = copy.deepcopy(receipt)
    tampered_receipt["receipt_hash"] = "0" * 64
    _assert_rejected(lambda: verify_skill_receipt(tampered_receipt), ErrorCode.RESULT_CONTRACT_MISMATCH)
    tampered_chain = copy.deepcopy(chain)
    tampered_chain["chain_hash"] = "0" * 64
    _assert_rejected(lambda: verify_skill_chain(tampered_chain, [receipt]), ErrorCode.RESULT_CONTRACT_MISMATCH)


def test_data_bundle_hash_unicode_collision_and_skill_manifest_exact_bytes() -> None:
    bundle = load_strict_json(FIXTURES_DIR / "plugin-data-bundle.json")
    verify_data_bundle(bundle)
    tampered = copy.deepcopy(bundle)
    tampered["bundle_hash"] = "0" * 64
    _assert_rejected(lambda: verify_data_bundle(tampered), ErrorCode.RESULT_CONTRACT_MISMATCH)

    _assert_rejected(
        lambda: build_files_sha256({"straße.txt": b"x", "strasse.txt": b"y"}),
        ErrorCode.ASSET_ERROR,
    )
    _assert_rejected(
        lambda: build_files_sha256({"cafe\u0301.txt": b"x", "caf\u00e9.txt": b"y"}),
        ErrorCode.ASSET_ERROR,
    )

    skill_dir = GOLDEN_DIR / "skill"
    files = {
        "skill.json": (skill_dir / "skill.json").read_bytes(),
        "prompt.txt": (skill_dir / "prompt.txt").read_bytes(),
    }
    manifest = build_files_sha256(files)
    verify_package_manifest(files, manifest)
    reordered = b"".join(reversed(manifest.splitlines(keepends=True)))
    _assert_rejected(lambda: verify_package_manifest(files, reordered), ErrorCode.RESULT_CONTRACT_MISMATCH)


def test_result_profile_workspace_duplicate_ids_and_candidate_write_binding() -> None:
    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    bundle = load_strict_json(ROOT / "contracts" / "examples" / "result-bundle.json")
    candidate = bundle["items"][0]
    verify_result_bundle(bundle, snapshot_workspace_id=snapshot["workspace_id"], snapshot_hash_value=snapshot["snapshot_hash"])

    _assert_rejected(
        lambda: verify_result_bundle(bundle, snapshot_workspace_id="workspace-other", snapshot_hash_value=snapshot["snapshot_hash"]),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )
    duplicate = copy.deepcopy(candidate)
    _assert_rejected(
        lambda: verify_result_bundle(
            {**bundle, "items": [candidate, duplicate]},
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        ),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )

    base_mismatch = copy.deepcopy(candidate)
    base_mismatch["base"]["revision_id"] = "revision-other"
    _assert_rejected(
        lambda: verify_result_bundle(
            {**bundle, "items": [base_mismatch]},
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        ),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )
    cross_workspace = copy.deepcopy(candidate)
    cross_workspace["write_set"][0]["workspace_id"] = "workspace-other"
    _assert_rejected(
        lambda: verify_result_bundle(
            {**bundle, "items": [cross_workspace]},
            snapshot_workspace_id=snapshot["workspace_id"],
            snapshot_hash_value=snapshot["snapshot_hash"],
        ),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )


def test_checkpoint_heartbeat_rpc_capability_and_ui_refs_are_bound() -> None:
    checkpoint = load_strict_json(FIXTURES_DIR / "checkpoint.json")
    verify_checkpoint(checkpoint, expected_snapshot_hash=checkpoint["run_snapshot_hash"], previous_seq=0)
    _assert_rejected(lambda: verify_checkpoint(checkpoint, previous_seq=checkpoint["checkpoint_seq"]), ErrorCode.CHECKPOINT_INVALID)
    _assert_rejected(lambda: verify_checkpoint(checkpoint, expected_snapshot_hash="b" * 64), ErrorCode.CHECKPOINT_INVALID)

    from plotpilot_plugin_sdk.verifier import validate_rpc_request, validate_rpc_response, validate_rpc_result  # noqa: E402

    heartbeat = load_strict_json(FIXTURES_DIR / "rpc-notification.json")
    validate_rpc_request(heartbeat, expected_lease_epoch=1)
    _assert_rejected(lambda: validate_rpc_request(heartbeat, expected_lease_epoch=2), ErrorCode.STALE_LEASE)

    request = load_strict_json(FIXTURES_DIR / "rpc-request.json")
    validate_rpc_request(request)
    wrong_result = load_strict_json(FIXTURES_DIR / "rpc-success.json")
    _assert_rejected(
        lambda: validate_rpc_response(wrong_result, "host.asset.create/v1", request=request),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )
    _assert_rejected(
        lambda: validate_rpc_result("job.pause", {"accepted": True, "checkpoint_asset_id": None}),
        ErrorCode.CHECKPOINT_INVALID,
    )

    descriptor = load_strict_json(FIXTURES_DIR / "capability-provider.json")
    verify_capability_descriptor(descriptor, expected_capability_id=descriptor["capability_id"])
    _assert_rejected(
        lambda: verify_capability_descriptor(descriptor, expected_capability_id="unknown.capability/v1"),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )
    invalid_descriptor = copy.deepcopy(descriptor)
    invalid_descriptor["supports"].append(invalid_descriptor["supports"][0])
    _assert_rejected(lambda: verify_capability_descriptor(invalid_descriptor), ErrorCode.RESULT_CONTRACT_MISMATCH)

    manifest = copy.deepcopy(load_strict_json(FIXTURES_DIR / "plugin-manifest-code.json"))
    manifest["ui"] = {
        "entry": "ui.js",
        "runtime": "worker-ui/v1",
        "contributions": [{"contribution_id": "contribution-1", "slot": "workbench.writing-assets.panel", "capability_id": "unknown.capability/v1"}],
    }
    _assert_rejected(lambda: verify_manifest(manifest), ErrorCode.RESULT_CONTRACT_MISMATCH)


def test_skill_chain_ref_pairing_and_bundleless_failed_receipt() -> None:
    bundle = load_strict_json(ROOT / "contracts" / "examples" / "result-bundle.json")
    snapshot = load_strict_json(GOLDEN_DIR / "run-snapshot" / "snapshot.json")
    candidate = bundle["items"][0]
    bad_ref_bundle = copy.deepcopy(bundle)
    bad_ref_bundle["skill_chain_result_refs"] = [{
        "schema": "skill-chain-ref/v1",
        "chain_result_id": "chain-1",
        "asset_id": "asset-chain-1",
        "asset_hash": None,
        "result_bundle_id": bundle["bundle_id"],
        "result_item_id": candidate["item_id"],
        "stream_id": None,
        "acked_prefix_hash": None,
    }]
    _assert_rejected(
        lambda: verify_result_bundle(bad_ref_bundle, snapshot_workspace_id=snapshot["workspace_id"], snapshot_hash_value=snapshot["snapshot_hash"]),
        ErrorCode.RESULT_CONTRACT_MISMATCH,
    )

    receipt = load_strict_json(FIXTURES_DIR / "skill-run-receipt.json")
    failed = copy.deepcopy(receipt)
    failed.update({"result_bundle_id": None, "result_item_id": None, "step_state": "failed"})
    failed["receipt_hash"] = hash_without_field(failed, "receipt_hash", "skill-run-receipt/v1")
    verify_skill_receipt(failed)

    executed_bundleless = copy.deepcopy(failed)
    executed_bundleless["step_state"] = "executed"
    executed_bundleless["receipt_hash"] = hash_without_field(executed_bundleless, "receipt_hash", "skill-run-receipt/v1")
    _assert_rejected(lambda: verify_skill_receipt(executed_bundleless), ErrorCode.RESULT_CONTRACT_MISMATCH)
