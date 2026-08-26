"""Validate the P0 integration queue and downstream submissions.

The validator is deliberately independent of the product runtime.  It reads
the external project matrix, checks the current P0 worktree write set, keeps
the P1--P6 creation gate closed, and validates an ``integration-ready/v1``
submission against the actual Git objects and the current contract manifest.
No merge or other repository mutation is performed.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = Path(
    r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\project-matrix.json"
)
BASE_SHA = "1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0"
P0_BRANCH = "codex/ppa-00-integration"
DOWNSTREAM_PROJECT_IDS = tuple(f"P{index}" for index in range(1, 7))
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")


def _run_git_at(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def _git_at(root: Path, *args: str) -> str:
    completed = _run_git_at(root, *args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git command failed"
        raise AssertionError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.rstrip("\r\n")


def _git(*args: str) -> str:
    return _git_at(ROOT, *args)


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is forbidden: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"evidence file is missing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_path(value: str) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"path must be a string: {value!r}")
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if (
        not path
        or path.startswith("/")
        or path.startswith("//")
        or WINDOWS_ABSOLUTE.match(path)
        or "\x00" in path
    ):
        raise AssertionError(f"invalid repository path: {value!r}")
    components = path.split("/")
    if any(component == ".." for component in components):
        raise AssertionError(f"path escapes repository: {value!r}")
    normalised = str(PurePosixPath(path))
    if normalised in {"", ".", ".."} or normalised.startswith("../"):
        raise AssertionError(f"invalid repository path: {value!r}")
    return normalised


def _pattern_matches(path: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if "/" not in pattern:
        return "/" not in path and fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path, pattern)


def _matrix_projects(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    projects = matrix.get("projects")
    if not isinstance(projects, list):
        raise AssertionError("project matrix projects must be a list")
    result: list[dict[str, Any]] = []
    for project in projects:
        if not isinstance(project, dict):
            raise AssertionError("project matrix contains a non-object project")
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise AssertionError("project matrix project id is invalid")
        result.append(project)
    ids = [project["id"] for project in result]
    if len(ids) != len(set(ids)):
        raise AssertionError("project matrix contains duplicate project ids")
    return result


def _p0_write_set(matrix: dict[str, Any]) -> list[str]:
    matches = [project for project in _matrix_projects(matrix) if project.get("id") == "P0"]
    if len(matches) != 1:
        raise AssertionError("project matrix must contain exactly one P0 project")
    values = matches[0].get("write_sets")
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
        raise AssertionError("project matrix has no valid P0 write set")
    return values


def _downstream_project(matrix: dict[str, Any], project_id: str) -> dict[str, Any]:
    if project_id not in DOWNSTREAM_PROJECT_IDS:
        raise AssertionError(f"submission project_id must be one of P1-P6: {project_id!r}")
    matches = [project for project in _matrix_projects(matrix) if project.get("id") == project_id]
    if len(matches) != 1:
        raise AssertionError(f"project matrix has no unique {project_id} project")
    values = matches[0].get("write_sets")
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
        raise AssertionError(f"{project_id} has no valid write set")
    return matches[0]


def _resolve_matrix_path(value: Any, root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise AssertionError("project worktree must be a non-empty path")
    path = Path(value)
    return path if path.is_absolute() else root / path


def _first_party_plugin_roots(project: dict[str, Any]) -> list[str]:
    """Return real plugin roots projected from ``first-party-plugins/<dir>``.

    ``first_party_plugins`` contains package IDs (for example
    ``com.plotpilot.prompt-skill-runtime``), not filesystem paths.  The
    matrix write set is the authority for the actual root that must not exist
    before the creation gate opens.
    """

    values = project.get("write_sets")
    if not isinstance(values, list):
        raise AssertionError(f"{project.get('id')} write_sets must be a list")
    roots: set[str] = set()
    prefix = "first-party-plugins/"
    for value in values:
        if not isinstance(value, str):
            raise AssertionError(f"{project.get('id')} write set contains a non-string")
        pattern = value.replace("\\", "/")
        if not pattern.startswith(prefix):
            continue
        suffix = pattern[len(prefix) :]
        directory = suffix.split("/", 1)[0]
        if not directory or any(char in directory for char in "*?[]"):
            raise AssertionError(f"invalid first-party plugin root pattern: {value!r}")
        roots.add(_normalise_path(prefix + directory))
    return sorted(roots)


def _branch_exists(root: Path, branch: str) -> bool:
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise AssertionError("project branch must be a non-empty Git branch name")
    completed = _run_git_at(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    detail = completed.stderr.strip() or "unable to inspect branch"
    raise AssertionError(f"cannot inspect project branch {branch!r}: {detail}")


def _verify_creation_gate(matrix: dict[str, Any], *, root: Path | None = None) -> list[dict[str, Any]]:
    """Fail closed if any P1-P6 worktree, branch, or plugin root exists."""

    active_root = ROOT if root is None else Path(root)
    repo_check = _run_git_at(active_root, "rev-parse", "--git-dir")
    if repo_check.returncode != 0:
        detail = repo_check.stderr.strip() or "not a Git repository"
        raise AssertionError(f"cannot inspect creation gate repository: {detail}")

    projects = _matrix_projects(matrix)
    downstream = [project for project in projects if project.get("id") != "P0"]
    ids = [str(project.get("id")) for project in downstream]
    if set(ids) != set(DOWNSTREAM_PROJECT_IDS) or len(ids) != len(DOWNSTREAM_PROJECT_IDS):
        raise AssertionError(f"project matrix must contain exactly P1-P6 for creation gate, found {ids}")

    absent: list[dict[str, Any]] = []
    for project in sorted(downstream, key=lambda item: str(item["id"])):
        project_id = str(project["id"])
        worktree = _resolve_matrix_path(project.get("worktree"), active_root)
        branch = project.get("branch")
        if not isinstance(branch, str) or not branch:
            raise AssertionError(f"{project_id} branch is invalid")
        worktree_exists = worktree.exists()
        branch_exists = _branch_exists(active_root, branch)
        plugin_roots = _first_party_plugin_roots(project)
        existing_plugin_roots = [
            plugin_root for plugin_root in plugin_roots if (active_root / plugin_root).exists()
        ]
        package_ids_value = project.get("first_party_plugins", [])
        if not isinstance(package_ids_value, list) or not all(
            isinstance(item, str) for item in package_ids_value
        ):
            raise AssertionError(f"{project_id} first_party_plugins must be a string list")
        package_ids = list(package_ids_value)
        if worktree_exists or branch_exists or existing_plugin_roots:
            raise AssertionError(
                f"{project_id} creation gate is open: worktree={worktree_exists}, "
                f"branch={branch_exists}, first_party_plugin_roots={existing_plugin_roots}"
            )
        absent.append(
            {
                "project_id": project_id,
                "worktree": str(worktree),
                "branch": branch,
                "worktree_exists": False,
                "branch_exists": False,
                # Keep the package-ID field for callers of the old CLI while
                # exposing the filesystem roots used by the real check.
                "first_party_plugins": package_ids,
                "first_party_plugin_roots": plugin_roots,
                "first_party_plugin_roots_exists": [],
            }
        )
    return absent


def verify_creation_gate(
    *, matrix_path: Path = DEFAULT_MATRIX, root: Path | None = None
) -> dict[str, Any]:
    """Read and evaluate the real P1-P6 creation gate."""

    matrix = _read_json(Path(matrix_path))
    if matrix.get("schema") != "plotpilot-seven-project-matrix/v1":
        raise AssertionError("unexpected project matrix schema")
    projects = _verify_creation_gate(matrix, root=root)
    closed = len(projects) == len(DOWNSTREAM_PROJECT_IDS) and all(
        not item["worktree_exists"]
        and not item["branch_exists"]
        and not item["first_party_plugin_roots_exists"]
        for item in projects
    )
    return {"closed": closed, "projects": projects}


def _assert_commit(root: Path, value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        raise AssertionError(f"{field} is not an exact lowercase commit SHA")
    completed = _run_git_at(root, "cat-file", "-t", value)
    if completed.returncode != 0 or completed.stdout.strip() != "commit":
        raise AssertionError(f"{field} does not name an existing commit: {value}")
    return value


def _assert_ancestor(root: Path, base_sha: str, head_sha: str) -> None:
    completed = _run_git_at(root, "merge-base", "--is-ancestor", base_sha, head_sha)
    if completed.returncode == 0:
        return
    if completed.returncode == 1:
        raise AssertionError("head_sha is not based on base_sha")
    detail = completed.stderr.strip() or "unable to check commit ancestry"
    raise AssertionError(f"cannot check commit ancestry: {detail}")


def _commit_changed_paths(root: Path, base_sha: str, head_sha: str) -> list[str]:
    raw = _git_at(
        root,
        "-c",
        "core.quotePath=false",
        "diff",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        base_sha,
        head_sha,
        "--",
    )
    paths = {_normalise_path(item) for item in raw.splitlines() if item}
    return sorted(paths)


def changed_paths(base_sha: str = BASE_SHA, *, root: Path | None = None) -> list[str]:
    """Return current worktree changes relative to ``base_sha``.

    This is the legacy P0-worktree API.  Submission validation uses
    :func:`_commit_changed_paths` instead, so an untracked file in the P0
    checkout cannot masquerade as a downstream commit diff.
    """

    active_root = ROOT if root is None else Path(root)
    paths: set[str] = set()
    diff = _git_at(
        active_root,
        "-c",
        "core.quotePath=false",
        "diff",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        base_sha,
        "--",
    )
    paths.update(_normalise_path(item) for item in diff.splitlines() if item)
    status = _git_at(
        active_root,
        "-c",
        "core.quotePath=false",
        "status",
        "--short",
        "--untracked-files=all",
    )
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, payload = line[:2], line[3:]
        if " -> " in payload and code[0] in {"R", "C"}:
            old, new = payload.split(" -> ", 1)
            paths.add(_normalise_path(old))
            paths.add(_normalise_path(new))
        else:
            paths.add(_normalise_path(payload))
    return sorted(paths)


def _resolve_evidence_path(value: Any, root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise AssertionError("evidence path must be a non-empty string")
    path = Path(value)
    return path if path.is_absolute() else root / path


def _verify_evidence_file(
    record: dict[str, Any],
    *,
    label: str,
    root: Path,
    inherited_exit_code: int | None = None,
) -> dict[str, Any]:
    path = _resolve_evidence_path(record.get("path"), root)
    expected_hash = record.get("sha256")
    if not isinstance(expected_hash, str) or not HEX64.fullmatch(expected_hash):
        raise AssertionError(f"{label} sha256 is not an exact lowercase hash")
    exit_code = record.get("exit_code", inherited_exit_code)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise AssertionError(f"{label} must bind an integer exit_code")
    if exit_code != 0:
        raise AssertionError(f"{label} has non-zero exit_code: {exit_code}")
    actual_size = path.stat().st_size if path.is_file() else None
    if actual_size is None:
        raise AssertionError(f"{label} file is missing: {path}")
    declared_bytes = record.get("bytes")
    if declared_bytes is not None:
        if isinstance(declared_bytes, bool) or not isinstance(declared_bytes, int):
            raise AssertionError(f"{label} bytes must be an integer")
        if declared_bytes != actual_size:
            raise AssertionError(f"{label} size drift: {path}")
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise AssertionError(f"{label} hash drift: {path}")
    return {
        "path": str(path),
        "bytes": actual_size,
        "sha256": actual_hash,
        "exit_code": exit_code,
    }


def _as_record_list(value: Any, *, label: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if "path" in value or "sha256" in value:
            return [value]
        values = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        raise AssertionError(f"{label} must be a list or object")
    if not values or not all(isinstance(item, dict) for item in values):
        raise AssertionError(f"{label} must contain evidence objects")
    return list(values)


def _nested_evidence_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key in ("path", "sha256"):
        if key in item:
            return [item]
    for key in ("evidence", "artifacts", "artifact", "stdout", "stderr", "output", "result"):
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, dict):
            if "path" in value or "sha256" in value:
                records.append(value)
            else:
                records.extend(_as_record_list(value, label=f"validation.{key}"))
        elif isinstance(value, list):
            records.extend(_as_record_list(value, label=f"validation.{key}"))
        else:
            raise AssertionError(f"validation.{key} must be an evidence object/list")
    return records


def _verify_submission_evidence(value: dict[str, Any], *, root: Path) -> list[dict[str, Any]]:
    """Validate command records and their content-addressed output files."""

    summaries: list[dict[str, Any]] = []
    validation = value.get("validation")
    direct_evidence = value.get("evidence")
    review = value.get("review")
    if validation is not None:
        if not isinstance(validation, list) or not validation:
            raise AssertionError("submission validation must be a non-empty list")
        for index, item in enumerate(validation):
            if not isinstance(item, dict):
                raise AssertionError(f"validation[{index}] must be an object")
            command = item.get("command")
            if not isinstance(command, str) or not command.strip():
                raise AssertionError(f"validation[{index}] command is required")
            exit_code = item.get("exit_code")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise AssertionError(f"validation[{index}] must bind an integer exit_code")
            if exit_code != 0:
                raise AssertionError(f"validation[{index}] has non-zero exit_code: {exit_code}")
            nested = _nested_evidence_records(item)
            # Every command claim must carry its own content-addressed output.
            # A review/artifact record elsewhere in the submission cannot bind
            # an unrelated command to an exit code or hide a missing log.
            if not nested:
                raise AssertionError(
                    f"validation[{index}] must bind at least one real evidence file"
                )
            for evidence_index, record in enumerate(nested):
                summaries.append(
                    _verify_evidence_file(
                        record,
                        label=f"validation[{index}].evidence[{evidence_index}]",
                        root=root,
                        inherited_exit_code=exit_code,
                    )
                )

    if direct_evidence is not None:
        records = (
            []
            if direct_evidence == []
            else _as_record_list(direct_evidence, label="submission evidence")
        )
        inherited = 0 if isinstance(validation, list) and validation else None
        for index, record in enumerate(records):
            summaries.append(
                _verify_evidence_file(
                    record,
                    label=f"evidence[{index}]",
                    root=root,
                    inherited_exit_code=inherited,
                )
            )

    # Some producers put raw-output records in the top-level artifacts field
    # and keep the command/exit-code binding in validation.  Accept that
    # existing shape, but only as evidence when a successful command record is
    # present; an unbound artifact alone is not sufficient.
    artifacts = value.get("artifacts")
    if artifacts is not None:
        records = [] if artifacts == [] else _as_record_list(artifacts, label="submission artifacts")
        if not isinstance(validation, list) or not validation:
            raise AssertionError("top-level artifacts require validation exit-code evidence")
        for index, record in enumerate(records):
            summaries.append(
                _verify_evidence_file(
                    record,
                    label=f"artifacts[{index}]",
                    root=root,
                    inherited_exit_code=0,
                )
            )

    # Final submissions use the queue template's review shape.  A closure
    # artifact may be a full content-addressed record or a reference to one of
    # the already checked top-level artifacts.  Both forms remain compatible
    # with delivery manifests and finding-closure evidence without trusting a
    # package ID as a filesystem path.
    if review is not None:
        if not isinstance(review, dict):
            raise AssertionError("submission review must be an object")
        reviewer = review.get("reviewer")
        if reviewer is not None and (not isinstance(reviewer, str) or not reviewer):
            raise AssertionError("reviewer must be a non-empty string when present")
        finding_ids = review.get("finding_ids")
        if finding_ids is not None and (
            not isinstance(finding_ids, list)
            or not all(isinstance(item, str) for item in finding_ids)
        ):
            raise AssertionError("review finding_ids must be a string list")
        closure = review.get("closure_artifact")
        if closure is not None:
            inherited = 0 if isinstance(validation, list) and validation else None
            if isinstance(closure, dict) and (
                "path" in closure or "sha256" in closure
            ):
                summaries.append(
                    _verify_evidence_file(
                        closure,
                        label="review.closure_artifact",
                        root=root,
                        inherited_exit_code=inherited,
                    )
                )
            else:
                reference = (
                    closure.get("artifact_id") or closure.get("id")
                    if isinstance(closure, dict)
                    else closure
                )
                if not isinstance(reference, str) or not reference:
                    raise AssertionError("review closure_artifact must be a record or reference")
                artifact_records = (
                    []
                    if artifacts in (None, [])
                    else _as_record_list(artifacts, label="submission artifacts")
                )
                referenced = any(
                    any(reference == item.get(key) for key in ("id", "artifact_id", "name", "path"))
                    for item in artifact_records
                )
                if not referenced:
                    raise AssertionError(
                        "review closure_artifact does not reference a checked artifact"
                    )

    if not summaries:
        raise AssertionError(
            "submission must include evidence files with real sha256 and exit_code bindings"
        )
    return summaries


def _verify_submission(
    path: Path,
    allowed_patterns: list[str] | None = None,
    *,
    matrix: dict[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Validate one downstream submission against matrix and Git reality.

    ``allowed_patterns`` remains as a positional compatibility parameter for
    callers of the original helper.  It is intentionally not trusted for a
    downstream submission; the project-specific patterns are always loaded
    from the matrix using ``submission.project_id``.
    """

    del allowed_patterns  # compatibility-only; matrix is the source of truth
    active_root = ROOT if root is None else Path(root)
    active_matrix = matrix if matrix is not None else _read_json(DEFAULT_MATRIX)
    if active_matrix.get("schema") != "plotpilot-seven-project-matrix/v1":
        raise AssertionError("unexpected project matrix schema")
    value = _read_json(Path(path))
    if value.get("schema") != "integration-ready/v1":
        raise AssertionError("submission schema must be integration-ready/v1")
    if value.get("status") == "template" or value.get("ready") is not True:
        raise AssertionError("submission is still a template or is not ready")
    submission_id = value.get("submission_id")
    if not isinstance(submission_id, str) or not submission_id:
        raise AssertionError("submission_id is required")
    project_id = value.get("project_id")
    if not isinstance(project_id, str):
        raise AssertionError("submission project_id is required")
    project = _downstream_project(active_matrix, project_id)
    source_branch = value.get("source_branch")
    if source_branch is not None and (not isinstance(source_branch, str) or not source_branch):
        raise AssertionError("source_branch must be a non-empty string when present")

    base_sha = _assert_commit(active_root, value.get("base_sha"), "base_sha")
    head_sha = _assert_commit(active_root, value.get("head_sha"), "head_sha")
    _assert_ancestor(active_root, base_sha, head_sha)
    actual_paths = _commit_changed_paths(active_root, base_sha, head_sha)

    submitted_paths = value.get("changed_paths")
    if not isinstance(submitted_paths, list) or not submitted_paths:
        raise AssertionError("submission changed_paths must be non-empty")
    normalised_submitted = [_normalise_path(item) for item in submitted_paths]
    if len(normalised_submitted) != len(set(normalised_submitted)):
        raise AssertionError("submission changed_paths contains duplicates")
    if sorted(normalised_submitted) != actual_paths:
        raise AssertionError(
            f"submission changed_paths do not equal git diff: submitted={normalised_submitted}, "
            f"actual={actual_paths}"
        )

    patterns = project.get("write_sets")
    if not isinstance(patterns, list) or not patterns or not all(isinstance(item, str) for item in patterns):
        raise AssertionError(f"{project_id} write set is invalid")
    out_of_set = [
        item for item in actual_paths if not any(_pattern_matches(item, pattern) for pattern in patterns)
    ]
    if out_of_set:
        raise AssertionError(f"{project_id} changed paths outside its write set: {out_of_set}")

    proof = value.get("write_set_proof")
    if not isinstance(proof, dict):
        raise AssertionError("submission write_set_proof is required")
    # A downstream submission must prove the write-set selected for its own
    # project.  The legacy p0_write_set_match field is retained in the queue
    # template for compatibility, but it is never sufficient for P1-P6.
    if proof.get("project_id") not in (None, project_id):
        raise AssertionError("submission write-set proof project_id does not match submission")
    if proof.get("project_write_set_match") is not True:
        raise AssertionError("submission project write-set proof failed")
    if "p0_write_set_match" in proof and proof["p0_write_set_match"] is not True:
        raise AssertionError("submission legacy write-set proof failed")
    proof_out = proof.get("out_of_set_paths", [])
    if not isinstance(proof_out, list) or proof_out:
        raise AssertionError(f"submission write-set proof contains out-of-set paths: {proof_out!r}")

    for field in ("contract_delta_ids", "dependency_delta_ids"):
        delta_ids = value.get(field)
        if not isinstance(delta_ids, list) or not all(isinstance(item, str) for item in delta_ids):
            raise AssertionError(f"{field} must be an explicit string list")

    manifest_path = active_root / "contracts" / "manifest-v1.json"
    actual_manifest_hash = _sha256(manifest_path)
    declared_manifest_hash = value.get("contract_manifest_sha256")
    if not isinstance(declared_manifest_hash, str) or not HEX64.fullmatch(declared_manifest_hash):
        raise AssertionError("contract_manifest_sha256 is not an exact lowercase hash")
    if declared_manifest_hash != actual_manifest_hash:
        raise AssertionError(
            "contract_manifest_sha256 does not match current contracts/manifest-v1.json"
        )

    evidence = _verify_submission_evidence(value, root=active_root)
    return {
        "path": str(path),
        "submission_id": submission_id,
        "project_id": project_id,
        "source_branch": source_branch,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "head_based_on_base": True,
        "changed_paths": actual_paths,
        "write_set": patterns,
        "contract_manifest_sha256": actual_manifest_hash,
        "evidence": evidence,
    }


def validate(
    *,
    matrix_path: Path = DEFAULT_MATRIX,
    submission_path: Path | None = None,
    require_clean: bool = False,
) -> dict[str, Any]:
    matrix = _read_json(Path(matrix_path))
    if matrix.get("schema") != "plotpilot-seven-project-matrix/v1":
        raise AssertionError("unexpected project matrix schema")
    branch = _git("branch", "--show-current")
    if branch != P0_BRANCH:
        raise AssertionError(f"current branch is not P0 integration branch: {branch}")
    head = _assert_commit(ROOT, _git("rev-parse", "HEAD"), "current HEAD")
    _assert_commit(ROOT, BASE_SHA, "P0 base_sha")
    _assert_ancestor(ROOT, BASE_SHA, head)
    patterns = _p0_write_set(matrix)
    paths = changed_paths(BASE_SHA)
    out_of_set = [path for path in paths if not any(_pattern_matches(path, pattern) for pattern in patterns)]
    if out_of_set:
        raise AssertionError(f"changed paths outside P0 write set: {out_of_set}")
    push_url = _git("remote", "get-url", "--push", "donor-local")
    if push_url != "DISABLED":
        raise AssertionError(f"donor-local push URL must remain DISABLED, got {push_url!r}")
    creation_gate = _verify_creation_gate(matrix)
    status = _git("status", "--short", "--untracked-files=all").splitlines()
    if require_clean and status:
        raise AssertionError(f"worktree is not clean: {status}")
    submission = (
        _verify_submission(submission_path, matrix=matrix, root=ROOT)
        if submission_path
        else None
    )
    return {
        "schema": "p0-merge-gate/v1",
        "branch": branch,
        "head": head,
        "base_sha": BASE_SHA,
        "changed_paths": paths,
        "p0_write_set": patterns,
        "out_of_set_paths": [],
        "donor_local_push": push_url,
        "p1_p6_absent": True,
        "creation_gate": creation_gate,
        "worktree_clean": not status,
        "submission": submission,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--submission", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate(
        matrix_path=args.matrix,
        submission_path=args.submission,
        require_clean=args.require_clean,
    )
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
