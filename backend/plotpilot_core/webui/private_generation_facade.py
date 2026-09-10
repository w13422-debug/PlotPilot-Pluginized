"""Private WebUI facade for plugin-backed chapter generation.

The facade deliberately does not know how to build a RunSnapshot.  A
server-owned binding authority must point it at an already-published,
immutable RunSnapshot Asset.  The first WebUI release composes the explicit
unavailable binding, so missing planning/model/plugin identities can never be
invented or silently routed to the legacy generator.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from backend.plotpilot_plugin_sdk import (
    ContractError,
    canonical_bytes,
    parse_json_bytes,
    verify_snapshot,
)

from ..assets import AssetStore
from ..bootstrap.production_job_runtime import ProductionJobRuntime
from ..repositories.authority import (
    ChapterGenerationWriterFence,
    ConflictError,
    CoreAuthorityRepository,
    NotFoundError,
)

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_FIELDS = frozenset(
    {"schema", "operation_key", "requested_mode", "expected_base", "outline"}
)
_BASE_FIELDS = frozenset({"revision_id", "content_hash"})
_CHAPTER_OPERATION = "generate"
_TERMINAL_JOB_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


@dataclass(frozen=True, slots=True)
class BindingAvailability:
    """Closed availability answer from the server-owned binding authority."""

    status: Literal["available", "unavailable"]
    reason_code: str | None
    reason: str | None

    @classmethod
    def available(cls) -> BindingAvailability:
        return cls("available", None, None)

    @classmethod
    def unavailable(cls, reason_code: str, reason: str) -> BindingAvailability:
        return cls("unavailable", reason_code, reason)


@dataclass(frozen=True, slots=True)
class BindingUnavailable:
    reason_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class VerifiedChapterRunSnapshotBinding:
    """Identity closure returned by a trusted, server-owned planner binding."""

    run_snapshot_asset_id: str
    snapshot_hash: str
    run_intent_id: str
    capability_id: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ChapterGenerationRequest:
    workspace_id: str
    chapter_document_id: str
    operation_key: str
    requested_mode: Literal["plugin"]
    expected_revision_id: str
    expected_content_hash: str
    outline: str

    def canonical_value(self) -> dict[str, Any]:
        return {
            "schema": "webui-chapter-generation-command/v1",
            "workspace_id": self.workspace_id,
            "chapter_document_id": self.chapter_document_id,
            "operation_key": self.operation_key,
            "requested_mode": self.requested_mode,
            "expected_base": {
                "revision_id": self.expected_revision_id,
                "content_hash": self.expected_content_hash,
            },
            "outline": self.outline,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_bytes(self.canonical_value())).hexdigest()


class ChapterRunSnapshotBindingPort(Protocol):
    def availability(
        self, *, workspace_id: str, chapter_document_id: str
    ) -> BindingAvailability: ...

    def resolve(
        self, request: ChapterGenerationRequest
    ) -> VerifiedChapterRunSnapshotBinding | BindingUnavailable: ...


class UnavailableChapterRunSnapshotBinding:
    """Honest first-release binding: no verified planner binding is installed."""

    _REASON_CODE = "verified_plugin_binding_unavailable"
    _REASON = (
        "No verified server-owned chapter RunSnapshot binding is configured."
    )

    def availability(
        self, *, workspace_id: str, chapter_document_id: str
    ) -> BindingAvailability:
        del workspace_id, chapter_document_id
        return BindingAvailability.unavailable(self._REASON_CODE, self._REASON)

    def resolve(self, request: ChapterGenerationRequest) -> BindingUnavailable:
        del request
        return BindingUnavailable(self._REASON_CODE, self._REASON)


_UNAVAILABLE_BINDING = UnavailableChapterRunSnapshotBinding()


class _PrivateGenerationFault(RuntimeError):
    def __init__(self, status: int, error_code: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.error_code = error_code
        self.reason = reason


class PrivateChapterGenerationFacade:
    """Validate private input, own the chapter fence, and delegate one Job start."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        assets: AssetStore,
        job_runtime: ProductionJobRuntime,
        binding: ChapterRunSnapshotBindingPort = _UNAVAILABLE_BINDING,
    ) -> None:
        if not isinstance(repository, CoreAuthorityRepository):
            raise TypeError("repository must be CoreAuthorityRepository")
        if not isinstance(assets, AssetStore):
            raise TypeError("assets must be AssetStore")
        if not isinstance(job_runtime, ProductionJobRuntime):
            raise TypeError("job_runtime must be the accepted ProductionJobRuntime")
        if job_runtime.repository is not repository or job_runtime.assets is not assets:
            raise TypeError("private generation dependencies must share one authority graph")
        if not callable(getattr(binding, "availability", None)) or not callable(
            getattr(binding, "resolve", None)
        ):
            raise TypeError("binding must implement the closed chapter binding port")
        self.repository = repository
        self.assets = assets
        self.job_runtime = job_runtime
        self.binding = binding
        # The facade and its GET reconciler share this short authority gate so
        # a reader cannot mistake the interval between fence acquire and
        # durable job.start reservation for an abandoned start.
        self._authority_lock = threading.RLock()

    @staticmethod
    def _chapter_not_found(
        workspace_id: str, chapter_document_id: str
    ) -> tuple[int, dict[str, Any]]:
        return 404, {
            "schema": "webui-chapter-generation-error/v1",
            "workspace_id": workspace_id,
            "chapter_document_id": chapter_document_id,
            "operation_key": None,
            "error_code": "chapter_not_found",
            "reason": "Chapter is unavailable.",
            "retryable": False,
        }

    @staticmethod
    def _error(
        status: int,
        workspace_id: str,
        chapter_document_id: str,
        error_code: str,
        reason: str,
        *,
        operation_key: str | None = None,
        retryable: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        return status, {
            "schema": "webui-chapter-generation-error/v1",
            "workspace_id": workspace_id,
            "chapter_document_id": chapter_document_id,
            "operation_key": operation_key,
            "error_code": error_code,
            "reason": reason,
            "retryable": retryable,
        }

    @staticmethod
    def _unavailable(
        workspace_id: str,
        chapter_document_id: str,
        reason_code: str,
        reason: str,
    ) -> tuple[int, dict[str, Any]]:
        return 409, {
            "schema": "webui-chapter-generation-unavailable/v1",
            "workspace_id": workspace_id,
            "chapter_document_id": chapter_document_id,
            "status": "unavailable",
            "reason_code": reason_code,
            "reason": reason,
        }

    def _chapter(self, workspace_id: str, chapter_document_id: str) -> Any:
        if not _IDENTITY.fullmatch(workspace_id) or not _IDENTITY.fullmatch(
            chapter_document_id
        ):
            raise NotFoundError(chapter_document_id)
        document = self.repository.get_document(chapter_document_id)
        if (
            document.workspace_id != workspace_id
            or document.document_type != "core.chapter"
        ):
            raise NotFoundError(chapter_document_id)
        return document

    def _current_base(self, document: Any) -> dict[str, str] | None:
        revision_id = document.current_revision_id
        if revision_id is None:
            return None
        revision = self.repository.get_revision(revision_id)
        if (
            revision.workspace_id != document.workspace_id
            or revision.document_id != document.document_id
        ):
            raise _PrivateGenerationFault(
                409, "base_authority_drift", "Chapter base authority is inconsistent."
            )
        return {"revision_id": revision.revision_id, "content_hash": revision.content_hash}

    @staticmethod
    def _parse_command(
        workspace_id: str,
        chapter_document_id: str,
        value: Mapping[str, Any],
    ) -> ChapterGenerationRequest:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise _PrivateGenerationFault(
                400, "malformed_request", "Generation command fields are invalid."
            )
        if value.get("schema") != "webui-chapter-generation-command/v1":
            raise _PrivateGenerationFault(
                400, "malformed_request", "Generation command schema is invalid."
            )
        operation_key = value.get("operation_key")
        if not isinstance(operation_key, str) or not _IDENTITY.fullmatch(operation_key):
            raise _PrivateGenerationFault(
                400, "malformed_request", "operation_key is invalid."
            )
        requested_mode = value.get("requested_mode")
        if requested_mode == "legacy":
            raise _PrivateGenerationFault(
                409,
                "legacy_generation_deferred",
                "Legacy chapter generation is not bound in the browser-only release.",
            )
        if requested_mode != "plugin":
            raise _PrivateGenerationFault(
                400, "malformed_request", "requested_mode must be plugin."
            )
        expected = value.get("expected_base")
        if not isinstance(expected, Mapping) or set(expected) != _BASE_FIELDS:
            raise _PrivateGenerationFault(
                400, "malformed_request", "expected_base fields are invalid."
            )
        revision_id = expected.get("revision_id")
        content_hash = expected.get("content_hash")
        outline = value.get("outline")
        if not isinstance(revision_id, str) or not _IDENTITY.fullmatch(revision_id):
            raise _PrivateGenerationFault(
                400, "malformed_request", "expected revision_id is invalid."
            )
        if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
            raise _PrivateGenerationFault(
                400, "malformed_request", "expected content_hash is invalid."
            )
        if not isinstance(outline, str):
            raise _PrivateGenerationFault(
                400, "malformed_request", "outline must be a string."
            )
        return ChapterGenerationRequest(
            workspace_id,
            chapter_document_id,
            operation_key,
            "plugin",
            revision_id,
            content_hash,
            outline,
        )

    def _binding_availability(
        self, workspace_id: str, chapter_document_id: str
    ) -> BindingAvailability:
        try:
            answer = self.binding.availability(
                workspace_id=workspace_id,
                chapter_document_id=chapter_document_id,
            )
        except Exception:  # noqa: BLE001 - a binding failure must fail closed
            return BindingAvailability.unavailable(
                "binding_authority_error",
                "The verified chapter binding authority is unavailable.",
            )
        if not isinstance(answer, BindingAvailability):
            return BindingAvailability.unavailable(
                "binding_authority_invalid",
                "The verified chapter binding authority returned an invalid result.",
            )
        if answer.status not in {"available", "unavailable"}:
            return BindingAvailability.unavailable(
                "binding_authority_invalid",
                "The verified chapter binding authority returned an invalid result.",
            )
        if answer.status == "available" and (
            answer.reason_code is not None or answer.reason is not None
        ):
            return BindingAvailability.unavailable(
                "binding_authority_invalid",
                "The verified chapter binding authority returned an invalid result.",
            )
        if answer.status == "unavailable" and (
            not isinstance(answer.reason_code, str)
            or not answer.reason_code
            or not isinstance(answer.reason, str)
            or not answer.reason
        ):
            return BindingAvailability.unavailable(
                "binding_authority_invalid",
                "The verified chapter binding authority returned an invalid result.",
            )
        return answer

    @staticmethod
    def _fence_projection(fence: ChapterGenerationWriterFence) -> dict[str, Any]:
        return {
            "operation": fence.operation,
            "writer_mode": fence.writer_mode,
            "job_id": fence.job_id,
            "job_start_operation_key": fence.operation_key,
            "chapter_writer_epoch": fence.writer_epoch,
        }

    def _job_projection(
        self, workspace_id: str, job_id: str
    ) -> tuple[int, dict[str, Any]]:
        return self.job_runtime.http.handle(
            "job.get",
            {
                "schema": "job-snapshot-query/v2",
                "workspace_id": workspace_id,
                "job_id": job_id,
            },
            path_identity={"workspace_id": workspace_id, "job_id": job_id},
        )

    def _reconcile_active_fence(
        self, fence: ChapterGenerationWriterFence
    ) -> tuple[ChapterGenerationWriterFence, dict[str, Any] | None]:
        if fence.state != "active":
            return fence, None
        status, projection = self._job_projection(fence.workspace_id, fence.job_id)
        if status == 200:
            snapshot = projection.get("snapshot")
            state = snapshot.get("state") if isinstance(snapshot, Mapping) else None
            if state in _TERMINAL_JOB_STATES:
                released = self.repository.release_chapter_generation_writer_fence(
                    workspace_id=fence.workspace_id,
                    chapter_document_id=fence.chapter_document_id,
                    operation=fence.operation,
                    writer_mode=fence.writer_mode,
                    job_id=fence.job_id,
                    writer_epoch=fence.writer_epoch,
                    release_operation_key=_stable_id(
                        "generation-release",
                        fence.workspace_id,
                        fence.chapter_document_id,
                        fence.operation,
                        fence.job_id,
                        fence.writer_epoch,
                    ),
                )
                return released, None
            return fence, projection

        receipt = self.job_runtime.execution_authority.get_production_start_receipt(
            fence.operation_key
        )
        if receipt is None:
            released = self.repository.release_chapter_generation_writer_fence(
                workspace_id=fence.workspace_id,
                chapter_document_id=fence.chapter_document_id,
                operation=fence.operation,
                writer_mode=fence.writer_mode,
                job_id=fence.job_id,
                writer_epoch=fence.writer_epoch,
                release_operation_key=_stable_id(
                    "generation-release",
                    fence.workspace_id,
                    fence.chapter_document_id,
                    fence.operation,
                    fence.job_id,
                    fence.writer_epoch,
                ),
            )
            return released, None
        # Any durable unresolved/uncertain receipt keeps the fence.  The facade
        # never guesses that a process is gone and never replays that identity.
        return fence, None

    def get(
        self, workspace_id: str, chapter_document_id: str
    ) -> tuple[int, dict[str, Any]]:
        with self._authority_lock:
            return self._get_locked(workspace_id, chapter_document_id)

    def _get_locked(
        self, workspace_id: str, chapter_document_id: str
    ) -> tuple[int, dict[str, Any]]:
        try:
            document = self._chapter(workspace_id, chapter_document_id)
            current_base = self._current_base(document)
        except (NotFoundError, ConflictError):
            return self._chapter_not_found(workspace_id, chapter_document_id)
        except _PrivateGenerationFault as fault:
            return self._error(
                fault.status,
                workspace_id,
                chapter_document_id,
                fault.error_code,
                fault.reason,
            )

        fence = self.repository.read_chapter_generation_writer_fence(
            workspace_id=workspace_id,
            chapter_document_id=chapter_document_id,
            operation=_CHAPTER_OPERATION,
        )
        current_job: dict[str, Any] | None = None
        if fence is not None and fence.state == "active":
            fence, current_job = self._reconcile_active_fence(fence)
        if fence is not None and fence.state == "active":
            return 200, {
                "schema": "webui-chapter-generation-status/v1",
                "workspace_id": workspace_id,
                "chapter_document_id": chapter_document_id,
                "status": "active",
                "reason_code": None,
                "reason": None,
                "current_base": current_base,
                "writer_fence": self._fence_projection(fence),
                "current_job": current_job,
            }

        availability = self._binding_availability(workspace_id, chapter_document_id)
        reason_code = availability.reason_code
        reason = availability.reason
        status = availability.status
        if current_base is None and status == "available":
            status = "unavailable"
            reason_code = "base_revision_unavailable"
            reason = "The chapter has no immutable base Revision."
        return 200, {
            "schema": "webui-chapter-generation-status/v1",
            "workspace_id": workspace_id,
            "chapter_document_id": chapter_document_id,
            "status": status,
            "reason_code": reason_code,
            "reason": reason,
            "current_base": current_base,
            "writer_fence": None,
            "current_job": None,
        }

    def _verify_binding(
        self,
        request: ChapterGenerationRequest,
        binding: VerifiedChapterRunSnapshotBinding,
    ) -> dict[str, Any]:
        if binding.request_fingerprint != request.fingerprint:
            raise _PrivateGenerationFault(
                409,
                "operation_payload_conflict",
                "The operation key is bound to different generation input.",
            )
        if (
            not _IDENTITY.fullmatch(binding.run_snapshot_asset_id)
            or not _HASH.fullmatch(binding.snapshot_hash)
            or not _IDENTITY.fullmatch(binding.run_intent_id)
            or not _IDENTITY.fullmatch(binding.capability_id)
        ):
            raise _PrivateGenerationFault(
                409,
                "binding_identity_conflict",
                "The verified RunSnapshot binding identity is invalid.",
            )
        try:
            self.assets.require(binding.run_snapshot_asset_id, mime="application/json")
            raw = self.assets.read(binding.run_snapshot_asset_id)
            value = parse_json_bytes(raw)
            if not isinstance(value, Mapping):
                raise TypeError("RunSnapshot is not an object")
            snapshot = dict(value)
            verify_snapshot(snapshot)
            if raw != canonical_bytes(snapshot):
                raise ValueError("RunSnapshot is not canonical")
            for reference in snapshot["asset_hashes"]:
                self.assets.require(
                    str(reference["asset_id"]), sha256=str(reference["sha256"])
                )
        except (
            ContractError,
            LookupError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise _PrivateGenerationFault(
                409,
                "verified_snapshot_unavailable",
                "The verified immutable RunSnapshot Asset is unavailable.",
            ) from exc

        if (
            snapshot["snapshot_hash"] != binding.snapshot_hash
            or snapshot["run_intent_id"] != binding.run_intent_id
            or snapshot["workspace_id"] != request.workspace_id
            or snapshot["scope"]["document_id"] != request.chapter_document_id
            or snapshot["scope"]["node_id"] is not None
            or snapshot["scope"]["operation"] != binding.capability_id
        ):
            raise _PrivateGenerationFault(
                409,
                "binding_identity_conflict",
                "The RunSnapshot does not match the chapter generation binding.",
            )
        chapter_inputs = [
            item
            for item in snapshot["input_revisions"]
            if item["document_id"] == request.chapter_document_id
        ]
        if len(chapter_inputs) != 1 or chapter_inputs[0] != {
            "document_id": request.chapter_document_id,
            "revision_id": request.expected_revision_id,
            "content_hash": request.expected_content_hash,
        }:
            raise _PrivateGenerationFault(
                409,
                "binding_identity_conflict",
                "The RunSnapshot is not bound to the requested chapter base.",
            )
        try:
            selected = self.job_runtime.capability_runtime.select(binding.capability_id)
        except (ContractError, KeyError, OSError, TypeError, ValueError) as exc:
            raise _PrivateGenerationFault(
                409,
                "verified_plugin_unavailable",
                "No verified installed plugin release provides this capability.",
            ) from exc
        releases = [
            item
            for item in snapshot["plugin_releases"]
            if item["plugin_id"] == selected.plugin_id
        ]
        if len(releases) != 1 or releases[0] != {
            "plugin_id": selected.plugin_id,
            "release_id": selected.release_id,
            "package_hash": selected.package_hash,
            "data_generation_id": selected.data_generation_id,
        }:
            raise _PrivateGenerationFault(
                409,
                "verified_plugin_unavailable",
                "The RunSnapshot plugin release is not the installed verified release.",
            )
        return snapshot

    def start(
        self,
        workspace_id: str,
        chapter_document_id: str,
        command: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        with self._authority_lock:
            return self._start_locked(workspace_id, chapter_document_id, command)

    def _start_locked(
        self,
        workspace_id: str,
        chapter_document_id: str,
        command: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        operation_key = command.get("operation_key") if isinstance(command, Mapping) else None
        operation_key = operation_key if isinstance(operation_key, str) else None
        try:
            document = self._chapter(workspace_id, chapter_document_id)
            request = self._parse_command(workspace_id, chapter_document_id, command)
            current_base = self._current_base(document)
            if current_base is None:
                return self._unavailable(
                    workspace_id,
                    chapter_document_id,
                    "base_revision_unavailable",
                    "The chapter has no immutable base Revision.",
                )
            if current_base != {
                "revision_id": request.expected_revision_id,
                "content_hash": request.expected_content_hash,
            }:
                raise _PrivateGenerationFault(
                    409, "stale_base", "The chapter base Revision has changed."
                )
            resolution = self.binding.resolve(request)
            if isinstance(resolution, BindingUnavailable):
                if (
                    not isinstance(resolution.reason_code, str)
                    or not _REASON_CODE.fullmatch(resolution.reason_code)
                    or not isinstance(resolution.reason, str)
                    or not resolution.reason
                ):
                    return self._unavailable(
                        workspace_id,
                        chapter_document_id,
                        "binding_authority_invalid",
                        "The verified chapter binding authority returned an invalid result.",
                    )
                return self._unavailable(
                    workspace_id,
                    chapter_document_id,
                    resolution.reason_code,
                    resolution.reason,
                )
            if not isinstance(resolution, VerifiedChapterRunSnapshotBinding):
                return self._unavailable(
                    workspace_id,
                    chapter_document_id,
                    "binding_authority_invalid",
                    "The verified chapter binding authority returned an invalid result.",
                )
            snapshot = self._verify_binding(request, resolution)
        except (NotFoundError, ConflictError):
            return self._chapter_not_found(workspace_id, chapter_document_id)
        except _PrivateGenerationFault as fault:
            if fault.error_code in {
                "legacy_generation_deferred",
                "verified_plugin_unavailable",
                "verified_snapshot_unavailable",
            }:
                return self._unavailable(
                    workspace_id,
                    chapter_document_id,
                    fault.error_code,
                    fault.reason,
                )
            return self._error(
                fault.status,
                workspace_id,
                chapter_document_id,
                fault.error_code,
                fault.reason,
                operation_key=operation_key,
            )
        except Exception:  # noqa: BLE001 - binding implementations fail closed
            return self._unavailable(
                workspace_id,
                chapter_document_id,
                "binding_authority_error",
                "The verified chapter binding authority is unavailable.",
            )

        job_start_operation_key = _stable_id(
            "generation-start", workspace_id, chapter_document_id, request.operation_key
        )
        job_id = _stable_id(
            "job",
            workspace_id,
            chapter_document_id,
            request.operation_key,
            resolution.run_intent_id,
            resolution.snapshot_hash,
        )
        existing_fence = self.repository.read_chapter_generation_writer_fence(
            workspace_id=workspace_id,
            chapter_document_id=chapter_document_id,
            operation=_CHAPTER_OPERATION,
        )
        if existing_fence is not None and existing_fence.state == "active":
            self._reconcile_active_fence(existing_fence)
        try:
            fence = self.repository.acquire_chapter_generation_writer_fence(
                workspace_id=workspace_id,
                chapter_document_id=chapter_document_id,
                operation=_CHAPTER_OPERATION,
                writer_mode="plugin",
                job_id=job_id,
                operation_key=job_start_operation_key,
            )
        except ConflictError as exc:
            code = (
                "operation_payload_conflict"
                if "operation key" in str(exc)
                else "writer_active"
            )
            reason = (
                "The operation key is bound to different generation input."
                if code == "operation_payload_conflict"
                else "Another chapter generation task is still active."
            )
            return self._error(
                409,
                workspace_id,
                chapter_document_id,
                code,
                reason,
                operation_key=request.operation_key,
            )

        # Jobs v2 writer_epoch is the first epoch of this newly created Job. It
        # is intentionally distinct from the chapter fence's monotonic epoch.
        job_command = {
            "schema": "job-start-command/v2",
            "operation_key": job_start_operation_key,
            "workspace_id": workspace_id,
            "job_id": job_id,
            "capability_id": resolution.capability_id,
            "run_snapshot_asset_id": resolution.run_snapshot_asset_id,
            "writer_epoch": 1,
        }
        status, job_result = self.job_runtime.http.handle(
            "job.start",
            job_command,
            path_identity={"workspace_id": workspace_id},
        )
        if status != 201:
            # Release only when durable WU2B authority proves no start receipt
            # and no Job.  Uncertain/active identities remain fenced.
            fence, _ = self._reconcile_active_fence(fence)
            error_code = str(job_result.get("error_code", "invalid_transition"))
            message = str(job_result.get("message", "Job start was rejected."))
            lowered = message.lower()
            if error_code == "duplicate_operation":
                private_code = "operation_payload_conflict"
            elif "uncertain" in lowered:
                private_code = "uncertain_start"
            elif fence.state == "active":
                private_code = "job_start_unresolved"
            else:
                private_code = "job_start_unavailable"
            return self._error(
                409,
                workspace_id,
                chapter_document_id,
                private_code,
                message,
                operation_key=request.operation_key,
                retryable=False,
            )
        if job_result.get("schema") != "job-command-result/v2":
            return self._error(
                409,
                workspace_id,
                chapter_document_id,
                "job_result_invalid",
                "The Job authority returned an incomplete result.",
                operation_key=request.operation_key,
            )
        return 201, {
            "schema": "webui-chapter-generation-result/v1",
            "workspace_id": workspace_id,
            "chapter_document_id": chapter_document_id,
            "operation_key": request.operation_key,
            "job_id": job_id,
            "run_snapshot_hash": str(snapshot["snapshot_hash"]),
            "writer_fence": self._fence_projection(fence),
            "job_result": job_result,
        }


__all__ = [
    "BindingAvailability",
    "BindingUnavailable",
    "ChapterGenerationRequest",
    "ChapterRunSnapshotBindingPort",
    "PrivateChapterGenerationFacade",
    "UnavailableChapterRunSnapshotBinding",
    "VerifiedChapterRunSnapshotBinding",
]
