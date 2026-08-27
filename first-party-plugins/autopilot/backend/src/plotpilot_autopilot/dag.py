"""Pure, explicit-enable Autopilot stage plan; execution remains P3-owned."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    MACRO_PLANNING = "macro_planning"
    ACT_PLANNING = "act_planning"
    WRITING = "writing"
    AUDITING = "auditing"
    PAUSED_FOR_REVIEW = "paused_for_review"
    COMPLETED = "completed"


ORDER = (Stage.MACRO_PLANNING, Stage.ACT_PLANNING, Stage.WRITING, Stage.AUDITING)


@dataclass(frozen=True, slots=True)
class AutopilotPlan:
    enabled: bool
    stages: tuple[Stage, ...]
    current: Stage


@dataclass(frozen=True, slots=True)
class StageOutcome:
    succeeded: bool = True
    book_done: bool = False
    pause_gate: bool = False


def build_plan(*, enabled: bool, current: Stage = Stage.MACRO_PLANNING) -> AutopilotPlan:
    if not enabled:
        raise ValueError("Autopilot requires explicit user enablement")
    return AutopilotPlan(True, ORDER, current)


def decide_next_stage(plan: AutopilotPlan, outcome: StageOutcome) -> Stage:
    if not plan.enabled:
        raise ValueError("disabled Autopilot cannot advance")
    if not outcome.succeeded:
        return plan.current
    if plan.current is Stage.MACRO_PLANNING:
        return Stage.ACT_PLANNING
    if plan.current is Stage.ACT_PLANNING:
        return Stage.WRITING
    if plan.current is Stage.WRITING:
        return Stage.AUDITING
    if plan.current is Stage.AUDITING:
        if outcome.pause_gate:
            return Stage.PAUSED_FOR_REVIEW
        return Stage.COMPLETED if outcome.book_done else Stage.WRITING
    return plan.current
