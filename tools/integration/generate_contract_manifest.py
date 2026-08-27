"""Generate the content-addressed P0 contract/golden/corpus manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
OUTPUT = CONTRACTS / "manifest-v1.json"
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


def file_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in (CONTRACTS / "json-schema", CONTRACTS / "examples", CONTRACTS / "golden", CONTRACTS / "corpus"):
        for path in sorted(root.rglob("*")):
            if path.is_file():
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


def schema_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((CONTRACTS / "json-schema").glob("*.schema.json")):
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


def render() -> dict[str, Any]:
    schemas = schema_records()
    files = file_records()
    closure = json.loads(FINDING_CLOSURE.read_text(encoding="utf-8"))
    closure_ids = [item["finding_id"] for item in closure.get("findings", [])]
    return {
        "schema": "plotpilot-contract-manifest/v1",
        "contract_version": "1.2.0",
        "source": {"formal_design": DESIGN_PATH, "version": "v1.2", "sha256": DESIGN_SHA256},
        "contract_families": FAMILY_IDS,
        "inventory": {
            "schema_count": len(schemas),
            "file_count_excluding_manifest": len(files),
            "negative_group_count": len(negative_records()),
            "negative_case_count": sum(item["negative_case_count"] for item in negative_records()),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    rendered = json.dumps(render(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"contract manifest drift: {args.output}")
            return 1
        print(f"contract manifest is deterministic ({len(json.loads(rendered)['files'])} files)")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"generated contract manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
