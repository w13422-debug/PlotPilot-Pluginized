"""Internal, closed supervisor identities and injectable process ports.

These types deliberately are not public wire contracts.  P0 remains the owner of
route composition; the supervisor only consumes an immutable route snapshot and
the current Core authority fence.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} is not a v1 ID")


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} is not lowercase SHA-256")


@dataclass(frozen=True)
class WorkerFence:
    """Current Core authority for one logical worker.

    The tuple ``release_id, pin_epoch, retire_epoch`` is the complete local
    process fence.  The supervisor never increments or persists any component.
    """

    worker_id: str
    pin_id: str
    plugin_id: str
    generation_id: str
    release_id: str
    data_generation_id: str | None
    pin_epoch: int
    retire_epoch: int
    allowed: bool = True

    def __post_init__(self) -> None:
        for value, label in (
            (self.worker_id, "worker_id"),
            (self.pin_id, "pin_id"),
            (self.plugin_id, "plugin_id"),
            (self.generation_id, "generation_id"),
        ):
            _require_id(value, label)
        _require_hash(self.release_id, "release_id")
        if self.data_generation_id is not None:
            _require_id(self.data_generation_id, "data_generation_id")
        for value, label in ((self.pin_epoch, "pin_epoch"), (self.retire_epoch, "retire_epoch")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} must be a positive integer")

    @property
    def token(self) -> tuple[str, int, int]:
        return self.release_id, self.pin_epoch, self.retire_epoch

    def same_authority(self, other: WorkerFence | None) -> bool:
        return other is not None and other.allowed and self == other


@dataclass(frozen=True)
class RuntimeRoute:
    """Immutable P0-composed route consumed by this slice."""

    worker_id: str
    plugin_id: str
    version: str
    generation_id: str
    release_id: str
    package_hash: str
    data_generation_id: str | None
    retire_epoch: int
    package_root: Path
    venv_root: Path
    working_root: Path
    private_data_root: Path | None
    python_executable: Path
    entrypoint: str
    ui_entry: str | None
    ui_bundle_hash: str | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.worker_id, "worker_id"),
            (self.plugin_id, "plugin_id"),
            (self.generation_id, "generation_id"),
        ):
            _require_id(value, label)
        if not isinstance(self.version, str) or not self.version:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "version must be non-empty")
        _require_hash(self.release_id, "release_id")
        _require_hash(self.package_hash, "package_hash")
        if self.data_generation_id is not None:
            _require_id(self.data_generation_id, "data_generation_id")
        if (self.data_generation_id is None) != (self.private_data_root is None):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "private_data_root must exist exactly when data_generation_id exists",
            )
        if not isinstance(self.retire_epoch, int) or isinstance(self.retire_epoch, bool) or self.retire_epoch < 1:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "retire_epoch must be positive")
        if _ENTRYPOINT.fullmatch(self.entrypoint) is None:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "entrypoint must be module:function")
        if (self.ui_entry is None) != (self.ui_bundle_hash is None):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "UI entry and bundle hash must be present together")
        if self.ui_bundle_hash is not None:
            _require_hash(self.ui_bundle_hash, "ui_bundle_hash")


@dataclass(frozen=True)
class ResolvedWorkerRoute:
    route: RuntimeRoute
    package_root: Path
    working_root: Path
    private_data_root: Path | None
    python_executable: Path
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class UiBundle:
    release_id: str
    bundle_hash: str
    route_path: str
    content: bytes
    media_type: str = "text/javascript; charset=utf-8"
    content_security_policy: str = (
        "default-src 'none'; connect-src 'none'; script-src 'none'; "
        "worker-src 'none'; object-src 'none'; base-uri 'none'"
    )


@dataclass(frozen=True)
class AttemptFence:
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    owner_id: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.job_id, "job_id"),
            (self.step_id, "step_id"),
            (self.attempt_id, "attempt_id"),
            (self.owner_id, "attempt owner_id"),
        ):
            _require_id(value, label)
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "attempt lease_epoch must be positive")


@dataclass(frozen=True)
class InstallFence:
    install_operation_id: str
    install_lease_epoch: int
    owner_instance_id: str

    def __post_init__(self) -> None:
        _require_id(self.install_operation_id, "install_operation_id")
        _require_id(self.owner_instance_id, "install owner_instance_id")
        if (
            not isinstance(self.install_lease_epoch, int)
            or isinstance(self.install_lease_epoch, bool)
            or self.install_lease_epoch < 1
        ):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "install lease epoch must be positive")


class SupervisorAuthority(Protocol):
    def claim(self, worker_id: str, owner_id: str) -> WorkerFence | None: ...
    def snapshot(self, worker_id: str) -> WorkerFence | None: ...
    def holds(self, fence: WorkerFence, owner_id: str) -> bool: ...
    def holds_attempt(self, fence: WorkerFence, attempt: AttemptFence, owner_id: str) -> bool: ...
    def holds_install(self, fence: WorkerFence, install: InstallFence, owner_id: str) -> bool: ...
    def release(self, fence: WorkerFence, owner_id: str) -> bool: ...
    def interrupt_attempt(
        self,
        fence: WorkerFence,
        attempt: AttemptFence,
        owner_id: str,
        reason: str,
    ) -> bool: ...


class ImmutableRouteSource(Protocol):
    def get_route(
        self,
        worker_id: str,
        generation_id: str,
        release_id: str,
        retire_epoch: int,
    ) -> RuntimeRoute | None: ...


class ManagedProcess(Protocol):
    def write(self, data: bytes, *, on_written: Callable[[], None] | None = None) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def poll(self) -> int | None: ...


class ProcessFactory(Protocol):
    def start(
        self,
        route: ResolvedWorkerRoute,
        *,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
        on_transport_error: Callable[[str], None],
    ) -> ManagedProcess: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...


class WorkerState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    TERMINATING = "terminating"
    STOPPED = "stopped"
    CRASHED = "crashed"
    FAILED = "failed"
    FENCED = "fenced"


@dataclass(frozen=True)
class WorkerTicket:
    worker_id: str
    lifecycle_id: str
    retain_id: str
    release_id: str
    pin_epoch: int
    retire_epoch: int


@dataclass(frozen=True)
class WorkerStatus:
    worker_id: str
    lifecycle_id: str
    state: WorkerState
    release_id: str
    pin_epoch: int
    retire_epoch: int
    clients: int
    worker_instance_id: str | None
    failure: str | None
    exit_code: int | None
    stderr_tail: bytes


__all__ = [
    "AttemptFence",
    "Clock",
    "ImmutableRouteSource",
    "InstallFence",
    "ManagedProcess",
    "ProcessFactory",
    "ResolvedWorkerRoute",
    "RuntimeRoute",
    "SupervisorAuthority",
    "UiBundle",
    "WorkerFence",
    "WorkerState",
    "WorkerStatus",
    "WorkerTicket",
]
