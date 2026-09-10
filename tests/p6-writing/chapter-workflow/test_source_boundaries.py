from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "first-party-plugins" / "chapter-workflow"


def test_installable_plugin_has_required_surface_and_no_build_artifacts() -> None:
    required = (
        PLUGIN / "plugin.json",
        PLUGIN / "backend" / "pyproject.toml",
        PLUGIN / "backend" / "requirements.lock",
        PLUGIN / "backend" / "src" / "plotpilot_chapter_workflow" / "worker.py",
    )
    assert all(path.is_file() for path in required)

    forbidden_files = ("*.whl", "*.ppplugin", "*.pyc", "files.sha256")
    forbidden_directories = ("build", "dist", "wheels", "__pycache__")
    assert not [
        path
        for pattern in forbidden_files
        for path in PLUGIN.rglob(pattern)
    ]
    assert not [
        path
        for path in PLUGIN.rglob("*")
        if path.is_dir() and path.name in forbidden_directories
    ]


def test_production_has_no_direct_core_p5_or_storage_import() -> None:
    forbidden = ("plotpilot_core", "plotpilot_story_state", "sqlite3", "sqlalchemy")
    for path in PLUGIN.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not [name for name in imports if name.startswith(forbidden)], (path, imports)