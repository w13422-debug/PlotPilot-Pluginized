from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.production_job_runtime import (
    ProductionJobRuntime,
    build_production_job_runtime,
)
from backend.plotpilot_core.bootstrap.production_plugin_runtime import (
    ProductionPluginRuntime,
    build_production_plugin_runtime,
)
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.supervisor.venv import InterpreterIdentity
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    sha256_hex,
)
from backend.plotpilot_plugin_sdk.framing import decode_frame, encode_frame
from backend.plotpilot_plugin_sdk.package import build_files_sha256
from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    request_key,
    snapshot_hash,
)

PLUGIN_ID = "com.plotpilot.synthetic"
CAPABILITY_ID = "synthetic.run/v1"
GENERATION_ID = "generation-1"
NOW = "2026-09-09T00:00:00Z"
_USE_STACK_DATA_GENERATION = object()


@dataclass(slots=True)
class Probe:
    def probe(self, python: Path) -> InterpreterIdentity:
        return InterpreterIdentity("cpython", "3.12.10", python.resolve(strict=True))


class AutoProcess:
    def __init__(
        self,
        capabilities: tuple[str, ...],
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
        on_transport_error: Callable[[str], None],
        factory: AutoProcessFactory,
    ) -> None:
        self.capabilities = capabilities
        self.on_stdout = on_stdout
        self.on_stderr = on_stderr
        self.on_exit = on_exit
        self.on_transport_error = on_transport_error
        self.factory = factory
        self.writes: list[bytes] = []
        self.code: int | None = None
        self.terminations = 0
        self.kills = 0

    def write(
        self, data: bytes, *, on_written: Callable[[], None] | None = None
    ) -> None:
        message = decode_frame(data)
        if message.get("method") in self.factory.fail_methods:
            self.factory.fail_methods.remove(str(message["method"]))
            raise BrokenPipeError("injected process write failure")
        self.writes.append(bytes(data))
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
                            "plugin_id": PLUGIN_ID,
                            "release_id": message["meta"]["plugin_release_id"],
                            "capabilities": list(self.capabilities),
                            "worker_instance_id": "worker-instance-1",
                        },
                    }
                )
            )
        elif message.get("method") == "job.cancel":
            self.on_stdout(
                encode_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": dict(self.factory.cancel_result),
                    }
                )
            )

    def terminate(self) -> None:
        self.terminations += 1
        self.exit(0)

    def kill(self) -> None:
        self.kills += 1
        self.exit(-9)

    def poll(self) -> int | None:
        return self.code

    def exit(self, code: int) -> None:
        if self.code is not None:
            return
        self.code = code
        self.on_exit(code)

    def emit(self, message: dict[str, Any]) -> None:
        self.on_stdout(encode_frame(message))


@dataclass(slots=True)
class AutoProcessFactory:
    processes: list[AutoProcess] = field(default_factory=list)
    fail_methods: set[str] = field(default_factory=set)
    cancel_result: dict[str, Any] = field(
        default_factory=lambda: {
            "accepted": True,
            "terminal_known": True,
            "attempt_state": "cancelled",
        }
    )

    @property
    def starts(self) -> int:
        return len(self.processes)

    def start(
        self,
        route: Any,
        *,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
        on_exit: Callable[[int], None],
        on_transport_error: Callable[[str], None],
    ) -> AutoProcess:
        process = AutoProcess(
            tuple(route.capabilities),
            on_stdout,
            on_stderr,
            on_exit,
            on_transport_error,
            self,
        )
        self.processes.append(process)
        return process


@dataclass(slots=True)
class Stack:
    root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    plugin: ProductionPluginRuntime
    processes: AutoProcessFactory
    data_generation_id: str | None = None
    package: Any | None = None
    runtime: ProductionJobRuntime | None = None

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.shutdown()
        self.repository.close()


def _short_root(tmp_path: Path, suffix: str) -> Path:
    token = hashlib.sha256(f"{tmp_path}:{suffix}".encode()).hexdigest()[:10]
    return tmp_path.parent / f"wu2b-{token}"


def _package_files(version: str = "1.0.0") -> dict[str, bytes]:
    wheel = f"backend/synthetic-{version}-py3-none-any.whl"
    manifest = {
        "schema": "plotpilot-plugin/v1",
        "plugin_id": PLUGIN_ID,
        "version": version,
        "display_name": "Synthetic Production Worker",
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
            "python": "3.12.*",
        },
        "capabilities": [
            {
                "capability_id": CAPABILITY_ID,
                "operations": ["run"],
                "result_contract": "artifact-bundle/v1",
            }
        ],
        "settings": None,
        "needs": [],
        "kind": "code",
        "backend": {
            "entrypoint": "synthetic_worker:main",
            "wheel": wheel,
            "requirements_lock": "backend/requirements.lock",
            "wheelhouse": "backend/wheelhouse",
            "max_concurrency": 1,
        },
        "storage": {
            "schema_version": 1,
            "migration_policy": "transactional-shadow",
            "migration_manifest": "migrations/manifest.json",
        },
        "ui": None,
        "data": None,
    }
    ordinary = {
        "plugin.json": canonical_bytes(manifest) + b"\n",
        wheel: f"synthetic-wheel-{version}".encode(),
        "backend/requirements.lock": b"",
        "backend/wheelhouse/dependency-1.0.0-py3-none-any.whl": b"dependency-wheel",
        "migrations/manifest.json": b"{}\n",
    }
    return {**ordinary, "files.sha256": build_files_sha256(ordinary)}


def _prepare_venv(root: Path, package: Any) -> None:
    venv = root / "plugins" / "venvs" / PLUGIN_ID / package.package_hash
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"fixture-python")
    (venv / "plotpilot-venv.json").write_text(
        json.dumps(
            {
                "schema": "plotpilot-venv/v1",
                "plugin_id": PLUGIN_ID,
                "release_id": package.release_id,
                "package_hash": package.package_hash,
                "python_implementation": "cpython",
                "python_version": "3.12.10",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _open_stack(
    tmp_path: Path,
    suffix: str,
    *,
    install: bool = True,
    prepare_venv: bool = True,
    compose: bool = True,
    data_generation_id: str | None = None,
) -> Stack:
    root = _short_root(tmp_path, suffix)
    (root / "core").mkdir(parents=True, exist_ok=True)
    repository = CoreAuthorityRepository(root / "core" / "core.db")
    assets = AssetStore(root / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel"))
    plugin = build_production_plugin_runtime(
        SimpleNamespace(repository=repository, assets=assets),
        data_root=root,
        interpreter_probe=Probe(),
    )
    processes = AutoProcessFactory()
    plugin.process_factory = processes  # type: ignore[assignment]
    plugin.supervisor._processes = processes
    package = None
    if install:
        package = plugin.packages.publish(_package_files())
        plugin.retirement.register_installed(package.release_id, at=NOW)
        generation = {
            "schema": "plugin-generation/v1",
            "generation_id": GENERATION_ID,
            "core_api_version": "1.0.0",
            "members": [
                {
                    "plugin_id": PLUGIN_ID,
                    "release_id": package.release_id,
                    "package_hash": package.package_hash,
                    "data_generation_id": data_generation_id,
                    "ui_bundle_hash": None,
                    "global_settings_revision_id": None,
                    "settings_schema_hash": None,
                    "data_bundle_asset_id": None,
                }
            ],
            "created_reason": "WU-2B regression",
            "created_at": NOW,
            "health_result_asset_id": "health-generation-1",
            "parent_generation_id": None,
            "base_generation_id": None,
        }
        plugin.lifecycle.put_generation(generation)
        with plugin.lifecycle.transaction() as connection:
            connection.execute(
                "UPDATE p2_plugin_generation_pointer SET current_generation_id=?,"
                "safe_mode=0,revision=revision+1 WHERE singleton=1",
                (GENERATION_ID,),
            )
        if prepare_venv:
            _prepare_venv(root, package)
        if data_generation_id is not None:
            private_data = (
                root
                / "plugins"
                / "data"
                / PLUGIN_ID
                / "generations"
                / data_generation_id
            )
            private_data.mkdir(parents=True, exist_ok=True)
            (private_data / "plugin.db").write_bytes(b"")
    stack = Stack(
        root,
        repository,
        assets,
        plugin,
        processes,
        data_generation_id,
        package,
    )
    if compose:
        stack.runtime = build_production_job_runtime(plugin)
    return stack


def _snapshot(
    stack: Stack,
    *,
    snapshot_id: str = "snapshot-1",
    run_intent_id: str = "intent-1",
    package_hash: str | None = None,
    data_generation_id: str | None | object = _USE_STACK_DATA_GENERATION,
) -> tuple[dict[str, Any], str]:
    release_id = "a" * 64 if stack.package is None else stack.package.release_id
    actual_package = "b" * 64 if stack.package is None else stack.package.package_hash
    snapshot_data_generation = (
        stack.data_generation_id
        if data_generation_id is _USE_STACK_DATA_GENERATION
        else data_generation_id
    )
    value: dict[str, Any] = {
        "schema": "run-snapshot/v1",
        "snapshot_id": snapshot_id,
        "core_contract_version": "1.2.0",
        "workspace_id": "ws-1",
        "scope": {
            "document_id": None,
            "node_id": None,
            "operation": CAPABILITY_ID,
        },
        "input_revisions": [],
        "plan_revision_id": "plan-1",
        "plugin_releases": [
            {
                "plugin_id": PLUGIN_ID,
                "release_id": release_id,
                "package_hash": package_hash or actual_package,
                "data_generation_id": snapshot_data_generation,
            }
        ],
        "plugin_settings_revisions": [],
        "data_bindings": [],
        "skill_releases": [],
        "model_profile_revision_id": None,
        "parameters_asset_id": None,
        "asset_hashes": [],
        "request_key": "0" * 64,
        "run_intent_id": run_intent_id,
        "created_at": NOW,
        "snapshot_hash": "0" * 64,
    }
    value["request_key"] = request_key(value)
    value["snapshot_hash"] = snapshot_hash(value)
    asset = stack.assets.put(
        canonical_bytes(value),
        mime="application/json",
        logical_role="run-snapshot",
        provenance="test:wu2b",
    )
    return value, asset.asset_id


def _start_command(asset_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "schema": "job-start-command/v2",
        "operation_key": "start-op-1",
        "workspace_id": "ws-1",
        "job_id": "job-1",
        "capability_id": CAPABILITY_ID,
        "run_snapshot_asset_id": asset_id,
        "writer_epoch": 1,
        **changes,
    }


def _control(command: str, revision: int, **changes: Any) -> dict[str, Any]:
    return {
        "schema": "job-control-command/v2",
        "operation_key": f"{command}-op-1",
        "workspace_id": "ws-1",
        "job_id": "job-1",
        "expected_job_revision": revision,
        "reason": f"operator requested {command}",
        "command": command,
        "resume_intent_id": "resume-1" if command == "resume" else None,
        **changes,
    }


def _rows(stack: Stack) -> tuple[int, int, int]:
    with stack.repository.read_connection() as connection:
        return tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("execution_job", "execution_step", "execution_attempt")
        )  # type: ignore[return-value]


def test_j1_empty_root_is_disabled_with_shared_graph_and_zero_process(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j1", install=False)
    try:
        assert stack.runtime is not None
        _, asset_id = _snapshot(stack)
        status, body = stack.runtime.http.handle(
            "job.start",
            _start_command(asset_id),
            path_identity={"workspace_id": "ws-1"},
        )
        assert status == 409
        assert body["error_code"] == "invalid_transition"
        assert "Disabled" in body["message"]
        assert stack.processes.starts == 0
        assert stack.runtime.repository is stack.repository
        assert stack.runtime.assets is stack.assets
        assert stack.runtime.execution is stack.plugin.execution
        assert stack.runtime.supervisor is stack.plugin.supervisor
    finally:
        stack.close()


@pytest.mark.parametrize("data_generation_id", [None, "data-generation-1"])
def test_f1_route_data_generation_identity_matches_real_worker_profile(
    tmp_path: Path, data_generation_id: str | None
) -> None:
    stack = _open_stack(
        tmp_path,
        f"f1-{data_generation_id or 'null'}",
        data_generation_id=data_generation_id,
    )
    try:
        assert stack.runtime is not None
        snapshot, asset_id = _snapshot(stack)
        status, _body = stack.runtime.http.handle(
            "job.start", _start_command(asset_id)
        )
        assert status == 201
        handshake = decode_frame(stack.processes.processes[0].writes[0])
        assert handshake["method"] == "runtime.handshake"
        assert handshake["params"]["data_generation_id"] == data_generation_id
        assert (
            snapshot["plugin_releases"][0]["data_generation_id"]
            == handshake["params"]["data_generation_id"]
        )
        if data_generation_id is not None:
            assert data_generation_id != handshake["params"]["generation_id"]
        selected = stack.runtime.capability_runtime.select(CAPABILITY_ID)
        assert selected.data_generation_id == data_generation_id
    finally:
        stack.close()

    mismatch = _open_stack(
        tmp_path,
        f"f1-mismatch-{data_generation_id or 'null'}",
        data_generation_id=data_generation_id,
    )
    try:
        assert mismatch.runtime is not None
        wrong = "data-generation-other"
        if data_generation_id == wrong:
            wrong = "data-generation-third"
        _, asset_id = _snapshot(mismatch, data_generation_id=wrong)
        status, body = mismatch.runtime.http.handle(
            "job.start", _start_command(asset_id)
        )
        assert (status, body["error_code"]) == (409, "invalid_transition")
        assert "Generation binding is stale" in body["message"]
        assert mismatch.processes.starts == 0
        assert _rows(mismatch) == (0, 0, 0)
    finally:
        mismatch.close()


def test_j2_verified_start_is_one_atomic_plan_attempt_and_exact_worker(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j2")
    try:
        assert stack.runtime is not None and stack.package is not None
        snapshot, asset_id = _snapshot(stack)
        status, body = stack.runtime.http.handle(
            "job.start",
            _start_command(asset_id),
            path_identity={"workspace_id": "ws-1"},
        )
        assert (status, body["state"], body["job_revision"]) == (201, "running", 2)
        assert _rows(stack) == (1, 1, 1)
        with stack.repository.read_connection() as connection:
            attempt = connection.execute("SELECT * FROM execution_attempt").fetchone()
            receipt = connection.execute(
                "SELECT * FROM execution_job_start_receipt"
            ).fetchone()
        assert attempt is not None and receipt is not None
        assert (
            attempt["release_id"],
            attempt["package_hash"],
            attempt["generation_id"],
            attempt["lease_epoch"],
        ) == (
            stack.package.release_id,
            stack.package.package_hash,
            GENERATION_ID,
            1,
        )
        assert receipt["state"] == "completed"
        assert receipt["run_snapshot_hash"] == snapshot["snapshot_hash"]
        requests = [
            decode_frame(frame)
            for frame in stack.processes.processes[0].writes
            if decode_frame(frame).get("method") == "job.start"
        ]
        assert len(requests) == 1
        assert requests[0]["meta"]["plugin_release_id"] == stack.package.release_id
        assert requests[0]["params"]["run_snapshot_asset_id"] == asset_id
    finally:
        stack.close()


def test_j3_replay_drift_and_uncertain_start_never_duplicate_process(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j3")
    try:
        assert stack.runtime is not None
        _, asset_id = _snapshot(stack)
        command = _start_command(asset_id)
        first = stack.runtime.http.handle("job.start", command)
        replay = stack.runtime.http.handle("job.start", command)
        assert first[0] == replay[0] == 201
        assert replay == first
        assert _rows(stack) == (1, 1, 1)
        assert stack.processes.starts == 1
        drift_status, drift = stack.runtime.http.handle(
            "job.start", {**command, "writer_epoch": 2}
        )
        assert (drift_status, drift["error_code"]) == (409, "duplicate_operation")
        assert stack.processes.starts == 1

        stack.runtime.shutdown()
        restarted_plugin = build_production_plugin_runtime(
            SimpleNamespace(repository=stack.repository, assets=stack.assets),
            data_root=stack.root,
            interpreter_probe=Probe(),
        )
        restarted_processes = AutoProcessFactory()
        restarted_plugin.process_factory = restarted_processes  # type: ignore[assignment]
        restarted_plugin.supervisor._processes = restarted_processes
        stack.plugin = restarted_plugin
        stack.processes = restarted_processes
        stack.runtime = build_production_job_runtime(restarted_plugin)
        restarted_replay = stack.runtime.http.handle("job.start", command)
        assert restarted_replay[0] == 201
        assert restarted_replay == first
        assert stack.processes.starts == 0
        assert _rows(stack) == (1, 1, 1)
    finally:
        stack.close()

    uncertain = _open_stack(tmp_path, "j3-uncertain")
    try:
        assert uncertain.runtime is not None
        _, asset_id = _snapshot(uncertain)
        uncertain.processes.fail_methods.add("job.start")
        command = _start_command(asset_id)
        first_status, first_error = uncertain.runtime.http.handle(
            "job.start", command
        )
        starts = uncertain.processes.starts
        replay_status, replay_error = uncertain.runtime.http.handle(
            "job.start", command
        )
        assert first_status >= 400 and first_error["error_code"]
        assert (replay_status, replay_error["error_code"]) == (
            409,
            "invalid_transition",
        )
        assert "uncertain" in replay_error["message"]
        assert uncertain.processes.starts == starts == 1
        assert _rows(uncertain) == (1, 1, 1)
        with uncertain.repository.read_connection() as connection:
            old_receipt = dict(
                connection.execute(
                    "SELECT * FROM execution_job_start_receipt "
                    "WHERE operation_key='start-op-1'"
                ).fetchone()
            )
        _, fresh_asset_id = _snapshot(
            uncertain,
            snapshot_id="snapshot-2",
            run_intent_id="intent-2",
        )
        fresh_command = _start_command(
            fresh_asset_id,
            operation_key="start-op-2",
            job_id="job-2",
        )
        fresh_status, fresh = uncertain.runtime.http.handle(
            "job.start", fresh_command
        )
        assert (fresh_status, fresh["job_id"], fresh["idempotent"]) == (
            201,
            "job-2",
            False,
        )
        assert uncertain.processes.starts == starts + 1
        assert _rows(uncertain) == (2, 2, 2)
        with uncertain.repository.read_connection() as connection:
            preserved = dict(
                connection.execute(
                    "SELECT * FROM execution_job_start_receipt "
                    "WHERE operation_key='start-op-1'"
                ).fetchone()
            )
        assert preserved == old_receipt
    finally:
        uncertain.close()


def test_j4_control_cas_checkpoint_lineage_replay_and_terminal_cancel(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j4")
    try:
        assert stack.runtime is not None
        snapshot, asset_id = _snapshot(stack)
        first_start = stack.runtime.http.handle("job.start", _start_command(asset_id))
        assert first_start[0] == 201
        with stack.repository.read_connection() as connection:
            attempt = dict(connection.execute("SELECT * FROM execution_attempt").fetchone())
        checkpoint = {
            "schema": "checkpoint/v1",
            "checkpoint_id": "checkpoint-1",
            "checkpoint_seq": 1,
            "job_id": "job-1",
            "step_id": attempt["step_id"],
            "source_attempt_id": attempt["attempt_id"],
            "lease_epoch": 1,
            "run_snapshot_hash": snapshot["snapshot_hash"],
            "replay_policy": "checkpoint_resume",
            "completed_units": 1,
            "total_units": 2,
            "unit_set_hash": "c" * 64,
            "state_asset_id": None,
            "created_at": NOW,
        }
        checkpoint["checkpoint_hash"] = hash_without_field(
            checkpoint, "checkpoint_hash", "checkpoint/v1"
        )
        checkpoint_asset = stack.assets.put(
            canonical_bytes(checkpoint),
            mime="application/json",
            logical_role="checkpoint",
            provenance="test:wu2b",
        )
        stack.plugin.execution.checkpoint_store.commit_checkpoint(
            checkpoint_asset.asset_id,
            operation_key="checkpoint-op-1",
            worker_run_id=attempt["worker_run_id"],
        )
        stale_status, stale = stack.runtime.http.handle(
            "job.pause", _control("pause", 1)
        )
        assert (stale_status, stale["error_code"]) == (409, "stale_revision")
        pause = stack.runtime.http.handle("job.pause", _control("pause", 2))
        pause_replay = stack.runtime.http.handle("job.pause", _control("pause", 2))
        assert pause[0] == pause_replay[0] == 200
        assert pause_replay == pause
        stack.runtime.pump.run_once()
        resume = stack.runtime.http.handle("job.resume", _control("resume", 3))
        assert (resume[0], resume[1]["state"], resume[1]["job_revision"]) == (
            200,
            "running",
            4,
        )
        with stack.repository.read_connection() as connection:
            attempts = connection.execute(
                "SELECT attempt_id,resume_of_attempt_id,lease_epoch,worker_run_id FROM "
                "execution_attempt ORDER BY ordinal"
            ).fetchall()
        assert len(attempts) == 2
        assert attempts[1]["resume_of_attempt_id"] == attempts[0]["attempt_id"]
        assert attempts[1]["lease_epoch"] == 2
        assert stack.runtime.http.handle(
            "job.pause", _control("pause", 2)
        ) == pause
        assert stack.runtime.http.handle(
            "job.start", _start_command(asset_id)
        ) == first_start
        cancel = stack.runtime.http.handle("job.cancel", _control("cancel", 4))
        cancel_replay = stack.runtime.http.handle("job.cancel", _control("cancel", 4))
        assert cancel[0] == cancel_replay[0] == 200
        assert cancel_replay == cancel
        cancel_requests = [
            decode_frame(frame)
            for process in stack.processes.processes
            for frame in process.writes
            if decode_frame(frame).get("method") == "job.cancel"
        ]
        assert len(cancel_requests) == 1
        assert cancel_requests[0]["params"] == {
            "worker_run_id": attempts[1]["worker_run_id"],
            "reason": "operator requested cancel",
        }
        assert stack.runtime.attempt_registry.get(attempts[1]["attempt_id"]) is not None
        with stack.repository.read_connection() as connection:
            cancelling = connection.execute(
                "SELECT a.state,j.job_state FROM execution_attempt a "
                "JOIN execution_job j ON j.job_id=a.job_id WHERE a.attempt_id=?",
                (attempts[1]["attempt_id"],),
            ).fetchone()
        assert tuple(cancelling) == ("cancelling", "cancelling")
        stack.runtime.pump.run_once()
        with stack.repository.read_connection() as connection:
            terminal = connection.execute(
                "SELECT a.state,s.state AS step_state,j.job_state "
                "FROM execution_attempt a JOIN execution_step s "
                "ON s.step_id=a.step_id JOIN execution_job j ON j.job_id=a.job_id "
                "WHERE a.attempt_id=?",
                (attempts[1]["attempt_id"],),
            ).fetchone()
        assert tuple(terminal) == ("cancelled", "cancelled", "cancelled")
        assert stack.runtime.attempt_registry.get(attempts[1]["attempt_id"]) is None
        assert stack.runtime.http.handle(
            "job.cancel", _control("cancel", 4)
        ) == cancel
        assert sum(
            decode_frame(frame).get("method") == "job.cancel"
            for process in stack.processes.processes
            for frame in process.writes
        ) == 1
        with stack.repository.read_connection() as connection:
            stored_requests = {
                str(row["operation"]): json.loads(str(row["request_json"]))
                for row in connection.execute(
                    "SELECT operation,request_json FROM execution_control_operation"
                ).fetchall()
            }
        assert {
            operation: request["expected_job_revision"]
            for operation, request in stored_requests.items()
        } == {"pause": 2, "resume": 3, "cancel": 4}
    finally:
        stack.close()


def test_j5_broker_uses_exact_durable_ports_and_rejects_generation_drift(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j5")
    try:
        assert stack.runtime is not None
        broker = stack.runtime.capability_broker
        authority = stack.plugin.execution
        assert broker.core is stack.assets
        assert broker.execution is authority
        assert broker.child_factory is authority
        assert broker.operation_ledger is authority.operation_ledger
        assert broker.child_records is authority.child_records
        assert broker.attempt_context is authority
        assert broker.receipt_port is authority
        with stack.plugin.lifecycle.transaction() as connection:
            connection.execute(
                "UPDATE p2_plugin_generation_pointer SET safe_mode=1,revision=revision+1 "
                "WHERE singleton=1"
            )
        with pytest.raises(Exception, match="Generation binding is stale"):
            stack.runtime.capability_runtime.select(CAPABILITY_ID)
        assert stack.processes.starts == 0
    finally:
        stack.close()


def test_f4_expected_revision_is_checked_inside_mutation_transaction(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "f4-cas")
    try:
        assert stack.runtime is not None
        _, asset_id = _snapshot(stack)
        assert stack.runtime.http.handle(
            "job.start", _start_command(asset_id)
        )[0] == 201
        control = stack.plugin.execution.control_port
        original_cancel = control.cancel
        raced = False

        def cancel_after_concurrent_revision(**kwargs: Any) -> Any:
            nonlocal raced
            if not raced:
                raced = True
                other = sqlite3.connect(stack.repository.database)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    changed = other.execute(
                        "UPDATE execution_job SET job_revision=job_revision+1 "
                        "WHERE job_id='job-1' AND job_revision=2"
                    ).rowcount
                    assert changed == 1
                    other.commit()
                finally:
                    other.close()
            return original_cancel(**kwargs)

        control.cancel = cancel_after_concurrent_revision  # type: ignore[method-assign]
        status, body = stack.runtime.http.handle(
            "job.cancel", _control("cancel", 2)
        )
        assert (status, body["error_code"]) == (409, "stale_revision")
        with stack.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT a.state,j.job_state,j.job_revision FROM execution_attempt a "
                "JOIN execution_job j ON j.job_id=a.job_id"
            ).fetchone()
            operations = connection.execute(
                "SELECT count(*) FROM execution_control_operation"
            ).fetchone()[0]
        assert tuple(row) == ("running", "running", 3)
        assert operations == 0
        assert not any(
            decode_frame(frame).get("method") == "job.cancel"
            for process in stack.processes.processes
            for frame in process.writes
        )
    finally:
        stack.close()


def test_f3_shutdown_converts_owned_cancelling_attempt_to_recoverable(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "f3-shutdown")
    try:
        assert stack.runtime is not None
        stack.processes.cancel_result = {
            "accepted": True,
            "terminal_known": False,
            "attempt_state": "cancelling",
        }
        _, asset_id = _snapshot(stack)
        assert stack.runtime.http.handle(
            "job.start", _start_command(asset_id)
        )[0] == 201
        assert stack.runtime.http.handle(
            "job.cancel", _control("cancel", 2)
        )[0] == 200
        stack.runtime.shutdown()
        with stack.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT a.state,s.state AS step_state,j.job_state "
                "FROM execution_attempt a JOIN execution_step s "
                "ON s.step_id=a.step_id JOIN execution_job j ON j.job_id=a.job_id"
            ).fetchone()
        assert tuple(row) == ("fenced", "needs_attention", "needs_attention")
        assert all(process.code is not None for process in stack.processes.processes)
    finally:
        stack.close()


def test_j6_pump_ticks_then_dispatches_duplicate_host_request_once(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j6")
    try:
        assert stack.runtime is not None and stack.package is not None
        _, asset_id = _snapshot(stack)
        assert stack.runtime.http.handle("job.start", _start_command(asset_id))[0] == 201
        with stack.repository.read_connection() as connection:
            attempt = dict(connection.execute("SELECT * FROM execution_attempt").fetchone())
        payload = b"x"
        request = build_request(
            "host.asset.create/v1",
            {
                "operation_key": "upload-op-1",
                "upload_id": "upload-1",
                "offset": 0,
                "mime": "text/plain",
                "total_size": 1,
                "expected_hash": sha256_hex(payload),
                "chunk_hash": sha256_hex(payload),
                "base64_chunk": "eA==",
                "final": True,
            },
            build_meta(
                "attempt",
                generation_id=GENERATION_ID,
                plugin_release_id=stack.package.release_id,
                deadline_at="2099-01-01T00:00:00Z",
                operation_id=attempt["worker_run_id"],
                job_id="job-1",
                step_id=attempt["step_id"],
                attempt_id=attempt["attempt_id"],
                lease_epoch=1,
            ),
            request_id="00000000-0000-4000-8000-000000000101",
        )
        process = stack.processes.processes[0]
        process.emit(request)
        process.emit(copy.deepcopy(request))
        order: list[str] = []
        original_tick = stack.runtime.supervisor.tick
        original_dispatch = stack.runtime.composition.dispatcher.dispatch_pending

        def tick() -> None:
            order.append("tick")
            original_tick()

        def dispatch(ticket: Any) -> Any:
            order.append("dispatch")
            return original_dispatch(ticket)

        stack.runtime.supervisor.tick = tick  # type: ignore[method-assign]
        stack.runtime.composition.dispatcher.dispatch_pending = dispatch  # type: ignore[method-assign]
        result = stack.runtime.pump.run_once()
        assert order[:2] == ["tick", "dispatch"]
        assert result.dispatched_request_ids == (
            "00000000-0000-4000-8000-000000000101",
        )
        metadata = stack.assets.require("asset-sha256-" + sha256_hex(payload))
        assert metadata.sha256 == sha256_hex(payload)
    finally:
        stack.close()


def test_f5_one_local_runtime_one_pump_and_concurrent_start_process(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "f5-local-owner")
    try:
        assert stack.runtime is not None
        _, asset_id = _snapshot(stack)
        ticks = 0
        original_tick = stack.runtime.supervisor.tick

        def counted_tick() -> None:
            nonlocal ticks
            ticks += 1
            original_tick()

        stack.runtime.supervisor.tick = counted_tick  # type: ignore[method-assign]
        command = _start_command(asset_id)
        barrier = threading.Barrier(2)

        def start() -> tuple[int, dict[str, Any]]:
            barrier.wait(timeout=2)
            return stack.runtime.http.handle("job.start", command)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(start), pool.submit(start)]
            first, second = (future.result(timeout=5) for future in results)
        assert first == second
        assert first[0] == 201
        assert first[1]["idempotent"] is False
        assert stack.processes.starts == 1
        assert _rows(stack) == (1, 1, 1)
        assert ticks == 0
        with pytest.raises(ContractError) as caught:
            build_production_job_runtime(stack.plugin)
        assert caught.value.code == ErrorCode.INVALID_TRANSITION
        assert stack.processes.starts == 1
        stack.runtime.pump.run_once()
        assert ticks == 1
    finally:
        stack.close()


def test_j7_startup_reconciles_sqlite_claim_and_attempt_without_process(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "j7", compose=False)
    try:
        assert stack.package is not None
        snapshot, _ = _snapshot(stack)
        stack.plugin.execution.create_from_verified_snapshot("job-orphan", snapshot)
        stack.plugin.execution.freeze_plan(
            "job-orphan",
            [
                {
                    "step_id": "step-orphan",
                    "result_contract": "artifact-bundle/v1",
                }
            ],
            output_step_id="step-orphan",
        )
        fence = stack.plugin.authority.claim(PLUGIN_ID, "orphan-owner")
        assert fence is not None
        stack.plugin.execution.start_attempt(
            job_id="job-orphan",
            step_id="step-orphan",
            attempt_id="attempt-orphan",
            worker_run_id="orphan-owner",
            plugin_id=PLUGIN_ID,
            release_id=stack.package.release_id,
            package_hash=stack.package.package_hash,
            capability_id=CAPABILITY_ID,
            generation_id=GENERATION_ID,
            lease_epoch=1,
            preallocated_receipt_id="receipt-orphan",
            expected_result_contract="artifact-bundle/v1",
        )
        stack.runtime = build_production_job_runtime(stack.plugin)
        assert stack.runtime.restart_reconciliation == {
            "receipts": 0,
            "attempts": 1,
            "jobs": 1,
            "claims": 1,
        }
        with stack.repository.read_connection() as connection:
            attempt_state = connection.execute(
                "SELECT state FROM execution_attempt WHERE attempt_id='attempt-orphan'"
            ).fetchone()[0]
            claim_state = connection.execute(
                "SELECT state FROM plugin_supervisor_claim WHERE owner_id='orphan-owner'"
            ).fetchone()[0]
        assert (attempt_state, claim_state, stack.processes.starts) == (
            "fenced",
            "released",
            0,
        )
    finally:
        stack.close()


def test_f6_restart_repairs_an_already_fenced_orphan_once(tmp_path: Path) -> None:
    stack = _open_stack(tmp_path, "f6-fenced-restart", compose=False)
    try:
        assert stack.package is not None
        snapshot, _ = _snapshot(stack)
        stack.plugin.execution.create_from_verified_snapshot("job-fenced", snapshot)
        stack.plugin.execution.freeze_plan(
            "job-fenced",
            [
                {
                    "step_id": "step-fenced",
                    "result_contract": "artifact-bundle/v1",
                }
            ],
            output_step_id="step-fenced",
        )
        fence = stack.plugin.authority.claim(PLUGIN_ID, "fenced-owner")
        assert fence is not None
        stack.plugin.execution.start_attempt(
            job_id="job-fenced",
            step_id="step-fenced",
            attempt_id="attempt-fenced",
            worker_run_id="fenced-owner",
            plugin_id=PLUGIN_ID,
            release_id=stack.package.release_id,
            package_hash=stack.package.package_hash,
            capability_id=CAPABILITY_ID,
            generation_id=GENERATION_ID,
            lease_epoch=1,
            preallocated_receipt_id="receipt-fenced",
            expected_result_contract="artifact-bundle/v1",
        )
        with stack.repository.transaction() as connection:
            connection.execute(
                "UPDATE execution_attempt SET state='fenced',revision=revision+1 "
                "WHERE attempt_id='attempt-fenced'"
            )
        stack.runtime = build_production_job_runtime(stack.plugin)
        assert stack.runtime.restart_reconciliation == {
            "receipts": 0,
            "attempts": 0,
            "jobs": 1,
            "claims": 1,
        }
        with stack.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT a.state,s.state AS step_state,j.job_state "
                "FROM execution_attempt a JOIN execution_step s "
                "ON s.step_id=a.step_id JOIN execution_job j ON j.job_id=a.job_id "
                "WHERE a.attempt_id='attempt-fenced'"
            ).fetchone()
        assert tuple(row) == ("fenced", "needs_attention", "needs_attention")
        assert stack.plugin.execution.reconcile_production_restart() == {
            "receipts": 0,
            "attempts": 0,
            "jobs": 0,
        }
    finally:
        stack.close()


@pytest.mark.parametrize("operation", ["start", "control"])
def test_f6_shutdown_closes_then_drains_admitted_resolver(
    tmp_path: Path, operation: str
) -> None:
    stack = _open_stack(tmp_path, f"f6-drain-{operation}")
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        assert stack.runtime is not None
        _, asset_id = _snapshot(stack)
        if operation == "start":
            resolver = stack.runtime.start_resolver
            attribute = "_resolve_start"
            method = "job.start"
            command = _start_command(asset_id)
        else:
            assert stack.runtime.http.handle(
                "job.start", _start_command(asset_id)
            )[0] == 201
            resolver = stack.runtime.control_resolver
            attribute = "_resolve_control"
            method = "job.cancel"
            command = _control("cancel", 2)
        original = getattr(resolver, attribute)
        entered = threading.Event()

        def blocked(command_value: dict[str, Any]) -> Any:
            entered.set()
            assert release.wait(timeout=3)
            return original(command_value)

        setattr(resolver, attribute, blocked)
        request = pool.submit(stack.runtime.http.handle, method, command)
        assert entered.wait(timeout=2)
        shutdown_finished = threading.Event()

        def shutdown() -> None:
            stack.runtime.shutdown()
            shutdown_finished.set()

        closing = pool.submit(shutdown)
        assert not shutdown_finished.wait(timeout=0.1)
        release.set()
        response = request.result(timeout=5)
        closing.result(timeout=5)
        assert response[0] in {200, 201}
        assert shutdown_finished.is_set()

        def projection() -> tuple[Any, ...]:
            with stack.repository.read_connection() as connection:
                row = connection.execute(
                    "SELECT j.job_state,j.job_revision,s.state,a.state "
                    "FROM execution_job j JOIN execution_step s ON s.job_id=j.job_id "
                    "JOIN execution_attempt a ON a.attempt_id=s.active_attempt_id "
                    "WHERE j.job_id='job-1'"
                ).fetchone()
                counts = tuple(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in (
                        "execution_job_event",
                        "execution_control_operation",
                        "execution_job_start_receipt",
                    )
                )
            return (*tuple(row), *counts)

        before = projection()
        rejected = stack.runtime.http.handle(method, command)
        assert rejected[0] == 409
        assert projection() == before
        assert before[0] == "needs_attention"
        assert before[2] == "needs_attention"
        assert before[3] == "fenced"
    finally:
        release.set()
        pool.shutdown(wait=True)
        stack.close()


def test_f6_start_completion_revalidates_active_job_step_attempt(
    tmp_path: Path,
) -> None:
    stack = _open_stack(tmp_path, "f6-completion-race")
    try:
        assert stack.runtime is not None
        _, asset_id = _snapshot(stack)
        original = stack.plugin.execution.complete_production_start

        def complete_after_step_loses_authority(**kwargs: Any) -> Any:
            other = sqlite3.connect(stack.repository.database)
            try:
                other.execute("BEGIN IMMEDIATE")
                changed = other.execute(
                    "UPDATE execution_step SET state='needs_attention',"
                    "revision=revision+1 WHERE job_id='job-1' AND state='running'"
                ).rowcount
                assert changed == 1
                other.commit()
            finally:
                other.close()
            return original(**kwargs)

        stack.plugin.execution.complete_production_start = (  # type: ignore[method-assign]
            complete_after_step_loses_authority
        )
        status, _body = stack.runtime.http.handle(
            "job.start", _start_command(asset_id)
        )
        assert status >= 400
        with stack.repository.read_connection() as connection:
            receipt = connection.execute(
                "SELECT state,response_json FROM execution_job_start_receipt"
            ).fetchone()
            row = connection.execute(
                "SELECT a.state,s.state AS step_state,j.job_state "
                "FROM execution_attempt a JOIN execution_step s "
                "ON s.step_id=a.step_id JOIN execution_job j ON j.job_id=a.job_id"
            ).fetchone()
        assert tuple(receipt) == ("uncertain", None)
        assert tuple(row) == ("fenced", "needs_attention", "needs_attention")
    finally:
        stack.close()


@pytest.mark.parametrize("mode", ["missing", "corrupt", "retired", "substituted"])
def test_j9_invalid_installed_artifact_or_snapshot_has_no_source_fallback(
    tmp_path: Path, mode: str
) -> None:
    stack = _open_stack(
        tmp_path,
        f"j9-{mode}",
        prepare_venv=mode != "missing",
    )
    try:
        assert stack.runtime is not None
        if mode == "corrupt":
            assert stack.package is not None
            manifest = (
                stack.root
                / "plugins"
                / "venvs"
                / PLUGIN_ID
                / stack.package.package_hash
                / "plotpilot-venv.json"
            )
            manifest.write_text("{}", encoding="utf-8")
        elif mode == "retired":
            assert stack.package is not None
            retired = stack.plugin.retirement.get(stack.package.release_id)
            retired.update(
                state="retired",
                package_present=False,
                retire_epoch=int(retired["retire_epoch"]) + 1,
                started_at=NOW,
                completed_at=NOW,
            )
            with stack.plugin.lifecycle.transaction() as connection:
                connection.execute(
                    "UPDATE p2_plugin_release_retirement SET retirement_json=?,"
                    "revision=revision+1 WHERE release_id=?",
                    (
                        json.dumps(retired, sort_keys=True, separators=(",", ":")),
                        stack.package.release_id,
                    ),
                )
            assert stack.plugin.retirement.get(stack.package.release_id)["state"] == "retired"
        _, asset_id = _snapshot(
            stack,
            package_hash="d" * 64 if mode == "substituted" else None,
        )
        status, body = stack.runtime.http.handle(
            "job.start", _start_command(asset_id)
        )
        assert status >= 400
        assert body["error_code"] in {
            "invalid_transition",
            "incompatible_generation",
            "release_retiring",
        }
        assert stack.processes.starts == 0
        assert _rows(stack) == (0, 0, 0)
    finally:
        stack.close()
