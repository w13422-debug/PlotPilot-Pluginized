from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_negative_groups  # noqa: E402


@pytest.mark.integration
def test_84_13_all_fourteen_groups_execute() -> None:
    result = verify_negative_groups()
    assert tuple(result) == tuple(f"84.13-{index:02d}" for index in range(1, 15))
    assert all(count > 0 for count in result.values())
