from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_core.api.v1.core import CoreHttpAdapter, create_core_router
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.configuration import (
    DuplicateConfigurationOperationError,
    GenerationConfigurationConflictError,
    MalformedConfigurationRequestError,
    ModelConfigurationAuthority,
    PlanningUnavailableError,
    StaleConfigurationCasError,
    UnknownConfigurationReferenceError,
    WorkspacePlanAuthority,
)
from backend.plotpilot_core.configuration.plan_authority import (
    PROJECT_BRIEF_DOCUMENT_TYPE,
    PROJECT_BRIEF_PAYLOAD_SCHEMA,
)
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.plugins.generation import validate_generation
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)
from backend.plotpilot_core.publication import PublicationService

NOW = "2030-01-02T03:04:05Z"
PROVIDER_ID = "com.plotpilot.provider.local"
PROVIDER_RELEASE = "a" * 64
PLAN_HASH = "b" * 64


def _member(plugin_id: str, character: str) -> dict:
    return {
        "plugin_id": plugin_id,
        "release_id": character * 64,
        "package_hash": character * 64,
        "data_generation_id": None,
        "ui_bundle_hash": None,
        "global_settings_revision_id": None,
        "settings_schema_hash": None,
        "data_bundle_asset_id": None,
    }


def _generation(
    generation_id: str = "generation-planning-1",
    *,
    planner: bool = True,
    provider: bool = True,
    prompt: bool = True,
) -> dict:
    members = []
    if planner:
        members.append(_member("com.plotpilot.project-planner", "b"))
    if prompt:
        members.append(_member("com.plotpilot.prompt-skill-runtime", "c"))
    if provider:
        members.append(_member(PROVIDER_ID, "a"))
    members.sort(key=lambda item: item["plugin_id"].encode())
    return validate_generation(
        {
            "schema": "plugin-generation/v1",
            "generation_id": generation_id,
            "core_api_version": "1.2.0",
            "members": members,
            "created_reason": "P1 test",
            "created_at": NOW,
            "health_result_asset_id": f"health-{generation_id}",
            "parent_generation_id": None,
            "base_generation_id": None,
        }
    )


def _install_generation(
    repository: CoreAuthorityRepository, generation: dict
) -> None:
    payload = canonical_bytes(generation)
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO p2_plugin_generation(generation_id,payload_json,payload_hash) "
            "VALUES(?,?,?)",
            (
                generation["generation_id"],
                payload.decode(),
                hashlib.sha256(payload).hexdigest(),
            ),
        )
        connection.execute(
            "UPDATE p2_plugin_generation_pointer SET current_generation_id=?,"
            "safe_mode=0,safe_mode_reason=NULL WHERE singleton=1",
            (generation["generation_id"],),
        )


def _configuration(
    repository: CoreAuthorityRepository,
    *,
    workspace_id: str = "workspace-1",
    workspace_revision: int = 0,
) -> tuple[ModelConfigurationAuthority, WorkspacePlanAuthority, dict]:
    repository.create_workspace(
        Workspace(workspace_id, "Novel", revision=workspace_revision)
    )
    configuration = ModelConfigurationAuthority(
        repository,
        clock=lambda: NOW,
        revision_id_factory=lambda: f"profile-revision-{workspace_id}",
    )
    selection_numbers = iter(range(1, 100))
    planning = WorkspacePlanAuthority(
        repository,
        clock=lambda: NOW,
        selection_id_factory=lambda: (
            f"selection-{workspace_id}-{next(selection_numbers)}"
        ),
    )
    configuration.put_secret(
        {
            "schema": "model-secret-put-command/v2",
            "operation_key": f"secret-{workspace_id}",
            "secret_id": f"secret-{workspace_id}",
            "value": f"RAW_VALUE_{workspace_id}_3451a7c8",
        }
    )
    profile = configuration.revise_profile(
        {
            "schema": "model-profile-revise-command/v2",
            "operation_key": f"profile-{workspace_id}",
            "profile_id": f"profile-{workspace_id}",
            "expected_parent_revision_id": None,
            "provider": {
                "plugin_id": PROVIDER_ID,
                "release_id": PROVIDER_RELEASE,
                "endpoint": "https://models.example.test/v1",
                "model_name": "planner-model",
                "options": {
                    "temperature": 0.4,
                    "top_p": 0.9,
                    "max_output_tokens": 4096,
                    "timeout_seconds": 120,
                    "max_retries": 0,
                },
                "api_key_ref": f"secret://secret-{workspace_id}",
            },
        }
    )["revision"]
    return configuration, planning, profile


def _selection_command(
    profile: dict,
    *,
    workspace_id: str = "workspace-1",
    operation_key: str = "plan-operation-1",
    expected_workspace_revision: int = 0,
    expected_plan_id: str | None = None,
    expected_plan_hash: str | None = None,
    plan_revision_id: str = "plan-revision-1",
    plan_revision_hash: str = PLAN_HASH,
    generation_id: str = "generation-planning-1",
) -> dict:
    return {
        "schema": "workspace-plan-selection-command/v2",
        "operation_key": operation_key,
        "workspace_id": workspace_id,
        "expected_workspace_revision": expected_workspace_revision,
        "expected_current_plan_revision_id": expected_plan_id,
        "expected_current_plan_revision_hash": expected_plan_hash,
        "selection_mode": "explicit",
        "plan_revision_id": plan_revision_id,
        "plan_revision_hash": plan_revision_hash,
        "model_profile_revision_id": profile["revision_id"],
        "model_profile_revision_hash": profile["revision_hash"],
        "expected_active_generation_id": generation_id,
    }


def _add_brief(
    repository: CoreAuthorityRepository,
    workspace_id: str = "workspace-1",
    document_id: str = "project-brief-1",
) -> dict[str, str]:
    repository.create_document(
        Document(
            document_id,
            workspace_id,
            "Project Brief",
            PROJECT_BRIEF_DOCUMENT_TYPE,
        )
    )
    revision = repository.publish_revision(
        document_id=document_id,
        content=json.dumps({"workspace_id": workspace_id, "premise": "A premise"}),
        expected_revision_id=None,
        created_by="test",
        payload_schema=PROJECT_BRIEF_PAYLOAD_SCHEMA,
        revision_id=f"revision-{document_id}",
    )
    return {
        "document_id": document_id,
        "revision_id": revision.revision_id,
        "content_hash": revision.content_hash,
    }


def test_internal_plan_registration_selection_replay_and_only_workspace_pointer(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(
        repository, workspace_revision=7
    )
    generation = _generation()
    _install_generation(repository, generation)
    try:
        reference = planning.register_verified_plan_reference(
            generation_id=generation["generation_id"],
            plan_revision_id="plan-revision-7",
            plan_revision_hash=PLAN_HASH,
        )
        assert reference.generation_id == generation["generation_id"]
        command = _selection_command(
            profile,
            expected_workspace_revision=7,
            plan_revision_id="plan-revision-7",
        )
        selected = planning.select_workspace_plan(command)
        assert selected["workspace_revision"] == 8
        assert selected["previous_plan_revision_id"] is None
        assert selected["idempotent"] is False
        assert planning.select_workspace_plan(command) == {
            **selected,
            "idempotent": True,
        }
        assert repository.get_workspace("workspace-1").current_plan_revision_id == (
            "plan-revision-7"
        )

        with repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_workspace_plan_selection"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM p1_configuration_operation "
                "WHERE route_id='workspace-plan.select'"
            ).fetchone()[0] == 1
            pointer_columns: list[tuple[str, str]] = []
            for table in (
                "workspace",
                "p1_plan_revision_reference",
                "p1_workspace_plan_selection",
                "p1_configuration_operation",
            ):
                for column in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall():
                    if column[1] == "current_plan_revision_id":
                        pointer_columns.append((table, column[1]))
            assert pointer_columns == [("workspace", "current_plan_revision_id")]
    finally:
        repository.close()


def test_second_selection_advances_once_and_preserves_hash_bound_history(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(repository)
    generation = _generation()
    _install_generation(repository, generation)
    try:
        planning.register_verified_plan_reference(
            generation_id=generation["generation_id"],
            plan_revision_id="plan-revision-1",
            plan_revision_hash=PLAN_HASH,
        )
        _add_brief(repository)
        first = planning.select_workspace_plan(_selection_command(profile))
        assert planning.planning_availability("workspace-1")["reason"] == "ready"
        # A title/status update advances Workspace CAS without changing the
        # sole Plan pointer; history must still supply the current Plan hash.
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE workspace SET title='Renamed',revision=revision+1 "
                "WHERE workspace_id='workspace-1'"
            )
        assert planning.planning_availability("workspace-1")["reason"] == "ready"
        next_hash = "d" * 64
        planning.register_verified_plan_reference(
            generation_id=generation["generation_id"],
            plan_revision_id="plan-revision-2",
            plan_revision_hash=next_hash,
        )
        second = planning.select_workspace_plan(
            _selection_command(
                profile,
                operation_key="plan-operation-2",
                expected_workspace_revision=2,
                expected_plan_id="plan-revision-1",
                expected_plan_hash=PLAN_HASH,
                plan_revision_id="plan-revision-2",
                plan_revision_hash=next_hash,
            )
        )
        assert first["workspace_revision"] == 1
        assert second["workspace_revision"] == 3
        assert second["previous_plan_revision_id"] == "plan-revision-1"
        assert second["previous_plan_revision_hash"] == PLAN_HASH
        with repository.read_connection() as connection:
            rows = connection.execute(
                "SELECT plan_revision_id,plan_revision_hash,workspace_revision "
                "FROM p1_workspace_plan_selection ORDER BY workspace_revision"
            ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("plan-revision-1", PLAN_HASH, 1),
            ("plan-revision-2", next_hash, 3),
        ]
    finally:
        repository.close()


def test_workspace_delete_with_plan_history_returns_closed_409_without_effect(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(repository)
    generation = _generation()
    _install_generation(repository, generation)
    planning.register_verified_plan_reference(
        generation_id=generation["generation_id"],
        plan_revision_id="plan-revision-1",
        plan_revision_hash=PLAN_HASH,
    )
    planning.select_workspace_plan(_selection_command(profile))
    assets = AssetStore(tmp_path / "assets")
    publication = PublicationService(repository, assets)
    authority = CoreAuthorityApplication(repository, publication)
    adapter = CoreHttpAdapter(authority, publication, assets)
    app = FastAPI()
    app.include_router(create_core_router(adapter))
    client = TestClient(app)
    try:
        with repository.read_connection() as connection:
            before = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "workspace",
                    "p1_workspace_plan_selection",
                    "p1_configuration_operation",
                    "core_authority_operation",
                    "schema_migration",
                )
            }
        response = client.request(
            "DELETE",
            "/api/v1/core/workspaces/workspace-1",
            json={
                "schema": "core-workspace-delete-command/v1",
                "operation_key": "delete-workspace-with-plan",
                "workspace_id": "workspace-1",
                "expected_revision": 1,
            },
        )
        assert response.status_code == 409
        assert response.json() == {
            "schema": "core-http-error/v1",
            "error_code": "stale_cas",
            "message": "workspace has dependent authority",
            "retryable": False,
        }
        with repository.read_connection() as connection:
            after = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in before
            }
            assert connection.execute(
                "SELECT current_plan_revision_id FROM workspace "
                "WHERE workspace_id='workspace-1'"
            ).fetchone()[0] == "plan-revision-1"
        assert after == before
    finally:
        client.close()
        repository.close()


def test_plan_registration_is_internal_generation_bound_and_immutable(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    planning = WorkspacePlanAuthority(repository, clock=lambda: NOW)
    try:
        with pytest.raises(GenerationConfigurationConflictError):
            planning.register_verified_plan_reference(
                generation_id="generation-missing",
                plan_revision_id="plan-1",
                plan_revision_hash=PLAN_HASH,
            )
        with pytest.raises(MalformedConfigurationRequestError):
            planning.register_verified_plan_reference(
                generation_id="generation/malformed whitespace",
                plan_revision_id="plan-1",
                plan_revision_hash=PLAN_HASH,
            )
        generation = _generation()
        _install_generation(repository, generation)
        first = planning.register_verified_plan_reference(
            generation_id=generation["generation_id"],
            plan_revision_id="plan-1",
            plan_revision_hash=PLAN_HASH,
        )
        assert planning.register_verified_plan_reference(
            generation_id=generation["generation_id"],
            plan_revision_id="plan-1",
            plan_revision_hash=PLAN_HASH,
        ) == first
        with pytest.raises(GenerationConfigurationConflictError):
            planning.register_verified_plan_reference(
                generation_id=generation["generation_id"],
                plan_revision_id="plan-1",
                plan_revision_hash="c" * 64,
            )
        with pytest.raises(GenerationConfigurationConflictError):
            planning.register_verified_plan_reference(
                generation_id=generation["generation_id"],
                plan_revision_id="plan-other",
                plan_revision_hash=PLAN_HASH,
            )
        with repository.read_connection() as connection:
            with pytest.raises(sqlite3.Error, match="immutable"):
                connection.execute(
                    "UPDATE p1_plan_revision_reference SET plan_revision_id='changed'"
                )
    finally:
        repository.close()


def test_selection_rejects_unregistered_stale_generation_profile_and_key_reuse(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(repository)
    generation = _generation()
    _install_generation(repository, generation)
    try:
        command = _selection_command(profile)
        with pytest.raises(UnknownConfigurationReferenceError):
            planning.select_workspace_plan(command)
        planning.register_verified_plan_reference(
            generation_id=generation["generation_id"],
            plan_revision_id="plan-revision-1",
            plan_revision_hash=PLAN_HASH,
        )
        with pytest.raises(StaleConfigurationCasError):
            planning.select_workspace_plan(
                {**command, "expected_workspace_revision": 1}
            )
        with pytest.raises(GenerationConfigurationConflictError):
            planning.select_workspace_plan(
                {**command, "expected_active_generation_id": "generation-other"}
            )
        with pytest.raises(UnknownConfigurationReferenceError):
            planning.select_workspace_plan(
                {**command, "model_profile_revision_hash": "e" * 64}
            )

        selected = planning.select_workspace_plan(command)
        with pytest.raises(DuplicateConfigurationOperationError):
            planning.select_workspace_plan(
                {**command, "plan_revision_hash": "f" * 64}
            )
        assert repository.get_workspace("workspace-1").revision == 1
        assert selected["workspace_revision"] == 1
    finally:
        repository.close()


def test_selection_receipt_failure_rolls_back_pointer_history_and_operation(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(repository)
    generation = _generation()
    _install_generation(repository, generation)
    planning.register_verified_plan_reference(
        generation_id=generation["generation_id"],
        plan_revision_id="plan-revision-1",
        plan_revision_hash=PLAN_HASH,
    )
    try:
        with repository.read_connection() as connection:
            connection.execute(
                "CREATE TEMP TRIGGER fail_plan_receipt BEFORE INSERT ON "
                "p1_configuration_operation WHEN NEW.route_id='workspace-plan.select' "
                "BEGIN SELECT RAISE(ABORT,'injected Plan receipt failure'); END"
            )
        with pytest.raises(sqlite3.Error, match="injected Plan receipt failure"):
            planning.select_workspace_plan(_selection_command(profile))
        workspace = repository.get_workspace("workspace-1")
        assert workspace.revision == 0
        assert workspace.current_plan_revision_id is None
        with repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_workspace_plan_selection"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM p1_configuration_operation "
                "WHERE route_id='workspace-plan.select'"
            ).fetchone()[0] == 0
    finally:
        repository.close()


def test_concurrent_plan_cas_has_one_winner_and_one_stale_loser(tmp_path) -> None:
    database = tmp_path / "core.db"
    first_repository = CoreAuthorityRepository(database)
    _, first_planning, profile = _configuration(first_repository)
    generation = _generation()
    _install_generation(first_repository, generation)
    first_planning.register_verified_plan_reference(
        generation_id=generation["generation_id"],
        plan_revision_id="plan-a",
        plan_revision_hash="1" * 64,
    )
    first_planning.register_verified_plan_reference(
        generation_id=generation["generation_id"],
        plan_revision_id="plan-b",
        plan_revision_hash="2" * 64,
    )
    second_repository = CoreAuthorityRepository(database)
    second_planning = WorkspacePlanAuthority(second_repository, clock=lambda: NOW)
    barrier = threading.Barrier(2)

    def select(authority: WorkspacePlanAuthority, suffix: str) -> str:
        barrier.wait(timeout=5)
        try:
            authority.select_workspace_plan(
                _selection_command(
                    profile,
                    operation_key=f"plan-{suffix}",
                    plan_revision_id=f"plan-{suffix}",
                    plan_revision_hash=("1" if suffix == "a" else "2") * 64,
                )
            )
            return "winner"
        except StaleConfigurationCasError:
            return "stale"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(select, first_planning, "a"),
                pool.submit(select, second_planning, "b"),
            )
            outcomes = [future.result(timeout=10) for future in futures]
        assert sorted(outcomes) == ["stale", "winner"]
        with first_repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_workspace_plan_selection"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT revision FROM workspace WHERE workspace_id='workspace-1'"
            ).fetchone()[0] == 1
    finally:
        second_repository.close()
        first_repository.close()


def test_planning_availability_has_deterministic_fail_closed_precedence(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(repository)
    try:
        assert planning.planning_availability("workspace-1")["reason"] == (
            "project_brief_missing"
        )
        _add_brief(repository)
        assert planning.planning_availability("workspace-1")["reason"] == (
            "active_generation_missing"
        )

        without_planner = _generation("generation-without-planner", planner=False)
        _install_generation(repository, without_planner)
        assert planning.planning_availability("workspace-1")["reason"] == (
            "planner_package_missing"
        )

        complete = _generation("generation-complete")
        _install_generation(repository, complete)
        assert planning.planning_availability("workspace-1")["reason"] == (
            "workspace_plan_missing"
        )
        planning.register_verified_plan_reference(
            generation_id=complete["generation_id"],
            plan_revision_id="plan-revision-1",
            plan_revision_hash=PLAN_HASH,
        )
        planning.select_workspace_plan(
            _selection_command(
                profile,
                generation_id=complete["generation_id"],
            )
        )
        assert planning.planning_availability("workspace-1") == {
            "schema": "project-planning-availability-result/v2",
            "workspace_id": "workspace-1",
            "available": True,
            "reason": "ready",
        }
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("provider", "prompt", "reason"),
    (
        (False, True, "provider_unavailable"),
        (True, False, "prompt_skill_unavailable"),
    ),
)
def test_planning_availability_provider_and_prompt_fail_closed(
    tmp_path, provider: bool, prompt: bool, reason: str
) -> None:
    root = tmp_path / reason
    root.mkdir()
    repository = CoreAuthorityRepository(root / "core.db")
    _, planning, profile = _configuration(repository)
    generation = _generation(provider=provider, prompt=prompt)
    _install_generation(repository, generation)
    _add_brief(repository)
    planning.register_verified_plan_reference(
        generation_id=generation["generation_id"],
        plan_revision_id="plan-revision-1",
        plan_revision_hash=PLAN_HASH,
    )
    planning.select_workspace_plan(_selection_command(profile))
    try:
        availability = planning.planning_availability("workspace-1")
        assert availability["available"] is False
        assert availability["reason"] == reason
    finally:
        repository.close()


def test_latest_selection_blocks_generation_rollback_until_new_explicit_cas(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, profile = _configuration(repository)
    _add_brief(repository)
    generation_one = _generation("generation-1")
    generation_two = _generation("generation-2")
    plan_id = "plan-shared"
    hash_one = "1" * 64
    hash_two = "2" * 64
    try:
        _install_generation(repository, generation_one)
        planning.register_verified_plan_reference(
            generation_id="generation-1",
            plan_revision_id=plan_id,
            plan_revision_hash=hash_one,
        )
        planning.select_workspace_plan(
            _selection_command(
                profile,
                operation_key="select-generation-1",
                plan_revision_id=plan_id,
                plan_revision_hash=hash_one,
                generation_id="generation-1",
            )
        )

        _install_generation(repository, generation_two)
        planning.register_verified_plan_reference(
            generation_id="generation-2",
            plan_revision_id=plan_id,
            plan_revision_hash=hash_two,
        )
        planning.select_workspace_plan(
            _selection_command(
                profile,
                operation_key="select-generation-2",
                expected_workspace_revision=1,
                expected_plan_id=plan_id,
                expected_plan_hash=hash_one,
                plan_revision_id=plan_id,
                plan_revision_hash=hash_two,
                generation_id="generation-2",
            )
        )
        assert planning.planning_availability("workspace-1")["reason"] == "ready"

        with repository.transaction() as connection:
            connection.execute(
                "UPDATE p2_plugin_generation_pointer SET current_generation_id=? "
                "WHERE singleton=1",
                ("generation-1",),
            )
        rolled_back = planning.planning_availability("workspace-1")
        assert rolled_back == {
            "schema": "project-planning-availability-result/v2",
            "workspace_id": "workspace-1",
            "available": False,
            "reason": "workspace_plan_missing",
        }

        selected = planning.select_workspace_plan(
            _selection_command(
                profile,
                operation_key="reselect-generation-1",
                expected_workspace_revision=2,
                expected_plan_id=plan_id,
                expected_plan_hash=hash_two,
                plan_revision_id=plan_id,
                plan_revision_hash=hash_one,
                generation_id="generation-1",
            )
        )
        assert selected["workspace_revision"] == 3
        assert planning.planning_availability("workspace-1")["reason"] == "ready"
        with repository.read_connection() as connection:
            latest = connection.execute(
                "SELECT active_generation_id,plan_revision_hash,workspace_revision "
                "FROM p1_workspace_plan_selection WHERE workspace_id='workspace-1' "
                "ORDER BY workspace_revision DESC LIMIT 1"
            ).fetchone()
        assert tuple(latest) == ("generation-1", hash_one, 3)
    finally:
        repository.close()


def test_planning_start_validates_brief_cas_but_creates_zero_job_rows(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    _, planning, _ = _configuration(repository)
    brief = _add_brief(repository)
    command = {
        "schema": "project-planning-start-command/v2",
        "operation_key": "planning-operation-1",
        "workspace_id": "workspace-1",
        "requested_mode": "plugin",
        "project_brief_document_id": brief["document_id"],
        "expected_project_brief": {
            "revision_id": brief["revision_id"],
            "content_hash": brief["content_hash"],
        },
    }
    try:
        with repository.read_connection() as connection:
            before = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "execution_job",
                    "execution_attempt",
                    "execution_job_start_receipt",
                    "p1_configuration_operation",
                )
            }
        with pytest.raises(PlanningUnavailableError):
            planning.start_planning(command)
        with pytest.raises(StaleConfigurationCasError):
            planning.start_planning(
                {
                    **command,
                    "operation_key": "planning-stale",
                    "expected_project_brief": {
                        **command["expected_project_brief"],
                        "content_hash": "0" * 64,
                    },
                }
            )
        with repository.read_connection() as connection:
            after = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in before
            }
        assert after == before
    finally:
        repository.close()


def test_plan_selection_replay_survives_repository_restart(tmp_path) -> None:
    database = tmp_path / "core.db"
    repository = CoreAuthorityRepository(database)
    _, planning, profile = _configuration(repository)
    generation = _generation()
    _install_generation(repository, generation)
    planning.register_verified_plan_reference(
        generation_id=generation["generation_id"],
        plan_revision_id="plan-revision-1",
        plan_revision_hash=PLAN_HASH,
    )
    command = _selection_command(profile)
    selected = planning.select_workspace_plan(command)
    repository.close()

    reopened = CoreAuthorityRepository(database)
    replay = WorkspacePlanAuthority(reopened, clock=lambda: NOW)
    try:
        assert replay.select_workspace_plan(command) == {
            **selected,
            "idempotent": True,
        }
        assert reopened.get_workspace("workspace-1").revision == 1
        with reopened.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_workspace_plan_selection"
            ).fetchone()[0] == 1
    finally:
        reopened.close()
