from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "integration"))

from plotpilot_plugin_sdk.core_api_v2 import (  # noqa: E402
    parse_candidate_query_result_v2,
    parse_candidate_v2,
    parse_job_sse_recovery_v2,
    validate_publication_v2,
    validate_story_state_projection_v2,
)
from plotpilot_plugin_sdk.errors import ContractError  # noqa: E402
from plotpilot_plugin_sdk.m4_m5_http_v2 import OperationKeyLedgerV2, validate_http_exchange  # noqa: E402
from verify_contracts import verify_v2_public_surface  # noqa: E402


GOLDEN = ROOT / "contracts" / "golden" / "m4-m5-public-surface-v2"


def read(name: str) -> dict[str, object]:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def test_v2_public_surface_gate_is_complete() -> None:
    result = verify_v2_public_surface()
    assert result == {
        "corpus_groups": 5,
        "cursor_domains": ["candidate", "core", "job"],
        "golden_files": 7,
        "negative_case_digest": "7fc220efb8a88f449404e28fdcc7735a397c0f949268df1eefdf966597a952b1",
        "negative_cases": 44,
        "http_exchanges": 19,
        "publication_path": "publication.accept",
        "routes": 19,
        "schemas": 6,
    }


def test_candidate_workspace_write_set_base_and_partial_rules_are_fail_closed() -> None:
    candidate_doc = read("candidate.json")
    candidate = candidate_doc["candidate"]
    parse_candidate_v2(candidate)

    cross_workspace = copy.deepcopy(candidate)
    cross_workspace["target"]["workspace_id"] = "ws-other"
    with pytest.raises(ContractError):
        parse_candidate_v2(cross_workspace)

    bad_base = copy.deepcopy(candidate)
    bad_base["base"]["content_hash"] = "b" * 64
    with pytest.raises(ContractError):
        parse_candidate_v2(bad_base)

    publication = read("publication.json")
    partial = candidate_doc["candidate_partial"]
    partial_result = copy.deepcopy(publication["result_complete"])
    partial_result.update(
        {
            "publication_operation_key": "publication-op-partial-v2",
            "candidate_id": partial["candidate_id"],
            "content_hash": partial["mutation"]["payload_hash"],
        }
    )
    with pytest.raises(ContractError):
        validate_publication_v2(
            publication["command_partial"],
            partial_result,
            candidate=partial,
            expected_workspace_id="ws-1",
        )


def test_candidate_preview_binds_exact_returned_bytes_and_payload_hash() -> None:
    candidate_doc = read("candidate.json")
    preview = candidate_doc["candidate_preview_result"]
    parse_candidate_query_result_v2(preview)

    for path, value in ((["base64_chunk"], "not-base64"), (["content_hash"], "0" * 64), (["payload_hash"], "0" * 64)):
        bad = copy.deepcopy(preview)
        bad[path[0]] = value
        with pytest.raises(ContractError):
            parse_candidate_query_result_v2(bad)


def test_publication_operation_key_is_idempotent_but_payload_reuse_is_rejected() -> None:
    publication = read("publication.json")
    ledger = OperationKeyLedgerV2()
    first = ledger.record("publication.accept", publication["command_complete"], 200, publication["result_complete"])
    replay = ledger.replay("publication.accept", publication["command_complete"])
    assert first[0] == replay[0] == 200
    assert replay[2] is True
    different = copy.deepcopy(publication["command_complete"])
    different["candidate_id"] = "candidate-other"
    with pytest.raises(ContractError):
        ledger.record("publication.accept", different, 200, publication["result_complete"])

    drifted_response = copy.deepcopy(publication["result_complete"])
    drifted_response["idempotent"] = True
    with pytest.raises(ContractError):
        ledger.record("publication.accept", publication["command_complete"], 200, drifted_response)


def test_every_v2_http_route_has_a_bound_success_exchange() -> None:
    http = read("http.json")
    candidates = {item["candidate_id"]: item for item in (read("candidate.json")["candidate"], read("candidate.json")["candidate_partial"], read("candidate.json")["candidate_incomplete_stream"])}
    assert http["exchange_count"] == 19
    assert len({item["route_id"] for item in http["exchanges"]}) == 19
    for exchange in http["exchanges"]:
        validate_http_exchange(
            exchange["route_id"],
            exchange["request"],
            exchange["status"],
            exchange["response"],
            candidate=candidates.get(exchange["request"].get("candidate_id")),
        )


def test_projection_receipt_and_job_sse_closures_are_bound() -> None:
    story = read("story-state.json")["projection"]
    validate_story_state_projection_v2(story, expected_workspace_id="ws-1")
    missing_receipt = copy.deepcopy(story)
    missing_receipt["receipt_closure"] = []
    with pytest.raises(ContractError):
        validate_story_state_projection_v2(missing_receipt)

    job = read("job.json")
    parse_job_sse_recovery_v2(job["sse_replay"])
    gap = copy.deepcopy(job["sse_gap"])
    gap["snapshot_required"] = False
    with pytest.raises(ContractError):
        parse_job_sse_recovery_v2(gap)
