from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools.integration import validate_merge_gate as merge_gate


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _commit(repo: Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", relative_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _matrix(repo: Path) -> dict[str, object]:
    write_sets: dict[str, list[str]] = {
        "P1": ["backend/plotpilot_core/domain/**"],
        "P2": ["backend/plotpilot_core/plugins/**", "first-party-plugins/prompt-skill-runtime/**"],
        "P3": ["backend/plotpilot_core/jobs/**", "first-party-plugins/provider-openai-compatible/**"],
        "P4": ["frontend/src/views/**"],
        "P5": ["first-party-plugins/project-planner/**", "first-party-plugins/story-state/**"],
        "P6": ["first-party-plugins/chapter-workflow/**"],
    }
    plugin_ids = {
        "P2": ["com.plotpilot.prompt-skill-runtime"],
        "P3": ["com.plotpilot.provider.openai-compatible"],
        "P5": ["com.plotpilot.project-planner", "com.plotpilot.story-state"],
        "P6": ["com.plotpilot.chapter-workflow"],
    }
    projects: list[dict[str, object]] = [
        {
            "id": "P0",
            "worktree": str(repo),
            "branch": "main",
            "write_sets": ["contracts/**", "backend/plotpilot_plugin_sdk/**"],
            "first_party_plugins": [],
        }
    ]
    for project_id, patterns in write_sets.items():
        projects.append(
            {
                "id": project_id,
                "worktree": str(repo / "worktrees" / project_id),
                "branch": f"codex/test-{project_id.lower()}",
                "write_sets": patterns,
                "first_party_plugins": plugin_ids.get(project_id, []),
            }
        )
    return {"schema": "plotpilot-seven-project-matrix/v1", "projects": projects}


def _fixture(repo: Path) -> tuple[dict[str, object], Path, str, str]:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "acceptance@example.invalid")
    _git(repo, "config", "user.name", "Acceptance")
    _commit(repo, "contracts/manifest-v1.json", '{"schema":"fixture-manifest"}\n', "bootstrap")
    base = _git(repo, "rev-parse", "HEAD")
    matrix = _matrix(repo)
    matrix_path = repo / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    evidence = repo / "evidence" / "pytest.txt"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("acceptance evidence\n", encoding="utf-8")
    manifest_hash = hashlib.sha256((repo / "contracts/manifest-v1.json").read_bytes()).hexdigest()
    return matrix, matrix_path, base, manifest_hash


def _submission(
    repo: Path,
    *,
    matrix: dict[str, object],
    base: str,
    head: str,
    changed_paths: list[str],
    manifest_hash: str,
) -> Path:
    evidence = repo / "evidence" / "pytest.txt"
    evidence_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
    value = {
        "schema": "integration-ready/v1",
        "status": "ready",
        "ready": True,
        "submission_id": "p1-acceptance",
        "project_id": "P1",
        "source_branch": "feature/p1",
        "base_sha": base,
        "head_sha": head,
        "contract_manifest_sha256": manifest_hash,
        "changed_paths": changed_paths,
        "write_set_proof": {
            "matrix_path": str(repo / "matrix.json"),
            "project_id": "P1",
            "project_write_set_match": True,
            "p0_write_set_match": True,
            "out_of_set_paths": [],
        },
        "contract_delta_ids": [],
        "dependency_delta_ids": [],
        "artifacts": [],
        "validation": [
            {
                "command": "pytest tests/acceptance/test_merge_gate_remediation.py -q",
                "exit_code": 0,
                "stdout": {
                    "path": str(evidence),
                    "bytes": evidence.stat().st_size,
                    "sha256": evidence_hash,
                },
            }
        ],
        "review": {
            "reviewer": "Sol Max",
            "finding_ids": [],
            "closure_artifact": {
                "path": str(evidence),
                "bytes": evidence.stat().st_size,
                "sha256": evidence_hash,
            },
        },
    }
    submission_path = repo / "submission.json"
    submission_path.write_text(json.dumps(value), encoding="utf-8")
    return submission_path


def test_p1_own_write_set_is_accepted_and_p0_path_is_rejected(tmp_path: Path) -> None:
    matrix, matrix_path, base, manifest_hash = _fixture(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature/p1")
    head = _commit(
        tmp_path,
        "backend/plotpilot_core/domain/owned.py",
        "# P1\n",
        "p1 implementation",
    )
    accepted = _submission(
        tmp_path,
        matrix=matrix,
        base=base,
        head=head,
        changed_paths=["backend/plotpilot_core/domain/owned.py"],
        manifest_hash=manifest_hash,
    )
    result = merge_gate._verify_submission(accepted, matrix=matrix, root=tmp_path)
    assert result["project_id"] == "P1"
    assert result["changed_paths"] == ["backend/plotpilot_core/domain/owned.py"]
    assert result["head_based_on_base"] is True
    accepted_copy = tmp_path / "accepted-submission.json"
    accepted_copy.write_bytes(accepted.read_bytes())

    # Use a second commit based on the same base so the P1 submission can only
    # be accepted if it applies P1's matrix write set, not P0's broad set.
    _git(tmp_path, "checkout", "-B", "feature/p1-bad", base)
    bad_head = _commit(tmp_path, "contracts/forbidden.txt", "no\n", "p1 touches p0")
    bad = _submission(
        tmp_path,
        matrix=matrix,
        base=base,
        head=bad_head,
        changed_paths=["contracts/forbidden.txt"],
        manifest_hash=manifest_hash,
    )
    with pytest.raises(AssertionError, match="outside its write set"):
        merge_gate._verify_submission(bad, matrix=matrix, root=tmp_path)
    assert matrix_path.is_file()

    proof_only = json.loads(accepted_copy.read_text(encoding="utf-8"))
    proof_only["write_set_proof"]["project_write_set_match"] = False
    accepted_copy.write_text(json.dumps(proof_only), encoding="utf-8")
    with pytest.raises(AssertionError, match="project write-set proof"):
        merge_gate._verify_submission(accepted_copy, matrix=matrix, root=tmp_path)


@pytest.mark.parametrize("field", ["base_sha", "contract_manifest_sha256", "changed_paths"])
def test_fictional_sha_hash_or_path_is_rejected(tmp_path: Path, field: str) -> None:
    matrix, _matrix_path, base, manifest_hash = _fixture(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature/p1")
    head = _commit(tmp_path, "backend/plotpilot_core/domain/owned.py", "# P1\n", "p1 implementation")
    submission_path = _submission(
        tmp_path,
        matrix=matrix,
        base=base,
        head=head,
        changed_paths=["backend/plotpilot_core/domain/owned.py"],
        manifest_hash=manifest_hash,
    )
    value = json.loads(submission_path.read_text(encoding="utf-8"))
    if field == "base_sha":
        value[field] = "0" * 40
    elif field == "contract_manifest_sha256":
        value[field] = "f" * 64
    else:
        value[field] = [
            "backend/plotpilot_core/domain/owned.py",
            "backend/plotpilot_core/domain/fictional.py",
        ]
    submission_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(AssertionError):
        merge_gate._verify_submission(submission_path, matrix=matrix, root=tmp_path)


def test_creation_gate_checks_actual_first_party_plugin_root(tmp_path: Path) -> None:
    matrix, matrix_path, _base, _manifest_hash = _fixture(tmp_path)
    gate = merge_gate.verify_creation_gate(matrix_path=matrix_path, root=tmp_path)
    assert gate["closed"] is True
    p2 = next(item for item in gate["projects"] if item["project_id"] == "P2")
    assert p2["first_party_plugin_roots"] == ["first-party-plugins/prompt-skill-runtime"]

    (tmp_path / "first-party-plugins" / "prompt-skill-runtime").mkdir(parents=True)
    with pytest.raises(AssertionError, match="P2 creation gate is open"):
        merge_gate.verify_creation_gate(matrix_path=matrix_path, root=tmp_path)
