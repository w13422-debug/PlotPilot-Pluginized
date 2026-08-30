from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_goldens, verify_positive_fixtures, verify_v2_public_surface  # noqa: E402


def test_design_goldens_recompute_exactly() -> None:
    result = verify_goldens()
    assert result["package_hash"] == "987e80013fe0cddd463eb8976fd75b62dbabef4f8e0e321ae6ad82a54f09b068"
    assert result["skill_package_hash"] == "5cf3df6acea3c792ee36fa3c0c5757f87c8219aba88cfc568bd4a67738983b7f"
    assert result["request_key"] == "5e346b6254626cb314a0d4040a64e8a9797c0a890537ce07e9920a911a525e61"
    assert result["snapshot_hash"] == "5a7e60677a5ced4803c96f7b7db657409051ba8f3f5b18aa8ac28978e2b4a1d2"


def test_all_positive_fixtures_are_schema_valid() -> None:
    result = verify_positive_fixtures()
    assert result["fixtures"] >= 35


def test_additive_m4_m5_v2_goldens_recompute_exactly() -> None:
    result = verify_v2_public_surface()
    assert result["routes"] == 19
    assert result["golden_files"] == 7
    assert result["http_exchanges"] == 19
    assert result["negative_cases"] == 44
    assert result["publication_path"] == "publication.accept"
