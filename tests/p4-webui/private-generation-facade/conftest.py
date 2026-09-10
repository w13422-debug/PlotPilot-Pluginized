"""Fixtures for the private chapter-generation facade."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.production_job_runtime import ProductionJobRuntime
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.jobs.production import ProductionCapabilitySelection
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.webui import (
    BindingAvailability,
    BindingUnavailable,
    ChapterGenerationRequest,
    PrivateChapterGenerationFacade,
    VerifiedChapterRunSnapshotBinding,
)
from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

CAPABILITY_ID = "writing.chapter.draft/v1"
PLUGIN_ID = "com.plotpilot.chapter-workflow"
NOW = "2026-09-09T00:00:00Z"


class FakeExecutionAuthority:
    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}

    def get_production_start_receipt(
        self, operation_key: str
    ) -> dict[str, Any] | None:
        value = self.receipts.get(operation_key)
        return None if value is None else copy.deepcopy(value)


class FakeCapabilityRuntime:
    def __init__(self, selection: ProductionCapabilitySelection) -> None:
        self.selection = selection
        self.enabled = True

    def select(self, capability_id: str) -> ProductionCapabilitySelection:
        if not self.enabled or capability_id != self.selection.capability_id:
            from backend.plotpilot_plugin_sdk import ContractError, ErrorCode

            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Disabled: no verified installed release provides the capability",
            )
        return self.selection


class FakeJobHttp:
    def __init__(self, execution: FakeExecutionAuthority) -> None:
        self.execution = execution
        self._lock = threading.RLock()
        self.commands: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.job_states: dict[str, str] = {}
        self.process_starts = 0
        self.fail_uncertain = False
        self.malformed_success = False

    def handle(
        self,
        route_id: str,
        request: dict[str, Any],
        *,
        path_identity: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if route_id == "job.get":
                state = self.job_states.get(request["job_id"])
                if state is None:
                    return 404, {
                        "schema": "job-http-error/v2",
                        "error_code": "unknown_job",
                        "message": "unknown Job",
                    }
                return 200, {
                    "schema": "job-snapshot-result/v2",
                    "workspace_id": request["workspace_id"],
                    "job_id": request["job_id"],
                    "snapshot": {"state": state},
                    "cursor": f"job/{request['job_id']}/1",
                }
            assert route_id == "job.start"
            assert path_identity == {"workspace_id": request["workspace_id"]}
            key = request["operation_key"]
            previous = self.commands.get(key)
            if previous is not None:
                if previous != request:
                    return 409, {
                        "schema": "job-http-error/v2",
                        "error_code": "duplicate_operation",
                        "message": "job.start operation key drifted",
                    }
                if self.fail_uncertain:
                    return 409, {
                        "schema": "job-http-error/v2",
                        "error_code": "invalid_transition",
                        "message": "job.start outcome is uncertain",
                    }
                return 201, copy.deepcopy(self.results[key])

            self.commands[key] = copy.deepcopy(request)
            self.process_starts += 1
            if self.fail_uncertain:
                self.execution.receipts[key] = {"state": "uncertain"}
                return 409, {
                    "schema": "job-http-error/v2",
                    "error_code": "invalid_transition",
                    "message": "job.start outcome is uncertain",
                }
            self.execution.receipts[key] = {"state": "completed"}
            self.job_states[request["job_id"]] = "running"
            result = {
                "schema": "job-command-result/v2",
                "operation_key": key,
                "workspace_id": request["workspace_id"],
                "job_id": request["job_id"],
                "command": "start",
                "accepted": True,
                "idempotent": False,
                "terminal_known": False,
                "state": "running",
                "job_revision": 2,
                "snapshot_cursor": f"job/{request['job_id']}/1",
            }
            if self.malformed_success:
                result = {"schema": "wrong-result/v1"}
            self.results[key] = copy.deepcopy(result)
            return 201, result


class MutableBinding:
    def __init__(self) -> None:
        self.enabled = True
        self.resolutions: dict[str, VerifiedChapterRunSnapshotBinding] = {}
        self.forced: BindingUnavailable | object | None = None

    def availability(
        self, *, workspace_id: str, chapter_document_id: str
    ) -> BindingAvailability:
        del workspace_id, chapter_document_id
        if self.enabled:
            return BindingAvailability.available()
        return BindingAvailability.unavailable("binding_disabled", "Binding disabled.")

    def resolve(
        self, request: ChapterGenerationRequest
    ) -> VerifiedChapterRunSnapshotBinding | BindingUnavailable | object:
        if self.forced is not None:
            return self.forced
        if not self.enabled:
            return BindingUnavailable("binding_disabled", "Binding disabled.")
        return self.resolutions[request.fingerprint]


@dataclass(slots=True)
class FacadeStack:
    root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    runtime: ProductionJobRuntime
    http: FakeJobHttp
    execution: FakeExecutionAuthority
    capability_runtime: FakeCapabilityRuntime
    binding: MutableBinding
    facade: PrivateChapterGenerationFacade
    base_revision_id: str
    base_content_hash: str

    def command(
        self,
        operation_key: str = "browser-op-1",
        *,
        outline: str = "Beat one",
        requested_mode: str = "plugin",
        revision_id: str | None = None,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": "webui-chapter-generation-command/v1",
            "operation_key": operation_key,
            "requested_mode": requested_mode,
            "expected_base": {
                "revision_id": revision_id or self.base_revision_id,
                "content_hash": content_hash or self.base_content_hash,
            },
            "outline": outline,
        }

    def request(self, command: dict[str, Any] | None = None) -> ChapterGenerationRequest:
        value = command or self.command()
        return ChapterGenerationRequest(
            "ws-1",
            "chapter-1",
            value["operation_key"],
            "plugin",
            value["expected_base"]["revision_id"],
            value["expected_base"]["content_hash"],
            value["outline"],
        )

    def bind(
        self,
        command: dict[str, Any] | None = None,
        *,
        snapshot_id: str = "snapshot-1",
        run_intent_id: str = "run-intent-1",
        capability_id: str = CAPABILITY_ID,
        input_revision_id: str | None = None,
        input_content_hash: str | None = None,
    ) -> VerifiedChapterRunSnapshotBinding:
        request = self.request(command)
        selection = self.capability_runtime.selection
        value: dict[str, Any] = {
            "schema": "run-snapshot/v1",
            "snapshot_id": snapshot_id,
            "core_contract_version": "1.2.0",
            "workspace_id": request.workspace_id,
            "scope": {
                "document_id": request.chapter_document_id,
                "node_id": None,
                "operation": capability_id,
            },
            "input_revisions": [
                {
                    "document_id": request.chapter_document_id,
                    "revision_id": input_revision_id or request.expected_revision_id,
                    "content_hash": input_content_hash or request.expected_content_hash,
                }
            ],
            "plan_revision_id": self.base_revision_id,
            "plugin_releases": [
                {
                    "plugin_id": selection.plugin_id,
                    "release_id": selection.release_id,
                    "package_hash": selection.package_hash,
                    "data_generation_id": selection.data_generation_id,
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
        asset = self.assets.put(
            canonical_bytes(value),
            mime="application/json",
            logical_role="run-snapshot",
            provenance="test:wu3",
        )
        result = VerifiedChapterRunSnapshotBinding(
            asset.asset_id,
            value["snapshot_hash"],
            run_intent_id,
            capability_id,
            request.fingerprint,
        )
        self.binding.resolutions[request.fingerprint] = result
        return result

    def close(self) -> None:
        self.repository.close()


@pytest.fixture
def facade_stack(tmp_path: Path) -> FacadeStack:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel one"))
    repository.create_workspace(Workspace("ws-2", "Novel two"))
    repository.create_document(
        Document("chapter-1", "ws-1", "Chapter one", "core.chapter")
    )
    repository.create_document(
        Document("chapter-other", "ws-2", "Other", "core.chapter")
    )
    revision = repository.publish_revision(
        document_id="chapter-1",
        content="Opening paragraph",
        expected_revision_id=None,
        created_by="test",
        revision_id="revision-chapter-1",
    )
    selection = ProductionCapabilitySelection(
        generation_id="generation-1",
        worker_id=PLUGIN_ID,
        plugin_id=PLUGIN_ID,
        release_id="release-chapter-workflow-1",
        package_hash="a" * 64,
        data_generation_id=None,
        version="1.0.0",
        capability_id=CAPABILITY_ID,
        result_contract="candidate-item/v1",
    )
    execution = FakeExecutionAuthority()
    http = FakeJobHttp(execution)
    capability_runtime = FakeCapabilityRuntime(selection)
    runtime = object.__new__(ProductionJobRuntime)
    runtime.repository = repository
    runtime.assets = assets
    runtime.execution_authority = execution
    runtime.capability_runtime = capability_runtime
    runtime.http = http
    binding = MutableBinding()
    facade = PrivateChapterGenerationFacade(repository, assets, runtime, binding)
    stack = FacadeStack(
        tmp_path,
        repository,
        assets,
        runtime,
        http,
        execution,
        capability_runtime,
        binding,
        facade,
        revision.revision_id,
        revision.content_hash,
    )
    try:
        yield stack
    finally:
        stack.close()
