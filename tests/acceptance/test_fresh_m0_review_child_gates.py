from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.integration import fresh_m0_review as fresh_review

CHILD_GATE_IDS = (
    "python-contracts-all",
    "cross-language-goldens",
    "node-contracts-all",
    "delivery-record-check",
    "merge-gate",
)


def _merge_stdout() -> str:
    return json.dumps(
        {
            "out_of_set_paths": [],
            "p1_p6_absent": True,
            "donor_local_push": "DISABLED",
            "creation_gate": {},
            "changed_paths": [],
        }
    )


def _child_result(label: str, *, exit_code: int = 0, status: str = "passed", stdout: str | None = None) -> dict[str, object]:
    return {
        "id": label,
        "command": f"fake {label}",
        "exit_code": exit_code,
        "status": status,
        "stdout": {"path": f"raw/{label}.stdout.txt", "bytes": 0, "sha256": "0" * 64},
        "stderr": {"path": f"raw/{label}.stderr.txt", "bytes": 0, "sha256": "0" * 64},
        "_stdout": _merge_stdout() if label == "merge-gate" and stdout is None else (stdout or "{}"),
    }


def _run_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    failing_label: str | None = None,
    exit_code: int = 0,
    status: str = "passed",
    merge_stdout: str | None = None,
) -> tuple[int, dict[str, object]]:
    review_path = tmp_path / "fresh-readonly-review.json"
    monkeypatch.setattr(fresh_review, "REVIEW_PATH", review_path)
    monkeypatch.setattr(fresh_review, "RAW", tmp_path / "raw")

    # These are unit tests for child-process result propagation.  Keep them
    # independent from mutable, repository-wide M0 evidence; the actual record
    # and browser evidence checks have their own executable acceptance gates.
    monkeypatch.setattr(fresh_review, "check_records", lambda _records, _label: [])
    real_read_json = fresh_review.read_json

    def isolated_read_json(path: Path) -> dict[str, object]:
        value = real_read_json(path)
        if path == fresh_review.DELIVERY / "evidence" / "browser-smoke.json":
            subflows = [
                {
                    "flow_id": "FLOW-07-foreshadow-ledger",
                    "status": "passed",
                    "exercised": True,
                    "ui_actions": [{}],
                    "api_trace": [{}],
                    "screenshots": [{}],
                },
                {
                    "flow_id": "FLOW-08-story-evolution-bible",
                    "status": "passed",
                    "exercised": True,
                    "ui_actions": [{}],
                    "api_trace": [{}],
                    "screenshots": [{}],
                },
            ]
            return {
                "flows": [{"subflows": subflows}] + [{} for _ in range(9)],
                "api_trace": [{}],
                "page_errors": [],
                "forbidden_generation_calls": [],
                "unexpected_external_calls": [],
                "constraints": {
                    "non_empty_chapter_body_saved": True,
                    "fake_provider_used": True,
                    "real_provider_used": False,
                    "external_network_used": False,
                    "sse_disconnect_recovery": True,
                },
            }
        if path == fresh_review.DELIVERY / "evidence" / "finding-closure.json":
            closure_path = fresh_review.ROOT / "docs" / "contracts" / "finding-closure-v1.json"
            value = dict(value)
            value["closure_index"] = {
                "path": "docs/contracts/finding-closure-v1.json",
                "bytes": closure_path.stat().st_size,
                "sha256": fresh_review.sha256_file(closure_path),
            }
        return value

    monkeypatch.setattr(fresh_review, "read_json", isolated_read_json)

    def fake_run(_command: list[str], label: str) -> dict[str, object]:
        if label == failing_label:
            return _child_result(label, exit_code=exit_code, status=status, stdout=merge_stdout)
        return _child_result(label)

    monkeypatch.setattr(fresh_review, "run", fake_run)
    return_code = fresh_review.main()
    return return_code, json.loads(review_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("child_id", CHILD_GATE_IDS)
def test_each_nonzero_child_exit_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, child_id: str
) -> None:
    return_code, review = _run_review(
        monkeypatch,
        tmp_path,
        failing_label=child_id,
        exit_code=37,
        status="passed",
    )

    assert return_code != 0
    assert review["verdict"] == "failed"
    matching = [
        finding
        for finding in review["findings"]
        if finding.get("id") == "F-REVIEW-CHILD-GATE" and finding.get("command_id") == child_id
    ]
    assert len(matching) == 1
    assert matching[0]["exit_code"] == 37
    assert matching[0]["status"] == "passed"


@pytest.mark.parametrize("child_id", CHILD_GATE_IDS)
def test_each_nonpassed_child_status_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, child_id: str
) -> None:
    return_code, review = _run_review(
        monkeypatch,
        tmp_path,
        failing_label=child_id,
        exit_code=0,
        status="failed",
    )

    assert return_code != 0
    assert review["verdict"] == "failed"
    matching = [
        finding
        for finding in review["findings"]
        if finding.get("id") == "F-REVIEW-CHILD-GATE" and finding.get("command_id") == child_id
    ]
    assert len(matching) == 1
    assert matching[0]["exit_code"] == 0
    assert matching[0]["status"] == "failed"


def test_all_passed_children_preserve_success_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    return_code, review = _run_review(monkeypatch, tmp_path)

    assert return_code == 0
    assert review["verdict"] == "passed_pre_commit"
    assert review["findings"] == []
    assert review["observations"]["child_gate_failures"] == []


def test_failed_merge_gate_with_non_json_stdout_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    return_code, review = _run_review(
        monkeypatch,
        tmp_path,
        failing_label="merge-gate",
        exit_code=37,
        status="failed",
        merge_stdout="not-json",
    )

    assert return_code != 0
    assert review["verdict"] == "failed"
    matching = [
        finding
        for finding in review["findings"]
        if finding.get("id") == "F-REVIEW-CHILD-GATE" and finding.get("command_id") == "merge-gate"
    ]
    assert len(matching) == 1
    assert matching[0]["exit_code"] == 37
    assert matching[0]["status"] == "failed"
