"""Pure, explicit-enable Autopilot stage plan; execution remains P3-owned."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    MACRO_PLANNING = "macro_planning"
    ACT_PLANNING = "act_planning"
    WRITING = "writing"
    AUDITING = "auditing"
    COMPLETED = "completed"


ORDER = (Stage.MACRO_PLANNING, Stage.ACT_PLANNING, Stage.WRITING, Stage.AUDITING, Stage.COMPLETED)


@dataclass(frozen=True, slots=True)
class AutopilotPlan:
    enabled: bool
    stages: tuple[Stage, ...]
    current: Stage


def build_plan(*, enabled: bool, current: Stage = Stage.MACRO_PLANNING) -> AutopilotPlan:
    if not enabled:
        raise ValueError("Autopilot requires explicit user enablement")
    return AutopilotPlan(True, ORDER, current)


def next_stage(plan: AutopilotPlan) -> Stage:
    if not plan.enabled:
        raise ValueError("disabled Autopilot cannot advance")
    index = plan.stages.index(plan.current)
    return plan.current if index == len(plan.stages) - 1 else plan.stages[index + 1]
