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
from plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs  # noqa: E402
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import (  # noqa: E402
    PromptSkillOperationLedgerV2,
    parse_prompt_skill_execute_request_v2,
    parse_prompt_skill_execute_result_v2,
    parse_prompt_skill_rpc_envelope_v2,
    parse_rpc_error_v2,
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
    group_paths = sorted(path for path in CORPUS_ROOT.glob("*.json") if path.name != "manifest.json")
    groups = [read_json(path) for path in group_paths]
    assert manifest["group_count"] == len(groups) == 1
    assert manifest["negative_case_count"] == sum(len(group["negative"]) for group in groups)
    return {"manifest": manifest, "groups": groups}


def _set_path(value: dict[str, Any], path: list[Any], replacement: Any) -> dict[str, Any]:
    target: Any = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = copy.deepcopy(replacement)
    return value


def _authority(request: dict[str, Any]) -> dict[str, Any]:
    return {field: request["params"][field] for field in ("workspace_id", "generation_id", "plugin_id", "plugin_release_id", "job_id", "step_id", "attempt_id", "lease_epoch", "operation_key")}


def test_v2_method_matrix_and_schemas_are_closed() -> None:
    matrix = read_json(SCHEMA_ROOT / "rpc-method-matrix.v2.json")
    assert matrix["schema"] == "rpc-method-matrix/v2"
    assert matrix["authority"] == "core"
    assert matrix["plugin_authority_allowed"] is False
    assert matrix["envelope"] == {"exactly_one": True, "members": ["request", "success", "error"]}
    method = matrix["methods"]
    assert len(method) == 1
    assert method[0]["method"] == "prompt.skill.execute/v2"
    assert method[0]["direction"] == "host-to-worker"
    assert method[0]["endpoint"] == "worker"
    assert method[0]["meta_profile"] == "attempt"
    assert method[0]["error_schema"] == "rpc-error-v1"
    assert method[0]["error_code_registry"] == "rpc-method-matrix/v1#error_codes"
    assert method[0]["secrets"] == {"location": "params", "mode": "one-shot", "response_forbidden": True}
    assert method[0]["job_mapping"]["start"]["method"] == "job.start"
    assert method[0]["job_mapping"]["resume"]["method"] == "job.resume"

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


def test_python_positive_terminal_identity_and_authority(golden: dict[str, Any]) -> None:
    request = parse_prompt_skill_execute_request_v2(golden["request"])
    result = parse_prompt_skill_execute_result_v2(golden["result"])
    assert request["method"] == "prompt.skill.execute/v2"
    assert result["status"] == "succeeded"
    assert validate_rpc_success_v2(golden["success"], request=request) == result
    assert parse_prompt_skill_rpc_envelope_v2(golden["request"]) == request
    assert parse_prompt_skill_rpc_envelope_v2(golden["success"], request=request) == result
    assert parse_prompt_skill_rpc_envelope_v2(golden["error"], request=request) == golden["error"]
    for status in ("failed", "cancelled", "uncertain"):
        assert parse_prompt_skill_execute_result_v2(golden[status])["status"] == status

    assert validate_prompt_skill_execute_v2(request, golden["success"]) == result
    rebuilt = parse_prompt_skill_execute_request_v2(golden["request"])
    assert rebuilt == request
    assert prompt_skill_operation_key(request) == golden["operation_digest"]
    operation_bytes = canonical_bytes(request["params"])
    assert operation_bytes.hex() == golden["operation_canonical_bytes_hex"]
    assert hash_jcs("prompt-skill-execute/v2", request["params"]) == golden["operation_digest"]
    permuted = copy.deepcopy(request)
    permuted["params"]["skill_releases"] = list(reversed(permuted["params"]["skill_releases"]))
    assert canonical_bytes(permuted["params"]) != operation_bytes
    assert prompt_skill_operation_key(permuted) != golden["operation_digest"]

    ledger = PromptSkillOperationLedgerV2()
    first, replayed = ledger.record(request, result, authoritative_context=_authority(request))
    assert first == result and replayed is False
    assert ledger.replay(request, authoritative_context=_authority(request)) == result
    with pytest.raises((ContractError, TypeError)):
        ledger.replay(request)


def test_packaged_resources_are_exact_and_frozen_v1_sources() -> None:
    sources = {
        "unicode-casefold-v1.json": ROOT / "contracts" / "unicode-casefold-v1.json",
        "rpc-method-matrix.v1.json": SCHEMA_ROOT / "rpc-method-matrix.v1.json",
        "rpc-method-matrix.v2.json": SCHEMA_ROOT / "rpc-method-matrix.v2.json",
        "rpc-error-v1.schema.json": SCHEMA_ROOT / "rpc-error-v1.schema.json",
        "prompt-skill-execute-request-v2.schema.json": SCHEMA_ROOT / "prompt-skill-execute-request-v2.schema.json",
        "prompt-skill-execute-result-v2.schema.json": SCHEMA_ROOT / "prompt-skill-execute-result-v2.schema.json",
        "rpc-method-success-v2.schema.json": SCHEMA_ROOT / "rpc-method-success-v2.schema.json",
    }
    resource_dir = ROOT / "backend" / "plotpilot_plugin_sdk" / "resources"
    assert not (resource_dir / "__init__.py").exists()
    for name, source in sources.items():
        assert (resource_dir / name).read_bytes() == source.read_bytes()


def test_authority_validation_precedes_ledger_mutation(golden: dict[str, Any]) -> None:
    request = golden["request"]
    result = golden["result"]
    authority = _authority(request)
    for field in authority:
        ledger = PromptSkillOperationLedgerV2()
        missing = dict(authority)
        del missing[field]
        with pytest.raises((ContractError, TypeError, KeyError)):
            ledger.record(request, result, authoritative_context=missing)
        _, replayed = ledger.record(request, result, authoritative_context=authority)
        assert replayed is False, field

    additional_ledger = PromptSkillOperationLedgerV2()
    additional = dict(authority)
    additional["unexpected_authority"] = True
    with pytest.raises(ContractError):
        additional_ledger.record(request, result, authoritative_context=additional)
    _, replayed = additional_ledger.record(request, result, authoritative_context=authority)
    assert replayed is False

    partial_ledger = PromptSkillOperationLedgerV2()
    with pytest.raises(ContractError):
        partial_ledger.record(request, result, authoritative_context={"lease_epoch": authority["lease_epoch"]})
    _, replayed = partial_ledger.record(request, result, authoritative_context=authority)
    assert replayed is False

    future_request = _set_path(copy.deepcopy(request), ["params", "lease_epoch"], authority["lease_epoch"] + 1)
    _set_path(future_request, ["meta", "lease_epoch"], authority["lease_epoch"] + 1)
    future_result = _set_path(copy.deepcopy(result), ["lease_epoch"], authority["lease_epoch"] + 1)
    future_ledger = PromptSkillOperationLedgerV2()
    with pytest.raises(ContractError):
        future_ledger.record(future_request, future_result, authoritative_context=authority)
    _, replayed = future_ledger.record(request, result, authoritative_context=authority)
    assert replayed is False


def test_unicode_scalar_schema_and_python_parity(golden: dict[str, Any]) -> None:
    request_schema = read_json(SCHEMA_ROOT / "prompt-skill-execute-request-v2.schema.json")
    result_schema = read_json(SCHEMA_ROOT / "prompt-skill-execute-result-v2.schema.json")
    success_schema = read_json(SCHEMA_ROOT / "rpc-method-success-v2.schema.json")
    request_validator = Draft202012Validator(request_schema)
    result_validator = Draft202012Validator(result_schema)
    success_validator = Draft202012Validator(success_schema)

    valid_request = copy.deepcopy(golden["request"])
    valid_request["params"]["secrets"][0]["value"] = "astral-😀"
    assert request_validator.is_valid(valid_request)
    parse_prompt_skill_execute_request_v2(valid_request)

    valid_result = copy.deepcopy(golden["result"])
    valid_result["warnings"] = [{"code": "prompt-skill.warning", "message": "astral-😀"}]
    assert result_validator.is_valid(valid_result)
    parse_prompt_skill_execute_result_v2(valid_result)

    valid_success = copy.deepcopy(golden["success"])
    valid_success["result"]["warnings"] = [{"code": "prompt-skill.warning", "message": "astral-😀"}]
    assert success_validator.is_valid(valid_success)
    validate_rpc_success_v2(valid_success, request=golden["request"])

    invalid_request = copy.deepcopy(golden["request"])
    invalid_request["params"]["secrets"][0]["value"] = "lone-\ud800"
    assert not request_validator.is_valid(invalid_request)
    with pytest.raises((ContractError, ValueError, UnicodeError)):
        parse_prompt_skill_execute_request_v2(invalid_request)

    invalid_result = copy.deepcopy(golden["result"])
    invalid_result["warnings"] = [{"code": "prompt-skill.warning", "message": "lone-\ud800"}]
    assert not result_validator.is_valid(invalid_result)
    with pytest.raises((ContractError, ValueError, UnicodeError)):
        parse_prompt_skill_execute_result_v2(invalid_result)

    invalid_success = copy.deepcopy(golden["success"])
    invalid_success["result"]["warnings"] = [{"code": "prompt-skill.warning", "message": "lone-\ud800"}]
    assert not success_validator.is_valid(invalid_success)
    with pytest.raises((ContractError, ValueError, UnicodeError)):
        validate_rpc_success_v2(invalid_success, request=golden["request"])

    invalid_error = copy.deepcopy(golden["error"])
    invalid_error["error"]["message"] = "lone-\ud800"
    with pytest.raises((ContractError, ValueError, UnicodeError)):
        parse_rpc_error_v2(invalid_error)


def _python_case_action(case: dict[str, Any], golden: dict[str, Any]) -> Callable[[], Any]:
    fixture = case["fixture"]
    mutation = case["mutation"]
    request = golden["request"]
    result = golden["result"]
    if fixture == "execute.ledger":
        operation = mutation["op"]

        def ledger_action() -> Any:
            if operation == "no-authority":
                return PromptSkillOperationLedgerV2().replay(request)
            if operation == "stale-replay":
                ledger = PromptSkillOperationLedgerV2()
                auth = _authority(request)
                ledger.record(request, result, authoritative_context=auth)
                stale = dict(auth)
                stale["attempt_id"] = "attempt-other"
                return ledger.replay(request, authoritative_context=stale)
            if operation == "future-poison" or operation == "future-epoch-rejected-before-mutation":
                future_request = _set_path(copy.deepcopy(request), ["params", "lease_epoch"], 99)
                _set_path(future_request, ["meta", "lease_epoch"], 99)
                future_result = _set_path(copy.deepcopy(result), ["lease_epoch"], 99)
                ledger = PromptSkillOperationLedgerV2()
                return ledger.record(future_request, future_result, authoritative_context=_authority(request))
            if operation == "authority-missing":
                auth = _authority(request)
                del auth[mutation["field"]]
                return PromptSkillOperationLedgerV2().record(request, result, authoritative_context=auth)
            if operation == "authority-additional":
                auth = _authority(request)
                auth[mutation["field"]] = copy.deepcopy(mutation["value"])
                return PromptSkillOperationLedgerV2().record(request, result, authoritative_context=auth)
            if operation == "authority-partial":
                auth = {"lease_epoch": request["params"]["lease_epoch"]}
                return PromptSkillOperationLedgerV2().record(request, result, authoritative_context=auth)
            raise AssertionError(f"unknown Prompt-Skill ledger operation {operation}")

        return ledger_action

    fixture_value = {
        "execute.request": golden["request"],
        "execute.result": golden["result"],
        "execute.success": golden["success"],
        "execute.failed": golden["failed"],
        "execute.cancelled": golden["cancelled"],
        "execute.uncertain": golden["uncertain"],
        "execute.error": golden["error"],
        "execute.envelope": golden["request"],
    }[fixture]
    mutated = _set_path(copy.deepcopy(fixture_value), mutation["path"], mutation["value"])
    if fixture == "execute.request":
        return lambda: parse_prompt_skill_execute_request_v2(mutated)
    if fixture == "execute.result":
        return lambda: validate_prompt_skill_execute_v2(request, mutated)
    if fixture in {"execute.failed", "execute.cancelled", "execute.uncertain"}:
        return lambda: parse_prompt_skill_execute_result_v2(mutated)
    if fixture == "execute.success":
        return lambda: validate_prompt_skill_execute_v2(request, mutated)
    if fixture == "execute.error":
        return lambda: parse_rpc_error_v2(mutated, request=request)
    if fixture == "execute.envelope":
        return lambda: parse_prompt_skill_rpc_envelope_v2(mutated)
    raise AssertionError(f"unknown Prompt-Skill fixture {fixture}")


def test_python_negative_corpus_is_fail_closed(golden: dict[str, Any], corpus: dict[str, Any]) -> None:
    observed: set[str] = set()
    for group in corpus["groups"]:
        for case in group["negative"]:
            with pytest.raises((ContractError, ValueError, KeyError, TypeError, AssertionError)):
                _python_case_action(case, golden)()
            observed.add(case["case_id"])
    assert len(observed) == corpus["manifest"]["negative_case_count"] > 25


def test_python_golden_and_corpus_determinism(corpus: dict[str, Any]) -> None:
    expected = read_json(GOLDEN_ROOT / "expected.json")
    golden = read_json(GOLDEN_ROOT / "execute.json")
    for name, digest in expected["fixture_files"].items():
        assert hashlib.sha256((GOLDEN_ROOT / name).read_bytes()).hexdigest() == digest
    cases = [case for group in corpus["groups"] for case in group["negative"]]
    assert len({case["case_id"] for case in cases}) == len(cases) == corpus["manifest"]["negative_case_count"]
    assert expected["operation_canonical_bytes_hex"] == golden["operation_canonical_bytes_hex"]
    assert expected["terminal_statuses"] == ["succeeded", "failed", "cancelled", "uncertain"]


def _run_typescript_cases() -> dict[str, Any]:
    script = r'''
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const rpc = await import(pathToFileURL('frontend/src/contracts/prompt-skill-rpc-v2.ts').href)
const canonical = await import(pathToFileURL('frontend/src/contracts/canonical.ts').href)
const golden = JSON.parse(readFileSync('contracts/golden/prompt-skill-rpc-v2/execute.json', 'utf8'))
const corpus = JSON.parse(readFileSync('contracts/corpus/prompt-skill-rpc-v2/01-execute.json', 'utf8'))
const setPath = (value, path, replacement) => { let target = value; for (const part of path.slice(0, -1)) target = target[part]; target[path.at(-1)] = structuredClone(replacement); return value }
const authority = request => Object.fromEntries(['workspace_id', 'generation_id', 'plugin_id', 'plugin_release_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'operation_key'].map(field => [field, request.params[field]]))
const rejected = async action => { try { await action(); return false } catch (_) { return true } }
const fixtures = { 'execute.request': golden.request, 'execute.result': golden.result, 'execute.success': golden.success, 'execute.failed': golden.failed, 'execute.cancelled': golden.cancelled, 'execute.uncertain': golden.uncertain, 'execute.error': golden.error, 'execute.envelope': golden.request }
const observed = {}
for (const item of corpus.negative) {
  let action
  if (item.fixture === 'execute.ledger') {
    action = () => {
      if (item.mutation.op === 'no-authority') return new rpc.PromptSkillOperationLedgerV2().replay(golden.request)
      if (item.mutation.op === 'stale-replay') { const ledger = new rpc.PromptSkillOperationLedgerV2(); const auth = authority(golden.request); ledger.record(golden.request, golden.result, auth); const stale = { ...auth, attempt_id: 'attempt-other' }; return ledger.replay(golden.request, stale) }
      if (item.mutation.op === 'future-poison' || item.mutation.op === 'future-epoch-rejected-before-mutation') { const futureRequest = setPath(structuredClone(golden.request), ['params', 'lease_epoch'], 99); setPath(futureRequest, ['meta', 'lease_epoch'], 99); const futureResult = setPath(structuredClone(golden.result), ['lease_epoch'], 99); const ledger = new rpc.PromptSkillOperationLedgerV2(); return ledger.record(futureRequest, futureResult, authority(golden.request)) }
      if (item.mutation.op === 'authority-missing') { const context = authority(golden.request); delete context[item.mutation.field]; return new rpc.PromptSkillOperationLedgerV2().record(golden.request, golden.result, context) }
      if (item.mutation.op === 'authority-additional') { const context = authority(golden.request); context[item.mutation.field] = structuredClone(item.mutation.value); return new rpc.PromptSkillOperationLedgerV2().record(golden.request, golden.result, context) }
      if (item.mutation.op === 'authority-partial') return new rpc.PromptSkillOperationLedgerV2().record(golden.request, golden.result, { lease_epoch: golden.request.params.lease_epoch })
      throw new Error('unknown ledger operation')
    }
  } else {
    const value = setPath(structuredClone(fixtures[item.fixture]), item.mutation.path, item.mutation.value)
    if (item.fixture === 'execute.request') action = () => rpc.parsePromptSkillExecuteRequestV2(value)
    else if (item.fixture === 'execute.result') action = () => rpc.validatePromptSkillExecuteV2(golden.request, value)
    else if (['execute.failed', 'execute.cancelled', 'execute.uncertain'].includes(item.fixture)) action = () => rpc.parsePromptSkillExecuteResultV2(value)
    else if (item.fixture === 'execute.success') action = () => rpc.validatePromptSkillExecuteV2(golden.request, value)
    else if (item.fixture === 'execute.error') action = () => rpc.parsePromptSkillErrorV2(value, golden.request)
    else if (item.fixture === 'execute.envelope') action = () => rpc.parsePromptSkillRpcEnvelopeV2(value)
    else throw new Error(`unknown fixture ${item.fixture}`)
  }
  observed[item.case_id] = await rejected(action)
}
rpc.parsePromptSkillExecuteRequestV2(golden.request)
rpc.parsePromptSkillExecuteResultV2(golden.result)
rpc.parsePromptSkillExecuteResultV2(golden.failed)
rpc.parsePromptSkillExecuteResultV2(golden.cancelled)
rpc.parsePromptSkillExecuteResultV2(golden.uncertain)
rpc.validateRpcMethodSuccessV2(golden.success, golden.request)
rpc.parsePromptSkillErrorV2(golden.error)
const authorityContext = authority(golden.request)
for (const field of Object.keys(authorityContext)) {
  const ledger = new rpc.PromptSkillOperationLedgerV2()
  const missing = { ...authorityContext }
  delete missing[field]
  let rejectedMissing = false
  try { ledger.record(golden.request, golden.result, missing) } catch (_) { rejectedMissing = true }
  if (!rejectedMissing) throw new Error(`missing authority accepted: ${field}`)
  if (ledger.record(golden.request, golden.result, authorityContext).replayed) throw new Error(`missing authority mutated ledger: ${field}`)
}
const additionalLedger = new rpc.PromptSkillOperationLedgerV2()
let rejectedAdditional = false
try { additionalLedger.record(golden.request, golden.result, { ...authorityContext, unexpected_authority: true }) } catch (_) { rejectedAdditional = true }
if (!rejectedAdditional) throw new Error('additional authority accepted')
if (additionalLedger.record(golden.request, golden.result, authorityContext).replayed) throw new Error('additional authority mutated ledger')
const partialLedger = new rpc.PromptSkillOperationLedgerV2()
let rejectedPartial = false
try { partialLedger.record(golden.request, golden.result, { lease_epoch: authorityContext.lease_epoch }) } catch (_) { rejectedPartial = true }
if (!rejectedPartial) throw new Error('partial authority accepted')
if (partialLedger.record(golden.request, golden.result, authorityContext).replayed) throw new Error('partial authority mutated ledger')
const futureRequest = setPath(structuredClone(golden.request), ['params', 'lease_epoch'], authorityContext.lease_epoch + 1)
setPath(futureRequest, ['meta', 'lease_epoch'], authorityContext.lease_epoch + 1)
const futureResult = setPath(structuredClone(golden.result), ['lease_epoch'], authorityContext.lease_epoch + 1)
const futureLedger = new rpc.PromptSkillOperationLedgerV2()
let rejectedFuture = false
try { futureLedger.record(futureRequest, futureResult, authorityContext) } catch (_) { rejectedFuture = true }
if (!rejectedFuture) throw new Error('future lease epoch accepted')
if (futureLedger.record(golden.request, golden.result, authorityContext).replayed) throw new Error('future epoch mutated ledger')
const validRequest = structuredClone(golden.request)
validRequest.params.secrets[0].value = 'astral-😀'
rpc.parsePromptSkillExecuteRequestV2(validRequest)
const validResult = structuredClone(golden.result)
validResult.warnings = [{ code: 'prompt-skill.warning', message: 'astral-😀' }]
rpc.parsePromptSkillExecuteResultV2(validResult)
const validSuccess = structuredClone(golden.success)
validSuccess.result.warnings = [{ code: 'prompt-skill.warning', message: 'astral-😀' }]
rpc.validateRpcMethodSuccessV2(validSuccess, golden.request)
const rejectUnicode = async action => { try { await action(); return false } catch (_) { return true } }
const invalidRequest = structuredClone(golden.request)
invalidRequest.params.secrets[0].value = 'lone-\ud800'
const invalidResult = structuredClone(golden.result)
invalidResult.warnings = [{ code: 'prompt-skill.warning', message: 'lone-\ud800' }]
const invalidSuccess = structuredClone(golden.success)
invalidSuccess.result.warnings = [{ code: 'prompt-skill.warning', message: 'lone-\ud800' }]
const invalidError = structuredClone(golden.error)
invalidError.error.message = 'lone-\ud800'
const unicode = {
  valid_request: !(await rejectUnicode(() => rpc.parsePromptSkillExecuteRequestV2(validRequest))),
  valid_result: !(await rejectUnicode(() => rpc.parsePromptSkillExecuteResultV2(validResult))),
  valid_success: !(await rejectUnicode(() => rpc.validateRpcMethodSuccessV2(validSuccess, golden.request))),
  invalid_request: await rejectUnicode(() => rpc.parsePromptSkillExecuteRequestV2(invalidRequest)),
  invalid_result: await rejectUnicode(() => rpc.parsePromptSkillExecuteResultV2(invalidResult)),
  invalid_success: await rejectUnicode(() => rpc.validateRpcMethodSuccessV2(invalidSuccess, golden.request)),
  invalid_error: await rejectUnicode(() => rpc.parsePromptSkillErrorV2(invalidError)),
}
const operationBytesHex = Buffer.from(canonical.utf8(canonical.canonicalJson(golden.request.params))).toString('hex')
const permuted = structuredClone(golden.request)
permuted.params.skill_releases.reverse()
const permutedBytesHex = Buffer.from(canonical.utf8(canonical.canonicalJson(permuted.params))).toString('hex')
const operationDigest = await rpc.promptSkillOperationKeyV2(golden.request)
const permutedDigest = await rpc.promptSkillOperationKeyV2(permuted)
console.log(JSON.stringify({ observed, operation_digest: operationDigest, operation_bytes_hex: operationBytesHex, permuted_bytes_changed: permutedBytesHex !== operationBytesHex, permuted_digest_changed: permutedDigest !== operationDigest, unicode }))
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


def test_typescript_cross_language_positive_and_negative(corpus: dict[str, Any], golden: dict[str, Any]) -> None:
    result = _run_typescript_cases()
    assert result["operation_digest"] == golden["operation_digest"]
    assert all(result["observed"].values())
    assert len(result["observed"]) == corpus["manifest"]["negative_case_count"]
    assert result["operation_bytes_hex"] == golden["operation_canonical_bytes_hex"]
    assert result["permuted_bytes_changed"] is True
    assert result["permuted_digest_changed"] is True
    assert result["unicode"] == {
        "valid_request": True,
        "valid_result": True,
        "valid_success": True,
        "invalid_request": True,
        "invalid_result": True,
        "invalid_success": True,
        "invalid_error": True,
    }
