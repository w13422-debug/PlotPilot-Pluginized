"""Generate the content-addressed P0 contract/golden/corpus manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

if __package__:
    from .contract_inventory import (
        contract_schema_paths,
        is_v2_contract_path,
        v1_contract_inventory,
    )
else:
    from contract_inventory import (
        contract_schema_paths,
        is_v2_contract_path,
        v1_contract_inventory,
    )


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
OUTPUT = CONTRACTS / "manifest-v1.json"
OUTPUT_V2 = CONTRACTS / "manifest-v2.json"
FINDING_CLOSURE = ROOT / "docs" / "contracts" / "finding-closure-v1.json"
DESIGN_PATH = r"C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized正式设计规划-2026-08-25-v1.md"
DESIGN_SHA256 = "e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b"
FAMILY_IDS = [
    "84.1-common-jcs",
    "84.2-manifest-capability-data",
    "84.3-rpc-envelope-method-matrix",
    "84.4-run-snapshot-request-key",
    "84.5-result-candidate-provenance",
    "84.6-event-sse-checkpoint-stream",
    "84.7-plan-generation-settings-lifecycle",
    "84.8-compatibility-history",
    "84.9-backup-restore",
    "84.10-plugin-ui-wire",
    "84.11-skill-package-receipt",
    "84.12-job-broker-outcome",
    "84.13-negative-golden",
    "84.14-finding-closure",
    "core-authority-command-query/v1",
    "publication-command-result/v1",
    "asset-metadata/v1",
    "plugin-ui-ingress-validator/v1",
    "operation-context-identity/v1",
    "export-current-revisions/v1",
    "core-http-request-error/v1",
    "core-http-request-failure-policy/v1",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_v2_path(path: Path) -> bool:
    """Compatibility wrapper around the single inventory path classifier."""

    return is_v2_contract_path(path)


def file_records(*, include_v2: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in (CONTRACTS / "json-schema", CONTRACTS / "examples", CONTRACTS / "golden", CONTRACTS / "corpus"):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                if not include_v2 and _is_v2_path(path):
                    continue
                records.append({"path": relative(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
    # This is a checked-in runtime contract rather than a schema/example/golden
    # or corpus fixture.  Keep the explicit record here so the Unicode identity
    # rule is content-addressed by the same manifest consumed at runtime.
    for explicit_contract in (CONTRACTS / "unicode-casefold-v1.json", CONTRACTS / ".gitattributes"):
        if explicit_contract.is_file():
            records.append({
                "path": relative(explicit_contract),
                "bytes": explicit_contract.stat().st_size,
                "sha256": sha256(explicit_contract),
            })
    return sorted(records, key=lambda item: item["path"].encode("utf-8"))


def schema_records(*, include_v2: bool = False) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    scope = "all" if include_v2 else "v1"
    for path in contract_schema_paths(scope):
        schema = json.loads(path.read_text(encoding="utf-8"))
        records.append({"contract_id": path.name.removesuffix(".schema.json"), "schema_id": schema.get("$id"), "path": relative(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
    return records


def negative_records() -> list[dict[str, Any]]:
    records = []
    for path in sorted((CONTRACTS / "corpus" / "negative" / "84.13").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            {
                "group_id": value["group_id"],
                "path": relative(path),
                "sha256": sha256(path),
                "negative_case_count": len(value["negative"]),
                "positive_fixture_ids": value["positive"],
            }
        )
    return records


def v2_negative_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((CONTRACTS / "corpus" / "m4-m5-public-surface-v2").glob("*.json")):
        if path.name == "manifest.json":
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            {
                "group_id": value["group_id"],
                "path": relative(path),
                "sha256": sha256(path),
                "negative_case_count": len(value["negative"]),
                "positive_fixture_ids": value["positive"],
            }
        )
    return records


def prompt_skill_negative_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    root = CONTRACTS / "corpus" / "prompt-skill-rpc-v2"
    for path in sorted(root.glob("*.json")):
        if path.name == "manifest.json":
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            {
                "group_id": value["group_id"],
                "path": relative(path),
                "sha256": sha256(path),
                "negative_case_count": len(value["negative"]),
                "positive_fixture_ids": value["positive"],
            }
        )
    return records


def golden_vectors() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("package", "skill", "run-snapshot", "backup", "contract-publication-v1", "core-http-request-failure-v1"):
        expected_path = CONTRACTS / "golden" / name / "expected.json"
        if expected_path.exists():
            values[name] = json.loads(expected_path.read_text(encoding="utf-8"))
        elif name == "backup":
            backup_path = CONTRACTS / "golden" / name / "backup.json"
            values[name] = {"bundle_hash": json.loads(backup_path.read_text(encoding="utf-8"))["bundle_hash"]}
    return values


def v2_golden_vectors() -> dict[str, Any]:
    expected_path = CONTRACTS / "golden" / "m4-m5-public-surface-v2" / "expected.json"
    if not expected_path.exists():
        return {}
    return json.loads(expected_path.read_text(encoding="utf-8"))


def prompt_skill_golden_vectors() -> dict[str, Any]:
    expected_path = CONTRACTS / "golden" / "prompt-skill-rpc-v2" / "expected.json"
    if not expected_path.exists():
        return {}
    return json.loads(expected_path.read_text(encoding="utf-8"))


def render() -> dict[str, Any]:
    """Render the frozen v1 manifest; v2 files are intentionally excluded."""

    inventory = v1_contract_inventory()
    schemas = schema_records()
    if len(schemas) != inventory["schema_count"]:
        raise ValueError("v1 schema projection drift")
    files = file_records()
    closure = json.loads(FINDING_CLOSURE.read_text(encoding="utf-8"))
    closure_ids = [item["finding_id"] for item in closure.get("findings", [])]
    return {
        "schema": "plotpilot-contract-manifest/v1",
        "contract_version": "1.2.0",
        "source": {"formal_design": DESIGN_PATH, "version": "v1.2", "sha256": DESIGN_SHA256},
        "contract_families": FAMILY_IDS,
        "inventory": {
            "schema_count": inventory["schema_count"],
            "file_count_excluding_manifest": len(files),
            "negative_group_count": inventory["negative_group_count"],
            "negative_case_count": inventory["negative_case_count"],
        },
        "schemas": schemas,
        "golden_vectors": golden_vectors(),
        "negative_groups": negative_records(),
        "finding_closure": {
            "path": relative(FINDING_CLOSURE),
            "sha256": sha256(FINDING_CLOSURE),
            "finding_count": closure.get("finding_count"),
            "ids": closure_ids,
        },
        "files": files,
    }


def manifest_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def render_v2() -> dict[str, Any]:
    """Render an additive manifest containing v1 plus the v2 surface."""

    v1 = render()
    v1_inventory = v1_contract_inventory()
    schemas = schema_records(include_v2=True)
    v2_schemas = contract_schema_paths("v2")
    if len(schemas) != v1_inventory["schema_count"] + len(v2_schemas):
        raise ValueError("v1/v2 schema projection partition drift")
    files = file_records(include_v2=True)
    v2_groups = v2_negative_records()
    prompt_skill_groups = prompt_skill_negative_records()
    prompt_skill_expected = prompt_skill_golden_vectors()
    v1_manifest_bytes = manifest_bytes(v1)
    return {
        "schema": "plotpilot-contract-manifest/v2",
        "contract_version": "2.0.0",
        "source": {
            "formal_design": DESIGN_PATH,
            "version": "v1.2-plus-adr-044",
            "sha256": DESIGN_SHA256,
            "adr": "docs/contracts/adr-044-m4-m5-public-surface-v2.md",
            "adjudication": "coordination/PPA-00/post-e0/public-surface-adjudication-v2.json",
        },
        "v1_immutable": {
            "manifest_path": relative(OUTPUT),
            "manifest_sha256": hashlib.sha256(v1_manifest_bytes).hexdigest(),
            "schema_count": v1["inventory"]["schema_count"],
            "file_count_excluding_manifest": v1["inventory"]["file_count_excluding_manifest"],
            "negative_group_count": v1["inventory"]["negative_group_count"],
        },
        "inventory": {
            "schema_count": v1_inventory["schema_count"] + len(v2_schemas),
            "v1_schema_count": v1_inventory["schema_count"],
            "v2_schema_count": len(v2_schemas),
            "file_count_excluding_manifest": len(files),
            "v1_file_count": len(file_records()),
            "v2_file_count": len(files) - len(file_records()),
            "negative_group_count_v1": len(negative_records()),
            "negative_group_count_v2": len(v2_groups),
            "negative_case_count_v2": sum(item["negative_case_count"] for item in v2_groups),
            "prompt_skill_schema_count": 3,
            "prompt_skill_negative_group_count_v2": len(prompt_skill_groups),
            "prompt_skill_negative_case_count_v2": sum(item["negative_case_count"] for item in prompt_skill_groups),
        },
        "contract_families": [
            "candidate/v2",
            "candidate-review/v2",
            "publication-command/v2",
            "story-state-projection-input/v2",
            "job-http/v2",
            "plugin-lifecycle/v2",
        ],
        "schemas": schemas,
        "golden_vectors": {"v1": golden_vectors(), "v2": v2_golden_vectors(), "prompt_skill_v2": prompt_skill_expected},
        "negative_groups": {"v1": negative_records(), "v2": v2_groups, "prompt_skill_v2": prompt_skill_groups},
        "prompt_skill_v2": {
            "schema_count": 3,
            "golden": prompt_skill_expected,
            "negative_groups": prompt_skill_groups,
            "negative_group_count": len(prompt_skill_groups),
            "negative_case_count": sum(item["negative_case_count"] for item in prompt_skill_groups),
        },
        "files": files,
    }


def _render_bytes(version: str) -> tuple[Path, bytes]:
    if version == "v1":
        return OUTPUT, manifest_bytes(render())
    if version == "v2":
        return OUTPUT_V2, manifest_bytes(render_v2())
    raise ValueError(f"unknown manifest version: {version}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--v2", action="store_true", help="generate/check only manifest-v2.json")
    parser.add_argument("--all", action="store_true", help="generate/check both v1 and v2 manifests")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    versions = ["v2"] if args.v2 else (["v1", "v2"] if args.all else ["v1"])
    for version in versions:
        default_path, rendered = _render_bytes(version)
        output = args.output if args.output != OUTPUT and len(versions) == 1 else default_path
        if args.check:
            if not output.exists() or output.read_bytes() != rendered:
                print(f"contract manifest drift: {output}")
                return 1
            print(f"contract manifest {version} is deterministic ({len(json.loads(rendered)['files'])} files)")
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(rendered)
        print(f"generated contract manifest {version}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
