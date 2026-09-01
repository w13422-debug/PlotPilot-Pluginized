"""Canonical contract inventory projections shared by Stage 0 producers and gates.

The v1 delivery must not infer its schema inventory independently in each
producer or verifier.  This module owns the path partition and the compact v1
inventory projection; callers may add presentation-only fields, but must not
recount the same tree.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
SCHEMAS = CONTRACTS / "json-schema"
V1_NEGATIVE = CONTRACTS / "corpus" / "negative" / "84.13"
SchemaScope = Literal["v1", "v2", "all"]


def relative(path: Path) -> str:
    """Return a repository-relative POSIX path or raise for foreign paths."""

    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def is_v2_contract_path(path: Path) -> bool:
    """Classify additive v2 contract artifacts from their frozen path grammar."""

    components = relative(path).split("/")
    leaf = components[-1]
    return any(component.endswith("-v2") for component in components[:-1]) or leaf.endswith(
        ("-v2.schema.json", ".v2.json")
    )


def contract_schema_paths(scope: SchemaScope = "v1") -> tuple[Path, ...]:
    """Project the schema tree into a complete, deterministic version scope."""

    if scope not in {"v1", "v2", "all"}:
        raise ValueError(f"unsupported contract schema scope: {scope!r}")
    paths = tuple(
        sorted(
            SCHEMAS.glob("*.schema.json"),
            key=lambda path: relative(path).encode("utf-8"),
        )
    )
    if scope == "all":
        return paths
    want_v2 = scope == "v2"
    return tuple(path for path in paths if is_v2_contract_path(path) is want_v2)


def _v1_paths(root: Path, pattern: str) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(
            root.glob(pattern),
            key=lambda item: relative(item).encode("utf-8"),
        )
        if not is_v2_contract_path(path)
    )


def v1_negative_group_paths() -> tuple[Path, ...]:
    return _v1_paths(V1_NEGATIVE, "*.json")


def v1_fixture_paths() -> tuple[Path, ...]:
    return _v1_paths(CONTRACTS / "examples" / "fixtures", "*.json")


def v1_combination_example_paths() -> tuple[Path, ...]:
    return _v1_paths(CONTRACTS / "examples", "*.json")


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {relative(path)}")
    return value


def v1_contract_inventory() -> dict[str, int]:
    """Return the single current v1 inventory used by every Stage 0 consumer."""

    negative_groups = [_read_object(path) for path in v1_negative_group_paths()]
    negative_case_count = 0
    for group in negative_groups:
        negative = group.get("negative")
        if not isinstance(negative, list):
            raise TypeError("v1 negative corpus group must contain a negative list")
        negative_case_count += len(negative)
    return {
        "schema_count": len(contract_schema_paths("v1")),
        "positive_fixture_count": len(v1_fixture_paths()),
        "combination_example_count": len(v1_combination_example_paths()),
        "negative_group_count": len(negative_groups),
        "negative_case_count": negative_case_count,
    }
