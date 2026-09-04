"""Pure Autopilot plans plus a closed, host-executed durable DAG description.

The DAG in this module is intentionally only a deterministic description.  It
contains no database, process registry, checkpoint ledger, or execution loop;
the P3 Host remains the sole authority for every durable operation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_RESULT_CONTRACTS = frozenset(
    {"candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"}
)
DEFAULT_MAX_DAG_STAGES = 64


class DAGValidationError(ValueError):
    """Raised before a malformed DAG can reach a Host capability."""


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DAGValidationError(f"DAG value is not deterministic JSON: {exc}") from exc


def _hash(prefix: str, value: object) -> str:
    return sha256(
        prefix.encode("ascii") + b"\n" + _canonical_json_bytes(value)
    ).hexdigest()


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DAGValidationError(f"{field} must be a PlotPilot identifier")
    return value


@dataclass(frozen=True, slots=True)
class DurableStage:
    """One Host-bound durable unit in an Autopilot DAG.

    The three asset/binding identities are supplied by the frozen parent job.
    This plugin only forwards them through the public Host capability port; it
    never substitutes an asset, resolves a release, or starts a local worker.
    """

    stage_id: str
    binding_id: str
    input_asset_id: str
    parameters_asset_id: str | None = None
    expected_result_contract: str = "candidate-batch/v1"
    propagate_cancel: bool = True
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.stage_id, "stage_id")
        _require_identifier(self.binding_id, "binding_id")
        _require_identifier(self.input_asset_id, "input_asset_id")
        if self.parameters_asset_id is not None:
            _require_identifier(self.parameters_asset_id, "parameters_asset_id")
        if self.expected_result_contract not in _RESULT_CONTRACTS:
            raise DAGValidationError(
                "expected_result_contract is outside the frozen Host enum"
            )
        if type(self.propagate_cancel) is not bool:
            raise DAGValidationError("propagate_cancel must be boolean")
        if not isinstance(self.depends_on, tuple):
            raise DAGValidationError("depends_on must be an immutable tuple")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise DAGValidationError("depends_on cannot contain duplicates")
        for dependency in self.depends_on:
            _require_identifier(dependency, "depends_on item")
            if dependency == self.stage_id:
                raise DAGValidationError("a stage cannot depend on itself")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "depends_on": sorted(self.depends_on),
            "expected_result_contract": self.expected_result_contract,
            "input_asset_id": self.input_asset_id,
            "parameters_asset_id": self.parameters_asset_id,
            "propagate_cancel": self.propagate_cancel,
            "stage_id": self.stage_id,
        }


@dataclass(frozen=True, slots=True)
class DurableDAG:
    """A topologically ordered, immutable Host execution description."""

    stages: tuple[DurableStage, ...]
    max_stages: int = DEFAULT_MAX_DAG_STAGES
    _ordered_stage_ids: tuple[str, ...] = field(init=False, repr=False)
    dag_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.stages, tuple):
            raise DAGValidationError("stages must be an immutable tuple")
        if (
            type(self.max_stages) is not int
            or not 1 <= self.max_stages <= DEFAULT_MAX_DAG_STAGES
        ):
            raise DAGValidationError(
                "max_stages is outside the configured durable DAG boundary"
            )
        if len(self.stages) > self.max_stages:
            raise DAGValidationError(
                "DAG exceeds its configured maximum stage boundary"
            )
        if any(not isinstance(stage, DurableStage) for stage in self.stages):
            raise DAGValidationError("DAG stages must be DurableStage values")

        by_id = {stage.stage_id: stage for stage in self.stages}
        if len(by_id) != len(self.stages):
            raise DAGValidationError("DAG stage IDs must be unique")
        for stage in self.stages:
            missing = sorted(set(stage.depends_on) - set(by_id))
            if missing:
                raise DAGValidationError(
                    f"stage {stage.stage_id} has missing dependencies: {', '.join(missing)}"
                )

        indegree = {stage.stage_id: len(stage.depends_on) for stage in self.stages}
        children: dict[str, list[str]] = {stage.stage_id: [] for stage in self.stages}
        for stage in self.stages:
            for dependency in stage.depends_on:
                children[dependency].append(stage.stage_id)
        ready = sorted(stage_id for stage_id, degree in indegree.items() if degree == 0)
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()
        if len(ordered) != len(self.stages):
            raise DAGValidationError("DAG contains a cycle")

        object.__setattr__(self, "_ordered_stage_ids", tuple(ordered))
        payload = {
            "schema": "autopilot-dag/v1",
            "stages": [by_id[stage_id].to_dict() for stage_id in ordered],
        }
        object.__setattr__(self, "dag_hash", _hash("autopilot-dag/v1", payload))

    @property
    def ordered_stage_ids(self) -> tuple[str, ...]:
        return self._ordered_stage_ids

    @property
    def ordered_stages(self) -> tuple[DurableStage, ...]:
        by_id = {stage.stage_id: stage for stage in self.stages}
        return tuple(by_id[stage_id] for stage_id in self._ordered_stage_ids)

    def stage(self, stage_id: str) -> DurableStage:
        _require_identifier(stage_id, "stage_id")
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise DAGValidationError(f"unknown DAG stage: {stage_id}")

    def to_dict(self) -> dict[str, object]:
        return {
            "dag_hash": self.dag_hash,
            "schema": "autopilot-dag/v1",
            "stages": [stage.to_dict() for stage in self.ordered_stages],
        }


def build_durable_dag(
    stages: Iterable[DurableStage] = (), *, max_stages: int = DEFAULT_MAX_DAG_STAGES
) -> DurableDAG:
    """Freeze and validate a Host-bound DAG before any side effect is possible."""

    return DurableDAG(tuple(stages), max_stages=max_stages)


# Backwards-compatible aliases for callers that prefer the capitalized name.
AutopilotDAG = DurableDAG
AutopilotStage = DurableStage


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


def build_plan(
    *, enabled: bool, current: Stage = Stage.MACRO_PLANNING
) -> AutopilotPlan:
    if not enabled:
        raise ValueError("Autopilot requires explicit user enablement")
    if not isinstance(current, Stage):
        raise ValueError("Autopilot current stage must be a Stage")  # noqa: TRY004
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


__all__ = [
    "DEFAULT_MAX_DAG_STAGES",
    "ORDER",
    "AutopilotDAG",
    "AutopilotPlan",
    "AutopilotStage",
    "DAGValidationError",
    "DurableDAG",
    "DurableStage",
    "Stage",
    "StageOutcome",
    "build_durable_dag",
    "build_plan",
    "decide_next_stage",
]
