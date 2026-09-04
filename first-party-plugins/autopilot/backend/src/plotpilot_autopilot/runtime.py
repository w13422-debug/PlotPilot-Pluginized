"""Host-only durable Autopilot DAG execution and recovery.

The runtime has no Core import, database handle, local checkpoint file, or
worker registry.  Each stage is delegated to the accepted P3 Host capability
port and each durable transition is materialized as Host-owned immutable
Assets followed by ``host.checkpoint.commit/v1``.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .checkpoint import (
    AutopilotCheckpointError,
    AutopilotIdentity,
    CheckpointEnvelope,
    CheckpointState,
    StageEffect,
    build_checkpoint_envelope,
    parse_canonical_json_bytes,
    recover_checkpoint_envelope,
    sha256_hex,
    validate_identifier,
    validate_sha256,
)
from .dag import DurableDAG, DurableStage

PLUGIN_ID = "com.plotpilot.autopilot"
CAPABILITY_ID = "autopilot.dag.run/v1"
MAX_HOST_ASSET_BYTES = 8 * 1024 * 1024
MAX_HOST_ASSET_PAGES = 16
DEFAULT_HOST_PAGE_SIZE = 1024 * 1024
DEFAULT_MAX_STAGE_POLLS = 128


class AutopilotRuntimeError(RuntimeError):
    """A Host response or durable transition was rejected fail-closed."""


class AutopilotHostPort(Protocol):
    """The P2 Host seam; all durable authority remains behind this port."""

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class HostAsset:
    asset_id: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        validate_identifier(self.asset_id, "asset_id")
        if sha256_hex(self.content) != self.sha256:
            raise AutopilotRuntimeError(
                "Host Asset content hash does not match its bytes"
            )


@dataclass(frozen=True, slots=True)
class AutopilotRunResult:
    """A transient execution result; its checkpoint remains Host-owned."""

    dag_hash: str
    completed_stage_ids: tuple[str, ...]
    stage_effects: tuple[StageEffect, ...]
    checkpoint: CheckpointEnvelope | None

    @property
    def completed(self) -> bool:
        """Whether every emitted effect has a final durable checkpoint."""
        return not self.stage_effects or self.checkpoint is not None

    @property
    def checkpoint_asset_id(self) -> str | None:
        return None if self.checkpoint is None else self.checkpoint.checkpoint_asset_id


class _HostAssets:
    """Strict public Host Asset read/write adapter with no local persistence."""

    def __init__(
        self,
        runtime: AutopilotRuntime,
        *,
        page_size: int = DEFAULT_HOST_PAGE_SIZE,
        max_bytes: int = MAX_HOST_ASSET_BYTES,
        max_pages: int = MAX_HOST_ASSET_PAGES,
    ) -> None:
        if type(page_size) is not int or not 1 <= page_size <= DEFAULT_HOST_PAGE_SIZE:
            raise AutopilotRuntimeError("page_size is outside the Host Asset bound")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_HOST_ASSET_BYTES:
            raise AutopilotRuntimeError("max_bytes is outside the Host Asset bound")
        if type(max_pages) is not int or not 1 <= max_pages <= MAX_HOST_ASSET_PAGES:
            raise AutopilotRuntimeError("max_pages is outside the Host Asset bound")
        self._runtime = runtime
        self._page_size = page_size
        self._max_bytes = max_bytes
        self._max_pages = max_pages

    def read(self, asset_id: str) -> HostAsset:
        validate_identifier(asset_id, "asset_id")
        offset = 0
        pages = 0
        total = 0
        chunks: list[bytes] = []
        while True:
            pages += 1
            if pages > self._max_pages:
                raise AutopilotRuntimeError(
                    "Host Asset exceeded the cumulative page limit"
                )
            result = self._runtime._call(
                "host.asset.read/v1",
                {"asset_id": asset_id, "offset": offset, "length": self._page_size},
                {"base64_chunk", "next_offset", "content_hash"},
            )
            encoded = result["base64_chunk"]
            if not isinstance(encoded, str):
                raise AutopilotRuntimeError(
                    "Host Asset page base64_chunk must be a string"
                )
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise AutopilotRuntimeError(
                    "Host Asset page is not valid base64"
                ) from exc
            if len(chunk) > self._page_size or total + len(chunk) > self._max_bytes:
                raise AutopilotRuntimeError(
                    "Host Asset exceeded the cumulative byte limit"
                )
            if result["content_hash"] != sha256_hex(chunk):
                raise AutopilotRuntimeError("Host Asset page hash is invalid")
            next_offset = result["next_offset"]
            if next_offset is None:
                chunks.append(chunk)
                break
            if (
                type(next_offset) is not int
                or next_offset != offset + len(chunk)
                or next_offset <= offset
            ):
                raise AutopilotRuntimeError("Host Asset pages are not contiguous")
            chunks.append(chunk)
            total += len(chunk)
            offset = next_offset
        content = b"".join(chunks)
        return HostAsset(asset_id=asset_id, content=content, sha256=sha256_hex(content))

    def write(self, content: bytes, *, operation_key: str, mime: str) -> HostAsset:
        validate_identifier(operation_key, "operation_key")
        if not isinstance(content, bytes):
            raise AutopilotRuntimeError("Host Asset content must be bytes")
        if len(content) > self._max_bytes:
            raise AutopilotRuntimeError(
                "Host Asset upload exceeds the cumulative byte limit"
            )
        if not isinstance(mime, str) or not mime:
            raise AutopilotRuntimeError("Host Asset MIME must be non-empty")
        digest = sha256_hex(content)
        upload_id = _operation_key("upload", operation_key)
        chunks = (
            [b""]
            if not content
            else [
                content[index : index + self._page_size]
                for index in range(0, len(content), self._page_size)
            ]
        )
        if len(chunks) > self._max_pages:
            raise AutopilotRuntimeError(
                "Host Asset upload exceeds the cumulative page limit"
            )
        accepted = 0
        asset_id: str | None = None
        for index, chunk in enumerate(chunks):
            final = index == len(chunks) - 1
            result = self._runtime._call(
                "host.asset.create/v1",
                {
                    "operation_key": _operation_key(
                        "asset-chunk", operation_key, str(accepted)
                    ),
                    "upload_id": upload_id,
                    "offset": accepted,
                    "mime": mime,
                    "total_size": len(content),
                    "expected_hash": digest,
                    "chunk_hash": sha256_hex(chunk),
                    "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                    "final": final,
                },
                {"upload_id", "accepted_bytes", "completed", "asset_id"},
            )
            if result["upload_id"] != upload_id:
                raise AutopilotRuntimeError(
                    "Host Asset upload acknowledgement changed upload_id"
                )
            if type(result["accepted_bytes"]) is not int or result[
                "accepted_bytes"
            ] != accepted + len(chunk):
                raise AutopilotRuntimeError(
                    "Host Asset upload acknowledgement is not contiguous"
                )
            if type(result["completed"]) is not bool:
                raise AutopilotRuntimeError(
                    "Host Asset upload acknowledgement completed must be boolean"
                )
            returned_asset_id = result["asset_id"]
            if not final:
                if result["completed"] or returned_asset_id is not None:
                    raise AutopilotRuntimeError(
                        "Host Asset upload completed before its final chunk"
                    )
            else:
                if not result["completed"]:
                    raise AutopilotRuntimeError(
                        "Host Asset upload did not complete its final chunk"
                    )
                validate_identifier(returned_asset_id, "Host Asset result asset_id")
                asset_id = returned_asset_id
            accepted = result["accepted_bytes"]
        if accepted != len(content) or asset_id is None:
            raise AutopilotRuntimeError(
                "Host Asset upload did not return a complete immutable Asset"
            )
        return HostAsset(asset_id=asset_id, content=content, sha256=digest)


def _operation_key(kind: str, *parts: str) -> str:
    material = "\n".join((kind, *parts)).encode("utf-8")
    return f"autopilot-{kind}-{sha256(material).hexdigest()[:48]}"


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise AutopilotRuntimeError(f"{field} must be a non-negative integer")
    return value


class AutopilotRuntime:
    """Execute a frozen DAG through only the accepted Host RPC operations."""

    def __init__(
        self,
        host: AutopilotHostPort,
        *,
        plugin_release_id: str,
        max_stage_polls: int = DEFAULT_MAX_STAGE_POLLS,
    ) -> None:
        if host is None or not callable(getattr(host, "call", None)):
            raise TypeError("AutopilotRuntime requires a P2 Host call port")
        validate_sha256(plugin_release_id, "plugin_release_id")
        if (
            type(max_stage_polls) is not int
            or not 1 <= max_stage_polls <= DEFAULT_MAX_STAGE_POLLS
        ):
            raise ValueError("max_stage_polls is outside the durable runtime bound")
        self._host = host
        self.plugin_release_id = plugin_release_id
        self._max_stage_polls = max_stage_polls
        self._assets = _HostAssets(self)

    def _call(
        self, method: str, params: Mapping[str, object], expected_fields: set[str]
    ) -> dict[str, object]:
        try:
            result = self._host.call(method, dict(params))
        except AutopilotRuntimeError:
            raise
        except Exception as exc:
            raise AutopilotRuntimeError(
                f"{method} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(result, Mapping):
            raise AutopilotRuntimeError(f"{method} returned a non-object")
        actual = set(result)
        if actual != expected_fields:
            raise AutopilotRuntimeError(
                f"{method} result fields are not closed: missing={sorted(expected_fields - actual)}, extra={sorted(actual - expected_fields)}"
            )
        return dict(result)

    def _recover(
        self, checkpoint_asset_id: str, *, identity: AutopilotIdentity, dag: DurableDAG
    ) -> CheckpointEnvelope:
        checkpoint_asset = self._assets.read(checkpoint_asset_id)
        checkpoint_value = parse_canonical_json_bytes(checkpoint_asset.content)
        if not isinstance(checkpoint_value, Mapping):
            raise AutopilotRuntimeError("checkpoint Asset must contain an object")
        state_asset_id = checkpoint_value.get("state_asset_id")
        try:
            validate_identifier(state_asset_id, "checkpoint state_asset_id")
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc
        state_asset = self._assets.read(state_asset_id)
        state_value = parse_canonical_json_bytes(state_asset.content)
        if not isinstance(state_value, Mapping):
            raise AutopilotRuntimeError(
                "checkpoint runtime-state Asset must contain an object"
            )
        try:
            return recover_checkpoint_envelope(
                checkpoint_value,
                state_value,
                state_asset_id=state_asset_id,
                checkpoint_asset_id=checkpoint_asset_id,
                identity=identity,
                dag_hash=dag.dag_hash,
                total_units=len(dag.ordered_stages),
            )
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc

    def _invoke_stage(
        self, stage: DurableStage, *, identity: AutopilotIdentity, dag_hash: str
    ) -> StageEffect:
        operation_key = _operation_key(
            "stage",
            identity.workspace_id,
            identity.job_id,
            identity.step_id,
            identity.attempt_id,
            str(identity.lease_epoch),
            identity.plugin_release_id,
            dag_hash,
            stage.stage_id,
        )
        invoke = self._call(
            "host.capability.invoke/v1",
            {
                "operation_key": operation_key,
                "binding_id": stage.binding_id,
                "input_asset_id": stage.input_asset_id,
                "parameters_asset_id": stage.parameters_asset_id,
                "expected_result_contract": stage.expected_result_contract,
                "propagate_cancel": stage.propagate_cancel,
            },
            {
                "accepted",
                "child_job_id",
                "child_step_id",
                "child_run_snapshot_asset_id",
                "child_run_snapshot_hash",
                "child_result_contract",
                "child_job_event_seq",
            },
        )
        if type(invoke["accepted"]) is not bool or not invoke["accepted"]:
            raise AutopilotRuntimeError("Host rejected the durable stage invocation")
        for field in ("child_job_id", "child_step_id", "child_run_snapshot_asset_id"):
            try:
                validate_identifier(invoke[field], field)
            except AutopilotCheckpointError as exc:
                raise AutopilotRuntimeError(str(exc)) from exc
        try:
            validate_sha256(
                invoke["child_run_snapshot_hash"], "child_run_snapshot_hash"
            )
        except AutopilotCheckpointError as exc:
            raise AutopilotRuntimeError(str(exc)) from exc
        if invoke["child_result_contract"] != stage.expected_result_contract:
            raise AutopilotRuntimeError(
                "Host stage result contract drifted from the frozen DAG"
            )
        after_event_seq = _nonnegative_int(
            invoke["child_job_event_seq"], "child_job_event_seq"
        )

        terminal_result: dict[str, object] | None = None
        for _ in range(self._max_stage_polls):
            poll = self._call(
                "host.capability.poll/v1",
                {
                    "child_job_id": invoke["child_job_id"],
                    "after_job_event_seq": after_event_seq,
                },
                {
                    "job_snapshot_asset_id",
                    "job_event_page_asset_id",
                    "next_job_event_seq",
                    "terminal",
                    "result_bundle_asset_id",
                    "provenance_receipt_id",
                },
            )
            next_event_seq = _nonnegative_int(
                poll["next_job_event_seq"], "next_job_event_seq"
            )
            if next_event_seq < after_event_seq:
                raise AutopilotRuntimeError("Host stage event sequence moved backwards")
            if type(poll["terminal"]) is not bool:
                raise AutopilotRuntimeError("Host stage terminal flag must be boolean")
            if poll["terminal"]:
                terminal_result = poll
                break
            if next_event_seq == after_event_seq:
                raise AutopilotRuntimeError("Host stage poll made no durable progress")
            after_event_seq = next_event_seq
        if terminal_result is None:
            raise AutopilotRuntimeError(
                "Host stage did not become terminal within the configured bound"
            )
        for field in ("result_bundle_asset_id", "provenance_receipt_id"):
            try:
                validate_identifier(terminal_result[field], field)
            except AutopilotCheckpointError as exc:
                raise AutopilotRuntimeError(
                    f"terminal stage omitted durable {field}"
                ) from exc
        return StageEffect(
            stage_id=stage.stage_id,
            operation_key=operation_key,
            child_job_id=invoke["child_job_id"],  # type: ignore[arg-type]
            child_step_id=invoke["child_step_id"],  # type: ignore[arg-type]
            child_run_snapshot_asset_id=invoke["child_run_snapshot_asset_id"],  # type: ignore[arg-type]
            child_run_snapshot_hash=invoke["child_run_snapshot_hash"],  # type: ignore[arg-type]
            result_bundle_asset_id=terminal_result["result_bundle_asset_id"],  # type: ignore[arg-type]
            provenance_receipt_id=terminal_result["provenance_receipt_id"],  # type: ignore[arg-type]
            child_result_contract=stage.expected_result_contract,
        )

    def _commit_checkpoint(
        self, state: CheckpointState, *, total_units: int
    ) -> CheckpointEnvelope:
        state_asset = self._assets.write(
            state.json_bytes,
            operation_key=_operation_key("state", state.checkpoint_id),
            mime="application/json",
        )
        envelope = build_checkpoint_envelope(
            state,
            state_asset_id=state_asset.asset_id,
            total_units=total_units,
        )
        checkpoint_asset = self._assets.write(
            envelope.json_bytes,
            operation_key=_operation_key("checkpoint-asset", state.checkpoint_id),
            mime="application/json",
        )
        committed = self._call(
            "host.checkpoint.commit/v1",
            {
                "operation_key": _operation_key(
                    "checkpoint-commit", state.checkpoint_id
                ),
                "checkpoint_asset_id": checkpoint_asset.asset_id,
            },
            {
                "accepted",
                "checkpoint_id",
                "completed_units",
                "total_units",
                "job_event_seq",
            },
        )
        if type(committed["accepted"]) is not bool or not committed["accepted"]:
            raise AutopilotRuntimeError(
                "Host rejected the durable Autopilot checkpoint"
            )
        if committed["checkpoint_id"] != state.checkpoint_id:
            raise AutopilotRuntimeError(
                "Host checkpoint acknowledgement changed checkpoint_id"
            )
        if committed["completed_units"] != len(state.completed_stages):
            raise AutopilotRuntimeError(
                "Host checkpoint acknowledgement changed completed_units"
            )
        if committed["total_units"] != total_units:
            raise AutopilotRuntimeError(
                "Host checkpoint acknowledgement changed total_units"
            )
        _nonnegative_int(committed["job_event_seq"], "checkpoint job_event_seq")
        return envelope.with_checkpoint_asset_id(checkpoint_asset.asset_id)

    def run(
        self,
        identity: AutopilotIdentity,
        dag: DurableDAG,
        *,
        checkpoint_asset_id: str | None = None,
    ) -> AutopilotRunResult:
        """Run or resume a frozen DAG without retaining local authoritative state."""
        if not isinstance(identity, AutopilotIdentity):
            raise TypeError("identity must be an AutopilotIdentity")
        if not isinstance(dag, DurableDAG):
            raise TypeError("dag must be a DurableDAG")
        if identity.plugin_release_id != self.plugin_release_id:
            raise AutopilotRuntimeError(
                "runtime release does not match the frozen Autopilot identity"
            )
        if checkpoint_asset_id is not None:
            try:
                validate_identifier(checkpoint_asset_id, "checkpoint_asset_id")
            except AutopilotCheckpointError as exc:
                raise AutopilotRuntimeError(str(exc)) from exc
            checkpoint = self._recover(checkpoint_asset_id, identity=identity, dag=dag)
            effects = list(checkpoint.state.completed_stages)
            previous_hash: str | None = checkpoint.checkpoint_hash
        else:
            checkpoint = None
            effects = []
            previous_hash = None

        ordered = dag.ordered_stages
        completed_ids = tuple(effect.stage_id for effect in effects)
        expected_prefix = tuple(stage.stage_id for stage in ordered[: len(effects)])
        if completed_ids != expected_prefix:
            raise AutopilotRuntimeError(
                "checkpoint completed stages are not the current DAG prefix"
            )

        for stage in ordered[len(effects) :]:
            completed_set = {effect.stage_id for effect in effects}
            if not set(stage.depends_on).issubset(completed_set):
                raise AutopilotRuntimeError(
                    "DAG dependency is not durably checkpointed before execution"
                )
            effect = self._invoke_stage(stage, identity=identity, dag_hash=dag.dag_hash)
            effects.append(effect)
            try:
                state = CheckpointState.build(
                    identity,
                    dag_hash=dag.dag_hash,
                    previous_checkpoint_hash=previous_hash,
                    completed_stages=tuple(effects),
                )
            except AutopilotCheckpointError as exc:
                raise AutopilotRuntimeError(str(exc)) from exc
            checkpoint = self._commit_checkpoint(state, total_units=len(ordered))
            previous_hash = checkpoint.checkpoint_hash

        return AutopilotRunResult(
            dag_hash=dag.dag_hash,
            completed_stage_ids=tuple(effect.stage_id for effect in effects),
            stage_effects=tuple(effects),
            checkpoint=checkpoint,
        )

    resume = run


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    """Describe the single Host-composed Autopilot capability without starting it."""
    validate_sha256(release_id, "release_id")
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "autopilot-dag/v1",
        "output_schema": "checkpoint/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel"],
        "deterministic": False,
        "accepted_data_formats": [],
    }


def main() -> None:
    """Installable entrypoint; P2 injects the Host port rather than a local daemon."""
    raise RuntimeError(
        "AutopilotRuntime must be constructed by the PlotPilot Host composition"
    )


__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_MAX_STAGE_POLLS",
    "MAX_HOST_ASSET_BYTES",
    "PLUGIN_ID",
    "AutopilotHostPort",
    "AutopilotRunResult",
    "AutopilotRuntime",
    "AutopilotRuntimeError",
    "HostAsset",
    "capability_descriptor",
    "main",
]
