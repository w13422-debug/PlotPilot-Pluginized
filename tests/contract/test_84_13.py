from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from verify_contracts import verify_negative_cases, verify_negative_groups  # noqa: E402


@pytest.mark.integration
def test_84_13_all_fourteen_groups_execute() -> None:
    result = verify_negative_cases()
    expected_counts = (6, 9, 9, 8, 7, 7, 11, 4, 7, 5, 7, 9, 6, 10)
    expected_groups = tuple(f"84.13-{index:02d}" for index in range(1, 15))

    assert tuple(result["groups"]) == expected_groups
    assert tuple(result["groups"][group]["case_count"] for group in expected_groups) == expected_counts
    assert result["group_count"] == 14
    assert result["case_count"] == 105
    assert result["positive_count"] == 44
    assert len(result["positive_fixture_ids"]) == 38
    assert result["registry"]["corpus_groups"] == 14
    assert result["registry"]["corpus_cases"] == 105
    assert all(item["passed"] for item in result["positive"])
    assert all(item["passed"] for group in result["groups"].values() for item in group["cases"])

    # These are the state/transaction boundaries that were previously only
    # described by a surface probe.  Their outcomes come from the executable
    # model/verifier actions, not from a fixed success marker.
    cases = {
        item["case_id"]: item
        for group in result["groups"].values()
        for item in group["cases"]
    }
    assert cases["install-crash-after-activate"]["observed_error_code"] == 1010
    assert cases["publication-after-package-delete-valid"]["outcome"] == "publication_provenance_backed"
    assert cases["aggregate-event-crash"]["observed_error_code"] == 1010
    assert cases["event-crash-after-core-write"]["observed_error_code"] == 1010
    assert cases["terminal-hydration"]["outcome"] == "terminal_view_hydrated"
    assert cases["navigation-watchdog"]["outcome"] == "navigation_acknowledged"
    assert cases["restore-new-root"]["outcome"] == "restore_target_is_distinct"
    assert cases["projection-rebuild"]["outcome"] == "projection_rebuilt"
    assert cases["unicode-casefold-collision"]["observed_error_code"] == 1005

    # Keep the legacy count-facing API covered as well; it is used by the M0
    # delivery gate and must be derived from the same complete execution.
    assert verify_negative_groups() == dict(zip(expected_groups, expected_counts))
