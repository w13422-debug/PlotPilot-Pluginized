from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "first-party-plugins" / "chapter-workflow"


def test_source_only_plugin_has_no_dead_installable_surface() -> None:
    assert not (PLUGIN / "plugin.json").exists()
    assert not list(PLUGIN.rglob("*.whl"))
    assert not list(PLUGIN.rglob("requirements*.txt"))
    assert not list(PLUGIN.rglob("worker.py"))


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
