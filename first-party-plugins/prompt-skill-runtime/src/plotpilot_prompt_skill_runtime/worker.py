"""Strict installable worker for the Prompt/Skill Runtime plugin.

The worker consumes the P0 ``prompt.skill.execute/v2`` contract and delegates
all execution authority to the composed Host/Core seam.  It owns only the
process protocol and the P0 operation replay guard; it never creates a Skill
release, ModelReceipt, result Bundle, or Job completion record.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from importlib.resources import files as resource_files
from pathlib import Path
from typing import NamedTuple
from typing import Any

from plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    hash_jcs,
    parse_json_bytes,
    validate_rpc_request,
)
from plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import METHOD as PROMPT_SKILL_METHOD
from plotpilot_plugin_sdk.prompt_skill_rpc_v2 import (
    PromptSkillOperationLedgerV2,
    parse_prompt_skill_execute_request_v2,
    parse_rpc_error_v2,
    validate_prompt_skill_execute_v2,
)
from plotpilot_plugin_sdk.rpc import ERROR_CODES, HOST_METHODS, build_request
from plotpilot_plugin_sdk.verifier import validate_rpc_response, validate_rpc_result

from .host_adapter import (
    CAPABILITY_ID,
    PLUGIN_ID,
    PromptSkillHostAdapter,
    capability_descriptor,
)

_RPC_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MIGRATION_PATH = Path("migrations/manifest.json")
_STATE_SCHEMA = "plotpilot-prompt-skill-worker-state/v1"
_MANIFEST_KEYS = {"schema", "module", "runner_owner", "from_schema", "to_schema", "steps"}
_MIGRATION_STEP_KEYS = {"step_id", "file", "sha256", "from_schema", "to_schema"}


class _ManifestSource(NamedTuple):
    raw: bytes
    value: dict[str, Any]
    source: str
    project_root: Path | None


class StdioHostPort:
    """Synchronous Host RPC bridge over the worker's already-open stdio."""

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._decoder = FrameDecoder()
        self._meta: dict[str, Any] | None = None
        self._lock = threading.RLock()

    def bind_meta(self, meta: Mapping[str, Any]) -> None:
        self._meta = dict(meta)

    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if method not in HOST_METHODS:
            raise ContractError(1010, f"worker may call only published Host RPC methods: {method!r}")
        if self._meta is None:
            raise ContractError(1010, "Host RPC requires a bound worker request")
        request = build_request(method, params, self._meta)
        validate_rpc_request(request)
        with self._lock:
            self._stdout.write(encode_frame(request))
            self._stdout.flush()
            while True:
                chunk = self._stdin.read(65536)
                if not chunk:
                    raise ContractError(1005, "Host RPC channel ended before its response")
                messages = self._decoder.feed(chunk)
                if len(messages) != 1:
                    if messages:
                        raise ContractError(1010, "Host RPC channel returned multiple frames for one call")
                    continue
                response = messages[0]
                if "method" in response or response.get("id") != request["id"]:
                    raise ContractError(1010, "Host RPC response is not bound to the worker request")
                validate_rpc_response(response, request=request)
                if "error" in response:
                    error = response["error"]
                    raise ContractError(int(error["code"]), str(error["message"]))
                result = response.get("result")
                if not isinstance(result, Mapping):
                    raise ContractError(1011, "Host RPC response result is not an object")
                return dict(result)


class _StandaloneComposition:
    """Explicitly stopped default composition used by the no-argument entrypoint."""

    def __init__(self, host: StdioHostPort) -> None:
        self.host = host

    def execute(self, _request: Mapping[str, Any], _prepared: Any, _runtime: Any) -> Mapping[str, Any]:
        raise ContractError(1010, "prompt.skill.execute/v2 requires the P3/Core composition callback")

    def authoritative_context(self, _request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ContractError(1002, "prompt.skill.execute/v2 requires authoritative Attempt context")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _migration_manifest_path() -> Path:
    """Return the source-tree manifest path used before wheel installation."""

    return Path(__file__).resolve().parents[2] / _MIGRATION_PATH


def _read_manifest_source() -> tuple[bytes, str, Path | None]:
    """Read the immutable outer manifest without falling back across drift.

    The source checkout keeps the install-time manifest outside ``src`` so it
    cannot be confused with the private SQLite migration metadata.  A built
    wheel installs the same file through ``tool.setuptools.data-files`` at the
    venv prefix.  If the preferred source file exists but is malformed, the
    worker fails closed instead of silently selecting another copy.
    """

    source_path = _migration_manifest_path()
    if source_path.is_symlink() or source_path.exists():
        if source_path.is_symlink() or not source_path.is_file():
            raise ContractError(1007, "Prompt Skill migration manifest is not a regular file")
        try:
            return source_path.read_bytes(), "source", source_path.parents[1]
        except OSError as exc:
            raise ContractError(1007, "Prompt Skill migration manifest is unavailable") from exc

    installed_path = Path(sys.prefix) / _MIGRATION_PATH
    if installed_path.is_symlink() or installed_path.exists():
        if installed_path.is_symlink() or not installed_path.is_file():
            raise ContractError(1007, "installed Prompt Skill migration manifest is not a regular file")
        try:
            return installed_path.read_bytes(), "installed", None
        except OSError as exc:
            raise ContractError(1007, "installed Prompt Skill migration manifest is unavailable") from exc

    raise ContractError(1007, "Prompt Skill migration manifest is unavailable")


def _require_manifest_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractError(1007, f"{label} is not a lowercase SHA-256")
    return value


def _require_manifest_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractError(1007, f"{label} is not a canonical ID")
    return value


def _read_migration_sql(source: _ManifestSource, declared: str) -> bytes:
    parts = declared.split("/")
    if source.source == "source":
        assert source.project_root is not None
        path = source.project_root / "src" / Path(*parts)
        if path.is_symlink() or not path.is_file():
            raise ContractError(1007, "Prompt Skill migration SQL resource is not a regular source file")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ContractError(1007, "Prompt Skill migration SQL resource is unavailable") from exc

    if not parts or parts[0] != "plotpilot_prompt_skill_runtime":
        raise ContractError(1007, "Prompt Skill migration resource is outside its package")
    try:
        return resource_files("plotpilot_prompt_skill_runtime").joinpath(*parts[1:]).read_bytes()
    except (FileNotFoundError, OSError, ModuleNotFoundError) as exc:
        raise ContractError(1007, "installed Prompt Skill migration SQL resource is unavailable") from exc


def _parse_manifest(raw: bytes, source_name: str, project_root: Path | None) -> _ManifestSource:
    try:
        value = parse_json_bytes(raw)
    except ContractError as exc:
        raise ContractError(1007, "Prompt Skill migration manifest is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise ContractError(1007, "Prompt Skill migration manifest has an unexpected closed shape")
    if value["schema"] != "p3-module-migrations/v1":
        raise ContractError(1007, "Prompt Skill migration manifest schema is unsupported")
    if value["module"] != "plotpilot_prompt_skill_runtime" or value["runner_owner"] != "P2":
        raise ContractError(1007, "Prompt Skill migration manifest owner or module drifted")
    from_schema = _require_manifest_hash(value["from_schema"], "migration from_schema")
    to_schema = _require_manifest_hash(value["to_schema"], "migration to_schema")
    if from_schema == to_schema:
        raise ContractError(1007, "Prompt Skill migration manifest must describe a schema transition")

    steps = value["steps"]
    if not isinstance(steps, list) or not steps:
        raise ContractError(1007, "Prompt Skill migration manifest must contain at least one step")
    step_ids: set[str] = set()
    step_files: set[str] = set()
    for index, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict) or set(raw_step) != _MIGRATION_STEP_KEYS:
            raise ContractError(1007, f"Prompt Skill migration step {index} is not closed")
        step_id = _require_manifest_id(raw_step["step_id"], f"migration step {index} step_id")
        file_name = raw_step["file"]
        if (
            not isinstance(file_name, str)
            or not file_name
            or len(file_name) > 240
            or "\\" in file_name
            or "\x00" in file_name
            or file_name.startswith("/")
            or any(part in {"", ".", ".."} for part in file_name.split("/"))
            or not file_name.startswith("plotpilot_prompt_skill_runtime/migrations/")
            or not file_name.endswith(".sql")
        ):
            raise ContractError(1007, f"migration step {index} file is not a package-relative SQL resource")
        if step_id in step_ids or file_name in step_files:
            raise ContractError(1007, "Prompt Skill migration steps must have unique IDs and resources")
        step_ids.add(step_id)
        step_files.add(file_name)
        if _require_manifest_hash(raw_step["sha256"], f"migration step {index} sha256") != hashlib.sha256(
            _read_migration_sql(_ManifestSource(raw, value, source_name, project_root), file_name)
        ).hexdigest():
            raise ContractError(1007, "Prompt Skill migration SQL resource hash does not match its manifest")
        if raw_step["from_schema"] != from_schema or raw_step["to_schema"] != to_schema:
            raise ContractError(1007, "Prompt Skill migration step transition does not match its manifest")
    return _ManifestSource(raw, value, source_name, project_root)


def _load_manifest() -> _ManifestSource:
    raw, source_name, project_root = _read_manifest_source()
    return _parse_manifest(raw, source_name, project_root)


def _manifest_bytes() -> bytes:
    return _load_manifest().raw


def _manifest() -> dict[str, Any]:
    return dict(_load_manifest().value)


def _state_error(message: str, *, cause: BaseException | None = None) -> ContractError:
    error = ContractError(1005, f"Prompt Skill worker state is invalid: {message}")
    if cause is not None:
        error.__cause__ = cause
    return error


def _state_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise _state_error(f"{label} is not a lowercase SHA-256")
    return value


def _state_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise _state_error(f"{label} is not a canonical ID")
    return value


def _state_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise _state_error(f"{label} is not a positive integer")
    return value


class PromptSkillWorker:
    """Dispatch the frozen worker matrix plus the additive P0 v2 method."""

    def __init__(
        self,
        adapter: PromptSkillHostAdapter | None = None,
        *,
        release_id: str | None = None,
        state_path: str | os.PathLike[str] | None = None,
        execute_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        authoritative_context: Mapping[str, Any] | Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        start_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        resume_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        cancel_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        migration_lease_validator: Callable[[Mapping[str, Any]], bool] | None = None,
        migration_apply_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
        migration_verify_handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        if release_id is not None and (
            not isinstance(release_id, str)
            or len(release_id) != 64
            or any(c not in "0123456789abcdef" for c in release_id)
        ):
            raise ValueError("release_id must be a lowercase SHA-256")
        if adapter is not None and not isinstance(adapter, PromptSkillHostAdapter):
            raise TypeError("adapter must be the integrated PromptSkillHostAdapter")
        self.adapter = adapter
        self.release_id = release_id
        self.execute_handler = execute_handler
        self.authoritative_context = authoritative_context
        self.start_handler = start_handler
        self.resume_handler = resume_handler
        self.cancel_handler = cancel_handler
        self.migration_lease_validator = migration_lease_validator
        self.migration_apply_handler = migration_apply_handler
        self.migration_verify_handler = migration_verify_handler
        self.worker_instance_id = f"prompt-skill-worker-{uuid.uuid4()}"
        self._generation_id: str | None = None
        self._state = "new"
        self._lock = threading.RLock()
        self.state_path = None if state_path is None else Path(state_path)
        if self.state_path is not None and self.state_path.exists() and self.state_path.is_dir():
            raise ValueError("state_path must name a file")
        self._persisted_release_id: str | None = None
        self._lease_highwater: dict[tuple[str, str, str, str], int] = {}
        self._ledger = PromptSkillOperationLedgerV2()
        self._ledgers: dict[str, PromptSkillOperationLedgerV2] = {}
        self._replay_records: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._plans: dict[str, dict[str, Any]] = {}
        self._shutdown = False
        self._load_state()
        if self._persisted_release_id is not None and self.release_id is not None and self.release_id != self._persisted_release_id:
            raise ContractError(1001, "worker state belongs to a different plugin release")

    def _ledger_for_generation(self, generation_id: str) -> PromptSkillOperationLedgerV2:
        ledger = self._ledgers.get(generation_id)
        if ledger is None:
            ledger = PromptSkillOperationLedgerV2()
            self._ledgers[generation_id] = ledger
        return ledger

    @staticmethod
    def _replay_key(request: Mapping[str, Any]) -> tuple[str, str, str]:
        params = request["params"]
        return params["generation_id"], params["workspace_id"], params["operation_key"]

    def _state_document(self) -> dict[str, Any]:
        replay_entries = [
            {
                "generation_id": generation_id,
                "workspace_id": workspace_id,
                "operation_key": operation_key,
                "request": record["request"],
                "result": record["result"],
                "authoritative_context": record["authoritative_context"],
            }
            for (generation_id, workspace_id, operation_key), record in sorted(self._replay_records.items())
        ]
        migration_plans = [
            {
                "plan_id": plan_id,
                "request": record["request"],
                "result": record["result"],
                "manifest_hash": record["manifest_hash"],
                "generation_id": record["generation_id"],
                "plugin_release_id": record["plugin_release_id"],
                "install_operation_id": record["install_operation_id"],
                "install_lease_epoch": record["install_lease_epoch"],
                "from_schema": record["from_schema"],
                "to_schema": record["to_schema"],
            }
            for plan_id, record in sorted(self._plans.items())
        ]
        lease_highwater = [
            {
                "generation_id": generation_id,
                "job_id": job_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "lease_epoch": lease_epoch,
            }
            for (generation_id, job_id, step_id, attempt_id), lease_epoch in sorted(self._lease_highwater.items())
        ]
        return {
            "schema": _STATE_SCHEMA,
            "release_id": self.release_id or self._persisted_release_id,
            "replay_entries": replay_entries,
            "migration_plans": migration_plans,
            "lease_highwater": lease_highwater,
        }

    def _persist_state(self) -> None:
        """Publish the complete worker state with one atomic replacement."""

        if self.state_path is None:
            return
        path = self.state_path
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.is_dir():
                raise IsADirectoryError(str(path))
            payload = json.dumps(
                self._state_document(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=str(parent),
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(temporary), str(path))
            finally:
                if temporary.exists():
                    temporary.unlink()
        except (OSError, TypeError, ValueError) as exc:
            raise ContractError(1005, "Prompt Skill worker state could not be atomically persisted") from exc

    def _load_state(self) -> None:
        if self.state_path is None:
            return
        path = self.state_path
        if not path.exists():
            if path.is_symlink():
                raise _state_error("state_path is a dangling or symlinked file")
            return
        if path.is_symlink() or not path.is_file():
            raise _state_error("state_path is not a regular file")
        try:
            value = parse_json_bytes(path.read_bytes())
        except (OSError, ContractError) as exc:
            raise _state_error("state file is not strict UTF-8 JSON", cause=exc) from exc
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "release_id",
            "replay_entries",
            "migration_plans",
            "lease_highwater",
        }:
            raise _state_error("state file has an unexpected closed shape")
        if value["schema"] != _STATE_SCHEMA:
            raise _state_error("state schema is unsupported")
        stored_release = value["release_id"]
        if stored_release is not None:
            self._persisted_release_id = _state_hash(stored_release, "release_id")

        replay_entries = value["replay_entries"]
        if not isinstance(replay_entries, list):
            raise _state_error("replay_entries must be an array")
        for index, raw_entry in enumerate(replay_entries):
            if not isinstance(raw_entry, dict) or set(raw_entry) != {
                "generation_id",
                "workspace_id",
                "operation_key",
                "request",
                "result",
                "authoritative_context",
            }:
                raise _state_error(f"replay entry {index} is not closed")
            generation_id = _state_id(raw_entry["generation_id"], f"replay entry {index} generation_id")
            workspace_id = _state_id(raw_entry["workspace_id"], f"replay entry {index} workspace_id")
            operation_key = _state_id(raw_entry["operation_key"], f"replay entry {index} operation_key")
            request = raw_entry["request"]
            result = raw_entry["result"]
            authority = raw_entry["authoritative_context"]
            if not isinstance(request, Mapping) or not isinstance(result, Mapping) or not isinstance(authority, Mapping):
                raise _state_error(f"replay entry {index} payloads must be objects")
            try:
                parsed_request = parse_prompt_skill_execute_request_v2(request)
                ledger = self._ledger_for_generation(generation_id)
                stored, replayed = ledger.record(
                    parsed_request,
                    result,
                    authoritative_context=authority,
                )
            except ContractError as exc:
                raise _state_error(f"replay entry {index} failed the v2 ledger gate", cause=exc) from exc
            if replayed:
                raise _state_error(f"replay entry {index} duplicates an earlier operation")
            key = self._replay_key(parsed_request)
            if key != (generation_id, workspace_id, operation_key):
                raise _state_error(f"replay entry {index} identity does not match its request")
            if self._persisted_release_id is not None and parsed_request["params"]["plugin_release_id"] != self._persisted_release_id:
                raise _state_error(f"replay entry {index} belongs to a different plugin release")
            if self._persisted_release_id is None:
                raise _state_error(f"replay entry {index} has no persisted plugin release")
            self._replay_records[key] = {
                "request": json.loads(json.dumps(parsed_request, ensure_ascii=False, allow_nan=False)),
                "result": json.loads(json.dumps(stored, ensure_ascii=False, allow_nan=False)),
                "authoritative_context": json.loads(json.dumps(dict(authority), ensure_ascii=False, allow_nan=False)),
            }

        migration_plans = value["migration_plans"]
        if not isinstance(migration_plans, list):
            raise _state_error("migration_plans must be an array")
        for index, raw_plan in enumerate(migration_plans):
            if not isinstance(raw_plan, dict) or set(raw_plan) != {
                "plan_id",
                "request",
                "result",
                "manifest_hash",
                "generation_id",
                "plugin_release_id",
                "install_operation_id",
                "install_lease_epoch",
                "from_schema",
                "to_schema",
            }:
                raise _state_error(f"migration plan {index} is not closed")
            plan_id = _state_id(raw_plan["plan_id"], f"migration plan {index} plan_id")
            generation_id = _state_id(raw_plan["generation_id"], f"migration plan {index} generation_id")
            plugin_release_id = _state_hash(raw_plan["plugin_release_id"], f"migration plan {index} plugin_release_id")
            install_operation_id = _state_id(raw_plan["install_operation_id"], f"migration plan {index} install_operation_id")
            install_lease_epoch = _state_positive_int(raw_plan["install_lease_epoch"], f"migration plan {index} install_lease_epoch")
            manifest_hash = _state_hash(raw_plan["manifest_hash"], f"migration plan {index} manifest_hash")
            from_schema = _state_hash(raw_plan["from_schema"], f"migration plan {index} from_schema")
            to_schema = _state_hash(raw_plan["to_schema"], f"migration plan {index} to_schema")
            request = raw_plan["request"]
            result = raw_plan["result"]
            if not isinstance(request, Mapping) or not isinstance(result, Mapping):
                raise _state_error(f"migration plan {index} request/result must be objects")
            if request.get("method") != "migration.plan":
                raise _state_error(f"migration plan {index} request method is not migration.plan")
            try:
                validate_rpc_request(request)
                self._validate_method_result("migration.plan", result, request=request)
            except ContractError as exc:
                raise _state_error(f"migration plan {index} failed the RPC gate", cause=exc) from exc
            params = request["params"]
            meta = request["meta"]
            if (
                plan_id != result["plan_id"]
                or generation_id != meta["generation_id"]
                or plugin_release_id != meta["plugin_release_id"]
                or install_operation_id != meta.get("install_operation_id")
                or install_lease_epoch != meta.get("install_lease_epoch")
                or from_schema != params["from_schema"]
                or to_schema != params["to_schema"]
                or manifest_hash != params["migration_manifest_hash"]
            ):
                raise _state_error(f"migration plan {index} identity does not match its request")
            if plan_id != self._plan_id(request):
                raise _state_error(f"migration plan {index} deterministic identity is invalid")
            if self._persisted_release_id is not None and plugin_release_id != self._persisted_release_id:
                raise _state_error(f"migration plan {index} belongs to a different plugin release")
            if self._persisted_release_id is None:
                raise _state_error(f"migration plan {index} has no persisted plugin release")
            if plan_id in self._plans:
                raise _state_error(f"migration plan {index} duplicates an earlier plan")
            self._plans[plan_id] = {
                "request": json.loads(json.dumps(dict(request), ensure_ascii=False, allow_nan=False)),
                "result": json.loads(json.dumps(dict(result), ensure_ascii=False, allow_nan=False)),
                "manifest_hash": manifest_hash,
                "generation_id": generation_id,
                "plugin_release_id": plugin_release_id,
                "install_operation_id": install_operation_id,
                "install_lease_epoch": install_lease_epoch,
                "from_schema": from_schema,
                "to_schema": to_schema,
            }

        lease_highwater = value["lease_highwater"]
        if not isinstance(lease_highwater, list):
            raise _state_error("lease_highwater must be an array")
        for index, raw_lease in enumerate(lease_highwater):
            if not isinstance(raw_lease, dict) or set(raw_lease) != {
                "generation_id",
                "job_id",
                "step_id",
                "attempt_id",
                "lease_epoch",
            }:
                raise _state_error(f"lease high-water entry {index} is not closed")
            key = (
                _state_id(raw_lease["generation_id"], f"lease high-water entry {index} generation_id"),
                _state_id(raw_lease["job_id"], f"lease high-water entry {index} job_id"),
                _state_id(raw_lease["step_id"], f"lease high-water entry {index} step_id"),
                _state_id(raw_lease["attempt_id"], f"lease high-water entry {index} attempt_id"),
            )
            epoch = _state_positive_int(raw_lease["lease_epoch"], f"lease high-water entry {index} lease_epoch")
            if key in self._lease_highwater:
                raise _state_error(f"lease high-water entry {index} duplicates an earlier key")
            self._lease_highwater[key] = epoch

    def _error(self, request: Mapping[str, Any] | None, exc: BaseException) -> dict[str, Any]:
        code = int(getattr(exc, "code", 1011))
        if code not in ERROR_CODES:
            code = 1011
        message = str(exc) or type(exc).__name__
        candidate_id = request.get("id") if isinstance(request, Mapping) else None
        request_id = candidate_id if isinstance(candidate_id, str) and _RPC_ID.fullmatch(candidate_id) else None
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
                "data": {
                    "error_id": f"prompt-skill-error-{hashlib.sha256(message.encode()).hexdigest()[:24]}",
                    "retryable": code in {1002, 1003, 1004, 1009},
                    "details_asset_id": None,
                },
            },
        }
        if isinstance(request, Mapping) and request.get("method") == PROMPT_SKILL_METHOD:
            parse_rpc_error_v2(response)
        return response

    def _check_meta(self, request: Mapping[str, Any]) -> None:
        meta = request["meta"]
        if self.release_id is not None and meta["plugin_release_id"] != self.release_id:
            raise ContractError(1001, "RPC plugin release does not match the installed worker")
        if self._persisted_release_id is not None and meta["plugin_release_id"] != self._persisted_release_id:
            raise ContractError(1001, "RPC plugin release does not match the persisted worker state")
        if self._state == "new" and request["method"] != "runtime.handshake":
            raise ContractError(1001, "worker requires runtime.handshake before any other method")
        if self._state == "handshaken" and request["method"] == "runtime.handshake":
            raise ContractError(1010, "worker handshake may only be completed once")
        if self._generation_id is not None and meta["generation_id"] != self._generation_id:
            raise ContractError(1001, "RPC generation does not match the handshaken worker")
        if self._shutdown:
            raise ContractError(1010, "worker has already shut down")
        if meta.get("context") == "attempt":
            identity = (meta["generation_id"], meta["job_id"], meta["step_id"], meta["attempt_id"])
            highwater = self._lease_highwater.get(identity)
            if highwater is not None and meta["lease_epoch"] < highwater:
                raise ContractError(1002, "RPC attempt lease epoch is stale")

    def _commit_epoch(self, request: Mapping[str, Any]) -> tuple[tuple[str, str, str, str], int] | None:
        meta = request.get("meta", {})
        if meta.get("context") == "attempt":
            identity = (meta["generation_id"], meta["job_id"], meta["step_id"], meta["attempt_id"])
            current = self._lease_highwater.get(identity, 0)
            epoch = max(current, meta["lease_epoch"])
            self._lease_highwater[identity] = epoch
            return identity, current
        return None

    def _commit_attempt_success(self, request: Mapping[str, Any]) -> None:
        previous = self._commit_epoch(request)
        try:
            self._persist_state()
        except Exception:
            if previous is not None:
                identity, old_value = previous
                if old_value:
                    self._lease_highwater[identity] = old_value
                else:
                    self._lease_highwater.pop(identity, None)
            raise

    @staticmethod
    def _plan_id(request: Mapping[str, Any]) -> str:
        params = request["params"]
        meta = request["meta"]
        return hash_jcs(
            "prompt-skill-migration-plan/v1",
            {
                "from_schema": params["from_schema"],
                "to_schema": params["to_schema"],
                "manifest_hash": params["migration_manifest_hash"],
                "generation_id": meta["generation_id"],
                "plugin_release_id": meta["plugin_release_id"],
                "install_operation_id": meta["install_operation_id"],
                "install_lease_epoch": meta["install_lease_epoch"],
            },
        )

    @staticmethod
    def _validate_method_result(
        method: str,
        result: Mapping[str, Any],
        *,
        request: Mapping[str, Any] | None = None,
    ) -> None:
        """Validate the closed method branch while tolerating duplicate P0 branches.

        The frozen v1 success schema contains two intentionally identical
        ``job.start``/``job.resume`` branches.  JSON Schema's ``oneOf`` reports
        that valid result as ambiguous even after the method matrix has bound
        the result fields.  ``validate_rpc_result`` performs that exact field
        binding first; only its known duplicate-branch diagnostic is ignored.
        Every type, pattern, required-field and semantic failure still raises.
        """

        try:
            validate_rpc_result(method, result, request=request)
        except ContractValidationError as exc:
            if method not in {"job.start", "job.resume"} or "is valid under each of" not in str(exc):
                raise

    def _result(self, request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise ContractError(1011, "worker result must be an object")
        value = {"jsonrpc": "2.0", "id": request["id"], "result": dict(result)}
        self._validate_method_result(request["method"], value["result"], request=request)
        return value

    def _resolve_authority(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        source = self.authoritative_context
        if source is None and self.adapter is not None:
            source = getattr(self.adapter.composition, "authoritative_context", None)
        if source is None:
            raise ContractError(1002, "Prompt Skill execute requires authoritative Attempt context")
        value = source(request) if callable(source) else source
        if not isinstance(value, Mapping):
            raise ContractError(1002, "authoritative Attempt context is not an object")
        return dict(value)

    def _execute_v2(self, request: Mapping[str, Any]) -> dict[str, Any]:
        parsed = parse_prompt_skill_execute_request_v2(request)
        self._check_meta(parsed)
        authority = self._resolve_authority(parsed)
        ledger = self._ledger_for_generation(parsed["params"]["generation_id"])
        self._ledger = ledger
        try:
            return {"jsonrpc": "2.0", "id": parsed["id"], "result": ledger.replay(parsed, authoritative_context=authority)}
        except ContractError as exc:
            if exc.code != 1011:
                raise
        if self.adapter is not None:
            result = self.adapter.execute(parsed, authoritative_context=authority)
        elif self.execute_handler is not None:
            result = self.execute_handler(parsed, authority)
        else:
            raise ContractError(1010, "prompt.skill.execute/v2 requires the P3/Core composition callback")
        if not isinstance(result, Mapping):
            raise ContractError(1011, "P3/Core composition returned a non-object result")
        parsed_result = validate_prompt_skill_execute_v2(parsed, result, authoritative_context=authority)
        ledger_entry_key = (parsed["params"]["workspace_id"], parsed["params"]["operation_key"])
        previous_ledger_entry = ledger._entries.get(ledger_entry_key)
        stored, replayed = ledger.record(parsed, parsed_result, authoritative_context=authority)
        if replayed:
            return {"jsonrpc": "2.0", "id": parsed["id"], "result": stored}
        key = self._replay_key(parsed)
        previous_record = self._replay_records.get(key)
        self._replay_records[key] = {
            "request": json.loads(json.dumps(dict(parsed), ensure_ascii=False, allow_nan=False)),
            "result": json.loads(json.dumps(dict(stored), ensure_ascii=False, allow_nan=False)),
            "authoritative_context": json.loads(json.dumps(dict(authority), ensure_ascii=False, allow_nan=False)),
        }
        try:
            self._commit_attempt_success(parsed)
        except Exception:
            if previous_record is None:
                self._replay_records.pop(key, None)
            else:
                self._replay_records[key] = previous_record
            if previous_ledger_entry is None:
                ledger._entries.pop(ledger_entry_key, None)
            else:
                ledger._entries[ledger_entry_key] = previous_ledger_entry
            raise
        return {"jsonrpc": "2.0", "id": parsed["id"], "result": stored}

    @staticmethod
    def _install_meta_pair(request: Mapping[str, Any]) -> tuple[str, int]:
        meta = request.get("meta")
        if not isinstance(meta, Mapping) or meta.get("context") != "install":
            raise ContractError(1002, "migration lifecycle requires install-scoped request metadata")
        install_operation_id = meta.get("install_operation_id")
        install_lease_epoch = meta.get("install_lease_epoch")
        if not isinstance(install_operation_id, str) or _ID.fullmatch(install_operation_id) is None:
            raise ContractError(1002, "migration install_operation_id is missing or invalid")
        if type(install_lease_epoch) is not int or install_lease_epoch < 1:
            raise ContractError(1002, "migration install_lease_epoch is missing or invalid")
        return install_operation_id, install_lease_epoch

    @staticmethod
    def _validate_core_install_fence(
        install_operation_id: str,
        install_lease_epoch: int,
        owner_instance_id: object,
    ) -> None:
        """Reuse Core's InstallFence value semantics when Core is composed here.

        The injected ``migration_lease_validator`` remains the only authority;
        this worker only validates the immutable request pair and never owns or
        synthesizes a lease. The standalone wheel may not import Core, so the
        read-only value adapter is intentionally lazy and optional.
        """

        if owner_instance_id is None:
            return
        try:
            from plotpilot_core.supervisor import InstallFence
        except ModuleNotFoundError:
            return
        InstallFence(install_operation_id, install_lease_epoch, owner_instance_id)

    def _check_install_lease(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        params = request["params"]
        install_operation_id, install_lease_epoch = self._install_meta_pair(request)
        self._validate_core_install_fence(
            install_operation_id,
            install_lease_epoch,
            params.get("owner_instance_id"),
        )
        if self.migration_lease_validator is None:
            raise ContractError(1010, "migration lifecycle requires the Core install-lease composition")
        try:
            valid = self.migration_lease_validator(params)
        except ContractError:
            raise
        except Exception as exc:  # noqa: BLE001 - the Core lease gate is a hard boundary
            raise ContractError(1002, "migration install lease validation failed") from exc
        if valid is not True:
            raise ContractError(1002, "migration install lease is stale")
        return {
            "db_lease_id": params.get("db_lease_id"),
            "db_lease_epoch": params.get("db_lease_epoch"),
            "owner_instance_id": params.get("owner_instance_id"),
            "install_operation_id": install_operation_id,
            "install_lease_epoch": install_lease_epoch,
        }

    def _require_plan(
        self,
        request: Mapping[str, Any],
        manifest: _ManifestSource,
        plan_id: str,
    ) -> dict[str, Any]:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise ContractError(1007, "migration plan is not owned by this worker")
        params = request["params"]
        meta = request["meta"]
        manifest_hash = hashlib.sha256(manifest.raw).hexdigest()
        install_operation_id, install_lease_epoch = self._install_meta_pair(request)
        if (
            plan["manifest_hash"] != manifest_hash
            or plan["from_schema"] != manifest.value["from_schema"]
            or plan["to_schema"] != manifest.value["to_schema"]
            or plan["generation_id"] != meta["generation_id"]
            or plan["plugin_release_id"] != meta["plugin_release_id"]
        ):
            raise ContractError(1007, "migration plan is not bound to this manifest, generation, and release")
        # The install fence is deliberately distinct from the immutable plan
        # identity above.  A plan replayed under another install operation or
        # epoch is a stale lease failure, not an alternate owned plan.  Reject
        # it before the Core validator or mutation handler sees the request.
        if (
            plan["install_operation_id"] != install_operation_id
            or plan["install_lease_epoch"] != install_lease_epoch
        ):
            raise ContractError(1002, "migration plan install fence is stale or belongs to another install")
        if plan["result"]["plan_id"] != plan_id:
            raise ContractError(1007, "migration plan result identity is corrupt")
        return plan

    def _require_verify_plan(self, request: Mapping[str, Any], manifest: _ManifestSource) -> dict[str, Any]:
        params = request["params"]
        meta = request["meta"]
        manifest_hash = hashlib.sha256(manifest.raw).hexdigest()
        install_operation_id, install_lease_epoch = self._install_meta_pair(request)
        candidates = [
            plan
            for plan in self._plans.values()
            if plan["manifest_hash"] == manifest_hash
            and plan["from_schema"] == manifest.value["from_schema"]
            and plan["to_schema"] == params["expected_schema"]
            and plan["generation_id"] == meta["generation_id"]
            and plan["plugin_release_id"] == meta["plugin_release_id"]
            and plan["install_operation_id"] == install_operation_id
            and plan["install_lease_epoch"] == install_lease_epoch
        ]
        if len(candidates) != 1:
            raise ContractError(1007, "migration.verify requires exactly one matching owned migration plan")
        return candidates[0]

    def _migration(self, request: Mapping[str, Any]) -> dict[str, Any]:
        method = request["method"]
        params = request["params"]
        manifest = _load_manifest()
        manifest_hash = hashlib.sha256(manifest.raw).hexdigest()
        if method == "migration.plan":
            if params["migration_manifest_hash"] != manifest_hash:
                raise ContractError(1007, "migration manifest hash is not the installed immutable manifest")
            if (
                params["from_schema"] != manifest.value["from_schema"]
                or params["to_schema"] != manifest.value["to_schema"]
            ):
                raise ContractError(1007, "unsupported Prompt Skill schema transition")
            # Planning is read-only with respect to the plugin database, but
            # it still creates an owned plan that may later authorize apply or
            # verify.  Require the same composed install-lease gate before
            # publishing that plan; otherwise a caller could prepare work
            # outside the Core install lifecycle and replay it after a lease
            # change.
            self._check_install_lease(request)
            plan_id = self._plan_id(request)
            result = {
                "plan_id": plan_id,
                "steps": json.loads(json.dumps(manifest.value["steps"], ensure_ascii=False, allow_nan=False)),
                "backward_compatible": params["from_schema"] == params["to_schema"],
                "requires_verified_backup": params["from_schema"] != params["to_schema"],
            }
            response = self._result(request, result)
            previous = self._plans.get(plan_id)
            self._plans[plan_id] = {
                "request": json.loads(json.dumps(dict(request), ensure_ascii=False, allow_nan=False)),
                "result": json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False)),
                "manifest_hash": manifest_hash,
                "generation_id": request["meta"]["generation_id"],
                "plugin_release_id": request["meta"]["plugin_release_id"],
                "install_operation_id": request["meta"]["install_operation_id"],
                "install_lease_epoch": request["meta"]["install_lease_epoch"],
                "from_schema": params["from_schema"],
                "to_schema": params["to_schema"],
            }
            try:
                self._persist_state()
            except Exception:
                if previous is None:
                    self._plans.pop(plan_id, None)
                else:
                    self._plans[plan_id] = previous
                raise
            return response

        if method == "migration.apply":
            plan = self._require_plan(request, manifest, params["plan_id"])
        else:
            plan = self._require_verify_plan(request, manifest)
        lease = self._check_install_lease(request)
        if method == "migration.apply":
            if self.migration_apply_handler is None:
                raise ContractError(1010, "migration.apply requires the Core migration composition")
            result = self.migration_apply_handler(params, lease)
            response = self._result(request, result)
            if response["result"]["applied_schema"] != plan["to_schema"]:
                raise ContractError(1007, "migration.apply returned a schema outside its owned plan")
        else:
            if self.migration_verify_handler is None:
                raise ContractError(1010, "migration.verify requires the Core migration composition")
            result = self.migration_verify_handler(params, lease)
            response = self._result(request, result)
            if response["result"]["schema_hash"] != params["expected_schema"]:
                raise ContractError(1007, "migration.verify returned a schema different from its request")
        return response

    def _dispatch(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if request.get("method") == PROMPT_SKILL_METHOD:
            return self._execute_v2(request)
        validate_rpc_request(request)
        self._check_meta(request)
        method = request["method"]
        params = request["params"]
        if method == "runtime.handshake":
            if (
                params["host_protocol"] != "1"
                or params["plugin_release_id"] != request["meta"]["plugin_release_id"]
                or (self.release_id is not None and params["plugin_release_id"] != self.release_id)
                or (self._persisted_release_id is not None and params["plugin_release_id"] != self._persisted_release_id)
                or params["generation_id"] != request["meta"]["generation_id"]
            ):
                raise ContractError(1001, "host and worker protocol/release are incompatible")
            response = self._result(request, {
                "plugin_protocol": "1", "plugin_id": PLUGIN_ID, "release_id": params["plugin_release_id"],
                "capabilities": [CAPABILITY_ID], "worker_instance_id": self.worker_instance_id,
            })
            previous_release = self.release_id
            previous_persisted_release = self._persisted_release_id
            previous_generation = self._generation_id
            previous_state = self._state
            previous_ledger = self._ledger
            self.release_id = params["plugin_release_id"]
            self._persisted_release_id = self.release_id
            self._generation_id = params["generation_id"]
            self._ledger = self._ledger_for_generation(self._generation_id)
            self._state = "handshaken"
            try:
                self._persist_state()
            except Exception:
                self.release_id = previous_release
                self._persisted_release_id = previous_persisted_release
                self._generation_id = previous_generation
                self._state = previous_state
                self._ledger = previous_ledger
                raise
            return response
        if method == "runtime.health":
            return self._result(request, {"status": "ok", "details_asset_id": None, "checked_at": _now()})
        if method == "runtime.heartbeat":
            return None
        if method == "capability.describe":
            if params["capability_id"] != CAPABILITY_ID:
                raise ContractError(1010, "unknown Prompt Skill capability")
            return self._result(request, {"descriptor": capability_descriptor(release_id=self.release_id)})
        if method == "runtime.shutdown":
            result = self._result(request, {"accepted": True})
            self._shutdown = True
            self._state = "shutdown"
            return result
        if method == "migration.plan" or method == "migration.apply" or method == "migration.verify":
            return self._migration(request)
        if method == "job.start":
            if params["capability_id"] != CAPABILITY_ID or self.start_handler is None:
                raise ContractError(1010, "job.start requires the P3/Core composition callback")
            response = self._result(request, self.start_handler(params, request["meta"]))
            self._commit_attempt_success(request)
            return response
        if method == "job.resume":
            if params["capability_id"] != CAPABILITY_ID or self.resume_handler is None:
                raise ContractError(1010, "job.resume requires the P3/Core composition callback")
            response = self._result(request, self.resume_handler(params, request["meta"]))
            self._commit_attempt_success(request)
            return response
        if method == "job.cancel":
            if self.cancel_handler is None:
                raise ContractError(1010, "job.cancel requires the P3/Core composition callback")
            response = self._result(request, self.cancel_handler(params, request["meta"]))
            self._commit_attempt_success(request)
            return response
        raise ContractError(1010, f"worker method {method!r} is not implemented by Prompt Skill Runtime")

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Handle one decoded message without breaking the RPC stream."""

        with self._lock:
            try:
                response = self._dispatch(request)
                return response
            except Exception as exc:  # noqa: BLE001 - protocol worker must fail closed
                return self._error(request, exc)

    def handle_frame(self, frame: bytes) -> bytes | None:
        decoder = FrameDecoder()
        messages = decoder.feed(frame)
        if len(messages) != 1 or decoder.buffer:
            raise ContractError(1010, "worker accepts exactly one RPC frame")
        response = self.handle(messages[0])
        return None if response is None else encode_frame(response)


def serve(worker: PromptSkillWorker, *, stdin: Any = None, stdout: Any = None, chunk_size: int = 65536) -> None:
    """Serve framed requests on stdio without logging to stdout."""

    source = sys.stdin.buffer if stdin is None else stdin
    target = sys.stdout.buffer if stdout is None else stdout
    decoder = FrameDecoder()
    read_chunk = getattr(source, "read1", source.read)
    while True:
        # ``BufferedReader.read(n)`` on a Windows pipe may wait for all
        # ``n`` bytes even when one complete RPC frame is already available.
        # A framed worker must consume the currently available bytes so the
        # host can receive the response without closing stdin.
        chunk = read_chunk(chunk_size)
        if not chunk:
            break
        for message in decoder.feed(chunk):
            response = worker.handle(message)
            if response is not None:
                target.write(encode_frame(response))
                target.flush()
    if decoder.buffer:
        raise ContractError(1010, "worker received an incomplete RPC frame at EOF")


def main(*, worker: PromptSkillWorker | None = None) -> None:
    """Run the installable entrypoint with an explicitly stopped composition."""

    source = sys.stdin.buffer
    target = sys.stdout.buffer
    if worker is None:
        worker = PromptSkillWorker()
    serve(worker, stdin=source, stdout=target)


__all__ = ["PromptSkillWorker", "StdioHostPort", "main", "serve"]
