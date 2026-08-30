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
from threading import Lock, RLock
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
    FINALIZING = "finalizing"
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
        if self.end_codepoint - self.start_codepoint != len(self.selected_text):
            _fail("rewrite selection range length does not match selected_text codepoints")
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
        if not isinstance(self.context_plan, FrozenContextPlan):
            _fail("context_plan must be FrozenContextPlan")
        try:
            self.context_plan.validate()
        except ValueError as exc:
            raise WorkflowError(f"invalid frozen context plan: {exc}") from exc
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
    settlement_receipt_id: str


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
    rewrite_selection_receipt: Mapping[str, Any] | None = None
    transition_epoch: int = 0
    pending_transition: _PendingTransition | None = None
    state_lock: RLock = field(default_factory=RLock, repr=False)
    finalizer_lock: Lock = field(default_factory=Lock, repr=False)


@dataclass(frozen=True, slots=True)
class _TransitionToken:
    invocation_id: str | None
    phase: str
    transition_epoch: int
    last_event_seq: int


@dataclass(slots=True)
class _PendingTransition:
    kind: str
    output: bytes
    skill_chain_refs: tuple[Mapping[str, Any], ...]
    terminal_state: SessionState | None = None
    candidate_status: str | None = None
    next_skill_index: int | None = None
    input_asset_id: str | None = None
    payload_asset_id: str | None = None
    candidate_bundle: Mapping[str, Any] | None = None
    persisted_bundle_id: str | None = None
    candidate_ids: tuple[str, ...] | None = None


@dataclass(slots=True)
class _PendingSettlement:
    publication: PublicationReceipt
    bundles: tuple[Mapping[str, Any], ...]
    bundle_ids: tuple[str, ...]
    batch_fingerprint: str
    persisted_ids: list[str] = field(default_factory=list)


class ChapterWorkflow:
    """State machine for one or more isolated chapter sessions."""

    def __init__(
        self,
        *,
        core: object,
        broker: object,
        result_bundles: object,
        publication: object | None = None,
        story_state: object | None = None,
        rewrite_selections: object | None = None,
        settlement_batch: object | None = None,
    ) -> None:
        self._core = core
        self._broker = broker
        self._result_bundles = result_bundles
        self._publication = publication
        self._story_state = story_state
        self._rewrite_selections = rewrite_selections
        self._settlement_batch = settlement_batch
        self._sessions: dict[str, _Session] = {}
        self._operation_sessions: dict[str, str] = {}
        self._publication_receipts: dict[str, PublicationReceipt] = {}
        self._accepted_publications: dict[str, PublicationReceipt] = {}
        self._settlements: dict[str, SettlementResult] = {}
        self._pending_settlements: dict[str, _PendingSettlement] = {}
        self._publication_lock = RLock()
        self._settlement_lock = RLock()

    def start(self, request: ChapterRequest) -> SessionSnapshot:
        if not isinstance(request, ChapterRequest):
            _fail("start requires a typed ChapterRequest")
        try:
            request.context_plan.validate()
        except ValueError as exc:
            raise WorkflowError(f"invalid frozen context plan at workflow start: {exc}") from exc
        existing_id = self._operation_sessions.get(request.operation_key)
        if existing_id is not None:
            existing = self._sessions[existing_id]
            if existing.request != request:
                _fail("operation_key was reused with a different chapter request")
            with existing.state_lock:
                return self._snapshot(existing)
        rewrite_receipt = self._read_rewrite_selection(request)
        context_bytes = self._assemble_context(request, rewrite_receipt)
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
            session_id=session_id,
            request=request,
            state=SessionState.RUNNING,
            phase="raw",
            skill_index=-1,
            invocation_id=None,
            result_bundle_id=bundle_id,
            result_item_id=item_id,
            phase_input=context_bytes,
            rewrite_selection_receipt=rewrite_receipt,
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
        with session.state_lock:
            if session.state is SessionState.FINALIZING:
                retry_pending = True
                token = None
                invocation_id = None
                last_event_seq = 0
            elif session.state is not SessionState.RUNNING:
                return self._snapshot(session)
            else:
                if session.invocation_id is None:
                    _fail("running chapter session has no Broker invocation")
                retry_pending = False
                token = self._transition_token(session)
                invocation_id = session.invocation_id
                last_event_seq = session.last_event_seq
        if retry_pending:
            self._resume_pending(session)
            with session.state_lock:
                return self._snapshot(session)

        events = self._call_events("poll", invocation_id, last_event_seq)
        with session.state_lock:
            if not self._transition_matches(session, token):
                return self._snapshot(session)
            self._apply_events(session, events)
            should_finalize = session.state is SessionState.FINALIZING
        if should_finalize:
            self._resume_pending(session)
        with session.state_lock:
            return self._snapshot(session)

    def pause(self, session_id: str) -> SessionSnapshot:
        return self._control(session_id, "pause")

    def cancel(self, session_id: str) -> SessionSnapshot:
        return self._control(session_id, "cancel")

    def accept(self, session_id: str, *, accepted_by: str, publication_operation_key: str) -> PublicationReceipt:
        session = self._session(session_id)
        with session.state_lock:
            if session.state is not SessionState.COMPLETED or session.candidate is None or session.candidate.partial:
                _fail("only a complete chapter Candidate can be accepted")
            candidate = session.candidate
        self._validate_document_bytes(candidate.content, complete=True)
        _require_id(accepted_by, "accepted_by")
        _require_id(publication_operation_key, "publication_operation_key")
        with self._publication_lock:
            previous = self._publication_receipts.get(publication_operation_key)
            if previous is not None:
                if previous.candidate_id != candidate.candidate_id:
                    _fail("publication_operation_key was reused for another Candidate")
                return previous
            if self._publication is None:
                _fail("injected Publication port is required")
            command = MappingProxyType(
                {
                    "schema": "publication-command/v1",
                    "publication_operation_key": publication_operation_key,
                    "workspace_id": candidate.target.workspace_id,
                    "candidate_id": candidate.candidate_id,
                    "accepted_by": accepted_by,
                }
            )
            publisher = getattr(self._publication, "publish", None)
            if not callable(publisher):
                _fail("injected Publication port must expose publish(command)")
            try:
                raw = _mapping(publisher(command), "Publication result")
            except WorkflowError:
                raise
            except Exception as exc:
                raise WorkflowError(f"Publication port failed: {exc}") from exc
            receipt = self._parse_publication(raw, candidate)
            self._publication_receipts[publication_operation_key] = receipt
            self._accepted_publications[receipt.publication_id] = receipt
            return receipt

    def settle_story_state(self, publication: PublicationReceipt, *, operation_key: str) -> SettlementResult:
        _require_id(operation_key, "settlement operation_key")
        with self._settlement_lock:
            accepted = self._accepted_publications.get(publication.publication_id)
            if accepted != publication:
                _fail("Story State settlement requires this workflow's typed Publication result")
            previous = self._settlements.get(operation_key)
            if previous is not None:
                if previous.chapter_publication != publication:
                    _fail("settlement operation_key was reused for another Publication")
                return previous
            pending = self._pending_settlements.get(operation_key)
            if pending is not None and pending.publication != publication:
                _fail("settlement operation_key was reused for another Publication")
            if pending is None:
                pending = self._prepare_settlement(publication, operation_key)
                self._pending_settlements[operation_key] = pending

            while len(pending.persisted_ids) < len(pending.bundles):
                index = len(pending.persisted_ids)
                declared_id = pending.bundle_ids[index]
                persisted_id = self._persist_bundle(pending.bundles[index])
                if persisted_id != declared_id:
                    _fail("Story State ResultBundle port changed the declared bundle identity")
                pending.persisted_ids.append(persisted_id)

            result = self._stage_story_state_batch(operation_key, pending)
            self._settlements[operation_key] = result
            del self._pending_settlements[operation_key]
            return result

    def _control(self, session_id: str, action: str) -> SessionSnapshot:
        session = self._session(session_id)
        expected_kind = "paused" if action == "pause" else "cancelled"
        expected_state = SessionState.PAUSED if action == "pause" else SessionState.CANCELLED
        while True:
            with session.state_lock:
                if session.state in {SessionState.PAUSED, SessionState.CANCELLED}:
                    return self._snapshot(session)
                if session.state is SessionState.FINALIZING:
                    retry_pending = True
                    token = None
                    invocation_id = None
                elif session.state is SessionState.RUNNING and session.invocation_id is not None:
                    retry_pending = False
                    token = self._transition_token(session)
                    invocation_id = session.invocation_id
                else:
                    _fail(f"cannot {action} a terminal chapter session")
            if retry_pending:
                self._resume_pending(session)
                with session.state_lock:
                    if session.state is SessionState.FINALIZING:
                        return self._snapshot(session)
                continue

            events = self._call_events(action, session.request.operation_key, invocation_id)
            with session.state_lock:
                if not self._transition_matches(session, token):
                    return self._snapshot(session)
                self._apply_events(session, events, required_terminal_kind=expected_kind)
                should_finalize = session.state is SessionState.FINALIZING
            if should_finalize:
                self._resume_pending(session)
            with session.state_lock:
                if session.state in {SessionState.PAUSED, SessionState.CANCELLED}:
                    return self._snapshot(session)
                if session.state is SessionState.FINALIZING:
                    return self._snapshot(session)
                if session.state is not expected_state:
                    _fail(f"Broker {action} did not return a matching terminal event")
                return self._snapshot(session)

    def _apply_events(
        self,
        session: _Session,
        events: Sequence[BrokerEvent],
        *,
        required_terminal_kind: str | None = None,
    ) -> None:
        prefix, last_event_seq, terminal = self._preflight_events(session, events)
        if required_terminal_kind is not None and (terminal is None or terminal.kind != required_terminal_kind):
            _fail(f"Broker control did not return {required_terminal_kind}")
        if not events:
            return
        if terminal is None:
            session.acked = bytearray(prefix)
            session.last_event_seq = last_event_seq
            session.transition_epoch += 1
            return
        if terminal.kind == "failed":
            session.acked = bytearray(prefix)
            session.last_event_seq = last_event_seq
            session.state = SessionState.FAILED
            session.error = terminal.error
            session.transition_epoch += 1
            return

        refs = tuple(session.skill_chain_refs)
        if terminal.kind == "completed" and terminal.skill_chain_ref is not None:
            refs += (MappingProxyType(dict(terminal.skill_chain_ref)),)
        if terminal.kind == "completed":
            try:
                self._validate_document_bytes(prefix, complete=True)
            except WorkflowError as exc:
                session.acked = bytearray(prefix)
                session.last_event_seq = last_event_seq
                session.state = SessionState.FAILED
                session.error = str(exc)
                session.transition_epoch += 1
                return
            next_index = session.skill_index + 1
            if next_index < len(session.request.context_plan.skills):
                pending = _PendingTransition(
                    kind="next_skill",
                    output=prefix,
                    skill_chain_refs=refs,
                    next_skill_index=next_index,
                )
            else:
                pending = _PendingTransition(
                    kind="candidate",
                    output=prefix,
                    skill_chain_refs=refs,
                    terminal_state=SessionState.COMPLETED,
                    candidate_status="complete",
                )
        else:
            output = prefix
            if not output and session.phase == "skill":
                output = session.phase_input
            try:
                self._validate_document_bytes(output, complete=False)
            except WorkflowError as exc:
                session.acked = bytearray(prefix)
                session.last_event_seq = last_event_seq
                session.state = SessionState.FAILED
                session.error = str(exc)
                session.transition_epoch += 1
                return
            pending = _PendingTransition(
                kind="candidate",
                output=output,
                skill_chain_refs=refs,
                terminal_state=SessionState.PAUSED if terminal.kind == "paused" else SessionState.CANCELLED,
                candidate_status="partial",
            )

        session.acked = bytearray(prefix)
        session.last_event_seq = last_event_seq
        session.pending_transition = pending
        session.state = SessionState.FINALIZING
        session.error = None
        session.transition_epoch += 1

    def _preflight_events(
        self, session: _Session, events: Sequence[BrokerEvent]
    ) -> tuple[bytes, int, BrokerEvent | None]:
        """Validate a returned event batch before any durable terminal effect."""

        expected_seq = session.last_event_seq
        prefix = bytes(session.acked)
        terminal_seen = False
        terminal: BrokerEvent | None = None
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
            terminal = event
        return prefix, expected_seq, terminal

    def _resume_pending(self, session: _Session) -> bool:
        if not session.finalizer_lock.acquire(blocking=False):
            return False
        pending: _PendingTransition | None = None
        try:
            with session.state_lock:
                if session.state is not SessionState.FINALIZING or session.pending_transition is None:
                    return False
                pending = session.pending_transition

            if pending.kind == "next_skill":
                if pending.input_asset_id is None:
                    asset_id = self._create_asset(pending.output, mime="text/plain;charset=utf-8")
                    with session.state_lock:
                        if session.pending_transition is not pending:
                            _fail("chapter transition changed during Skill Asset creation")
                        pending.input_asset_id = asset_id
                if pending.next_skill_index is None or pending.input_asset_id is None:
                    _fail("pending Skill transition is incomplete")
                skill = session.request.context_plan.skills[pending.next_skill_index]
                invocation = BrokerInvocation(
                    invocation_key=f"{session.request.operation_key}:skill:{skill.order}",
                    operation_key=session.request.operation_key,
                    capability_id="writing.skill.apply/v1",
                    input_asset_id=pending.input_asset_id,
                    input_hash=sha256(pending.output).hexdigest(),
                    expected_result_contract="candidate-batch/v1",
                    result_bundle_id=session.result_bundle_id,
                    result_item_id=session.result_item_id,
                    phase="skill",
                    skill=skill,
                )
                with session.state_lock:
                    token = self._transition_token(session)
                invocation_id = self._broker_start(invocation)
                with session.state_lock:
                    if session.pending_transition is not pending or not self._transition_matches(
                        session, token, required_state=SessionState.FINALIZING
                    ):
                        _fail("chapter transition changed during Broker start")
                    session.phase = "skill"
                    session.skill_index = pending.next_skill_index
                    session.phase_input = pending.output
                    session.acked.clear()
                    session.last_event_seq = 0
                    session.invocation_id = invocation_id
                    session.skill_chain_refs = list(pending.skill_chain_refs)
                    session.pending_transition = None
                    session.state = SessionState.RUNNING
                    session.error = None
                    session.transition_epoch += 1
                return True

            if pending.kind != "candidate" or pending.terminal_state is None or pending.candidate_status is None:
                _fail("unknown pending chapter transition")
            self._validate_document_bytes(pending.output, complete=pending.candidate_status == "complete")
            if pending.payload_asset_id is None:
                asset_id = self._create_asset(pending.output, mime="text/plain;charset=utf-8")
                with session.state_lock:
                    if session.pending_transition is not pending:
                        _fail("chapter transition changed during payload Asset creation")
                    pending.payload_asset_id = asset_id
            if pending.candidate_bundle is None:
                bundle = self._build_candidate_bundle(session, pending)
                self._verify_result_bundle(
                    bundle,
                    workspace_id=session.request.target.workspace_id,
                    snapshot_hash=session.request.producer.input_snapshot_hash,
                )
                pending.candidate_bundle = bundle
            if pending.persisted_bundle_id is None:
                persisted_id = self._persist_bundle(pending.candidate_bundle)
                if persisted_id != session.result_bundle_id:
                    _fail("ResultBundle port changed the deterministic bundle identity")
                pending.persisted_bundle_id = persisted_id
            if pending.candidate_ids is None:
                candidate_ids = self._stage_candidate(
                    f"{session.request.operation_key}:candidate", session.result_bundle_id
                )
                if len(candidate_ids) != 1:
                    _fail("chapter ResultBundle must stage exactly one Candidate")
                pending.candidate_ids = candidate_ids
            candidate = ChapterCandidate(
                pending.candidate_ids[0],
                session.result_bundle_id,
                pending.payload_asset_id,
                pending.output,
                sha256(pending.output).hexdigest(),
                pending.candidate_status,
                session.request.operation,
                session.request.target,
            )
            with session.state_lock:
                if session.pending_transition is not pending:
                    _fail("chapter transition changed before Candidate commit")
                session.skill_chain_refs = list(pending.skill_chain_refs)
                session.candidate = candidate
                session.pending_transition = None
                session.state = pending.terminal_state
                session.error = None
                session.transition_epoch += 1
            return True
        except WorkflowError as exc:
            with session.state_lock:
                if pending is not None and session.pending_transition is pending:
                    session.error = str(exc)
            raise
        finally:
            session.finalizer_lock.release()

    def _build_candidate_bundle(self, session: _Session, pending: _PendingTransition) -> Mapping[str, Any]:
        if pending.payload_asset_id is None or pending.candidate_status is None:
            _fail("Candidate transition is missing its payload Asset")
        content = pending.output
        content_hash = sha256(content).hexdigest()
        payload_asset_id = pending.payload_asset_id
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
                    "source_type": "revision",
                    "source_id": request.target.base_revision_id,
                    "revision_or_hash": request.target.base_content_hash,
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
                    "status": pending.candidate_status,
                }
            ],
            "warnings": [],
            "partial": pending.candidate_status == "partial",
            "provenance_receipt_id": request.producer.provenance_receipt_id,
            "skill_chain_result_refs": [dict(ref) for ref in pending.skill_chain_refs],
        }
        return bundle

    @staticmethod
    def _validate_document_bytes(content: bytes, *, complete: bool) -> str:
        try:
            text = bytes(content).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkflowError("chapter document bytes must be strict UTF-8") from exc
        if complete and not text.strip():
            _fail("complete chapter output must be non-blank text")
        return text

    @staticmethod
    def _transition_token(session: _Session) -> _TransitionToken:
        return _TransitionToken(
            session.invocation_id,
            session.phase,
            session.transition_epoch,
            session.last_event_seq,
        )

    @staticmethod
    def _transition_matches(
        session: _Session,
        token: _TransitionToken | None,
        *,
        required_state: SessionState = SessionState.RUNNING,
    ) -> bool:
        return (
            token is not None
            and session.state is required_state
            and ChapterWorkflow._transition_token(session) == token
        )

    @staticmethod
    def _payload_schema(request: ChapterRequest) -> str:
        del request
        return "core/document-text/v1"

    def _read_rewrite_selection(self, request: ChapterRequest) -> Mapping[str, Any] | None:
        selection = request.rewrite_selection
        if selection is None:
            return None
        if self._rewrite_selections is None:
            _fail("injected Rewrite selection receipt port is required")
        reader = getattr(self._rewrite_selections, "read_rewrite_selection", None)
        if not callable(reader):
            _fail("Rewrite selection receipt port must expose read_rewrite_selection(request)")
        command = MappingProxyType(
            {
                "schema": "rewrite-selection-read/v1",
                "workspace_id": request.target.workspace_id,
                "document_id": request.target.document_id,
                "base_revision_id": request.target.base_revision_id,
                "base_content_hash": request.target.base_content_hash,
                "start_codepoint": selection.start_codepoint,
                "end_codepoint": selection.end_codepoint,
                "selected_text": selection.selected_text,
                "selected_hash": selection.selected_hash,
            }
        )
        try:
            raw = _mapping(reader(command), "Rewrite selection receipt")
            receipt = json.loads(_stable_bytes(raw).decode("utf-8"))
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"Rewrite selection receipt port failed: {exc}") from exc
        expected = {
            "schema",
            "receipt_id",
            "workspace_id",
            "document_id",
            "base_revision_id",
            "base_content_hash",
            "current_revision_id",
            "total_codepoints",
            "start_codepoint",
            "end_codepoint",
            "selected_text",
            "selected_hash",
        }
        if set(receipt) != expected or receipt.get("schema") != "rewrite-selection-receipt/v1":
            _fail("Rewrite selection receipt is not closed rewrite-selection-receipt/v1")
        _require_id(receipt.get("receipt_id"), "rewrite selection receipt_id")
        total = receipt.get("total_codepoints")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            _fail("Rewrite selection receipt total_codepoints must be a non-negative integer")
        bindings = {
            "workspace_id": request.target.workspace_id,
            "document_id": request.target.document_id,
            "base_revision_id": request.target.base_revision_id,
            "base_content_hash": request.target.base_content_hash,
            "current_revision_id": request.target.base_revision_id,
            "start_codepoint": selection.start_codepoint,
            "end_codepoint": selection.end_codepoint,
            "selected_text": selection.selected_text,
            "selected_hash": selection.selected_hash,
        }
        if any(receipt.get(name) != value for name, value in bindings.items()):
            _fail("Rewrite selection receipt does not bind the requested base Revision and exact selection")
        if selection.end_codepoint > total:
            _fail("Rewrite selection range is outside the bound base Revision")
        return MappingProxyType(receipt)

    @staticmethod
    def _assemble_context(
        request: ChapterRequest, rewrite_receipt: Mapping[str, Any] | None
    ) -> bytes:
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
            "rewrite_selection_receipt": dict(rewrite_receipt) if rewrite_receipt is not None else None,
        }
        return _stable_bytes(value)

    def _prepare_settlement(self, publication: PublicationReceipt, operation_key: str) -> _PendingSettlement:
        if self._story_state is None:
            _fail("injected Story State settlement port is required")
        proposer = getattr(self._story_state, "propose_after_chapter", None)
        if not callable(proposer):
            _fail("Story State port must expose propose_after_chapter(request)")
        request = MappingProxyType(
            {
                "schema": "post-chapter-story-state-request/v1",
                "operation_key": operation_key,
                "workspace_id": publication.workspace_id,
                "chapter_candidate_id": publication.candidate_id,
                "chapter_publication_id": publication.publication_id,
                "chapter_document_id": publication.entity_id,
                "chapter_revision_id": publication.revision_id,
                "chapter_content_hash": publication.content_hash,
            }
        )
        try:
            raw_bundles = tuple(proposer(request))
            bundles = tuple(
                json.loads(_stable_bytes(_mapping(raw, f"Story State bundle[{index}]")).decode("utf-8"))
                for index, raw in enumerate(raw_bundles)
            )
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"Story State proposal port failed: {exc}") from exc

        bundle_ids: list[str] = []
        seen_bundle_ids: set[str] = set()
        seen_item_ids: set[str] = set()
        for index, bundle in enumerate(bundles):
            self._verify_story_state_bundle(bundle, publication)
            bundle_id = _require_id(bundle.get("bundle_id"), f"Story State bundle[{index}] bundle_id")
            if bundle_id in seen_bundle_ids:
                _fail("Story State settlement returned duplicate bundle_id")
            seen_bundle_ids.add(bundle_id)
            bundle_ids.append(bundle_id)
            for item in bundle["items"]:
                item_id = _require_id(item.get("item_id"), "Story State item_id")
                if item_id in seen_item_ids:
                    _fail("Story State settlement returned duplicate item_id")
                seen_item_ids.add(item_id)

        fingerprint_material = {
            "operation_key": operation_key,
            "chapter_publication_id": publication.publication_id,
            "bundles": [sha256(_stable_bytes(bundle)).hexdigest() for bundle in bundles],
        }
        fingerprint = sha256(_stable_bytes(fingerprint_material)).hexdigest()
        return _PendingSettlement(publication, bundles, tuple(bundle_ids), fingerprint)

    def _stage_story_state_batch(
        self, operation_key: str, pending: _PendingSettlement
    ) -> SettlementResult:
        if self._settlement_batch is None:
            _fail("injected atomic Story State Candidate batch port is required")
        stager = getattr(self._settlement_batch, "stage_story_state_batch", None)
        if not callable(stager):
            _fail("atomic Story State Candidate batch port must expose stage_story_state_batch(command)")
        command = MappingProxyType(
            {
                "schema": "story-state-candidate-batch-stage/v1",
                "operation_key": operation_key,
                "chapter_publication_id": pending.publication.publication_id,
                "batch_fingerprint": pending.batch_fingerprint,
                "bundle_ids": pending.bundle_ids,
            }
        )
        try:
            raw = _mapping(stager(command), "Story State atomic batch result")
            result = json.loads(_stable_bytes(raw).decode("utf-8"))
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(f"Story State atomic Candidate batch failed: {exc}") from exc
        expected = {
            "schema",
            "receipt_id",
            "operation_key",
            "chapter_publication_id",
            "batch_fingerprint",
            "bundle_ids",
            "candidate_groups",
            "idempotent",
        }
        if set(result) != expected or result.get("schema") != "story-state-candidate-batch-result/v1":
            _fail("Story State atomic batch result is not closed")
        if (
            result.get("operation_key") != operation_key
            or result.get("chapter_publication_id") != pending.publication.publication_id
            or result.get("batch_fingerprint") != pending.batch_fingerprint
            or result.get("bundle_ids") != list(pending.bundle_ids)
        ):
            _fail("Story State atomic batch result is not bound to the requested batch")
        if not isinstance(result.get("idempotent"), bool):
            _fail("Story State atomic batch idempotent must be boolean")
        groups = result.get("candidate_groups")
        if not isinstance(groups, list) or len(groups) != len(pending.bundles):
            _fail("Story State atomic batch result has the wrong Candidate groups")
        candidate_ids: list[str] = []
        for bundle, bundle_id, group in zip(pending.bundles, pending.bundle_ids, groups, strict=True):
            group = _mapping(group, "Story State Candidate group")
            if set(group) != {"bundle_id", "candidate_ids"} or group.get("bundle_id") != bundle_id:
                _fail("Story State Candidate group is not bound to its ResultBundle")
            values = group.get("candidate_ids")
            if not isinstance(values, list) or len(values) != len(bundle["items"]):
                _fail("Story State Candidate group cardinality does not match its ResultBundle")
            for value in values:
                candidate_ids.append(_require_id(value, "Story State candidate_id"))
        if len(candidate_ids) != len(set(candidate_ids)):
            _fail("Story State atomic batch returned duplicate Candidate identities")
        return SettlementResult(
            pending.publication,
            tuple(candidate_ids),
            pending.bundle_ids,
            _require_id(result.get("receipt_id"), "Story State settlement receipt_id"),
        )

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
            isolated = json.loads(_stable_bytes(bundle).decode("utf-8"))
            return _require_id(persister(MappingProxyType(isolated)), "persisted bundle_id")
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
    def _parse_publication(raw: Mapping[str, Any], candidate: ChapterCandidate) -> PublicationReceipt:
        raw = json.loads(_stable_bytes(raw).decode("utf-8"))
        expected = {
            "schema", "publication_id", "candidate_id", "workspace_id", "entity_kind", "entity_id", "resulting_revision", "idempotent"
        }
        if set(raw) != expected or raw.get("schema") != "publication-result/v1":
            _fail("Publication result is not closed publication-result/v1")
        if (
            raw.get("candidate_id") != candidate.candidate_id
            or raw.get("workspace_id") != candidate.target.workspace_id
        ):
            _fail("Publication result is not bound to the accepted Candidate")
        if raw.get("entity_kind") != "document" or raw.get("entity_id") != candidate.target.document_id:
            _fail("Publication result target does not match the chapter")
        revision = _mapping(raw.get("resulting_revision"), "Publication resulting_revision")
        required = {"revision_id", "workspace_id", "entity_kind", "entity_id", "content_hash", "revision_number"}
        if set(revision) != required:
            _fail("Publication resulting_revision is not closed")
        if (
            revision.get("workspace_id") != candidate.target.workspace_id
            or revision.get("entity_kind") != "document"
            or revision.get("entity_id") != candidate.target.document_id
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
            MappingProxyType({**raw, "resulting_revision": MappingProxyType(dict(revision))}),
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
