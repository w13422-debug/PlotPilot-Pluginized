"""Production composition and lifecycle owner for the frozen Jobs v2 runtime."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.plotpilot_core.bootstrap.production_plugin_runtime import (
    ProductionPluginRuntime,
)
from backend.plotpilot_core.jobs.chapter_runtime import StartedChapterAttempt
from backend.plotpilot_core.jobs.http_rpc.composition import (
    JobRuntimeComposition,
    compose_job_runtime,
)
from backend.plotpilot_core.jobs.production import (
    ProductionAttemptRegistry,
    ProductionCapabilityRuntime,
    ProductionIngressGate,
    ProductionJobControlResolver,
    ProductionJobStartResolver,
    ProductionProvenanceReceiptResolver,
    ProductionStreamCommitPolicyResolver,
    ProductionWorkerLauncher,
    build_production_capability_broker,
)
from backend.plotpilot_core.supervisor.models import WorkerFence
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode

_LOCAL_RUNTIME_LOCK = threading.Lock()
_LOCAL_RUNTIMES: dict[int, tuple[ProductionPluginRuntime, Any]] = {}


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


def _deadline() -> str:
    return (
        (datetime.now(UTC) + timedelta(minutes=5))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _claim_local_runtime(
    plugin_runtime: ProductionPluginRuntime, runtime: Any
) -> None:
    key = id(plugin_runtime)
    with _LOCAL_RUNTIME_LOCK:
        if key in _LOCAL_RUNTIMES:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "one ProductionPluginRuntime already has a live Job runtime",
            )
        _LOCAL_RUNTIMES[key] = (plugin_runtime, runtime)


def _release_local_runtime(
    plugin_runtime: ProductionPluginRuntime, runtime: Any
) -> None:
    key = id(plugin_runtime)
    with _LOCAL_RUNTIME_LOCK:
        current = _LOCAL_RUNTIMES.get(key)
        if (
            current is not None
            and current[0] is plugin_runtime
            and current[1] is runtime
        ):
            del _LOCAL_RUNTIMES[key]


@dataclass(frozen=True, slots=True)
class ProductionPumpResult:
    """Bounded observations from one explicitly requested pump cycle."""

    dispatched_request_ids: tuple[str, ...]
    passthrough_events: int
    launched_attempt_ids: tuple[str, ...]
    released_attempt_ids: tuple[str, ...]


def _release_stale_claims(plugin_runtime: ProductionPluginRuntime) -> int:
    """Release process claims left by an earlier host, using their exact fence."""

    with plugin_runtime.repository.read_connection() as connection:
        rows = connection.execute(
            "SELECT worker_id,pin_id,owner_id,plugin_id,generation_id,release_id,"
            "data_generation_id,pin_epoch,retire_epoch "
            "FROM plugin_supervisor_claim WHERE state='active' "
            "ORDER BY worker_id,pin_epoch"
        ).fetchall()
    released = 0
    for row in rows:
        fence = WorkerFence(
            worker_id=str(row["worker_id"]),
            pin_id=str(row["pin_id"]),
            plugin_id=str(row["plugin_id"]),
            generation_id=str(row["generation_id"]),
            release_id=str(row["release_id"]),
            data_generation_id=(
                None
                if row["data_generation_id"] is None
                else str(row["data_generation_id"])
            ),
            pin_epoch=int(row["pin_epoch"]),
            retire_epoch=int(row["retire_epoch"]),
        )
        if not plugin_runtime.supervisor_authority.release(
            fence, str(row["owner_id"])
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "stale production worker claim could not be released exactly",
            )
        released += 1
    return released


class ProductionJobPump:
    """Single-owner, caller-driven supervisor and Host-RPC pump."""

    def __init__(
        self,
        composition: JobRuntimeComposition,
        launcher: ProductionWorkerLauncher,
        registry: ProductionAttemptRegistry,
        ingress: ProductionIngressGate,
        provenance: ProductionProvenanceReceiptResolver,
        *,
        batch_limit: int = 64,
    ) -> None:
        if batch_limit < 1:
            raise ValueError("batch_limit must be positive")
        self.composition = composition
        self.authority = composition.authority
        self.repository = composition.repository
        self.supervisor = composition.supervisor
        self.dispatcher = composition.dispatcher
        self.launcher = launcher
        self.registry = registry
        self.ingress = ingress
        self.provenance = provenance
        self.batch_limit = batch_limit
        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._open = True

    def _require_open(self) -> None:
        with self._state_lock:
            if not self._open:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "production Job pump is stopped",
                )

    def _created_attempts(self) -> tuple[dict[str, Any], ...]:
        with self.repository.read_connection() as connection:
            rows = connection.execute(
                "SELECT a.*,j.run_snapshot_asset_id FROM execution_attempt a "
                "JOIN execution_step s ON s.job_id=a.job_id AND s.step_id=a.step_id "
                "JOIN execution_job j ON j.job_id=a.job_id "
                "WHERE a.state='created' AND a.worker_run_id IS NULL "
                "AND s.active_attempt_id IS NULL AND s.state='pending' "
                "AND j.job_state='queued' ORDER BY a.created_at,a.attempt_id LIMIT ?",
                (self.batch_limit,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def _release_inactive(self) -> tuple[str, ...]:
        released: list[str] = []
        for started in self.registry.tracked():
            binding = started.binding
            with self.repository.read_connection() as connection:
                row = connection.execute(
                    "SELECT a.state,s.active_attempt_id,j.job_state "
                    "FROM execution_attempt a JOIN execution_step s "
                    "ON s.job_id=a.job_id AND s.step_id=a.step_id "
                    "JOIN execution_job j ON j.job_id=a.job_id "
                    "WHERE a.attempt_id=?",
                    (binding.attempt_id,),
                ).fetchone()
            active = row is not None and (
                row["state"] in {"running", "cancelling"}
                and row["active_attempt_id"] == binding.attempt_id
                and row["job_state"] in {"running", "cancelling"}
            )
            if active:
                continue
            detached = self.registry.detach(binding.attempt_id)
            if detached is None:
                continue
            if detached.fence is not None:
                try:
                    self.supervisor.unbind_attempt(
                        detached.ticket, detached.fence
                    )
                except ContractError:
                    # A terminal durable transition can invalidate the Attempt
                    # before the terminal ACK is observed.  P2 tick owns that
                    # bounded fencing path; releasing this retain is still exact.
                    pass
            self.supervisor.release(detached.ticket)
            released.append(binding.attempt_id)
        return tuple(released)

    def _cancel_operation_key(self, attempt_id: str) -> str | None:
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT operation_key FROM execution_control_operation "
                "WHERE attempt_id=? AND operation='cancel' "
                "ORDER BY created_at,operation_key LIMIT 1",
                (attempt_id,),
            ).fetchone()
        return None if row is None else str(row["operation_key"])

    def _settle_cancel_response(self, started: StartedChapterAttempt) -> None:
        binding = started.binding
        operation_key = self._cancel_operation_key(binding.attempt_id)
        if operation_key is None:
            return
        try:
            result = self.launcher.take_cancel_response(
                started, operation_key=operation_key
            )
            if result is None:
                return
            if not result["accepted"]:
                self.launcher.compensate(
                    ticket=started.ticket,
                    attempt_id=binding.attempt_id,
                    reason="Worker rejected job.cancel",
                )
                return
            if not result["terminal_known"]:
                return
            outcome = str(result["attempt_state"])
            if outcome not in {"failed", "cancelled"}:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "terminal job.cancel response cannot publish a result",
                )
            with self.repository.read_connection() as connection:
                local_seq = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(local_seq),0)+1 "
                        "FROM execution_job_event WHERE attempt_id=?",
                        (binding.attempt_id,),
                    ).fetchone()[0]
                )
            terminal_key = _stable_id(
                "cancel-terminal", binding.attempt_id, operation_key
            )
            rpc_id = _stable_id("cancel-rpc", binding.attempt_id, operation_key)
            meta = {
                "protocol_version": "1",
                "generation_id": binding.generation_id,
                "plugin_release_id": binding.release_id,
                "deadline_at": _deadline(),
                "context": "attempt",
                "operation_id": terminal_key,
                "job_id": binding.job_id,
                "step_id": binding.step_id,
                "attempt_id": binding.attempt_id,
                "lease_epoch": binding.lease_epoch,
            }
            params = {
                "operation_key": terminal_key,
                "worker_run_id": binding.worker_run_id,
                "outcome": outcome,
                "result_bundle_asset_id": None,
                "candidate_stage_operation_key": None,
                "terminal_detail_asset_id": None,
                "local_seq": local_seq,
            }
            receipt = self.provenance.resolve_provenance_receipt(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": "host.job.complete/v1",
                    "meta": meta,
                    "params": params,
                }
            )
            self.authority.complete_attempt(
                job_id=binding.job_id,
                step_id=binding.step_id,
                attempt_id=binding.attempt_id,
                lease_epoch=binding.lease_epoch,
                operation_key=terminal_key,
                worker_run_id=binding.worker_run_id,
                outcome=outcome,
                result_bundle_asset_id=None,
                candidate_stage_operation_key=None,
                terminal_detail_asset_id=None,
                local_seq=local_seq,
                provenance_receipt=receipt,
                operation_meta=meta,
                rpc_id=rpc_id,
            )
        except BaseException:
            self.launcher.compensate(
                ticket=started.ticket,
                attempt_id=binding.attempt_id,
                reason="job.cancel terminal closure failed",
            )
            raise

    def run_once(self) -> ProductionPumpResult:
        """Tick first, then dispatch each tracked durable Attempt exactly once."""

        self._require_open()
        if not self._run_lock.acquire(blocking=False):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "production Job pump already has an owner",
            )
        try:
            self._require_open()
            self.supervisor.tick()
            handled: list[str] = []
            passthrough = 0
            for started in self.registry.durable()[: self.batch_limit]:
                batch = self.dispatcher.dispatch_pending(started.ticket)
                handled.extend(batch.handled_request_ids)
                passthrough += len(batch.passthrough)
                if started.binding.attempt_id in {
                    item.binding.attempt_id for item in self.registry.durable()
                }:
                    self._settle_cancel_response(started)
            released = self._release_inactive()
            launched: list[str] = []
            for row in self._created_attempts():
                self.launcher.launch_created_attempt(row)
                launched.append(str(row["attempt_id"]))
            return ProductionPumpResult(
                tuple(handled), passthrough, tuple(launched), released
            )
        finally:
            self._run_lock.release()

    tick = run_once

    def close(self) -> None:
        with self._state_lock:
            self._open = False
        # Wait for an already admitted cycle; do not race supervisor shutdown.
        with self._run_lock:
            return


class ProductionJobRuntime:
    """One production Jobs v2 graph over the exact WU-2A/Core authorities."""

    def __init__(self, plugin_runtime: ProductionPluginRuntime) -> None:
        if not isinstance(plugin_runtime, ProductionPluginRuntime):
            raise TypeError("plugin_runtime must be ProductionPluginRuntime")
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        _claim_local_runtime(plugin_runtime, self)
        try:
            self._initialize(plugin_runtime)
        except BaseException:
            _release_local_runtime(plugin_runtime, self)
            raise

    def _initialize(self, plugin_runtime: ProductionPluginRuntime) -> None:
        self.plugin_runtime = plugin_runtime
        self.core_runtime = plugin_runtime.core_runtime
        self.repository = plugin_runtime.repository
        self.assets = plugin_runtime.assets
        self.execution_authority = plugin_runtime.execution_authority
        self.execution = self.execution_authority
        self.supervisor = plugin_runtime.supervisor

        self.restart_reconciliation = dict(
            self.execution_authority.reconcile_production_restart()
        )
        self.restart_reconciliation["claims"] = _release_stale_claims(
            plugin_runtime
        )

        self.ingress = ProductionIngressGate()
        self.capability_runtime = ProductionCapabilityRuntime(plugin_runtime)
        self.capability_broker = build_production_capability_broker(
            self.execution_authority, self.capability_runtime
        )
        self.attempt_registry = ProductionAttemptRegistry(
            self.execution_authority
        )
        self.worker_launcher = ProductionWorkerLauncher(
            self.execution_authority,
            plugin_runtime,
            self.capability_runtime,
            self.attempt_registry,
        )
        self.start_resolver = ProductionJobStartResolver(
            self.execution_authority,
            self.capability_runtime,
            self.worker_launcher,
            self.ingress,
        )
        self.control_resolver = ProductionJobControlResolver(
            self.execution_authority,
            self.worker_launcher,
            self.attempt_registry,
            self.capability_runtime,
            self.ingress,
        )
        self.provenance_receipt_resolver = ProductionProvenanceReceiptResolver(
            self.execution_authority
        )
        self.stream_commit_policy_resolver = ProductionStreamCommitPolicyResolver(
            self.execution_authority
        )
        self.composition = compose_job_runtime(
            self.execution_authority,
            self.supervisor,
            capability_broker=self.capability_broker,
            start_resolver=self.start_resolver,
            control_resolver=self.control_resolver,
            provenance_receipt_resolver=self.provenance_receipt_resolver,
            stream_commit_policy_resolver=self.stream_commit_policy_resolver,
        )
        self.job_runtime = self.composition
        self.command_query = self.composition.command_query
        self.http = self.command_query
        self.router = self.composition.router()
        self.jobs_router = self.router
        self.pump = ProductionJobPump(
            self.composition,
            self.worker_launcher,
            self.attempt_registry,
            self.ingress,
            self.provenance_receipt_resolver,
        )

    def shutdown(self) -> None:
        """Stop only WU-2B/P2 ownership; shared Core remains caller-owned."""

        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self.ingress.close_and_drain()
            self.pump.close()
            self.supervisor.shutdown()
            self.shutdown_reconciliation = dict(
                self.execution_authority.reconcile_production_restart()
            )
            self.attempt_registry.clear()
            self._shutdown_complete = True
            _release_local_runtime(self.plugin_runtime, self)

    close = shutdown


def build_production_job_runtime(
    plugin_runtime: ProductionPluginRuntime,
) -> ProductionJobRuntime:
    """Compose WU-2B without mounting routes or starting a background service."""

    return ProductionJobRuntime(plugin_runtime)


__all__ = [
    "ProductionJobPump",
    "ProductionJobRuntime",
    "ProductionPumpResult",
    "build_production_job_runtime",
]
