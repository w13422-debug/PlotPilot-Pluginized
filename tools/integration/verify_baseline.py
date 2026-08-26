"""Re-verify the pre-construction donor evidence without mutating either tree.

The external manifest and bootstrap evidence are inputs, not substitutes for a
fresh check.  This command re-reads both, recomputes every recorded backup
file hash, checks the donor Git identity/status, and confirms the P1--P6
creation gate is still closed.  It writes a small, reviewable result file in
the P0 delivery area when ``--output`` is supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(
    r"C:\Users\Administrator\Desktop\PlotPilot-Pluginized-preconstruction-backup-20260826\donor-backup-manifest.json"
)
DEFAULT_BOOTSTRAP = Path(
    r"C:\Users\Administrator\Desktop\Novel-Agent- (2)\novel-agent\cases\plotpilot-pluginized-seven-project-construction-20260826\evidence\p0-monorepo-bootstrap.json"
)
DEFAULT_MATRIX = Path(
    r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized七项目任务书-2026-08-26\project-matrix.json"
)


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is forbidden: {path}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return completed.stdout.rstrip("\r\n")


def _expected_status(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssertionError(f"{label} must be a list of strings")
    return sorted(item.rstrip("\r\n") for item in value)


def _verify_gate_roots(matrix: dict[str, Any]) -> dict[str, Any]:
    projects = matrix.get("projects")
    if not isinstance(projects, list):
        raise AssertionError("project matrix has no projects list")
    absent: list[dict[str, Any]] = []
    for project in projects:
        if not isinstance(project, dict) or project.get("id") == "P0":
            continue
        project_id = str(project.get("id"))
        worktree = Path(str(project.get("worktree")))
        branch = str(project.get("branch"))
        exists = worktree.exists()
        branch_exists = bool(
            subprocess.run(
                ["git", "-C", str(ROOT), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if exists or branch_exists:
            raise AssertionError(f"{project_id} creation gate is open: worktree={exists}, branch={branch_exists}")
        absent.append({"project_id": project_id, "worktree": str(worktree), "branch": branch, "worktree_exists": False, "branch_exists": False})
    return {"p1_p6_absent": True, "projects": absent}


def verify(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    bootstrap_path: Path = DEFAULT_BOOTSTRAP,
    matrix_path: Path = DEFAULT_MATRIX,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    bootstrap = _read_json(bootstrap_path)
    matrix = _read_json(matrix_path)

    donor = Path(str(manifest["donor_root"]))
    product = Path(str(bootstrap["product_root"]))
    if donor != Path(str(bootstrap["source_donor"])):
        raise AssertionError("bootstrap and donor manifest disagree on donor root")
    if product != ROOT:
        raise AssertionError(f"bootstrap product root is not this worktree: {product}")
    if manifest["backup_root"] != str(manifest_path.parent):
        raise AssertionError("manifest backup_root is not the manifest directory")
    if not donor.is_dir() or not product.is_dir():
        raise AssertionError("donor or product root is missing")

    donor_head = _git(donor, "rev-parse", "HEAD")
    donor_branch = _git(donor, "branch", "--show-current")
    donor_status = _git(donor, "-c", "core.quotePath=true", "status", "--short", "--untracked-files=all")
    donor_status_lines = [] if not donor_status else donor_status.splitlines()
    expected_status = _expected_status(manifest["donor_status_before"], "donor_status_before")
    if expected_status != _expected_status(manifest["donor_status_after"], "donor_status_after"):
        raise AssertionError("recorded donor status changed between before and after")
    if sorted(donor_status_lines) != expected_status:
        raise AssertionError(f"donor status drift: expected {expected_status!r}, got {donor_status_lines!r}")
    if donor_head != manifest["donor_head_before"] or donor_head != manifest["donor_head_after"]:
        raise AssertionError("donor HEAD drifted from the external manifest")
    if donor_head != bootstrap["source_donor_head"] or donor_branch != manifest["donor_branch"]:
        raise AssertionError("donor Git identity does not match bootstrap/manifest")
    if _git(donor, "cat-file", "-t", donor_head) != "commit":
        raise AssertionError("donor HEAD is not a commit object")

    manifest_sha = _sha256(manifest_path)
    if manifest_sha != bootstrap["external_backup_manifest_sha256"]:
        raise AssertionError("external backup manifest hash differs from bootstrap evidence")
    if manifest.get("file_count") != len(manifest.get("files", [])):
        raise AssertionError("manifest file_count is inconsistent")

    files: list[dict[str, Any]] = []
    for entry in manifest["files"]:
        source = Path(str(entry["source_path"]))
        backup = Path(str(entry["backup_path"]))
        if not source.is_file() or not backup.is_file():
            raise AssertionError(f"backup file is missing: {entry['relative_path']}")
        source_bytes = source.stat().st_size
        backup_bytes = backup.stat().st_size
        source_sha = _sha256(source)
        backup_sha = _sha256(backup)
        if source_bytes != entry["source_bytes"] or backup_bytes != entry["backup_bytes"]:
            raise AssertionError(f"byte size drift: {entry['relative_path']}")
        if source_sha != entry["source_sha256"] or backup_sha != entry["backup_sha256"] or source_sha != backup_sha:
            raise AssertionError(f"hash drift: {entry['relative_path']}")
        files.append(
            {
                "relative_path": entry["relative_path"],
                "source_path": str(source),
                "backup_path": str(backup),
                "source_bytes": source_bytes,
                "backup_bytes": backup_bytes,
                "source_sha256": source_sha,
                "backup_sha256": backup_sha,
                "match": True,
            }
        )

    product_head = _git(product, "rev-parse", "HEAD")
    product_branch = _git(product, "branch", "--show-current")
    push_url = _git(product, "remote", "get-url", "--push", "donor-local")
    if product_head != manifest["expected_product_commit"] or product_head != bootstrap["baseline_commit"]:
        raise AssertionError("product baseline commit differs from expected bootstrap commit")
    if product_branch != bootstrap["branch"] or product_branch != "codex/ppa-00-integration":
        raise AssertionError("product branch differs from the P0 integration branch")
    if push_url != "DISABLED":
        raise AssertionError(f"donor-local push URL is not DISABLED: {push_url!r}")
    gate = _verify_gate_roots(matrix)

    result = {
        "schema": "p0-baseline-verification/v1",
        "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "manifest": str(manifest_path),
            "manifest_sha256": manifest_sha,
            "bootstrap": str(bootstrap_path),
            "bootstrap_sha256": _sha256(bootstrap_path),
            "project_matrix": str(matrix_path),
            "design_version": matrix["design"]["version"],
            "design_sha256": matrix["design"]["sha256"],
        },
        "donor": {
            "root": str(donor),
            "head": donor_head,
            "branch": donor_branch,
            "status": donor_status_lines,
            "status_matches_external_manifest": True,
            "head_matches_external_manifest": True,
            "head_object_type": "commit",
            "files": files,
            "all_files_match": True,
            "status_unchanged": True,
        },
        "product": {
            "root": str(product),
            "baseline_head": product_head,
            "branch": product_branch,
            "donor_local_push": push_url,
            "current_status": _git(product, "status", "--short", "--untracked-files=all").splitlines(),
            "baseline_evidence_clean": bool(bootstrap["clean"]),
        },
        "creation_gate": gate,
        "passed": True,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--bootstrap", type=Path, default=DEFAULT_BOOTSTRAP)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(manifest_path=args.manifest, bootstrap_path=args.bootstrap, matrix_path=args.matrix)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
