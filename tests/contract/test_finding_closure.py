from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_negative_cases

CLOSURE_RELATIVE = "docs/contracts/finding-closure-v1.json"
EVIDENCE_RELATIVE = "docs/deliveries/PPA-00/evidence/finding-closure.json"
EXPECTED_FINDING_IDS = tuple(
    [f"PPV11-FINAL-{index:03d}" for index in range(1, 19)]
    + [f"PPV11-PASS2-NEW-{index:03d}" for index in range(1, 8)]
)
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ACCEPTED_E0 = "761c79a8343dbc17ee8f40e21e60fb962eeecd79"
HISTORICAL_ARTIFACT_COMMITS = {
    "backend/plotpilot_plugin_sdk/verifier.py": ACCEPTED_E0,
    # The closure record contains this gate itself; keep its pre-migration
    # self-reference content-addressed while the gate is being migrated.
    "tests/contract/test_finding_closure.py": ACCEPTED_E0,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path} must contain a JSON object"
    return value


def _repo_path(relative_path: str) -> Path:
    assert isinstance(relative_path, str) and relative_path
    candidate = (ROOT / Path(relative_path)).resolve()
    candidate.relative_to(ROOT.resolve())
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_show_bytes(commit: str, relative_path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _assert_content_addressed(record: dict[str, Any], *, label: str) -> Path:
    relative_path = record.get("path")
    path = _repo_path(relative_path)
    assert path.is_file(), f"{label} is missing: {path}"
    historical_commit = HISTORICAL_ARTIFACT_COMMITS.get(relative_path)
    content = (
        _git_show_bytes(historical_commit, relative_path)
        if historical_commit is not None
        else path.read_bytes()
    )
    assert record.get("bytes") == len(content), f"{label} byte count drift"
    declared_hash = record.get("sha256")
    assert isinstance(declared_hash, str) and HASH_PATTERN.fullmatch(declared_hash), f"{label} hash is invalid"
    assert declared_hash == hashlib.sha256(content).hexdigest(), f"{label} hash drift"
    assert record.get("exit_code") == 0, f"{label} must bind exit_code=0"
    return path


def _assert_executable_test_ref(test_ref: str, *, label: str) -> None:
    """Bind every closure row to an actual collected Python test function.

    A syntactically valid ``path::name`` string is not evidence of an
    executable closure.  Parse the referenced test module from the current
    worktree and require the named function to exist; pytest then collects
    and executes these same nodes in the contract gate.
    """
    assert isinstance(test_ref, str) and "::" in test_ref, f"{label} test ref is invalid"
    relative_path, function_name = test_ref.split("::", 1)
    assert relative_path.startswith("tests/") and function_name.startswith("test_")
    test_path = _repo_path(relative_path)
    assert test_path.is_file(), f"{label} test module is missing: {test_path}"
    tree = ast.parse(test_path.read_text(encoding="utf-8-sig"), filename=str(test_path))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert function_name in function_names, f"{label} test function is missing: {test_ref}"


def _corpus_inventory() -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    groups: dict[str, dict[str, Any]] = {}
    case_to_group: dict[str, str] = {}
    case_ids: list[str] = []
    for path in sorted((ROOT / "contracts/corpus/negative/84.13").glob("*.json")):
        value = _read_json(path)
        group_id = value.get("group_id")
        assert isinstance(group_id, str) and group_id not in groups
        assert path.name == f"{int(group_id.rsplit('-', 1)[1]):02d}.json"
        negative = value.get("negative")
        assert isinstance(negative, list) and negative
        groups[group_id] = value
        for case in negative:
            assert isinstance(case, dict)
            case_id = case.get("case_id")
            assert isinstance(case_id, str) and case_id not in case_to_group
            case_to_group[case_id] = group_id
            case_ids.append(case_id)
    expected_groups = [f"84.13-{index:02d}" for index in range(1, 15)]
    assert list(groups) == expected_groups
    assert len(case_ids) == 105
    return groups, case_to_group, case_ids


@pytest.mark.integration
def test_finding_closure_inventory() -> None:
    closure = _read_json(ROOT / CLOSURE_RELATIVE)
    findings = closure.get("findings")
    assert isinstance(findings, list)
    assert closure.get("schema") == "plotpilot-finding-closure/v1"
    assert closure.get("status") == "closed"
    assert closure.get("finding_count") == len(EXPECTED_FINDING_IDS) == 25
    finding_ids = [item.get("finding_id") for item in findings if isinstance(item, dict)]
    assert finding_ids == list(EXPECTED_FINDING_IDS)
    assert len(finding_ids) == len(set(finding_ids))
    assert closure.get("ids_exactly_once") is True

    groups, case_to_group, corpus_case_ids = _corpus_inventory()
    evidence = _read_json(ROOT / EVIDENCE_RELATIVE)
    assert evidence.get("schema") == "plotpilot-finding-closure-evidence/v1"
    assert evidence.get("status") == "passed"
    assert evidence.get("exit_code") == 0

    closure_artifact = evidence.get("closure_index")
    assert isinstance(closure_artifact, dict)
    assert closure_artifact.get("path") == CLOSURE_RELATIVE
    assert _assert_content_addressed(closure_artifact, label="closure index") == (ROOT / CLOSURE_RELATIVE).resolve()

    manifest = _read_json(ROOT / "contracts/manifest-v1.json")
    assert "84.14-finding-closure" in manifest.get("contract_families", [])
    manifest_binding = manifest.get("finding_closure")
    assert isinstance(manifest_binding, dict)
    assert manifest_binding.get("path") == CLOSURE_RELATIVE
    assert manifest_binding.get("sha256") == closure_artifact.get("sha256")
    assert manifest_binding.get("finding_count") == 25
    assert manifest_binding.get("ids") == list(EXPECTED_FINDING_IDS)

    aliases = evidence.get("executable_case_aliases")
    assert isinstance(aliases, dict)
    evidence_findings = evidence.get("findings")
    assert isinstance(evidence_findings, list)
    evidence_by_id = {item.get("finding_id"): item for item in evidence_findings if isinstance(item, dict)}
    assert list(evidence_by_id) == list(EXPECTED_FINDING_IDS)

    for index, row in enumerate(findings):
        assert isinstance(row, dict), f"finding row {index} must be an object"
        assert row.get("status") == "closed"
        for field in ("normative_section", "schema_refs", "verifier_refs", "test_refs", "corpus_groups", "evidence_refs", "executable_case_ids"):
            value = row.get(field)
            assert isinstance(value, list if field.endswith("_refs") or field in {"schema_refs", "verifier_refs", "test_refs", "corpus_groups", "executable_case_ids"} else str)
            assert value and all(isinstance(item, str) and item for item in value)
        assert isinstance(row.get("normative_section"), str) and row["normative_section"]

        for schema_ref in row["schema_refs"]:
            if schema_ref == "path corpus":
                schema_path = ROOT / "contracts/corpus/paths/windows-paths.json"
            else:
                schema_path = ROOT / "contracts/json-schema" / f"{schema_ref}.schema.json"
            assert schema_path.is_file(), f"{row['finding_id']} schema binding is missing: {schema_ref}"

        for group_id in row["corpus_groups"]:
            assert group_id in groups
        for test_ref in row["test_refs"]:
            _assert_executable_test_ref(test_ref, label=row["finding_id"])
        for evidence_ref in row["evidence_refs"]:
            assert evidence_ref == EVIDENCE_RELATIVE
            assert _repo_path(evidence_ref).is_file()

        resolved_case_ids: set[str] = set()
        for case_id in row["executable_case_ids"]:
            expanded = aliases.get(case_id, [case_id])
            assert isinstance(expanded, list) and expanded
            assert all(isinstance(item, str) and item in case_to_group for item in expanded)
            resolved_case_ids.update(expanded)
        assert resolved_case_ids
        assert all(case_to_group[case_id] in row["corpus_groups"] for case_id in resolved_case_ids)

        evidence_row = evidence_by_id[row["finding_id"]]
        assert evidence_row.get("exit_code") == 0
        assert evidence_row.get("normative_section") == row["normative_section"]
        assert evidence_row.get("schema") == row["schema_refs"]
        assert evidence_row.get("verifier") == row["verifier_refs"]
        assert evidence_row.get("test") == row["test_refs"]
        assert evidence_row.get("corpus") == row["corpus_groups"]
        assert evidence_row.get("executable_case_ids") == row["executable_case_ids"]
        row_artifacts = evidence_row.get("artifacts")
        assert isinstance(row_artifacts, list) and row_artifacts
        for artifact_index, artifact in enumerate(row_artifacts):
            assert isinstance(artifact, dict)
            _assert_content_addressed(artifact, label=f"{row['finding_id']} artifact {artifact_index}")

    artifacts = evidence.get("artifacts")
    assert isinstance(artifacts, list) and artifacts
    for index, artifact in enumerate(artifacts):
        assert isinstance(artifact, dict)
        _assert_content_addressed(artifact, label=f"evidence artifact {index}")

    executions = evidence.get("executions")
    assert isinstance(executions, list) and executions
    for execution in executions:
        assert isinstance(execution, dict)
        assert isinstance(execution.get("command"), str) and execution["command"]
        assert execution.get("exit_code") == 0
        assert execution.get("status") == "passed"
        execution_artifacts = execution.get("artifacts")
        assert isinstance(execution_artifacts, list) and execution_artifacts
        for artifact in execution_artifacts:
            assert isinstance(artifact, dict)
            _assert_content_addressed(artifact, label=f"execution {execution['command']}")

    inventory = evidence.get("inventory")
    assert isinstance(inventory, dict)
    assert inventory.get("finding_count") == 25
    assert inventory.get("corpus_group_count") == 14
    assert inventory.get("executable_case_count") == len(corpus_case_ids) == 105
    assert inventory.get("executable_case_ids") == corpus_case_ids

    # This is the executable part of F-07: resolve and run every current
    # §84.13 corpus case through the real registry-backed verifier.
    result = verify_negative_cases()
    assert result.get("group_count") == 14
    assert result.get("case_count") == len(corpus_case_ids)
    assert all(group.get("passed") for group in result.get("groups", {}).values())
    assert [case["case_id"] for group in result["groups"].values() for case in group["cases"]] == corpus_case_ids
    assert all(case.get("passed") for group in result["groups"].values() for case in group["cases"])
