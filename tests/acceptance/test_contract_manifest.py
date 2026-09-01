from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_all  # noqa: E402


def test_m0_contract_gate_is_green() -> None:
    summary = verify_all()
    assert summary["schemas"]["schemas"] == 64
    assert summary["schemas"]["v1_schemas"] == 55
    assert summary["schemas"]["v2_schemas"] == 9
    assert len(summary["negative"]) == 14
    assert summary["v2_public_surface"]["routes"] == 19
    assert summary["v2_public_surface"]["http_exchanges"] == 19
    assert summary["v2_public_surface"]["negative_cases"] == 62
    assert summary["prompt_skill_rpc_v2"]["schema_count"] == 3
    assert summary["prompt_skill_rpc_v2"]["corpus_groups"] == 1
    assert summary["prompt_skill_rpc_v2"]["negative_cases"] > 25
    manifest_v1 = ROOT / "contracts" / "manifest-v1.json"
    manifest_v2 = ROOT / "contracts" / "manifest-v2.json"
    assert hashlib.sha256(manifest_v1.read_bytes()).hexdigest() == "dff5bfc05b14d8d626b79c31f6ef4ef4cfc166f729af06d9c73f6e6300811bc4"
    assert hashlib.sha256(manifest_v2.read_bytes()).hexdigest() != "b25f74bf3e28573fd5499b6a0988cba04228707d694b6c1cf8d3f663913fe71a"
    inventory = json.loads(manifest_v2.read_text(encoding="utf-8"))["inventory"]
    assert (inventory["schema_count"], inventory["v1_schema_count"], inventory["v2_schema_count"]) == (64, 55, 9)
    assert (inventory["file_count_excluding_manifest"], inventory["v1_file_count"], inventory["v2_file_count"]) == (170, 141, 29)
    assert (inventory["negative_group_count_v2"], inventory["negative_case_count_v2"]) == (5, 62)
    assert (inventory["prompt_skill_negative_group_count_v2"], inventory["prompt_skill_negative_case_count_v2"]) == (1, summary["prompt_skill_rpc_v2"]["negative_cases"])
