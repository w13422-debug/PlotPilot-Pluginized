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
