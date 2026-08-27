from __future__ import annotations

from hashlib import sha256

import pytest

from plotpilot_autopilot import Stage, StageOutcome, build_plan, decide_next_stage
from plotpilot_chapter_workflow import ContextSource, SkillRef, freeze_context_plan
from plotpilot_quality_suite import scan_language_style


def test_context_and_skill_chain_are_frozen_and_ordered() -> None:
    content = "上一章"
    source = ContextSource("chapter-1", "rev-7", "chapter", content, sha256(content.encode()).hexdigest())
    skills = (
        SkillRef(10, "skill.character", "rel-1", "a" * 64),
        SkillRef(20, "skill.project", "rel-2", "b" * 64),
    )
    first = freeze_context_plan("writing.chapter.draft/v1", (source,), skills)
    second = freeze_context_plan("writing.chapter.draft/v1", (source,), skills)
    assert first == second
    assert first.skills == skills
    with pytest.raises(ValueError, match="ascending"):
        freeze_context_plan("writing.chapter.draft/v1", (source,), tuple(reversed(skills)))


def test_context_fingerprint_has_no_delimiter_collision() -> None:
    content = "body"
    digest = sha256(content.encode()).hexdigest()
    left = ContextSource("c", "rev", "a:b", content, digest)
    right = ContextSource("b:c", "rev", "a", content, digest)
    assert freeze_context_plan("op", (left,), ()).fingerprint != freeze_context_plan("op", (right,), ()).fingerprint


def test_autopilot_requires_explicit_enable_and_preserves_baseline_order() -> None:
    with pytest.raises(ValueError, match="explicit"):
        build_plan(enabled=False)
    plan = build_plan(enabled=True)
    assert decide_next_stage(plan, StageOutcome()) is Stage.ACT_PLANNING
    assert decide_next_stage(build_plan(enabled=True, current=Stage.ACT_PLANNING), StageOutcome()) is Stage.WRITING
    assert decide_next_stage(build_plan(enabled=True, current=Stage.WRITING), StageOutcome()) is Stage.AUDITING
    audit = build_plan(enabled=True, current=Stage.AUDITING)
    assert decide_next_stage(audit, StageOutcome(book_done=False)) is Stage.WRITING
    assert decide_next_stage(audit, StageOutcome(pause_gate=True)) is Stage.PAUSED_FOR_REVIEW
    assert decide_next_stage(audit, StageOutcome(book_done=True)) is Stage.COMPLETED


def test_quality_only_returns_findings_and_never_changes_text() -> None:
    body = "首先是震惊，其次是愤怒，最后是释然。他开始分析自己的感情。"
    before = body
    findings = scan_language_style(body)
    assert body == before
    assert [finding.rule_id for finding in findings] == ["style.eight_legs", "style.over_rational", "style.over_rational"]
    assert all(finding.source_hash == sha256(body.encode()).hexdigest() for finding in findings)


def test_quality_matches_donor_vectors_and_does_not_invent_transition_warning() -> None:
    assert scan_language_style("与此同时，他走了。") == ()
    assert [item.rule_id for item in scan_language_style("首先是惊讶，其次是愤怒，最后是释然。") ] == ["style.eight_legs"]
    one = "她的笑容像春天一样温暖。"
    two = one + "这里停顿许久以后。他的目光像刀锋一样锐利。"
    assert scan_language_style(one) == ()
    assert [item.rule_id for item in scan_language_style(two)] == ["style.number_metaphor"]
