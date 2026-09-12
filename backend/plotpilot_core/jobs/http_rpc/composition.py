"""Production composition for the frozen v2 Job and chapter runtime surfaces.

The composition deliberately owns no persistence or process state.  It joins
the accepted P1 ``ExecutionAuthority`` and P2 supervisor through their public
ports, and every read or mutation remains anchored in those exact objects.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_core.api.v1.jobs.rpc import AttemptStartBinding
from backend.plotpilot_core.api.v2.jobs.router import build_job_router
from backend.plotpilot_core.api.v2.jobs.rpc.command_query import (
    JobCommandQueryAdapter,
)
from backend.plotpilot_core.api.v2.jobs.sse.adapter import JobSSEAdapter
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.broker.service import CapabilityBroker
from backend.plotpilot_core.events import (
    CoreEventStore,
    EventRecoveryService,
    JobEventStore,
    JobSnapshotStore,
)
from backend.plotpilot_core.jobs.backup import JobRuntimeBackupContributor
from backend.plotpilot_core.jobs.chapter_runtime import ChapterJobRuntime
from backend.plotpilot_core.jobs.checkpoint_adapter import DurableCheckpointAdapter
from backend.plotpilot_core.jobs.http_rpc.chapter_handlers import (
    CHAPTER_HOST_METHODS,
    DisposableAssetUploadBuffer,
    DisposablePollCursorBuffer,
    build_chapter_host_handlers,
)
from backend.plotpilot_core.jobs.http_rpc.dispatcher import (
    HostRpcApplicationDispatcher,
    HostRpcHandler,
    PreparedHostRpcResult,
)
from backend.plotpilot_core.jobs.http_rpc.model_handlers import (
    build_model_host_handlers,
)
from backend.plotpilot_core.model.broker import (
    HostProviderAdapterPort,
    ModelBroker,
)
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.job_control import JobControl
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import validate_rpc_request


class _RepositoryJobCatalog:
    """Read Job identities from the sole P1 repository without another ledger."""

    def __init__(self, authority: ExecutionAuthority) -> None:
        self.execution_authority = authority
        self.repository = authority.repository

    def list_job_ids(self, *, workspace_id: str) -> tuple[str, ...]:
        with self.repository.read_connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM execution_job WHERE workspace_id=? ORDER BY job_id",
                (workspace_id,),
            ).fetchall()
        return tuple(str(row["job_id"]) for row in rows)


class _StartResolverBinding:
    def __init__(
        self,
        repository: CoreAuthorityRepository,
        resolver: Any,
    ) -> None:
        owned_repository = getattr(resolver, "repository", repository)
        if owned_repository is not repository:
            raise TypeError("start_resolver belongs to another P1 repository")
        method = getattr(resolver, "resolve_start", None)
        if not callable(method):
            method = resolver if callable(resolver) else None
        if method is None:
            raise TypeError("start_resolver must be callable or expose resolve_start")
        self.repository = repository
        self._method = method

    def resolve_start(self, command: Mapping[str, Any]):
        return self._method(command)


class _ControlResolverBinding:
    def __init__(
        self,
        repository: CoreAuthorityRepository,
        resolver: Any,
    ) -> None:
        owned_repository = getattr(resolver, "repository", repository)
        if owned_repository is not repository:
            raise TypeError("control_resolver belongs to another P1 repository")
        method = getattr(resolver, "resolve_control", None)
        if not callable(method):
            method = resolver if callable(resolver) else None
        if method is None:
            raise TypeError(
                "control_resolver must be callable or expose resolve_control"
            )
        self.repository = repository
        self._method = method

    def resolve_control(self, command: Mapping[str, Any]):
        return self._method(command)


class _AttemptBoundHandler:
    def __init__(self, owner: _AttemptScopedChapterHandlers, method: str) -> None:
        self._owner = owner
        self._method = method

    def __call__(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        return self._owner.dispatch(self._method, request)


class _AttemptScopedChapterHandlers(Mapping[str, HostRpcHandler]):
    """Resolve one immutable P1 Attempt binding for each Host request.

    No Attempt is cached here: restart and late-output decisions therefore use
    the durable P1 rows on every request.  ``ChapterHostHandlerSet`` performs
    the final active-Attempt validation while its repository read gate is held.
    """

    def __init__(
        self,
        authority: ExecutionAuthority,
        checkpoints: DurableCheckpointAdapter,
        *,
        capability_broker: CapabilityBroker,
        provenance_receipt_resolver: Any,
        stream_commit_policy_resolver: Any,
    ) -> None:
        receipt_method = getattr(
            provenance_receipt_resolver,
            "resolve_provenance_receipt",
            None,
        )
        if not callable(receipt_method) and not callable(provenance_receipt_resolver):
            raise TypeError(
                "provenance_receipt_resolver must be callable or expose "
                "resolve_provenance_receipt"
            )
        policy_method = getattr(
            stream_commit_policy_resolver,
            "resolve_stream_commit_policy",
            None,
        )
        if not callable(policy_method) and not callable(stream_commit_policy_resolver):
            raise TypeError(
                "stream_commit_policy_resolver must be callable or expose "
                "resolve_stream_commit_policy"
            )
        self._authority = authority
        self._repository = authority.repository
        self._checkpoints = checkpoints
        self._asset_uploads = DisposableAssetUploadBuffer(authority.assets)
        self._poll_cursors = DisposablePollCursorBuffer()
        self._capability_broker = capability_broker
        self._receipt_resolver = provenance_receipt_resolver
        self._stream_policy_resolver = stream_commit_policy_resolver
        self._handlers = {
            method: _AttemptBoundHandler(self, method)
            for method in CHAPTER_HOST_METHODS
        }

    def __getitem__(self, key: str) -> HostRpcHandler:
        return self._handlers[key]

    def __iter__(self) -> Iterator[str]:
        return iter(CHAPTER_HOST_METHODS)

    def __len__(self) -> int:
        return len(CHAPTER_HOST_METHODS)

    def _resolve_attempt(
        self,
        request: Mapping[str, Any],
    ) -> tuple[str, str, AttemptStartBinding]:
        validate_rpc_request(request)
        meta = request.get("meta")
        if not isinstance(meta, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Host request meta is not an object",
            )
        with self._repository.read_connection() as connection:
            row = connection.execute(
                "SELECT j.workspace_id,j.run_snapshot_hash,"
                "a.job_id,a.step_id,a.attempt_id,a.worker_run_id,a.plugin_id,"
                "a.release_id,a.package_hash,a.capability_id,a.generation_id,"
                "a.lease_epoch,a.preallocated_receipt_id,"
                "a.expected_result_contract "
                "FROM execution_attempt a "
                "JOIN execution_job j ON j.job_id=a.job_id "
                "WHERE a.job_id=? AND a.step_id=? AND a.attempt_id=? "
                "AND a.lease_epoch=?",
                (
                    meta.get("job_id"),
                    meta.get("step_id"),
                    meta.get("attempt_id"),
                    meta.get("lease_epoch"),
                ),
            ).fetchone()
        if row is None:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "Host request does not identify a durable P1 Attempt",
            )
        binding = AttemptStartBinding(
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
        return str(row["workspace_id"]), str(row["run_snapshot_hash"]), binding

    def dispatch(
        self,
        method: str,
        request: Mapping[str, Any],
    ) -> PreparedHostRpcResult:
        if request.get("method") != method:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Host request method crossed its composed handler",
            )
        workspace_id, run_snapshot_hash, attempt = self._resolve_attempt(request)
        bound = build_chapter_host_handlers(
            self._authority,
            workspace_id=workspace_id,
            run_snapshot_hash=run_snapshot_hash,
            attempt=attempt,
            provenance_receipt_resolver=self._receipt_resolver,
            stream_commit_policy_resolver=self._stream_policy_resolver,
            checkpoints=self._checkpoints,
            asset_uploads=self._asset_uploads,
            capability_broker=self._capability_broker,
            poll_cursors=self._poll_cursors,
        )
        return bound[method](request)


@dataclass(frozen=True, slots=True)
class JobRuntimeComposition:
    """All P3 adapters joined over one P1 authority and one P2 supervisor."""

    authority: ExecutionAuthority
    repository: CoreAuthorityRepository
    assets: AssetStore
    supervisor: Any
    job_control: JobControl
    checkpoints: DurableCheckpointAdapter
    snapshots: JobSnapshotStore
    core_events: CoreEventStore
    events: JobEventStore
    recovery: EventRecoveryService
    sse: JobSSEAdapter
    chapter: ChapterJobRuntime
    capability_broker: CapabilityBroker
    backup: JobRuntimeBackupContributor
    handlers: Mapping[str, HostRpcHandler]
    model_broker: ModelBroker
    model_handlers: Mapping[str, HostRpcHandler]
    dispatcher: HostRpcApplicationDispatcher
    command_query: JobCommandQueryAdapter

    @property
    def http(self) -> JobCommandQueryAdapter:
        return self.command_query

    def router(self):
        return build_job_router(self.command_query)


def _select_stream_policy_resolver(
    explicit: Any | None,
    start_resolver: Any,
    control_resolver: Any,
) -> Any:
    explicit_method = getattr(explicit, "resolve_stream_commit_policy", None)
    if callable(explicit_method) or callable(explicit):
        return explicit
    for candidate in (start_resolver, control_resolver):
        if callable(getattr(candidate, "resolve_stream_commit_policy", None)):
            return candidate
    raise TypeError(
        "stream_commit_policy_resolver must be supplied or exposed by "
        "start_resolver or control_resolver"
    )


def compose_job_runtime(
    authority: ExecutionAuthority,
    supervisor: Any,
    *,
    capability_broker: CapabilityBroker,
    start_resolver: Any,
    control_resolver: Any,
    provenance_receipt_resolver: Any,
    stream_commit_policy_resolver: Any | None = None,
    model_broker: ModelBroker | None = None,
    host_provider_adapter: HostProviderAdapterPort | None = None,
    sse_replay_limit: int = 10_000,
) -> JobRuntimeComposition:
    """Compose P3 without opening another database or process authority."""

    if not isinstance(authority, ExecutionAuthority):
        raise TypeError("authority must be the accepted ExecutionAuthority")
    repository = authority.repository
    assets = authority.assets
    if not isinstance(repository, CoreAuthorityRepository):
        raise TypeError("authority repository must be CoreAuthorityRepository")
    if not isinstance(assets, AssetStore):
        raise TypeError("authority assets must be AssetStore")
    if not isinstance(capability_broker, CapabilityBroker):
        raise TypeError("capability_broker must be the accepted CapabilityBroker")
    broker_authorities = (
        (capability_broker.core, assets),
        (capability_broker.child_factory, authority),
        (capability_broker.operation_ledger, authority.operation_ledger),
        (capability_broker.child_records, authority.child_records),
        (capability_broker.attempt_context, authority),
    )
    if any(actual is not expected for actual, expected in broker_authorities):
        raise TypeError(
            "capability_broker ports must use the exact composed ExecutionAuthority"
        )
    if model_broker is not None and host_provider_adapter is not None:
        raise TypeError(
            "an explicit model_broker cannot be combined with a Host Provider adapter"
        )
    if model_broker is None:
        model_broker = ModelBroker(authority, host_provider_adapter)
    if (
        not isinstance(model_broker, ModelBroker)
        or model_broker.authority is not authority
        or model_broker.repository is not repository
        or model_broker.assets is not assets
    ):
        raise TypeError(
            "model_broker must use the exact composed ExecutionAuthority"
        )

    required_supervisor_ports = (
        "acquire",
        "release",
        "_peek_host_events",
        "_dispose_host_event",
        "bind_attempt",
        "interrupt_attempt",
        "unbind_attempt",
        "send_worker_request",
        "take_worker_response",
        "respond_host_request",
        "respond_host_error",
    )
    if any(
        not callable(getattr(supervisor, name, None))
        for name in required_supervisor_ports
    ):
        raise TypeError("supervisor does not expose the accepted P2 Job ports")

    job_control = JobControl(supervisor)
    checkpoints = DurableCheckpointAdapter(authority)
    snapshots = JobSnapshotStore(repository, assets, checkpoints)
    core_events = CoreEventStore(repository)
    events = JobEventStore(repository)
    recovery = EventRecoveryService(core_events, events)
    sse = JobSSEAdapter(recovery, snapshots, replay_limit=sse_replay_limit)
    chapter = ChapterJobRuntime(
        authority,
        job_control=job_control,
        checkpoints=checkpoints,
        attempt_lifecycle=supervisor,
    )
    stream_policy = _select_stream_policy_resolver(
        stream_commit_policy_resolver,
        start_resolver,
        control_resolver,
    )
    handlers = _AttemptScopedChapterHandlers(
        authority,
        checkpoints,
        capability_broker=capability_broker,
        provenance_receipt_resolver=provenance_receipt_resolver,
        stream_commit_policy_resolver=stream_policy,
    )
    model_handlers = build_model_host_handlers(model_broker)
    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {**dict(handlers), **dict(model_handlers)},
        event_disposition=job_control,
    )
    catalog = _RepositoryJobCatalog(authority)
    command_query = JobCommandQueryAdapter(
        catalog,
        snapshots=snapshots,
        events=events,
        sse=sse,
        start_resolver=_StartResolverBinding(repository, start_resolver),
        control_resolver=_ControlResolverBinding(repository, control_resolver),
    )
    backup = JobRuntimeBackupContributor(repository)
    return JobRuntimeComposition(
        authority=authority,
        repository=repository,
        assets=assets,
        supervisor=supervisor,
        job_control=job_control,
        checkpoints=checkpoints,
        snapshots=snapshots,
        core_events=core_events,
        events=events,
        recovery=recovery,
        sse=sse,
        chapter=chapter,
        capability_broker=capability_broker,
        backup=backup,
        handlers=handlers,
        model_broker=model_broker,
        model_handlers=model_handlers,
        dispatcher=dispatcher,
        command_query=command_query,
    )


__all__ = ["JobRuntimeComposition", "compose_job_runtime"]
