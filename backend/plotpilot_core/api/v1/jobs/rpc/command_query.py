"""Thin Job command/query application adapter over the accepted authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Any, Protocol

from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import ContractError, ContractValidationError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import (
    assert_valid,
    hash_without_field,
    verify_checkpoint,
    verify_snapshot,
    verify_job_snapshot,
)


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\."
    r"(?!000)[0-9]{3}Z)$"
)


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{label} is not a v1 ID")
    return value


def _require_optional_id(value: Any, label: str) -> str | None:
    return None if value is None else _require_id(value, label)


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class JobCommandResult:
    """Job creation result without claiming a non-atomic replay decision."""

    job_id: str
    workspace_id: str
    job_state: str
    job_revision: int
    replayed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "job_state": self.job_state,
            "job_revision": self.job_revision,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class JobSnapshotExtensions:
    """Projection data owned by later checkpoint/stream source nodes.

    The extension reader is invoked while the accepted repository read gate is
    held, so the base Job rows and extension high-waters share one authority
    view.  No fallback table or second store is created here.
    """

    checkpoint: JobCheckpointBinding | None
    stream_high_waters: tuple[JobStreamHighWaterBinding, ...]


@dataclass(frozen=True, slots=True)
class JobCheckpointBinding:
    workspace_id: str
    job_id: str
    step_id: str
    checkpoint_id: str
    checkpoint: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class JobStreamHighWaterBinding:
    workspace_id: str
    job_id: str
    step_id: str
    high_water: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AttemptStartBinding:
    """Immutable identity allocated by the future P1 start transaction seam."""

    job_id: str
    step_id: str
    attempt_id: str
    worker_run_id: str
    plugin_id: str
    release_id: str
    package_hash: str
    capability_id: str
    generation_id: str
    lease_epoch: int
    preallocated_receipt_id: str
    expected_result_contract: str


class AttemptStartPort(Protocol):
    def start_attempt(self, **command: Any) -> AttemptStartBinding: ...


class JobSnapshotExtensionReader(Protocol):
    def read(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        workspace_id: str,
    ) -> JobSnapshotExtensions: ...


class JobCommandQueryAdapter:
    """Application boundary that never owns a database or Job state machine."""

    def __init__(
        self,
        authority: ExecutionAuthority,
        *,
        snapshot_extensions: JobSnapshotExtensionReader,
        attempt_starter: AttemptStartPort | None = None,
    ) -> None:
        repository = getattr(authority, "repository", None)
        if repository is None or not callable(getattr(repository, "read_connection", None)):
            raise TypeError("Job adapter requires the accepted ExecutionAuthority repository")
        self._authority = authority
        self._repository = repository
        if not callable(getattr(snapshot_extensions, "read", None)):
            raise TypeError("Job snapshot extension authority is required")
        self._snapshot_extensions = snapshot_extensions
        if attempt_starter is not None and not callable(
            getattr(attempt_starter, "start_attempt", None)
        ):
            raise TypeError("Attempt start port must expose start_attempt")
        self._attempt_starter = attempt_starter

    @property
    def authority(self) -> ExecutionAuthority:
        return self._authority

    def create_or_get(
        self,
        *,
        job_id: str,
        run_snapshot: Mapping[str, Any],
    ) -> JobCommandResult:
        """Create one Job or return the request-key-bound existing Job."""

        _require_id(job_id, "job_id")
        snapshot = dict(run_snapshot)
        verify_snapshot(snapshot)
        existing = self._authority.find_by_request_key(
            snapshot["workspace_id"],
            snapshot["request_key"],
        )
        row = self._authority.create_from_verified_snapshot(job_id, snapshot)
        # P1 does not yet return an atomic created/replayed outcome.  A known
        # prior row or a different winning job_id proves replay; otherwise the
        # answer is deliberately indeterminate rather than a false claim.
        replayed = True if existing is not None or row["job_id"] != job_id else None
        return JobCommandResult(
            job_id=str(row["job_id"]),
            workspace_id=str(row["workspace_id"]),
            job_state=str(row["job_state"]),
            job_revision=int(row["job_revision"]),
            replayed=replayed,
        )

    def freeze_plan(
        self,
        *,
        job_id: str,
        steps: list[Mapping[str, Any]],
        output_step_id: str,
    ) -> None:
        """Delegate plan freezing; the adapter performs no state mutation."""

        _require_id(job_id, "job_id")
        _require_id(output_step_id, "output_step_id")
        if not isinstance(steps, list) or not steps:
            raise ContractValidationError("execution plan requires Steps")
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                raise ContractValidationError(f"steps[{index}] must be an object")
            if set(step) != {"step_id", "depends_on", "result_contract"}:
                raise ContractValidationError(
                    f"steps[{index}] fields are not closed"
                )
            _require_id(step.get("step_id"), f"steps[{index}].step_id")
            _require_id(step.get("result_contract"), f"steps[{index}].result_contract")
            dependencies = step.get("depends_on", ())
            if isinstance(dependencies, (str, bytes)) or not isinstance(
                dependencies, (list, tuple)
            ):
                raise ContractValidationError(f"steps[{index}].depends_on must be an array")
            for dep_index, dependency in enumerate(dependencies):
                _require_id(
                    dependency,
                    f"steps[{index}].depends_on[{dep_index}]",
                )
        self._authority.freeze_plan(job_id, steps, output_step_id=output_step_id)

    def start_attempt(self, **command: Any) -> AttemptStartBinding:
        """Start only through a P1 port returning its atomic allocated binding."""

        required = {
            "job_id",
            "step_id",
            "attempt_id",
            "worker_run_id",
            "plugin_id",
            "release_id",
            "package_hash",
            "capability_id",
            "generation_id",
        }
        optional = {
            "lease_epoch",
            "preallocated_receipt_id",
            "expected_result_contract",
            "lease_expires_at",
        }
        if "secrets" in command:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "one-shot secrets require the unpublished P1/P2 scrub seam",
            )
        missing = sorted(required - set(command))
        extra = sorted(set(command) - required - optional)
        if missing or extra:
            raise ContractValidationError(
                f"Attempt start command is not closed: missing={missing}, extra={extra}"
            )
        for name in (
            "job_id",
            "step_id",
            "attempt_id",
            "worker_run_id",
            "plugin_id",
            "release_id",
            "capability_id",
            "generation_id",
        ):
            _require_id(command[name], name)
        if not isinstance(command["package_hash"], str) or _HASH.fullmatch(
            command["package_hash"]
        ) is None:
            raise ContractValidationError("package_hash is not a SHA-256 digest")
        requested_epoch = _require_positive_int(command.get("lease_epoch", 1), "lease_epoch")
        requested_receipt = _require_optional_id(
            command.get("preallocated_receipt_id"), "preallocated_receipt_id"
        )
        _require_optional_id(
            command.get("expected_result_contract"), "expected_result_contract"
        )
        expires_at = command.get("lease_expires_at")
        if expires_at is not None and (
            not isinstance(expires_at, str) or _TIMESTAMP.fullmatch(expires_at) is None
        ):
            raise ContractValidationError("lease_expires_at is not a v1 timestamp")
        if self._attempt_starter is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "P1 AttemptStartBinding port is not composed",
            )
        binding = self._attempt_starter.start_attempt(**command)
        if not isinstance(binding, AttemptStartBinding):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Attempt start port did not return AttemptStartBinding",
            )
        for name in (
            "job_id",
            "step_id",
            "attempt_id",
            "worker_run_id",
            "plugin_id",
            "release_id",
            "package_hash",
            "capability_id",
            "generation_id",
        ):
            if getattr(binding, name) != command[name]:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    f"AttemptStartBinding {name} drifted from the command",
                )
        _require_positive_int(binding.lease_epoch, "binding.lease_epoch")
        if requested_epoch != 1 and binding.lease_epoch != requested_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "allocated Attempt epoch drifted")
        _require_id(binding.preallocated_receipt_id, "binding.preallocated_receipt_id")
        if requested_receipt is not None and binding.preallocated_receipt_id != requested_receipt:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "AttemptStartBinding receipt drifted from the command",
            )
        _require_id(binding.expected_result_contract, "binding.expected_result_contract")
        expected_contract = command.get("expected_result_contract")
        if expected_contract is not None and binding.expected_result_contract != expected_contract:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "AttemptStartBinding result contract drifted from the command",
            )
        return binding

    def get_snapshot(self, *, workspace_id: str, job_id: str) -> dict[str, Any]:
        """Project one authoritative ``job-snapshot/v1`` under one read gate."""

        _require_id(workspace_id, "workspace_id")
        _require_id(job_id, "job_id")
        with self._repository.read_connection() as connection:
            job = connection.execute(
                "SELECT * FROM execution_job WHERE workspace_id=? AND job_id=?",
                (workspace_id, job_id),
            ).fetchone()
            if job is None:
                # Cross-Workspace and unknown Job deliberately share one error.
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            steps = connection.execute(
                "SELECT step_id,state,revision FROM execution_step WHERE job_id=? "
                "ORDER BY step_ordinal,step_id",
                (job_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT a.attempt_id,a.step_id,a.state,a.lease_epoch FROM execution_attempt a "
                "JOIN execution_step s ON s.step_id=a.step_id "
                "WHERE a.job_id=? ORDER BY s.step_ordinal,a.ordinal,a.attempt_id",
                (job_id,),
            ).fetchall()
            candidate_rows = connection.execute(
                "SELECT candidate_id FROM execution_candidate_binding WHERE job_id=? "
                "ORDER BY candidate_id",
                (job_id,),
            ).fetchall()
            extensions = self._snapshot_extensions.read(
                connection,
                job_id=job_id,
                workspace_id=workspace_id,
            )
            if not isinstance(extensions, JobSnapshotExtensions):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "snapshot extension reader returned an untyped projection",
                )
            step_ids = [str(row["step_id"]) for row in steps]
            step_order = {step_id: index for index, step_id in enumerate(step_ids)}
            attempt_scope = {
                str(row["attempt_id"]): (str(row["step_id"]), int(row["lease_epoch"]))
                for row in attempts
            }
            checkpoint_id: str | None = None
            if extensions.checkpoint is not None:
                checkpoint = extensions.checkpoint
                if not isinstance(checkpoint, JobCheckpointBinding):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "checkpoint extension is not a JobCheckpointBinding",
                    )
                for value, label in (
                    (checkpoint.workspace_id, "checkpoint.workspace_id"),
                    (checkpoint.job_id, "checkpoint.job_id"),
                    (checkpoint.step_id, "checkpoint.step_id"),
                    (checkpoint.checkpoint_id, "checkpoint.checkpoint_id"),
                ):
                    _require_id(value, label)
                if (
                    checkpoint.workspace_id != workspace_id
                    or checkpoint.job_id != job_id
                    or checkpoint.step_id not in step_order
                ):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "checkpoint extension is outside the Job scope",
                    )
                checkpoint_value = dict(checkpoint.checkpoint)
                verify_checkpoint(
                    checkpoint_value,
                    expected_snapshot_hash=str(job["run_snapshot_hash"]),
                )
                source_attempt = attempt_scope.get(
                    str(checkpoint_value.get("source_attempt_id", ""))
                )
                if (
                    checkpoint_value.get("checkpoint_id") != checkpoint.checkpoint_id
                    or checkpoint_value.get("job_id") != job_id
                    or checkpoint_value.get("step_id") != checkpoint.step_id
                    or source_attempt is None
                    or source_attempt[0] != checkpoint.step_id
                    or checkpoint_value.get("lease_epoch") != source_attempt[1]
                ):
                    raise ContractError(
                        ErrorCode.CHECKPOINT_INVALID,
                        "checkpoint contract is not owned by the bound Job Attempt",
                    )
                checkpoint_id = checkpoint.checkpoint_id
            stream_values: list[dict[str, Any]] = []
            stream_keys: list[tuple[int, str, str]] = []
            seen_stream_ids: set[str] = set()
            for binding in extensions.stream_high_waters:
                if not isinstance(binding, JobStreamHighWaterBinding):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream extension is not a JobStreamHighWaterBinding",
                    )
                for value, label in (
                    (binding.workspace_id, "stream.workspace_id"),
                    (binding.job_id, "stream.job_id"),
                    (binding.step_id, "stream.step_id"),
                ):
                    _require_id(value, label)
                value = dict(binding.high_water)
                stream_id = _require_id(value.get("stream_id"), "stream.stream_id")
                output_role = _require_id(value.get("output_role"), "stream.output_role")
                target = value.get("target")
                if (
                    binding.workspace_id != workspace_id
                    or binding.job_id != job_id
                    or binding.step_id not in step_order
                    or value.get("step_id") != binding.step_id
                    or not isinstance(target, Mapping)
                    or target.get("workspace_id") != workspace_id
                ):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream extension is outside the Job scope",
                    )
                if stream_id in seen_stream_ids:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream extension contains duplicate stream_id",
                    )
                seen_stream_ids.add(stream_id)
                stream_keys.append((step_order[binding.step_id], output_role, stream_id))
                stream_values.append(value)
            if stream_keys != sorted(stream_keys):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "stream extensions are not in canonical Job/role/stream order",
                )

            snapshot: dict[str, Any] = {
                "schema": "job-snapshot/v1",
                "job_id": str(job["job_id"]),
                "workspace_id": str(job["workspace_id"]),
                "job_state": str(job["job_state"]),
                "job_revision": int(job["job_revision"]),
                "steps": [
                    {
                        "step_id": str(row["step_id"]),
                        "state": str(row["state"]),
                        "revision": int(row["revision"]),
                    }
                    for row in steps
                ],
                "attempts": [
                    {
                        "attempt_id": str(row["attempt_id"]),
                        "state": str(row["state"]),
                        "lease_epoch": int(row["lease_epoch"]),
                    }
                    for row in attempts
                ],
                "candidate_ids": [str(row["candidate_id"]) for row in candidate_rows],
                "current_checkpoint_id": checkpoint_id,
                "stream_high_waters": stream_values,
                "core_event_high_water": int(job["core_event_high_water"]),
                "job_event_high_water": int(job["job_event_high_water"]),
                "created_at": str(job["created_at"]),
                "snapshot_hash": "",
            }
            snapshot["snapshot_hash"] = hash_without_field(
                snapshot,
                "snapshot_hash",
                "job-snapshot/v1",
            )
            verify_job_snapshot(snapshot)
            return snapshot

    def get_event_page(
        self,
        *,
        workspace_id: str,
        job_id: str,
        after_job_event_seq: int = 0,
    ) -> dict[str, Any]:
        """Project committed Job events without inventing a second cursor store."""

        _require_id(workspace_id, "workspace_id")
        _require_id(job_id, "job_id")
        if (
            isinstance(after_job_event_seq, bool)
            or not isinstance(after_job_event_seq, int)
            or after_job_event_seq < 0
        ):
            raise ContractValidationError(
                "after_job_event_seq must be a non-negative integer"
            )
        with self._repository.read_connection() as connection:
            job = connection.execute(
                "SELECT job_event_high_water FROM execution_job "
                "WHERE workspace_id=? AND job_id=?",
                (workspace_id, job_id),
            ).fetchone()
            if job is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            high_water = int(job["job_event_high_water"])
            rows = connection.execute(
                "SELECT job_event_seq,event_json FROM execution_job_event "
                "WHERE job_id=? AND job_event_seq>? AND job_event_seq<=? "
                "ORDER BY job_event_seq",
                (job_id, after_job_event_seq, high_water),
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in rows:
                event = json.loads(str(row["event_json"]))
                assert_valid("plugin-job-event/v1", event)
                if event["job_id"] != job_id or event["job_event_seq"] != row["job_event_seq"]:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stored Job event identity is inconsistent",
                    )
                events.append(event)
            next_seq = (
                int(rows[-1]["job_event_seq"]) + 1
                if rows
                else after_job_event_seq + 1
            )
            page = {
                "schema": "job-event-page/v1",
                "job_id": job_id,
                "after_job_event_seq": after_job_event_seq,
                "events": events,
                "next_job_event_seq": next_seq,
                "high_water_seq": high_water,
            }
            assert_valid("job-event-page/v1", page)
            return page
