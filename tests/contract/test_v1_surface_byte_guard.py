from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ACCEPTED_E0 = "761c79a8343dbc17ee8f40e21e60fb962eeecd79"
E0_BLOB_OVERRIDES = {
    "backend/plotpilot_plugin_sdk/package.py": "c41ee05ee7e4ab2afd5c0780238ac12277faa3ec",
    "contracts/manifest-v1.json": "bb0421c52fdace39ddc3e2d5e397bc0cc2ccf335",
    "backend/plotpilot_plugin_sdk/rpc.py": "bc62f78bd575af50c3d69241b88464832fd77377",
    "backend/plotpilot_plugin_sdk/verifier.py": "69485ea5516ac92f51e3d30dd60eb5fcce6dfef9",
    "backend/plotpilot_plugin_sdk/pyproject.toml": "f25334cb37d4101300015b29d1f5ed7bbb79f7c0",
}
POST_E0_API_SOURCE_FILES = frozenset(
    {
        "backend/plotpilot_core/api/v1/core/__init__.py",
        "backend/plotpilot_core/api/v1/core/adapter.py",
        "backend/plotpilot_core/api/v1/core/router.py",
        "backend/plotpilot_core/api/v1/webui/__init__.py",
        "backend/plotpilot_core/api/v1/webui/chapter_generation.py",
    }
)


def _tracked_paths_at_e0(root: str) -> set[str]:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ACCEPTED_E0, "--", root],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return {line.decode("utf-8") for line in completed.stdout.splitlines() if line}


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


def _expected_blob(path: str) -> str:
    override = E0_BLOB_OVERRIDES.get(path)
    if override is not None:
        return override
    return _e0_blob(path)


def _assert_e0_bytes_unchanged(root: str) -> None:
    paths = _tracked_paths_at_e0(root)
    paths.update(
        path
        for path in E0_BLOB_OVERRIDES
        if path == root or path.startswith(f"{root}/")
    )
    assert paths, f"accepted E0 contains no tracked files under {root}"
    for path in sorted(paths):
        current = ROOT / Path(path)
        assert current.is_file(), f"accepted E0 file is missing: {path}"
        assert _current_filtered_blob(path) == _expected_blob(path), (
            f"accepted E0 bytes changed: {path}"
        )


def test_existing_contract_sdk_and_frontend_bytes_are_frozen() -> None:
    for root in ("contracts", "backend/plotpilot_plugin_sdk", "frontend/src/contracts"):
        _assert_e0_bytes_unchanged(root)


def test_sdk_byte_guard_reaches_overrides_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = "backend/plotpilot_plugin_sdk"
    pyproject_path = f"{root}/pyproject.toml"
    verifier_path = f"{root}/verifier.py"
    unmapped_path = next(
        path
        for path in sorted(_tracked_paths_at_e0(root))
        if path not in E0_BLOB_OVERRIDES
    )

    assert _expected_blob(pyproject_path) == E0_BLOB_OVERRIDES[pyproject_path]
    assert _expected_blob(unmapped_path) == _e0_blob(unmapped_path)
    _assert_e0_bytes_unchanged(root)

    for path in (pyproject_path, verifier_path, unmapped_path):
        real_reader = _current_filtered_blob

        def forged_reader(
            candidate_path: str,
            *,
            tampered_path: str = path,
            reader=real_reader,
        ) -> str:
            if candidate_path == tampered_path:
                return "0" * 40
            return reader(candidate_path)

        with monkeypatch.context() as patched:
            patched.setitem(globals(), "_current_filtered_blob", forged_reader)
            with pytest.raises(AssertionError) as raised:
                _assert_e0_bytes_unchanged(root)
            assert str(raised.value).startswith(f"accepted E0 bytes changed: {path}")


def test_v1_api_surface_inventory_and_bytes_are_frozen() -> None:
    root = "backend/plotpilot_core/api/v1"
    e0_paths = _tracked_paths_at_e0(root)
    current_paths = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / root).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert current_paths == e0_paths | POST_E0_API_SOURCE_FILES
    for path in sorted(e0_paths):
        current = ROOT / Path(path)
        assert current.is_file(), f"accepted E0 file is missing: {path}"
    for path in sorted(POST_E0_API_SOURCE_FILES):
        assert (ROOT / Path(path)).is_file(), f"authorized post-E0 file is missing: {path}"
    for path in sorted(e0_paths):
        assert _current_filtered_blob(path) == _expected_blob(path), (
            f"accepted E0 bytes changed: {path}"
        )
