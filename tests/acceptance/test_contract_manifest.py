from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_all  # noqa: E402


def test_m0_contract_gate_is_green() -> None:
    summary = verify_all()
    assert summary["schemas"]["schemas"] == 61
    assert summary["schemas"]["v1_schemas"] == 55
    assert summary["schemas"]["v2_schemas"] == 6
    assert len(summary["negative"]) == 14
    assert summary["v2_public_surface"]["routes"] == 19
    assert summary["v2_public_surface"]["http_exchanges"] == 19
    assert summary["v2_public_surface"]["negative_cases"] == 44
