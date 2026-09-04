"""Host-composed Autopilot execution, recovery, and Candidate completion.

The module deliberately has no Core import, database handle, process registry,
or framing implementation.  ``FramedStdioWorker`` owns the protocol boundary;
this domain runtime consumes only its bound Host and Asset ports.  A pending
child poll is recoverable rather than a local durable transition, so a retry
replays the same Host operation identity instead of inventing a second
checkpoint authority.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Protocol

try:  # Installed plugins resolve the SDK as a top-level package.
    from plotpilot_plugin_sdk.canonical import (
        canonical_bytes,
        parse_json_bytes,
        sha256_hex,
    )
    from plotpilot_plugin_sdk.stdio_worker import (
        FramedStdioWorker,
        HostAsset,
        HostAssetClient,
        WorkerContext,
    )
    from plotpilot_plugin_sdk.verifier import validate_rpc_result, verify_result_bundle
except ModuleNotFoundError:  # Repository-source execution keeps the same SDK implementation.
    from backend.plotpilot_plugin_sdk.canonical import (
        canonical_bytes,
        parse_json_bytes,
        sha256_hex,
    )
    from backend.plotpilot_plugin_sdk.stdio_worker import (
        FramedStdioWorker,
        HostAsset,
        HostAssetClient,
        WorkerContext,
    )
    from backend.plotpilot_plugin_sdk.verifier import (
        validate_rpc_result,
        verify_result_bundle,
    )

from .checkpoint import (
    AutopilotCheckpointError,
    AutopilotIdentity,
    CheckpointEnvelope,
    StageEffect,
    parse_canonical_json_bytes,
    recover_checkpoint_envelope,
    validate_identifier,
    validate_sha256,
)
from .dag import DurableDAG, DurableStage, build_durable_dag

PLUGIN_ID = "com.plotpilot.autopilot"
PLUGIN_VERSION = "1.0.0"
CAPABILITY_ID = "autopilot.dag.run/v1"
RESULT_CONTRACT = "candidate-batch/v1"

# These bounds are intentionally local execution limits, not persistent state.
MAX_HOST_ASSET_BYTES = 64 * 1024 * 1024
MAX_HOST_ASSET_PAGES = 256
DEFAULT_HOST_PAGE_SIZE = 1024 * 1024
DEFAULT_MAX_STAGE_POLLS = 128
DEFAULT_POLL_BUDGET_SECONDS = 30.0
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.05
DEFAULT_MAX_BACKOFF_SECONDS = 1.0


class AutopilotRuntimeError(RuntimeError):
    """A Host response, identity boundary, or runtime input failed closed."""


class AutopilotHostPort(Protocol):
    """The Host port supplied by the shared stdio worker."""

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class AutopilotAssetPort(Protocol):
    """The bounded SDK Asset helper; it never grants a persistence handle."""

    def read(self, asset_id: str) -> HostAsset: ...

    def create(
        self,
        content: bytes | str,
        *,
        operation_key: str,
        mime: str = "application/json",
    ) -> HostAsset: ...


def _operation_key(kind: str, *parts: str) -> str:
    """Derive a canonical Host idempotency key from explicit immutable inputs."""

    material = "\n".join((kind, *parts)).encode("utf-8")
    return f"autopilot-{kind}-{sha256(material).hexdigest()[:48]}"


def _preallocated_receipt_id(attempt_id: str) -> str:
    """Match Core's default preallocated receipt derivation without importing Core."""

    validate_identifier(attempt_id, "attempt_id")
    return "receipt-" + sha256(attempt_id.encode("utf-8")).hexdigest()[:48]


def _finite_positive(value: object, label: str, *, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= maximum
    ):
        raise ValueError(f"{label} must be a finite value between 0 and {maximum}")
    return float(value)


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise AutopilotRuntimeError(f"{label} must be a non-negative integer")
    return value


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.startswith("\ufeff"):
        raise AutopilotRuntimeError(f"{label} must be non-empty UTF-8 text without a BOM")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AutopilotRuntimeError(f"{label} must be strict UTF-8 text") from exc
    return value


@dataclass(frozen=True, slots=True)
class CandidateProjection:
    """A Candidate-only document mutation assembled from frozen run inputs."""

    text: str
    mutation_mode: str
    workspace_id: str
    document_id: str
    base_revision_id: str
    base_content_hash: str
    source_refs: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        _strict_text(self.text, "candidate text")
        if self.mutation_mode not in {"replace", "append_text"}:
            raise AutopilotRuntimeError(
                "Autopilot Candidate mutation must be replace or append_text"
            )
        for value, label in (
            (self.workspace_id, "workspace_id"),
            (self.document_id, "document_id"),
            (self.base_revision_id, "base_revision_id"),
        ):
            validate_identifier(value, label)
        validate_sha256(self.base_content_hash, "base_content_hash")
        if not isinstance(self.source_refs, tuple):
            raise AutopilotRuntimeError("candidate source_refs must be an immutable tuple")
        for reference in self.source_refs:
            if not isinstance(reference, Mapping) or set(reference) != {
                "workspace_id",
                "source_type",
                "source_id",
                "revision_or_hash",
            }:
                raise AutopilotRuntimeError("candidate source_refs are not closed")
            if reference["workspace_id"] != self.workspace_id:
                raise AutopilotRuntimeError(
                    "candidate source_refs cannot cross the frozen workspace"
                )
            validate_identifier(reference["source_id"], "candidate source_id")
            if (
                not isinstance(reference["source_type"], str)
                or not reference["source_type"]
                or not isinstance(reference["revision_or_hash"], str)
                or not reference["revision_or_hash"]
            ):
                raise AutopilotRuntimeError("candidate source_refs values are invalid")


@dataclass(frozen=True, slots=True)
class AutopilotRunResult:
    """One bounded execution outcome with no process-local durable authority."""

    dag_hash: str
    completed_stage_ids: tuple[str, ...]
    stage_effects: tuple[StageEffect, ...]
    checkpoint: CheckpointEnvelope | None
    pending_stage_id: str | None = None
    retry_after_seconds: float | None = None
    cancelled: bool = False
    result_bundle: Mapping[str, object] | None = None
    result_bundle_asset_id: str | None = None
    provenance_receipt_id: str | None = None
    candidate_stage_operation_key: str | None = None
    completion_operation_key: str | None = None

    def __post_init__(self) -> None:
        validate_sha256(self.dag_hash, "dag_hash")
        if self.pending_stage_id is not None:
            validate_identifier(self.pending_stage_id, "pending_stage_id")
            if self.cancelled:
                raise AutopilotRuntimeError("a run cannot be pending and cancelled")
            if (
                self.retry_after_seconds is None
                or self.retry_after_seconds < 0.0
                or not math.isfinite(self.retry_after_seconds)
            ):
                raise AutopilotRuntimeError("pending runs require a finite retry delay")
        elif self.retry_after_seconds is not None:
            raise AutopilotRuntimeError("only a pending run may expose retry delay")
        if self.result_bundle_asset_id is not None:
            validate_identifier(self.result_bundle_asset_id, "result_bundle_asset_id")
        if self.provenance_receipt_id is not None:
            validate_identifier(self.provenance_receipt_id, "provenance_receipt_id")
        for value, label in (
            (self.candidate_stage_operation_key, "candidate_stage_operation_key"),
            (self.completion_operation_key, "completion_operation_key"),
        ):
            if value is not None:
                validate_identifier(value, label)

    @property
    def completed(self) -> bool:
        """Whether this invocation reached all stages without pending/cancel."""

        return self.pending_stage_id is None and not self.cancelled

    @property
    def pending(self) -> bool:
        """Whether a legal quiescent child poll exhausted this invocation's budget."""

        return self.pending_stage_id is not None

    @property
    def checkpoint_asset_id(self) -> str | None:
        return None if self.checkpoint is None else self.checkpoint.checkpoint_asset_id


@dataclass(frozen=True, slots=True)
class _ActiveChild:
    child_job_id: str
    propagate_cancel: bool


@dataclass(frozen=True, slots=True)
class _StagePollOutcome:
    effect: StageEffect | None = None
    pending: bool = False
    retry_after_seconds: float | None = None
    cancelled: bool = False


def _default_sleeper(delay_seconds: float, cancelled: threading.Event) -> bool:
    """Wait with an interruptible event instead of a busy or unbounded sleep."""

    return cancelled.wait(delay_seconds)


class AutopilotRuntime:
    """Execute a frozen DAG only through the accepted SDK Host/Asset seams."""

    def __init__(
        self,
        host: AutopilotHostPort,
        *,
        plugin_release_id: str,
        assets: AutopilotAssetPort | None = None,
        max_stage_polls: int = DEFAULT_MAX_STAGE_POLLS,
        poll_budget_seconds: float = DEFAULT_POLL_BUDGET_SECONDS,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[..., object] = _default_sleeper,
    ) -> None:
        if host is None or not callable(getattr(host, "call", None)):
            raise TypeError("AutopilotRuntime requires an accepted Host call port")
        validate_sha256(plugin_release_id, "plugin_release_id")
        if (
            type(max_stage_polls) is not int
            or not 1 <= max_stage_polls <= DEFAULT_MAX_STAGE_POLLS
        ):
            raise ValueError("max_stage_polls is outside the bounded runtime limit")
        budget = _finite_positive(
            poll_budget_seconds, "poll_budget_seconds", maximum=3600.0
        )
        initial_backoff = _finite_positive(
            initial_backoff_seconds, "initial_backoff_seconds", maximum=60.0
        )
        maximum_backoff = _finite_positive(
            max_backoff_seconds, "max_backoff_seconds", maximum=60.0
        )
        if initial_backoff > maximum_backoff:
            raise ValueError("initial_backoff_seconds cannot exceed max_backoff_seconds")
        if not callable(monotonic) or not callable(sleeper):
            raise TypeError("monotonic and sleeper must be callable")

        self._host = host
        self.plugin_release_id = plugin_release_id
        self._assets: AutopilotAssetPort = assets or HostAssetClient(
            host,  # type: ignore[arg-type] - HostAssetClient only needs call().
            page_size=DEFAULT_HOST_PAGE_SIZE,
            max_bytes=MAX_HOST_ASSET_BYTES,
            max_pages=MAX_HOST_ASSET_PAGES,
        )
        if not callable(getattr(self._assets, "read", None)) or not callable(
            getattr(self._assets, "create", None)
        ):
            raise TypeError("assets must expose the accepted SDK read/create helpers")
        self._max_stage_polls = max_stage_polls
        self._poll_budget_seconds = budget
        self._initial_backoff_seconds = initial_backoff
        self._max_backoff_seconds = maximum_backoff
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._cancel_event = threading.Event()
        self._cancel_reason: str | None = None
        self._active_child: _ActiveChild | None = None
        self._cancelled_child_ids: set[str] = set()
        self._cancel_terminal_known = False
        self._state_lock = threading.RLock()

    @property
    def cancel_terminal_known(self) -> bool:
        return self._cancel_terminal_known

    @property
    def pending_child_job_id(self) -> str | None:
        with self._state_lock:
            return None if self._active_child is None else self._active_child.child_job_id

    def _call(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        host: AutopilotHostPort | None = None,
    ) -> dict[str, object]:
        caller = self._host if host is None else host
        try:
            result = caller.call(method, dict(params))
            if not isinstance(result, Mapping):
                raise AutopilotRuntimeError(f"{method} returned a non-object")
            value = dict(result)
            validate_rpc_result(method, value)
            return value
        except AutopilotRuntimeError:
            raise
        except Exception as exc:
            raise AutopilotRuntimeError(
                f"{method} failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _stable_operation_key(
        self,
        kind: str,
        *,
        identity: AutopilotIdentity,
        dag_hash: str,
        payload_hash: str,
        parts: tuple[str, ...] = (),
    ) -> str:
        """Exclude the mutable Attempt/lease while retaining frozen run identity."""

        return _operation_key(
            kind,
            identity.workspace_id,
            identity.job_id,
            identity.step_id,
            identity.plugin_release_id,
            identity.run_snapshot_hash,
            dag_hash,
            payload_hash,
            *parts,
        )

    def _clock_now(self, previous: float | None = None) -> float:
        try:
            value = self._monotonic()
        except Exception as exc:
            raise AutopilotRuntimeError("injected monotonic clock failed") from exc
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise AutopilotRuntimeError("injected monotonic clock returned non-finite time")
        now = float(value)
        if previous is not None and now < previous:
            raise AutopilotRuntimeError("injected monotonic clock moved backwards")
        return now

    def _wait_once(self, delay_seconds: float) -> bool:
        """Perform the mandatory interruptible wait before every poll retry."""

        if delay_seconds <= 0.0:
            raise AutopilotRuntimeError("poll retry attempted without a real wait")
        try:
            result = self._sleeper(delay_seconds, self._cancel_event)
        except TypeError:
            # A one-argument test sleeper remains useful, but cancellation is
            # still observed immediately after it returns.
            result = self._sleeper(delay_seconds)
        return bool(result) or self._cancel_event.is_set()

    def _set_active_child(self, child_job_id: str, *, propagate_cancel: bool) -> None:
        with self._state_lock:
            self._active_child = _ActiveChild(child_job_id, propagate_cancel)

    def _clear_active_child(self, child_job_id: str) -> None:
        with self._state_lock:
            if (
                self._active_child is not None
                and self._active_child.child_job_id == child_job_id
            ):
                self._active_child = None

    def cancel(
        self,
        reason: str,
        *,
        host: AutopilotHostPort | None = None,
    ) -> bool:
        """Interrupt a pending wait and forward at most one child cancellation."""

        if not isinstance(reason, str) or not reason.strip():
            raise AutopilotRuntimeError("cancel reason must be non-empty")
        self._cancel_event.set()
        with self._state_lock:
            self._cancel_reason = reason
            active = self._active_child
            if (
                active is None
                or not active.propagate_cancel
                or active.child_job_id in self._cancelled_child_ids
            ):
                return False
            # Mark before the RPC so a duplicate cancel cannot create a second
            # Host operation if its first acknowledgement is lost.
            self._cancelled_child_ids.add(active.child_job_id)

        operation_key = _operation_key("child-cancel", active.child_job_id)
        response = self._call(
            "host.capability.cancel/v1",
            {
                "operation_key": operation_key,
                "child_job_id": active.child_job_id,
                "reason": reason,
            },
            host=host,
        )
        if type(response["accepted"]) is not bool or not response["accepted"]:
            raise AutopilotRuntimeError("Host rejected child cancellation")
        if type(response["terminal_known"]) is not bool:
            raise AutopilotRuntimeError("Host child cancellation terminal_known is invalid")
        self._cancel_terminal_known = bool(response["terminal_known"])
        _nonnegative_int(response["child_job_event_seq"], "child_job_event_seq")
        return True

    def _cancel_if_requested(self) -> bool:
        if not self._cancel_event.is_set():
            return False
        self.cancel(self._cancel_reason or "cancelled")
        return True

    def _recover(
        self,
        checkpoint_asset_id: str,
        *,
        identity: AutopilotIdentity,
        dag: DurableDAG,
        resume_of_attempt_id: str,
    ) -> CheckpointEnvelope:
        checkpoint_asset = self._assets.read(checkpoint_asset_id)
        checkpoint_value = parse_canonical_json_bytes(checkpoint_asset.content)
        if not isinstance(checkpoint_value, Mapping):
            raise AutopilotRuntimeError("checkpoint Asset must contain an object")
        state_asset_id = checkpoint_value.get("state_asset_id")
        try:
            validate_identifier(state_asset_id, "checkpoint state_asset_id")
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc
        state_asset = self._assets.read(state_asset_id)
        state_value = parse_canonical_json_bytes(state_asset.content)
        if not isinstance(state_value, Mapping):
            raise AutopilotRuntimeError(
                "checkpoint runtime-state Asset must contain an object"
            )
        try:
            return recover_checkpoint_envelope(
                dict(checkpoint_value),
                state_value,
                state_asset_id=state_asset_id,
                checkpoint_asset_id=checkpoint_asset_id,
                identity=identity,
                dag_hash=dag.dag_hash,
                total_units=len(dag.ordered_stages),
                resume_of_attempt_id=resume_of_attempt_id,
            )
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc

    def _invoke_stage(
        self,
        stage: DurableStage,
        *,
        identity: AutopilotIdentity,
        dag_hash: str,
        payload_hash: str,
    ) -> _StagePollOutcome:
        operation_key = self._stable_operation_key(
            "stage",
            identity=identity,
            dag_hash=dag_hash,
            payload_hash=payload_hash,
            parts=(stage.stage_id,),
        )
        invoke = self._call(
            "host.capability.invoke/v1",
            {
                "operation_key": operation_key,
                "binding_id": stage.binding_id,
                "input_asset_id": stage.input_asset_id,
                "parameters_asset_id": stage.parameters_asset_id,
                "expected_result_contract": stage.expected_result_contract,
                "propagate_cancel": stage.propagate_cancel,
            },
        )
        if type(invoke["accepted"]) is not bool or not invoke["accepted"]:
            raise AutopilotRuntimeError("Host rejected the durable stage invocation")
        try:
            child_job_id = validate_identifier(invoke["child_job_id"], "child_job_id")
            child_step_id = validate_identifier(invoke["child_step_id"], "child_step_id")
            child_snapshot_asset_id = validate_identifier(
                invoke["child_run_snapshot_asset_id"], "child_run_snapshot_asset_id"
            )
            child_snapshot_hash = validate_sha256(
                invoke["child_run_snapshot_hash"], "child_run_snapshot_hash"
            )
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc
        if invoke["child_result_contract"] != stage.expected_result_contract:
            raise AutopilotRuntimeError(
                "Host stage result contract drifted from the frozen DAG"
            )
        after_event_seq = _nonnegative_int(
            invoke["child_job_event_seq"], "child_job_event_seq"
        )
        self._set_active_child(child_job_id, propagate_cancel=stage.propagate_cancel)
        if self._cancel_if_requested():
            return _StagePollOutcome(cancelled=True)

        previous_clock = self._clock_now()
        deadline = previous_clock + self._poll_budget_seconds
        backoff = self._initial_backoff_seconds
        for _poll_index in range(self._max_stage_polls):
            if self._cancel_if_requested():
                return _StagePollOutcome(cancelled=True)
            poll = self._call(
                "host.capability.poll/v1",
                {
                    "child_job_id": child_job_id,
                    "after_job_event_seq": after_event_seq,
                },
            )
            next_event_seq = _nonnegative_int(
                poll["next_job_event_seq"], "next_job_event_seq"
            )
            if next_event_seq < after_event_seq:
                raise AutopilotRuntimeError("Host stage event sequence moved backwards")
            if type(poll["terminal"]) is not bool:
                raise AutopilotRuntimeError("Host stage terminal flag must be boolean")
            if poll["terminal"]:
                try:
                    result_bundle_asset_id = validate_identifier(
                        poll["result_bundle_asset_id"], "result_bundle_asset_id"
                    )
                    provenance_receipt_id = validate_identifier(
                        poll["provenance_receipt_id"], "provenance_receipt_id"
                    )
                except AutopilotCheckpointError as exc:
                    raise AutopilotRuntimeError(
                        "terminal stage omitted durable result identity"
                    ) from exc
                self._clear_active_child(child_job_id)
                return _StagePollOutcome(
                    effect=StageEffect(
                        stage_id=stage.stage_id,
                        operation_key=operation_key,
                        child_job_id=child_job_id,
                        child_step_id=child_step_id,
                        child_run_snapshot_asset_id=child_snapshot_asset_id,
                        child_run_snapshot_hash=child_snapshot_hash,
                        result_bundle_asset_id=result_bundle_asset_id,
                        provenance_receipt_id=provenance_receipt_id,
                        child_result_contract=stage.expected_result_contract,
                    )
                )

            # Equality is a legal quiescent poll.  A regression above remains
            # fail-closed, while any retry below always performs a real wait.
            progressed = next_event_seq > after_event_seq
            after_event_seq = next_event_seq
            now = self._clock_now(previous_clock)
            previous_clock = now
            remaining = deadline - now
            if remaining <= 0.0:
                return _StagePollOutcome(
                    pending=True,
                    retry_after_seconds=min(self._max_backoff_seconds, backoff),
                )
            delay = min(backoff, self._max_backoff_seconds, remaining)
            if self._wait_once(delay) or self._cancel_if_requested():
                return _StagePollOutcome(cancelled=True)
            now = self._clock_now(previous_clock)
            previous_clock = now
            if now >= deadline:
                return _StagePollOutcome(
                    pending=True,
                    retry_after_seconds=min(self._max_backoff_seconds, backoff),
                )
            backoff = (
                self._initial_backoff_seconds
                if progressed
                else min(self._max_backoff_seconds, backoff * 2.0)
            )

        # The count bound complements the elapsed-time budget if an injected
        # test clock does not advance.  It is still a recoverable pending state.
        return _StagePollOutcome(
            pending=True,
            retry_after_seconds=min(self._max_backoff_seconds, backoff),
        )

    def _complete_candidate(
        self,
        result: AutopilotRunResult,
        *,
        identity: AutopilotIdentity,
        dag_hash: str,
        payload_hash: str,
        candidate: CandidateProjection,
        worker_run_id: str,
    ) -> AutopilotRunResult:
        if result.pending or result.cancelled:
            return result
        validate_identifier(worker_run_id, "worker_run_id")
        candidate_payload = candidate.text.encode("utf-8")
        candidate_payload_hash = sha256_hex(candidate_payload)
        payload_asset = self._assets.create(
            candidate_payload,
            operation_key=self._stable_operation_key(
                "candidate-payload",
                identity=identity,
                dag_hash=dag_hash,
                payload_hash=payload_hash,
            ),
            mime="text/plain;charset=utf-8",
        )
        receipt_id = _preallocated_receipt_id(identity.attempt_id)
        # Bundle/item IDs are owned by this current Candidate projection.  Host
        # operation identities below remain stable across a direct resume edge.
        bundle_id = _operation_key(
            "candidate-bundle",
            identity.job_id,
            identity.step_id,
            identity.attempt_id,
            dag_hash,
            candidate_payload_hash,
        )
        item_id = _operation_key(
            "candidate-item",
            identity.job_id,
            identity.step_id,
            identity.attempt_id,
            candidate.document_id,
            candidate_payload_hash,
        )
        item = {
            "schema": "candidate-item/v1",
            "item_id": item_id,
            "item_kind": "document",
            "target": {
                "workspace_id": candidate.workspace_id,
                "entity_kind": "document",
                "entity_id": candidate.document_id,
            },
            "mutation": {
                "mode": candidate.mutation_mode,
                "payload_schema": "core/document-text/v1",
                "payload_hash": candidate_payload_hash,
            },
            "payload_asset_id": payload_asset.asset_id,
            "base": {
                "revision_id": candidate.base_revision_id,
                "content_hash": candidate.base_content_hash,
            },
            "write_set": [
                {
                    "workspace_id": candidate.workspace_id,
                    "entity_kind": "document",
                    "entity_id": candidate.document_id,
                    "revision_id": candidate.base_revision_id,
                    "content_hash": candidate.base_content_hash,
                }
            ],
            "parent_candidate_ids": [],
            "source_refs": [dict(reference) for reference in candidate.source_refs],
            "status": "complete",
        }
        bundle: dict[str, object] = {
            "schema": "result-bundle/v1",
            "contract_id": RESULT_CONTRACT,
            "bundle_id": bundle_id,
            "bundle_type": "candidate_batch",
            "producer": {
                "plugin_id": PLUGIN_ID,
                "release_id": identity.plugin_release_id,
                "capability_id": CAPABILITY_ID,
                "job_id": identity.job_id,
                "step_id": identity.step_id,
                "attempt_id": identity.attempt_id,
                "lease_epoch": identity.lease_epoch,
            },
            "input_snapshot_hash": identity.run_snapshot_hash,
            "items": [item],
            "warnings": [],
            "partial": False,
            "provenance_receipt_id": receipt_id,
            "skill_chain_result_refs": [],
        }
        try:
            verify_result_bundle(
                bundle,
                snapshot_workspace_id=identity.workspace_id,
                snapshot_hash_value=identity.run_snapshot_hash,
            )
        except Exception as exc:
            raise AutopilotRuntimeError("Autopilot Candidate result is invalid") from exc

        bundle_asset = self._assets.create(
            canonical_bytes(bundle),
            operation_key=self._stable_operation_key(
                "result-bundle",
                identity=identity,
                dag_hash=dag_hash,
                payload_hash=payload_hash,
            ),
            mime="application/json",
        )
        candidate_stage_operation_key = self._stable_operation_key(
            "candidate-stage",
            identity=identity,
            dag_hash=dag_hash,
            payload_hash=payload_hash,
            parts=(candidate.document_id,),
        )
        completion_operation_key = self._stable_operation_key(
            "complete",
            identity=identity,
            dag_hash=dag_hash,
            payload_hash=payload_hash,
            parts=(candidate.document_id,),
        )
        completion = self._call(
            "host.job.complete/v1",
            {
                "operation_key": completion_operation_key,
                "worker_run_id": worker_run_id,
                "outcome": "succeeded",
                "result_bundle_asset_id": bundle_asset.asset_id,
                "candidate_stage_operation_key": candidate_stage_operation_key,
                "terminal_detail_asset_id": None,
                "local_seq": 1,
            },
        )
        if type(completion["accepted"]) is not bool or not completion["accepted"]:
            raise AutopilotRuntimeError("Host rejected Autopilot Candidate completion")
        if completion["provenance_receipt_id"] != receipt_id:
            raise AutopilotRuntimeError(
                "Host completion receipt differs from the frozen Candidate bundle"
            )
        return replace(
            result,
            result_bundle=bundle,
            result_bundle_asset_id=bundle_asset.asset_id,
            provenance_receipt_id=receipt_id,
            candidate_stage_operation_key=candidate_stage_operation_key,
            completion_operation_key=completion_operation_key,
        )

    def run(
        self,
        identity: AutopilotIdentity,
        dag: DurableDAG,
        *,
        checkpoint_asset_id: str | None = None,
        resume_of_attempt_id: str | None = None,
        payload_hash: str | None = None,
        candidate: CandidateProjection | None = None,
        worker_run_id: str | None = None,
    ) -> AutopilotRunResult:
        """Run a DAG or one authoritative direct resume edge.

        A supplied checkpoint is accepted only with the ``job.resume`` source
        Attempt that selected it.  This method never commits a checkpoint: a
        budget expiry returns pending before a new StageEffect, checkpoint,
        next-stage advance, Candidate, or terminal completion can be emitted.
        """

        if not isinstance(identity, AutopilotIdentity):
            raise TypeError("identity must be an AutopilotIdentity")
        if not isinstance(dag, DurableDAG):
            raise TypeError("dag must be a DurableDAG")
        if identity.plugin_release_id != self.plugin_release_id:
            raise AutopilotRuntimeError(
                "runtime release does not match the frozen Autopilot identity"
            )
        effective_payload_hash = dag.dag_hash if payload_hash is None else payload_hash
        try:
            validate_sha256(effective_payload_hash, "payload_hash")
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc
        if checkpoint_asset_id is None:
            if resume_of_attempt_id is not None:
                raise AutopilotRuntimeError(
                    "resume_of_attempt_id requires the current resume checkpoint Asset"
                )
            checkpoint = None
            durable_effects: list[StageEffect] = []
        else:
            if not isinstance(resume_of_attempt_id, str):
                raise AutopilotRuntimeError(
                    "checkpoint recovery is allowed only from job.resume direct lineage"
                )
            try:
                validate_identifier(checkpoint_asset_id, "checkpoint_asset_id")
                validate_identifier(resume_of_attempt_id, "resume_of_attempt_id")
            except AutopilotCheckpointError as exc:
                raise AutopilotRuntimeError(str(exc)) from exc
            checkpoint = self._recover(
                checkpoint_asset_id,
                identity=identity,
                dag=dag,
                resume_of_attempt_id=resume_of_attempt_id,
            )
            durable_effects = list(checkpoint.state.completed_stages)

        ordered = dag.ordered_stages
        completed_ids = tuple(effect.stage_id for effect in durable_effects)
        expected_prefix = tuple(stage.stage_id for stage in ordered[: len(durable_effects)])
        if completed_ids != expected_prefix:
            raise AutopilotRuntimeError(
                "checkpoint completed stages are not the current DAG prefix"
            )
        for stage, effect in zip(
            ordered[: len(durable_effects)], durable_effects, strict=True
        ):
            expected_operation_key = self._stable_operation_key(
                "stage",
                identity=identity,
                dag_hash=dag.dag_hash,
                payload_hash=effective_payload_hash,
                parts=(stage.stage_id,),
            )
            if effect.operation_key != expected_operation_key:
                raise AutopilotRuntimeError(
                    "checkpoint stage operation identity drifted from the "
                    "current payload"
                )

        # Effects made after this invocation begins remain local until every
        # stage reaches terminal state.  A pending return deliberately drops
        # them; stable child operation keys make the next run replay-safe.
        new_effects: list[StageEffect] = []
        for stage in ordered[len(durable_effects) :]:
            completed_set = {
                effect.stage_id for effect in (*durable_effects, *new_effects)
            }
            if not set(stage.depends_on).issubset(completed_set):
                raise AutopilotRuntimeError(
                    "DAG dependency is not durably complete before execution"
                )
            stage_outcome = self._invoke_stage(
                stage,
                identity=identity,
                dag_hash=dag.dag_hash,
                payload_hash=effective_payload_hash,
            )
            if stage_outcome.cancelled:
                return AutopilotRunResult(
                    dag_hash=dag.dag_hash,
                    completed_stage_ids=completed_ids,
                    stage_effects=tuple(durable_effects),
                    checkpoint=checkpoint,
                    cancelled=True,
                )
            if stage_outcome.pending:
                return AutopilotRunResult(
                    dag_hash=dag.dag_hash,
                    completed_stage_ids=completed_ids,
                    stage_effects=tuple(durable_effects),
                    checkpoint=checkpoint,
                    pending_stage_id=stage.stage_id,
                    retry_after_seconds=stage_outcome.retry_after_seconds,
                )
            if stage_outcome.effect is None:
                raise AutopilotRuntimeError("stage completed without a durable effect")
            new_effects.append(stage_outcome.effect)

        result = AutopilotRunResult(
            dag_hash=dag.dag_hash,
            completed_stage_ids=tuple(
                effect.stage_id for effect in (*durable_effects, *new_effects)
            ),
            stage_effects=(*durable_effects, *new_effects),
            checkpoint=checkpoint,
        )
        if candidate is None:
            if worker_run_id is not None:
                raise AutopilotRuntimeError(
                    "worker_run_id requires a Candidate projection for terminal completion"
                )
            return result
        if worker_run_id is None:
            raise AutopilotRuntimeError(
                "Candidate completion requires the current worker_run_id"
            )
        return self._complete_candidate(
            result,
            identity=identity,
            dag_hash=dag.dag_hash,
            payload_hash=effective_payload_hash,
            candidate=candidate,
            worker_run_id=worker_run_id,
        )

    resume = run


def _asset_json(asset: HostAsset, *, label: str) -> Mapping[str, object]:
    try:
        value = parse_json_bytes(asset.content)
        if not isinstance(value, Mapping) or canonical_bytes(value) != asset.content:
            raise AutopilotRuntimeError(f"{label} must be canonical JSON object bytes")
        return dict(value)
    except AutopilotRuntimeError:
        raise
    except Exception as exc:
        raise AutopilotRuntimeError(f"{label} is not strict canonical JSON") from exc


def _stage_from_mapping(value: object) -> DurableStage:
    if not isinstance(value, Mapping):
        raise AutopilotRuntimeError("Autopilot DAG stage must be an object")
    expected = {
        "stage_id",
        "binding_id",
        "input_asset_id",
        "parameters_asset_id",
        "expected_result_contract",
        "propagate_cancel",
        "depends_on",
    }
    if set(value) != expected:
        raise AutopilotRuntimeError("Autopilot DAG stage fields are not closed")
    dependencies = value["depends_on"]
    if not isinstance(dependencies, list):
        raise AutopilotRuntimeError("Autopilot DAG dependencies must be an array")
    try:
        return DurableStage(
            stage_id=value["stage_id"],  # type: ignore[arg-type]
            binding_id=value["binding_id"],  # type: ignore[arg-type]
            input_asset_id=value["input_asset_id"],  # type: ignore[arg-type]
            parameters_asset_id=value["parameters_asset_id"],  # type: ignore[arg-type]
            expected_result_contract=value["expected_result_contract"],  # type: ignore[arg-type]
            propagate_cancel=value["propagate_cancel"],  # type: ignore[arg-type]
            depends_on=tuple(dependencies),
        )
    except Exception as exc:
        raise AutopilotRuntimeError("Autopilot DAG stage is invalid") from exc


def _snapshot_parameter_hash(
    snapshot: Mapping[str, object], parameter_asset_id: str
) -> str:
    asset_hashes = snapshot.get("asset_hashes")
    if not isinstance(asset_hashes, list):
        raise AutopilotRuntimeError("RunSnapshot asset hashes are invalid")
    matches = [
        entry
        for entry in asset_hashes
        if isinstance(entry, Mapping) and entry.get("asset_id") == parameter_asset_id
    ]
    if len(matches) != 1:
        raise AutopilotRuntimeError(
            "Autopilot parameters Asset is absent from the frozen RunSnapshot"
        )
    try:
        return validate_sha256(matches[0].get("sha256"), "parameters Asset hash")
    except AutopilotCheckpointError as exc:
        raise AutopilotRuntimeError(str(exc)) from exc


def _candidate_from_snapshot(
    snapshot: Mapping[str, object],
    *,
    parameter_asset_id: str,
    parameter_hash: str,
    candidate_input: Mapping[str, object] | None,
    dag: DurableDAG,
) -> CandidateProjection:
    scope = snapshot.get("scope")
    if not isinstance(scope, Mapping):
        raise AutopilotRuntimeError("RunSnapshot scope is invalid")
    workspace_id = snapshot.get("workspace_id")
    document_id = scope.get("document_id")
    try:
        validate_identifier(workspace_id, "RunSnapshot workspace_id")
        validate_identifier(document_id, "RunSnapshot scope document_id")
    except AutopilotCheckpointError as exc:
        raise AutopilotRuntimeError(str(exc)) from exc
    revisions = snapshot.get("input_revisions")
    if not isinstance(revisions, list):
        raise AutopilotRuntimeError("RunSnapshot input_revisions are invalid")
    matches = [
        revision
        for revision in revisions
        if isinstance(revision, Mapping) and revision.get("document_id") == document_id
    ]
    if len(matches) != 1:
        raise AutopilotRuntimeError(
            "Autopilot Candidate document is absent from frozen input revisions"
        )
    revision = matches[0]
    try:
        base_revision_id = validate_identifier(
            revision.get("revision_id"), "base_revision_id"
        )
        base_content_hash = validate_sha256(
            revision.get("content_hash"), "base_content_hash"
        )
        snapshot_id = validate_identifier(snapshot.get("snapshot_id"), "snapshot_id")
        snapshot_hash = validate_sha256(
            snapshot.get("snapshot_hash"), "snapshot_hash"
        )
    except AutopilotCheckpointError as exc:
        raise AutopilotRuntimeError(str(exc)) from exc

    if candidate_input is None:
        text = "Autopilot completed stages: " + ", ".join(dag.ordered_stage_ids) + "."
        mode = "append_text"
    else:
        if set(candidate_input) != {"text", "mode"}:
            raise AutopilotRuntimeError("Autopilot Candidate input fields are not closed")
        text = _strict_text(candidate_input.get("text"), "candidate text")
        mode = candidate_input.get("mode")
        if not isinstance(mode, str):
            raise AutopilotRuntimeError("candidate mode must be a string")
    return CandidateProjection(
        text=text,
        mutation_mode=mode,
        workspace_id=workspace_id,
        document_id=document_id,
        base_revision_id=base_revision_id,
        base_content_hash=base_content_hash,
        source_refs=(
            {
                "workspace_id": workspace_id,
                "source_type": "run_snapshot",
                "source_id": snapshot_id,
                "revision_or_hash": snapshot_hash,
            },
            {
                "workspace_id": workspace_id,
                "source_type": "autopilot_parameters",
                "source_id": parameter_asset_id,
                "revision_or_hash": parameter_hash,
            },
        ),
    )


def _load_worker_input(
    context: WorkerContext,
) -> tuple[DurableDAG, str, CandidateProjection]:
    if context.run_snapshot is None:
        raise AutopilotRuntimeError("Autopilot handler requires a bound RunSnapshot")
    snapshot = context.run_snapshot
    parameter_asset_id = snapshot.get("parameters_asset_id")
    try:
        parameter_asset_id = validate_identifier(
            parameter_asset_id, "RunSnapshot parameters_asset_id"
        )
    except AutopilotCheckpointError as exc:
        raise AutopilotRuntimeError(
            "Autopilot requires its frozen parameters Asset"
        ) from exc
    parameter_asset = context.assets.read(parameter_asset_id)
    parameter_hash = _snapshot_parameter_hash(snapshot, parameter_asset_id)
    if parameter_asset.sha256 != parameter_hash:
        raise AutopilotRuntimeError(
            "Autopilot parameters Asset hash drifted from the RunSnapshot"
        )
    value = _asset_json(parameter_asset, label="Autopilot parameters Asset")
    allowed = {"schema", "dag_hash", "stages", "candidate"}
    if set(value) - allowed or not {"schema", "stages"}.issubset(value):
        raise AutopilotRuntimeError("Autopilot parameters fields are not closed")
    if value["schema"] != "autopilot-dag/v1":
        raise AutopilotRuntimeError("Autopilot parameters schema is invalid")
    raw_stages = value["stages"]
    if not isinstance(raw_stages, list):
        raise AutopilotRuntimeError("Autopilot parameters stages must be an array")
    dag = build_durable_dag(tuple(_stage_from_mapping(item) for item in raw_stages))
    if "dag_hash" in value and value["dag_hash"] != dag.dag_hash:
        raise AutopilotRuntimeError("Autopilot parameters DAG hash drifted")
    raw_candidate = value.get("candidate")
    if raw_candidate is not None and not isinstance(raw_candidate, Mapping):
        raise AutopilotRuntimeError("Autopilot Candidate input must be an object")
    candidate = _candidate_from_snapshot(
        snapshot,
        parameter_asset_id=parameter_asset_id,
        parameter_hash=parameter_hash,
        candidate_input=None if raw_candidate is None else dict(raw_candidate),
        dag=dag,
    )
    return dag, parameter_hash, candidate


def _identity_from_context(context: WorkerContext) -> AutopilotIdentity:
    if context.identity is None:
        raise AutopilotRuntimeError("Autopilot handler requires an Attempt identity")
    identity = context.identity
    created_at = context.meta.get("deadline_at")
    try:
        return AutopilotIdentity(
            workspace_id=identity.workspace_id,
            job_id=identity.job_id,
            step_id=identity.step_id,
            attempt_id=identity.attempt_id,
            lease_epoch=identity.lease_epoch,
            plugin_release_id=identity.plugin_release_id,
            run_snapshot_hash=identity.run_snapshot_hash,
            created_at=created_at,  # type: ignore[arg-type]
        )
    except (AutopilotCheckpointError, TypeError, ValueError) as exc:
        raise AutopilotRuntimeError("bound Attempt identity is invalid") from exc


def _worker_run_id(context: WorkerContext) -> str:
    if context.identity is None:
        raise AutopilotRuntimeError("Autopilot worker run requires Attempt identity")
    identity = context.identity
    return _operation_key(
        "worker-run",
        identity.job_id,
        identity.step_id,
        identity.attempt_id,
        str(identity.lease_epoch),
        identity.operation_id,
    )


class _AutopilotDomain:
    """Lifecycle handlers registered into one shared SDK stdio worker."""

    def __init__(self, *, runtime_options: Mapping[str, object] | None = None) -> None:
        self._runtime_options = dict(runtime_options or {})
        self._pending_runs: dict[str, AutopilotRuntime] = {}

    def _start_or_resume(
        self,
        params: Mapping[str, Any],
        context: WorkerContext,
        *,
        is_resume: bool,
    ) -> Mapping[str, object]:
        identity = _identity_from_context(context)
        dag, payload_hash, candidate = _load_worker_input(context)
        checkpoint_asset_id = params["checkpoint_asset_id"]
        resume_of_attempt_id: str | None = None
        if is_resume:
            if checkpoint_asset_id is None:
                raise AutopilotRuntimeError(
                    "job.resume requires the Core-authoritative checkpoint Asset"
                )
            resume_of_attempt_id = params["resume_of_attempt_id"]
            try:
                validate_identifier(checkpoint_asset_id, "checkpoint_asset_id")
                validate_identifier(resume_of_attempt_id, "resume_of_attempt_id")
            except AutopilotCheckpointError as exc:
                raise AutopilotRuntimeError(str(exc)) from exc
        elif checkpoint_asset_id is not None:
            raise AutopilotRuntimeError(
                "job.start cannot select an arbitrary checkpoint Asset"
            )

        worker_run_id = _worker_run_id(context)
        runtime = AutopilotRuntime(
            context.host,
            plugin_release_id=identity.plugin_release_id,
            assets=context.assets,
            **self._runtime_options,
        )
        result = runtime.run(
            identity,
            dag,
            checkpoint_asset_id=checkpoint_asset_id,
            resume_of_attempt_id=resume_of_attempt_id,
            payload_hash=payload_hash,
            candidate=candidate,
            worker_run_id=worker_run_id,
        )
        if result.pending:
            self._pending_runs[worker_run_id] = runtime
        else:
            self._pending_runs.pop(worker_run_id, None)
        return {
            "accepted": True,
            "worker_run_id": worker_run_id,
            "provenance_receipt_id": result.provenance_receipt_id
            or _preallocated_receipt_id(identity.attempt_id),
            "output_streams": [],
        }

    def start(
        self, params: Mapping[str, Any], context: WorkerContext
    ) -> Mapping[str, object]:
        return self._start_or_resume(params, context, is_resume=False)

    def resume(
        self, params: Mapping[str, Any], context: WorkerContext
    ) -> Mapping[str, object]:
        return self._start_or_resume(params, context, is_resume=True)

    def cancel(
        self, params: Mapping[str, Any], context: WorkerContext
    ) -> Mapping[str, object]:
        worker_run_id = params["worker_run_id"]
        try:
            validate_identifier(worker_run_id, "worker_run_id")
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc
        runtime = self._pending_runs.pop(worker_run_id, None)
        if runtime is None:
            return {
                "accepted": True,
                "terminal_known": True,
                "attempt_state": "cancelled",
            }
        runtime.cancel(params["reason"], host=context.host)
        return {
            "accepted": True,
            "terminal_known": runtime.cancel_terminal_known,
            "attempt_state": "cancelling",
        }


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    """Describe the exact shared-worker operation and Candidate result surface."""

    validate_sha256(release_id, "release_id")
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "run-snapshot/v1",
        "output_schema": "result-bundle/v1",
        "result_contract": RESULT_CONTRACT,
        "supports": ["run", "resume", "cancel"],
        "deterministic": False,
        "accepted_data_formats": [],
    }


def build_worker(
    *,
    worker_instance_id: str | None = None,
    clock: Callable[[], str] | None = None,
    request_id_factory: Callable[[], str] | None = None,
    runtime_options: Mapping[str, object] | None = None,
) -> FramedStdioWorker:
    """Create the one accepted framed-stdio worker and register this domain."""

    options: dict[str, object] = {}
    if worker_instance_id is not None:
        options["worker_instance_id"] = worker_instance_id
    if clock is not None:
        options["clock"] = clock
    if request_id_factory is not None:
        options["request_id_factory"] = request_id_factory
    worker = FramedStdioWorker(
        plugin_id=PLUGIN_ID,
        plugin_version=PLUGIN_VERSION,
        **options,
    )
    domain = _AutopilotDomain(runtime_options=runtime_options)
    worker.register_domain(
        CAPABILITY_ID,
        descriptor=lambda release_id: capability_descriptor(release_id=release_id),
        operations=(CAPABILITY_ID,),
        start=domain.start,
        resume=domain.resume,
        cancel=domain.cancel,
    )
    return worker


def main() -> None:
    """Run the shared framed-stdio dispatcher for the manifest entrypoint."""

    build_worker().serve()


__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_INITIAL_BACKOFF_SECONDS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MAX_STAGE_POLLS",
    "DEFAULT_POLL_BUDGET_SECONDS",
    "MAX_HOST_ASSET_BYTES",
    "MAX_HOST_ASSET_PAGES",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "RESULT_CONTRACT",
    "AutopilotAssetPort",
    "AutopilotHostPort",
    "AutopilotRunResult",
    "AutopilotRuntime",
    "AutopilotRuntimeError",
    "CandidateProjection",
    "build_worker",
    "capability_descriptor",
    "main",
]
