"""Pure, explicit Plan-switch preparation.

Core owns ``workspace.current_plan_revision_id``.  This module therefore does
not persist a second pointer; it validates a user-requested switch and returns
an immutable intent for a future P0-owned CAS command.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from ..generation import (
    DataBundleResolver,
    resolve_plan_generation,
    validate_generation,
    validate_plan,
)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PlanSwitchIntent:
    workspace_id: str
    operation_id: str
    expected_workspace_revision: int
    expected_current_plan_revision_id: str | None
    target_plan_revision_id: str
    target_plan_canonical_hash: str
    plan_id: str
    plan_revision: int
    generation_id: str
    intent_hash: str
    _plan_bytes: bytes
    _resolution_bytes: tuple[bytes, ...]

    @property
    def plan(self) -> Mapping[str, Any]:
        # Return a fresh tree.  Callers can mutate their projection without
        # changing the canonical bytes or the intent hash.
        return _deep_freeze(json.loads(self._plan_bytes))

    @property
    def resolution(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(_deep_freeze(json.loads(item)) for item in self._resolution_bytes)

    def verify_integrity(self) -> None:
        payload = {
            "workspace_id": self.workspace_id,
            "operation_id": self.operation_id,
            "expected_workspace_revision": self.expected_workspace_revision,
            "expected_current_plan_revision_id": self.expected_current_plan_revision_id,
            "target_plan_revision_id": self.target_plan_revision_id,
            "target_plan_canonical_hash": self.target_plan_canonical_hash,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "generation_id": self.generation_id,
            "plan": json.loads(self._plan_bytes),
            "resolution": [json.loads(item) for item in self._resolution_bytes],
        }
        if hashlib.sha256(canonical_bytes(payload)).hexdigest() != self.intent_hash:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "Plan switch intent integrity check failed",
            )


def prepare_plan_switch(
    *,
    workspace_id: str,
    operation_id: str,
    expected_workspace_revision: int,
    expected_current_plan_revision_id: str | None,
    target_plan_revision_id: str,
    plan: Mapping[str, Any],
    generation: Mapping[str, Any],
    release_catalog: Mapping[str, Mapping[str, Any]],
    data_bundle_resolver: DataBundleResolver,
    user_confirmed: bool,
) -> PlanSwitchIntent:
    """Validate a user-explicit Plan selection without committing Core state."""
    if not user_confirmed:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION, "Plan switch requires an explicit user action"
        )
    if not workspace_id or not operation_id or not target_plan_revision_id:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "Plan switch requires stable workspace/operation IDs",
        )
    if expected_workspace_revision < 0:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "Plan switch requires a non-negative expected Workspace revision",
        )
    if expected_current_plan_revision_id is not None and (
        not isinstance(expected_current_plan_revision_id, str)
        or not expected_current_plan_revision_id
    ):
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "expected current Plan revision must be a stable opaque ID or null",
        )
    plan_value = validate_plan(plan)
    generation_value = validate_generation(generation)
    resolution = resolve_plan_generation(
        plan_value,
        generation_value,
        data_bundle_resolver=data_bundle_resolver,
        release_catalog=release_catalog,
    )
    resolution_value = [copy.deepcopy(item) for item in resolution]
    plan_hash = hashlib.sha256(canonical_bytes(plan_value)).hexdigest()
    payload = {
        "workspace_id": workspace_id,
        "operation_id": operation_id,
        "expected_workspace_revision": expected_workspace_revision,
        "expected_current_plan_revision_id": expected_current_plan_revision_id,
        "target_plan_revision_id": target_plan_revision_id,
        "target_plan_canonical_hash": plan_hash,
        "plan_id": plan_value["plan_id"],
        "plan_revision": plan_value["revision"],
        "generation_id": generation_value["generation_id"],
        "plan": plan_value,
        "resolution": resolution_value,
    }
    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    intent = PlanSwitchIntent(
        workspace_id=workspace_id,
        operation_id=operation_id,
        expected_workspace_revision=expected_workspace_revision,
        expected_current_plan_revision_id=expected_current_plan_revision_id,
        target_plan_revision_id=target_plan_revision_id,
        target_plan_canonical_hash=plan_hash,
        plan_id=plan_value["plan_id"],
        plan_revision=plan_value["revision"],
        generation_id=generation_value["generation_id"],
        intent_hash=digest,
        _plan_bytes=canonical_bytes(plan_value),
        _resolution_bytes=tuple(canonical_bytes(item) for item in resolution_value),
    )
    intent.verify_integrity()
    return intent


__all__ = ["PlanSwitchIntent", "prepare_plan_switch"]
