"""Frozen v2 Job command/query boundary over the sole P1 authority.

The adapter owns neither a Job ledger nor command routing policy.  Query
projections come from the accepted Job snapshot/event stores, while start and
control decisions are supplied by explicit, repository-bound resolvers.  This
keeps capability selection, operation-key replay, revision CAS, checkpoint
selection, and resume lineage inside their durable authority owners.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from backend.plotpilot_core.events import JobEventStore, JobSnapshotStore
from backend.plotpilot_core.events.store import pinned_read_transaction
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
)
from backend.plotpilot_plugin_sdk.m4_m5_http_v2 import (
    parse_http_request,
    parse_http_response,
    validate_http_exchange,
)

from ..sse.adapter import JobSSEAdapter

_ROUTES = frozenset(
    {
        "job.list",
        "job.get",
        "job.start",
        "job.pause",
        "job.resume",
        "job.cancel",
        "job.events",
        "job.sse-recovery",
    }
)
_CONTROL_ROUTES = frozenset({"job.pause", "job.resume", "job.cancel"})
_TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_CURSOR_ERRORS = frozenset(
    {"cursor_ahead", "cursor_domain_mismatch", "sse_recovery_required"}
)


class JobAuthorityQueryPort(Protocol):
    """P1-owned Job identity catalog used only to seed snapshot projection.

    ``execution_authority`` must be the accepted P1 ``ExecutionAuthority``.
    The catalog returns identities only; the v2 response is always rebuilt and
    validated from ``JobSnapshotStore``.  This explicit port avoids embedding
    SQL or a second list truth source in the HTTP adapter.
    """

    execution_authority: ExecutionAuthority
    repository: Any

    def list_job_ids(self, *, workspace_id: str) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class JobCommandResolution:
    """Durable command decision returned by a repository-bound resolver."""

    operation_key: str
    workspace_id: str
    job_id: str
    command: str
    accepted: bool
    idempotent: bool
    terminal_known: bool
    state: str
    job_revision: int
    snapshot_cursor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "job-command-result/v2",
            "operation_key": self.operation_key,
            "workspace_id": self.workspace_id,
            "job_id": self.job_id,
            "command": self.command,
            "accepted": self.accepted,
            "idempotent": self.idempotent,
            "terminal_known": self.terminal_known,
            "state": self.state,
            "job_revision": self.job_revision,
            "snapshot_cursor": self.snapshot_cursor,
        }


class JobStartResolver(Protocol):
    repository: Any

    def resolve_start(self, command: Mapping[str, Any]) -> JobCommandResolution: ...


class JobControlResolver(Protocol):
    repository: Any

    def resolve_control(self, command: Mapping[str, Any]) -> JobCommandResolution: ...


class JobHttpApplicationError(RuntimeError):
    """Typed resolver failure that is already expressed in frozen HTTP terms."""

    def __init__(
        self,
        status: int,
        error_code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.retryable = retryable


class JobCommandQueryAdapter:
    """Dispatch exactly the eight frozen v2 Job routes.

    The six constructor dependencies are deliberately mandatory.  Repository
    identity is compared by object identity, so an in-memory resolver, second
    SQLite authority, or separately composed Event/SSE stack fails at startup.
    """

    def __init__(
        self,
        authority: JobAuthorityQueryPort,
        *,
        snapshots: JobSnapshotStore,
        events: JobEventStore,
        sse: JobSSEAdapter,
        start_resolver: JobStartResolver,
        control_resolver: JobControlResolver,
    ) -> None:
        execution_authority = getattr(authority, "execution_authority", None)
        repository = getattr(authority, "repository", None)
        if (
            not isinstance(execution_authority, ExecutionAuthority)
            or repository is None
            or execution_authority.repository is not repository
            or not callable(getattr(authority, "list_job_ids", None))
        ):
            raise TypeError(
                "authority must expose the accepted ExecutionAuthority, its "
                "repository, and list_job_ids"
            )
        if not isinstance(snapshots, JobSnapshotStore):
            raise TypeError("snapshots must be JobSnapshotStore")
        if not isinstance(events, JobEventStore):
            raise TypeError("events must be JobEventStore")
        if not isinstance(sse, JobSSEAdapter):
            raise TypeError("sse must be JobSSEAdapter")
        if (
            snapshots.repository is not repository
            or events.repository is not repository
            or sse.repository is not repository
            or sse.snapshots is not snapshots
            or sse.events is not events
        ):
            raise TypeError("all Job query dependencies must share one P1 repository")
        if getattr(
            start_resolver, "repository", None
        ) is not repository or not callable(
            getattr(start_resolver, "resolve_start", None)
        ):
            raise TypeError("start_resolver must be bound to the P1 repository")
        if getattr(
            control_resolver, "repository", None
        ) is not repository or not callable(
            getattr(control_resolver, "resolve_control", None)
        ):
            raise TypeError("control_resolver must be bound to the P1 repository")

        self.authority = authority
        self.execution_authority = execution_authority
        self.repository = repository
        self.snapshots = snapshots
        self.events = events
        self.sse = sse
        self.start_resolver = start_resolver
        self.control_resolver = control_resolver

    def handle(
        self,
        route_id: str,
        request: Mapping[str, Any],
        *,
        path_identity: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if route_id not in _ROUTES:
            raise ContractValidationError(f"unsupported v2 Job route: {route_id}")
        try:
            parsed = parse_http_request(route_id, request)
            self._validate_path_identity(parsed, path_identity)
        except Exception as exc:  # noqa: BLE001
            return self._exception_error(route_id, exc, request, ingress=True)

        try:
            status, response = self._dispatch(route_id, parsed)
            _, validated = validate_http_exchange(route_id, parsed, status, response)
            return status, validated
        except Exception as exc:  # noqa: BLE001
            return self._exception_error(route_id, exc, parsed, ingress=False)

    dispatch = handle

    @staticmethod
    def _validate_path_identity(
        request: Mapping[str, Any],
        path_identity: Mapping[str, str] | None,
    ) -> None:
        if path_identity is None:
            return
        if not isinstance(path_identity, Mapping):
            raise ContractValidationError("path identity must be an object")
        for name, value in path_identity.items():
            if not isinstance(name, str) or request.get(name) != value:
                raise ContractValidationError(
                    f"path identity {name!s} does not match the request"
                )

    def _dispatch(
        self, route_id: str, request: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if route_id == "job.list":
            return 200, self._list(request)
        if route_id == "job.get":
            return 200, self._get(request)
        if route_id == "job.start":
            resolution = self.start_resolver.resolve_start(request)
            return 201, self._command_response(request, resolution)
        if route_id in _CONTROL_ROUTES:
            resolution = self.control_resolver.resolve_control(request)
            return 200, self._command_response(request, resolution)
        if route_id == "job.events":
            return 200, self._event_page(request)
        if route_id == "job.sse-recovery":
            return 200, self.sse.recover_query(request)
        raise ContractValidationError(f"unsupported v2 Job route: {route_id}")

    def _list(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workspace_id = str(request["workspace_id"])
        raw_ids = self.authority.list_job_ids(workspace_id=workspace_id)
        if isinstance(raw_ids, (str, bytes, bytearray)) or not isinstance(
            raw_ids, Sequence
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "P1 Job identity catalog did not return a sequence",
            )
        job_ids = tuple(raw_ids)
        if any(not isinstance(job_id, str) or not job_id for job_id in job_ids):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "P1 Job identity catalog returned an invalid Job ID",
            )
        if len(set(job_ids)) != len(job_ids):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "P1 Job identity catalog returned a duplicate Job ID",
            )

        projected = {
            job_id: dict(
                self.snapshots.capture_v2(
                    job_id,
                    workspace_id=workspace_id,
                ).value
            )
            for job_id in sorted(job_ids)
        }
        requested_state = request["state"]
        filtered_ids = [
            job_id
            for job_id in sorted(projected)
            if requested_state is None or projected[job_id]["state"] == requested_state
        ]
        total = len(filtered_ids)

        cursor = request["cursor"]
        if cursor is not None:
            cursor_job_id, cursor_seq = self._cursor_parts(str(cursor))
            cursor_snapshot = projected.get(cursor_job_id)
            if cursor_snapshot is None:
                raise JobHttpApplicationError(
                    400,
                    "malformed_request",
                    "Job list cursor is outside the requested Workspace",
                )
            if cursor_seq > int(cursor_snapshot["job_event_high_water"]):
                raise JobHttpApplicationError(
                    400,
                    "malformed_request",
                    "Job list cursor is ahead of its authoritative Job",
                )
            filtered_ids = [job_id for job_id in filtered_ids if job_id > cursor_job_id]

        visible_ids = filtered_ids[: int(request["limit"])]
        items = [projected[job_id] for job_id in visible_ids]
        next_cursor = None
        if items:
            last = items[-1]
            next_cursor = self._snapshot_cursor(last)
        return {
            "schema": "job-list-result/v2",
            "workspace_id": workspace_id,
            "items": items,
            "next_cursor": next_cursor,
            "cursor_domain": "job",
            "total": total,
        }

    def _get(self, request: Mapping[str, Any]) -> dict[str, Any]:
        captured = self.snapshots.capture_v2(
            str(request["job_id"]),
            workspace_id=str(request["workspace_id"]),
        )
        snapshot = dict(captured.value)
        return {
            "schema": "job-snapshot-result/v2",
            "workspace_id": request["workspace_id"],
            "job_id": request["job_id"],
            "snapshot": snapshot,
            "cursor": self._snapshot_cursor(snapshot),
        }

    def _command_response(
        self,
        request: Mapping[str, Any],
        resolution: JobCommandResolution,
    ) -> dict[str, Any]:
        if not isinstance(resolution, JobCommandResolution):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job resolver did not return JobCommandResolution",
            )
        expected_command = (
            "start"
            if request["schema"] == "job-start-command/v2"
            else request["command"]
        )
        expected_identity = (
            request["operation_key"],
            request["workspace_id"],
            request["job_id"],
            expected_command,
        )
        actual_identity = (
            resolution.operation_key,
            resolution.workspace_id,
            resolution.job_id,
            resolution.command,
        )
        if actual_identity != expected_identity:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job resolver result crosses request identity",
            )
        if not resolution.accepted:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "successful Job command result must be accepted",
            )
        terminal = resolution.state in _TERMINAL_STATES
        if resolution.terminal_known != terminal:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job resolver terminal marker disagrees with its state",
            )

        response = resolution.to_dict()
        parse_http_response(
            "job.start" if expected_command == "start" else f"job.{expected_command}",
            201 if expected_command == "start" else 200,
            response,
        )
        current = dict(
            self.snapshots.capture_v2(
                resolution.job_id,
                workspace_id=resolution.workspace_id,
            ).value
        )
        _, response_cursor_seq = self._cursor_parts(resolution.snapshot_cursor)
        current_revision = int(current["job_revision"])
        current_high_water = int(current["job_event_high_water"])
        if (
            resolution.job_revision > current_revision
            or response_cursor_seq > current_high_water
            or (
                resolution.job_revision == current_revision
                and resolution.state != current["state"]
            )
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job resolver result is not anchored in current P1 authority",
            )
        if request["schema"] == "job-start-command/v2":
            if (
                not resolution.idempotent
                and resolution.job_revision == current_revision
                and int(request["writer_epoch"]) != int(current["writer_epoch"])
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Job start writer epoch is not authoritative",
                )
        elif resolution.job_revision < int(request["expected_job_revision"]):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job control result predates its expected revision",
            )
        return response

    def _event_page(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workspace_id = str(request["workspace_id"])
        job_id = str(request["job_id"])
        after_seq = int(request["after_job_event_seq"])
        with pinned_read_transaction(self.repository) as connection:
            captured = self.snapshots.capture_v2(
                job_id,
                workspace_id=workspace_id,
                connection=connection,
            )
            window = self.events.window(
                job_id,
                after_seq,
                limit=int(request["limit"]),
                connection=connection,
            )
        if captured.high_water_seq != window.durable_high_water_seq:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job Event page and snapshot high-waters diverged",
            )
        if window.gap:
            raise JobHttpApplicationError(
                400,
                "malformed_request",
                "Job Event retention gap requires the SSE recovery route",
            )
        revision = int(captured.value["job_revision"])
        events = [self._project_event(event, revision) for event in window.events]
        return {
            "schema": "job-event-page-result/v2",
            "workspace_id": workspace_id,
            "job_id": job_id,
            "events": events,
            "next_cursor": f"job/{job_id}/{window.next_after_seq}",
            "high_water_seq": window.durable_high_water_seq,
            "cursor_domain": "job",
        }

    @staticmethod
    def _project_event(event: Mapping[str, Any], revision: int) -> dict[str, Any]:
        return {
            "event_id": event.get("event_id"),
            "job_id": event.get("job_id"),
            "job_event_seq": event.get("job_event_seq"),
            "event_type": event.get("event_type"),
            "aggregate_revision": max(1, revision),
            "payload_asset_id": event.get("payload_asset_id"),
            "payload_hash": event.get("payload_hash"),
            "occurred_at": event.get("occurred_at"),
        }

    @staticmethod
    def _cursor_parts(cursor: str) -> tuple[str, int]:
        parts = cursor.split("/")
        if len(parts) != 3 or parts[0] != "job" or not parts[2].isdigit():
            raise ContractValidationError("Job cursor is malformed")
        return parts[1], int(parts[2])

    @staticmethod
    def _snapshot_cursor(snapshot: Mapping[str, Any]) -> str:
        return f"job/{snapshot['job_id']}/{snapshot['job_event_high_water']}"

    def _exception_error(
        self,
        route_id: str,
        exc: Exception,
        request: Mapping[str, Any],
        *,
        ingress: bool,
    ) -> tuple[int, dict[str, Any]]:
        if isinstance(exc, JobHttpApplicationError):
            return self._error(
                route_id,
                exc.status,
                exc.error_code,
                str(exc),
                request,
                retryable=exc.retryable,
            )

        message = str(exc) or type(exc).__name__
        lowered = message.lower()
        if ingress or isinstance(exc, ContractValidationError):
            code = (
                "cursor_domain_mismatch"
                if "cursor domain mismatch" in lowered
                else "malformed_request"
            )
            return self._error(route_id, 400, code, message, request)
        if isinstance(exc, KeyError) or "unknown job" in lowered:
            return self._error(route_id, 404, "unknown_job", message, request)
        if isinstance(exc, ContractError):
            if exc.code == int(ErrorCode.DUPLICATE_REQUEST):
                return self._error(
                    route_id, 409, "duplicate_operation", message, request
                )
            if exc.code == int(ErrorCode.STALE_LEASE):
                return self._error(route_id, 409, "stale_revision", message, request)
            if exc.code == int(ErrorCode.CANCELLED):
                return self._error(route_id, 409, "terminal_job", message, request)
            if exc.code == int(ErrorCode.INVALID_TRANSITION):
                if "ahead" in lowered:
                    status = 409 if route_id == "job.sse-recovery" else 400
                    return self._error(
                        route_id, status, "cursor_ahead", message, request
                    )
                if "terminal" in lowered:
                    return self._error(route_id, 409, "terminal_job", message, request)
                if route_id in _CONTROL_ROUTES or route_id == "job.start":
                    return self._error(
                        route_id, 409, "invalid_transition", message, request
                    )
            if exc.code in {
                int(ErrorCode.CHECKPOINT_INVALID),
                int(ErrorCode.INCOMPATIBLE_GENERATION),
                int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT),
                int(ErrorCode.ASSET_ERROR),
            } and (route_id in _CONTROL_ROUTES or route_id == "job.start"):
                return self._error(
                    route_id, 409, "invalid_transition", message, request
                )
        return self._error(route_id, 400, "malformed_request", message, request)

    @staticmethod
    def _operation_key(request: Mapping[str, Any]) -> str | None:
        value = request.get("operation_key")
        return value if isinstance(value, str) else None

    def _error(
        self,
        route_id: str,
        status: int,
        error_code: str,
        message: str,
        request: Mapping[str, Any],
        *,
        retryable: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        result = {
            "schema": "job-http-error/v2",
            "error_code": error_code,
            "message": message or error_code,
            "retryable": retryable,
            "operation_key": self._operation_key(request),
            "cursor_domain": "job" if error_code in _CURSOR_ERRORS else None,
        }
        try:
            return status, parse_http_response(route_id, status, result)
        except Exception:  # noqa: BLE001
            fallback = {
                **result,
                "error_code": "malformed_request",
                "retryable": False,
                "cursor_domain": None,
            }
            return 400, parse_http_response(route_id, 400, fallback)


__all__ = [
    "JobAuthorityQueryPort",
    "JobCommandQueryAdapter",
    "JobCommandResolution",
    "JobControlResolver",
    "JobHttpApplicationError",
    "JobStartResolver",
]
