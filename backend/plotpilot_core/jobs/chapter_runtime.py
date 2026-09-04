"""Thin chapter execution facade over durable Core/P2 authorities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from backend.plotpilot_core.api.v1.jobs.rpc import AttemptStartBinding
from backend.plotpilot_core.jobs.checkpoint_adapter import (
    AuthorityAttemptStartAdapter,
    DurableCheckpointAdapter,
    StreamCheckpointRecovery,
)
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.job_control import JobControl, RunSnapshotWorker
from backend.plotpilot_core.supervisor.models import AttemptFence, WorkerTicket
from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    parse_json_bytes,
    verify_snapshot,
)


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


@dataclass(frozen=True, slots=True)
class StartedChapterAttempt:
    """The P2 process retain and the exact P1 Attempt binding it owns."""

    ticket: WorkerTicket
    binding: AttemptStartBinding
    fence: AttemptFence | None = None


class AttemptLifecyclePort(Protocol):
    """Public P2 ports required after Core allocates an Attempt fence."""

    def bind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None: ...

    def interrupt_attempt(
        self, ticket: WorkerTicket, fence: AttemptFence, reason: str
    ) -> None: ...

    def unbind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None: ...

    def send_worker_request(
        self,
        ticket: WorkerTicket,
        method: str,
        params: Mapping[str, object],
        meta: Mapping[str, object],
        *,
        request_id: str,
    ) -> str: ...

    def take_worker_response(
        self, ticket: WorkerTicket, request_id: str
    ) -> RpcEvent | None: ...


class ChapterJobRuntime:
    """Coordinate existing authorities without retaining execution state."""

    def __init__(
        self,
        authority: ExecutionAuthority,
        *,
        job_control: JobControl | None = None,
        checkpoints: DurableCheckpointAdapter | None = None,
        attempt_lifecycle: AttemptLifecyclePort | None = None,
    ) -> None:
        repository = getattr(authority, "repository", None)
        if repository is None or not callable(
            getattr(repository, "read_connection", None)
        ):
            raise TypeError("chapter runtime requires the accepted ExecutionAuthority")
        self.authority = authority
        self.repository = repository
        self.assets = authority.assets
        self.job_control = job_control
        self.attempt_lifecycle = attempt_lifecycle
        if attempt_lifecycle is not None:
            required = (
                "bind_attempt",
                "interrupt_attempt",
                "unbind_attempt",
                "send_worker_request",
                "take_worker_response",
            )
            if any(
                not callable(getattr(attempt_lifecycle, name, None))
                for name in required
            ):
                raise TypeError(
                    "chapter runtime requires the public P2 Attempt lifecycle ports"
                )
        self.checkpoints = checkpoints or DurableCheckpointAdapter(authority)
        if self.checkpoints.authority is not authority:
            raise TypeError(
                "chapter runtime checkpoint adapter belongs to another authority"
            )
        self.attempts = AuthorityAttemptStartAdapter(authority)
        self.control_port = authority.control_port

    @staticmethod
    def _require_authoritative_owner(started: StartedChapterAttempt) -> None:
        if (
            not isinstance(started, StartedChapterAttempt)
            or started.binding.worker_run_id != started.ticket.lifecycle_id
            or (
                started.fence is not None
                and started.fence.owner_id != started.ticket.lifecycle_id
            )
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "chapter Attempt is not owned by its P2 lifecycle",
            )

    def _verify_run_snapshot_worker(
        self,
        *,
        job_id: str,
        run_snapshot_worker: RunSnapshotWorker,
        package_hash: str,
    ) -> None:
        if not isinstance(run_snapshot_worker, RunSnapshotWorker):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "start_attempt requires a RunSnapshotWorker",
            )
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT run_snapshot_json,run_snapshot_hash FROM execution_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
        try:
            snapshot = parse_json_bytes(str(row["run_snapshot_json"]).encode("utf-8"))
            if not isinstance(snapshot, Mapping):
                raise TypeError("RunSnapshot is not an object")
            verify_snapshot(snapshot)
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "Job RunSnapshot authority is invalid"
            ) from exc
        if snapshot.get("snapshot_hash") != row["run_snapshot_hash"]:
            raise ContractError(ErrorCode.ASSET_ERROR, "Job RunSnapshot hash drifted")
        if snapshot.get("snapshot_id") != run_snapshot_worker.run_snapshot_id:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "worker belongs to another RunSnapshot",
            )
        release = next(
            (
                value
                for value in snapshot.get("plugin_releases", ())
                if isinstance(value, Mapping)
                and value.get("plugin_id") == run_snapshot_worker.plugin_id
            ),
            None,
        )
        if (
            release is None
            or release.get("release_id") != run_snapshot_worker.release_id
            or release.get("package_hash") != package_hash
            or release.get("data_generation_id") != run_snapshot_worker.generation_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "worker release/generation is outside the RunSnapshot",
            )

    def start_attempt(
        self,
        *,
        run_snapshot_worker: RunSnapshotWorker,
        job_id: str,
        step_id: str,
        attempt_id: str,
        package_hash: str,
        capability_id: str,
        preallocated_receipt_id: str,
        expected_result_contract: str,
        lease_epoch: int = 1,
        lease_expires_at: str | None = None,
    ) -> StartedChapterAttempt:
        """Acquire the frozen P2 release, then bind its lifecycle to P1."""

        if self.job_control is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "chapter runtime has no composed JobControl",
            )
        self._verify_run_snapshot_worker(
            job_id=job_id,
            run_snapshot_worker=run_snapshot_worker,
            package_hash=package_hash,
        )
        ticket = self.job_control.request_worker(run_snapshot_worker)
        try:
            command: dict[str, Any] = {
                "job_id": job_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "worker_run_id": ticket.lifecycle_id,
                "plugin_id": run_snapshot_worker.plugin_id,
                "release_id": run_snapshot_worker.release_id,
                "package_hash": package_hash,
                "capability_id": capability_id,
                "generation_id": run_snapshot_worker.generation_id,
                "lease_epoch": lease_epoch,
                "preallocated_receipt_id": preallocated_receipt_id,
                "expected_result_contract": expected_result_contract,
            }
            if lease_expires_at is not None:
                command["lease_expires_at"] = lease_expires_at
            binding = self.attempts.start_attempt(**command)
            if (
                binding.worker_run_id != ticket.lifecycle_id
                or binding.release_id != ticket.release_id
                or binding.plugin_id != run_snapshot_worker.plugin_id
                or binding.generation_id != run_snapshot_worker.generation_id
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "allocated Attempt is not owned by the acquired worker",
                )
            fence = AttemptFence(
                binding.job_id,
                binding.step_id,
                binding.attempt_id,
                binding.lease_epoch,
                ticket.lifecycle_id,
            )
        except BaseException:
            self.job_control.release_worker(ticket)
            raise
        if self.attempt_lifecycle is not None:
            try:
                self.attempt_lifecycle.bind_attempt(ticket, fence)
            except BaseException as bind_error:
                try:
                    self.attempt_lifecycle.interrupt_attempt(
                        ticket,
                        fence,
                        "P2 bind_attempt failed after P1 start_attempt committed",
                    )
                except BaseException as compensation_error:
                    raise compensation_error from bind_error
                self.job_control.release_worker(ticket)
                raise
        return StartedChapterAttempt(ticket=ticket, binding=binding, fence=fence)

    def release_worker(self, started: StartedChapterAttempt) -> None:
        if self.job_control is None or not isinstance(started, StartedChapterAttempt):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "release_worker requires a started chapter Attempt",
            )
        try:
            if self.attempt_lifecycle is not None and started.fence is not None:
                self.attempt_lifecycle.unbind_attempt(started.ticket, started.fence)
        finally:
            self.job_control.release_worker(started.ticket)

    def send_worker_request(
        self,
        started: StartedChapterAttempt,
        method: str,
        params: Mapping[str, object],
        meta: Mapping[str, object],
        *,
        request_id: str,
    ) -> str:
        """Send through the sole lifecycle-fenced P2 session."""

        if (
            self.attempt_lifecycle is None
            or not isinstance(started, StartedChapterAttempt)
            or started.fence is None
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "worker request requires a composed and bound Attempt lifecycle",
            )
        self._require_authoritative_owner(started)
        return self.attempt_lifecycle.send_worker_request(
            started.ticket,
            method,
            params,
            meta,
            request_id=request_id,
        )

    def take_worker_response(
        self, started: StartedChapterAttempt, request_id: str
    ) -> RpcEvent | None:
        if (
            self.attempt_lifecycle is None
            or not isinstance(started, StartedChapterAttempt)
            or started.fence is None
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "worker response requires a composed and bound Attempt lifecycle",
            )
        self._require_authoritative_owner(started)
        event = self.attempt_lifecycle.take_worker_response(started.ticket, request_id)
        if event is None:
            return None
        result = event.message.get("result")
        if isinstance(result, Mapping) and {
            "accepted",
            "provenance_receipt_id",
            "output_streams",
        }.issubset(result):
            worker_run_id = result.get("worker_run_id")
            if worker_run_id != started.ticket.lifecycle_id:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Worker start/resume response did not echo the P2 lifecycle identity",
                )
        return event

    def recover_stream(self, **identity: Any) -> StreamCheckpointRecovery | None:
        return self.checkpoints.recover_stream(**identity)

    @staticmethod
    def require_automatic_replay(
        recovered: StreamCheckpointRecovery,
    ) -> StreamCheckpointRecovery:
        if not isinstance(recovered, StreamCheckpointRecovery):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "automatic replay requires a verified stream recovery",
            )
        if not recovered.automatic_replay_allowed:
            raise ContractError(
                ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                f"stream recovery requires {recovered.recovery_action}",
            )
        return recovered

    def materialize_incomplete_candidate(
        self,
        recovered: StreamCheckpointRecovery,
        *,
        base_revision_id: str,
        base_content_hash: str,
    ):
        """Stage one idempotent partial Candidate; never invoke Publication."""

        if not isinstance(recovered, StreamCheckpointRecovery):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "incomplete Candidate requires a verified stream recovery",
            )
        verified = self.checkpoints.recover_stream(
            workspace_id=recovered.workspace_id,
            job_id=recovered.job_id,
            step_id=recovered.step_id,
            attempt_id=recovered.attempt_id,
            lease_epoch=recovered.lease_epoch,
            worker_run_id=recovered.worker_run_id,
            stream_id=str(recovered.stream_prefix["stream_id"]),
            run_snapshot_hash=recovered.run_snapshot_hash,
            expected_result_contract=recovered.expected_result_contract,
        )
        if (
            verified is None
            or verified.checkpoint_id != recovered.checkpoint_id
            or verified.prefix_asset_id != recovered.prefix_asset_id
            or verified.prefix_hash != recovered.prefix_hash
        ):
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "stream recovery is no longer the durable high-water",
            )
        target = dict(verified.stream_prefix["target"])
        operation_key = _stable_id(
            "chapter-incomplete",
            verified.job_id,
            verified.step_id,
            verified.attempt_id,
            verified.lease_epoch,
            verified.stream_prefix["stream_id"],
        )
        item_id = _stable_id(
            "stream-item",
            verified.job_id,
            verified.step_id,
            verified.stream_prefix["stream_id"],
        )
        item = {
            "schema": "candidate-item/v1",
            "item_id": item_id,
            "item_kind": "incomplete_stream",
            "target": target,
            "mutation": {
                "mode": "replace",
                "payload_schema": "core/document-text/v1",
                "payload_hash": verified.prefix_hash,
            },
            "payload_asset_id": verified.prefix_asset_id,
            "base": {
                "revision_id": base_revision_id,
                "content_hash": base_content_hash,
            },
            "write_set": [
                {
                    **target,
                    "revision_id": base_revision_id,
                    "content_hash": base_content_hash,
                }
            ],
            "parent_candidate_ids": [],
            "source_refs": [],
            "status": "partial",
        }
        return self.authority.stage_incomplete_stream(
            job_id=verified.job_id,
            step_id=verified.step_id,
            attempt_id=verified.attempt_id,
            lease_epoch=verified.lease_epoch,
            operation_key=operation_key,
            item=item,
            writer_epoch=verified.lease_epoch,
        )

    def pause(self, **command: Any):
        return self.control_port.pause(**command)

    def await_user(self, **command: Any):
        return self.control_port.await_user(**command)

    def cancel(self, **command: Any):
        return self.control_port.cancel(**command)

    def resume(self, **command: Any):
        return self.control_port.resume(**command)

    def apply_control(self, operation: str, **command: Any):
        return self.control_port.apply(operation, **command)


__all__ = ["AttemptLifecyclePort", "ChapterJobRuntime", "StartedChapterAttempt"]
