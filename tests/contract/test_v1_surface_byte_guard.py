from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ACCEPTED_E0 = "761c79a8343dbc17ee8f40e21e60fb962eeecd79"


def _tracked_paths_at_e0(root: str) -> set[str]:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ACCEPTED_E0, "--", root],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return {
        line.decode("utf-8")
        for line in completed.stdout.splitlines()
        if line
    }


def _e0_blob(path: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", f"{ACCEPTED_E0}:{path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _current_filtered_blob(path: str) -> str:
    """Hash the working-tree file as Git would store it, including EOL filters."""
    completed = subprocess.run(
        ["git", "hash-object", f"--path={path}", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _assert_e0_bytes_unchanged(root: str) -> None:
    paths = _tracked_paths_at_e0(root)
    assert paths, f"accepted E0 contains no tracked files under {root}"
    for path in sorted(paths):
        current = ROOT / Path(path)
        assert current.is_file(), f"accepted E0 file is missing: {path}"
        assert _current_filtered_blob(path) == _e0_blob(path), f"accepted E0 bytes changed: {path}"


def test_existing_contract_sdk_and_frontend_bytes_are_frozen() -> None:
    for root in ("contracts", "backend/plotpilot_plugin_sdk", "frontend/src/contracts"):
        _assert_e0_bytes_unchanged(root)


def test_v1_api_surface_inventory_and_bytes_are_frozen() -> None:
    root = "backend/plotpilot_core/api/v1"
    e0_paths = _tracked_paths_at_e0(root)
    current_paths = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / root).rglob("*")
        if path.is_file()
    }
    assert current_paths == e0_paths
    _assert_e0_bytes_unchanged(root)
