"""WU-2B production Job ingress, policy and process coordination.

This module composes the already accepted P1/P2/P3 authorities.  It owns no
Job ledger, Asset store, release catalog or process supervisor of its own.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from backend.plotpilot_core.api.v1.jobs.rpc import AttemptStartBinding
from backend.plotpilot_core.api.v2.jobs.rpc.command_query import JobCommandResolution
from backend.plotpilot_core.broker.service import CapabilityBinding, CapabilityBroker
from backend.plotpilot_core.jobs.chapter_runtime import StartedChapterAttempt
from backend.plotpilot_core.jobs.http_rpc.chapter_handlers import StreamCommitPolicy
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.models import (
    AttemptFence,
    WorkerState,
    WorkerTicket,
)
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    assert_valid,
    canonical_bytes,
    parse_json_bytes,
    sha256_hex,
    verify_result_bundle,
    verify_snapshot,
    verify_stream_prefix,
)
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    validate_rpc_result,
)

_TERMINAL_JOB_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_ACTIVE_ATTEMPT_STATES = frozenset({"running", "cancelling"})
_JOB_RESULT_FIELDS = frozenset(
    {
        "schema",
        "operation_key",
        "workspace_id",
        "job_id",
        "command",
        "accepted",
        "idempotent",
        "terminal_known",
        "state",
        "job_revision",
        "snapshot_cursor",
    }
)
_CONTROL_AUTHORITY_RESULT = "authority_result"
_CONTROL_PUBLIC_RESPONSE = "public_response"


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{sha256_hex(material)[:48]}"


def _request_id(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "plotpilot:" + ":".join(map(str, parts))))


def _deadline() -> str:
    value = datetime.now(UTC) + timedelta(hours=1)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _external_state(state: str) -> str:
    return "needs_attention" if state in {"waiting_user", "interrupted"} else state


def _binding_from_row(row: Mapping[str, Any]) -> AttemptStartBinding:
    return AttemptStartBinding(
        job_id=str(row["job_id"]),
        step_id=str(row["step_id"]),
        attempt_id=str(row["attempt_id"]),
        worker_run_id=str(row["worker_run_id"]),
        plugin_id=str(row["plugin_id"]),
        release_id=str(row["release_id"]),
        package_hash=str(row["package_hash"]),
        capability_id=str(row["capability_id"]),
        generation_id=str(row["generation_id"]),
        lease_epoch=int(row["lease_epoch"]),
        preallocated_receipt_id=str(row["preallocated_receipt_id"]),
        expected_result_contract=str(row["expected_result_contract"]),
    )


def _resolution(
    authority: ExecutionAuthority,
    *,
    operation_key: str,
    workspace_id: str,
    job_id: str,
    command: str,
    idempotent: bool,
) -> JobCommandResolution:
    with authority.repository.read_connection() as connection:
        row = connection.execute(
            "SELECT job_state,job_revision,job_event_high_water FROM execution_job "
            "WHERE workspace_id=? AND job_id=?",
            (workspace_id, job_id),
        ).fetchone()
    if row is None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
    state = _external_state(str(row["job_state"]))
    return JobCommandResolution(
        operation_key=operation_key,
        workspace_id=workspace_id,
        job_id=job_id,
        command=command,
        accepted=True,
        idempotent=idempotent,
        terminal_known=state in _TERMINAL_JOB_STATES,
        state=state,
        job_revision=int(row["job_revision"]),
        snapshot_cursor=f"job/{job_id}/{int(row['job_event_high_water'])}",
    )


def _stored_resolution(
    value: Mapping[str, Any],
    *,
    operation_key: str,
    workspace_id: str,
    job_id: str,
    command: str,
) -> JobCommandResolution:
    """Rehydrate the exact durable public result without reading later Job state."""

    if set(value) != _JOB_RESULT_FIELDS or value.get("schema") != "job-command-result/v2":
        raise ContractError(
            ErrorCode.ASSET_ERROR, "stored Job command response has invalid fields"
        )
    if (
        value.get("operation_key") != operation_key
        or value.get("workspace_id") != workspace_id
        or value.get("job_id") != job_id
        or value.get("command") != command
    ):
        raise ContractError(
            ErrorCode.ASSET_ERROR, "stored Job command response identity drifted"
        )
    if (
        not isinstance(value.get("accepted"), bool)
        or not isinstance(value.get("idempotent"), bool)
        or not isinstance(value.get("terminal_known"), bool)
        or not isinstance(value.get("state"), str)
        or isinstance(value.get("job_revision"), bool)
        or not isinstance(value.get("job_revision"), int)
        or int(value["job_revision"]) < 1
        or not isinstance(value.get("snapshot_cursor"), str)
    ):
        raise ContractError(
            ErrorCode.ASSET_ERROR, "stored Job command response types drifted"
        )
    return JobCommandResolution(
        operation_key=operation_key,
        workspace_id=workspace_id,
        job_id=job_id,
        command=command,
        accepted=bool(value["accepted"]),
        idempotent=bool(value["idempotent"]),
        terminal_known=bool(value["terminal_known"]),
        state=str(value["state"]),
        job_revision=int(value["job_revision"]),
        snapshot_cursor=str(value["snapshot_cursor"]),
    )


@dataclass(frozen=True, slots=True)
class ProductionCapabilitySelection:
    generation_id: str
    worker_id: str
    plugin_id: str
    release_id: str
    package_hash: str
    data_generation_id: str | None
    version: str
    capability_id: str
    result_contract: str


class ProductionCapabilityRuntime:
    """Immutable CapabilityBroker release/binding view of one Generation."""

    def __init__(self, plugin_runtime: Any) -> None:
        self.plugin_runtime = plugin_runtime
        self.repository = plugin_runtime.repository
        self.assets = plugin_runtime.assets
        self._generation_id: str | None = None
        self._selections: tuple[ProductionCapabilitySelection, ...] = ()
        self._load_current()
        bindings: dict[str, CapabilityBinding] = {}
        for selected in self._selections:
            binding = CapabilityBinding(
                binding_id=selected.capability_id,
                capability_id=selected.capability_id,
                plugin_id=selected.plugin_id,
                release_requirement=selected.version,
                result_contract=selected.result_contract,
                required=True,
                propagate_cancel=True,
            )
            if binding.binding_id in bindings:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "current Generation exports an ambiguous capability binding",
                )
            bindings[binding.binding_id] = binding
        self.bindings: Mapping[str, CapabilityBinding] = MappingProxyType(bindings)

    def _load_current(self) -> None:
        try:
            state = self.plugin_runtime.lifecycle.generation_state()
        except Exception as exc:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Disabled: current Generation authority is unavailable",
            ) from exc
        if state.safe_mode or state.current is None:
            return
        generation = dict(state.current)
        generation_id = str(generation["generation_id"])
        selected: list[ProductionCapabilitySelection] = []
        for member in generation["members"]:
            plugin_id = str(member["plugin_id"])
            availability = self.plugin_runtime.routes.availability(plugin_id)
            route = availability.route
            if route is None:
                continue
            try:
                package = self.plugin_runtime.packages.require_release(
                    release_id=str(member["release_id"]),
                    plugin_id=plugin_id,
                    package_hash=str(member["package_hash"]),
                )
            except (ContractError, KeyError, OSError, TypeError, ValueError):
                continue
            if (
                route.generation_id != generation_id
                or route.release_id != package.release_id
                or route.package_hash != package.package_hash
                or route.data_generation_id != member["data_generation_id"]
                or package.kind != "code"
            ):
                continue
            capabilities = package.manifest.get("capabilities")
            if not isinstance(capabilities, list):
                continue
            for descriptor in capabilities:
                if not isinstance(descriptor, Mapping):
                    continue
                operations = descriptor.get("operations")
                if not isinstance(operations, list) or "run" not in operations:
                    continue
                selected.append(
                    ProductionCapabilitySelection(
                        generation_id=generation_id,
                        worker_id=plugin_id,
                        plugin_id=plugin_id,
                        release_id=package.release_id,
                        package_hash=package.package_hash,
                        data_generation_id=route.data_generation_id,
                        version=package.version,
                        capability_id=str(descriptor["capability_id"]),
                        result_contract=str(descriptor["result_contract"]),
                    )
                )
        self._generation_id = generation_id
        self._selections = tuple(selected)

    def current_generation(self) -> str:
        if self._generation_id is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Disabled: no executable current Generation is installed",
            )
        state = self.plugin_runtime.lifecycle.generation_state()
        current = None if state.current is None else state.current.get("generation_id")
        if state.safe_mode or current != self._generation_id:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "CapabilityBroker Generation binding is stale",
            )
        return self._generation_id

    def resolve_release(self, plugin_id: str, requirement: str) -> str:
        generation_id = self.current_generation()
        matches = [
            item
            for item in self._selections
            if item.generation_id == generation_id
            and item.plugin_id == plugin_id
            and item.version == requirement
        ]
        release_ids = {item.release_id for item in matches}
        if len(release_ids) != 1:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "exact installed Capability release is unavailable",
            )
        selected = matches[0]
        availability = self.plugin_runtime.routes.availability(plugin_id)
        route = availability.route
        if route is None or (
            route.generation_id != generation_id
            or route.release_id != selected.release_id
            or route.package_hash != selected.package_hash
            or route.data_generation_id != selected.data_generation_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "installed Capability route is no longer executable",
            )
        return selected.release_id

    def select(self, capability_id: str) -> ProductionCapabilitySelection:
        generation_id = self.current_generation()
        matches = [
            item
            for item in self._selections
            if item.generation_id == generation_id
            and item.capability_id == capability_id
        ]
        if len(matches) != 1:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Disabled: no unique verified installed release provides the capability",
            )
        selected = matches[0]
        self.resolve_release(selected.plugin_id, selected.version)
        return selected


class ProductionIngressGate:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._accepting = True
        self._admitted = 0

    def require_open(self) -> None:
        with self._lock:
            if not self._accepting:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "production Job runtime ingress is stopped",
                )

    @contextmanager
    def admit(self):
        """Count one accepted resolver call until all durable work is finished."""

        with self._condition:
            if not self._accepting:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "production Job runtime ingress is stopped",
                )
            self._admitted += 1
        try:
            yield
        finally:
            with self._condition:
                self._admitted -= 1
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._accepting = False

    def drain(self) -> None:
        with self._condition:
            while self._admitted:
                self._condition.wait()

    def close_and_drain(self) -> None:
        self.close()
        self.drain()


class ProductionAttemptRegistry:
    """Process transport handles, revalidated against SQLite on every use."""

    def __init__(self, authority: ExecutionAuthority) -> None:
        self.authority = authority
        self.repository = authority.repository
        self._lock = threading.RLock()
        self._started: dict[str, StartedChapterAttempt] = {}

    def track(self, started: StartedChapterAttempt) -> None:
        binding = started.binding
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT a.state,a.worker_run_id,s.active_attempt_id,j.job_state "
                "FROM execution_attempt a JOIN execution_step s "
                "ON s.job_id=a.job_id AND s.step_id=a.step_id "
                "JOIN execution_job j ON j.job_id=a.job_id WHERE a.attempt_id=?",
                (binding.attempt_id,),
            ).fetchone()
        if row is None or (
            row["worker_run_id"] != started.ticket.lifecycle_id
            or row["active_attempt_id"] != binding.attempt_id
            or row["state"] not in _ACTIVE_ATTEMPT_STATES
            or row["job_state"] not in {"running", "cancelling"}
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE, "tracked Attempt is not durable and active"
            )
        with self._lock:
            old = self._started.get(binding.attempt_id)
            if old is not None and old != started:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Attempt is already tracked by another process lifecycle",
                )
            self._started[binding.attempt_id] = started

    def get(self, attempt_id: str) -> StartedChapterAttempt | None:
        with self._lock:
            return self._started.get(attempt_id)

    def detach(self, attempt_id: str) -> StartedChapterAttempt | None:
        with self._lock:
            return self._started.pop(attempt_id, None)

    def durable(self) -> tuple[StartedChapterAttempt, ...]:
        with self._lock:
            values = tuple(self._started.values())
        accepted: list[StartedChapterAttempt] = []
        with self.repository.read_connection() as connection:
            for started in values:
                binding = started.binding
                row = connection.execute(
                    "SELECT a.state,a.worker_run_id,s.active_attempt_id,j.job_state "
                    "FROM execution_attempt a JOIN execution_step s "
                    "ON s.job_id=a.job_id AND s.step_id=a.step_id "
                    "JOIN execution_job j ON j.job_id=a.job_id WHERE a.attempt_id=?",
                    (binding.attempt_id,),
                ).fetchone()
                if row is not None and (
                    row["state"] in _ACTIVE_ATTEMPT_STATES
                    and row["worker_run_id"] == started.ticket.lifecycle_id
                    and row["active_attempt_id"] == binding.attempt_id
                    and row["job_state"] in {"running", "cancelling"}
                ):
                    accepted.append(started)
        return tuple(accepted)

    def clear(self) -> None:
        with self._lock:
            self._started.clear()

    def tracked(self) -> tuple[StartedChapterAttempt, ...]:
        with self._lock:
            return tuple(self._started.values())


class ProductionWorkerLauncher:
    def __init__(
        self,
        authority: ExecutionAuthority,
        plugin_runtime: Any,
        capability_runtime: ProductionCapabilityRuntime,
        registry: ProductionAttemptRegistry,
    ) -> None:
        self.authority = authority
        self.plugin_runtime = plugin_runtime
        self.capability_runtime = capability_runtime
        self.supervisor = plugin_runtime.supervisor
        self.registry = registry

    def acquire(self, selected: ProductionCapabilitySelection) -> WorkerTicket:
        ticket = self.supervisor.acquire(
            selected.worker_id, expected_release_id=selected.release_id
        )
        try:
            deadline = time.monotonic() + float(
                self.supervisor._config.handshake_timeout
            )
            while True:
                status = self.supervisor.status(selected.worker_id)
                if (
                    status is not None
                    and status.lifecycle_id == ticket.lifecycle_id
                    and status.state == WorkerState.READY
                ):
                    return ticket
                if status is None or status.state in {
                    WorkerState.STOPPED,
                    WorkerState.CRASHED,
                    WorkerState.FAILED,
                    WorkerState.FENCED,
                }:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "verified installed worker failed before becoming ready",
                    )
                if time.monotonic() >= deadline:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "verified installed worker handshake did not become ready",
                    )
                time.sleep(0.005)
        except BaseException:
            self.supervisor.release(ticket)
            raise

    def bind_and_send(
        self,
        *,
        row: Mapping[str, Any],
        ticket: WorkerTicket,
        method: str,
        params: Mapping[str, object],
        request_id: str,
    ) -> StartedChapterAttempt:
        binding = _binding_from_row(row)
        if (
            binding.worker_run_id != ticket.lifecycle_id
            or binding.release_id != ticket.release_id
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "Attempt owner differs from the acquired process lifecycle",
            )
        fence = AttemptFence(
            binding.job_id,
            binding.step_id,
            binding.attempt_id,
            binding.lease_epoch,
            ticket.lifecycle_id,
        )
        self.supervisor.bind_attempt(ticket, fence)
        started = StartedChapterAttempt(ticket=ticket, binding=binding, fence=fence)
        meta = {
            "protocol_version": "1",
            "generation_id": binding.generation_id,
            "plugin_release_id": binding.release_id,
            "deadline_at": _deadline(),
            "context": "attempt",
            "operation_id": ticket.lifecycle_id,
            "job_id": binding.job_id,
            "step_id": binding.step_id,
            "attempt_id": binding.attempt_id,
            "lease_epoch": binding.lease_epoch,
        }
        self.supervisor.send_worker_request(
            ticket, method, params, meta, request_id=request_id
        )
        self.registry.track(started)
        return started

    def compensate(
        self,
        *,
        ticket: WorkerTicket | None,
        attempt_id: str | None,
        reason: str,
    ) -> None:
        if ticket is None:
            return
        if attempt_id is not None:
            started = self.registry.detach(attempt_id)
            fence = None if started is None else started.fence
            if fence is None:
                with self.authority.repository.read_connection() as connection:
                    row = connection.execute(
                        "SELECT job_id,step_id,lease_epoch,worker_run_id "
                        "FROM execution_attempt WHERE attempt_id=?",
                        (attempt_id,),
                    ).fetchone()
                if row is not None and row["worker_run_id"] == ticket.lifecycle_id:
                    fence = AttemptFence(
                        str(row["job_id"]),
                        str(row["step_id"]),
                        attempt_id,
                        int(row["lease_epoch"]),
                        ticket.lifecycle_id,
                    )
            if fence is not None:
                try:
                    self.supervisor.interrupt_attempt(ticket, fence, reason)
                except Exception:  # noqa: BLE001, S110 -- bounded compensation
                    pass
                self.authority.mark_production_attempt_needs_attention(
                    attempt_id=attempt_id,
                    worker_run_id=ticket.lifecycle_id,
                    reason=reason,
                )
        try:
            self.supervisor.release(ticket)
        except Exception:  # noqa: BLE001, S110 -- bounded compensation
            pass

    @staticmethod
    def cancel_request_id(operation_key: str) -> str:
        return _request_id("job-cancel", operation_key)

    def send_cancel(
        self,
        started: StartedChapterAttempt,
        *,
        operation_key: str,
        reason: str,
    ) -> str:
        binding = started.binding
        if binding.worker_run_id != started.ticket.lifecycle_id:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "cancel belongs to another process lifecycle",
            )
        request_id = self.cancel_request_id(operation_key)
        self.supervisor.send_worker_request(
            started.ticket,
            "job.cancel",
            {"worker_run_id": binding.worker_run_id, "reason": reason},
            {
                "protocol_version": "1",
                "generation_id": binding.generation_id,
                "plugin_release_id": binding.release_id,
                "deadline_at": _deadline(),
                "context": "attempt",
                "operation_id": operation_key,
                "job_id": binding.job_id,
                "step_id": binding.step_id,
                "attempt_id": binding.attempt_id,
                "lease_epoch": binding.lease_epoch,
            },
            request_id=request_id,
        )
        return request_id

    def take_cancel_response(
        self, started: StartedChapterAttempt, *, operation_key: str
    ) -> Mapping[str, Any] | None:
        event = self.supervisor.take_worker_response(
            started.ticket, self.cancel_request_id(operation_key)
        )
        if event is None:
            return None
        message = event.message
        if "error" in message:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Worker rejected the durable job.cancel request",
            )
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Worker job.cancel response is not an object",
            )
        validate_rpc_result("job.cancel", result)
        return dict(result)

    def launch_created_attempt(self, row: Mapping[str, Any]) -> None:
        selected = self.capability_runtime.select(str(row["capability_id"]))
        if (
            selected.plugin_id != row["plugin_id"]
            or selected.release_id != row["release_id"]
            or selected.generation_id != row["generation_id"]
            or selected.result_contract != row["expected_result_contract"]
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "queued child Attempt no longer matches current Generation",
            )
        ticket: WorkerTicket | None = None
        committed = False
        try:
            ticket = self.acquire(selected)
            self.authority.start_attempt(
                job_id=str(row["job_id"]),
                step_id=str(row["step_id"]),
                attempt_id=str(row["attempt_id"]),
                worker_run_id=ticket.lifecycle_id,
                plugin_id=selected.plugin_id,
                release_id=selected.release_id,
                package_hash=selected.package_hash,
                capability_id=selected.capability_id,
                generation_id=selected.generation_id,
                lease_epoch=int(row["lease_epoch"]),
                preallocated_receipt_id=str(row["preallocated_receipt_id"]),
                expected_result_contract=selected.result_contract,
            )
            committed = True
            with self.authority.repository.read_connection() as connection:
                attempt = connection.execute(
                    "SELECT * FROM execution_attempt WHERE attempt_id=?",
                    (row["attempt_id"],),
                ).fetchone()
            if attempt is None:
                raise ContractError(ErrorCode.ASSET_ERROR, "child Attempt disappeared")
            self.bind_and_send(
                row=dict(attempt),
                ticket=ticket,
                method="job.start",
                params={
                    "capability_id": selected.capability_id,
                    "run_snapshot_asset_id": str(row["run_snapshot_asset_id"]),
                    "checkpoint_asset_id": None,
                    "secrets": None,
                },
                request_id=_request_id("child-start", row["attempt_id"]),
            )
        except BaseException:
            self.compensate(
                ticket=ticket,
                attempt_id=str(row["attempt_id"]) if committed else None,
                reason="queued child process activation failed",
            )
            raise


class ProductionJobStartResolver:
    def __init__(
        self,
        authority: ExecutionAuthority,
        capability_runtime: ProductionCapabilityRuntime,
        launcher: ProductionWorkerLauncher,
        ingress: ProductionIngressGate,
    ) -> None:
        self.authority = authority
        self.repository = authority.repository
        self.assets = authority.assets
        self.capability_runtime = capability_runtime
        self.launcher = launcher
        self.ingress = ingress
        self._lock = threading.RLock()

    def _snapshot(self, command: Mapping[str, Any]) -> dict[str, Any]:
        asset_id = str(command["run_snapshot_asset_id"])
        try:
            self.assets.require(asset_id, mime="application/json")
            raw = self.assets.read(asset_id)
            value = parse_json_bytes(raw)
            if not isinstance(value, Mapping):
                raise ContractValidationError("RunSnapshot Asset is not an object")
            snapshot = dict(value)
            verify_snapshot(snapshot)
            if raw != canonical_bytes(snapshot):
                raise ContractValidationError("RunSnapshot Asset is not canonical")
            for reference in snapshot["asset_hashes"]:
                self.assets.require(
                    str(reference["asset_id"]), sha256=str(reference["sha256"])
                )
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "RunSnapshot Asset is missing, corrupt or non-canonical",
            ) from exc
        if (
            snapshot["workspace_id"] != command["workspace_id"]
            or snapshot["scope"]["operation"] != command["capability_id"]
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "RunSnapshot Workspace/capability differs from job.start",
            )
        return snapshot

    @staticmethod
    def _verify_snapshot_release(
        snapshot: Mapping[str, Any], selected: ProductionCapabilitySelection
    ) -> None:
        matches = [
            release
            for release in snapshot["plugin_releases"]
            if release["plugin_id"] == selected.plugin_id
        ]
        if len(matches) != 1:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "RunSnapshot does not bind exactly one selected plugin release",
            )
        release = matches[0]
        if (
            release["release_id"] != selected.release_id
            or release["package_hash"] != selected.package_hash
            or release["data_generation_id"] != selected.data_generation_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "RunSnapshot release/package/Generation binding is stale",
            )

    def _replay(
        self, command: Mapping[str, Any], receipt: Mapping[str, Any]
    ) -> JobCommandResolution:
        request_json = json.dumps(
            dict(command), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        request_hash = sha256_hex(canonical_bytes(dict(command)))
        if (
            receipt["request_hash"] != request_hash
            or receipt["request_json"] != request_json
        ):
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "job.start operation key was reused with different content",
            )
        if receipt["state"] != "completed":
            raise ContractError(
                ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                "job.start outcome is uncertain and will not be replayed automatically",
            )
        try:
            response = json.loads(str(receipt["response_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "completed job.start receipt is corrupt"
            ) from exc
        if not isinstance(response, dict):
            raise ContractError(
                ErrorCode.ASSET_ERROR, "completed job.start response closure drifted"
            )
        stored_resolution = _stored_resolution(
            response,
            operation_key=str(command["operation_key"]),
            workspace_id=str(command["workspace_id"]),
            job_id=str(command["job_id"]),
            command="start",
        )
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT j.run_snapshot_asset_id,j.run_snapshot_hash,s.step_id,"
                "a.attempt_id,a.worker_run_id,a.plugin_id,a.release_id,a.package_hash,"
                "a.capability_id,a.generation_id,a.lease_epoch "
                "FROM execution_job j JOIN execution_step s ON s.job_id=j.job_id "
                "JOIN execution_attempt a ON a.job_id=j.job_id AND a.step_id=s.step_id "
                "WHERE j.workspace_id=? AND j.job_id=? AND s.step_id=? AND a.attempt_id=?",
                (
                    receipt["workspace_id"],
                    receipt["job_id"],
                    receipt["step_id"],
                    receipt["attempt_id"],
                ),
            ).fetchone()
        if row is None or tuple(
            row[name]
            for name in (
                "run_snapshot_asset_id",
                "run_snapshot_hash",
                "step_id",
                "attempt_id",
                "worker_run_id",
                "plugin_id",
                "release_id",
                "package_hash",
                "capability_id",
                "generation_id",
                "lease_epoch",
            )
        ) != tuple(
            receipt[name]
            for name in (
                "run_snapshot_asset_id",
                "run_snapshot_hash",
                "step_id",
                "attempt_id",
                "worker_run_id",
                "plugin_id",
                "release_id",
                "package_hash",
                "capability_id",
                "generation_id",
                "writer_epoch",
            )
        ):
            raise ContractError(
                ErrorCode.ASSET_ERROR, "completed job.start authority closure drifted"
            )
        return stored_resolution

    def resolve_start(self, command: Mapping[str, Any]) -> JobCommandResolution:
        with self.ingress.admit():
            return self._resolve_start(command)

    def _resolve_start(self, command: Mapping[str, Any]) -> JobCommandResolution:
        with self._lock:
            operation_key = str(command["operation_key"])
            existing = self.authority.get_production_start_receipt(operation_key)
            if existing is not None:
                return self._replay(command, existing)
            snapshot = self._snapshot(command)
            selected = self.capability_runtime.select(str(command["capability_id"]))
            self._verify_snapshot_release(snapshot, selected)
            if int(command["writer_epoch"]) != 1:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "job.start writer epoch is not the authoritative first epoch",
                )
            step_id = _stable_id("step", command["job_id"], command["capability_id"])
            attempt_id = _stable_id(
                "attempt", command["job_id"], operation_key, snapshot["snapshot_hash"]
            )
            receipt = self.authority.reserve_production_start(
                request=command,
                snapshot=snapshot,
                run_snapshot_asset_id=str(command["run_snapshot_asset_id"]),
                step_id=step_id,
                attempt_id=attempt_id,
                plugin_id=selected.plugin_id,
                generation_id=selected.generation_id,
                release_id=selected.release_id,
                package_hash=selected.package_hash,
                capability_id=selected.capability_id,
                result_contract=selected.result_contract,
            )
            request_hash = str(receipt["request_hash"])
            ticket: WorkerTicket | None = None
            committed = False
            try:
                ticket = self.launcher.acquire(selected)
                self.authority.bind_production_start_worker(
                    operation_key=operation_key,
                    request_hash=request_hash,
                    worker_run_id=ticket.lifecycle_id,
                )
                attempt = self.authority.commit_production_start(
                    operation_key=operation_key,
                    request_hash=request_hash,
                    snapshot=snapshot,
                )
                committed = True
                self.launcher.bind_and_send(
                    row=attempt,
                    ticket=ticket,
                    method="job.start",
                    params={
                        "capability_id": selected.capability_id,
                        "run_snapshot_asset_id": str(command["run_snapshot_asset_id"]),
                        "checkpoint_asset_id": None,
                        "secrets": None,
                    },
                    request_id=_request_id("job-start", operation_key),
                )
                resolution = _resolution(
                    self.authority,
                    operation_key=operation_key,
                    workspace_id=str(command["workspace_id"]),
                    job_id=str(command["job_id"]),
                    command="start",
                    idempotent=False,
                )
                self.authority.complete_production_start(
                    operation_key=operation_key,
                    request_hash=request_hash,
                    response=resolution.to_dict(),
                )
                return resolution
            except BaseException as exc:
                self.authority.mark_production_start_uncertain(
                    operation_key=operation_key, reason=str(exc) or type(exc).__name__
                )
                self.launcher.compensate(
                    ticket=ticket,
                    attempt_id=attempt_id if committed else None,
                    reason="job.start process/authority activation failed",
                )
                if isinstance(exc, ContractError):
                    raise
                if isinstance(exc, Exception):
                    raise ContractError(
                        ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                        "job.start process outcome is uncertain",
                    ) from exc
                raise


class ProductionJobControlResolver:
    def __init__(
        self,
        authority: ExecutionAuthority,
        launcher: ProductionWorkerLauncher,
        registry: ProductionAttemptRegistry,
        capability_runtime: ProductionCapabilityRuntime,
        ingress: ProductionIngressGate,
    ) -> None:
        self.authority = authority
        self.repository = authority.repository
        self.launcher = launcher
        self.registry = registry
        self.capability_runtime = capability_runtime
        self.ingress = ingress
        self._lock = threading.RLock()

    def _job(self, command: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.repository.read_connection() as connection:
            job = connection.execute(
                "SELECT * FROM execution_job WHERE workspace_id=? AND job_id=?",
                (command["workspace_id"], command["job_id"]),
            ).fetchone()
            if job is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            rows = connection.execute(
                "SELECT s.step_id,s.state AS step_state,s.active_attempt_id,"
                "a.* FROM execution_step s LEFT JOIN execution_attempt a "
                "ON a.attempt_id=s.active_attempt_id WHERE s.job_id=? "
                "ORDER BY s.step_ordinal,s.step_id",
                (command["job_id"],),
            ).fetchall()
        active = [row for row in rows if row["attempt_id"] is not None]
        if len(active) != 1:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Job control requires one exact active Attempt lineage",
            )
        return dict(job), dict(active[0])

    def _checkpoint(
        self, job_id: str, step_id: str
    ) -> tuple[dict[str, Any], str] | None:
        checkpoint = self.authority.checkpoint_store.get_latest(job_id, step_id)
        if checkpoint is None:
            return None
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT json_extract(e.event_json,'$.payload_asset_id') "
                "AS payload_asset_id FROM execution_checkpoint c "
                "JOIN execution_job_event e ON e.job_id=c.job_id "
                "AND e.job_event_seq=c.job_event_seq "
                "WHERE c.job_id=? AND c.step_id=? AND c.checkpoint_id=?",
                (job_id, step_id, checkpoint["checkpoint_id"]),
            ).fetchone()
        if row is None or row["payload_asset_id"] is None:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "checkpoint authority is missing its immutable Asset",
            )
        return checkpoint, str(row["payload_asset_id"])

    def _replay(
        self, command: Mapping[str, Any], row: Mapping[str, Any]
    ) -> JobCommandResolution:
        operation = str(command["command"])
        if row["operation"] != operation or row["method"] != f"job.{operation}":
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "control operation key is bound to another command",
            )
        try:
            request = json.loads(str(row["request_json"]))
            response_envelope = json.loads(str(row["response_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "durable control operation is corrupt"
            ) from exc
        if not isinstance(request, dict) or not isinstance(response_envelope, dict):
            raise ContractError(
                ErrorCode.ASSET_ERROR, "durable control operation closure is invalid"
            )
        if set(response_envelope) != {
            _CONTROL_AUTHORITY_RESULT,
            _CONTROL_PUBLIC_RESPONSE,
        }:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "durable control operation lacks its exact public response",
            )
        authority_result = response_envelope[_CONTROL_AUTHORITY_RESULT]
        public_response = response_envelope[_CONTROL_PUBLIC_RESPONSE]
        if not isinstance(authority_result, Mapping) or not isinstance(
            public_response, Mapping
        ):
            raise ContractError(
                ErrorCode.ASSET_ERROR, "durable control response envelope is invalid"
            )
        if sha256_hex(canonical_bytes(request)) != row["payload_hash"]:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "durable control request hash drifted"
            )
        reason_name = "resume_reason" if operation == "resume" else "reason"
        if (
            request.get("job_id") != command["job_id"]
            or request.get("operation_key") != command["operation_key"]
            or request.get(reason_name) != command["reason"]
            or request.get("expected_job_revision")
            != command["expected_job_revision"]
        ):
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "control operation key was reused with different content",
            )
        if operation == "resume":
            expected_attempt = _stable_id(
                "attempt-resume",
                command["job_id"],
                command["operation_key"],
                command["resume_intent_id"],
            )
            if row["attempt_id"] != expected_attempt:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "resume intent differs from the durable operation",
                )
        return _stored_resolution(
            public_response,
            operation_key=str(command["operation_key"]),
            workspace_id=str(command["workspace_id"]),
            job_id=str(command["job_id"]),
            command=operation,
        )

    def resolve_control(self, command: Mapping[str, Any]) -> JobCommandResolution:
        with self.ingress.admit():
            return self._resolve_control(command)

    def _resolve_control(self, command: Mapping[str, Any]) -> JobCommandResolution:
        with self._lock:
            with self.repository.read_connection() as connection:
                stored = connection.execute(
                    "SELECT * FROM execution_control_operation "
                    "WHERE job_id=? AND operation_key=?",
                    (command["job_id"], command["operation_key"]),
                ).fetchone()
            if stored is not None:
                return self._replay(command, dict(stored))
            job, attempt = self._job(command)
            if int(command["expected_job_revision"]) != int(job["job_revision"]):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "expected Job revision CAS failed"
                )
            operation = str(command["command"])
            if operation == "pause":
                checkpoint_binding = self._checkpoint(
                    str(job["job_id"]), str(attempt["step_id"])
                )
                if checkpoint_binding is None:
                    raise ContractError(
                        ErrorCode.CHECKPOINT_INVALID,
                        "pause requires the exact active Attempt checkpoint",
                    )
                checkpoint, checkpoint_asset_id = checkpoint_binding
                if (
                    checkpoint["source_attempt_id"] != attempt["attempt_id"]
                    or int(checkpoint["lease_epoch"]) != int(attempt["lease_epoch"])
                ):
                    raise ContractError(
                        ErrorCode.CHECKPOINT_INVALID,
                        "pause requires the exact active Attempt checkpoint",
                    )
                self.authority.pause_from_committed_checkpoint(
                    job_id=str(job["job_id"]),
                    step_id=str(attempt["step_id"]),
                    attempt_id=str(attempt["attempt_id"]),
                    lease_epoch=int(attempt["lease_epoch"]),
                    operation_key=str(command["operation_key"]),
                    worker_run_id=str(attempt["worker_run_id"]),
                    reason=str(command["reason"]),
                    checkpoint=checkpoint,
                    checkpoint_asset_id=checkpoint_asset_id,
                    expected_job_revision=int(command["expected_job_revision"]),
                    store_public_response=True,
                )
                started = self.registry.detach(str(attempt["attempt_id"]))
                if started is not None:
                    self.launcher.supervisor.release(started.ticket)
            elif operation == "cancel":
                decision = self.authority.control_port.cancel(
                    job_id=str(job["job_id"]),
                    step_id=str(attempt["step_id"]),
                    attempt_id=str(attempt["attempt_id"]),
                    lease_epoch=int(attempt["lease_epoch"]),
                    operation_key=str(command["operation_key"]),
                    worker_run_id=str(attempt["worker_run_id"]),
                    reason=str(command["reason"]),
                    expected_job_revision=int(command["expected_job_revision"]),
                    store_public_response=True,
                )
                if not bool(decision.result["terminal_known"]):
                    started = self.registry.get(str(attempt["attempt_id"]))
                    if started is None:
                        raise ContractError(
                            ErrorCode.STALE_LEASE,
                            "cancel requires the exact tracked process lifecycle",
                        )
                    try:
                        self.launcher.send_cancel(
                            started,
                            operation_key=str(command["operation_key"]),
                            reason=str(command["reason"]),
                        )
                    except BaseException:
                        self.launcher.compensate(
                            ticket=started.ticket,
                            attempt_id=str(attempt["attempt_id"]),
                            reason="job.cancel process delivery failed",
                        )
                        raise
            elif operation == "resume":
                if command["resume_intent_id"] is None:
                    raise ContractValidationError(
                        "resume requires a non-null resume_intent_id"
                    )
                checkpoint_binding = self._checkpoint(
                    str(job["job_id"]), str(attempt["step_id"])
                )
                if checkpoint_binding is None:
                    raise ContractError(
                        ErrorCode.CHECKPOINT_INVALID,
                        "resume requires the exact suspended Attempt checkpoint",
                    )
                checkpoint, checkpoint_asset_id = checkpoint_binding
                if checkpoint[
                    "source_attempt_id"
                ] != attempt["attempt_id"]:
                    raise ContractError(
                        ErrorCode.CHECKPOINT_INVALID,
                        "resume requires the exact suspended Attempt checkpoint",
                    )
                selected = self.capability_runtime.select(
                    str(attempt["capability_id"])
                )
                if (
                    selected.plugin_id != attempt["plugin_id"]
                    or selected.release_id != attempt["release_id"]
                    or selected.package_hash != attempt["package_hash"]
                    or selected.generation_id != attempt["generation_id"]
                ):
                    raise ContractError(
                        ErrorCode.INCOMPATIBLE_GENERATION,
                        "resume Attempt release is no longer current",
                    )
                ticket: WorkerTicket | None = None
                new_attempt_id = _stable_id(
                    "attempt-resume",
                    command["job_id"],
                    command["operation_key"],
                    command["resume_intent_id"],
                )
                committed = False
                try:
                    ticket = self.launcher.acquire(selected)
                    self.authority.control_port.resume(
                        job_id=str(job["job_id"]),
                        step_id=str(attempt["step_id"]),
                        operation_key=str(command["operation_key"]),
                        checkpoint=checkpoint,
                        checkpoint_asset_id=checkpoint_asset_id,
                        resume_of_attempt_id=str(attempt["attempt_id"]),
                        lease_epoch=int(attempt["lease_epoch"]),
                        worker_run_id=ticket.lifecycle_id,
                        new_attempt_id=new_attempt_id,
                        plugin_id=selected.plugin_id,
                        release_id=selected.release_id,
                        package_hash=selected.package_hash,
                        capability_id=selected.capability_id,
                        generation_id=selected.generation_id,
                        preallocated_receipt_id=_stable_id(
                            "receipt", new_attempt_id
                        ),
                        resume_reason=str(command["reason"]),
                        expected_job_revision=int(command["expected_job_revision"]),
                        store_public_response=True,
                    )
                    committed = True
                    with self.repository.read_connection() as connection:
                        new_attempt = connection.execute(
                            "SELECT * FROM execution_attempt WHERE attempt_id=?",
                            (new_attempt_id,),
                        ).fetchone()
                    if new_attempt is None:
                        raise ContractError(
                            ErrorCode.ASSET_ERROR, "resumed Attempt disappeared"
                        )
                    self.launcher.bind_and_send(
                        row=dict(new_attempt),
                        ticket=ticket,
                        method="job.resume",
                        params={
                            "capability_id": selected.capability_id,
                            "run_snapshot_asset_id": str(job["run_snapshot_asset_id"]),
                            "resume_of_attempt_id": str(attempt["attempt_id"]),
                            "checkpoint_asset_id": checkpoint.get("state_asset_id"),
                            "resume_intent_id": str(command["resume_intent_id"]),
                            "resume_reason": str(command["reason"]),
                            "secrets": None,
                        },
                        request_id=_request_id(
                            "job-resume", command["operation_key"]
                        ),
                    )
                except BaseException:
                    self.launcher.compensate(
                        ticket=ticket,
                        attempt_id=new_attempt_id if committed else None,
                        reason="job.resume process/authority activation failed",
                    )
                    raise
            else:
                raise ContractValidationError(
                    f"unsupported production Job control: {operation}"
                )
            with self.repository.read_connection() as connection:
                stored = connection.execute(
                    "SELECT * FROM execution_control_operation "
                    "WHERE job_id=? AND operation_key=?",
                    (command["job_id"], command["operation_key"]),
                ).fetchone()
            if stored is None:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "control operation lost its durable public response",
                )
            return self._replay(command, dict(stored))


class ProductionProvenanceReceiptResolver:
    def __init__(self, authority: ExecutionAuthority) -> None:
        self.authority = authority
        self.repository = authority.repository
        self.assets = authority.assets

    def resolve_provenance_receipt(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        meta = request.get("meta")
        params = request.get("params")
        if not isinstance(meta, Mapping) or not isinstance(params, Mapping):
            raise ContractValidationError("completion request is not an object")
        with self.repository.read_connection() as connection:
            attempt = connection.execute(
                "SELECT a.*,j.workspace_id,j.run_snapshot_hash,j.job_state,"
                "s.active_attempt_id,s.expected_result_contract AS step_contract "
                "FROM execution_attempt a JOIN execution_job j ON j.job_id=a.job_id "
                "JOIN execution_step s ON s.job_id=a.job_id AND s.step_id=a.step_id "
                "WHERE a.job_id=? AND a.step_id=? AND a.attempt_id=? AND a.lease_epoch=?",
                (
                    meta.get("job_id"),
                    meta.get("step_id"),
                    meta.get("attempt_id"),
                    meta.get("lease_epoch"),
                ),
            ).fetchone()
        if attempt is None or (
            attempt["active_attempt_id"] != attempt["attempt_id"]
            or attempt["worker_run_id"] != params.get("worker_run_id")
            or attempt["state"] not in _ACTIVE_ATTEMPT_STATES
            or attempt["job_state"] not in {"running", "cancelling"}
            or attempt["expected_result_contract"] != attempt["step_contract"]
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "completion does not belong to the active durable Attempt",
            )
        bundle_asset_id = params.get("result_bundle_asset_id")
        bundle: Mapping[str, Any] | None = None
        bundle_hash: str | None = None
        if bundle_asset_id is not None:
            try:
                raw = self.assets.read(str(bundle_asset_id))
                value = parse_json_bytes(raw)
                if not isinstance(value, Mapping):
                    raise ContractValidationError("result Bundle is not an object")
                bundle = dict(value)
                if raw != canonical_bytes(bundle):
                    raise ContractValidationError("result Bundle is not canonical")
                verify_result_bundle(
                    bundle,
                    snapshot_hash_value=str(attempt["run_snapshot_hash"]),
                    snapshot_workspace_id=str(attempt["workspace_id"]),
                )
                bundle_hash = sha256_hex(raw)
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError(
                    ErrorCode.ASSET_ERROR, "result Bundle Asset is invalid"
                ) from exc
            producer = bundle["producer"]
            expected = (
                attempt["plugin_id"],
                attempt["release_id"],
                attempt["capability_id"],
                attempt["job_id"],
                attempt["step_id"],
                attempt["attempt_id"],
                attempt["lease_epoch"],
            )
            actual = tuple(
                producer[name]
                for name in (
                    "plugin_id",
                    "release_id",
                    "capability_id",
                    "job_id",
                    "step_id",
                    "attempt_id",
                    "lease_epoch",
                )
            )
            if actual != expected or bundle["contract_id"] != attempt[
                "expected_result_contract"
            ]:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "result Bundle is outside the active Attempt/result contract",
                )
        parent_receipts: list[str] = []
        with self.repository.read_connection() as connection:
            children = connection.execute(
                "SELECT child_job_id FROM p3_broker_child_record "
                "WHERE json_extract(record_json,'$.parent_attempt_id')=? "
                "ORDER BY child_job_id",
                (attempt["attempt_id"],),
            ).fetchall()
            for child in children:
                child_job = connection.execute(
                    "SELECT job_state,provenance_receipt_id FROM execution_job "
                    "WHERE job_id=?",
                    (child["child_job_id"],),
                ).fetchone()
                if (
                    child_job is None
                    or child_job["job_state"] not in _TERMINAL_JOB_STATES
                    or child_job["provenance_receipt_id"] is None
                ):
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "parent completion requires every durable child receipt",
                    )
                receipt_row = connection.execute(
                    "SELECT receipt_hash,receipt_json FROM execution_receipt "
                    "WHERE receipt_id=? AND job_id=?",
                    (
                        child_job["provenance_receipt_id"],
                        child["child_job_id"],
                    ),
                ).fetchone()
                if receipt_row is None:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR, "child provenance receipt is missing"
                    )
                receipt = json.loads(str(receipt_row["receipt_json"]))
                assert_valid("provenance-receipt/v1", receipt)
                if receipt["receipt_hash"] != receipt_row[
                    "receipt_hash"
                ] or receipt["receipt_hash"] != hash_without_field(
                    receipt, "receipt_hash", "provenance-receipt/v1"
                ):
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "child provenance receipt closure drifted",
                    )
                parent_receipts.append(str(receipt["receipt_id"]))
        staged_items: list[str] = []
        skill_refs: list[Mapping[str, Any]] = []
        if bundle is not None:
            skill_refs = list(bundle["skill_chain_result_refs"])
            if bundle["contract_id"] == "candidate-batch/v1":
                staged_items = [
                    str(item["item_id"])
                    for item in bundle["items"]
                    if item["status"] not in {"failed", "skipped"}
                ]
        receipt: dict[str, Any] = {
            "schema": "provenance-receipt/v1",
            "receipt_id": str(attempt["preallocated_receipt_id"]),
            "plugin_id": str(attempt["plugin_id"]),
            "release_id": str(attempt["release_id"]),
            "package_hash": str(attempt["package_hash"]),
            "capability_id": str(attempt["capability_id"]),
            "job_id": str(attempt["job_id"]),
            "step_id": str(attempt["step_id"]),
            "attempt_id": str(attempt["attempt_id"]),
            "lease_epoch": int(attempt["lease_epoch"]),
            "run_snapshot_hash": str(attempt["run_snapshot_hash"]),
            "bundle_id": None if bundle is None else bundle["bundle_id"],
            "bundle_hash": bundle_hash,
            "parent_receipt_ids": parent_receipts,
            "model_receipt_ids": [],
            "skill_chain_result_refs": skill_refs,
            "staged_items": staged_items,
            "created_at": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        receipt["receipt_hash"] = hash_without_field(
            receipt, "receipt_hash", "provenance-receipt/v1"
        )
        assert_valid("provenance-receipt/v1", receipt)
        return receipt


class ProductionStreamCommitPolicyResolver:
    """Resolve logical progress only from durable Core-owned Assets/checkpoints."""

    def __init__(self, authority: ExecutionAuthority) -> None:
        self.authority = authority
        self.repository = authority.repository
        self.assets = authority.assets

    @staticmethod
    def _policy(value: Mapping[str, Any]) -> StreamCommitPolicy:
        return StreamCommitPolicy(
            completed_units=value["completed_units"],
            total_units=value.get("total_units"),
            replay_policy=str(value.get("replay_policy", "checkpoint_resume")),
            provider_outcome=str(value.get("provider_outcome", "confirmed")),
            provider_replay_policy=str(
                value.get("provider_replay_policy", "idempotent_auto")
            ),
        )

    def resolve_stream_commit_policy(
        self,
        request: Mapping[str, Any],
        stream_prefix: Mapping[str, Any],
        attempt: AttemptStartBinding,
    ) -> StreamCommitPolicy:
        verify_stream_prefix(stream_prefix)
        expected = (
            attempt.job_id,
            attempt.step_id,
            attempt.attempt_id,
            attempt.lease_epoch,
        )
        actual = tuple(
            stream_prefix[name]
            for name in ("job_id", "step_id", "attempt_id", "lease_epoch")
        )
        if actual != expected:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "stream policy request is outside the active Attempt",
            )
        params = request.get("params")
        requested_asset = (
            params.get("stream_prefix_asset_id")
            if isinstance(params, Mapping)
            else None
        )
        latest = self.authority.checkpoint_store.get_latest(
            attempt.job_id, attempt.step_id
        )
        if latest is not None and latest.get("state_asset_id") is not None:
            try:
                state = parse_json_bytes(
                    self.assets.read(str(latest["state_asset_id"]))
                )
            except Exception as exc:
                raise ContractError(
                    ErrorCode.ASSET_ERROR, "stream checkpoint state Asset is invalid"
                ) from exc
            if isinstance(state, Mapping) and state.get(
                "stream_prefix_asset_id"
            ) == requested_asset:
                return self._policy(
                    {
                        "completed_units": latest["completed_units"],
                        "total_units": latest["total_units"],
                        "replay_policy": latest["replay_policy"],
                        "provider_outcome": state.get(
                            "provider_outcome", "confirmed"
                        ),
                        "provider_replay_policy": state.get(
                            "provider_replay_policy", "idempotent_auto"
                        ),
                    }
                )
        with self.repository.read_connection() as connection:
            job = connection.execute(
                "SELECT run_snapshot_json FROM execution_job WHERE job_id=?",
                (attempt.job_id,),
            ).fetchone()
        if job is None:
            raise ContractError(ErrorCode.STALE_LEASE, "stream Job disappeared")
        snapshot = json.loads(str(job["run_snapshot_json"]))
        verify_snapshot(snapshot)
        parameters_asset_id = snapshot.get("parameters_asset_id")
        if parameters_asset_id is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "logical stream progress policy is absent; byte/prefix counts are not authority",
            )
        try:
            policy_asset = parse_json_bytes(self.assets.read(parameters_asset_id))
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "stream policy parameters Asset is invalid"
            ) from exc
        if (
            not isinstance(policy_asset, Mapping)
            or policy_asset.get("schema") != "production-stream-policies/v1"
            or not isinstance(policy_asset.get("policies"), Sequence)
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "RunSnapshot parameters do not contain Core-owned logical stream policy",
            )
        matches = [
            item
            for item in policy_asset["policies"]
            if isinstance(item, Mapping)
            and item.get("stream_id") == stream_prefix["stream_id"]
            and item.get("output_role") == stream_prefix["output_role"]
        ]
        if len(matches) != 1:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "logical stream policy is missing or ambiguous",
            )
        return self._policy(matches[0])


def build_production_capability_broker(
    authority: ExecutionAuthority,
    runtime: ProductionCapabilityRuntime,
) -> CapabilityBroker:
    return CapabilityBroker(
        core=authority.assets,
        execution=authority,
        runtime=runtime,
        bindings=runtime.bindings,
        child_factory=authority,
        operation_ledger=authority.operation_ledger,
        child_records=authority.child_records,
        attempt_context=authority,
        receipt_port=authority,
    )


__all__ = [
    "ProductionAttemptRegistry",
    "ProductionCapabilityRuntime",
    "ProductionCapabilitySelection",
    "ProductionIngressGate",
    "ProductionJobControlResolver",
    "ProductionJobStartResolver",
    "ProductionProvenanceReceiptResolver",
    "ProductionStreamCommitPolicyResolver",
    "ProductionWorkerLauncher",
    "build_production_capability_broker",
]
