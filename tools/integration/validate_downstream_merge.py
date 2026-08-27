"""Validate an already integrated P1--P6 submission after M0.

``validate_merge_gate.py`` owns the M0 creation gate and deliberately rejects
P1--P6 paths in the P0 checkout.  Once M0 has opened the downstream projects,
this small gate validates the other half of the contract: the recorded
submission still names the real project-specific diff, and the P0 merge is a
non-fast-forward merge with the exact accepted base and submission parents.
It never performs a merge or mutates a repository.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

try:
    from . import validate_merge_gate as merge_gate
except ImportError:  # pragma: no cover - supports direct script execution
    import validate_merge_gate as merge_gate


ROOT = Path(__file__).resolve().parents[2]
P0_BRANCH = "codex/ppa-00-integration"


def _read_json(path: Path) -> dict[str, Any]:
    return merge_gate._read_json(path)


def _git(*args: str) -> str:
    return merge_gate._git(*args)


def _assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _verify_receipt_evidence(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    validation = receipt.get("validation")
    if not isinstance(validation, list) or not validation:
        raise AssertionError("merge receipt validation must be a non-empty list")
    summaries: list[dict[str, Any]] = []
    for index, item in enumerate(validation):
        if not isinstance(item, dict):
            raise AssertionError(f"merge receipt validation[{index}] must be an object")
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            raise AssertionError(f"merge receipt validation[{index}] command is required")
        if item.get("status") != "passed" or item.get("exit_code") != 0:
            raise AssertionError(
                f"merge receipt validation[{index}] is not a passed zero-exit command"
            )
        for stream in ("stdout", "stderr"):
            record = item.get(stream)
            if not isinstance(record, dict):
                raise AssertionError(
                    f"merge receipt validation[{index}] missing {stream} evidence"
                )
            summaries.append(
                merge_gate._verify_evidence_file(
                    record,
                    label=f"merge receipt validation[{index}].{stream}",
                    root=ROOT,
                    inherited_exit_code=0,
                )
            )
    closure = receipt.get("closure_evidence")
    if not isinstance(closure, dict):
        raise AssertionError("merge receipt closure_evidence is required")
    if closure.get("finding_id") != "F-P1-INT-01" or closure.get("status") != "CLOSED":
        raise AssertionError("F-P1-INT-01 closure evidence is not closed")
    summaries.append(
        merge_gate._verify_evidence_file(
            closure,
            label="merge receipt closure_evidence",
            root=ROOT,
            inherited_exit_code=0,
        )
    )
    return summaries


def validate(
    *,
    submission_path: Path,
    merge_receipt_path: Path,
    require_clean: bool = False,
) -> dict[str, Any]:
    submission = _read_json(submission_path)
    receipt = _read_json(merge_receipt_path)
    if receipt.get("schema") != "ppa-merge-receipt/v1":
        raise AssertionError("unexpected merge receipt schema")
    if receipt.get("status") != "integrated":
        raise AssertionError("merge receipt is not integrated")
    if receipt.get("merge_method") != "git merge --no-ff":
        raise AssertionError("merge receipt does not record a no-ff merge")

    verified = merge_gate._verify_submission(
        submission_path,
        matrix=merge_gate._read_json(merge_gate.DEFAULT_MATRIX),
        root=ROOT,
    )
    base_sha = verified["base_sha"]
    submission_head = verified["head_sha"]
    reviewed_head = receipt.get("reviewed_implementation_head")
    merge_commit = receipt.get("merge_commit")
    for value, label in (
        (reviewed_head, "reviewed_implementation_head"),
        (merge_commit, "merge_commit"),
    ):
        merge_gate._assert_commit(ROOT, value, label)
    if _git("branch", "--show-current") != P0_BRANCH:
        raise AssertionError("current branch is not the P0 integration branch")
    current_head = merge_gate._assert_commit(ROOT, _git("rev-parse", "HEAD"), "current HEAD")
    if require_clean and _git("status", "--short", "--untracked-files=all"):
        raise AssertionError("P0 worktree is not clean")
    if _git("remote", "get-url", "--push", "donor-local") != "DISABLED":
        raise AssertionError("donor-local push URL is not DISABLED")

    _assert_equal(receipt.get("project_id"), verified["project_id"], "project_id")
    _assert_equal(receipt.get("accepted_base_sha"), base_sha, "accepted_base_sha")
    _assert_equal(receipt.get("submission_head"), submission_head, "submission_head")
    _assert_equal(receipt.get("submission_parent"), reviewed_head, "submission_parent")
    _assert_equal(receipt.get("submission_parent"), _git("rev-parse", f"{submission_head}^"), "submission parent")
    _assert_equal(receipt.get("merge_parents"), [base_sha, submission_head], "merge_parents")
    _assert_equal(receipt.get("changed_paths"), verified["changed_paths"], "changed_paths")
    _assert_equal(receipt.get("changed_paths_count"), len(verified["changed_paths"]), "changed_paths_count")
    _assert_equal(receipt.get("out_of_set_paths"), [], "out_of_set_paths")
    _assert_equal(receipt.get("contract_manifest_sha256"), verified["contract_manifest_sha256"], "contract_manifest_sha256")
    if reviewed_head != submission.get("reviewed_implementation_head"):
        raise AssertionError("submission reviewed implementation head disagrees with receipt")
    if submission.get("evidence_only_submission_head") != submission_head:
        raise AssertionError("submission does not separate the evidence-only submission head")

    merge_parents = _git("rev-list", "--parents", "-n", "1", merge_commit).split()
    _assert_equal(merge_parents, [merge_commit, base_sha, submission_head], "Git merge parents")
    merge_gate._assert_ancestor(ROOT, merge_commit, current_head)
    summaries = _verify_receipt_evidence(receipt)
    return {
        "schema": "p0-downstream-merge-gate/v1",
        "passed": True,
        "project_id": verified["project_id"],
        "submission_id": submission.get("submission_id"),
        "base_sha": base_sha,
        "submission_head": submission_head,
        "reviewed_implementation_head": reviewed_head,
        "merge_commit": merge_commit,
        "merge_parents": [base_sha, submission_head],
        "current_head": current_head,
        "current_head_contains_merge": True,
        "changed_paths": verified["changed_paths"],
        "out_of_set_paths": [],
        "contract_manifest_sha256": verified["contract_manifest_sha256"],
        "verified_evidence_records": len(summaries),
        "donor_push_url": "DISABLED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--merge-receipt", type=Path, required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    try:
        result = validate(
            submission_path=args.submission,
            merge_receipt_path=args.merge_receipt,
            require_clean=args.require_clean,
        )
    except (AssertionError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": "p0-downstream-merge-gate/v1", "passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
