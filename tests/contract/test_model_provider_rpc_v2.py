from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PROVIDER_SRC = (
    ROOT
    / "first-party-plugins"
    / "provider-openai-compatible"
    / "backend"
    / "src"
)
PROMPT_SKILL_SRC = (
    ROOT / "first-party-plugins" / "prompt-skill-runtime" / "src"
)
for source in (str(BACKEND), str(PROVIDER_SRC), str(PROMPT_SKILL_SRC)):
    if source not in sys.path:
        sys.path.insert(0, source)

from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs, sha256_hex  # noqa: E402
from plotpilot_plugin_sdk.errors import ContractError  # noqa: E402
from plotpilot_plugin_sdk.model_provider_rpc_v2 import (  # noqa: E402
    HASH_DOMAINS,
    METHOD,
    MODEL_RECEIPT_ANCHOR_FIELDS,
    PLANNER_CONTEXT_FIELDS,
    TERMINAL_STATES,
    ModelProviderOperationLedgerV2,
    build_model_provider_invoke_request_v2,
    get_model_provider_rpc_method_matrix_v2,
    model_provider_operation_digest_v2,
    parse_model_provider_invoke_request_v2,
    parse_model_provider_invoke_result_v2,
    parse_model_provider_rpc_envelope_v2,
    parse_model_provider_rpc_error_v2,
    validate_model_provider_invoke_result_v2,
    validate_model_provider_rpc_response_v2,
    validate_model_provider_rpc_success_v2,
)
from plotpilot_plugin_sdk.verifier import (  # noqa: E402
    RESERVED_PROVIDER_RPC_METHODS,
    verify_capability_descriptor,
    verify_manifest,
    verify_plan,
)
from plotpilot_provider_openai_compatible import ModelReceipt, ProviderInvocation  # noqa: E402
from plotpilot_prompt_skill_runtime import (  # noqa: E402
    AssetRef,
    FrozenModelInvocation,
    decode_model_receipt,
    verify_model_receipt_asset,
)
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import (  # noqa: E402
    parse_prompt_skill_execute_result_v2,
)


SCHEMA_ROOT = ROOT / "contracts" / "json-schema"
RESOURCE_ROOT = ROOT / "backend" / "plotpilot_plugin_sdk" / "resources"
GOLDEN_ROOT = ROOT / "contracts" / "golden" / "model-provider-rpc-v2"
CORPUS_ROOT = ROOT / "contracts" / "corpus" / "model-provider-rpc-v2"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return read_json(GOLDEN_ROOT / "invoke.json")


@pytest.fixture(scope="module")
def corpus() -> tuple[dict[str, Any], dict[str, Any]]:
    return read_json(CORPUS_ROOT / "manifest.json"), read_json(
        CORPUS_ROOT / "01-invoke.json"
    )


def _assert_closed(node: Any, path: str = "$") -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
        for key, child in node.items():
            _assert_closed(child, f"{path}/{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            _assert_closed(child, f"{path}/{index}")


def _resolve_local_refs(value: Any, root: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"} and value["$ref"].startswith("#/$defs/"):
            return _resolve_local_refs(root["$defs"][value["$ref"].split("/")[-1]], root)
        return {key: _resolve_local_refs(child, root) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_local_refs(child, root) for child in value]
    return value


def test_p0b_schemas_matrix_and_p2_anchor_are_closed_and_exact() -> None:
    matrix = read_json(SCHEMA_ROOT / "model-provider-rpc-method-matrix.v2.json")
    assert matrix == get_model_provider_rpc_method_matrix_v2()
    assert matrix["schema"] == "model-provider-rpc-method-matrix/v2"
    assert matrix["authority"] == "core"
    assert matrix["direction"] == "host-to-provider"
    assert matrix["plugin_authority_allowed"] is False
    assert matrix["reserved_method_ids"] == [METHOD]
    assert [entry["method"] for entry in matrix["methods"]] == [METHOD]
    assert matrix["methods"][0]["terminal_states"] == list(TERMINAL_STATES)
    assert matrix["methods"][0]["terminal_states_use_jsonrpc_success"] is True

    schemas = {
        name: read_json(SCHEMA_ROOT / name)
        for name in (
            "model-provider-invoke-request-v2.schema.json",
            "model-provider-invoke-result-v2.schema.json",
            "model-provider-invoke-success-v2.schema.json",
        )
    }
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        _assert_closed(schema, name)

    result_anchor = schemas["model-provider-invoke-result-v2.schema.json"][
        "properties"
    ]["model_receipt_anchor"]
    p2 = read_json(SCHEMA_ROOT / "prompt-skill-execute-result-v2.schema.json")
    p2_anchor = _resolve_local_refs(p2["$defs"]["model-receipt"], p2)
    assert result_anchor == p2_anchor
    assert tuple(result_anchor["required"]) == MODEL_RECEIPT_ANCHOR_FIELDS
    request_context = schemas["model-provider-invoke-request-v2.schema.json"][
        "properties"
    ]["params"]["properties"]["planner_context"]
    assert tuple(request_context["required"]) == PLANNER_CONTEXT_FIELDS
    assert len(PLANNER_CONTEXT_FIELDS) == 19
    assert len(MODEL_RECEIPT_ANCHOR_FIELDS) == 23

    assert not list(SCHEMA_ROOT.glob("model-receipt-v2*"))
    assert not (SCHEMA_ROOT / "model-receipt-v1.schema.json").exists()


def test_packaged_resources_are_exact_and_defensive() -> None:
    names = (
        "model-provider-rpc-method-matrix.v2.json",
        "model-provider-invoke-request-v2.schema.json",
        "model-provider-invoke-result-v2.schema.json",
        "model-provider-invoke-success-v2.schema.json",
    )
    for name in names:
        assert (RESOURCE_ROOT / name).read_bytes() == (SCHEMA_ROOT / name).read_bytes()
    first = get_model_provider_rpc_method_matrix_v2()
    first["reserved_method_ids"].append("forged.method/v1")
    assert get_model_provider_rpc_method_matrix_v2()["reserved_method_ids"] == [METHOD]


def _asset_verifier(golden: Mapping[str, Any]) -> Callable[[Mapping[str, Any], str], None]:
    source = {
        "model_request_asset": bytes.fromhex(golden["model_request_asset_bytes_hex"]),
        "response_asset": bytes.fromhex(golden["response_asset_bytes_hex"]),
    }

    def verify(identity: Mapping[str, Any], role: str) -> None:
        if role not in source:
            raise ContractError(1005, f"unknown Asset role {role}")
        if sha256_hex(source[role]) != identity["content_hash"]:
            raise ContractError(1005, f"{role} content hash drift")

    return verify


def _decoded_receipt(golden: Mapping[str, Any], state: str) -> Any:
    return decode_model_receipt(golden["canonical_receipts"][state])


def _receipt_proof(golden: Mapping[str, Any], state: str) -> Any:
    receipt = _decoded_receipt(golden, state)
    identity = golden["receipt_asset_identities"][state]
    return SimpleNamespace(
        receipt=receipt,
        asset_id=identity["asset_id"],
        asset_hash=identity["content_hash"],
    )


def _receipt_verifier(
    golden: Mapping[str, Any], state: str
) -> Callable[[Mapping[str, Any]], Any]:
    proof = _receipt_proof(golden, state)

    def verify(_anchor: Mapping[str, Any]) -> Any:
        return proof

    return verify


def test_request_result_terminal_success_error_and_defensive_copy(
    golden: dict[str, Any],
) -> None:
    request = parse_model_provider_invoke_request_v2(golden["request"])
    assert request["method"] == METHOD
    request["params"]["planner_context"]["job_id"] = "mutated-copy"
    assert golden["request"]["params"]["planner_context"]["job_id"] == "job-1"

    rebuilt = build_model_provider_invoke_request_v2(
        golden["request"]["params"]["planner_context"],
        golden["request"]["params"]["model_request_asset"],
        request_id=golden["request"]["id"],
    )
    assert rebuilt == golden["request"]
    assert model_provider_operation_digest_v2(rebuilt) == golden["operation_digest"]
    assert canonical_bytes(rebuilt["params"]).hex() == golden[
        "operation_canonical_bytes_hex"
    ]

    for state in TERMINAL_STATES:
        result = parse_model_provider_invoke_result_v2(
            golden["terminal_results"][state]
        )
        decoded = _decoded_receipt(golden, state)
        assert (
            validate_model_provider_rpc_success_v2(
                golden["terminal_successes"][state],
                request=golden["request"],
                canonical_receipt=decoded,
                asset_verifier=_asset_verifier(golden),
            )
            == result
        )
        assert result["provider_terminal_state"] == state
    assert golden["terminal_results"]["failed"]["response_asset"] is None
    assert (
        golden["terminal_results"]["failed"][
            "provider_transport_response_hash"
        ]
        is not None
    )
    assert (
        parse_model_provider_rpc_error_v2(
            golden["error"], request=golden["request"]
        )
        == golden["error"]
    )
    assert (
        validate_model_provider_rpc_response_v2(
            golden["request"], golden["error"]
        )
        == golden["error"]
    )


def _provider_objects(golden: Mapping[str, Any]) -> tuple[ProviderInvocation, ModelReceipt]:
    receipt = golden["canonical_receipts"]["receipted"]
    invocation = ProviderInvocation(
        invocation_id=receipt["invocation_id"],
        invocation_key=receipt["invocation_key"],
        endpoint=receipt["endpoint"],
        model=receipt["model"],
        messages=({"role": "user", "content": "Create the macro plan."},),
        parameters={"temperature": 0},
        profile_revision_id=receipt["profile_revision_id"],
        provider_plugin_id=receipt["provider_plugin_id"],
        provider_release_id=receipt["provider_release_id"],
        stream=False,
        input_context=receipt["input_context"],
        profile_revision=receipt["profile_revision"],
        body={
            "model": "planner-model-1",
            "messages": [{"role": "user", "content": "Create the macro plan."}],
            "temperature": 0,
            "stream": False,
        },
    )
    model_receipt = ModelReceipt(
        receipt_id=receipt["receipt_id"],
        invocation_id=receipt["invocation_id"],
        invocation_key=receipt["invocation_key"],
        state=receipt["state"],
        request_hash=receipt["request_hash"],
        response_hash=receipt["response_hash"],
        profile_revision_id=receipt["profile_revision_id"],
        provider_plugin_id=receipt["provider_plugin_id"],
        provider_release_id=receipt["provider_release_id"],
        endpoint=receipt["endpoint"],
        model=receipt["model"],
        lifecycle=tuple(receipt["lifecycle"]),
        prompt_tokens=receipt["prompt_tokens"],
        completion_tokens=receipt["completion_tokens"],
        total_tokens=receipt["total_tokens"],
        cost=receipt["cost"],
        retry_count=receipt["retry_count"],
        stream_termination=receipt["stream_termination"],
        error=receipt["error"],
        input_context=receipt["input_context"],
        profile_revision=receipt["profile_revision"],
        metadata=receipt["metadata"],
        response_asset_id=receipt["response_asset_id"],
        recovered=receipt["recovered"],
        uncertain=receipt["uncertain"],
        receipt_hash=receipt["receipt_hash"],
    )
    return invocation, model_receipt


def _frozen_invocation(receipt: Mapping[str, Any]) -> FrozenModelInvocation:
    return FrozenModelInvocation(
        invocation_id=receipt["invocation_id"],
        invocation_key=receipt["invocation_key"],
        request_hash=receipt["request_hash"],
        response_asset_id=receipt["response_asset_id"],
        response_hash=receipt["response_hash"],
        profile_revision_id=receipt["profile_revision_id"],
        provider_plugin_id=receipt["provider_plugin_id"],
        provider_release_id=receipt["provider_release_id"],
        input_context=receipt["input_context"],
        endpoint=receipt["endpoint"],
        model=receipt["model"],
        profile_revision=receipt["profile_revision"],
        max_retries=receipt["retry_count"],
        terminal_receipt_id=receipt["receipt_id"],
        terminal_receipt_hash=receipt["receipt_hash"],
    )


def test_real_provider_to_overlay_to_prompt_skill_cross_layer_bridge(
    golden: dict[str, Any],
) -> None:
    invocation, model_receipt = _provider_objects(golden)
    receipt = model_receipt.as_dict()
    assert invocation.request_hash == golden["result"][
        "provider_transport_request_hash"
    ]
    assert receipt == golden["canonical_receipts"]["receipted"]
    assert len(receipt) == 27

    receipt_bytes = canonical_bytes(receipt)
    identity = golden["receipt_asset_identities"]["receipted"]
    assert receipt_bytes.hex() == golden["receipt_asset_bytes_hex"]["receipted"]
    receipt_asset = AssetRef(identity["asset_id"], receipt_bytes, sha256_hex(receipt_bytes))
    proof = verify_model_receipt_asset(
        receipt_asset,
        invocation=_frozen_invocation(receipt),
        expected_context=receipt["input_context"],
        require_receipted=True,
    )
    result = validate_model_provider_rpc_success_v2(
        golden["success"],
        request=golden["request"],
        canonical_receipt=proof,
        asset_verifier=_asset_verifier(golden),
    )

    prompt_result = read_json(
        ROOT / "contracts" / "golden" / "prompt-skill-rpc-v2" / "execute.json"
    )["result"]
    prompt_result["model_receipt"] = copy.deepcopy(result["model_receipt_anchor"])
    parsed_prompt = parse_prompt_skill_execute_result_v2(prompt_result)
    assert parsed_prompt["model_receipt"] == result["model_receipt_anchor"]

    bridge_digest = hash_jcs(
        "model-provider-rpc-bridge/v2", golden["bridge_projection"]
    )
    assert bridge_digest == golden["bridge_digest"]


def test_existing_decoder_remains_the_only_full_receipt_authority(
    golden: dict[str, Any],
) -> None:
    receipt = golden["canonical_receipts"]["receipted"]
    assert len(decode_model_receipt(receipt)) == 27
    for field in receipt:
        missing = copy.deepcopy(receipt)
        del missing[field]
        with pytest.raises(ContractError):
            decode_model_receipt(missing)

    extra = copy.deepcopy(receipt)
    extra["unexpected"] = True
    extra["receipt_hash"] = hash_jcs(
        "model-receipt/v1",
        {key: value for key, value in extra.items() if key != "receipt_hash"},
    )
    with pytest.raises(ContractError):
        decode_model_receipt(extra)

    wrong_type = copy.deepcopy(receipt)
    wrong_type["provider_plugin_id"] = 7
    wrong_type["receipt_hash"] = hash_jcs(
        "model-receipt/v1",
        {key: value for key, value in wrong_type.items() if key != "receipt_hash"},
    )
    with pytest.raises(ContractError):
        decode_model_receipt(wrong_type)

    self_hash = copy.deepcopy(receipt)
    self_hash["receipt_hash"] = "0" * 64
    with pytest.raises(ContractError):
        decode_model_receipt(self_hash)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("profile_revision_id", "model-profile-rev-other"),
        ("provider_plugin_id", "com.plotpilot.provider.other"),
        ("provider_release_id", "8" * 64),
        ("endpoint", "https://models.example.test/v2"),
        ("model", "other-model"),
        ("input_context", {"job_id": "job-other"}),
        ("profile_revision", {"profile_revision_id": "model-profile-rev-other"}),
        ("metadata", {"fixture": "drift"}),
    ],
)
def test_rehashed_full_receipt_drift_fails_existing_asset_binding(
    golden: dict[str, Any], field: str, replacement: Any
) -> None:
    original = golden["canonical_receipts"]["receipted"]
    drifted = copy.deepcopy(original)
    drifted[field] = replacement
    drifted["receipt_hash"] = hash_jcs(
        "model-receipt/v1",
        {key: value for key, value in drifted.items() if key != "receipt_hash"},
    )
    decoded = decode_model_receipt(drifted)
    content = canonical_bytes(decoded.as_dict())
    asset = AssetRef("asset-model-receipt-receipted-1", content, sha256_hex(content))
    with pytest.raises(ContractError):
        verify_model_receipt_asset(asset, invocation=_frozen_invocation(original))


def test_six_hash_domains_are_separate_and_authoritatively_bound(
    golden: dict[str, Any],
) -> None:
    assert HASH_DOMAINS == (
        "model_request_asset.content_hash",
        "model_receipt_anchor.content_hash",
        "provider_transport_request_hash",
        "provider_transport_response_hash",
        "response_asset.content_hash",
        "model_receipt_anchor.receipt_hash",
    )
    result = golden["result"]
    values = {
        result["model_request_asset"]["content_hash"],
        result["model_receipt_anchor"]["content_hash"],
        result["provider_transport_request_hash"],
        result["provider_transport_response_hash"],
        result["response_asset"]["content_hash"],
        result["model_receipt_anchor"]["receipt_hash"],
    }
    assert len(values) == 6

    mutations = (
        ("provider_transport_request_hash", result["model_request_asset"]["content_hash"]),
        ("provider_transport_response_hash", result["response_asset"]["content_hash"]),
    )
    for field, replacement in mutations:
        drifted = copy.deepcopy(result)
        drifted[field] = replacement
        with pytest.raises(ContractError):
            validate_model_provider_invoke_result_v2(
                golden["request"],
                drifted,
                canonical_receipt=_decoded_receipt(golden, "receipted"),
                asset_verifier=_asset_verifier(golden),
            )

    response_hash_drift = copy.deepcopy(result)
    response_hash_drift["response_asset"]["content_hash"] = result[
        "provider_transport_response_hash"
    ]
    with pytest.raises(ContractError):
        validate_model_provider_invoke_result_v2(
            golden["request"],
            response_hash_drift,
            canonical_receipt=_decoded_receipt(golden, "receipted"),
            asset_verifier=_asset_verifier(golden),
        )


def test_exact_replay_is_contract_only_and_defensive(golden: dict[str, Any]) -> None:
    ledger = ModelProviderOperationLedgerV2()
    first, replayed = ledger.record(
        golden["request"],
        golden["success"],
        receipt_verifier=_receipt_verifier(golden, "receipted"),
        asset_verifier=_asset_verifier(golden),
    )
    assert replayed is False
    first["provider_terminal_state"] = "failed"
    assert ledger.replay(golden["request"])["provider_terminal_state"] == "receipted"
    second, replayed = ledger.record(
        golden["request"],
        golden["success"],
        receipt_verifier=_receipt_verifier(golden, "receipted"),
        asset_verifier=_asset_verifier(golden),
    )
    assert replayed is True and second["provider_terminal_state"] == "receipted"

    changed_request = copy.deepcopy(golden["request"])
    changed_response = copy.deepcopy(golden["success"])
    changed_request["id"] = "123e4567-e89b-42d3-a456-426614174101"
    changed_response["id"] = changed_request["id"]
    with pytest.raises(ContractError):
        ledger.record(
            changed_request,
            changed_response,
            canonical_receipt=_decoded_receipt(golden, "receipted"),
        )


def _reserved_fixtures() -> dict[str, dict[str, Any]]:
    fixtures = ROOT / "contracts" / "examples" / "fixtures"
    ordinary_plan = read_json(fixtures / "plugin-plan.json")
    ordinary_descriptor = read_json(fixtures / "capability-provider.json")
    ordinary_manifest = read_json(fixtures / "plugin-manifest-code.json")
    binding = copy.deepcopy(ordinary_plan)
    binding["bindings"][0]["capability_id"] = METHOD
    synthesizer = copy.deepcopy(ordinary_plan)
    synthesizer["result_mode"] = "synthesize"
    synthesizer["synthesizer"] = {
        "binding_id": synthesizer["bindings"][0]["binding_id"],
        "capability_id": METHOD,
        "plugin_id": synthesizer["bindings"][0]["plugin_id"],
        "release_requirement": synthesizer["bindings"][0]["release_requirement"],
    }
    descriptor = copy.deepcopy(ordinary_descriptor)
    descriptor["capability_id"] = METHOD
    manifest = copy.deepcopy(ordinary_manifest)
    manifest["capabilities"][0]["capability_id"] = METHOD
    return {
        "ordinary.plan": ordinary_plan,
        "ordinary.descriptor": ordinary_descriptor,
        "ordinary.manifest": ordinary_manifest,
        "reserved.plan.binding": binding,
        "reserved.plan.synthesizer": synthesizer,
        "reserved.descriptor": descriptor,
        "reserved.manifest": manifest,
    }


def test_reserved_method_boundary_rejects_business_surfaces() -> None:
    values = _reserved_fixtures()
    assert RESERVED_PROVIDER_RPC_METHODS == frozenset({METHOD})
    verify_plan(values["ordinary.plan"])
    verify_capability_descriptor(values["ordinary.descriptor"])
    verify_manifest(values["ordinary.manifest"])
    for fixture, action in (
        ("reserved.plan.binding", verify_plan),
        ("reserved.plan.synthesizer", verify_plan),
        ("reserved.descriptor", verify_capability_descriptor),
        ("reserved.manifest", verify_manifest),
    ):
        with pytest.raises(ContractError):
            action(values[fixture])


def _mutate(value: Any, mutation: Mapping[str, Any]) -> Any:
    result = copy.deepcopy(value)

    def set_path(target: Any, path: list[Any], replacement: Any) -> None:
        cursor = target
        for token in path[:-1]:
            cursor = cursor[token]
        cursor[path[-1]] = copy.deepcopy(replacement)

    operation = mutation["op"]
    if operation == "noop":
        return result
    if operation == "set":
        set_path(result, mutation["path"], mutation["value"])
        return result
    if operation == "delete":
        cursor = result
        for token in mutation["path"][:-1]:
            cursor = cursor[token]
        del cursor[mutation["path"][-1]]
        return result
    if operation == "set-many":
        for change in mutation["changes"]:
            set_path(result, change["path"], change["value"])
        return result
    raise AssertionError(f"unsupported corpus mutation: {operation}")


def _corpus_fixtures(golden: Mapping[str, Any]) -> dict[str, Any]:
    fixtures: dict[str, Any] = {
        "invoke.request": golden["request"],
        "invoke.bare-result": golden["result"],
        "invoke.error": golden["error"],
        "invoke.exchange.receipted": {
            "request": golden["request"],
            "response": golden["success"],
        },
        "invoke.ledger": golden["request"],
    }
    for state in TERMINAL_STATES:
        fixtures[f"invoke.result.{state}"] = golden["terminal_results"][state]
        fixtures[f"invoke.success.{state}"] = golden["terminal_successes"][state]
    fixtures.update(_reserved_fixtures())
    return fixtures


def _negative_action(
    case: Mapping[str, Any], golden: Mapping[str, Any]
) -> Callable[[], Any]:
    fixtures = _corpus_fixtures(golden)
    kind = case["kind"]
    fixture_id = case["fixture"]
    mutation = case["mutation"]

    if kind == "ledger":
        operation = mutation["op"]

        def ledger_action() -> Any:
            if operation == "unknown-replay":
                return ModelProviderOperationLedgerV2().replay(golden["request"])
            ledger = ModelProviderOperationLedgerV2()
            ledger.record(
                golden["request"],
                golden["success"],
                canonical_receipt=_decoded_receipt(golden, "receipted"),
            )
            if operation == "request-drift":
                request = copy.deepcopy(golden["request"])
                response = copy.deepcopy(golden["success"])
                request["id"] = "123e4567-e89b-42d3-a456-426614174101"
                response["id"] = request["id"]
                return ledger.record(
                    request,
                    response,
                    canonical_receipt=_decoded_receipt(golden, "receipted"),
                )
            if operation == "result-drift":
                result = copy.deepcopy(golden["result"])
                result["response_asset"]["content_hash"] = "f" * 64
                return ledger.record(
                    golden["request"],
                    result,
                    canonical_receipt=_decoded_receipt(golden, "receipted"),
                )
            raise AssertionError(operation)

        return ledger_action

    value = _mutate(fixtures[fixture_id], mutation)
    if kind == "request":
        return lambda: parse_model_provider_invoke_request_v2(value)
    if kind == "result":
        state = fixture_id.rsplit(".", 1)[-1]
        return lambda: validate_model_provider_invoke_result_v2(
            golden["request"],
            value,
            receipt_verifier=_receipt_verifier(golden, state),
            asset_verifier=_asset_verifier(golden),
        )
    if kind == "response":
        if fixture_id == "invoke.error":
            return lambda: validate_model_provider_rpc_response_v2(
                golden["request"], value
            )
        state = fixture_id.rsplit(".", 1)[-1]
        return lambda: validate_model_provider_rpc_response_v2(
            golden["request"],
            value,
            receipt_verifier=_receipt_verifier(golden, state),
            asset_verifier=_asset_verifier(golden),
        )
    if kind == "response-no-evidence":
        return lambda: validate_model_provider_rpc_response_v2(
            golden["request"], value
        )
    if kind == "error-with-evidence":
        return lambda: validate_model_provider_rpc_response_v2(
            golden["request"],
            value,
            canonical_receipt=_decoded_receipt(golden, "receipted"),
        )
    if kind == "envelope":
        return lambda: parse_model_provider_rpc_envelope_v2(
            value,
            request=golden["request"] if "method" not in value else None,
            canonical_receipt=(
                _decoded_receipt(golden, "receipted")
                if "result" in value and "method" not in value
                else None
            ),
        )
    if kind == "exchange":
        return lambda: validate_model_provider_rpc_response_v2(
            value["request"],
            value["response"],
            receipt_verifier=_receipt_verifier(golden, "receipted"),
            asset_verifier=_asset_verifier(golden),
        )
    if kind == "reserved":
        if fixture_id.startswith("reserved.plan"):
            return lambda: verify_plan(value)
        if fixture_id == "reserved.descriptor":
            return lambda: verify_capability_descriptor(value)
        return lambda: verify_manifest(value)
    raise AssertionError(f"unsupported corpus kind: {kind}")


def test_shared_negative_corpus_is_complete_unique_and_fail_closed(
    golden: dict[str, Any], corpus: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    manifest, group = corpus
    case_ids = [case["case_id"] for case in group["negative"]]
    assert manifest["schema"] == "model-provider-rpc-corpus-manifest/v2"
    assert manifest["group_count"] == 1
    assert manifest["negative_case_count"] == len(case_ids) == 76
    assert len(case_ids) == len(set(case_ids))
    digest = hashlib.sha256(
        json.dumps(sorted(case_ids), ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
    assert digest == manifest["negative_case_digest"]
    for case in group["negative"]:
        with pytest.raises(
            (ContractError, AssertionError, KeyError, TypeError, ValueError)
        ):
            _negative_action(case, golden)()


def test_golden_corpus_hashes_and_frozen_authorities(golden: dict[str, Any]) -> None:
    expected = read_json(GOLDEN_ROOT / "expected.json")
    for name, digest in expected["fixture_files"].items():
        assert hashlib.sha256((GOLDEN_ROOT / name).read_bytes()).hexdigest() == digest
    for name, digest in expected["corpus_files"].items():
        assert hashlib.sha256((CORPUS_ROOT / name).read_bytes()).hexdigest() == digest
    assert expected["operation_digest"] == golden["operation_digest"]
    assert expected["bridge_digest"] == golden["bridge_digest"]
    assert expected["hash_domains"] == list(HASH_DOMAINS)

    frozen = {
        "contracts/manifest-v1.json": "dff5bfc05b14d8d626b79c31f6ef4ef4cfc166f729af06d9c73f6e6300811bc4",
        "contracts/json-schema/rpc-method-matrix.v1.json": "7f2ef272ad8f0637b82800bb1e11856800cbc5bf799c38b095105da9382e5400",
        "contracts/json-schema/rpc-method-matrix.v2.json": "cee750df34fa73413f2e15ddc177af3d91fd521beb396e1e94a18ec2ecfb959c",
        "contracts/json-schema/prompt-skill-execute-request-v2.schema.json": "32697ec1ed5c280c2c55ae5af205be21090ddbef3064f297ff15d93f9e5735f7",
        "contracts/json-schema/prompt-skill-execute-result-v2.schema.json": "037db0d1b20490cd97bbde18e4a88a233327fb0a85eb83f1781b32a6e4dd63cd",
        "first-party-plugins/provider-openai-compatible/backend/src/plotpilot_provider_openai_compatible/provider.py": "8af2dcde324f40579a1bc77c094b474bc68f56752bc3f547cd5961f2976617d8",
        "first-party-plugins/prompt-skill-runtime/src/plotpilot_prompt_skill_runtime/attribution.py": "c2a810061806b47604b0a97ec09c4564c1ce1d9843ffc0ef911456c63ac7f14d",
        "backend/plotpilot_plugin_sdk/macro_planning_v2.py": "6ab4f6f70b03195eaeed473e0a2000e2661cb2ffaac95401c4f702effca91b20",
        "contracts/corpus/macro-planning-host-v1/integer-representations.json": "2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b",
        "contracts/corpus/macro-planning-host-v1/negative.json": "098c8ac356c87f0a2725d93c53cfc4ff32a954d0382f6dab6735fc80ba2b87fe",
        "contracts/examples/fixtures/macro-planning-host-positive.json": "b84c13701dde2fbad631bc06d039726dba68926f3d3f1bbe59f149097a7f6d5f",
        "contracts/golden/macro-planning-host-v1/positive.json": "b84c13701dde2fbad631bc06d039726dba68926f3d3f1bbe59f149097a7f6d5f",
        "contracts/json-schema/model-config-command-query-v2.schema.json": "2a5d4772028b7e2beed24f3b1459c17c4ac7c3e123368aad0a7eb9c3c80cc61a",
        "contracts/json-schema/model-profile-revision-v1.schema.json": "67d9528c22dfaacb8e292e55fea83c9ab0c41114f7f3ce9c377aeeb0f238c103",
        "contracts/json-schema/model-planning-http-error-v2.schema.json": "38304bd891774c128f9e42a1b6506aeb40975880149d3a59ea0047d17f510199",
        "contracts/json-schema/project-planning-command-query-v2.schema.json": "06a5142a8bef4b42d1342207132eb5a73e75e1c435ccc2b265ae3d762593c639",
        "contracts/json-schema/project-planner-runtime-input-v2.schema.json": "4c91ddee12d221e6758d5690496f12a8c15eabbce085e74530c3165687b3f117",
        "contracts/json-schema/project-planner-model-output-v1.schema.json": "32bb38601b75343a32320e665e9b0068522b2a0adef30936dbb5b7e5c6783029",
    }
    for relative, digest in frozen.items():
        blob = subprocess.run(
            [
                "git",
                "show",
                f"71bf310aabcd9e7a5ec1f86307eb64eed145d052:{relative}",
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        # The controller recorded these two Python authorities from its CRLF
        # Windows checkout while this isolated worktree materializes the same
        # Git blob with LF.  Accept only those two byte projections and still
        # require an empty Git diff, rather than rewriting a frozen path.
        byte_hashes = {hashlib.sha256(blob).hexdigest()}
        if b"\r\n" not in blob:
            byte_hashes.add(hashlib.sha256(blob.replace(b"\n", b"\r\n")).hexdigest())
        assert digest in byte_hashes
        assert (
            subprocess.run(
                ["git", "diff", "--quiet", "--", relative], cwd=ROOT, check=False
            ).returncode
            == 0
        )
