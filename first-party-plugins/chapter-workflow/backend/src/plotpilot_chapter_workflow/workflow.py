"""Real, source-only Chapter Workflow orchestration.

The workflow owns deterministic assembly and state transitions only.  Every
durable effect crosses an injected SDK/Broker port: immutable Asset creation,
ResultBundle persistence, Candidate staging, typed Publication, and P5 Story
State proposal.  It cannot write chapter text or Story State directly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Sequence

from .planning import FrozenContextPlan, SkillRef


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkflowError(ValueError):
    """Fail-closed request, port, event, or state error."""


def _fail(message: str) -> NoReturn:
    raise WorkflowError(message)


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail(f"{label} is not a v1 identity")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        _fail(f"{label} must be lowercase SHA-256")
    return value


def _require_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(f"{label} must be {'text' if allow_empty else 'non-blank text'}")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkflowError(f"{label} must be strict UTF-8") from exc
    return value


def _stable_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise WorkflowError(f"value is not deterministic JSON: {exc}") from exc


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be a mapping")
    return value


class ChapterOperation(str, Enum):
    GENERATE = "generate"
    CONTINUE = "continue"
    REWRITE = "rewrite"

    @property
    def capability_id(self) -> str:
        return {
            self.GENERATE: "writing.chapter.draft/v1",
            self.CONTINUE: "writing.chapter.continue/v1",
            self.REWRITE: "writing.chapter.rewrite/v1",
        }[self]

    @property
    def mutation_mode(self) -> str:
        # Rewrite output is the complete replacement chapter.  The frozen
        # selection is an input/provenance constraint, not an ad-hoc patch
        # schema.  This stays within the current P1 document mutation seam.
        return {self.GENERATE: "replace", self.CONTINUE: "append_text", self.REWRITE: "replace"}[self]


class SessionState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChapterTarget:
    workspace_id: str
    document_id: str
    base_revision_id: str
    base_content_hash: str

    def __post_init__(self) -> None:
        _require_id(self.workspace_id, "workspace_id")
        _require_id(self.document_id, "document_id")
        _require_id(self.base_revision_id, "base_revision_id")
        _require_hash(self.base_content_hash, "base_content_hash")


@dataclass(frozen=True, slots=True)
class RewriteSelection:
    start_codepoint: int
    end_codepoint: int
    selected_text: str
    selected_hash: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start_codepoint, int)
            or isinstance(self.start_codepoint, bool)
            or not isinstance(self.end_codepoint, int)
            or isinstance(self.end_codepoint, bool)
            or self.start_codepoint < 0
            or self.end_codepoint <= self.start_codepoint
        ):
            _fail("rewrite selection must be a non-empty codepoint range")
        _require_text(self.selected_text, "selected_text")
        _require_hash(self.selected_hash, "selected_hash")
        if sha256(self.selected_text.encode("utf-8")).hexdigest() != self.selected_hash:
            _fail("rewrite selection hash mismatch")


@dataclass(frozen=True, slots=True)
class ProducerRef:
    plugin_id: str
    release_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    provenance_receipt_id: str
    input_snapshot_hash: str

    def __post_init__(self) -> None:
        for name in ("plugin_id", "job_id", "step_id", "attempt_id", "provenance_receipt_id"):
            _require_id(getattr(self, name), name)
        _require_hash(self.release_id, "release_id")
        _require_hash(self.input_snapshot_hash, "input_snapshot_hash")
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            _fail("lease_epoch must be a positive integer")


@dataclass(frozen=True, slots=True)
class ChapterRequest:
    operation_key: str
    operation: ChapterOperation
    target: ChapterTarget
    context_plan: FrozenContextPlan
    producer: ProducerRef
    instruction: str
    rewrite_selection: RewriteSelection | None = None

    def __post_init__(self) -> None:
        _require_id(self.operation_key, "operation_key")
        if not isinstance(self.operation, ChapterOperation):
            _fail("operation must be ChapterOperation")
        _require_text(self.instruction, "instruction")
        if self.context_plan.operation != self.operation.capability_id:
            _fail("context plan operation does not match chapter operation")
        for skill in self.context_plan.skills:
            _require_hash(skill.release_id, "Skill release_id")
        if self.operation is ChapterOperation.REWRITE:
            if self.rewrite_selection is None:
                _fail("rewrite requires an exact selection")
        elif self.rewrite_selection is not None:
            _fail("rewrite selection is only valid for rewrite")


@dataclass(frozen=True, slots=True)
class BrokerInvocation:
    invocation_key: str
    operation_key: str
    capability_id: str
    input_asset_id: str
    input_hash: str
    expected_result_contract: str
    result_bundle_id: str
    result_item_id: str
    phase: str
    skill: SkillRef | None = None

    def __post_init__(self) -> None:
        for name in ("invocation_key", "operation_key", "capability_id", "input_asset_id"):
            _require_id(getattr(self, name), name)
        _require_id(self.result_bundle_id, "result_bundle_id")
        _require_id(self.result_item_id, "result_item_id")
        _require_hash(self.input_hash, "input_hash")
        if self.expected_result_contract != "candidate-batch/v1":
            _fail("chapter invocation must request candidate-batch/v1")
        if self.phase not in {"raw", "skill"}:
            _fail("Broker phase must be raw or skill")
        if (self.phase == "skill") != (self.skill is not None):
            _fail("Skill phase/identity binding is invalid")


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    event_seq: int
    kind: str
    chunk: bytes = b""
    prefix_size: int | None = None
    prefix_hash: str | None = None
    error: str | None = None
    skill_chain_ref: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_seq, int) or isinstance(self.event_seq, bool) or self.event_seq < 1:
            _fail("Broker event_seq must be a positive integer")
        if self.kind not in {"acked", "completed", "paused", "cancelled", "failed"}:
            _fail("unknown Broker event kind")
        if not isinstance(self.chunk, (bytes, bytearray, memoryview)):
            _fail("Broker chunk must be bytes")
        object.__setattr__(self, "chunk", bytes(self.chunk))
        if self.kind == "acked":
            if not self.chunk:
                _fail("acked Broker event requires a non-empty chunk")
            if not isinstance(self.prefix_size, int) or isinstance(self.prefix_size, bool) or self.prefix_size < 1:
                _fail("acked Broker event requires prefix_size")
            _require_hash(self.prefix_hash, "Broker prefix_hash")
            if self.error is not None:
                _fail("acked Broker event cannot contain error")
        else:
            if self.chunk or self.prefix_size is not None:
                _fail("terminal Broker event cannot carry an unacknowledged chunk")
            if self.prefix_hash is not None:
                _require_hash(self.prefix_hash, "Broker terminal prefix_hash")
            if self.kind == "failed":
                _require_text(self.error, "Broker failure error")
            elif self.error is not None:
                _fail("non-failed terminal event cannot contain error")
        if self.skill_chain_ref is not None:
            object.__setattr__(self, "skill_chain_ref", MappingProxyType(dict(self.skill_chain_ref)))
            if self.kind != "completed":
                _fail("Skill chain evidence is valid only on completion")


@dataclass(frozen=True, slots=True)
class ChapterCandidate:
    candidate_id: str
    bundle_id: str
    payload_asset_id: str
    content: bytes
    content_hash: str
    status: str
    operation: ChapterOperation
    target: ChapterTarget
    source_candidate_ids: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return self.status == "partial"


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    publication_id: str
    candidate_id: str
    workspace_id: str
    entity_id: str
    revision_id: str
    content_hash: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SettlementResult:
    chapter_publication: PublicationReceipt
    story_state_candidate_ids: tuple[str, ...]
    bundle_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    state: SessionState
    phase: str
    skill_index: int
    invocation_id: str | None
    last_event_seq: int
    acked_size: int
    acked_prefix_hash: str
    candidate: ChapterCandidate | None
    error: str | None


@dataclass(slots=True)
class _Session:
    session_id: str
    request: ChapterRequest
    state: SessionState
    phase: str
    skill_index: int
    invocation_id: str | None
    result_bundle_id: str
    result_item_id: str
    last_event_seq: int = 0
    phase_input: bytes = b""
    acked: bytearray = field(default_factory=bytearray)
    candidate: ChapterCandidate | None = None
    error: str | None = None
    skill_chain_refs: list[Mapping[str, Any]] = field(default_factory=list)


class ChapterWorkflow:
    """State machine for one or more isolated chapter sessions."""

    def __init__(self, *, core: object, broker: object, result_bundles: object, publication: object | None = None, story_state: object | None = None) -> None:
        self._core = core
        self._broker = broker
        self._result_bundles = result_bundles
        self._publication = publication
        self._story_state = story_state
        self._sessions: dict[str, _Session] = {}
        self._operation_sessions: dict[str, str] = {}
        self._publication_receipts: dict[str, PublicationReceipt] = {}
        self._accepted_publications: dict[str, PublicationReceipt] = {}
        self._settlements: dict[str, SettlementResult] = {}

    def start(self, request: ChapterRequest) -> SessionSnapshot:
        existing_id = self._operation_sessions.get(request.operation_key)
        if existing_id is not None:
            existing = self._sessions[existing_id]
            if existing.request != request:
                _fail("operation_key was reused with a different chapter request")
            return self._snapshot(existing)
        context_bytes = self._assemble_context(request)
        asset_id = self._create_asset(context_bytes, mime="application/vnd.plotpilot.chapter-context+json")
        session_hash = sha256(_stable_bytes({
            "operation_key": request.operation_key,
            "workspace_id": request.target.workspace_id,
            "document_id": request.target.document_id,
            "snapshot_hash": request.producer.input_snapshot_hash,
        })).hexdigest()
        session_id = f"chapter-session-{session_hash}"
        bundle_id = f"chapter-bundle-{session_hash}"
        item_id = f"chapter-item-{session_hash}"
        session = _Session(
            session_id,
            request,
            SessionState.RUNNING,
            "raw",
            -1,
            None,
            bundle_id,
            item_id,
            phase_input=context_bytes,
        )
        invocation = BrokerInvocation(
            invocation_key=f"{request.operation_key}:raw",
            operation_key=request.operation_key,
            capability_id=request.operation.capability_id,
            input_asset_id=asset_id,
            input_hash=sha256(context_bytes).hexdigest(),
            expected_result_contract="candidate-batch/v1",
            result_bundle_id=bundle_id,
            result_item_id=item_id,
            phase="raw",
        )
        session.invocation_id = self._broker_start(invocation)
        self._sessions[session_id] = session
        self._operation_sessions[request.operation_key] = session_id
        return self._snapshot(session)

    def poll(self, session_id: str) -> SessionSnapshot:
        session = self._session(session_id)
        if session.state is not SessionState.RUNNING:
            return self._snapshot(session)
        events = self._call_events("poll", session.invocation_id, session.last_event_seq)
        self._apply_events(session, events)
        return self._snapshot(session)

    def pause(self, session_id: str) -> SessionSnapshot:
        return self._control(session_id, "pause")

    def cancel(self, session_id: str) -> SessionSnapshot:
        return self._control(session_id, "cancel")

    def accept(self, session_id: str, *, accepted_by: str, publication_operation_key: str) -> PublicationReceipt:
        session = self._session(session_id)
        if session.state is not SessionState.COMPLETED or session.candidate is None or session.candidate.partial:
            _fail("only a complete chapter Candidate can be accepted")
        _require_id(accepted_by, "accepted_by")
        _require_id(publication_operation_key, "publication_operation_key")
        previous = self._publication_receipts.get(publication_operation_key)
        if previous is not None:
            if previous.candidate_id != session.candidate.candidate_id:
                _fail("publication_operation_key was reused for another Candidate")
            return previous
        if self._publication is None:
            _fail("injected Publication port is required")
        command = {
            "schema": "publication-command/v1",
            "publication_operation_key": publication_operation_key,
            "workspace_id": session.request.target.workspace_id,
            "candidate_id": session.candidate.candidate_id,
            "accepted_by": accepted_by,
        }
        publisher = getattr(self._publication, "publish", None)
        if not callable(publisher):
            _fail("injected Publication port must expose publish(command)")
        try:
            raw = _mapping(publisher(command), "Publication result")
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"Publication port failed: {exc}") from exc
        receipt = self._parse_publication(raw, command, session.candidate)
        self._publication_receipts[publication_operation_key] = receipt
        self._accepted_publications[receipt.publication_id] = receipt
        return receipt

    def settle_story_state(self, publication: PublicationReceipt, *, operation_key: str) -> SettlementResult:
        _require_id(operation_key, "settlement operation_key")
        accepted = self._accepted_publications.get(publication.publication_id)
        if accepted != publication:
            _fail("Story State settlement requires this workflow's typed Publication result")
        previous = self._settlements.get(operation_key)
        if previous is not None:
            if previous.chapter_publication != publication:
                _fail("settlement operation_key was reused for another Publication")
            return previous
        if self._story_state is None:
            _fail("injected Story State settlement port is required")
        proposer = getattr(self._story_state, "propose_after_chapter", None)
        if not callable(proposer):
            _fail("Story State port must expose propose_after_chapter(request)")
        request = {
            "schema": "post-chapter-story-state-request/v1",
            "operation_key": operation_key,
            "workspace_id": publication.workspace_id,
            "chapter_candidate_id": publication.candidate_id,
            "chapter_publication_id": publication.publication_id,
            "chapter_document_id": publication.entity_id,
            "chapter_revision_id": publication.revision_id,
            "chapter_content_hash": publication.content_hash,
        }
        try:
            bundles = tuple(proposer(MappingProxyType(request)))
        except Exception as exc:
            raise WorkflowError(f"Story State proposal port failed: {exc}") from exc
        bundle_ids: list[str] = []
        candidate_ids: list[str] = []
        seen_bundles: set[str] = set()
        for index, raw_bundle in enumerate(bundles):
            bundle = dict(_mapping(raw_bundle, f"Story State bundle[{index}]"))
            self._verify_story_state_bundle(bundle, publication)
            bundle_id = self._persist_bundle(bundle)
            if bundle_id in seen_bundles:
                _fail("Story State settlement returned duplicate bundle_id")
            seen_bundles.add(bundle_id)
            staged = self._stage_candidate(f"{operation_key}:{index}", bundle_id)
            bundle_ids.append(bundle_id)
            candidate_ids.extend(staged)
        if len(candidate_ids) != len(set(candidate_ids)):
            _fail("Story State settlement staged duplicate Candidate identities")
        result = SettlementResult(publication, tuple(candidate_ids), tuple(bundle_ids))
        self._settlements[operation_key] = result
        return result

    def _control(self, session_id: str, action: str) -> SessionSnapshot:
        session = self._session(session_id)
        if session.state in {SessionState.PAUSED, SessionState.CANCELLED}:
            return self._snapshot(session)
        if session.state is not SessionState.RUNNING or session.invocation_id is None:
            _fail(f"cannot {action} a terminal chapter session")
        events = self._call_events(action, session.request.operation_key, session.invocation_id)
        self._apply_events(session, events)
        expected = SessionState.PAUSED if action == "pause" else SessionState.CANCELLED
        if session.state is not expected:
            _fail(f"Broker {action} did not return a matching terminal event")
        return self._snapshot(session)

    def _apply_events(self, session: _Session, events: Sequence[BrokerEvent]) -> None:
        self._preflight_events(session, events)
        terminal_seen = False
        for event in events:
            if not isinstance(event, BrokerEvent):
                _fail("Broker returned an untyped event")
            if terminal_seen:
                _fail("Broker returned events after a terminal event")
            if event.event_seq != session.last_event_seq + 1:
                _fail("Broker event sequence is not contiguous")
            session.last_event_seq = event.event_seq
            if event.kind == "acked":
                session.acked.extend(event.chunk)
                if event.prefix_size != len(session.acked):
                    _fail("Broker ACK prefix_size does not match exact received bytes")
                if event.prefix_hash != sha256(bytes(session.acked)).hexdigest():
                    _fail("Broker ACK prefix_hash does not match exact received bytes")
                continue
            terminal_seen = True
            local_hash = sha256(bytes(session.acked)).hexdigest()
            if event.prefix_hash is not None and event.prefix_hash != local_hash:
                _fail("Broker terminal hash does not match the exact ACK prefix")
            if event.kind == "completed":
                if session.phase == "skill":
                    if event.skill_chain_ref is None:
                        _fail("completed Skill phase requires bundle-backed chain evidence")
                    self._verify_skill_chain_ref(event.skill_chain_ref, session)
                    session.skill_chain_refs.append(event.skill_chain_ref)
                elif event.skill_chain_ref is not None:
                    _fail("raw generation cannot claim Skill chain evidence")
                self._complete_phase(session)
            elif event.kind in {"paused", "cancelled"}:
                session.state = SessionState.PAUSED if event.kind == "paused" else SessionState.CANCELLED
                self._materialize_candidate(session, status="partial")
            else:
                session.state = SessionState.FAILED
                session.error = event.error

    def _preflight_events(self, session: _Session, events: Sequence[BrokerEvent]) -> None:
        """Validate a returned event batch before any durable terminal effect."""

        expected_seq = session.last_event_seq
        prefix = bytes(session.acked)
        terminal_seen = False
        for event in events:
            if not isinstance(event, BrokerEvent):
                _fail("Broker returned an untyped event")
            if terminal_seen:
                _fail("Broker returned events after a terminal event")
            expected_seq += 1
            if event.event_seq != expected_seq:
                _fail("Broker event sequence is not contiguous")
            if event.kind == "acked":
                prefix += event.chunk
                if event.prefix_size != len(prefix):
                    _fail("Broker ACK prefix_size does not match exact received bytes")
                if event.prefix_hash != sha256(prefix).hexdigest():
                    _fail("Broker ACK prefix_hash does not match exact received bytes")
                continue
            terminal_seen = True
            if event.prefix_hash is not None and event.prefix_hash != sha256(prefix).hexdigest():
                _fail("Broker terminal hash does not match the exact ACK prefix")
            if event.kind == "completed":
                if session.phase == "skill":
                    if event.skill_chain_ref is None:
                        _fail("completed Skill phase requires bundle-backed chain evidence")
                    self._verify_skill_chain_ref(event.skill_chain_ref, session)
                elif event.skill_chain_ref is not None:
                    _fail("raw generation cannot claim Skill chain evidence")

    def _complete_phase(self, session: _Session) -> None:
        output = bytes(session.acked)
        if session.request.context_plan.skills and session.skill_index + 1 < len(session.request.context_plan.skills):
            session.skill_index += 1
            skill = session.request.context_plan.skills[session.skill_index]
            input_asset_id = self._create_asset(output, mime="text/plain;charset=utf-8")
            invocation = BrokerInvocation(
                invocation_key=f"{session.request.operation_key}:skill:{skill.order}",
                operation_key=session.request.operation_key,
                capability_id="writing.skill.apply/v1",
                input_asset_id=input_asset_id,
                input_hash=sha256(output).hexdigest(),
                expected_result_contract="candidate-batch/v1",
                result_bundle_id=session.result_bundle_id,
                result_item_id=session.result_item_id,
                phase="skill",
                skill=skill,
            )
            session.phase = "skill"
            session.phase_input = output
            session.acked.clear()
            session.last_event_seq = 0
            session.invocation_id = self._broker_start(invocation)
            return
        session.state = SessionState.COMPLETED
        self._materialize_candidate(session, status="complete")

    def _materialize_candidate(self, session: _Session, *, status: str) -> None:
        if session.candidate is not None:
            return
        content = bytes(session.acked)
        if status == "partial" and not content and session.phase == "skill":
            content = session.phase_input
        content_hash = sha256(content).hexdigest()
        payload_asset_id = self._create_asset(content, mime="text/plain;charset=utf-8")
        bundle_id = session.result_bundle_id
        item_id = session.result_item_id
        request = session.request
        source_refs = [
            {
                "workspace_id": request.target.workspace_id,
                "source_type": source.kind,
                "source_id": source.source_id,
                "revision_or_hash": source.revision_id,
            }
            for source in request.context_plan.sources
        ]
        if request.rewrite_selection is not None:
            source_refs.append(
                {
                    "workspace_id": request.target.workspace_id,
                    "source_type": "rewrite-selection",
                    "source_id": request.target.document_id,
                    "revision_or_hash": request.rewrite_selection.selected_hash,
                }
            )
        bundle = {
            "schema": "result-bundle/v1",
            "contract_id": "candidate-batch/v1",
            "bundle_id": bundle_id,
            "bundle_type": "candidate_batch",
            "producer": {
                "plugin_id": request.producer.plugin_id,
                "release_id": request.producer.release_id,
                "capability_id": request.operation.capability_id,
                "job_id": request.producer.job_id,
                "step_id": request.producer.step_id,
                "attempt_id": request.producer.attempt_id,
                "lease_epoch": request.producer.lease_epoch,
            },
            "input_snapshot_hash": request.producer.input_snapshot_hash,
            "items": [
                {
                    "schema": "candidate-item/v1",
                    "item_id": item_id,
                    # Core alone owns durable incomplete_stream.  A plugin
                    # partial is an ordinary review-only document Candidate.
                    "item_kind": "document",
                    "target": {"workspace_id": request.target.workspace_id, "entity_kind": "document", "entity_id": request.target.document_id},
                    "mutation": {"mode": request.operation.mutation_mode, "payload_schema": self._payload_schema(request), "payload_hash": content_hash},
                    "payload_asset_id": payload_asset_id,
                    "base": {"revision_id": request.target.base_revision_id, "content_hash": request.target.base_content_hash},
                    "write_set": [
                        {
                            "workspace_id": request.target.workspace_id,
                            "entity_kind": "document",
                            "entity_id": request.target.document_id,
                            "revision_id": request.target.base_revision_id,
                            "content_hash": request.target.base_content_hash,
                        }
                    ],
                    "parent_candidate_ids": [],
                    "source_refs": source_refs,
                    "status": status,
                }
            ],
            "warnings": [],
            "partial": status == "partial",
            "provenance_receipt_id": request.producer.provenance_receipt_id,
            "skill_chain_result_refs": [dict(ref) for ref in session.skill_chain_refs],
        }
        self._verify_result_bundle(
            bundle,
            workspace_id=request.target.workspace_id,
            snapshot_hash=request.producer.input_snapshot_hash,
        )
        persisted_id = self._persist_bundle(bundle)
        if persisted_id != bundle_id:
            _fail("ResultBundle port changed the deterministic bundle identity")
        candidate_ids = self._stage_candidate(f"{request.operation_key}:candidate", bundle_id)
        if len(candidate_ids) != 1:
            _fail("chapter ResultBundle must stage exactly one Candidate")
        session.candidate = ChapterCandidate(
            candidate_ids[0], bundle_id, payload_asset_id, content, content_hash, status, request.operation, request.target
        )

    @staticmethod
    def _payload_schema(request: ChapterRequest) -> str:
        del request
        return "core/document-text/v1"

    @staticmethod
    def _assemble_context(request: ChapterRequest) -> bytes:
        selection: dict[str, Any] | None = None
        if request.rewrite_selection is not None:
            selection = {
                "start_codepoint": request.rewrite_selection.start_codepoint,
                "end_codepoint": request.rewrite_selection.end_codepoint,
                "selected_text": request.rewrite_selection.selected_text,
                "selected_hash": request.rewrite_selection.selected_hash,
            }
        value = {
            "schema": "chapter-workflow-context/v1",
            "operation": request.operation.capability_id,
            "instruction": request.instruction,
            "target": {
                "workspace_id": request.target.workspace_id,
                "document_id": request.target.document_id,
                "base_revision_id": request.target.base_revision_id,
                "base_content_hash": request.target.base_content_hash,
            },
            "sources": [
                {
                    "kind": source.kind,
                    "source_id": source.source_id,
                    "revision_id": source.revision_id,
                    "content": source.content,
                    "content_hash": source.content_hash,
                }
                for source in request.context_plan.sources
            ],
            "skills": [
                {
                    "order": skill.order,
                    "skill_id": skill.skill_id,
                    "release_id": skill.release_id,
                    "package_hash": skill.package_hash,
                    "parameters_asset_id": skill.parameters_asset_id,
                }
                for skill in request.context_plan.skills
            ],
            "context_fingerprint": request.context_plan.fingerprint,
            "rewrite_selection": selection,
        }
        return _stable_bytes(value)

    def _create_asset(self, content: bytes, *, mime: str) -> str:
        creator = getattr(self._core, "create_asset", None)
        if not callable(creator):
            _fail("injected Core port must expose create_asset(content, *, mime)")
        try:
            result = creator(bytes(content), mime=mime)
        except Exception as exc:
            raise WorkflowError(f"Core Asset creation failed: {exc}") from exc
        if isinstance(result, str):
            return _require_id(result, "created asset_id")
        value = _mapping(result, "created Asset metadata")
        asset_id = _require_id(value.get("asset_id"), "created asset_id")
        if "sha256" in value and value["sha256"] != sha256(content).hexdigest():
            _fail("created Asset metadata hash does not match bytes")
        return asset_id

    def _persist_bundle(self, bundle: Mapping[str, Any]) -> str:
        persister = getattr(self._result_bundles, "persist_result_bundle", None)
        if not callable(persister):
            _fail("injected ResultBundle port must expose persist_result_bundle(bundle)")
        try:
            return _require_id(persister(MappingProxyType(dict(bundle))), "persisted bundle_id")
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"ResultBundle persistence failed: {exc}") from exc

    def _stage_candidate(self, operation_key: str, bundle_id: str) -> tuple[str, ...]:
        stager = getattr(self._core, "stage_candidate", None)
        if not callable(stager):
            _fail("injected Core port must expose stage_candidate(operation_key, bundle_id)")
        try:
            values = tuple(stager(operation_key, bundle_id))
        except Exception as exc:
            raise WorkflowError(f"Candidate staging failed: {exc}") from exc
        if not values:
            _fail("Candidate staging returned no identities")
        for value in values:
            _require_id(value, "candidate_id")
        return values

    def _broker_start(self, invocation: BrokerInvocation) -> str:
        starter = getattr(self._broker, "start", None)
        if not callable(starter):
            _fail("injected Broker port must expose start(invocation)")
        try:
            return _require_id(starter(invocation), "Broker invocation_id")
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"Broker start failed: {exc}") from exc

    def _call_events(self, method: str, *args: object) -> tuple[BrokerEvent, ...]:
        caller = getattr(self._broker, method, None)
        if not callable(caller):
            _fail(f"injected Broker port must expose {method}(...)")
        try:
            return tuple(caller(*args))
        except Exception as exc:
            raise WorkflowError(f"Broker {method} failed: {exc}") from exc

    @staticmethod
    def _parse_publication(raw: Mapping[str, Any], command: Mapping[str, Any], candidate: ChapterCandidate) -> PublicationReceipt:
        expected = {
            "schema", "publication_id", "candidate_id", "workspace_id", "entity_kind", "entity_id", "resulting_revision", "idempotent"
        }
        if set(raw) != expected or raw.get("schema") != "publication-result/v1":
            _fail("Publication result is not closed publication-result/v1")
        if raw.get("candidate_id") != command["candidate_id"] or raw.get("workspace_id") != command["workspace_id"]:
            _fail("Publication result is not bound to the accepted Candidate")
        if raw.get("entity_kind") != "document" or raw.get("entity_id") != candidate.target.document_id:
            _fail("Publication result target does not match the chapter")
        revision = _mapping(raw.get("resulting_revision"), "Publication resulting_revision")
        required = {"revision_id", "workspace_id", "entity_kind", "entity_id", "content_hash", "revision_number"}
        if set(revision) != required:
            _fail("Publication resulting_revision is not closed")
        if (
            revision.get("workspace_id") != raw["workspace_id"]
            or revision.get("entity_kind") != "document"
            or revision.get("entity_id") != raw["entity_id"]
        ):
            _fail("Publication resulting Revision crosses identity")
        revision_number = revision.get("revision_number")
        if not isinstance(revision_number, int) or isinstance(revision_number, bool) or revision_number < 1:
            _fail("Publication revision_number must be a positive integer")
        if not isinstance(raw.get("idempotent"), bool):
            _fail("Publication idempotent must be boolean")
        return PublicationReceipt(
            _require_id(raw.get("publication_id"), "publication_id"),
            _require_id(raw.get("candidate_id"), "candidate_id"),
            _require_id(raw.get("workspace_id"), "publication workspace_id"),
            _require_id(raw.get("entity_id"), "publication entity_id"),
            _require_id(revision.get("revision_id"), "publication revision_id"),
            _require_hash(revision.get("content_hash"), "publication content_hash"),
            MappingProxyType(dict(raw)),
        )

    @staticmethod
    def _verify_story_state_bundle(bundle: Mapping[str, Any], publication: PublicationReceipt) -> None:
        if bundle.get("schema") != "result-bundle/v1" or bundle.get("contract_id") != "candidate-batch/v1" or bundle.get("bundle_type") != "candidate_batch":
            _fail("Story State settlement must return candidate-batch ResultBundles")
        _require_id(bundle.get("bundle_id"), "Story State bundle_id")
        items = bundle.get("items")
        if not isinstance(items, list) or not items:
            _fail("Story State settlement bundle requires Candidate items")
        for item in items:
            item = _mapping(item, "Story State Candidate item")
            if item.get("schema") != "candidate-item/v1" or item.get("status") != "complete":
                _fail("Story State settlement may stage only complete Candidate items")
            target = _mapping(item.get("target"), "Story State Candidate target")
            if target.get("workspace_id") != publication.workspace_id or target.get("entity_kind") == "document":
                _fail("Story State Candidate must stay in the chapter Workspace and cannot target正文")
            refs = item.get("source_refs")
            if not isinstance(refs, list) or not any(
                isinstance(ref, Mapping)
                and ref.get("workspace_id") == publication.workspace_id
                and ref.get("source_id") == publication.entity_id
                and ref.get("revision_or_hash") == publication.revision_id
                for ref in refs
            ):
                _fail("Story State Candidate is not bound to the published chapter Revision")

        ChapterWorkflow._verify_result_bundle(
            bundle,
            workspace_id=publication.workspace_id,
            known_parent_ids={publication.candidate_id},
        )

    @staticmethod
    def _verify_result_bundle(
        bundle: Mapping[str, Any],
        *,
        workspace_id: str,
        snapshot_hash: str | None = None,
        known_parent_ids: set[str] | None = None,
    ) -> None:
        try:
            try:
                from plotpilot_plugin_sdk import verify_result_bundle
            except ModuleNotFoundError:
                from backend.plotpilot_plugin_sdk import verify_result_bundle
            verify_result_bundle(
                bundle,
                snapshot_workspace_id=workspace_id,
                snapshot_hash_value=snapshot_hash,
                known_parent_ids=known_parent_ids,
            )
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"ResultBundle contract verification failed: {exc}") from exc

    @staticmethod
    def _verify_skill_chain_ref(ref: Mapping[str, Any], session: _Session) -> None:
        required = {
            "schema",
            "chain_result_id",
            "asset_id",
            "asset_hash",
            "result_bundle_id",
            "result_item_id",
            "stream_id",
            "acked_prefix_hash",
        }
        if set(ref) != required or ref.get("schema") != "skill-chain-ref/v1":
            _fail("Skill chain reference is not closed skill-chain-ref/v1")
        _require_id(ref.get("chain_result_id"), "chain_result_id")
        _require_id(ref.get("asset_id"), "Skill chain asset_id")
        _require_hash(ref.get("asset_hash"), "Skill chain asset_hash")
        if ref.get("result_bundle_id") != session.result_bundle_id or ref.get("result_item_id") != session.result_item_id:
            _fail("Skill chain evidence is not anchored to this chapter ResultBundle")
        if ref.get("stream_id") is not None or ref.get("acked_prefix_hash") is not None:
            _fail("chapter ResultBundle Skill refs must be bundle-backed")
        chain_id = ref["chain_result_id"]
        if any(existing["chain_result_id"] == chain_id for existing in session.skill_chain_refs):
            _fail("duplicate Skill chain result identity")

    def _session(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise WorkflowError("unknown chapter session") from exc

    @staticmethod
    def _snapshot(session: _Session) -> SessionSnapshot:
        acked = bytes(session.acked)
        return SessionSnapshot(
            session.session_id,
            session.state,
            session.phase,
            session.skill_index,
            session.invocation_id,
            session.last_event_seq,
            len(acked),
            sha256(acked).hexdigest(),
            session.candidate,
            session.error,
        )


__all__ = [
    "BrokerEvent",
    "BrokerInvocation",
    "ChapterCandidate",
    "ChapterOperation",
    "ChapterRequest",
    "ChapterTarget",
    "ChapterWorkflow",
    "ProducerRef",
    "PublicationReceipt",
    "RewriteSelection",
    "SessionSnapshot",
    "SessionState",
    "SettlementResult",
    "WorkflowError",
]
