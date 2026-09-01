from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
import sys

sys.path.insert(0, str(ROOT / "first-party-plugins" / "prompt-skill-runtime" / "src"))
sys.path.insert(0, str(ROOT / "backend"))

from plotpilot_plugin_sdk import ContractError, canonical_bytes, hash_jcs, sha256_hex
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import (
    parse_prompt_skill_execute_result_v2,
    validate_prompt_skill_execute_v2,
)
from plotpilot_plugin_sdk.rpc import ChunkUploadLedger, build_meta, build_request
from plotpilot_prompt_skill_runtime.host_adapter import (
    CAPABILITY_ID,
    MAX_ASSET_BYTES,
    HostAssetReader,
    HostAssetWriter,
    PromptSkillHostAdapter,
    _verify_bundle_asset,
    _verify_bundle_chain_refs,
    _verify_chain_asset,
    _verify_receipt_context,
    capability_descriptor,
)
from plotpilot_prompt_skill_runtime.chain import ChainAnchor, ChainExecution, build_chain_result
from plotpilot_prompt_skill_runtime.runtime import PromptSkillRuntime
from plotpilot_prompt_skill_runtime.worker import PromptSkillWorker


def _golden() -> dict[str, object]:
    return json.loads((ROOT / "contracts/golden/prompt-skill-rpc-v2/execute.json").read_text(encoding="utf-8"))


def _result_bound_to(request: dict[str, object]) -> dict[str, object]:
    """Return the frozen success golden with every nested identity rebound."""

    result = copy.deepcopy(_golden()["result"])
    params = request["params"]
    assert isinstance(params, dict)
    for field in (
        "operation_key",
        "workspace_id",
        "plugin_id",
        "plugin_release_id",
        "plugin_package_hash",
        "skill_releases",
        "run_snapshot_id",
        "run_snapshot_asset_id",
        "run_snapshot_hash",
        "generation_id",
        "job_id",
        "step_id",
        "attempt_id",
        "lease_epoch",
        "chain_id",
        "chain_asset_id",
        "chain_content_hash",
        "input_asset_id",
        "input_content_hash",
        "parameters_asset_id",
        "parameters_content_hash",
        "model_profile_revision_id",
    ):
        result[field] = copy.deepcopy(params[field])
    result["anchor"] = copy.deepcopy(params["anchor"])
    for nested_name in ("model_receipt", "result_bundle"):
        nested = result[nested_name]
        assert isinstance(nested, dict)
        for field in (
            "operation_key",
            "workspace_id",
            "plugin_id",
            "plugin_release_id",
            "plugin_package_hash",
            "generation_id",
            "job_id",
            "step_id",
            "attempt_id",
            "lease_epoch",
            "chain_id",
            "chain_asset_id",
            "chain_content_hash",
            "run_snapshot_id",
            "run_snapshot_asset_id",
            "run_snapshot_hash",
            "input_asset_id",
            "input_content_hash",
            "model_profile_revision_id",
        ):
            if field in params and field in nested:
                nested[field] = copy.deepcopy(params[field])
    return result


def _v2_request(*, generation_id: str = "generation-1", operation_key: str = "execute-op-1", request_id: str) -> dict[str, object]:
    request = copy.deepcopy(_golden()["request"])
    params = request["params"]
    meta = request["meta"]
    assert isinstance(params, dict) and isinstance(meta, dict)
    params["generation_id"] = generation_id
    params["operation_key"] = operation_key
    meta["generation_id"] = generation_id
    meta["operation_id"] = operation_key
    request["id"] = request_id
    return request


class Host:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.uploads = ChunkUploadLedger()

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        if method == "host.asset.read/v1":
            offset = int(params["offset"])
            chunk = self.content[offset : offset + int(params["length"])]
            next_offset = None if offset + len(chunk) == len(self.content) else offset + len(chunk)
            return {"base64_chunk": base64.b64encode(chunk).decode("ascii"), "next_offset": next_offset, "content_hash": sha256_hex(chunk)}
        if method == "host.asset.create/v1":
            payload = dict(params)
            payload.pop("mime")
            return self.uploads.create(**payload)  # type: ignore[arg-type]
        raise AssertionError(method)


def test_asset_reader_enforces_cumulative_pages_and_bytes() -> None:
    host = Host(b"abcdef")
    assert HostAssetReader(host, page_size=2, max_bytes=6, max_pages=3)("asset-input").content == b"abcdef"
    with pytest.raises(ContractError, match="cumulative byte"):
        HostAssetReader(Host(b"abcdef"), page_size=2, max_bytes=5, max_pages=3)("asset-input")
    with pytest.raises(ContractError, match="cumulative page"):
        HostAssetReader(Host(b"abcdef"), page_size=1, max_bytes=6, max_pages=2)("asset-input")


def test_asset_writer_uses_distinct_operation_keys_and_binds_final_ack() -> None:
    host = Host(b"")
    asset = HostAssetWriter(host, page_size=2).write(b"abc", operation_key="upload-op")
    assert asset.content == b"abc"
    keys = [params["operation_key"] for method, params in host.calls if method == "host.asset.create/v1"]
    assert len(keys) == len(set(keys)) == 2
    with pytest.raises(ContractError, match="cumulative byte"):
        HostAssetWriter(host, max_bytes=MAX_ASSET_BYTES).write(b"x" * (MAX_ASSET_BYTES + 1), operation_key="too-large")


def test_descriptor_is_the_single_v2_runtime_capability() -> None:
    descriptor = capability_descriptor(release_id="a" * 64)
    assert descriptor["capability_id"] == CAPABILITY_ID == "prompt.skill.execute/v2"
    assert descriptor["input_schema"] == "prompt-skill-execute-request/v2"
    assert descriptor["output_schema"] == "prompt-skill-execute-result/v2"
    assert descriptor["supports"] == ["run", "resume", "cancel"]


def test_adapter_requires_integrated_runtime_and_composed_core() -> None:
    assert not hasattr(PromptSkillHostAdapter, "publish")
    with pytest.raises(TypeError):
        PromptSkillHostAdapter(object(), release_id="a" * 64)  # type: ignore[arg-type]
    runtime = object.__new__(PromptSkillRuntime)
    adapter = PromptSkillHostAdapter(runtime, release_id="a" * 64)
    with pytest.raises(ContractError, match="P3/Core composition"):
        adapter.execute(_golden()["request"])  # type: ignore[arg-type]


def test_worker_v2_delegates_only_after_authority_and_replays_exactly() -> None:
    golden = _golden()
    request = golden["request"]
    result = copy.deepcopy(golden["result"])
    for field in (
        "operation_key", "workspace_id", "plugin_id", "plugin_release_id", "plugin_package_hash",
        "skill_releases", "run_snapshot_id", "run_snapshot_asset_id", "run_snapshot_hash",
        "generation_id", "job_id", "step_id", "attempt_id", "lease_epoch", "chain_id",
        "chain_asset_id", "chain_content_hash", "input_asset_id", "input_content_hash",
        "parameters_asset_id", "parameters_content_hash", "model_profile_revision_id", "anchor",
    ):
        result[field] = copy.deepcopy(request["params"][field])  # type: ignore[index]
    calls: list[object] = []

    def authority(value: dict[str, object]) -> dict[str, object]:
        params = value["params"]  # type: ignore[index]
        return {field: params[field] for field in ("workspace_id", "generation_id", "plugin_id", "plugin_release_id", "job_id", "step_id", "attempt_id", "lease_epoch", "operation_key")}

    worker = PromptSkillWorker(
        release_id="a" * 64,
        authoritative_context=authority,
        execute_handler=lambda value, _context: calls.append(value) or result,  # type: ignore[arg-type]
    )
    handshake = build_request(
        "runtime.handshake",
        {"host_protocol": "1", "generation_id": "generation-1", "plugin_release_id": "a" * 64, "data_generation_id": None},
        build_meta("control", generation_id="generation-1", plugin_release_id="a" * 64, deadline_at="2026-08-31T00:00:00Z"),
        request_id="123e4567-e89b-12d3-a456-426614174000",
    )
    assert worker.handle(handshake) is not None
    first = worker.handle(request)  # type: ignore[arg-type]
    replay = worker.handle(request)  # type: ignore[arg-type]
    assert first == replay
    assert len(calls) == 1
    assert validate_prompt_skill_execute_v2(request, first["result"], authoritative_context=authority(request)) == result  # type: ignore[index,arg-type]


def test_worker_rejects_v2_without_authority_or_composition_and_does_not_poison_epoch() -> None:
    golden = _golden()
    request = copy.deepcopy(golden["request"])
    worker = PromptSkillWorker(release_id="a" * 64)
    handshake = build_request(
        "runtime.handshake",
        {"host_protocol": "1", "generation_id": "generation-1", "plugin_release_id": "a" * 64, "data_generation_id": None},
        build_meta("control", generation_id="generation-1", plugin_release_id="a" * 64, deadline_at="2026-08-31T00:00:00Z"),
        request_id="123e4567-e89b-12d3-a456-426614174001",
    )
    worker.handle(handshake)
    rejected = worker.handle(request)
    assert rejected is not None and rejected["error"]["code"] == 1002  # type: ignore[index]
    assert parse_prompt_skill_execute_result_v2(golden["failed"]) ["status"] == "failed"


def _handshake(worker: PromptSkillWorker, *, generation_id: str = "generation-1", release_id: str = "a" * 64) -> None:
    request = build_request(
        "runtime.handshake",
        {"host_protocol": "1", "generation_id": generation_id, "plugin_release_id": release_id, "data_generation_id": None},
        build_meta("control", generation_id=generation_id, plugin_release_id=release_id, deadline_at="2026-08-31T00:00:00Z"),
        request_id="123e4567-e89b-12d3-a456-426614174010",
    )
    response = worker.handle(request)
    assert response is not None and "result" in response


def _authority_for(value: dict[str, object]) -> dict[str, object]:
    params = value["params"]
    assert isinstance(params, dict)
    return {
        field: params[field]
        for field in (
            "workspace_id",
            "generation_id",
            "plugin_id",
            "plugin_release_id",
            "job_id",
            "step_id",
            "attempt_id",
            "lease_epoch",
            "operation_key",
        )
    }


def test_worker_uses_the_p0_method_discriminated_job_start_and_resume_routes() -> None:
    calls: list[str] = []
    result = {"accepted": True, "worker_run_id": "worker-run-1", "provenance_receipt_id": "provenance-1", "output_streams": []}
    worker = PromptSkillWorker(
        release_id="a" * 64,
        start_handler=lambda _params, _meta: calls.append("start") or result,
        resume_handler=lambda _params, _meta: calls.append("resume") or result,
    )
    _handshake(worker)
    start = build_request(
        "job.start",
        {"capability_id": "prompt.skill.execute/v2", "run_snapshot_asset_id": "snapshot-asset-1", "checkpoint_asset_id": None, "secrets": None},
        build_meta("attempt", generation_id="generation-1", plugin_release_id="a" * 64, deadline_at="2026-08-31T00:00:00Z", job_id="job-1", step_id="step-1", attempt_id="attempt-1", lease_epoch=1),
        request_id="123e4567-e89b-12d3-a456-426614174011",
    )
    resume = build_request(
        "job.resume",
        {"capability_id": "prompt.skill.execute/v2", "run_snapshot_asset_id": "snapshot-asset-1", "resume_of_attempt_id": "attempt-1", "checkpoint_asset_id": None, "resume_intent_id": "resume-1", "resume_reason": "retry", "secrets": None},
        build_meta("attempt", generation_id="generation-1", plugin_release_id="a" * 64, deadline_at="2026-08-31T00:00:00Z", job_id="job-1", step_id="step-1", attempt_id="attempt-2", lease_epoch=2),
        request_id="123e4567-e89b-12d3-a456-426614174012",
    )
    assert worker.handle(start)["result"] == result  # type: ignore[index]
    assert worker.handle(resume)["result"] == result  # type: ignore[index]
    assert calls == ["start", "resume"]


def test_worker_rejects_a_result_from_a_different_p0_method() -> None:
    worker = PromptSkillWorker(
        release_id="a" * 64,
        start_handler=lambda _params, _meta: {"accepted": True, "checkpoint_asset_id": None},
    )
    _handshake(worker)
    request = build_request(
        "job.start",
        {"capability_id": "prompt.skill.execute/v2", "run_snapshot_asset_id": "snapshot-asset-1", "checkpoint_asset_id": None, "secrets": None},
        build_meta("attempt", generation_id="generation-1", plugin_release_id="a" * 64, deadline_at="2026-08-31T00:00:00Z", job_id="job-1", step_id="step-1", attempt_id="attempt-1", lease_epoch=1),
        request_id="123e4567-e89b-12d3-a456-426614174013",
    )
    response = worker.handle(request)
    assert response is not None and response["error"]["code"] == 1011  # type: ignore[index]


def test_chain_asset_requires_the_closed_chain_and_complete_ordered_receipts() -> None:
    chain = _golden_chain()
    receipt = _golden_receipt()
    execution = ChainExecution(chain, (receipt,))
    params, content = _chain_params(chain)
    _verify_chain_asset(HostAssetReader(Host(content)), params, execution)
    for partial in (
        {"schema": "skill-chain-result/v1", "chain_id": chain["chain_id"], "run_snapshot_hash": chain["run_snapshot_hash"]},
        {**chain, "receipt_ids": [], "receipt_hashes": [], "chain_hash": "0" * 64},
    ):
        raw = canonical_bytes(partial)
        bad_params = {**params, "chain_content_hash": hashlib.sha256(raw).hexdigest()}
        with pytest.raises(ContractError):
            _verify_chain_asset(HostAssetReader(Host(raw)), bad_params, execution)


def test_chain_asset_rejects_empty_partial_foreign_duplicate_reordered_and_fabricated_receipts() -> None:
    execution = _two_receipt_execution()
    chain = dict(execution.chain)
    params, content = _chain_params(chain, asset_id="asset-chain-provenance")
    _verify_chain_asset(HostAssetReader(Host(content)), params, execution)
    first, second = execution.receipts
    foreign = _rehashed_receipt(dict(first), chain_id="foreign-chain")
    for candidate in (
        ChainExecution(chain, ()),
        ChainExecution(chain, (first,)),
        ChainExecution(chain, (foreign, second)),
        ChainExecution(chain, (first, first)),
        ChainExecution(chain, (second, first)),
    ):
        with pytest.raises(ContractError):
            _verify_chain_asset(HostAssetReader(Host(content)), params, candidate)

    fabricated = copy.deepcopy(chain)
    fabricated["receipt_ids"] = ["fabricated:receipt-0", "fabricated:receipt-1"]
    fabricated["receipt_hashes"] = ["1" * 64, "2" * 64]
    fabricated["chain_hash"] = sha256_hex(
        b"skill-chain/v1\n" + b"1" * 64 + b"\n" + b"2" * 64 + b"\n" + ("c" * 64).encode("ascii") + b"\n"
    )
    fabricated_raw = canonical_bytes(fabricated)
    fabricated_params = {**params, "chain_content_hash": hashlib.sha256(fabricated_raw).hexdigest()}
    with pytest.raises(ContractError):
        _verify_chain_asset(HostAssetReader(Host(fabricated_raw)), fabricated_params, execution)


def test_chain_foreign_input_accepted_bypass_is_closed() -> None:
    """Regression for CHAIN_FOREIGN_INPUT_ACCEPTED."""

    execute_input_hash = _two_receipt_execution().chain["input_hash"]
    foreign_execution = _two_receipt_execution(input_hash="f" * 64)
    params, content = _chain_params(dict(foreign_execution.chain), asset_id="asset-chain-foreign-input")
    params["input_content_hash"] = execute_input_hash
    with pytest.raises(ContractError, match="execute input_content_hash"):
        _verify_chain_asset(HostAssetReader(Host(content)), params, foreign_execution)


def test_bundle_chain_refs_reject_empty_partial_foreign_duplicate_and_reordered_projections() -> None:
    execution = _two_receipt_execution()
    chain = dict(execution.chain)
    params, _content = _chain_params(chain, asset_id="asset-chain-provenance")
    result = {
        "chain_asset_id": params["chain_asset_id"],
        "chain_content_hash": params["chain_content_hash"],
        "result_bundle": {
            "bundle_id": chain["result_bundle_id"],
            "result_item_id": chain["result_item_id"],
        },
    }
    expected = {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": chain["chain_id"],
        "asset_id": params["chain_asset_id"],
        "asset_hash": params["chain_content_hash"],
        "result_bundle_id": chain["result_bundle_id"],
        "result_item_id": chain["result_item_id"],
        "stream_id": chain["stream_id"],
        "acked_prefix_hash": chain["acked_prefix_hash"],
    }
    bundle = {"skill_chain_result_refs": [expected]}
    _verify_bundle_chain_refs(bundle, result, execution)
    partial = {key: value for key, value in expected.items() if key != "asset_hash"}
    foreign = {**expected, "chain_result_id": "foreign-chain"}
    for refs in ([], [partial], [foreign], [expected, copy.deepcopy(expected)], [foreign, expected]):
        with pytest.raises(ContractError):
            _verify_bundle_chain_refs({"skill_chain_result_refs": refs}, result, execution)


def _receipt_context(
    result: dict[str, object], *, index: int, release: dict[str, object]
) -> dict[str, object]:
    return {
        "job_id": result["job_id"],
        "step_id": result["step_id"],
        "attempt_id": result["attempt_id"],
        "lease_epoch": result["lease_epoch"],
        "run_snapshot_hash": result["run_snapshot_hash"],
        "generation_id": result["generation_id"],
        "chain_id": result["chain_id"],
        "chain_index": index,
        "skill_id": release["skill_id"],
        "release_id": release["release_id"],
        "package_hash": release["package_hash"],
        "parameters_asset_id": release["parameters_asset_id"],
        "parameters_hash": release["parameters_content_hash"],
        "input_asset_id": result["input_asset_id"],
        "input_hash": result["input_content_hash"],
    }


def test_model_receipt_chain_index_is_bounded_and_positionally_bound() -> None:
    result = _golden()["result"]
    assert isinstance(result, dict)
    releases = result["skill_releases"]
    assert isinstance(releases, list) and len(releases) >= 2

    _verify_receipt_context({"input_context": _receipt_context(result, index=1, release=releases[1])}, result)
    with pytest.raises(ContractError, match="outside the frozen chain"):
        _verify_receipt_context(
            {"input_context": _receipt_context(result, index=len(releases), release=releases[1])}, result
        )
    misaligned = _receipt_context(result, index=1, release=releases[1])
    misaligned["skill_id"] = releases[0]["skill_id"]
    with pytest.raises(ContractError, match="release/index"):
        _verify_receipt_context({"input_context": misaligned}, result)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        pytest.param("attempt_id", None, id="MODEL_RECEIPT_MISSING_ATTEMPT"),
        pytest.param("attempt_id", "foreign-attempt", id="MODEL_RECEIPT_FOREIGN_ATTEMPT"),
        pytest.param("lease_epoch", None, id="MODEL_RECEIPT_MISSING_LEASE"),
        pytest.param("lease_epoch", 0, id="MODEL_RECEIPT_STALE_LEASE"),
        pytest.param("lease_epoch", 2, id="MODEL_RECEIPT_FUTURE_LEASE"),
    ),
)
def test_model_receipt_attempt_and_lease_must_match_execute_identity(
    field: str, replacement: object | None
) -> None:
    result = _golden()["result"]
    assert isinstance(result, dict)
    releases = result["skill_releases"]
    assert isinstance(releases, list) and isinstance(releases[0], dict)
    context = _receipt_context(result, index=0, release=releases[0])
    if replacement is None:
        context.pop(field)
    else:
        context[field] = replacement
    with pytest.raises(ContractError):
        _verify_receipt_context({"input_context": context}, result)


def test_model_receipt_foreign_lease_accepted_bypass_is_closed() -> None:
    """Regression for MODEL_RECEIPT_FOREIGN_LEASE_ACCEPTED: omitted attempt plus lease 99."""

    result = _golden()["result"]
    assert isinstance(result, dict)
    releases = result["skill_releases"]
    assert isinstance(releases, list) and isinstance(releases[0], dict)
    context = _receipt_context(result, index=0, release=releases[0])
    context.pop("attempt_id")
    context["lease_epoch"] = 99
    with pytest.raises(ContractError):
        _verify_receipt_context({"input_context": context}, result)


def _golden_chain() -> dict[str, object]:
    return json.loads((ROOT / "contracts/examples/fixtures/skill-chain-result.json").read_text(encoding="utf-8"))


def _golden_receipt() -> dict[str, object]:
    return json.loads((ROOT / "contracts/examples/fixtures/skill-run-receipt.json").read_text(encoding="utf-8"))


def _chain_params(chain: dict[str, object], *, asset_id: str = "asset-chain-1") -> tuple[dict[str, object], bytes]:
    content = canonical_bytes(chain)
    return {
        "chain_asset_id": asset_id,
        "chain_content_hash": hashlib.sha256(content).hexdigest(),
        "chain_id": chain["chain_id"],
        "run_snapshot_hash": chain["run_snapshot_hash"],
        "input_content_hash": chain["input_hash"],
    }, content


def _rehashed_receipt(receipt: dict[str, object], **changes: object) -> dict[str, object]:
    value = copy.deepcopy(receipt)
    value.update(changes)
    value["receipt_hash"] = hash_jcs(
        "skill-run-receipt/v1",
        {key: item for key, item in value.items() if key != "receipt_hash"},
    )
    return value


def _two_receipt_execution(*, input_hash: str | None = None) -> ChainExecution:
    first_fixture = _golden_receipt()
    first = _rehashed_receipt(
        first_fixture,
        chain_id="skill-chain-provenance",
        receipt_id="skill-chain-provenance:receipt-0",
        chain_index=0,
        previous_receipt_hash=None,
        input_hash=first_fixture["input_hash"] if input_hash is None else input_hash,
    )
    second = _rehashed_receipt(
        _golden_receipt(),
        chain_id="skill-chain-provenance",
        receipt_id="skill-chain-provenance:receipt-1",
        chain_index=1,
        previous_receipt_hash=first["receipt_hash"],
        input_asset_id=first["output_asset_id"],
        input_hash=first["output_hash"],
        output_asset_id="asset-output-provenance-2",
        output_hash="c" * 64,
    )
    chain = build_chain_result(
        chain_id="skill-chain-provenance",
        run_snapshot_hash=first["run_snapshot_hash"],
        input_hash=first["input_hash"],
        receipts=(first, second),
        anchor=ChainAnchor.bundle("bundle-golden", "candidate-item-1"),
        final_output_asset_id=second["output_asset_id"],
        final_output_hash=second["output_hash"],
    )
    return ChainExecution(chain, (first, second))


_EXPECTED_BUNDLE_PRODUCER = {
    "plugin_id": "com.plotpilot.prompt-skill",
    "release_id": "a" * 64,
    "capability_id": CAPABILITY_ID,
    "job_id": "job-1",
    "step_id": "step-1",
    "attempt_id": "attempt-1",
    "lease_epoch": 1,
}


def _bundle_verification_case(
    producer: dict[str, object] | None = None,
) -> tuple[HostAssetReader, dict[str, object], dict[str, object], ChainExecution]:
    execution = _two_receipt_execution()
    chain = execution.chain
    chain_content_hash = sha256_hex(canonical_bytes(dict(chain)))
    result: dict[str, object] = {
        "capability_id": CAPABILITY_ID,
        "plugin_id": _EXPECTED_BUNDLE_PRODUCER["plugin_id"],
        "plugin_release_id": _EXPECTED_BUNDLE_PRODUCER["release_id"],
        "job_id": _EXPECTED_BUNDLE_PRODUCER["job_id"],
        "step_id": _EXPECTED_BUNDLE_PRODUCER["step_id"],
        "attempt_id": _EXPECTED_BUNDLE_PRODUCER["attempt_id"],
        "lease_epoch": _EXPECTED_BUNDLE_PRODUCER["lease_epoch"],
        "chain_asset_id": "asset-chain-provenance",
        "chain_content_hash": chain_content_hash,
        "result_bundle": {
            "bundle_id": chain["result_bundle_id"],
            "result_item_id": chain["result_item_id"],
            "asset_id": "asset-result-bundle",
            "content_hash": "0" * 64,
            "run_snapshot_hash": chain["run_snapshot_hash"],
        },
    }
    bundle = {
        "schema": "result-bundle/v1",
        "contract_id": "artifact-bundle/v1",
        "bundle_id": chain["result_bundle_id"],
        "bundle_type": "artifact",
        "producer": copy.deepcopy(_EXPECTED_BUNDLE_PRODUCER if producer is None else producer),
        "input_snapshot_hash": chain["run_snapshot_hash"],
        "items": [
            {
                "schema": "artifact-item/v1",
                "item_id": chain["result_item_id"],
                "artifact_kind": "prompt-output",
                "payload_asset_id": "asset-output-provenance",
                "payload_hash": "c" * 64,
                "mime": "text/plain",
                "source_refs": [],
                "status": "complete",
            }
        ],
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": "model-receipt-1",
        "skill_chain_result_refs": [
            {
                "schema": "skill-chain-ref/v1",
                "chain_result_id": chain["chain_id"],
                "asset_id": result["chain_asset_id"],
                "asset_hash": result["chain_content_hash"],
                "result_bundle_id": chain["result_bundle_id"],
                "result_item_id": chain["result_item_id"],
                "stream_id": chain["stream_id"],
                "acked_prefix_hash": chain["acked_prefix_hash"],
            }
        ],
    }
    content = canonical_bytes(bundle)
    identity = result["result_bundle"]
    assert isinstance(identity, dict)
    identity["content_hash"] = sha256_hex(content)
    snapshot = {"workspace_id": "ws-1", "snapshot_hash": chain["run_snapshot_hash"]}
    return HostAssetReader(Host(content)), result, snapshot, execution


def test_bundle_producer_exactly_binds_the_execute_identity() -> None:
    _verify_bundle_asset(*_bundle_verification_case())


def test_bundle_foreign_producer_accepted_bypass_is_closed() -> None:
    """Regression for BUNDLE_FOREIGN_PRODUCER_ACCEPTED."""

    foreign = {
        **_EXPECTED_BUNDLE_PRODUCER,
        "job_id": "foreign-job",
        "attempt_id": "foreign-attempt",
        "lease_epoch": 999,
    }
    with pytest.raises(ContractError, match="exactly bound"):
        _verify_bundle_asset(*_bundle_verification_case(foreign))


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        pytest.param("plugin_id", "com.plotpilot.foreign", id="foreign-plugin"),
        pytest.param("release_id", "b" * 64, id="foreign-release"),
        pytest.param("capability_id", "prompt.skill.foreign/v2", id="foreign-capability"),
        pytest.param("job_id", "foreign-job", id="foreign-job"),
        pytest.param("step_id", "foreign-step", id="foreign-step"),
        pytest.param("attempt_id", "foreign-attempt", id="foreign-attempt"),
        pytest.param("lease_epoch", 999, id="foreign-lease"),
    ),
)
def test_bundle_rejects_each_foreign_producer_identity(field: str, replacement: object) -> None:
    producer = {**_EXPECTED_BUNDLE_PRODUCER, field: replacement}
    with pytest.raises(ContractError, match="exactly bound"):
        _verify_bundle_asset(*_bundle_verification_case(producer))


@pytest.mark.parametrize(
    "producer",
    (
        pytest.param(
            {key: value for key, value in _EXPECTED_BUNDLE_PRODUCER.items() if key != "attempt_id"},
            id="missing-producer-identity",
        ),
        pytest.param(
            {"plugin_id": _EXPECTED_BUNDLE_PRODUCER["plugin_id"]},
            id="partial-producer-identity",
        ),
        pytest.param(
            {**_EXPECTED_BUNDLE_PRODUCER, "package_hash": "b" * 64},
            id="incompatible-package-identity",
        ),
        pytest.param(
            {**_EXPECTED_BUNDLE_PRODUCER, "generation_id": "foreign-generation"},
            id="incompatible-generation-identity",
        ),
    ),
)
def test_bundle_rejects_missing_partial_or_incompatible_producer_identity(
    producer: dict[str, object],
) -> None:
    with pytest.raises(ContractError):
        _verify_bundle_asset(*_bundle_verification_case(producer))


def test_worker_replay_survives_restart_without_future_or_replacement_poisoning(tmp_path: Path) -> None:
    golden = _golden()
    request = copy.deepcopy(golden["request"])
    result = copy.deepcopy(golden["result"])
    for field in (
        "operation_key", "workspace_id", "plugin_id", "plugin_release_id", "plugin_package_hash", "skill_releases", "run_snapshot_id", "run_snapshot_asset_id", "run_snapshot_hash", "generation_id", "job_id", "step_id", "attempt_id", "lease_epoch", "chain_id", "chain_asset_id", "chain_content_hash", "input_asset_id", "input_content_hash", "parameters_asset_id", "parameters_content_hash", "model_profile_revision_id", "anchor",
    ):
        result[field] = copy.deepcopy(request["params"][field])  # type: ignore[index]
    state = tmp_path / "worker-state.json"
    calls: list[int] = []

    def authority(value: dict[str, object]) -> dict[str, object]:
        params = value["params"]  # type: ignore[index]
        return {field: params[field] for field in ("workspace_id", "generation_id", "plugin_id", "plugin_release_id", "job_id", "step_id", "attempt_id", "lease_epoch", "operation_key")}

    first = PromptSkillWorker(release_id="a" * 64, state_path=state, authoritative_context=authority, execute_handler=lambda _value, _context: calls.append(1) or result)  # type: ignore[arg-type]
    _handshake(first)
    assert first.handle(request)["result"] == result  # type: ignore[index]
    future = copy.deepcopy(request)
    future["id"] = "123e4567-e89b-12d3-a456-426614174014"
    future["params"]["lease_epoch"] = 99  # type: ignore[index]
    future["meta"]["lease_epoch"] = 99  # type: ignore[index]
    first.execute_handler = lambda _value, _context: (_ for _ in ()).throw(RuntimeError("future rejected"))
    assert first.handle(future)["error"]["code"] == 1011  # type: ignore[index]
    second = PromptSkillWorker(release_id="a" * 64, state_path=state, authoritative_context=authority, execute_handler=lambda _value, _context: calls.append(2) or result)  # type: ignore[arg-type]
    _handshake(second)
    assert second.handle(request)["result"] == result  # type: ignore[index]
    replacement = copy.deepcopy(request)
    replacement["id"] = "123e4567-e89b-12d3-a456-426614174015"
    replacement["params"]["generation_id"] = "generation-replacement"  # type: ignore[index]
    replacement["meta"]["generation_id"] = "generation-replacement"  # type: ignore[index]
    replacement["params"]["lease_epoch"] = 1  # type: ignore[index]
    replacement["meta"]["lease_epoch"] = 1  # type: ignore[index]
    replacement_result = copy.deepcopy(result)
    for field in (
        "operation_key", "workspace_id", "plugin_id", "plugin_release_id", "plugin_package_hash", "skill_releases", "run_snapshot_id", "run_snapshot_asset_id", "run_snapshot_hash", "generation_id", "job_id", "step_id", "attempt_id", "lease_epoch", "chain_id", "chain_asset_id", "chain_content_hash", "input_asset_id", "input_content_hash", "parameters_asset_id", "parameters_content_hash", "model_profile_revision_id", "anchor",
    ):
        replacement_result[field] = copy.deepcopy(replacement["params"][field])  # type: ignore[index]
    for nested_name in ("model_receipt", "result_bundle"):
        nested = replacement_result.get(nested_name)
        if isinstance(nested, dict):
            for field in (
                "operation_key", "workspace_id", "plugin_id", "plugin_release_id", "plugin_package_hash", "generation_id", "job_id", "step_id", "attempt_id", "lease_epoch", "chain_id", "chain_asset_id", "chain_content_hash", "run_snapshot_id", "run_snapshot_asset_id", "run_snapshot_hash", "input_asset_id", "input_content_hash",
            ):
                if field in nested and field in replacement["params"]:  # type: ignore[operator]
                    nested[field] = copy.deepcopy(replacement["params"][field])  # type: ignore[index]
    third = PromptSkillWorker(release_id="a" * 64, state_path=tmp_path / "replacement-state.json", authoritative_context=authority, execute_handler=lambda _value, _context: replacement_result)  # type: ignore[arg-type]
    _handshake(third, generation_id="generation-replacement")
    assert third.handle(replacement)["result"] == replacement_result  # type: ignore[index]
    assert calls == [1]


def test_worker_operation_key_is_scoped_by_generation_after_restart(tmp_path: Path) -> None:
    state = tmp_path / "worker-state.json"
    calls: list[str] = []
    request_one = _v2_request(
        generation_id="generation-one",
        operation_key="shared-operation",
        request_id="123e4567-e89b-12d3-a456-426614174020",
    )
    result_one = _result_bound_to(request_one)
    first = PromptSkillWorker(
        release_id="a" * 64,
        state_path=state,
        authoritative_context=_authority_for,
        execute_handler=lambda _value, _context: calls.append("one") or result_one,
    )
    _handshake(first, generation_id="generation-one")
    assert first.handle(request_one)["result"] == result_one  # type: ignore[index]

    request_two = _v2_request(
        generation_id="generation-two",
        operation_key="shared-operation",
        request_id="123e4567-e89b-12d3-a456-426614174021",
    )
    result_two = _result_bound_to(request_two)
    second = PromptSkillWorker(
        release_id="a" * 64,
        state_path=state,
        authoritative_context=_authority_for,
        execute_handler=lambda _value, _context: calls.append("two") or result_two,
    )
    _handshake(second, generation_id="generation-two")
    assert second.handle(request_two)["result"] == result_two  # type: ignore[index]
    assert calls == ["one", "two"]
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert [
        (entry["generation_id"], entry["operation_key"])
        for entry in persisted["replay_entries"]
    ] == [("generation-one", "shared-operation"), ("generation-two", "shared-operation")]


def test_worker_rejects_corrupt_duplicate_and_different_release_state(tmp_path: Path) -> None:
    request = _v2_request(request_id="123e4567-e89b-12d3-a456-426614174022")
    result = _result_bound_to(request)
    state = tmp_path / "valid-state.json"
    worker = PromptSkillWorker(
        release_id="a" * 64,
        state_path=state,
        authoritative_context=_authority_for,
        execute_handler=lambda _value, _context: result,
    )
    _handshake(worker)
    assert worker.handle(request)["result"] == result  # type: ignore[index]
    document = json.loads(state.read_text(encoding="utf-8"))

    duplicate = tmp_path / "duplicate-state.json"
    duplicate_document = copy.deepcopy(document)
    duplicate_document["replay_entries"].append(copy.deepcopy(document["replay_entries"][0]))
    duplicate.write_text(json.dumps(duplicate_document), encoding="utf-8")
    with pytest.raises(ContractError, match="duplicates an earlier operation"):
        PromptSkillWorker(release_id="a" * 64, state_path=duplicate)

    with pytest.raises(ContractError, match="different plugin release"):
        PromptSkillWorker(release_id="b" * 64, state_path=state)

    corrupt = tmp_path / "corrupt-state.json"
    corrupt.write_bytes(b"\xff\xfe not json")
    with pytest.raises(ContractError, match="strict UTF-8 JSON"):
        PromptSkillWorker(release_id="a" * 64, state_path=corrupt)


def test_worker_error_result_does_not_poison_the_persisted_replay_state(tmp_path: Path) -> None:
    state = tmp_path / "worker-state.json"
    request = _v2_request(request_id="123e4567-e89b-12d3-a456-426614174023")
    result = _result_bound_to(request)
    calls: list[str] = []

    def rejected(_value: dict[str, object], _context: dict[str, object]) -> dict[str, object]:
        calls.append("rejected")
        raise RuntimeError("composition failed before Core acceptance")

    worker = PromptSkillWorker(
        release_id="a" * 64,
        state_path=state,
        authoritative_context=_authority_for,
        execute_handler=rejected,
    )
    _handshake(worker)
    failed = worker.handle(request)
    assert failed is not None and failed["error"]["code"] == 1011  # type: ignore[index]
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["replay_entries"] == []
    assert persisted["lease_highwater"] == []

    worker.execute_handler = lambda _value, _context: calls.append("accepted") or result
    accepted = worker.handle(request)
    assert accepted is not None and accepted["result"] == result  # type: ignore[index]
    assert calls == ["rejected", "accepted"]
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert len(persisted["replay_entries"]) == 1


def test_worker_migration_plan_apply_verify_is_fenced_recoverable_and_drift_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = ROOT / "first-party-plugins" / "prompt-skill-runtime"
    manifest_path = plugin_root / "migrations" / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    release_id = "a" * 64
    generation_id = "migration-generation"
    state = tmp_path / "migration-state.json"
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    lease_calls: list[dict[str, object]] = []
    apply_calls: list[dict[str, object]] = []
    verify_calls: list[dict[str, object]] = []

    def lease_validator(params: dict[str, object]) -> bool:
        lease_calls.append(dict(params))
        if "from_schema" in params:
            return True
        return (
            params.get("db_lease_id") == "lease-1"
            and params.get("db_lease_epoch") == 1
            and params.get("owner_instance_id") == "owner-1"
        )

    def apply_handler(params: dict[str, object], _lease: dict[str, object]) -> dict[str, object]:
        apply_calls.append(dict(params))
        return {"applied_schema": manifest["to_schema"], "receipt_hash": "b" * 64}

    def verify_handler(params: dict[str, object], _lease: dict[str, object]) -> dict[str, object]:
        verify_calls.append(dict(params))
        return {"valid": True, "schema_hash": manifest["to_schema"], "errors": []}

    plan_request = build_request(
        "migration.plan",
        {
            "from_schema": manifest["from_schema"],
            "to_schema": manifest["to_schema"],
            "migration_manifest_hash": manifest_hash,
        },
        build_meta(
            "install",
            generation_id=generation_id,
            plugin_release_id=release_id,
            deadline_at="2026-09-01T00:00:00Z",
            operation_id="migration-plan",
            install_operation_id="install-1",
            install_lease_epoch=1,
        ),
        request_id="123e4567-e89b-12d3-a456-426614174024",
    )
    first = PromptSkillWorker(
        release_id=release_id,
        state_path=state,
        migration_lease_validator=lease_validator,
    )
    _handshake(first, generation_id=generation_id, release_id=release_id)
    planned = first.handle(plan_request)
    assert planned is not None and "result" in planned
    plan_id = planned["result"]["plan_id"]  # type: ignore[index]
    assert planned["result"]["steps"] == manifest["steps"]  # type: ignore[index]
    assert planned["result"]["backward_compatible"] is False  # type: ignore[index]
    assert planned["result"]["requires_verified_backup"] is True  # type: ignore[index]
    assert lease_calls and lease_calls[0]["from_schema"] == manifest["from_schema"]
    persisted_plan = json.loads(state.read_text(encoding="utf-8"))["migration_plans"][0]
    assert (
        persisted_plan["install_operation_id"],
        persisted_plan["install_lease_epoch"],
    ) == ("install-1", 1)

    same_transition_other_install = copy.deepcopy(plan_request)
    same_transition_other_install["id"] = "123e4567-e89b-12d3-a456-426614174029"
    same_transition_other_install["meta"]["install_operation_id"] = "install-2"  # type: ignore[index]
    # Pair membership is part of the deterministic plan identity.  Do not
    # publish this second plan: the later cross-install verify probe must be a
    # replay of install-1's only durable plan, not a valid install-2 plan.
    assert PromptSkillWorker._plan_id(same_transition_other_install) != plan_id

    unsupported = copy.deepcopy(plan_request)
    unsupported["id"] = "123e4567-e89b-12d3-a456-426614174025"
    unsupported["params"]["from_schema"] = "1" * 64  # type: ignore[index]
    unsupported["params"]["to_schema"] = "2" * 64  # type: ignore[index]
    rejected = first.handle(unsupported)
    assert rejected is not None and rejected["error"]["code"] == 1007  # type: ignore[index]

    apply_request = build_request(
        "migration.apply",
        {
            "plan_id": plan_id,
            "db_lease_id": "lease-1",
            "db_lease_epoch": 1,
            "owner_instance_id": "owner-1",
        },
        build_meta(
            "install",
            generation_id=generation_id,
            plugin_release_id=release_id,
            deadline_at="2026-09-01T00:00:00Z",
            operation_id="migration-apply",
            install_operation_id="install-1",
            install_lease_epoch=1,
        ),
        request_id="123e4567-e89b-12d3-a456-426614174026",
    )
    drifted_manifest = manifest_bytes + b"\n"
    monkeypatch.setattr(
        "plotpilot_prompt_skill_runtime.worker._read_manifest_source",
        lambda: (drifted_manifest, "source", plugin_root),
    )
    drifted = first.handle(apply_request)
    assert drifted is not None and drifted["error"]["code"] == 1007  # type: ignore[index]
    assert apply_calls == []
    monkeypatch.undo()

    stale_apply = copy.deepcopy(apply_request)
    stale_apply["id"] = "123e4567-e89b-12d3-a456-426614174027"
    stale_apply["params"]["db_lease_epoch"] = 2  # type: ignore[index]
    stale_apply["meta"]["install_lease_epoch"] = 2  # type: ignore[index]
    second = PromptSkillWorker(
        release_id=release_id,
        state_path=state,
        migration_lease_validator=lease_validator,
        migration_apply_handler=apply_handler,
        migration_verify_handler=verify_handler,
    )
    _handshake(second, generation_id=generation_id, release_id=release_id)
    stale = second.handle(stale_apply)
    assert stale is not None and stale["error"]["code"] == 1002  # type: ignore[index]
    assert apply_calls == []

    cross_install_apply = copy.deepcopy(apply_request)
    cross_install_apply["id"] = "123e4567-e89b-12d3-a456-426614174030"
    cross_install_apply["meta"]["install_operation_id"] = "install-2"  # type: ignore[index]
    rejected_cross_install = second.handle(cross_install_apply)
    assert rejected_cross_install is not None and rejected_cross_install["error"]["code"] == 1002  # type: ignore[index]
    assert apply_calls == []

    missing_install_meta = copy.deepcopy(apply_request)
    missing_install_meta["id"] = "123e4567-e89b-12d3-a456-426614174031"
    del missing_install_meta["meta"]["install_operation_id"]  # type: ignore[index]
    rejected_missing_meta = second.handle(missing_install_meta)
    assert rejected_missing_meta is not None and "error" in rejected_missing_meta
    assert apply_calls == []

    applied = second.handle(apply_request)
    assert applied is not None and applied["result"]["applied_schema"] == manifest["to_schema"]  # type: ignore[index]

    verify_request = build_request(
        "migration.verify",
        {
            "db_lease_id": "lease-1",
            "db_lease_epoch": 1,
            "owner_instance_id": "owner-1",
            "expected_schema": manifest["to_schema"],
        },
        build_meta(
            "install",
            generation_id=generation_id,
            plugin_release_id=release_id,
            deadline_at="2026-09-01T00:00:00Z",
            operation_id="migration-verify",
            install_operation_id="install-1",
            install_lease_epoch=1,
        ),
        request_id="123e4567-e89b-12d3-a456-426614174028",
    )
    cross_epoch_verify = copy.deepcopy(verify_request)
    cross_epoch_verify["id"] = "123e4567-e89b-12d3-a456-426614174032"
    cross_epoch_verify["meta"]["install_lease_epoch"] = 2  # type: ignore[index]
    rejected_cross_epoch = second.handle(cross_epoch_verify)
    assert rejected_cross_epoch is not None and rejected_cross_epoch["error"]["code"] == 1007  # type: ignore[index]
    assert verify_calls == []

    cross_install_verify = copy.deepcopy(verify_request)
    cross_install_verify["id"] = "123e4567-e89b-12d3-a456-426614174033"
    cross_install_verify["meta"]["install_operation_id"] = "install-2"  # type: ignore[index]
    rejected_verify_install = second.handle(cross_install_verify)
    assert rejected_verify_install is not None and rejected_verify_install["error"]["code"] == 1007  # type: ignore[index]
    assert verify_calls == []

    verified = second.handle(verify_request)
    assert verified is not None and verified["result"]["schema_hash"] == manifest["to_schema"]  # type: ignore[index]
    assert len(apply_calls) == len(verify_calls) == 1
