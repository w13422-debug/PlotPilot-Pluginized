from __future__ import annotations

import sys
from pathlib import Path

import pytest
from plotpilot_core.plugins.store import PackageStore

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from helpers import StatusSource


@pytest.fixture
def package_store(tmp_path: Path) -> PackageStore:
    return PackageStore(tmp_path / "packages")


@pytest.fixture
def empty_status_source() -> StatusSource:
    return StatusSource()
