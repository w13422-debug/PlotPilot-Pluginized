"""Lease- and release-fenced lazy worker lifecycle."""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from .lookup import ImmutableRuntimeLookup
from .models import (
    AttemptFence,
    Clock,
    InstallFence,
    ManagedProcess,
    ProcessFactory,
    ResolvedWorkerRoute,
    SupervisorAuthority,
    WorkerFence,
    WorkerState,
    WorkerStatus,
    WorkerTicket,
)
from .rpc import FramedRpcSession, RpcEvent

_TERMINAL = {
    WorkerState.STOPPED,
    WorkerState.CRASHED,
    WorkerState.FAILED,
    WorkerState.FENCED,
}
_PROTOCOL_STATES = {WorkerState.STARTING, WorkerState.READY}
_RELEASE_ID = re.compile(r"^[0-9a-f]{64}$")


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class SupervisorConfig:
    handshake_timeout: float = 10.0
    heartbeat_timeout: float = 30.0
    idle_timeout: float = 60.0
    shutdown_timeout: float = 5.0
    termination_timeout: float = 5.0
    stderr_limit_bytes: int = 64 * 1024
    event_queue_limit: int = 64
    rpc_pending_limit: int = 64
    rpc_inbound_limit: int = 64

    def __post_init__(self) -> None:
        for name in (
            "handshake_timeout",
            "heartbeat_timeout",
            "idle_timeout",
            "shutdown_timeout",
            "termination_timeout",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if (
            not isinstance(self.stderr_limit_bytes, int)
            or isinstance(self.stderr_limit_bytes, bool)
            or self.stderr_limit_bytes < 1024
        ):
            raise ValueError("stderr_limit_bytes must be an integer >= 1024")
        for name in ("event_queue_limit", "rpc_pending_limit", "rpc_inbound_limit"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass
class _Record:
    lifecycle_id: str
    fence: WorkerFence
    route: ResolvedWorkerRoute
    process: ManagedProcess
    session: FramedRpcSession
    state: WorkerState
    retains: set[str]
    retain_sequence: int
    started_at: float
    last_heartbeat: float
    worker_instance_id: str | None = None
    attempt_fence: AttemptFence | None = None
    install_fence: InstallFence | None = None
    idle_since: float | None = None
    stop_deadline: float | None = None
    failure: str | None = None
    exit_code: int | None = None
    claim_released: bool = False
    claim_release_requires_poll: bool = False
    exit_poll_confirmed: bool = False
    termination_target: WorkerState | None = None
    termination_deadline: float | None = None
    kill_sent: bool = False
    attempt_reconcile_required: bool = False
    attempt_reconciled: bool = False
    reconcile_reason: str | None = None
    stderr_tail: bytearray = field(default_factory=bytearray)
    events: list[RpcEvent] = field(default_factory=list)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)
    rpc_lock: threading.RLock = field(default_factory=threading.RLock)
    deferred_terminal_ack: bool = False


class PluginProcessSupervisor:
    """Own process observations while Core owns every run/stop decision."""

    def __init__(
        self,
        *,
        authority: SupervisorAuthority,
        lookup: ImmutableRuntimeLookup,
        processes: ProcessFactory,
        clock: Clock | None = None,
        config: SupervisorConfig | None = None,
        id_factory: Callable[[], str] | None = None,
        retain_id_factory: Callable[[], str] | None = None,
        durable_host_replay: Callable[[Mapping[str, object]], bytes | None]
        | None = None,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._authority = authority
        self._lookup = lookup
        self._processes = processes
        self._clock = clock or SystemClock()
        self._config = config or SupervisorConfig()
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._retain_id_factory = retain_id_factory or (lambda: str(uuid.uuid4()))
        self._durable_host_replay = durable_host_replay
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._records: dict[tuple[str, str], _Record] = {}
        self._current: dict[str, str] = {}
        self._pending_releases: dict[tuple[str, str], tuple[WorkerFence, str]] = {}
        self._worker_locks: dict[str, threading.Lock] = {}
        self._pending_retry_lock = threading.Lock()
        self._lock = threading.RLock()
        self._acquire_condition = threading.Condition(self._lock)
        self._active_acquires = 0
        self._accepting = True
        self._callbacks_enabled = True
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False

    def _deadline(self, seconds: float) -> str:
        value = self._utc_now() + timedelta(seconds=seconds)
        value = value.astimezone(timezone.utc).replace(microsecond=0)
        return value.isoformat().replace("+00:00", "Z")

    def _worker_lock(self, worker_id: str) -> threading.Lock:
        with self._lock:
            return self._worker_locks.setdefault(worker_id, threading.Lock())

    @staticmethod
    def _key(worker_id: str, lifecycle_id: str) -> tuple[str, str]:
        return worker_id, lifecycle_id

    def _current_record_locked(self, worker_id: str) -> _Record | None:
        lifecycle_id = self._current.get(worker_id)
        return (
            None
            if lifecycle_id is None
            else self._records.get(self._key(worker_id, lifecycle_id))
        )

    def _record_locked(self, worker_id: str, lifecycle_id: str) -> _Record | None:
        return self._records.get(self._key(worker_id, lifecycle_id))

    @staticmethod
    def _matches_lifecycle(record: _Record, ticket: WorkerTicket) -> bool:
        return (
            record.lifecycle_id == ticket.lifecycle_id
            and record.fence.worker_id == ticket.worker_id
            and record.fence.release_id == ticket.release_id
            and record.fence.pin_epoch == ticket.pin_epoch
            and record.fence.retire_epoch == ticket.retire_epoch
        )

    @staticmethod
    def _ticket(record: _Record, retain_id: str) -> WorkerTicket:
        return WorkerTicket(
            worker_id=record.fence.worker_id,
            lifecycle_id=record.lifecycle_id,
            retain_id=retain_id,
            release_id=record.fence.release_id,
            pin_epoch=record.fence.pin_epoch,
            retire_epoch=record.fence.retire_epoch,
        )

    def _new_retain_id_locked(self, record: _Record) -> str:
        record.retain_sequence += 1
        return f"{self._retain_id_factory()}:{record.retain_sequence}"

    def _is_current_locked(self, record: _Record) -> bool:
        return self._current.get(record.fence.worker_id) == record.lifecycle_id

    @staticmethod
    def _is_busy(record: _Record) -> bool:
        return (
            bool(record.retains)
            or record.attempt_fence is not None
            or record.install_fence is not None
        )

    def _mark_termination_locked(
        self,
        record: _Record,
        target: WorkerState,
        failure: str | None,
        *,
        reconcile_attempt: bool = False,
    ) -> bool:
        if reconcile_attempt and record.attempt_fence is not None:
            record.attempt_reconcile_required = True
            record.reconcile_reason = failure or "worker terminated"
        if record.state == WorkerState.TERMINATING:
            if target in {WorkerState.FAILED, WorkerState.FENCED}:
                record.termination_target = target
            if failure:
                record.failure = failure
            return False
        if record.state in _TERMINAL:
            return False
        record.state = WorkerState.TERMINATING
        record.termination_target = target
        record.failure = failure
        record.stop_deadline = None
        record.termination_deadline = (
            self._clock.monotonic() + self._config.termination_timeout
        )
        return True

    def _begin_termination(
        self,
        key: tuple[str, str],
        target: WorkerState,
        failure: str | None,
        *,
        reconcile_attempt: bool = False,
    ) -> None:
        process: ManagedProcess | None = None
        with self._lock:
            record = self._records.get(key)
            if record is not None and self._mark_termination_locked(
                record,
                target,
                failure,
                reconcile_attempt=reconcile_attempt,
            ):
                process = record.process
        if process is None:
            return
        try:
            process.terminate()
        except Exception as exc:  # noqa: BLE001 -- injected process adapter boundary
            with self._lock:
                record = self._records.get(key)
                if record is not None:
                    record.failure = (
                        f"{record.failure or 'termination'}; terminate failed: {exc}"
                    )

    def _defer_termination_for_terminal_ack(
        self,
        key: tuple[str, str],
        *,
        attempt: AttemptFence | None = None,
        install: InstallFence | None = None,
        failure: str,
    ) -> bool:
        """Fence a lifecycle while leaving stdin open for one staged terminal ACK."""

        with self._lock:
            record = self._records.get(key)
        if record is None:
            return False
        replay: tuple[str, bytes] | None = None
        with record.rpc_lock:
            try:
                exact_commit = (
                    attempt is not None
                    and record.session.has_terminal_attempt_commit(attempt)
                    or install is not None
                    and record.session.has_terminal_install_commit(install)
                )
                if not exact_commit:
                    replay = record.session.stage_terminal_replay(
                        attempt=attempt, install=install
                    )
                    exact_commit = replay is not None
                pending = record.session.terminal_commit_write_pending()
            except ContractError:
                return False
            with self._lock:
                current = self._records.get(key)
                if (
                    current is not record
                    or record.state in _TERMINAL
                    or not exact_commit
                    or not pending
                ):
                    return False
                self._mark_termination_locked(record, WorkerState.FENCED, failure)
                record.deferred_terminal_ack = True
                process = record.process
        if replay is not None:
            request_id, frame = replay
            try:
                process.write(
                    frame,
                    on_written=lambda: self._mark_host_frame_written(
                        record, request_id, frame
                    ),
                )
            except Exception as exc:  # noqa: BLE001 -- exact lifecycle is failed closed
                with self._lock:
                    current = self._records.get(key)
                    if current is record:
                        record.deferred_terminal_ack = False
                        record.failure = f"terminal Host replay write failed: {exc}"
                try:
                    process.terminate()
                except Exception:  # noqa: BLE001, S110 -- kill deadline remains armed
                    pass
        return True

    def _release_or_enqueue(self, fence: WorkerFence, lifecycle_id: str) -> bool:
        key = self._key(fence.worker_id, lifecycle_id)
        try:
            released = self._authority.release(fence, lifecycle_id)
        except Exception:  # noqa: BLE001 -- injected authority adapter boundary
            released = False
        with self._lock:
            if released:
                self._pending_releases.pop(key, None)
            else:
                self._pending_releases[key] = (fence, lifecycle_id)
        return released

    def _holds_authority(self, fence: WorkerFence, lifecycle_id: str) -> bool:
        try:
            snapshot = self._authority.snapshot(fence.worker_id)
            return fence.same_authority(snapshot) and self._authority.holds(
                fence, lifecycle_id
            )
        except Exception:  # noqa: BLE001 -- fail closed at the authority boundary
            return False

    def _current_authority(self, record: _Record) -> bool:
        return self._holds_authority(record.fence, record.lifecycle_id)

    def _reconcile_and_release(self, key: tuple[str, str]) -> None:
        with self._lock:
            record = self._records.get(key)
        if record is None:
            return
        with record.cleanup_lock:
            self._reconcile_and_release_serial(key)

    def _reconcile_and_release_serial(self, key: tuple[str, str]) -> None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            if record.claim_released:
                if not self._is_current_locked(record):
                    self._records.pop(key, None)
                return
            if record.claim_release_requires_poll and not record.exit_poll_confirmed:
                return
            if record.exit_code is None:
                return
            fence = record.fence
            lifecycle_id = record.lifecycle_id
            attempt = record.attempt_fence
            reconcile = (
                record.attempt_reconcile_required
                and not record.attempt_reconciled
                and attempt is not None
            )
            reason = record.reconcile_reason or "worker exited unexpectedly"
        if reconcile and attempt is not None:
            try:
                held = self._authority.holds_attempt(fence, attempt, lifecycle_id)
                reconciled = not held or self._authority.interrupt_attempt(
                    fence, attempt, lifecycle_id, reason
                )
            except Exception:  # noqa: BLE001 -- retry exact reconciliation on tick
                reconciled = False
            if not reconciled:
                return
            with self._lock:
                record = self._records.get(key)
                if record is None:
                    return
                record.attempt_reconciled = True
        released = self._release_or_enqueue(fence, lifecycle_id)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.claim_released = released
            if released and not self._is_current_locked(record):
                self._records.pop(key, None)

    def acquire(
        self, worker_id: str, *, expected_release_id: str | None = None
    ) -> WorkerTicket:
        """Admit an acquire only while the supervisor owns live ingress."""

        with self._acquire_condition:
            if not self._accepting:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "plugin process supervisor is shut down",
                )
            self._active_acquires += 1
        try:
            return self._acquire_open(
                worker_id, expected_release_id=expected_release_id
            )
        finally:
            with self._acquire_condition:
                self._active_acquires -= 1
                self._acquire_condition.notify_all()

    def _acquire_open(
        self, worker_id: str, *, expected_release_id: str | None = None
    ) -> WorkerTicket:
        """Lazy-start or retain the exact worker currently authorized by Core."""

        if expected_release_id is not None and (
            not isinstance(expected_release_id, str)
            or _RELEASE_ID.fullmatch(expected_release_id) is None
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "expected release is not lowercase SHA-256",
            )
        with self._worker_lock(worker_id):
            current = self._authority.snapshot(worker_id)
            if expected_release_id is not None and (
                current is None
                or not current.allowed
                or current.release_id != expected_release_id
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Core authority does not hold the RunSnapshot-bound release",
                )
            reusable: _Record | None = None
            with self._lock:
                old = self._current_record_locked(worker_id)
                if (
                    old is not None
                    and old.fence.same_authority(current)
                    and old.state in _PROTOCOL_STATES
                ):
                    reusable = old
            if reusable is not None and self._holds_authority(
                reusable.fence, reusable.lifecycle_id
            ):
                with self._lock:
                    old = self._current_record_locked(worker_id)
                    if old is reusable and old.state in _PROTOCOL_STATES:
                        retain_id = self._new_retain_id_locked(old)
                        was_idle = not old.retains
                        old.retains.add(retain_id)
                        old.idle_since = None
                        if was_idle and old.state == WorkerState.READY:
                            old.last_heartbeat = self._clock.monotonic()
                        return self._ticket(old, retain_id)

            terminate_key: tuple[str, str] | None = None
            with self._lock:
                old = self._current_record_locked(worker_id)
                if old is not None and old.state not in _TERMINAL:
                    terminate_key = self._key(worker_id, old.lifecycle_id)
                elif old is not None and old.claim_released:
                    self._records.pop(self._key(worker_id, old.lifecycle_id), None)
            if terminate_key is not None:
                self._begin_termination(
                    terminate_key,
                    WorkerState.FENCED,
                    "replaced by a new Core authority fence",
                    reconcile_attempt=True,
                )

            lifecycle_id = self._id_factory()
            fence: WorkerFence | None = None
            claimed = False
            try:
                fence = self._authority.claim(worker_id, lifecycle_id)
                if fence is None or not fence.allowed:
                    raise ContractError(
                        ErrorCode.RELEASE_RETIRING,
                        "Core authority rejected the worker pin",
                    )
                claimed = True
                if (
                    expected_release_id is not None
                    and fence.release_id != expected_release_id
                ):
                    self._release_or_enqueue(fence, lifecycle_id)
                    claimed = False
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "Core authority changed release while requesting a RunSnapshot-bound worker",
                    )
                route = self._lookup.resolve_worker(fence, lifecycle_id)
                session = FramedRpcSession(
                    plugin_id=fence.plugin_id,
                    generation_id=fence.generation_id,
                    release_id=fence.release_id,
                    expected_capabilities=route.capabilities,
                    id_factory=self._id_factory,
                    attempt_authority=lambda attempt: self._authority.holds_attempt(
                        fence, attempt, lifecycle_id
                    ),
                    install_authority=lambda install: self._authority.holds_install(
                        fence, install, lifecycle_id
                    ),
                    durable_host_replay=self._durable_host_replay,
                    pending_limit=self._config.rpc_pending_limit,
                    inbound_limit=self._config.rpc_inbound_limit,
                )
                handshake = session.begin_handshake(
                    data_generation_id=fence.data_generation_id,
                    deadline_at=self._deadline(self._config.handshake_timeout),
                )
                now = self._clock.monotonic()
                retain_id = f"{self._retain_id_factory()}:1"
                key = self._key(worker_id, lifecycle_id)
                callback_lock = threading.Lock()
                pending_callbacks: list[tuple[str, bytes | int | str]] = []
                callbacks_armed = False

                def dispatch(kind: str, payload: bytes | int | str) -> None:
                    nonlocal callbacks_armed
                    with callback_lock:
                        if not callbacks_armed:
                            pending_callbacks.append((kind, payload))
                            return
                    if kind == "stdout":
                        assert isinstance(payload, bytes)
                        self._on_stdout(worker_id, lifecycle_id, payload)
                    elif kind == "stderr":
                        assert isinstance(payload, bytes)
                        self._on_stderr(worker_id, lifecycle_id, payload)
                    elif kind == "transport":
                        assert isinstance(payload, str)
                        self._on_transport_error(worker_id, lifecycle_id, payload)
                    else:
                        assert isinstance(payload, int)
                        self._on_exit(worker_id, lifecycle_id, payload)

                process = self._processes.start(
                    route,
                    on_stdout=lambda data: dispatch("stdout", data),
                    on_stderr=lambda data: dispatch("stderr", data),
                    on_exit=lambda code: dispatch("exit", code),
                    on_transport_error=lambda error: dispatch("transport", error),
                )
                record = _Record(
                    lifecycle_id=lifecycle_id,
                    fence=fence,
                    route=route,
                    process=process,
                    session=session,
                    state=WorkerState.STARTING,
                    retains=set(),
                    retain_sequence=0,
                    started_at=now,
                    last_heartbeat=now,
                )
                if not self._holds_authority(fence, lifecycle_id):
                    record.claim_release_requires_poll = True
                    with self._lock:
                        self._records[key] = record
                    claimed = False
                    self._begin_termination(
                        key,
                        WorkerState.FENCED,
                        "Core authority changed while starting the worker",
                    )
                    with callback_lock:
                        callbacks_armed = True
                        queued = tuple(pending_callbacks)
                        pending_callbacks.clear()
                    for kind, payload in queued:
                        dispatch(kind, payload)
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "Core authority changed while starting the worker",
                    )
            except Exception:
                if claimed and fence is not None:
                    self._release_or_enqueue(fence, lifecycle_id)
                raise

            with self._lock:
                previous_lifecycle = self._current.get(worker_id)
                record.retains.add(retain_id)
                record.retain_sequence = 1
                self._records[key] = record
                self._current[worker_id] = lifecycle_id
                if (
                    previous_lifecycle is not None
                    and previous_lifecycle != lifecycle_id
                ):
                    previous_key = self._key(worker_id, previous_lifecycle)
                    previous = self._records.get(previous_key)
                    if previous is not None and previous.claim_released:
                        self._records.pop(previous_key, None)
            with callback_lock:
                callbacks_armed = True
                queued = tuple(pending_callbacks)
                pending_callbacks.clear()
            for kind, payload in queued:
                dispatch(kind, payload)
            with self._lock:
                live = self._records.get(key)
                startup_committed = (
                    live is not None and live.state == WorkerState.STARTING
                )
            if not startup_committed:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "worker exited before startup committed",
                )
            try:
                process.write(handshake)
            except Exception as exc:
                self._begin_termination(
                    key,
                    WorkerState.FAILED,
                    f"handshake write failed: {exc}",
                    reconcile_attempt=True,
                )
                raise
            return self._ticket(record, retain_id)

    def _on_stdout(self, worker_id: str, lifecycle_id: str, data: bytes) -> None:
        key = self._key(worker_id, lifecycle_id)
        with self._lock:
            if not self._callbacks_enabled:
                return
            record = self._records.get(key)
            if record is None or record.state not in _PROTOCOL_STATES:
                return
        if not self._current_authority(record):
            self._begin_termination(
                key,
                WorkerState.FENCED,
                "Core authority fence changed",
                reconcile_attempt=True,
            )
            return
        failure: str | None = None
        replay_frames: list[tuple[str, bytes]] = []
        with record.rpc_lock:
            try:
                events = record.session.feed(data)
                replay_by_id = {
                    str(event.message["id"]): record.session.host_replay_frame(
                        str(event.message["id"])
                    )
                    for event in events
                    if event.kind == "host_replay"
                }
            except Exception as exc:  # noqa: BLE001 -- protocol is closed on every parser error
                failure = f"RPC protocol failure: {exc}"
                events = ()
                replay_by_id = {}
        with self._lock:
            record = self._records.get(key)
            if record is None or record.state not in _PROTOCOL_STATES:
                return
            now = self._clock.monotonic()
            if failure is None:
                for event in events:
                    if event.kind == "handshake":
                        if record.state != WorkerState.STARTING:
                            failure = (
                                "RPC protocol failure: late or duplicate handshake"
                            )
                            break
                        record.state = WorkerState.READY
                        record.last_heartbeat = now
                        record.worker_instance_id = str(
                            event.message["result"]["worker_instance_id"]
                        )
                    elif event.kind == "heartbeat":
                        record.last_heartbeat = now
                    elif event.kind == "host_replay":
                        frame = replay_by_id.get(str(event.message["id"]))
                        if frame is not None:
                            replay_frames.append((str(event.message["id"]), frame))
                    else:
                        if len(record.events) >= self._config.event_queue_limit:
                            failure = "RPC event capacity exceeded"
                            break
                        record.events.append(event)
        if failure is not None:
            self._begin_termination(
                key, WorkerState.FAILED, failure, reconcile_attempt=True
            )
            return
        for request_id, frame in replay_frames:
            try:
                record.process.write(
                    frame,
                    on_written=lambda request_id=request_id, frame=frame: (
                        self._mark_host_frame_written(
                            record,
                            request_id,
                            frame,
                        )
                    ),
                )
            except Exception as exc:  # noqa: BLE001 -- isolate transport failure to this lifecycle
                self._begin_termination(
                    key,
                    WorkerState.FAILED,
                    f"Host replay write failed: {exc}",
                    reconcile_attempt=True,
                )
                return

    def _on_stderr(self, worker_id: str, lifecycle_id: str, data: bytes) -> None:
        with self._lock:
            if not self._callbacks_enabled:
                return
            record = self._record_locked(worker_id, lifecycle_id)
            if record is None:
                return
            record.stderr_tail.extend(data)
            overflow = len(record.stderr_tail) - self._config.stderr_limit_bytes
            if overflow > 0:
                del record.stderr_tail[:overflow]

    def _on_transport_error(
        self, worker_id: str, lifecycle_id: str, error: str
    ) -> None:
        with self._lock:
            if not self._callbacks_enabled:
                return
        self._begin_termination(
            self._key(worker_id, lifecycle_id),
            WorkerState.FAILED,
            f"RPC transport failure: {error}",
            reconcile_attempt=True,
        )

    def _on_exit(self, worker_id: str, lifecycle_id: str, code: int) -> None:
        key = self._key(worker_id, lifecycle_id)
        with self._lock:
            if not self._callbacks_enabled:
                return
            record = self._records.get(key)
        if record is None:
            return
        with record.rpc_lock, self._lock:
            current = self._records.get(key)
            if current is not record or record.exit_code is not None:
                return
            if record.claim_release_requires_poll and not record.exit_poll_confirmed:
                return
            record.exit_code = code
            try:
                record.session.finish()
            except ContractError as exc:
                record.termination_target = WorkerState.FAILED
                record.failure = f"RPC EOF protocol failure: {exc}"
                if record.attempt_fence is not None:
                    record.attempt_reconcile_required = True
                    record.reconcile_reason = record.failure
            if record.termination_target is not None:
                record.state = record.termination_target
            elif record.state == WorkerState.STOPPING:
                record.state = WorkerState.STOPPED
                record.failure = (
                    None if code == 0 else f"shutdown exited with code {code}"
                )
                if code != 0 and record.attempt_fence is not None:
                    record.attempt_reconcile_required = True
                    record.reconcile_reason = record.failure
            else:
                record.state = WorkerState.CRASHED
                record.failure = f"worker exited with code {code}"
                if record.attempt_fence is not None:
                    record.attempt_reconcile_required = True
                    record.reconcile_reason = record.failure
        self._reconcile_and_release(key)

    def feed_stdout(self, ticket: WorkerTicket, data: bytes) -> None:
        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if record is None or not self._matches_lifecycle(record, ticket):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "late process bytes belong to an old lifecycle",
                )
        self._on_stdout(ticket.worker_id, ticket.lifecycle_id, data)

    def process_exited(self, ticket: WorkerTicket, code: int) -> None:
        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if record is None or not self._matches_lifecycle(record, ticket):
                return
        self._on_exit(ticket.worker_id, ticket.lifecycle_id, code)

    def release(self, ticket: WorkerTicket) -> None:
        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if record is None or not self._matches_lifecycle(record, ticket):
                return
            if ticket.retain_id not in record.retains:
                return
            record.retains.remove(ticket.retain_id)
            if not self._is_busy(record):
                record.idle_since = self._clock.monotonic()

    def _ticket_record(
        self,
        ticket: WorkerTicket,
        *,
        allowed_states: frozenset[WorkerState] = frozenset({WorkerState.READY}),
    ) -> _Record:
        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if (
                record is None
                or not self._matches_lifecycle(record, ticket)
                or not self._is_current_locked(record)
                or record.state not in allowed_states
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "operation belongs to an old worker lifecycle",
                )
            return record

    def bind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        record = self._ticket_record(ticket)
        if fence.owner_id != ticket.lifecycle_id or not self._current_authority(record):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Attempt binding belongs to an old worker"
            )
        if not self._authority.holds_attempt(record.fence, fence, record.lifecycle_id):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Core authority rejected the Attempt fence"
            )
        with record.rpc_lock, self._lock:
            current = self._records.get(
                self._key(ticket.worker_id, ticket.lifecycle_id)
            )
            if (
                current is not record
                or record.state != WorkerState.READY
                or not self._is_current_locked(record)
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Attempt binding raced worker replacement"
                )
            record.session.bind_attempt(fence, authority_checked=True)
            record.attempt_fence = fence
            record.last_heartbeat = self._clock.monotonic()
            record.idle_since = None

    def interrupt_attempt(
        self, ticket: WorkerTicket, fence: AttemptFence, reason: str
    ) -> None:
        """Fence an exact Attempt into the existing bounded reconciliation path."""

        if not isinstance(ticket, WorkerTicket) or not isinstance(fence, AttemptFence):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Attempt interruption requires exact worker and Attempt fences",
            )
        if fence.owner_id != ticket.lifecycle_id:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "Attempt interruption belongs to another worker lifecycle",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Attempt interruption requires a non-empty reason",
            )

        reason = reason.strip()
        key = self._key(ticket.worker_id, ticket.lifecycle_id)
        with self._lock:
            record = self._records.get(key)
            if record is None or not self._matches_lifecycle(record, ticket):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Attempt interruption belongs to an unknown worker lifecycle",
                )

        with record.cleanup_lock, self._lock:
            current = self._records.get(key)
            if current is not record or not self._matches_lifecycle(record, ticket):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Attempt interruption raced worker cleanup",
                )
            if record.claim_released:
                if record.attempt_fence == fence and record.attempt_reconciled:
                    return
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "worker claim was released before Attempt interruption",
                )
            if record.attempt_fence not in {None, fence}:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "worker lifecycle is bound to another Attempt",
                )
            record.attempt_fence = fence
            record.attempt_reconcile_required = True
            record.attempt_reconciled = False
            record.reconcile_reason = reason

        self._begin_termination(
            key,
            WorkerState.FENCED,
            reason,
            reconcile_attempt=True,
        )
        self._reconcile_and_release(key)

    def unbind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        record = self._ticket_record(
            ticket,
            allowed_states=frozenset({WorkerState.READY, WorkerState.TERMINATING}),
        )
        if not self._current_authority(record):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Attempt unbind belongs to an old worker"
            )
        held = self._authority.holds_attempt(record.fence, fence, record.lifecycle_id)
        closed_attempt = getattr(self._authority, "closed_attempt", None)
        durable_closed = bool(
            callable(closed_attempt)
            and closed_attempt(record.fence, fence, record.lifecycle_id)
        )
        with record.rpc_lock, self._lock:
            terminal_commit = record.session.has_terminal_attempt_commit(fence)
            if (
                record.state not in {WorkerState.READY, WorkerState.TERMINATING}
                or not self._is_current_locked(record)
                or (not held and not terminal_commit and not durable_closed)
                or (
                    record.state == WorkerState.TERMINATING
                    and not terminal_commit
                    and not durable_closed
                )
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Attempt unbind raced worker termination"
                )
            record.session.unbind_attempt(fence)
            record.attempt_fence = None
            if not self._is_busy(record):
                record.idle_since = self._clock.monotonic()

    def send_worker_request(
        self,
        ticket: WorkerTicket,
        method: str,
        params: Mapping[str, object],
        meta: Mapping[str, object],
        *,
        request_id: str,
    ) -> str:
        """Write one canonical Worker RPC through the current lifecycle.

        The existing :class:`FramedRpcSession` remains the sole framing,
        identity and pending-response authority.  Retrying the same non-secret
        request ID writes the exact retained frame; a one-shot secret request
        fails closed as an uncertain external effect instead of replaying it.
        """

        record = self._ticket_record(ticket)
        if not self._current_authority(record):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Worker request belongs to an old lifecycle"
            )
        with record.rpc_lock, self._lock:
            current = self._records.get(
                self._key(ticket.worker_id, ticket.lifecycle_id)
            )
            if (
                current is not record
                or record.state != WorkerState.READY
                or not self._is_current_locked(record)
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Worker request raced lifecycle replacement"
                )
            frame = record.session.build_worker_request(
                method,
                params,
                meta,
                request_id=request_id,
            )
            process = record.process
        try:
            process.write(frame)
        except BufferError:
            self._begin_termination(
                self._key(ticket.worker_id, ticket.lifecycle_id),
                WorkerState.FAILED,
                "Worker request writer capacity exceeded",
                reconcile_attempt=True,
            )
            raise
        return request_id

    def take_worker_response(
        self, ticket: WorkerTicket, request_id: str
    ) -> RpcEvent | None:
        """Take one validated Worker response without draining Host events."""

        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if (
                record is None
                or not self._matches_lifecycle(record, ticket)
                or not self._is_current_locked(record)
                or record.state in _TERMINAL
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Worker response belongs to an old lifecycle"
                )
            for index, event in enumerate(record.events):
                if (
                    event.kind == "response"
                    and str(event.message.get("id")) == request_id
                ):
                    return record.events.pop(index)
            return None

    def bind_install(self, ticket: WorkerTicket, fence: InstallFence) -> None:
        record = self._ticket_record(ticket)
        if not self._current_authority(record) or not self._authority.holds_install(
            record.fence, fence, record.lifecycle_id
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE, "install binding belongs to an old worker"
            )
        with record.rpc_lock, self._lock:
            if record.state != WorkerState.READY or not self._is_current_locked(record):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "install binding raced worker termination"
                )
            record.session.bind_install(fence, authority_checked=True)
            record.install_fence = fence
            record.idle_since = None

    def unbind_install(self, ticket: WorkerTicket, fence: InstallFence) -> None:
        record = self._ticket_record(
            ticket,
            allowed_states=frozenset({WorkerState.READY, WorkerState.TERMINATING}),
        )
        if not self._current_authority(record):
            raise ContractError(
                ErrorCode.STALE_LEASE, "install unbind belongs to an old worker"
            )
        held = self._authority.holds_install(record.fence, fence, record.lifecycle_id)
        with record.rpc_lock, self._lock:
            terminal_commit = record.session.has_terminal_install_commit(fence)
            if (
                record.state not in {WorkerState.READY, WorkerState.TERMINATING}
                or not self._is_current_locked(record)
                or (not held and not terminal_commit)
                or (record.state == WorkerState.TERMINATING and not terminal_commit)
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "install unbind raced worker termination"
                )
            record.session.unbind_install(fence)
            record.install_fence = None
            if not self._is_busy(record):
                record.idle_since = self._clock.monotonic()

    def _retry_pending_releases(self) -> None:
        with self._pending_retry_lock:
            self._retry_pending_releases_serial()

    def _retry_pending_releases_serial(self) -> None:
        with self._lock:
            pending = tuple(self._pending_releases.values())
        for fence, lifecycle_id in pending:
            key = self._key(fence.worker_id, lifecycle_id)
            with self._lock:
                record = self._records.get(key)
            cleanup_lock = (
                record.cleanup_lock if record is not None else threading.Lock()
            )
            with cleanup_lock:
                with self._lock:
                    record = self._records.get(key)
                    if key not in self._pending_releases or (
                        record is not None and record.claim_released
                    ):
                        continue
                released = self._release_or_enqueue(fence, lifecycle_id)
                if released:
                    with self._lock:
                        record = self._records.get(key)
                        if record is None:
                            continue
                        record.claim_released = True
                        if not self._is_current_locked(record):
                            self._records.pop(key, None)

    def _confirm_cleanup_exit(
        self, key: tuple[str, str], process: ManagedProcess
    ) -> bool:
        try:
            code = process.poll()
        except Exception:  # noqa: BLE001 -- a failed observation cannot release the live-process claim
            return False
        if code is None:
            return False
        with self._lock:
            record = self._records.get(key)
            if (
                record is None
                or record.process is not process
                or not record.claim_release_requires_poll
                or record.exit_poll_confirmed
            ):
                return False
            record.exit_poll_confirmed = True
            worker_id = record.fence.worker_id
            lifecycle_id = record.lifecycle_id
        self._on_exit(worker_id, lifecycle_id, code)
        return True

    def tick(self) -> None:
        """Run watchdog/idle transitions without changing Core authority."""

        with self._lock:
            if self._shutdown_complete:
                return
        self._retry_pending_releases()
        now = self._clock.monotonic()
        with self._lock:
            keys = tuple(self._records)
        for key in keys:
            with self._lock:
                record = self._records.get(key)
                if record is None:
                    continue
                state = record.state
                termination_deadline = record.termination_deadline
                kill_sent = record.kill_sent
                process = record.process
                current = self._is_current_locked(record)
                cleanup_poll_pending = (
                    record.claim_release_requires_poll
                    and not record.exit_poll_confirmed
                )
            if cleanup_poll_pending and self._confirm_cleanup_exit(key, process):
                continue
            if state in _TERMINAL:
                self._reconcile_and_release(key)
                continue
            if state == WorkerState.TERMINATING:
                if (
                    termination_deadline is not None
                    and now >= termination_deadline
                    and not kill_sent
                ):
                    with self._lock:
                        live = self._records.get(key)
                        if (
                            live is None
                            or live.state != WorkerState.TERMINATING
                            or live.kill_sent
                        ):
                            continue
                        live.kill_sent = True
                    try:
                        process.kill()
                    except Exception as exc:  # noqa: BLE001 -- injected process adapter boundary
                        with self._lock:
                            live = self._records.get(key)
                            if live is not None:
                                live.failure = f"{live.failure or 'termination'}; kill failed: {exc}"
                continue
            if not current or not self._current_authority(record):
                self._begin_termination(
                    key,
                    WorkerState.FENCED,
                    "Core authority fence changed",
                    reconcile_attempt=True,
                )
                continue

            attempt = record.attempt_fence
            install = record.install_fence
            if attempt is not None:
                try:
                    attempt_live = self._authority.holds_attempt(
                        record.fence, attempt, record.lifecycle_id
                    )
                except Exception:  # noqa: BLE001 -- fail closed
                    attempt_live = False
                if not attempt_live:
                    if self._defer_termination_for_terminal_ack(
                        key,
                        attempt=attempt,
                        failure="Attempt lease fence changed",
                    ):
                        continue
                    self._begin_termination(
                        key,
                        WorkerState.FENCED,
                        "Attempt lease fence changed",
                        reconcile_attempt=False,
                    )
                    continue
            if install is not None:
                try:
                    install_live = self._authority.holds_install(
                        record.fence, install, record.lifecycle_id
                    )
                except Exception:  # noqa: BLE001 -- fail closed
                    install_live = False
                if not install_live:
                    if self._defer_termination_for_terminal_ack(
                        key,
                        install=install,
                        failure="install lease fence changed",
                    ):
                        continue
                    self._begin_termination(
                        key,
                        WorkerState.FENCED,
                        "install lease fence changed",
                        reconcile_attempt=True,
                    )
                    continue

            with self._lock:
                record = self._records.get(key)
                if record is None:
                    continue
                state = record.state
                if (
                    state == WorkerState.STARTING
                    and now - record.started_at >= self._config.handshake_timeout
                ):
                    action = (WorkerState.FAILED, "runtime.handshake timeout", False)
                elif (
                    state == WorkerState.READY
                    and attempt is not None
                    and now - record.last_heartbeat >= self._config.heartbeat_timeout
                ):
                    action = (WorkerState.FAILED, "runtime.heartbeat timeout", True)
                else:
                    action = None
            if action is not None:
                target, reason, reconcile = action
                if reconcile and attempt is not None:
                    try:
                        interrupted = self._authority.interrupt_attempt(
                            record.fence,
                            attempt,
                            record.lifecycle_id,
                            reason,
                        )
                    except Exception:  # noqa: BLE001 -- exact retry occurs after exit
                        interrupted = False
                    with self._lock:
                        live = self._records.get(key)
                        if live is not None:
                            live.attempt_reconcile_required = True
                            live.attempt_reconciled = interrupted
                            live.reconcile_reason = reason
                self._begin_termination(
                    key, target, reason, reconcile_attempt=reconcile
                )
                continue

            shutdown_frame: bytes | None = None
            with record.rpc_lock, self._lock:
                record = self._records.get(key)
                if record is None:
                    continue
                if (
                    record.state == WorkerState.READY
                    and not self._is_busy(record)
                    and record.idle_since is not None
                    and now - record.idle_since >= self._config.idle_timeout
                ):
                    try:
                        shutdown_frame = record.session.build_shutdown(
                            reason="idle",
                            deadline_at=self._deadline(self._config.shutdown_timeout),
                        )
                        record.state = WorkerState.STOPPING
                        record.stop_deadline = now + self._config.shutdown_timeout
                    except Exception as exc:  # noqa: BLE001 -- session boundary
                        action = (
                            WorkerState.FAILED,
                            f"idle shutdown failed: {exc}",
                            True,
                        )
                    else:
                        action = None
                elif (
                    record.state == WorkerState.STOPPING
                    and record.stop_deadline is not None
                    and now >= record.stop_deadline
                ):
                    action = (WorkerState.STOPPED, "graceful shutdown timeout", False)
                else:
                    action = None
            if shutdown_frame is not None:
                try:
                    process.write(shutdown_frame)
                except Exception as exc:  # noqa: BLE001 -- isolate writer failure
                    self._begin_termination(
                        key,
                        WorkerState.FAILED,
                        f"idle shutdown failed: {exc}",
                        reconcile_attempt=True,
                    )
            elif action is not None:
                self._begin_termination(
                    key, action[0], action[1], reconcile_attempt=action[2]
                )

    def _peek_host_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]:
        """View admitted Host RPCs without dropping a durable operation retry."""

        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if (
                record is None
                or not self._matches_lifecycle(record, ticket)
                or not self._is_current_locked(record)
                or record.state in _TERMINAL
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Host event belongs to an old lifecycle"
                )
            return tuple(
                event for event in record.events if event.kind == "host_request"
            )

    def _dispose_host_event(self, ticket: WorkerTicket, request_id: str) -> None:
        """Drop exactly one Host event after a canonical response is staged."""

        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if (
                record is None
                or not self._matches_lifecycle(record, ticket)
                or not self._is_current_locked(record)
                or record.state not in _PROTOCOL_STATES
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Host event belongs to an old lifecycle"
                )
        with record.rpc_lock, self._lock:
            current = self._records.get(
                self._key(ticket.worker_id, ticket.lifecycle_id)
            )
            if (
                current is not record
                or not self._is_current_locked(record)
                or record.state not in _PROTOCOL_STATES
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Host event raced lifecycle termination"
                )
            if not record.session.has_host_request(request_id):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH, "Host event id was not admitted"
                )
            if not record.session.has_staged_host_response(request_id):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Host event has no staged canonical response",
                )
            for index, event in enumerate(record.events):
                if (
                    event.kind == "host_request"
                    and str(event.message.get("id")) == request_id
                ):
                    record.events.pop(index)
                    return
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH, "Host event is not pending disposition"
        )

    def drain_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]:
        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if record is None or not self._matches_lifecycle(record, ticket):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "event read belongs to an old lifecycle"
                )
            values = tuple(record.events)
            record.events.clear()
            return values

    def respond_host_request(
        self, ticket: WorkerTicket, request_id: str, result: dict[str, object]
    ) -> None:
        """Enqueue the canonical response produced by the durable Host dispatcher."""

        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if (
                record is None
                or not self._matches_lifecycle(record, ticket)
                or not self._is_current_locked(record)
                or record.state in _TERMINAL
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Host response belongs to an old lifecycle"
                )
        if not self._current_authority(record):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Host response belongs to an old lifecycle"
            )
        with record.rpc_lock:
            frame = record.session.build_host_success(request_id, result)
        self._write_host_response_frame(ticket, record, request_id, frame)

    def respond_host_error(
        self,
        ticket: WorkerTicket,
        request_id: str,
        *,
        code: int,
        message: str,
        error_id: str,
        retryable: bool = False,
        details_asset_id: str | None = None,
    ) -> None:
        """Write one validated canonical Host error through the same lifecycle."""

        with self._lock:
            record = self._record_locked(ticket.worker_id, ticket.lifecycle_id)
            if (
                record is None
                or not self._matches_lifecycle(record, ticket)
                or not self._is_current_locked(record)
                or record.state in _TERMINAL
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Host response belongs to an old lifecycle"
                )
        if not self._current_authority(record):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Host response belongs to an old lifecycle"
            )
        with record.rpc_lock:
            frame = record.session.build_host_error(
                request_id,
                code=code,
                message=message,
                error_id=error_id,
                retryable=retryable,
                details_asset_id=details_asset_id,
            )
        self._write_host_response_frame(ticket, record, request_id, frame)

    def _write_host_response_frame(
        self,
        ticket: WorkerTicket,
        record: _Record,
        request_id: str,
        frame: bytes,
    ) -> None:
        with self._lock:
            current = self._records.get(
                self._key(ticket.worker_id, ticket.lifecycle_id)
            )
            if current is not record or record.state in _TERMINAL:
                raise ContractError(
                    ErrorCode.STALE_LEASE, "Host response raced worker termination"
                )
            process = record.process
        # A failed transport write leaves the canonical frame available for
        # exact retry. Capacity overflow is a local lifecycle failure instead
        # of back-pressuring the global supervisor.
        try:
            process.write(
                frame,
                on_written=lambda: self._mark_host_frame_written(
                    record, request_id, frame
                ),
            )
        except BufferError:
            self._begin_termination(
                self._key(ticket.worker_id, ticket.lifecycle_id),
                WorkerState.FAILED,
                "Host response writer capacity exceeded",
                reconcile_attempt=True,
            )
            raise

    def _mark_host_frame_written(
        self, record: _Record, request_id: str, frame: bytes
    ) -> None:
        process: ManagedProcess | None = None
        with self._lock:
            if not self._callbacks_enabled:
                return
        with record.rpc_lock:
            record.session.mark_host_response_written(request_id, frame)
            with self._lock:
                key = self._key(record.fence.worker_id, record.lifecycle_id)
                if (
                    self._records.get(key) is record
                    and record.state == WorkerState.TERMINATING
                    and record.deferred_terminal_ack
                    and not record.session.terminal_commit_write_pending()
                ):
                    record.deferred_terminal_ack = False
                    process = record.process
        if process is not None:
            try:
                process.terminate()
            except Exception as exc:  # noqa: BLE001 -- injected process adapter boundary
                with self._lock:
                    current = self._records.get(
                        self._key(record.fence.worker_id, record.lifecycle_id)
                    )
                    if current is record:
                        record.failure = f"{record.failure or 'termination'}; terminate failed: {exc}"

    def shutdown(self) -> None:
        """Fence every local lifecycle and release each durable claim once.

        New acquires are stopped before in-flight acquires are drained.  Exit
        callbacks remain enabled only until every known claim is released;
        late callbacks after closure therefore cannot mutate supervisor state.
        """

        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            wait_deadline = time.monotonic() + self._config.shutdown_timeout
            with self._acquire_condition:
                self._accepting = False
                while self._active_acquires:
                    remaining = wait_deadline - time.monotonic()
                    if remaining <= 0:
                        raise ContractError(
                            ErrorCode.INVALID_TRANSITION,
                            "supervisor shutdown timed out waiting for acquire",
                        )
                    self._acquire_condition.wait(timeout=remaining)

            with self._lock:
                keys = tuple(self._records)
                for record in self._records.values():
                    record.retains.clear()
            for key in keys:
                with self._lock:
                    record = self._records.get(key)
                if record is None:
                    continue
                reconcile = False
                attempt = record.attempt_fence
                if attempt is not None:
                    try:
                        reconcile = self._authority.holds_attempt(
                            record.fence, attempt, record.lifecycle_id
                        )
                    except Exception:  # noqa: BLE001 -- fail closed at authority boundary
                        reconcile = True
                self._begin_termination(
                    key,
                    WorkerState.FENCED if attempt is not None else WorkerState.STOPPED,
                    "supervisor shutdown",
                    reconcile_attempt=reconcile,
                )

            kill_at = time.monotonic() + self._config.shutdown_timeout
            final_deadline = kill_at + self._config.termination_timeout
            killed: set[tuple[str, str]] = set()
            while True:
                self._retry_pending_releases()
                with self._lock:
                    snapshot = tuple(self._records.items())
                    pending = bool(self._pending_releases)
                for key, record in snapshot:
                    if record.exit_code is None:
                        try:
                            code = record.process.poll()
                        except Exception:  # noqa: BLE001 -- failed observation is not exit proof
                            code = None
                        if code is not None:
                            self._on_exit(
                                record.fence.worker_id, record.lifecycle_id, code
                            )
                    else:
                        self._reconcile_and_release(key)
                with self._lock:
                    unreleased = tuple(
                        (key, record)
                        for key, record in self._records.items()
                        if not record.claim_released
                    )
                    pending = pending or bool(self._pending_releases)
                if not unreleased and not pending:
                    break
                now = time.monotonic()
                if now >= kill_at:
                    for key, record in unreleased:
                        if key in killed:
                            continue
                        killed.add(key)
                        try:
                            record.process.kill()
                        except Exception:  # noqa: BLE001, S110 -- exit proof remains required
                            pass
                if now >= final_deadline:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "supervisor shutdown could not confirm process exit and claim release",
                    )
                time.sleep(0.005)

            with self._lock:
                self._callbacks_enabled = False
                self._records.clear()
                self._current.clear()
                self._pending_releases.clear()
                self._shutdown_complete = True

    close = shutdown

    def status(self, worker_id: str) -> WorkerStatus | None:
        with self._lock:
            record = self._current_record_locked(worker_id)
            if record is None:
                return None
            return WorkerStatus(
                worker_id=worker_id,
                lifecycle_id=record.lifecycle_id,
                state=record.state,
                release_id=record.fence.release_id,
                pin_epoch=record.fence.pin_epoch,
                retire_epoch=record.fence.retire_epoch,
                clients=len(record.retains),
                worker_instance_id=record.worker_instance_id,
                failure=record.failure,
                exit_code=record.exit_code,
                stderr_tail=bytes(record.stderr_tail),
            )


__all__ = ["PluginProcessSupervisor", "SupervisorConfig", "SystemClock"]
