"""Contract gates for the isolated host macro-planning slice."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from plotpilot_plugin_sdk.errors import ContractError  # noqa: E402
from plotpilot_plugin_sdk.m4_m5_http_v2 import validate_http_exchange  # noqa: E402
from plotpilot_plugin_sdk.macro_planning_v2 import (  # noqa: E402
    JSON_MAX_SAFE_INTEGER,
    WIRE_WHITESPACE_CODEPOINTS,
    contains_wire_whitespace,
    has_non_wire_whitespace_character,
    is_wire_whitespace_character,
    model_profile_revision_hash,
    parse_macro_planning,
    parse_model_profile_revision_v1,
    parse_project_planner_model_output_v1,
    parse_project_planner_runtime_input_v2,
    parse_project_planning_v2,
    planner_runtime_input_hash,
    validate_project_planning_start,
    validate_project_planning_start_exchange,
    validate_secret_put_exchange,
    validate_workspace_plan_selection,
)
from verify_contracts import (  # noqa: E402
    verify_contract_manifest,
    verify_macro_planning_host,
    verify_schemas,
)

SCHEMA_DIR = ROOT / "contracts" / "json-schema"
POSITIVE_PATH = ROOT / "contracts" / "golden" / "macro-planning-host-v1" / "positive.json"
INTEGER_VECTOR_PATH = (
    ROOT / "contracts" / "corpus" / "macro-planning-host-v1" / "integer-representations.json"
)
SCHEMA_FILES = (
    "model-config-command-query-v2.schema.json",
    "model-profile-revision-v1.schema.json",
    "model-planning-http-error-v2.schema.json",
    "project-planning-command-query-v2.schema.json",
    "project-planner-runtime-input-v2.schema.json",
    "project-planner-model-output-v1.schema.json",
)
MACRO_ROUTES = (
    "model-secret.put",
    "model-profile.revise",
    "workspace-plan.select",
    "project-planning.get",
    "project-planning.start",
)


def _positive() -> dict[str, Any]:
    return json.loads(POSITIVE_PATH.read_text(encoding="utf-8"))


def _rejected(action: Any) -> None:
    with pytest.raises(ContractError):
        action()


def _object_nodes(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    found: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    if isinstance(value, dict):
        if value.get("type") == "object":
            found.append((path, value))
        for key, child in value.items():
            found.extend(_object_nodes(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_object_nodes(child, (*path, str(index))))
    return found


def test_macro_planning_proof_gate_is_complete() -> None:
    assert verify_macro_planning_host() == {
        "routes": 5,
        "schemas": 6,
        "fixtures": 17,
        "http_exchanges": 5,
        "negative_cases": 46,
        "negative_case_digest": "378ebbccc4fe6bc28f373e8267232dfe2a54151811dd049fa66b91309f3e1fd4",
        "safe_integer_boundary_fields": 5,
        "canonical_integer_fields": 8,
        "integer_vector_count": 34,
        "integer_vector_accepted": 19,
        "integer_vector_rejected": 15,
        "integer_vector_source_sha256": "2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b",
        "integer_vector_result_digest": "d4cfe7ec27c052badd2f67723fa5a4123becd60b6c1be11ce37b07b58f00f13d",
        "wire_whitespace_codepoints": 30,
        "representative_whitespace_cases": 12,
        "corpus_files": 11,
        "model_profile_revision_hash": "9570e425de7546804e3efe66fa09dd9dfac73510285e066d9aaceab5032dd8ed",
        "planner_runtime_input_hash": "881cb610c9e018a25078364c4b89fdbf6e3ef014231290b9f9c5b731b178acbd",
    }


def test_six_generated_schemas_are_closed_and_discriminated() -> None:
    documents = [json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8")) for name in SCHEMA_FILES]
    assert all(document["$schema"] == "https://json-schema.org/draft/2020-12/schema" for document in documents)
    for document in documents:
        nodes = _object_nodes(document)
        assert nodes
        assert all(node.get("additionalProperties") is False for _, node in nodes)

    discriminators: list[str] = []
    for document in documents:
        branches = document.get("oneOf", [document])
        discriminators.extend(branch["properties"]["schema"]["const"] for branch in branches)
    assert len(discriminators) == len(set(discriminators)) == 16
    assert verify_schemas()["schemas"] == 70


def test_all_five_routes_require_exact_trusted_path_identity() -> None:
    positive = _positive()
    assert tuple(exchange["route_id"] for exchange in positive["exchanges"]) == MACRO_ROUTES
    for exchange in positive["exchanges"]:
        validate_http_exchange(
            exchange["route_id"],
            exchange["request"],
            exchange["status"],
            exchange["response"],
            path_params=exchange["path_params"],
        )
        _rejected(
            lambda exchange=exchange: validate_http_exchange(
                exchange["route_id"],
                exchange["request"],
                exchange["status"],
                exchange["response"],
            )
        )
        extra = {**exchange["path_params"], "extra": "forbidden"}
        _rejected(
            lambda exchange=exchange, extra=extra: validate_http_exchange(
                exchange["route_id"],
                exchange["request"],
                exchange["status"],
                exchange["response"],
                path_params=extra,
            )
        )
        field = next(iter(exchange["path_params"]))
        mismatched = {**exchange["path_params"], field: "other-identity"}
        _rejected(
            lambda exchange=exchange, mismatched=mismatched: validate_http_exchange(
                exchange["route_id"],
                exchange["request"],
                exchange["status"],
                exchange["response"],
                path_params=mismatched,
            )
        )


def test_raw_secret_is_input_only_and_recursively_rejected_from_outputs() -> None:
    fixtures = _positive()["fixtures"]
    command = fixtures["model_secret_put_command"]
    validate_secret_put_exchange(command, fixtures["model_secret_put_result"])
    validate_secret_put_exchange(command, fixtures["model_secret_error"])
    raw = command["value"]
    for response in (
        {"nested": [{"message": f"wrapped:{raw}:value"}]},
        {"nested": {f"wrapped:{raw}:key": "redacted"}},
    ):
        _rejected(lambda response=response: validate_secret_put_exchange(command, response))

    config = json.loads((SCHEMA_DIR / SCHEMA_FILES[0]).read_text(encoding="utf-8"))
    value_branches = [
        branch["properties"]["schema"]["const"]
        for branch in config["oneOf"]
        if "value" in branch.get("properties", {})
    ]
    assert value_branches == ["model-secret-put-command/v2"]


@pytest.mark.parametrize(
    "bad_reference",
    [
        "sk-live-not-a-reference",
        "provider-main",
        " secret://provider-main",
        "secret://provider-main ",
        "secret:///provider-main",
        "secret://provider-main?alias=x",
        "secret://provider-main#fragment",
    ],
)
def test_secret_references_use_exact_host_opaque_grammar(bad_reference: str) -> None:
    command = copy.deepcopy(_positive()["fixtures"]["model_profile_revise_command"])
    command["provider"]["api_key_ref"] = bad_reference
    _rejected(lambda: parse_macro_planning(command))


def test_profile_revision_is_append_only_hash_bound_and_defensively_copied() -> None:
    profile = _positive()["fixtures"]["model_profile_revision"]
    parsed = parse_model_profile_revision_v1(profile)
    assert parsed["revision_hash"] == model_profile_revision_hash(profile)
    parsed["provider"]["model_name"] = "mutated-copy"
    assert profile["provider"]["model_name"] == "planner-model-1"

    bad_hash = copy.deepcopy(profile)
    bad_hash["revision_hash"] = "0" * 64
    _rejected(lambda: parse_model_profile_revision_v1(bad_hash))
    missing_parent = copy.deepcopy(profile)
    missing_parent["revision_number"] = 2
    _rejected(lambda: parse_model_profile_revision_v1(missing_parent))


def test_plan_selection_is_explicit_cas_and_hash_bound() -> None:
    fixtures = _positive()["fixtures"]
    command = fixtures["workspace_plan_selection_command"]
    result = fixtures["workspace_plan_selection_result"]
    validate_workspace_plan_selection(
        command,
        result,
        expected_workspace_revision=7,
        expected_current_plan_revision_id=None,
        expected_current_plan_revision_hash=None,
        active_generation_id="generation-planning-1",
    )
    _rejected(lambda: validate_workspace_plan_selection(command, result, expected_workspace_revision=6))
    tampered = copy.deepcopy(result)
    tampered["plan_revision_hash"] = "0" * 64
    _rejected(lambda: validate_workspace_plan_selection(command, tampered))
    automatic = copy.deepcopy(command)
    automatic["selection_mode"] = "automatic"
    _rejected(lambda: parse_macro_planning(automatic))


def test_planning_availability_and_start_authority_fail_closed() -> None:
    fixtures = _positive()["fixtures"]
    parse_project_planning_v2(fixtures["project_planning_availability_ready"])
    parse_project_planning_v2(fixtures["project_planning_availability_unavailable"])
    fail_open = copy.deepcopy(fixtures["project_planning_availability_unavailable"])
    fail_open["available"] = True
    _rejected(lambda: parse_project_planning_v2(fail_open))

    command = fixtures["project_planning_start_command"]
    authority = {
        "document_id": command["project_brief_document_id"],
        **command["expected_project_brief"],
    }
    validate_project_planning_start(command, expected_project_brief=authority)
    validate_project_planning_start_exchange(command, fixtures["project_planning_start_result"])
    stale = {**authority, "content_hash": "0" * 64}
    _rejected(lambda: validate_project_planning_start(command, expected_project_brief=stale))
    for field in ("job_id", "run_snapshot_id", "model_profile_revision_id", "generation_id", "writer_epoch"):
        injected = copy.deepcopy(command)
        injected[field] = "caller-authority" if field != "writer_epoch" else 99
        _rejected(lambda injected=injected: parse_macro_planning(injected))

    wrong_result = copy.deepcopy(fixtures["project_planning_start_result"])
    wrong_result["workspace_id"] = "workspace-other"
    _rejected(lambda: validate_project_planning_start_exchange(command, wrong_result))


def test_runtime_input_and_model_output_are_closed_hash_bound_data_only() -> None:
    fixtures = _positive()["fixtures"]
    runtime_input = fixtures["project_planner_runtime_input"]
    parsed = parse_project_planner_runtime_input_v2(runtime_input)
    assert parsed["input_hash"] == planner_runtime_input_hash(runtime_input)
    parsed["targets"]["outline"]["document_id"] = "mutated-copy"
    assert runtime_input["targets"]["outline"]["document_id"] == "document-outline"
    for field in ("generated", "source_refs", "run_snapshot", "execution", "attempt_id", "worker_id"):
        injected = copy.deepcopy(runtime_input)
        injected[field] = {}
        _rejected(lambda injected=injected: parse_project_planner_runtime_input_v2(injected))

    output = fixtures["project_planner_model_output"]
    assert set(output) == {"schema", "setting", "bible", "outline"}
    parse_project_planner_model_output_v1(output)
    for role in ("setting", "bible", "outline"):
        blank = copy.deepcopy(output)
        blank[role] = " \t "
        _rejected(lambda blank=blank: parse_project_planner_model_output_v1(blank))
    injected_output = copy.deepcopy(output)
    injected_output["auto_accept"] = True
    _rejected(lambda: parse_project_planner_model_output_v1(injected_output))


def test_all_p0a_integers_use_draft_2020_12_mathematical_semantics() -> None:
    fixtures = _positive()["fixtures"]
    assert JSON_MAX_SAFE_INTEGER == 9_007_199_254_740_991

    profile = fixtures["model_profile_revision"]
    for raw_token in ("1", "1.0", "1e0"):
        represented = copy.deepcopy(profile)
        represented["revision_number"] = json.loads(raw_token)
        before_hash = copy.deepcopy(represented)
        assert model_profile_revision_hash(represented) == profile["revision_hash"]
        assert represented == before_hash
        parsed = parse_model_profile_revision_v1(represented)
        assert type(parsed["revision_number"]) is int
        assert parsed["revision_number"] == 1
        assert represented["revision_number"] == json.loads(raw_token)

    option_float_profile = copy.deepcopy(profile)
    for field in ("max_output_tokens", "timeout_seconds", "max_retries"):
        option_float_profile["provider"]["options"][field] = float(
            option_float_profile["provider"]["options"][field]
        )
    before_hash = copy.deepcopy(option_float_profile)
    assert model_profile_revision_hash(option_float_profile) == profile["revision_hash"]
    assert option_float_profile == before_hash
    parsed_float_options = parse_model_profile_revision_v1(option_float_profile)
    assert all(
        type(parsed_float_options["provider"]["options"][field]) is int
        for field in ("max_output_tokens", "timeout_seconds", "max_retries")
    )
    assert all(
        type(option_float_profile["provider"]["options"][field]) is float
        for field in ("max_output_tokens", "timeout_seconds", "max_retries")
    )

    float_command = copy.deepcopy(fixtures["model_profile_revise_command"])
    for field in ("max_output_tokens", "timeout_seconds", "max_retries"):
        float_command["provider"]["options"][field] = float(
            float_command["provider"]["options"][field]
        )
    parsed_command = parse_macro_planning(float_command)
    assert all(
        type(parsed_command["provider"]["options"][field]) is int
        for field in ("max_output_tokens", "timeout_seconds", "max_retries")
    )
    assert all(
        type(float_command["provider"]["options"][field]) is float
        for field in ("max_output_tokens", "timeout_seconds", "max_retries")
    )

    float_result = copy.deepcopy(fixtures["model_profile_revise_result"])
    float_result["revision"]["revision_number"] = 1.0
    for field in ("max_output_tokens", "timeout_seconds", "max_retries"):
        float_result["revision"]["provider"]["options"][field] = float(
            float_result["revision"]["provider"]["options"][field]
        )
    float_result["revision"]["revision_hash"] = model_profile_revision_hash(
        float_result["revision"]
    )
    parsed_result = parse_macro_planning(float_result)
    assert type(parsed_result["revision"]["revision_number"]) is int
    assert all(
        type(parsed_result["revision"]["provider"]["options"][field]) is int
        for field in ("max_output_tokens", "timeout_seconds", "max_retries")
    )
    assert type(float_result["revision"]["revision_number"]) is float

    profile = copy.deepcopy(fixtures["model_profile_revision"])
    profile["revision_number"] = JSON_MAX_SAFE_INTEGER
    profile["parent_revision_id"] = "revision-model-profile-parent-max-safe"
    profile["revision_hash"] = model_profile_revision_hash(profile)
    parse_model_profile_revision_v1(profile)

    maximum_command = copy.deepcopy(fixtures["workspace_plan_selection_command"])
    maximum_command["expected_workspace_revision"] = JSON_MAX_SAFE_INTEGER
    parse_macro_planning(maximum_command)
    monotonic_command = copy.deepcopy(maximum_command)
    monotonic_command["expected_workspace_revision"] = JSON_MAX_SAFE_INTEGER - 1
    maximum_result = copy.deepcopy(fixtures["workspace_plan_selection_result"])
    maximum_result["workspace_revision"] = JSON_MAX_SAFE_INTEGER
    validate_workspace_plan_selection(monotonic_command, maximum_result)

    start_result = copy.deepcopy(fixtures["project_planning_start_result"])
    start_result["writer_epoch"] = JSON_MAX_SAFE_INTEGER
    parse_project_planning_v2(start_result)

    runtime_input = copy.deepcopy(fixtures["project_planner_runtime_input"])
    runtime_input["writer_epoch"] = JSON_MAX_SAFE_INTEGER
    runtime_input["input_hash"] = planner_runtime_input_hash(runtime_input)
    parse_project_planner_runtime_input_v2(runtime_input)

    authority_float_cases = (
        ("workspace_plan_selection_command", "expected_workspace_revision", "0.0", 0),
        ("workspace_plan_selection_result", "workspace_revision", "1.0", 1),
        ("project_planning_start_result", "writer_epoch", "1e0", 1),
    )
    for fixture_name, field, raw_token, expected in authority_float_cases:
        represented = copy.deepcopy(fixtures[fixture_name])
        represented[field] = json.loads(raw_token)
        parsed = parse_macro_planning(represented)
        assert type(parsed[field]) is int
        assert parsed[field] == expected
        assert type(represented[field]) is float

    runtime_max_float = copy.deepcopy(fixtures["project_planner_runtime_input"])
    runtime_max_float["writer_epoch"] = json.loads("9007199254740991.0")
    before_runtime_hash = copy.deepcopy(runtime_max_float)
    max_float_hash = planner_runtime_input_hash(runtime_max_float)
    assert runtime_max_float == before_runtime_hash
    runtime_max_float["input_hash"] = max_float_hash
    parsed_runtime_max = parse_project_planner_runtime_input_v2(runtime_max_float)
    assert type(parsed_runtime_max["writer_epoch"]) is int
    assert parsed_runtime_max["writer_epoch"] == JSON_MAX_SAFE_INTEGER
    assert type(runtime_max_float["writer_epoch"]) is float
    assert max_float_hash == runtime_input["input_hash"]

    for invalid in (True, 1.5, float("inf"), float("-inf"), float("nan")):
        invalid_profile = copy.deepcopy(fixtures["model_profile_revision"])
        invalid_profile["revision_number"] = invalid
        _rejected(lambda invalid_profile=invalid_profile: model_profile_revision_hash(invalid_profile))
        _rejected(lambda invalid_profile=invalid_profile: parse_model_profile_revision_v1(invalid_profile))

    unsafe = JSON_MAX_SAFE_INTEGER + 1
    unsafe_profile = copy.deepcopy(profile)
    unsafe_profile["revision_number"] = unsafe
    _rejected(lambda: model_profile_revision_hash(unsafe_profile))
    _rejected(lambda: parse_model_profile_revision_v1(unsafe_profile))

    unsafe_command = copy.deepcopy(maximum_command)
    unsafe_command["expected_workspace_revision"] = unsafe
    _rejected(lambda: parse_macro_planning(unsafe_command))
    unsafe_result = copy.deepcopy(maximum_result)
    unsafe_result["workspace_revision"] = unsafe
    _rejected(lambda: parse_macro_planning(unsafe_result))
    unsafe_start = copy.deepcopy(start_result)
    unsafe_start["writer_epoch"] = unsafe
    _rejected(lambda: parse_project_planning_v2(unsafe_start))
    unsafe_runtime = copy.deepcopy(runtime_input)
    unsafe_runtime["writer_epoch"] = unsafe
    _rejected(lambda: planner_runtime_input_hash(unsafe_runtime))
    _rejected(lambda: parse_project_planner_runtime_input_v2(unsafe_runtime))

    bounds_by_field = {
        "revision_number": (1, JSON_MAX_SAFE_INTEGER, 2),
        "expected_workspace_revision": (0, JSON_MAX_SAFE_INTEGER, 1),
        "workspace_revision": (1, JSON_MAX_SAFE_INTEGER, 1),
        "writer_epoch": (1, JSON_MAX_SAFE_INTEGER, 2),
        "max_output_tokens": (1, 10_000_000, 3),
        "timeout_seconds": (1, 86_400, 3),
        "max_retries": (0, 16, 3),
    }
    occurrences = {field: [] for field in bounds_by_field}
    for schema_file in SCHEMA_FILES:
        document = json.loads((SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))
        for _, node in _object_nodes(document):
            for field, (minimum, maximum, _) in bounds_by_field.items():
                if field in node.get("properties", {}):
                    occurrences[field].append(node["properties"][field])
                    assert node["properties"][field] == {
                        "maximum": maximum,
                        "minimum": minimum,
                        "type": "integer",
                    }
    assert {
        field: len(values)
        for field, values in occurrences.items()
    } == {
        field: expected_count
        for field, (_, _, expected_count) in bounds_by_field.items()
    }


def test_shared_raw_integer_vector_is_generated_and_hash_bound() -> None:
    raw = INTEGER_VECTOR_PATH.read_bytes()
    vectors = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == (
        "2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b"
    )
    assert (
        vectors["field_count"],
        vectors["vector_count"],
        vectors["accepted_count"],
        vectors["rejected_count"],
    ) == (8, 34, 19, 15)
    raw_tokens = {vector["raw_token"] for vector in vectors["vectors"]}
    assert {
        "0",
        "1",
        "1.0",
        "1e0",
        "true",
        "1.5",
        "1e999",
        "9007199254740991",
        "9007199254740991.0",
        "9007199254740992",
        "9007199254740992.0",
    } <= raw_tokens
    assert {
        vector["field_id"]
        for vector in vectors["vectors"]
    } == {
        field["field_id"]
        for field in vectors["fields"]
    }
    assert "integer-representations.json" in (
        ROOT / "tools" / "integration" / "verify_contracts.py"
    ).read_text(encoding="utf-8")
    assert "integer-representations.json" in (
        ROOT / "tools" / "integration" / "verify_contracts.mjs"
    ).read_text(encoding="utf-8")


def test_wire_whitespace_is_explicit_and_cross_language_stable() -> None:
    expected = tuple(
        [*range(0x0009, 0x000E)]
        + [*range(0x001C, 0x0021)]
        + [
            0x0085,
            0x00A0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
            0xFEFF,
        ]
    )
    assert WIRE_WHITESPACE_CODEPOINTS == expected
    assert len(expected) == len(set(expected)) == 30

    fixtures = _positive()["fixtures"]
    internal_space = copy.deepcopy(fixtures["model_profile_revise_command"])
    internal_space["provider"]["model_name"] = "planner model 1"
    parse_macro_planning(internal_space)

    roles = ("setting", "bible", "outline")
    for index, codepoint in enumerate(expected):
        character = chr(codepoint)
        assert is_wire_whitespace_character(character)
        assert contains_wire_whitespace(f"left{character}right")
        assert not has_non_wire_whitespace_character(character)
        assert has_non_wire_whitespace_character(f"{character}content")

        endpoint = copy.deepcopy(fixtures["model_profile_revise_command"])
        endpoint["provider"]["endpoint"] = f"https://models.example.test/v1{character}suffix"
        _rejected(lambda endpoint=endpoint: parse_macro_planning(endpoint))

        for value in (f"{character}planner-model-1", f"planner-model-1{character}"):
            model_name = copy.deepcopy(fixtures["model_profile_revise_command"])
            model_name["provider"]["model_name"] = value
            _rejected(lambda model_name=model_name: parse_macro_planning(model_name))

        error = copy.deepcopy(fixtures["model_profile_error"])
        error["message"] = character
        _rejected(lambda error=error: parse_macro_planning(error))

        output = copy.deepcopy(fixtures["project_planner_model_output"])
        output[roles[index % len(roles)]] = character
        _rejected(lambda output=output: parse_project_planner_model_output_v1(output))

    schema_patterns: list[str] = []
    for schema_file in SCHEMA_FILES:
        document = json.loads((SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))

        def collect_patterns(value: Any) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("pattern"), str):
                    schema_patterns.append(value["pattern"])
                for child in value.values():
                    collect_patterns(child)
            elif isinstance(value, list):
                for child in value:
                    collect_patterns(child)

        collect_patterns(document)
    assert all(r"\s" not in pattern and r"\S" not in pattern for pattern in schema_patterns)

    python_source = (ROOT / "backend" / "plotpilot_plugin_sdk" / "macro_planning_v2.py").read_text(encoding="utf-8")
    assert ".strip(" not in python_source
    assert ".isspace(" not in python_source
    node_source = (ROOT / "tools" / "integration" / "verify_contracts.mjs").read_text(encoding="utf-8")
    macro_node_source = node_source.split("function verifyMacroPlanningHost()", 1)[1].split(
        "async function verifyPromptSkill()", 1
    )[0]
    assert ".trim(" not in macro_node_source
    assert r"/\s" not in macro_node_source


def test_manifests_and_frozen_contract_bytes_are_exact() -> None:
    manifest = verify_contract_manifest()
    assert manifest["v2_schemas"] == 15
    assert manifest["v2_files"] == 182
    frozen = {
        "contracts/manifest-v1.json": "dff5bfc05b14d8d626b79c31f6ef4ef4cfc166f729af06d9c73f6e6300811bc4",
        "contracts/corpus/manifest.json": "bcaafab242980366546c34c824256a0396ed370345d70f3e5b1582090a68ce77",
        "contracts/json-schema/rpc-method-matrix.v1.json": "7f2ef272ad8f0637b82800bb1e11856800cbc5bf799c38b095105da9382e5400",
        "contracts/json-schema/rpc-method-matrix.v2.json": "cee750df34fa73413f2e15ddc177af3d91fd521beb396e1e94a18ec2ecfb959c",
        "contracts/json-schema/rpc-request-v1.schema.json": "2fdb68c75daf0de4e6a0fb17a898609dd506358e1d8e0818436351bac67a241d",
        "contracts/json-schema/rpc-envelope-v1.schema.json": "8bed71688adecd9e915a5d038774bdc6e3dfaf17300076a979dd2bc3ed7c2aed",
        "contracts/json-schema/rpc-method-success-v2.schema.json": "851b6a3f0a153683b8ceb9f6920e8f626f3a4f7d474f4b2e584d8c524b49e82a",
        "contracts/json-schema/prompt-skill-execute-result-v2.schema.json": "037db0d1b20490cd97bbde18e4a88a233327fb0a85eb83f1781b32a6e4dd63cd",
    }
    assert {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in frozen
    } == frozen


def test_macro_planning_contract_family_is_separate_from_provider_rpc_overlay() -> None:
    core_matrix = json.loads(
        (SCHEMA_DIR / "core-api-method-matrix.v2.json").read_text(encoding="utf-8")
    )
    route_ids = [route["route_id"] for route in core_matrix["routes"]]
    assert core_matrix["macro_planning_routes"] == list(MACRO_ROUTES)
    assert [route_id for route_id in route_ids if route_id in MACRO_ROUTES] == list(
        MACRO_ROUTES
    )

    provider_matrix = json.loads(
        (SCHEMA_DIR / "model-provider-rpc-method-matrix.v2.json").read_text(
            encoding="utf-8"
        )
    )
    reserved_method = "model.provider.invoke/v1"
    assert provider_matrix["reserved_method_ids"] == [reserved_method]
    assert provider_matrix["authority"] == "core"
    assert provider_matrix["direction"] == "host-to-provider"
    assert provider_matrix["plugin_authority_allowed"] is False
    assert reserved_method not in route_ids
    assert reserved_method not in core_matrix["macro_planning_routes"]

    provider_schema_names = {
        "model-provider-invoke-request-v2.schema.json",
        "model-provider-invoke-result-v2.schema.json",
        "model-provider-invoke-success-v2.schema.json",
    }
    assert set(SCHEMA_FILES).isdisjoint(provider_schema_names)
    assert all((SCHEMA_DIR / name).is_file() for name in provider_schema_names)

    p0a_authority_paths = [
        SCHEMA_DIR / "core-api-method-matrix.v2.json",
        *(SCHEMA_DIR / name for name in SCHEMA_FILES),
        POSITIVE_PATH,
        INTEGER_VECTOR_PATH,
        ROOT / "contracts" / "corpus" / "macro-planning-host-v1" / "negative.json",
        ROOT / "contracts" / "corpus" / "macro-planning-host-v1" / "manifest.json",
    ]
    for path in p0a_authority_paths:
        content = path.read_text(encoding="utf-8")
        assert reserved_method not in content, path
        assert "model-provider-invoke" not in content, path
