"""Validate the P0 integration queue and the current worktree write set.

The validator is deliberately independent of the product runtime.  It reads
the external project matrix, checks every changed path (including untracked
files), verifies the P0 identity and donor push fence, and keeps the P1--P6
creation gate closed.  It can also validate a downstream
``integration-ready/v1`` submission document without copying or merging it.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = Path(
    r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\project-matrix.json"
)
BASE_SHA = "1c481237b6fa32ef5f85d7f8da4cb16f366cd4f0"
P0_BRANCH = "codex/ppa-00-integration"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return completed.stdout.rstrip("\r\n")


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is forbidden: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _normalise_path(value: str) -> str:
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or "\x00" in path:
        raise AssertionError(f"invalid changed path: {value!r}")
    normalised = str(PurePosixPath(path))
    if normalised == "." or normalised == ".." or normalised.startswith("../"):
        raise AssertionError(f"path escapes repository: {value!r}")
    return normalised


def _pattern_matches(path: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if "/" not in pattern:
        return "/" not in path and fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path, pattern)


def _p0_write_set(matrix: dict[str, Any]) -> list[str]:
    for project in matrix.get("projects", []):
        if isinstance(project, dict) and project.get("id") == "P0":
            values = project.get("write_sets")
            if isinstance(values, list) and all(isinstance(item, str) for item in values):
                return values
    raise AssertionError("project matrix has no P0 write set")


def changed_paths(base_sha: str = BASE_SHA) -> list[str]:
    paths: set[str] = set()
    # Diff catches staged and unstaged tracked edits relative to the immutable
    # bootstrap commit.  Status adds untracked files, which git diff omits.
    diff = _git("diff", "--name-only", "--diff-filter=ACDMRTUXB", base_sha, "--")
    paths.update(_normalise_path(item) for item in diff.splitlines() if item)
    status = _git("-c", "core.quotePath=false", "status", "--short", "--untracked-files=all")
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, payload = line[:2], line[3:]
        # For renames git prints ``old -> new``.  Both names are checked so a
        # rename cannot hide a write outside the owning project.
        if " -> " in payload and code[0] in {"R", "C"}:
            old, new = payload.split(" -> ", 1)
            paths.add(_normalise_path(old))
            paths.add(_normalise_path(new))
        else:
            paths.add(_normalise_path(payload))
    return sorted(paths)


def _verify_creation_gate(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    absent: list[dict[str, Any]] = []
    for project in matrix.get("projects", []):
        if not isinstance(project, dict) or project.get("id") == "P0":
            continue
        project_id = str(project.get("id"))
        worktree = Path(str(project.get("worktree")))
        branch = str(project.get("branch"))
        worktree_exists = worktree.exists()
        branch_exists = subprocess.run(
            ["git", "-C", str(ROOT), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        first_party = [str(item) for item in project.get("first_party_plugins", []) if isinstance(item, str)]
        first_party_exists = [item for item in first_party if (ROOT / item).exists()]
        if worktree_exists or branch_exists or first_party_exists:
            raise AssertionError(
                f"{project_id} creation gate is open: worktree={worktree_exists}, "
                f"branch={branch_exists}, first_party_plugins={first_party_exists}"
            )
        absent.append(
            {
                "project_id": project_id,
                "worktree": str(worktree),
                "branch": branch,
                "worktree_exists": False,
                "branch_exists": False,
                "first_party_plugins": first_party,
            }
        )
    if len(absent) != 6:
        raise AssertionError(f"expected six closed P1-P6 gates, found {len(absent)}")
    return absent


def _verify_submission(path: Path, allowed_patterns: list[str]) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("schema") != "integration-ready/v1":
        raise AssertionError("submission schema must be integration-ready/v1")
    if value.get("status") == "template" or value.get("ready") is not True:
        raise AssertionError("submission is still a template or is not ready")
    if not isinstance(value.get("submission_id"), str) or not value["submission_id"]:
        raise AssertionError("submission_id is required")
    if not isinstance(value.get("project_id"), str) or value["project_id"] == "P0":
        raise AssertionError("submission must come from a downstream project")
    for field, pattern in (("base_sha", HEX40), ("head_sha", HEX40), ("contract_manifest_sha256", HEX64)):
        if not isinstance(value.get(field), str) or not pattern.fullmatch(value[field]):
            raise AssertionError(f"{field} is not an exact lowercase hash")
    paths = value.get("changed_paths")
    if not isinstance(paths, list) or not paths:
        raise AssertionError("submission changed_paths must be non-empty")
    normalised = [_normalise_path(str(item)) for item in paths]
    out = [item for item in normalised if not any(_pattern_matches(item, pattern) for pattern in allowed_patterns)]
    proof = value.get("write_set_proof")
    if not isinstance(proof, dict) or proof.get("p0_write_set_match") is not True or out:
        raise AssertionError(f"submission write-set proof failed: {out}")
    if value.get("contract_delta_ids") is None or value.get("dependency_delta_ids") is None:
        raise AssertionError("submission delta lists must be explicit")
    if not isinstance(value.get("validation"), list) or not value["validation"]:
        raise AssertionError("submission must include validation evidence")
    return {"path": str(path), "submission_id": value["submission_id"], "changed_paths": normalised}


def validate(*, matrix_path: Path = DEFAULT_MATRIX, submission_path: Path | None = None, require_clean: bool = False) -> dict[str, Any]:
    matrix = _read_json(matrix_path)
    if matrix.get("schema") != "plotpilot-seven-project-matrix/v1":
        raise AssertionError("unexpected project matrix schema")
    branch = _git("branch", "--show-current")
    if branch != P0_BRANCH:
        raise AssertionError(f"current branch is not P0 integration branch: {branch}")
    head = _git("rev-parse", "HEAD")
    if not HEX40.fullmatch(head):
        raise AssertionError("current HEAD is not a commit SHA")
    _git("cat-file", "-e", f"{BASE_SHA}^{{commit}}")
    patterns = _p0_write_set(matrix)
    paths = changed_paths()
    out_of_set = [path for path in paths if not any(_pattern_matches(path, pattern) for pattern in patterns)]
    # .codex/active-work.json is intentionally local and ignored; if a caller
    # passes it explicitly to git in a future workflow it must still not be a
    # release write.  It is not included in status once .gitignore is active.
    if out_of_set:
        raise AssertionError(f"changed paths outside P0 write set: {out_of_set}")
    push_url = _git("remote", "get-url", "--push", "donor-local")
    if push_url != "DISABLED":
        raise AssertionError(f"donor-local push URL must remain DISABLED, got {push_url!r}")
    gate = _verify_creation_gate(matrix)
    status = _git("status", "--short", "--untracked-files=all").splitlines()
    if require_clean and status:
        raise AssertionError(f"worktree is not clean: {status}")
    submission = _verify_submission(submission_path, patterns) if submission_path else None
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
        "creation_gate": gate,
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
    result = validate(matrix_path=args.matrix, submission_path=args.submission, require_clean=args.require_clean)
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
