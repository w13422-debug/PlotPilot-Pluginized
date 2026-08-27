from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from plotpilot_plugin_sdk import ContractError


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "first-party-plugins" / "prompt-skill-runtime" / "src"))
from plotpilot_prompt_skill_runtime.package import calculate_skill_identity
from plotpilot_prompt_skill_runtime.versions import ActiveVersion, protect_active_version
from plotpilot_plugin_sdk.verifier import verify_skill_chain, verify_skill_receipt


def fixture(name: str) -> dict:
    return json.loads((ROOT / "contracts" / "examples" / "fixtures" / name).read_text(encoding="utf-8"))


def test_order_freeze_and_active_version_protection() -> None:
    current = ActiveVersion("user-v1", "a" * 64, source="user", release_id="a" * 64)
    incoming = ActiveVersion("sync-v2", "b" * 64, source="system", release_id="b" * 64)
    decision = protect_active_version(current, incoming)
    assert decision.active is current
    assert decision.changed is False
    golden_root = ROOT / "contracts" / "golden" / "skill"
    files = {name: (golden_root / name).read_bytes().replace(b"\r\n", b"\n") for name in ("prompt.txt", "skill.json")}
    golden = calculate_skill_identity(files, "com.plotpilot.skill.golden", "1.0.0")
    assert golden.package_hash == "5cf3df6acea3c792ee36fa3c0c5757f87c8219aba88cfc568bd4a67738983b7f"


def test_golden_chain_and_tamper_fail_closed() -> None:
    receipt = fixture("skill-run-receipt.json")
    chain = fixture("skill-chain-result.json")
    verify_skill_receipt(receipt)
    verify_skill_chain(chain, [receipt])
    tampered = copy.deepcopy(receipt)
    tampered["frozen"] = False
    with pytest.raises(ContractError):
        verify_skill_chain(chain, [tampered])
