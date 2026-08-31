from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from plotpilot_plugin_sdk.errors import ContractError  # noqa: E402
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import (  # noqa: E402
    METHOD,
    PromptSkillOperationLedgerV2,
    build_prompt_skill_execute_request_v2,
    build_rpc_success_v2,
    parse_prompt_skill_execute_request_v2,
    parse_prompt_skill_execute_result_v2,
    prompt_skill_operation_key,
    validate_prompt_skill_execute_v2,
    validate_rpc_success_v2,
)


GOLDEN_ROOT = ROOT / "contracts" / "golden" / "prompt-skill-rpc-v2"
CORPUS_ROOT = ROOT / "contracts" / "corpus" / "prompt-skill-rpc-v2"
SCHEMA_ROOT = ROOT / "contracts" / "json-schema"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return read_json(GOLDEN_ROOT / "execute.json")


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    manifest = read_json(CORPUS_ROOT / "manifest.json")
    groups = [read_json(CORPUS_ROOT / "01-execute.json")]
    assert manifest["group_count"] == len(groups) == 1
    assert manifest["negative_case_count"] == sum(len(group["negative"]) for group in groups)
    return {"manifest": manifest, "groups": groups}


def test_v2_method_matrix_and_schemas_are_closed() -> None:
    matrix = read_json(SCHEMA_ROOT / "rpc-method-matrix.v2.json")
    assert matrix["schema"] == "rpc-method-matrix/v2"
    assert matrix["authority"] == "core"
    assert matrix["plugin_authority_allowed"] is False
    assert [item["method"] for item in matrix["methods"]] == [METHOD]
    assert matrix["methods"][0] == {
        "authority": "core",
        "consumer": "P2",
        "direction": "request-response",
        "lease_fenced": True,
        "method": METHOD,
        "operation_key_required": True,
        "request_schema": "prompt-skill-execute-request/v2",
        "result_contract": "artifact-bundle/v1",
        "result_schema": "prompt-skill-execute-result/v2",
        "success_schema": "rpc-method-success/v2",
    }

    for name in (
        "prompt-skill-execute-request-v2.schema.json",
        "prompt-skill-execute-result-v2.schema.json",
        "rpc-method-success-v2.schema.json",
    ):
        schema = read_json(SCHEMA_ROOT / name)
        Draft202012Validator.check_schema(schema)

        def assert_closed(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, (name, node)
                for child in node.values():
                    assert_closed(child)
            elif isinstance(node, list):
                for child in node:
                    assert_closed(child)

        assert_closed(schema)


def test_python_positive_request_result_success_and_ledger(golden: dict[str, Any]) -> None:
    request = parse_prompt_skill_execute_request_v2(golden["request"])
    result = parse_prompt_skill_execute_result_v2(golden["result"])
    assert request["method"] == METHOD
    assert result["status"] == "succeeded"
    assert validate_rpc_success_v2(golden["success"], request=request) == golden["result"]
    assert validate_prompt_skill_execute_v2(request, golden["success"], expected_lease_epoch=1) == golden["result"]

    rebuilt_request = build_prompt_skill_execute_request_v2(request["params"], request_id=request["id"])
    assert rebuilt_request == request
    assert build_rpc_success_v2(request, result) == golden["success"]
    assert len(prompt_skill_operation_key(request)) == 64

    ledger = PromptSkillOperationLedgerV2()
    first, replayed = ledger.record(request, result, expected_lease_epoch=1)
    assert first == result
    assert replayed is False
    second, replayed = ledger.record(request, golden["success"], expected_lease_epoch=1)
    assert second == result
    assert replayed is True
    assert ledger.replay(request) == result


def _set_path(value: dict[str, Any], path: list[str], replacement: Any) -> dict[str, Any]:
    target: Any = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = copy.deepcopy(replacement)
    return value


def _python_case_action(case: dict[str, Any], golden: dict[str, Any]) -> Callable[[], Any]:
    mutation = case["mutation"]
    fixture = case["fixture"]
    if mutation["op"] == "set-both":
        request = copy.deepcopy(golden["request"])
        result = copy.deepcopy(golden["result"])
        _set_path(request, mutation["request_path"], mutation["value"])
        _set_path(result, mutation["result_path"], mutation["value"])
        return lambda: validate_prompt_skill_execute_v2(
            request,
            result,
            expected_lease_epoch=mutation["expected_lease_epoch"],
        )

    if fixture == "execute.request":
        request = copy.deepcopy(golden["request"])
        _set_path(request, mutation["path"], mutation["value"])
        return lambda: parse_prompt_skill_execute_request_v2(request)
    if fixture == "execute.result":
        result = copy.deepcopy(golden["result"])
        _set_path(result, mutation["path"], mutation["value"])
        return lambda: validate_prompt_skill_execute_v2(golden["request"], result)
    if fixture == "execute.success":
        success = copy.deepcopy(golden["success"])
        _set_path(success, mutation["path"], mutation["value"])
        return lambda: validate_prompt_skill_execute_v2(golden["request"], success)
    raise AssertionError(f"unknown Prompt/Skill corpus fixture: {fixture}")


def test_python_negative_corpus_is_fail_closed(golden: dict[str, Any], corpus: dict[str, Any]) -> None:
    observed: set[str] = set()
    for group in corpus["groups"]:
        for case in group["negative"]:
            with pytest.raises(ContractError):
                _python_case_action(case, golden)()
            observed.add(case["case_id"])
    assert len(observed) == corpus["manifest"]["negative_case_count"]


def test_python_golden_and_corpus_determinism(corpus: dict[str, Any]) -> None:
    expected = read_json(GOLDEN_ROOT / "expected.json")
    for name, digest in expected["fixture_files"].items():
        actual = hashlib.sha256((GOLDEN_ROOT / name).read_bytes()).hexdigest()
        assert actual == digest
    case_ids = sorted(case["case_id"] for group in corpus["groups"] for case in group["negative"])
    assert case_ids == sorted(
        {
            "prompt-skill-v2-identity-drift-workspace",
            "prompt-skill-v2-identity-drift-plugin-release",
            "prompt-skill-v2-missing-model-receipt",
            "prompt-skill-v2-stale-lease",
            "prompt-skill-v2-mismatched-result-bundle",
            "prompt-skill-v2-extra-request-field",
            "prompt-skill-v2-extra-result-field",
            "prompt-skill-v2-model-receipt-binding-drift",
        }
    )


def _run_typescript_case(corpus: dict[str, Any]) -> dict[str, Any]:
    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const rpc = await import(pathToFileURL('frontend/src/contracts/prompt-skill-rpc-v2.ts').href)
const golden = JSON.parse(readFileSync('contracts/golden/prompt-skill-rpc-v2/execute.json', 'utf8'))
const corpus = JSON.parse(readFileSync('contracts/corpus/prompt-skill-rpc-v2/01-execute.json', 'utf8'))
const setPath = (value, path, replacement) => {
  let target = value
  for (const part of path.slice(0, -1)) target = target[part]
  target[path[path.length - 1]] = structuredClone(replacement)
}
const rejected = action => {
  try { action(); return false } catch (_) { return true }
}
const observed = {}
for (const item of corpus.negative) {
  let action
  if (item.mutation.op === 'set-both') {
    const request = structuredClone(golden.request)
    const result = structuredClone(golden.result)
    setPath(request, item.mutation.request_path, item.mutation.value)
    setPath(result, item.mutation.result_path, item.mutation.value)
    action = () => rpc.validatePromptSkillExecuteV2(request, result, item.mutation.expected_lease_epoch)
  } else if (item.fixture === 'execute.request') {
    const request = structuredClone(golden.request)
    setPath(request, item.mutation.path, item.mutation.value)
    action = () => rpc.parsePromptSkillExecuteRequestV2(request)
  } else if (item.fixture === 'execute.result') {
    const result = structuredClone(golden.result)
    setPath(result, item.mutation.path, item.mutation.value)
    action = () => rpc.validatePromptSkillExecuteV2(golden.request, result)
  } else if (item.fixture === 'execute.success') {
    const success = structuredClone(golden.success)
    setPath(success, item.mutation.path, item.mutation.value)
    action = () => rpc.validatePromptSkillExecuteV2(golden.request, success)
  } else throw new Error(`unknown fixture ${item.fixture}`)
  observed[item.case_id] = rejected(action)
}
rpc.parsePromptSkillExecuteRequestV2(golden.request)
rpc.parsePromptSkillExecuteResultV2(golden.result)
rpc.validateRpcMethodSuccessV2(golden.success, golden.request)
rpc.validatePromptSkillExecuteV2(golden.request, golden.success, 1)
console.log(JSON.stringify({ observed, operation_key_length: (await rpc.promptSkillOperationKeyV2(golden.request)).length }))
'''
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=os.environ.copy(),
        check=False,
    )
    assert completed.returncode == 0, f"TypeScript verifier failed\nstdout={completed.stdout}\nstderr={completed.stderr}"
    return json.loads(completed.stdout)


def test_typescript_cross_language_positive_and_negative(corpus: dict[str, Any]) -> None:
    result = _run_typescript_case(corpus)
    assert result["operation_key_length"] == 64
    assert all(result["observed"].values())
    assert len(result["observed"]) == corpus["manifest"]["negative_case_count"]
