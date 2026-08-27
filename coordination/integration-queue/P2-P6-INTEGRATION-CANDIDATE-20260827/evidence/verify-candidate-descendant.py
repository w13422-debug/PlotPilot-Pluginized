from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
CANDIDATE_REL = "coordination/integration-queue/P2-P6-INTEGRATION-CANDIDATE-20260827"
CANDIDATE = ROOT / CANDIDATE_REL
EXPECTED_BRANCH = "codex/ppa-00-integration-candidate-r2"
IMPLEMENTATION_HEAD = "63f8a0e863afe77e70aef3d0f6687039c6cfb7ec"
OLD_CANDIDATE = "8646812681e7b9af89a86070a7ec875b603a1b72"
CONTRACT_MANIFEST_SHA256 = "cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024"
CENTRAL_CLOSURE_PATH = (
    CANDIDATE_REL
    + "/evidence/closures/P2-central-closure-active-work.json"
)
CENTRAL_CLOSURE_BYTES = 70403
CENTRAL_CLOSURE_SHA256 = (
    "fd4aeacd934fd0e6e7cfd06fb8c1d8325db6a813b95eed4be81429123c105f84"
)
CENTRAL_CLOSURE_BLOB = "87158ac51355e5e072c8d000c6fdd6a2be58d72e"
CENTRAL_CLOSURE_REPO = Path(
    r"C:\Users\Administrator\Desktop\Novel-Agent- (2)\novel-agent"
)
CENTRAL_CLOSURE_POINTER = (
    "/tasks/task_id=PPA-P2-P2S1-IMPL-001-SAME-SOL-REREVIEW-37"
)
CENTRAL_CLOSURE_REVIEWER = "01a0424d-7474-7c42-adbc-ce22a7a7875b"
CENTRAL_CLOSURE_SOURCE = "5daece82633a700876474b6468f9948f62d466d7"
FORBIDDEN = (
    "be9e7273ed697d25a05454c17810b0872a8fe3cf",
    "2bbdc0efca047e388494b59bbe4ff4fcd34fcec6",
    "1262c2fda32b357596ce67d1ff3cdcc406ac13b5",
    "90929ea91b944cc5e22483d21631d806f9aed208",
)
TAGS = {
    "M0-OPEN": ("0a527454a2e9d20e353c41391d53d734a6dd62dc", "786986a02219973a69a0d1c1114191149924d7f7"),
    "M0-OPEN-R2": ("2b375224ec4762b9b25bca10cb80035f715b0b6f", "36ca847d8b328e3f4f7791c836e0530e8a5ea5b3"),
    "M0-OPEN-R3": ("9c7dbb76b0ce63c55b922e7ee629f25d229b9269", "b6e20df15de9eb4b25de1acfe4e8a8f9e7080bde"),
    "M0-OPEN-R4": ("385bc499032910195081e83a270b59c539f33539", "42123d1a5126bb2bef31304b0498e2e7def9183e"),
}


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        text=True,
        encoding="utf-8",
        errors="strict",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def value(*args: str) -> str:
    return git(*args).stdout.strip()


def ancestor(older: str, newer: str) -> bool:
    result = git("merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr.strip())
    return result.returncode == 0


def digest(path: Path) -> tuple[int, str]:
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


def candidate_file(relative_path: str) -> Path:
    assert isinstance(relative_path, str)
    normalized = relative_path.replace("\\", "/")
    prefix = CANDIDATE_REL + "/"
    assert normalized.startswith(prefix), normalized
    path = (ROOT / Path(*normalized.split("/"))).resolve()
    try:
        path.relative_to(CANDIDATE.resolve())
    except ValueError as exc:
        raise AssertionError(f"candidate path escaped evidence directory: {relative_path}") from exc
    assert path.is_file(), path
    return path


def raw_git(repository: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def verify_central_closure(
    closure: dict[str, Any], current: str
) -> dict[str, Any]:
    assert closure["path"] == CENTRAL_CLOSURE_PATH
    assert closure["bytes"] == CENTRAL_CLOSURE_BYTES
    assert closure["sha256"] == CENTRAL_CLOSURE_SHA256
    assert closure["json_pointer"] == CENTRAL_CLOSURE_POINTER
    assert closure["reviewer_agent_id"] == CENTRAL_CLOSURE_REVIEWER
    assert closure["verdict"] == "PASS"
    assert closure["merge_eligible"] is True

    path = candidate_file(closure["path"])
    raw = path.read_bytes()
    assert len(raw) == CENTRAL_CLOSURE_BYTES
    assert hashlib.sha256(raw).hexdigest() == CENTRAL_CLOSURE_SHA256

    committed = raw_git(ROOT, "show", f"{current}:{CENTRAL_CLOSURE_PATH}")
    assert committed.returncode == 0, committed.stderr.decode(errors="replace")
    assert committed.stdout == raw

    provenance = closure["git_blob"]
    assert provenance["object"] == CENTRAL_CLOSURE_BLOB
    assert provenance["bytes"] == CENTRAL_CLOSURE_BYTES
    assert provenance["sha256"] == CENTRAL_CLOSURE_SHA256
    source_repository = Path(provenance["repository"]).resolve()
    assert source_repository == CENTRAL_CLOSURE_REPO.resolve()
    assert source_repository.exists()

    blob_type = raw_git(source_repository, "cat-file", "-t", CENTRAL_CLOSURE_BLOB)
    assert blob_type.returncode == 0, blob_type.stderr.decode(errors="replace")
    assert blob_type.stdout.strip() == b"blob"
    source_blob = raw_git(source_repository, "cat-file", "-p", CENTRAL_CLOSURE_BLOB)
    assert source_blob.returncode == 0, source_blob.stderr.decode(errors="replace")
    assert len(source_blob.stdout) == CENTRAL_CLOSURE_BYTES
    assert hashlib.sha256(source_blob.stdout).hexdigest() == CENTRAL_CLOSURE_SHA256
    assert source_blob.stdout == raw

    source_document = json.loads(raw.decode("utf-8"))
    tasks = source_document.get("tasks")
    assert isinstance(tasks, list)
    task = next(
        (item for item in tasks if item.get("task_id") == CENTRAL_CLOSURE_POINTER.split("=", 1)[1]),
        None,
    )
    assert isinstance(task, dict)
    assert task["agent_id"] == CENTRAL_CLOSURE_REVIEWER
    assert task["source_head"] == CENTRAL_CLOSURE_SOURCE
    assert task["verdict"] == "PASS"
    assert task["merge_eligible"] is True

    return {
        "path": CENTRAL_CLOSURE_PATH,
        "bytes": len(raw),
        "sha256": CENTRAL_CLOSURE_SHA256,
        "git_blob": CENTRAL_CLOSURE_BLOB,
        "git_blob_bytes": len(source_blob.stdout),
        "git_blob_sha256": hashlib.sha256(source_blob.stdout).hexdigest(),
        "committed_bytes": len(committed.stdout),
    }


def repository_records(value_: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value_, dict):
        if {"path", "bytes", "sha256"}.issubset(value_):
            path = value_["path"]
            if isinstance(path, str) and path.replace("\\", "/").startswith(CANDIDATE_REL + "/"):
                result.append(value_)
        for nested in value_.values():
            result.extend(repository_records(nested))
    elif isinstance(value_, list):
        for nested in value_:
            result.extend(repository_records(nested))
    return result


def main() -> int:
    assert value("branch", "--show-current") == EXPECTED_BRANCH
    current = value("rev-parse", "HEAD")
    assert value("show", "-s", "--format=%P", current).split() == [IMPLEMENTATION_HEAD]
    assert ancestor(IMPLEMENTATION_HEAD, current)
    assert value("rev-parse", "refs/heads/codex/ppa-00-integration") == OLD_CANDIDATE
    assert not ancestor(OLD_CANDIDATE, current)
    assert not value("status", "--porcelain=v1", "--untracked-files=all")
    assert value("remote", "get-url", "--push", "donor-local") == "DISABLED"
    assert digest(ROOT / "contracts/manifest-v1.json")[1] == CONTRACT_MANIFEST_SHA256

    changed = []
    for line in value("diff", "--name-status", IMPLEMENTATION_HEAD, current).splitlines():
        status, path = line.split("\t", 1)
        assert status == "A"
        assert path.startswith(CANDIDATE_REL + "/")
        changed.append(path)
    assert changed
    assert git("diff", "--check", IMPLEMENTATION_HEAD, current).returncode == 0

    manifest = json.loads((CANDIDATE / "candidate-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "integration_candidate_ready_pending_fresh_sol"
    assert manifest["branch"] == EXPECTED_BRANCH
    assert manifest["tested_implementation_head"] == IMPLEMENTATION_HEAD
    assert manifest["contract_manifest_sha256"] == CONTRACT_MANIFEST_SHA256
    remediation = manifest["evidence_remediation"]
    assert remediation["finding_id"] == "P2P6-SOL-FINAL-001"
    assert remediation["task_id"] == "PPA-P0-P2P6-SOL-FINAL-001-REMEDIATION-40"
    manifest_closure = remediation["central_closure"]
    assert manifest_closure["receipt_path"].endswith("/receipts/P2.json")
    assert manifest_closure["path"] == CENTRAL_CLOSURE_PATH
    assert manifest_closure["bytes"] == CENTRAL_CLOSURE_BYTES
    assert manifest_closure["sha256"] == CENTRAL_CLOSURE_SHA256
    assert manifest_closure["git_blob"]["object"] == CENTRAL_CLOSURE_BLOB

    records = repository_records(manifest)
    for record in records:
        path = candidate_file(record["path"])
        size, sha256 = digest(path)
        assert size == record["bytes"]
        assert sha256 == record["sha256"]

    batches = []
    central_closures = []
    for receipt_record in manifest["batch_receipts"]:
        receipt_path = candidate_file(receipt_record["path"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["status"] == "integration_candidate_pending_fresh_sol"
        assert value("show", "-s", "--format=%P", receipt["merge_commit"]).split() == receipt["merge_parents"]
        assert ancestor(receipt["merge_commit"], current)
        assert receipt["out_of_set_paths"] == []
        receipt_records = repository_records(receipt)
        for record in receipt_records:
            path = candidate_file(record["path"])
            size, sha256 = digest(path)
            assert size == record["bytes"]
            assert sha256 == record["sha256"]
        records.extend(receipt_records)
        if receipt["project_id"] == "P2":
            central_closures.append(verify_central_closure(receipt["central_closure"], current))
        else:
            other_closure = receipt.get("central_closure")
            if isinstance(other_closure, dict):
                other_path = other_closure.get("path")
                assert not (
                    isinstance(other_path, str)
                    and other_path.replace("\\", "/").startswith(CANDIDATE_REL + "/")
                )
        batches.append(receipt["project_id"])
    assert batches == ["P2", "P3", "P4", "P5", "P6"]
    assert len(central_closures) == 1
    assert central_closures[0]["path"] == manifest_closure["path"]
    assert central_closures[0]["bytes"] == manifest_closure["bytes"]
    assert central_closures[0]["sha256"] == manifest_closure["sha256"]
    assert central_closures[0]["git_blob"] == manifest_closure["git_blob"]["object"]

    forbidden = {}
    for commit in FORBIDDEN:
        included = ancestor(commit, current)
        assert not included
        forbidden[commit] = included

    tag_results = {}
    for tag, expected in TAGS.items():
        actual = (value("rev-parse", tag), value("rev-parse", f"{tag}^{{}}"))
        assert actual == expected
        tag_results[tag] = {"tag_object": actual[0], "peeled_commit": actual[1]}

    print(
        json.dumps(
            {
                "schema": "p0-p2-p6-post-commit-candidate-gate/v1",
                "passed": True,
                "candidate_head": current,
                "candidate_parent": IMPLEMENTATION_HEAD,
                "evidence_commit_changed_paths": len(changed),
                "evidence_commit_only_candidate_directory": True,
                "verified_content_records": len(records),
                "central_closure_records": len(central_closures),
                "central_closure": central_closures[0],
                "batch_order": batches,
                "forbidden_evidence_only": forbidden,
                "contract_manifest_sha256": CONTRACT_MANIFEST_SHA256,
                "old_tags": tag_results,
                "donor_push": "DISABLED",
                "worktree_clean": True,
                "independent_acceptance": "not_performed",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
