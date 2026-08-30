import pytest
from plotpilot_story_state import StatePayload, failed_items_for_retry

from .support import proposal


@pytest.mark.parametrize(
    "kind,entity_id",
    [
        ("bible", "bible-1"),
        ("character", "character-1"),
        ("relationship", "relationship-1"),
        ("world", "world-1"),
        ("item", "item-1"),
        ("foreshadowing", "foreshadowing-1"),
        ("story_evolution", "evolution-1"),
    ],
)
def test_closed_payload_profiles_round_trip(kind, entity_id):
    value = proposal(kind=kind, entity_id=entity_id).payload
    parsed = StatePayload.from_mapping(value)
    assert StatePayload.from_mapping(parsed.to_dict()) == parsed


def test_unknown_missing_and_invalid_enum_payload_fields_fail_closed():
    value = proposal().payload
    value = {**value, "unknown": 1}
    with pytest.raises(ValueError, match="top-level"):
        StatePayload.from_mapping(value)
    value = StatePayload.from_mapping(proposal().payload).to_dict()
    value["body"] = {**value["body"], "status": "invented"}
    with pytest.raises(ValueError, match="invalid character status"):
        StatePayload.from_mapping(value)
    value = StatePayload.from_mapping(proposal().payload).to_dict()
    value["body"].pop("name")
    with pytest.raises(ValueError, match="fields are not closed"):
        StatePayload.from_mapping(value)


def test_retry_converges_by_latest_terminal_state():
    first_failure = proposal(outcome="failure", error="temporary", retry_id="try-1", terminal_seq=1)
    second_failure = proposal(outcome="failure", error="again", retry_id="try-2", terminal_seq=2)
    success = proposal(retry_id="try-3", terminal_seq=3)
    assert failed_items_for_retry((first_failure, second_failure), retry_id="try-3") == (
        ("operation-story-1", "item-story-1", "try-3"),
    )
    assert failed_items_for_retry((first_failure, second_failure, success), retry_id="try-4") == ()


def test_retry_rejects_duplicate_gap_and_identity_reuse():
    first = proposal(outcome="failure", error="temporary", retry_id="try-1", terminal_seq=1)
    duplicate = proposal(outcome="failure", error="again", retry_id="try-2", terminal_seq=1)
    with pytest.raises(ValueError, match="terminal_seq"):
        failed_items_for_retry((first, duplicate), retry_id="try-3")
    gap = proposal(outcome="failure", error="again", retry_id="try-2", terminal_seq=3)
    with pytest.raises(ValueError, match="continuous"):
        failed_items_for_retry((first, gap), retry_id="try-4")
    with pytest.raises(ValueError, match="advance"):
        failed_items_for_retry((first,), retry_id="try-1")
