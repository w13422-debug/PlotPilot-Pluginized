from __future__ import annotations

import hashlib
import itertools
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from plotpilot_core.supervisor import (
    AttemptFence,
    ImmutableRuntimeLookup,
    InstallFence,
    PluginProcessSupervisor,
    RuntimeRoute,
    SupervisorConfig,
    WorkerFence,
)
from plotpilot_core.supervisor.venv import InterpreterIdentity

RELEASE_A = "a" * 64
RELEASE_B = "b" * 64
PACKAGE_HASH = "c" * 64
UI_BYTES = b"self.onmessage = () => {};\n"
UI_HASH = hashlib.sha256(UI_BYTES).hexdigest()


@dataclass
class FakeClock:
    value: float = 100.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class FakeAuthority:
    values: dict[str, WorkerFence] = field(default_factory=dict)
    claims: dict[str, tuple[WorkerFence, str]] = field(default_factory=dict)
    releases: list[tuple[WorkerFence, str]] = field(default_factory=list)
    interruptions: list[tuple[WorkerFence, AttemptFence, str, str]] = field(default_factory=list)
    attempts: dict[tuple[str, str], AttemptFence] = field(default_factory=dict)
    installs: dict[tuple[str, str], InstallFence] = field(default_factory=dict)
    release_failures: int = 0
    release_exceptions: int = 0
    interrupt_failures: int = 0
    interrupt_exceptions: int = 0

    def claim(self, worker_id: str, owner_id: str) -> WorkerFence | None:
        fence = self.values.get(worker_id)
        if fence is None or not fence.allowed:
            return None
        self.claims[fence.pin_id] = (fence, owner_id)
        return fence

    def snapshot(self, worker_id: str) -> WorkerFence | None:
        return self.values.get(worker_id)

    def holds(self, fence: WorkerFence, owner_id: str) -> bool:
        return self.claims.get(fence.pin_id) == (fence, owner_id)

    def holds_attempt(self, fence: WorkerFence, attempt: AttemptFence, owner_id: str) -> bool:
        return self.holds(fence, owner_id) and self.attempts.get((fence.pin_id, owner_id)) == attempt

    def holds_install(self, fence: WorkerFence, install: InstallFence, owner_id: str) -> bool:
        return self.holds(fence, owner_id) and self.installs.get((fence.pin_id, owner_id)) == install

    def release(self, fence: WorkerFence, owner_id: str) -> bool:
        self.releases.append((fence, owner_id))
        if self.release_exceptions > 0:
            self.release_exceptions -= 1
            raise RuntimeError("injected release failure")
        if self.release_failures > 0:
            self.release_failures -= 1
            return False
        if self.claims.get(fence.pin_id) != (fence, owner_id):
            return False
        del self.claims[fence.pin_id]
        return True

    def interrupt_attempt(self, fence: WorkerFence, attempt: AttemptFence, owner_id: str, reason: str) -> bool:
        if self.interrupt_exceptions > 0:
            self.interrupt_exceptions -= 1
            raise RuntimeError("injected interrupt failure")
        if self.interrupt_failures > 0:
            self.interrupt_failures -= 1
            return False
        if not self.holds_attempt(fence, attempt, owner_id):
            return False
        self.interruptions.append((fence, attempt, owner_id, reason))
        self.attempts.pop((fence.pin_id, owner_id), None)
        return True


@dataclass
class FakeInterpreterProbe:
    """Return exact target identities without executing fixture byte files."""

    implementation: str = "cpython"
    version: str = "3.12.10"
    results: list[tuple[str, str, Path | None] | Exception] = field(default_factory=list)
    calls: list[Path] = field(default_factory=list)

    def probe(self, python: Path) -> InterpreterIdentity:
        self.calls.append(python)
        value: tuple[str, str, Path | None] | Exception
        if self.results:
            value = self.results.pop(0)
        else:
            value = (self.implementation, self.version, None)
        if isinstance(value, Exception):
            raise value
        implementation, version, executable = value
        return InterpreterIdentity(
            implementation=implementation,
            version=version,
            executable=(executable or python).resolve(strict=True),
        )


@dataclass
class FakeRoutes:
    values: dict[tuple[str, str, str, int], RuntimeRoute] = field(default_factory=dict)

    def get_route(self, worker_id: str, generation_id: str, release_id: str, retire_epoch: int) -> RuntimeRoute | None:
        return self.values.get((worker_id, generation_id, release_id, retire_epoch))


@dataclass
class FakePackage:
    plugin_id: str
    version: str
    package_hash: str
    release_id: str
    manifest: Mapping[str, Any]
    source: Path
    files: dict[str, bytes]

    def read_bytes(self, relative_path: str) -> bytes:
        return self.files[relative_path]


@dataclass
class FakePackageStore:
    package: FakePackage

    def get(self, plugin_id: str, version: str, package_hash: str | None = None) -> FakePackage:
        assert (plugin_id, version, package_hash) == (
            self.package.plugin_id,
            self.package.version,
            self.package.package_hash,
        )
        return self.package


class FakeProcess:
    def __init__(
        self,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
        on_transport_error: Callable[[str], None] | None = None,
    ) -> None:
        self.on_stdout = on_stdout
        self.on_stderr = on_stderr
        self.on_exit = on_exit
        self.on_transport_error = on_transport_error or (lambda message: None)
        self.writes: list[bytes] = []
        self.write_failures = 0
        self.terminations = 0
        self.kills = 0
        self.code: int | None = None

    def write(self, data: bytes, *, on_written: Callable[[], None] | None = None) -> None:
        if self.write_failures > 0:
            self.write_failures -= 1
            raise BrokenPipeError("injected worker write failure")
        self.writes.append(bytes(data))
        if on_written is not None:
            on_written()

    def terminate(self) -> None:
        self.terminations += 1

    def kill(self) -> None:
        self.kills += 1

    def poll(self) -> int | None:
        return self.code

    def emit(self, data: bytes) -> None:
        self.on_stdout(data)

    def emit_stderr(self, data: bytes) -> None:
        self.on_stderr(data)

    def emit_transport_error(self, message: str = "injected transport failure") -> None:
        self.on_transport_error(message)

    def exit(self, code: int) -> None:
        self.code = code
        self.on_exit(code)


@dataclass
class FakeProcessFactory:
    processes: list[FakeProcess] = field(default_factory=list)

    def start(self, route, *, on_stdout, on_stderr, on_exit, on_transport_error) -> FakeProcess:
        del route
        process = FakeProcess(on_stdout, on_stderr, on_exit, on_transport_error)
        self.processes.append(process)
        return process


@dataclass
class Harness:
    supervisor: PluginProcessSupervisor
    authority: FakeAuthority
    routes: FakeRoutes
    packages: FakePackageStore
    processes: FakeProcessFactory
    clock: FakeClock
    fence: WorkerFence
    route: RuntimeRoute
    interpreter_probe: FakeInterpreterProbe


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    package_root = tmp_path / "package"
    package_root.mkdir()
    working_root = tmp_path / "runtime" / "worker-1"
    working_root.mkdir(parents=True)
    private_data_root = tmp_path / "data" / "com.plotpilot.test" / "generations" / "data-generation-1"
    private_data_root.mkdir(parents=True)
    venv_root = tmp_path / "venvs" / "com.plotpilot.test" / PACKAGE_HASH
    python_executable = venv_root / "Scripts" / "python.exe"
    python_executable.parent.mkdir(parents=True)
    python_executable.write_bytes(b"test-python")
    (venv_root / "plotpilot-venv.json").write_text(
        json.dumps(
            {
                "schema": "plotpilot-venv/v1",
                "plugin_id": "com.plotpilot.test",
                "release_id": RELEASE_A,
                "package_hash": PACKAGE_HASH,
                "python_implementation": "cpython",
                "python_version": "3.12.10",
            }
        ),
        encoding="utf-8",
    )
    fence = WorkerFence(
        worker_id="worker-1",
        pin_id="pin-worker-1",
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        data_generation_id="data-generation-1",
        pin_epoch=1,
        retire_epoch=1,
    )
    route = RuntimeRoute(
        worker_id=fence.worker_id,
        plugin_id=fence.plugin_id,
        version="1.0.0",
        generation_id=fence.generation_id,
        release_id=fence.release_id,
        package_hash=PACKAGE_HASH,
        data_generation_id=fence.data_generation_id,
        retire_epoch=fence.retire_epoch,
        package_root=package_root,
        venv_root=venv_root,
        working_root=working_root,
        private_data_root=private_data_root,
        python_executable=python_executable,
        entrypoint="plugin_worker:main",
        ui_entry="ui/worker.js",
        ui_bundle_hash=UI_HASH,
    )
    package = FakePackage(
        plugin_id=route.plugin_id,
        version=route.version,
        package_hash=route.package_hash,
        release_id=route.release_id,
        manifest={
            "backend": {
                "entrypoint": route.entrypoint,
                "wheel": "backend/plugin.whl",
                "requirements_lock": "backend/requirements.lock",
            },
            "ui": {"entry": route.ui_entry, "runtime": "worker-ui/v1", "contributions": []},
            "capabilities": [{"capability_id": "fixture.echo/v1"}],
            "compatibility": {"python": "3.12.*"},
        },
        source=package_root,
        files={
            "backend/plugin.whl": b"wheel",
            "backend/requirements.lock": b"locked",
            "ui/worker.js": UI_BYTES,
        },
    )
    authority = FakeAuthority({fence.worker_id: fence})
    routes = FakeRoutes({(fence.worker_id, fence.generation_id, fence.release_id, fence.retire_epoch): route})
    packages = FakePackageStore(package)
    processes = FakeProcessFactory()
    clock = FakeClock()
    interpreter_probe = FakeInterpreterProbe()
    counter = itertools.count(1)
    retain_counter = itertools.count(1)
    supervisor = PluginProcessSupervisor(
        authority=authority,
        lookup=ImmutableRuntimeLookup(routes, packages, authority, interpreter_probe),
        processes=processes,
        clock=clock,
        config=SupervisorConfig(
            handshake_timeout=5,
            heartbeat_timeout=10,
            idle_timeout=20,
            shutdown_timeout=3,
            termination_timeout=2,
            stderr_limit_bytes=1024,
        ),
        id_factory=lambda: f"00000000-0000-4000-8000-{next(counter):012x}",
        retain_id_factory=lambda: f"retain-{next(retain_counter)}",
        utc_now=lambda: __import__("datetime").datetime(2026, 8, 28, tzinfo=__import__("datetime").timezone.utc),
    )
    return Harness(supervisor, authority, routes, packages, processes, clock, fence, route, interpreter_probe)
