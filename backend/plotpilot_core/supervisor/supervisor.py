"""Lease- and release-fenced lazy worker lifecycle."""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
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

    def __post_init__(self) -> None:
        for name in ("handshake_timeout", "heartbeat_timeout", "idle_timeout", "shutdown_timeout", "termination_timeout"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if not isinstance(self.stderr_limit_bytes, int) or isinstance(self.stderr_limit_bytes, bool) or self.stderr_limit_bytes < 1024:
            raise ValueError("stderr_limit_bytes must be an integer >= 1024")


@dataclass
class _Record:
    lifecycle_id: str
    fence: WorkerFence
    route: ResolvedWorkerRoute
    process: ManagedProcess
    session: FramedRpcSession
    state: WorkerState
    clients: int
    started_at: float
    last_heartbeat: float
    idle_since: float | None = None
    stop_deadline: float | None = None
    failure: str | None = None
    exit_code: int | None = None
    claim_released: bool = False
    termination_target: WorkerState | None = None
    termination_deadline: float | None = None
    kill_sent: bool = False
    stderr_tail: bytearray = field(default_factory=bytearray)
    events: list[RpcEvent] = field(default_factory=list)


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
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._authority = authority
        self._lookup = lookup
        self._processes = processes
        self._clock = clock or SystemClock()
        self._config = config or SupervisorConfig()
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._records: dict[str, _Record] = {}
        self._pending_releases: list[tuple[WorkerFence, str]] = []
        self._lock = threading.RLock()

    def _deadline(self, seconds: float) -> str:
        value = self._utc_now() + timedelta(seconds=seconds)
        value = value.astimezone(timezone.utc).replace(microsecond=0)
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _ticket(record: _Record) -> WorkerTicket:
        return WorkerTicket(
            worker_id=record.fence.worker_id,
            lifecycle_id=record.lifecycle_id,
            release_id=record.fence.release_id,
            pin_epoch=record.fence.pin_epoch,
            retire_epoch=record.fence.retire_epoch,
        )

    @staticmethod
    def _matches_ticket(record: _Record, ticket: WorkerTicket) -> bool:
        return (
            record.lifecycle_id == ticket.lifecycle_id
            and record.fence.worker_id == ticket.worker_id
            and record.fence.release_id == ticket.release_id
            and record.fence.pin_epoch == ticket.pin_epoch
            and record.fence.retire_epoch == ticket.retire_epoch
        )

    def _begin_termination(self, record: _Record, target: WorkerState, failure: str | None) -> None:
        if record.state == WorkerState.TERMINATING:
            if target == WorkerState.FAILED:
                record.termination_target = target
            if failure:
                record.failure = failure
            return
        record.state = WorkerState.TERMINATING
        record.termination_target = target
        record.failure = failure
        record.stop_deadline = None
        record.termination_deadline = self._clock.monotonic() + self._config.termination_timeout
        try:
            record.process.terminate()
        except Exception as exc:  # noqa: BLE001 -- injected process adapter boundary
            record.failure = f"{failure or 'termination'}; terminate failed: {exc}"

    def _current_authority(self, record: _Record) -> bool:
        return record.fence.same_authority(self._authority.snapshot(record.fence.worker_id)) and self._authority.holds(
            record.fence,
            record.lifecycle_id,
        )

    def _release_claim(self, record: _Record) -> None:
        if record.claim_released:
            return
        try:
            record.claim_released = self._authority.release(record.fence, record.lifecycle_id)
        except Exception:  # noqa: BLE001 -- injected authority adapter boundary
            record.claim_released = False

    def acquire(self, worker_id: str) -> WorkerTicket:
        """Lazy-start or retain the exact worker currently authorized by Core."""

        with self._lock:
            old = self._records.get(worker_id)
            current = self._authority.snapshot(worker_id)
            if old is not None and old.fence.same_authority(current) and old.state in {WorkerState.STARTING, WorkerState.READY}:
                was_idle = old.clients == 0
                old.clients += 1
                old.idle_since = None
                if was_idle and old.state == WorkerState.READY:
                    # Heartbeats are attempt-profile notifications.  An idle
                    # worker is not required to emit them; a new retain starts
                    # a fresh watchdog grace period without fabricating one.
                    old.last_heartbeat = self._clock.monotonic()
                return self._ticket(old)
            if old is not None and old.state not in {WorkerState.STOPPED, WorkerState.CRASHED, WorkerState.FAILED, WorkerState.FENCED}:
                self._begin_termination(old, WorkerState.FENCED, "replaced by a new Core authority fence")

            lifecycle_id = self._id_factory()
            # This is the atomic pin-vs-retirement boundary.  A plain read
            # followed by spawn would permit retirement to win in between.
            fence = self._authority.claim(worker_id, lifecycle_id)
            if fence is None or not fence.allowed:
                raise ContractError(ErrorCode.RELEASE_RETIRING, "Core authority rejected the worker pin")
            try:
                route = self._lookup.resolve_worker(fence, lifecycle_id)
                session = FramedRpcSession(
                    plugin_id=fence.plugin_id,
                    generation_id=fence.generation_id,
                    release_id=fence.release_id,
                    expected_capabilities=route.capabilities,
                    id_factory=self._id_factory,
                    attempt_authority=lambda attempt: self._authority.holds_attempt(
                        fence,
                        attempt,
                        lifecycle_id,
                    ),
                    install_authority=lambda install: self._authority.holds_install(
                        fence,
                        install,
                        lifecycle_id,
                    ),
                )
                handshake = session.begin_handshake(
                    data_generation_id=fence.data_generation_id,
                    deadline_at=self._deadline(self._config.handshake_timeout),
                )
            except Exception:
                self._authority.release(fence, lifecycle_id)
                raise

            callback_lock = threading.Lock()
            pending_callbacks: list[tuple[str, bytes | int]] = []
            callbacks_armed = False

            def dispatch(kind: str, payload: bytes | int) -> None:
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
                else:
                    assert isinstance(payload, int)
                    self._on_exit(worker_id, lifecycle_id, fence, payload)

            def on_stdout(data: bytes) -> None:
                dispatch("stdout", data)

            def on_stderr(data: bytes) -> None:
                dispatch("stderr", data)

            def on_exit(code: int) -> None:
                dispatch("exit", code)

            try:
                process = self._processes.start(
                    route,
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                    on_exit=on_exit,
                )
            except Exception:
                self._authority.release(fence, lifecycle_id)
                raise
            now = self._clock.monotonic()
            record = _Record(
                lifecycle_id=lifecycle_id,
                fence=fence,
                route=route,
                process=process,
                session=session,
                state=WorkerState.STARTING,
                clients=1,
                started_at=now,
                last_heartbeat=now,
            )
            self._records[worker_id] = record
            with callback_lock:
                callbacks_armed = True
                queued = tuple(pending_callbacks)
                pending_callbacks.clear()
            for kind, payload in queued:
                dispatch(kind, payload)
            if record.state in {WorkerState.STOPPED, WorkerState.CRASHED, WorkerState.FAILED, WorkerState.FENCED}:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "worker exited before startup committed")
            try:
                process.write(handshake)
            except Exception as exc:
                self._begin_termination(record, WorkerState.FAILED, f"handshake write failed: {exc}")
                raise
            return self._ticket(record)

    def _on_stdout(self, worker_id: str, lifecycle_id: str, data: bytes) -> None:
        with self._lock:
            record = self._records.get(worker_id)
            if record is None or record.lifecycle_id != lifecycle_id:
                return
            if not self._current_authority(record):
                self._begin_termination(record, WorkerState.FENCED, "Core authority fence changed")
                return
            try:
                events = record.session.feed(data)
            except Exception as exc:  # noqa: BLE001 -- close the process on any transport callback failure
                self._begin_termination(record, WorkerState.FAILED, f"RPC protocol failure: {exc}")
                return
            now = self._clock.monotonic()
            for event in events:
                record.events.append(event)
                if event.kind == "handshake":
                    record.state = WorkerState.READY
                    record.last_heartbeat = now
                elif event.kind == "heartbeat":
                    record.last_heartbeat = now

    def _on_stderr(self, worker_id: str, lifecycle_id: str, data: bytes) -> None:
        with self._lock:
            record = self._records.get(worker_id)
            if record is None or record.lifecycle_id != lifecycle_id:
                return
            record.stderr_tail.extend(data)
            overflow = len(record.stderr_tail) - self._config.stderr_limit_bytes
            if overflow > 0:
                del record.stderr_tail[:overflow]

    def _on_exit(self, worker_id: str, lifecycle_id: str, fence: WorkerFence, code: int) -> None:
        with self._lock:
            record = self._records.get(worker_id)
            if record is None or record.lifecycle_id != lifecycle_id:
                # Replacement callbacks still release only their exact old
                # pin/epoch; Core's CAS makes this a no-op for a new owner.
                try:
                    released = self._authority.release(fence, lifecycle_id)
                except Exception:  # noqa: BLE001 -- injected authority adapter boundary
                    released = False
                if not released and (fence, lifecycle_id) not in self._pending_releases:
                    self._pending_releases.append((fence, lifecycle_id))
                return
            record.exit_code = code
            try:
                record.session.finish()
            except ContractError as exc:
                record.termination_target = WorkerState.FAILED
                record.failure = f"RPC EOF protocol failure: {exc}"
            if record.termination_target is not None:
                record.state = record.termination_target
            elif record.state == WorkerState.STOPPING:
                record.state = WorkerState.STOPPED
                record.failure = None if code == 0 else f"shutdown exited with code {code}"
            else:
                record.state = WorkerState.CRASHED
                record.failure = f"worker exited with code {code}"
            self._release_claim(record)

    def feed_stdout(self, ticket: WorkerTicket, data: bytes) -> None:
        """Injected transport seam used by adapters and deterministic tests."""

        with self._lock:
            record = self._records.get(ticket.worker_id)
            if record is None or not self._matches_ticket(record, ticket):
                raise ContractError(ErrorCode.STALE_LEASE, "late process bytes belong to an old lifecycle")
        self._on_stdout(ticket.worker_id, ticket.lifecycle_id, data)

    def process_exited(self, ticket: WorkerTicket, code: int) -> None:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if record is None or not self._matches_ticket(record, ticket):
                return
        with self._lock:
            record = self._records.get(ticket.worker_id)
            assert record is not None
            fence = record.fence
        self._on_exit(ticket.worker_id, ticket.lifecycle_id, fence, code)

    def release(self, ticket: WorkerTicket) -> None:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if record is None or not self._matches_ticket(record, ticket):
                return
            if record.clients <= 0:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "worker retain count is already zero")
            record.clients -= 1
            if record.clients == 0:
                record.idle_since = self._clock.monotonic()

    def bind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if record is None or not self._matches_ticket(record, ticket) or not self._current_authority(record):
                raise ContractError(ErrorCode.STALE_LEASE, "Attempt binding belongs to an old worker")
            if fence.owner_id != ticket.lifecycle_id:
                raise ContractError(ErrorCode.STALE_LEASE, "Attempt owner is not the current worker connection")
            if not self._authority.holds_attempt(record.fence, fence, record.lifecycle_id):
                raise ContractError(ErrorCode.STALE_LEASE, "Core authority rejected the Attempt fence")
            record.session.bind_attempt(fence)
            record.last_heartbeat = self._clock.monotonic()

    def unbind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if (
                record is None
                or not self._matches_ticket(record, ticket)
                or not self._current_authority(record)
                or not self._authority.holds_attempt(record.fence, fence, record.lifecycle_id)
            ):
                raise ContractError(ErrorCode.STALE_LEASE, "Attempt unbind belongs to an old worker")
            record.session.unbind_attempt(fence)

    def bind_install(self, ticket: WorkerTicket, fence: InstallFence) -> None:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if (
                record is None
                or not self._matches_ticket(record, ticket)
                or not self._current_authority(record)
                or not self._authority.holds_install(record.fence, fence, record.lifecycle_id)
            ):
                raise ContractError(ErrorCode.STALE_LEASE, "install binding belongs to an old worker")
            record.session.bind_install(fence)

    def unbind_install(self, ticket: WorkerTicket, fence: InstallFence) -> None:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if (
                record is None
                or not self._matches_ticket(record, ticket)
                or not self._current_authority(record)
                or not self._authority.holds_install(record.fence, fence, record.lifecycle_id)
            ):
                raise ContractError(ErrorCode.STALE_LEASE, "install unbind belongs to an old worker")
            record.session.unbind_install(fence)

    def tick(self) -> None:
        """Run watchdog/idle transitions without changing Core authority."""

        with self._lock:
            now = self._clock.monotonic()
            pending: list[tuple[WorkerFence, str]] = []
            for fence, owner_id in self._pending_releases:
                try:
                    released = self._authority.release(fence, owner_id)
                except Exception:  # noqa: BLE001 -- injected authority adapter boundary
                    released = False
                if not released:
                    pending.append((fence, owner_id))
            self._pending_releases = pending
            for record in tuple(self._records.values()):
                if record.state in {WorkerState.STOPPED, WorkerState.CRASHED, WorkerState.FAILED, WorkerState.FENCED}:
                    if record.exit_code is not None and not record.claim_released:
                        self._release_claim(record)
                    continue
                if record.state == WorkerState.TERMINATING:
                    code = record.process.poll()
                    if code is None and record.termination_deadline is not None and now >= record.termination_deadline and not record.kill_sent:
                        record.kill_sent = True
                        try:
                            record.process.kill()
                        except Exception as exc:  # noqa: BLE001 -- injected process adapter boundary
                            record.failure = f"{record.failure or 'termination'}; kill failed: {exc}"
                    continue
                if not self._current_authority(record):
                    self._begin_termination(record, WorkerState.FENCED, "Core authority fence changed")
                    continue
                if record.state == WorkerState.STARTING and now - record.started_at >= self._config.handshake_timeout:
                    self._begin_termination(record, WorkerState.FAILED, "runtime.handshake timeout")
                    continue
                if (
                    record.state == WorkerState.READY
                    and record.session.attempt_fence is not None
                    and now - record.last_heartbeat >= self._config.heartbeat_timeout
                ):
                    attempt = record.session.attempt_fence
                    assert attempt is not None
                    if not self._authority.holds_attempt(record.fence, attempt, record.lifecycle_id):
                        self._begin_termination(record, WorkerState.FENCED, "Attempt lease fence changed")
                    else:
                        interrupted = self._authority.interrupt_attempt(
                            record.fence,
                            attempt,
                            record.lifecycle_id,
                            "runtime.heartbeat timeout",
                        )
                        target = WorkerState.FAILED if interrupted else WorkerState.FENCED
                        self._begin_termination(record, target, "runtime.heartbeat timeout")
                    continue
                if (
                    record.state == WorkerState.READY
                    and record.clients == 0
                    and record.idle_since is not None
                    and now - record.idle_since >= self._config.idle_timeout
                ):
                    try:
                        deadline = self._deadline(self._config.shutdown_timeout)
                        record.process.write(record.session.build_shutdown(reason="idle", deadline_at=deadline))
                        record.state = WorkerState.STOPPING
                        record.stop_deadline = now + self._config.shutdown_timeout
                    except Exception as exc:  # noqa: BLE001 -- injected transport/process boundary
                        self._begin_termination(record, WorkerState.FAILED, f"idle shutdown failed: {exc}")
                    continue
                if record.state == WorkerState.STOPPING and record.stop_deadline is not None and now >= record.stop_deadline:
                    self._begin_termination(record, WorkerState.STOPPED, "graceful shutdown timeout")

    def drain_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]:
        with self._lock:
            record = self._records.get(ticket.worker_id)
            if record is None or not self._matches_ticket(record, ticket):
                raise ContractError(ErrorCode.STALE_LEASE, "event read belongs to an old lifecycle")
            values = tuple(record.events)
            record.events.clear()
            return values

    def respond_host_request(self, ticket: WorkerTicket, request_id: str, result: dict[str, object]) -> None:
        """Return a verifier-bound success response from the injected Host dispatcher."""

        with self._lock:
            record = self._records.get(ticket.worker_id)
            if record is None or not self._matches_ticket(record, ticket) or not self._current_authority(record):
                raise ContractError(ErrorCode.STALE_LEASE, "Host response belongs to an old lifecycle")
            frame = record.session.build_host_success(request_id, result)
            record.process.write(frame)

    def status(self, worker_id: str) -> WorkerStatus | None:
        with self._lock:
            record = self._records.get(worker_id)
            if record is None:
                return None
            return WorkerStatus(
                worker_id=worker_id,
                lifecycle_id=record.lifecycle_id,
                state=record.state,
                release_id=record.fence.release_id,
                pin_epoch=record.fence.pin_epoch,
                retire_epoch=record.fence.retire_epoch,
                clients=record.clients,
                worker_instance_id=record.session.worker_instance_id,
                failure=record.failure,
                exit_code=record.exit_code,
                stderr_tail=bytes(record.stderr_tail),
            )


__all__ = ["PluginProcessSupervisor", "SupervisorConfig", "SystemClock"]
