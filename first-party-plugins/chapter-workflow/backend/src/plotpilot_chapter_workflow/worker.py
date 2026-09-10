"""Installable Host-RPC adapter for the accepted Chapter Workflow domain.

This module owns only package/input adaptation.  Composition injects the
already-authoritative Host and Broker ports into ``ChapterWorkflow``; the worker
never opens a Core repository, database, or a second publication authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

try:
    from plotpilot_plugin_sdk import ContractError, ErrorCode
    from plotpilot_plugin_sdk.stdio_worker import FramedStdioWorker, WorkerContext
except ModuleNotFoundError:  # pragma: no cover - repository-source fallback
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[5] / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from plotpilot_plugin_sdk import ContractError, ErrorCode
    from plotpilot_plugin_sdk.stdio_worker import FramedStdioWorker, WorkerContext

from .planning import ContextSource, SkillRef, freeze_context_plan
from .workflow import (
    ChapterOperation,
    ChapterRequest,
    ChapterTarget,
    ChapterWorkflow,
    ProducerRef,
    RewriteSelection,
    SessionSnapshot,
)

PLUGIN_ID = "com.plotpilot.chapter-workflow"
PLUGIN_VERSION = "1.0.0"
CAPABILITY_OPERATIONS: dict[str, ChapterOperation] = {
    "writing.chapter.draft/v1": ChapterOperation.GENERATE,
    "writing.chapter.continue/v1": ChapterOperation.CONTINUE,
    "writing.chapter.rewrite/v1": ChapterOperation.REWRITE,
}
CAPABILITY_IDS = tuple(CAPABILITY_OPERATIONS)


class ChapterWorkflowWorkerError(ContractError):
    """Fail-closed package and Host/Broker composition boundary error."""


class ChapterWorkflowPort(Protocol):
    """The reviewed domain interface; its ports remain composition-owned."""

    def start(self, request: ChapterRequest) -> SessionSnapshot: ...

    def poll(self, session_id: str) -> SessionSnapshot: ...

    def pause(self, session_id: str) -> SessionSnapshot: ...

    def cancel(self, session_id: str) -> SessionSnapshot: ...


class ChapterWorkflowFactory(Protocol):
    """P0/P3 composition injects a workflow bound to Host and Broker ports."""

    def __call__(self, context: WorkerContext) -> ChapterWorkflowPort: ...


def _mapping(
    value: object, *, fields: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"{label} must contain exactly {sorted(fields)!r}",
        )
    return value


def _sequence(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"{label} must be a JSON array",
        )
    return value


def _context_source(value: object) -> ContextSource:
    fields = frozenset(
        {"source_id", "revision_id", "kind", "content", "content_hash"}
    )
    item = _mapping(value, fields=fields, label="context source")
    try:
        return ContextSource(
            item["source_id"],
            item["revision_id"],
            item["kind"],
            item["content"],
            item["content_hash"],
        )
    except (TypeError, ValueError) as exc:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"invalid context source: {exc}",
        ) from exc


def _skill_ref(value: object) -> SkillRef:
    required = {"order", "skill_id", "release_id", "package_hash"}
    optional = required | {"parameters_asset_id"}
    if not isinstance(value, Mapping) or set(value) not in {required, optional}:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "Skill reference has an invalid field set",
        )
    try:
        return SkillRef(
            value["order"],
            value["skill_id"],
            value["release_id"],
            value["package_hash"],
            value.get("parameters_asset_id"),
        )
    except (TypeError, ValueError) as exc:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"invalid Skill reference: {exc}",
        ) from exc


def _rewrite_selection(value: object) -> RewriteSelection:
    fields = frozenset(
        {"start_codepoint", "end_codepoint", "selected_text", "selected_hash"}
    )
    selection = _mapping(value, fields=fields, label="rewrite selection")
    try:
        return RewriteSelection(
            selection["start_codepoint"],
            selection["end_codepoint"],
            selection["selected_text"],
            selection["selected_hash"],
        )
    except (TypeError, ValueError) as exc:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"invalid rewrite selection: {exc}",
        ) from exc


def chapter_request_from_payload(
    capability_id: str, payload: Mapping[str, object]
) -> ChapterRequest:
    """Decode one exact package payload into the accepted typed request.

    The operation is derived exclusively from the manifest capability.  All
    request facts then remain subject to the domain's existing validation.
    """

    operation = CAPABILITY_OPERATIONS.get(capability_id)
    if operation is None:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "Chapter Workflow capability is not exposed by this package",
        )
    expected = {
        "operation_key",
        "target",
        "context_sources",
        "skills",
        "producer",
        "instruction",
    }
    if operation is ChapterOperation.REWRITE:
        expected.add("rewrite_selection")
    if set(payload) != expected:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "Chapter Workflow payload field set does not match its capability",
        )
    target = _mapping(
        payload["target"],
        fields=frozenset(
            {"workspace_id", "document_id", "base_revision_id", "base_content_hash"}
        ),
        label="chapter target",
    )
    producer = _mapping(
        payload["producer"],
        fields=frozenset(
            {
                "plugin_id",
                "release_id",
                "job_id",
                "step_id",
                "attempt_id",
                "lease_epoch",
                "provenance_receipt_id",
                "input_snapshot_hash",
            }
        ),
        label="chapter producer",
    )
    try:
        sources = tuple(
            _context_source(value)
            for value in _sequence(payload["context_sources"], label="context_sources")
        )
        skills = tuple(
            _skill_ref(value) for value in _sequence(payload["skills"], label="skills")
        )
        return ChapterRequest(
            payload["operation_key"],
            operation,
            ChapterTarget(
                target["workspace_id"],
                target["document_id"],
                target["base_revision_id"],
                target["base_content_hash"],
            ),
            freeze_context_plan(operation.capability_id, sources, skills),
            ProducerRef(
                producer["plugin_id"],
                producer["release_id"],
                producer["job_id"],
                producer["step_id"],
                producer["attempt_id"],
                producer["lease_epoch"],
                producer["provenance_receipt_id"],
                producer["input_snapshot_hash"],
            ),
            payload["instruction"],
            _rewrite_selection(payload["rewrite_selection"])
            if operation is ChapterOperation.REWRITE
            else None,
        )
    except (TypeError, ValueError) as exc:
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"invalid Chapter Workflow payload: {exc}",
        ) from exc


class ChapterWorkflowWorker:
    """Small adapter that delegates all workflow state transitions unchanged."""

    def __init__(self, workflow: ChapterWorkflowPort) -> None:
        if not isinstance(workflow, ChapterWorkflow):
            raise TypeError("Chapter Workflow worker requires the accepted workflow")
        self._workflow = workflow

    def dispatch(
        self, capability_id: str, payload: Mapping[str, object]
    ) -> SessionSnapshot:
        if not isinstance(payload, Mapping):
            raise ChapterWorkflowWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Chapter Workflow payload must be an object",
            )
        return self._workflow.start(
            chapter_request_from_payload(capability_id, payload)
        )

    def control(self, action: str, session_id: str) -> SessionSnapshot:
        controls = {
            "poll": self._workflow.poll,
            "pause": self._workflow.pause,
            "cancel": self._workflow.cancel,
        }
        handler = controls.get(action)
        if handler is None or not isinstance(session_id, str) or not session_id:
            raise ChapterWorkflowWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Chapter Workflow control request is malformed",
            )
        return handler(session_id)


def capability_descriptor(
    capability_id: str, *, release_id: str
) -> dict[str, object]:
    if capability_id not in CAPABILITY_OPERATIONS:
        raise ValueError("unknown Chapter Workflow capability")
    return {
        "schema": "capability-provider/v1",
        "capability_id": capability_id,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "run-snapshot/v1",
        "output_schema": "result-bundle/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel", "validate"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


def _payload_from_context(context: WorkerContext) -> Mapping[str, object]:
    snapshot = context.run_snapshot
    if not isinstance(snapshot, Mapping):
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "Chapter Workflow requires a bound RunSnapshot",
        )
    payload = snapshot.get("chapter_workflow_request")
    if not isinstance(payload, Mapping):
        raise ChapterWorkflowWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "RunSnapshot omits the Chapter Workflow request payload",
        )
    return payload


def build_production_worker(
    workflow_factory: ChapterWorkflowFactory | None = None,
) -> FramedStdioWorker:
    """Create the only no-argument Host-RPC entrypoint composition.

    Until P0/P3 injects its accepted Host/Broker factory, job dispatch fails
    closed rather than reaching into Core-owned storage.
    """

    worker = FramedStdioWorker(plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)

    for capability_id, operation in CAPABILITY_OPERATIONS.items():
        def start(
            _params: Mapping[str, Any],
            context: WorkerContext,
            *,
            capability: str = capability_id,
            expected_operation: ChapterOperation = operation,
        ) -> Mapping[str, object]:
            if workflow_factory is None or context.identity is None:
                raise ChapterWorkflowWorkerError(
                    ErrorCode.INVALID_TRANSITION,
                    "Chapter Workflow requires injected Host/Broker composition",
                )
            request = chapter_request_from_payload(
                capability, _payload_from_context(context)
            )
            if request.operation is not expected_operation:
                raise ChapterWorkflowWorkerError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "RunSnapshot Chapter Workflow operation is capability-mismatched",
                )
            snapshot = ChapterWorkflowWorker(workflow_factory(context)).dispatch(
                capability, _payload_from_context(context)
            )
            return {
                "accepted": True,
                "worker_run_id": context.identity.operation_id,
                "provenance_receipt_id": request.producer.provenance_receipt_id,
                "output_streams": [
                    {
                        "stream_id": snapshot.session_id,
                        "kind": "chapter-workflow-session/v1",
                    }
                ],
            }

        worker.register_domain(
            capability_id,
            descriptor=lambda release_id, capability=capability_id: capability_descriptor(
                capability, release_id=release_id
            ),
            operations=(capability_id,),
            start=start,
            resume=start,
        )
    return worker


def main() -> None:
    """Run the sole package entrypoint via the accepted framed stdio runtime."""

    build_production_worker().serve()


__all__ = [
    "CAPABILITY_IDS",
    "CAPABILITY_OPERATIONS",
    "PLUGIN_ID",
    "ChapterWorkflowFactory",
    "ChapterWorkflowPort",
    "ChapterWorkflowWorker",
    "ChapterWorkflowWorkerError",
    "build_production_worker",
    "capability_descriptor",
    "chapter_request_from_payload",
    "main",
]