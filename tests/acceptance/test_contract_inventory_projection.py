from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.integration import contract_inventory as projection
from tools.integration import (
    fresh_m0_review,
    generate_contract_manifest,
    generate_m0_delivery,
    validate_m0_delivery,
)


def _relative_set(paths: tuple[Path, ...]) -> set[str]:
    return {projection.relative(path) for path in paths}


def test_v1_and_v2_schema_projections_are_complete_and_disjoint() -> None:
    v1 = _relative_set(projection.contract_schema_paths("v1"))
    v2 = _relative_set(projection.contract_schema_paths("v2"))
    all_schemas = _relative_set(projection.contract_schema_paths("all"))

    assert v1
    assert v2
    assert v1.isdisjoint(v2)
    assert v1 | v2 == all_schemas
    assert all(not projection.is_v2_contract_path(projection.ROOT / path) for path in v1)
    assert all(projection.is_v2_contract_path(projection.ROOT / path) for path in v2)
    assert "contracts/json-schema/plugin-manifest-v1.schema.json" in v1
    assert "contracts/json-schema/prompt-skill-execute-request-v2.schema.json" in v2


def test_unknown_schema_projection_scope_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported contract schema scope"):
        projection.contract_schema_paths("future")  # type: ignore[arg-type]


def test_all_stage0_inventory_consumers_share_the_v1_projection() -> None:
    expected = projection.v1_contract_inventory()
    rendered_manifest = generate_contract_manifest.render()
    m0_inventory = generate_m0_delivery.corpus_inventory()
    m0_inventory.pop("negative_group_ids")

    assert rendered_manifest["inventory"]["schema_count"] == expected["schema_count"]
    assert rendered_manifest["inventory"]["negative_group_count"] == expected["negative_group_count"]
    assert rendered_manifest["inventory"]["negative_case_count"] == expected["negative_case_count"]
    assert m0_inventory == expected
    assert validate_m0_delivery.contract_inventory() == expected
    assert fresh_m0_review.v1_contract_inventory() == expected


def test_contract_golden_and_m0_open_are_projected_without_checkout_root() -> None:
    contract = generate_m0_delivery.build_contract_golden_manifest()
    parity = generate_m0_delivery.read_json(generate_m0_delivery.DELIVERY / "parity-ledger.json")
    identity = generate_m0_delivery.read_json(
        generate_m0_delivery.EVIDENCE / "m0.2-identity-runtime.json"
    )
    m0_open = generate_m0_delivery.build_m0_open_manifest(contract, parity, identity)
    serialized = json.dumps(m0_open, ensure_ascii=False, sort_keys=True)

    assert contract["inventory"] == projection.v1_contract_inventory()
    assert str(generate_m0_delivery.ROOT) not in serialized
    assert m0_open["identity"]["worktree"] == "."
    assert m0_open["verification"]["contract_manifest"]["path"] == "contracts/manifest-v1.json"
    assert m0_open["verification"]["contract_golden_delivery"]["path"] == (
        "docs/deliveries/PPA-00/contract-golden-manifest.json"
    )


def test_v1_inventory_filters_future_v2_fixture_and_example_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contracts = tmp_path / "contracts"
    schemas = contracts / "json-schema"
    fixtures = contracts / "examples" / "fixtures"
    negatives = contracts / "corpus" / "negative" / "84.13"
    for directory in (schemas, fixtures, negatives):
        directory.mkdir(parents=True, exist_ok=True)
    (schemas / "example-v1.schema.json").write_text("{}", encoding="utf-8")
    (schemas / "example-v2.schema.json").write_text("{}", encoding="utf-8")
    (fixtures / "positive.json").write_text("{}", encoding="utf-8")
    (fixtures / "future.v2.json").write_text("{}", encoding="utf-8")
    (contracts / "examples" / "combination.json").write_text("{}", encoding="utf-8")
    (contracts / "examples" / "combination.v2.json").write_text("{}", encoding="utf-8")
    (negatives / "01.json").write_text(
        json.dumps({"group_id": "84.13-01", "negative": []}), encoding="utf-8"
    )

    monkeypatch.setattr(projection, "ROOT", tmp_path)
    monkeypatch.setattr(projection, "CONTRACTS", contracts)
    monkeypatch.setattr(projection, "SCHEMAS", schemas)
    monkeypatch.setattr(projection, "V1_NEGATIVE", negatives)

    inventory = projection.v1_contract_inventory()
    assert inventory["schema_count"] == 1
    assert inventory["positive_fixture_count"] == 1
    assert inventory["combination_example_count"] == 1


def test_v2_manifest_binds_v1_rendered_in_the_same_invocation() -> None:
    v1 = generate_contract_manifest.render()
    expected_hash = generate_contract_manifest.hashlib.sha256(
        generate_contract_manifest.manifest_bytes(v1)
    ).hexdigest()

    assert generate_contract_manifest.render_v2()["v1_immutable"]["manifest_sha256"] == expected_hash


def test_contract_document_renderer_covers_the_complete_v1_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: dict[str, str] = {}

    def capture(path: Path, content: str) -> None:
        rendered[generate_m0_delivery.rel(path)] = content

    monkeypatch.setattr(generate_m0_delivery, "write_text", capture)
    generate_m0_delivery.render_contract_docs()

    schema_map = rendered["docs/contracts/schema-map.md"]
    for path in projection.contract_schema_paths("v1"):
        contract_id = path.name.removesuffix(".schema.json")
        assert f"`{contract_id}`" in schema_map
