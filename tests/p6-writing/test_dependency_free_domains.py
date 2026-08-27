from __future__ import annotations

from hashlib import sha256

import pytest

from plotpilot_autopilot import Stage, build_plan, next_stage
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


def test_autopilot_requires_explicit_enable_and_preserves_baseline_order() -> None:
    with pytest.raises(ValueError, match="explicit"):
        build_plan(enabled=False)
    plan = build_plan(enabled=True)
    observed = [plan.current]
    while observed[-1] is not Stage.COMPLETED:
        plan = build_plan(enabled=True, current=next_stage(plan))
        observed.append(plan.current)
    assert observed == [Stage.MACRO_PLANNING, Stage.ACT_PLANNING, Stage.WRITING, Stage.AUDITING, Stage.COMPLETED]


def test_quality_only_returns_findings_and_never_changes_text() -> None:
    body = "与此同时，他仿佛落叶，好像孤舟。这意味着危险临近。"
    before = body
    findings = scan_language_style(body)
    assert body == before
    assert [finding.rule_id for finding in findings] == [
        "style.cliche-transition",
        "style.excessive-simile",
        "style.over-rationalized",
    ]
    assert all(finding.source_hash == sha256(body.encode()).hexdigest() for finding in findings)
