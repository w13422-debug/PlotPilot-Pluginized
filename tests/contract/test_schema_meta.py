from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "json-schema"


@pytest.mark.integration
def test_all_generated_schemas_are_draft_2020_12_and_closed() -> None:
    paths = sorted(SCHEMA_DIR.glob("*.schema.json"))
    v1_paths = [path for path in paths if path.name.endswith("-v1.schema.json")]
    v2_paths = [path for path in paths if path.name.endswith("-v2.schema.json")]
    assert (len(paths), len(v1_paths), len(v2_paths)) == (64, 55, 9)
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object":
                    assert value.get("additionalProperties") is False, path.name
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)


@pytest.mark.integration
def test_union_roots_are_closed() -> None:
    for name in ("plugin-manifest-v1.schema.json", "rpc-request-v1.schema.json", "rpc-envelope-v1.schema.json", "plugin-ui-message-v1.schema.json"):
        value = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
        assert value.get("unevaluatedProperties") is False
