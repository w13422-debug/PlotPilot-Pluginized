from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from plotpilot_plugin_sdk import ContractError

from backend.plotpilot_core.supervisor.models import (
    ResolvedWorkerRoute,
    RuntimeRoute,
    WorkerFence,
)
from backend.plotpilot_core.supervisor.supervisor import (
    PluginProcessSupervisor,
    SupervisorConfig,
)
from backend.plotpilot_plugin_sdk.framing import decode_frame, encode_frame

PLUGIN = "com.plotpilot.shutdown"
GENERATION = "generation-1"
RELEASE = "a" * 64
PACKAGE = "b" * 64


@dataclass(slots=True)
class Authority:
    fence: WorkerFence
    owner: str | None = None
    release_calls: int = 0

    def snapshot(self, worker_id: str) -> WorkerFence | None:
        return self.fence if worker_id == self.fence.worker_id else None

    def claim(self, worker_id: str, owner_id: str) -> WorkerFence | None:
        if worker_id != self.fence.worker_id or self.owner not in {None, owner_id}:
            return None
        self.owner = owner_id
        return self.fence

    def holds(self, fence: WorkerFence, owner_id: str) -> bool:
        return fence == self.fence and self.owner == owner_id

    def release(self, fence: WorkerFence, owner_id: str) -> bool:
        self.release_calls += 1
        if fence != self.fence or self.owner != owner_id:
            return False
        self.owner = None
        return True

    def holds_attempt(self, *args: Any) -> bool:
        del args
        return False

    def holds_install(self, *args: Any) -> bool:
        del args
        return False

    def interrupt_attempt(self, *args: Any) -> bool:
        del args
        return False


@dataclass(slots=True)
class Lookup:
    route: ResolvedWorkerRoute

    def resolve_worker(
        self, fence: WorkerFence, lifecycle_id: str
    ) -> ResolvedWorkerRoute:
        del lifecycle_id
        assert fence.worker_id == self.route.route.worker_id
        assert fence.release_id == self.route.route.release_id
        return self.route


class BlockingProcess:
    def __init__(
        self,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self.on_stdout = on_stdout
        self.on_stderr = on_stderr
        self.on_exit = on_exit
        self.termination_started = threading.Event()
        self.allow_exit = threading.Event()
        self.writes: list[bytes] = []
        self.terminations = 0
        self.kills = 0
        self.code: int | None = None

    def write(
        self, data: bytes, *, on_written: Callable[[], None] | None = None
    ) -> None:
        message = decode_frame(data)
        self.writes.append(data)
        if on_written is not None:
            on_written()
        if message.get("method") == "runtime.handshake":
            self.on_stdout(
                encode_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "plugin_protocol": "1",
                            "plugin_id": PLUGIN,
                            "release_id": RELEASE,
                            "capabilities": ["shutdown.run/v1"],
                            "worker_instance_id": "worker-shutdown-1",
                        },
                    }
                )
            )

    def terminate(self) -> None:
        self.terminations += 1
        self.termination_started.set()
        self.allow_exit.wait(timeout=1)
        self._exit(0)

    def kill(self) -> None:
        self.kills += 1
        self.allow_exit.set()
        self._exit(-9)

    def poll(self) -> int | None:
        return self.code

    def _exit(self, code: int) -> None:
        if self.code is not None:
            return
        self.code = code
        self.on_exit(code)


@dataclass(slots=True)
class Processes:
    process: BlockingProcess | None = None

    def start(
        self,
        route: ResolvedWorkerRoute,
        *,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
        on_transport_error: Callable[[str], None],
    ) -> BlockingProcess:
        del route, on_transport_error
        self.process = BlockingProcess(on_stdout, on_stderr, on_exit)
        return self.process


def _supervisor(tmp_path: Path) -> tuple[PluginProcessSupervisor, Authority, Processes]:
    fence = WorkerFence(
        worker_id=PLUGIN,
        pin_id="pin-1",
        plugin_id=PLUGIN,
        generation_id=GENERATION,
        release_id=RELEASE,
        data_generation_id=None,
        pin_epoch=1,
        retire_epoch=1,
    )
    route = RuntimeRoute(
        worker_id=PLUGIN,
        plugin_id=PLUGIN,
        version="1.0.0",
        generation_id=GENERATION,
        release_id=RELEASE,
        package_hash=PACKAGE,
        data_generation_id=None,
        retire_epoch=1,
        package_root=tmp_path,
        venv_root=tmp_path,
        working_root=tmp_path,
        private_data_root=None,
        python_executable=tmp_path / "python.exe",
        entrypoint="shutdown_worker:main",
        ui_entry=None,
        ui_bundle_hash=None,
    )
    resolved = ResolvedWorkerRoute(
        route=route,
        package_root=tmp_path,
        working_root=tmp_path,
        private_data_root=None,
        python_executable=tmp_path / "python.exe",
        capabilities=("shutdown.run/v1",),
    )
    authority = Authority(fence)
    processes = Processes()
    supervisor = PluginProcessSupervisor(
        authority=authority,
        lookup=Lookup(resolved),  # type: ignore[arg-type]
        processes=processes,
        config=SupervisorConfig(
            handshake_timeout=0.5,
            heartbeat_timeout=0.5,
            idle_timeout=0.5,
            shutdown_timeout=0.5,
            termination_timeout=0.5,
        ),
    )
    return supervisor, authority, processes


def test_j8_shutdown_is_idempotent_race_safe_and_fences_late_callbacks(
    tmp_path: Path,
) -> None:
    supervisor, authority, processes = _supervisor(tmp_path)
    ticket = supervisor.acquire(PLUGIN, expected_release_id=RELEASE)
    assert supervisor.status(PLUGIN) is not None
    assert processes.process is not None

    errors: list[BaseException] = []

    def close() -> None:
        try:
            supervisor.shutdown()
        except RuntimeError as exc:  # pragma: no cover - assertion captures it
            errors.append(exc)

    first = threading.Thread(target=close)
    second = threading.Thread(target=close)
    first.start()
    assert processes.process.termination_started.wait(timeout=1)
    second.start()
    with pytest.raises(ContractError, match="shut down"):
        supervisor.acquire(PLUGIN, expected_release_id=RELEASE)
    processes.process.allow_exit.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert authority.release_calls == 1
    assert authority.owner is None
    assert processes.process.terminations == 1
    assert supervisor.status(PLUGIN) is None
    supervisor.release(ticket)
    supervisor.shutdown()

    processes.process.on_stderr(b"late")
    processes.process.on_exit(99)
    assert supervisor.status(PLUGIN) is None
    assert authority.release_calls == 1
