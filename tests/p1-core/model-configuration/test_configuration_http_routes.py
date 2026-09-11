from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.macro_planning_v2 import (
    FIXED_HTTP_ERROR_MESSAGES as SDK_FIXED_HTTP_ERROR_MESSAGES,
)
from backend.plotpilot_core.api.v2.configuration import (
    ROUTE_ALLOWLIST,
    build_configuration_router,
    route_inventory,
)
from backend.plotpilot_core.bootstrap.model_configuration_adapters import (
    build_model_configuration_adapters,
)
from backend.plotpilot_core.configuration.plan_authority import (
    PROJECT_BRIEF_DOCUMENT_TYPE,
    PROJECT_BRIEF_PAYLOAD_SCHEMA,
)
from backend.plotpilot_core.configuration.authority import FIXED_ERROR_MESSAGES
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.plugins.generation import validate_generation
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository

NOW = "2030-01-02T03:04:05Z"
RAW = "HTTP_ONLY_RAW_SECRET_8e07752c27ebfb41"
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


def _activate_generation(repository: CoreAuthorityRepository) -> str:
    generation = validate_generation(
        {
            "schema": "plugin-generation/v1",
            "generation_id": "generation-planning-1",
            "core_api_version": "1.2.0",
            "members": sorted(
                (
                    _member("com.plotpilot.project-planner", "b"),
                    _member("com.plotpilot.prompt-skill-runtime", "c"),
                    _member("com.plotpilot.provider.local", "a"),
                ),
                key=lambda item: item["plugin_id"].encode(),
            ),
            "created_reason": "HTTP test",
            "created_at": NOW,
            "health_result_asset_id": "asset-health-http",
            "parent_generation_id": None,
            "base_generation_id": None,
        }
    )
    payload = canonical_bytes(generation)
    with repository.transaction() as connection:
        connection.execute(
            "INSERT INTO p2_plugin_generation VALUES(?,?,?)",
            (
                generation["generation_id"],
                payload.decode(),
                hashlib.sha256(payload).hexdigest(),
            ),
        )
        connection.execute(
            "UPDATE p2_plugin_generation_pointer SET current_generation_id=? "
            "WHERE singleton=1",
            (generation["generation_id"],),
        )
    return generation["generation_id"]


def _stack(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    adapters = build_model_configuration_adapters(repository)
    app = FastAPI()
    router = adapters.router()
    app.include_router(router)
    return repository, adapters, router, TestClient(app)


def _secret_command(
    operation_key: str = "secret-operation-1",
    value: str = RAW,
    secret_id: str = "provider-main",
) -> dict:
    return {
        "schema": "model-secret-put-command/v2",
        "operation_key": operation_key,
        "secret_id": secret_id,
        "value": value,
    }


def _profile_command(
    *,
    operation_key: str = "profile-operation-1",
    expected_parent_revision_id: str | None = None,
    api_key_ref: str = "secret://provider-main",
) -> dict:
    return {
        "schema": "model-profile-revise-command/v2",
        "operation_key": operation_key,
        "profile_id": "model-profile-planner",
        "expected_parent_revision_id": expected_parent_revision_id,
        "provider": {
            "plugin_id": "com.plotpilot.provider.local",
            "release_id": PROVIDER_RELEASE,
            "endpoint": "https://models.example.test/v1",
            "model_name": "planner-model-1",
            "options": {
                "temperature": 0.4,
                "top_p": 0.9,
                "max_output_tokens": 4096,
                "timeout_seconds": 120,
                "max_retries": 0,
            },
            "api_key_ref": api_key_ref,
        },
    }


def _brief(repository: CoreAuthorityRepository) -> dict[str, str]:
    repository.create_document(
        Document(
            "document-project-brief",
            "workspace-1",
            "Project Brief",
            PROJECT_BRIEF_DOCUMENT_TYPE,
        )
    )
    revision = repository.publish_revision(
        document_id="document-project-brief",
        content=json.dumps({"workspace_id": "workspace-1", "premise": "test"}),
        expected_revision_id=None,
        created_by="test",
        payload_schema=PROJECT_BRIEF_PAYLOAD_SCHEMA,
        revision_id="revision-project-brief-1",
    )
    return {"revision_id": revision.revision_id, "content_hash": revision.content_hash}


def test_router_is_exact_five_route_allowlist_without_plan_registration(tmp_path) -> None:
    repository, adapters, router, client = _stack(tmp_path)
    try:
        expected = tuple(
            sorted(ROUTE_ALLOWLIST, key=lambda item: (item["path"], item["method"]))
        )
        assert route_inventory(router) == expected
        assert len(router.routes) == 5
        assert {item["route_id"] for item in expected} == {
            "model-secret.put",
            "model-profile.revise",
            "workspace-plan.select",
            "project-planning.get",
            "project-planning.start",
        }
        assert not any("register" in item["path"] for item in expected)
        assert adapters.plan_registrar is adapters.planning
    finally:
        client.close()
        repository.close()


def test_core_and_sdk_share_the_adjudicated_fixed_error_messages() -> None:
    assert FIXED_ERROR_MESSAGES == SDK_FIXED_HTTP_ERROR_MESSAGES


def test_secret_http_create_replace_replay_conflict_and_no_raw_echo(tmp_path) -> None:
    repository, _, _, client = _stack(tmp_path)
    try:
        created = client.put(
            "/api/v2/core/secrets/provider-main", json=_secret_command()
        )
        assert created.status_code == 201
        assert created.json()["created"] is True
        assert created.json()["idempotent"] is False
        assert RAW not in created.text

        replay = client.put(
            "/api/v2/core/secrets/provider-main", json=_secret_command()
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent"] is True

        replacement = "HTTP_REPLACEMENT_RAW_3451a7c8e2506f0e"
        replaced = client.put(
            "/api/v2/core/secrets/provider-main",
            json=_secret_command("secret-operation-2", replacement),
        )
        assert replaced.status_code == 200
        assert replaced.json()["created"] is False
        assert replacement not in replaced.text

        conflict = client.put(
            "/api/v2/core/secrets/provider-main",
            json=_secret_command("secret-operation-2", RAW),
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "schema": "model-secret-http-error/v2",
            "secret_id": "provider-main",
            "error_code": "duplicate_operation",
            "message": "Operation key was reused with different input.",
            "retryable": False,
            "operation_key": "secret-operation-2",
        }
        assert RAW not in conflict.text and replacement not in conflict.text
    finally:
        client.close()
        repository.close()


def test_trusted_path_and_closed_body_identity_reject_before_mutation(tmp_path) -> None:
    repository, adapters, _, client = _stack(tmp_path)
    try:
        mismatch = client.put(
            "/api/v2/core/secrets/path-secret",
            json=_secret_command(secret_id="body-secret"),
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["secret_id"] == "path-secret"

        extra = client.put(
            "/api/v2/core/secrets/provider-main",
            json={**_secret_command(), "unexpected": True},
        )
        assert extra.status_code == 400
        missing = client.put(
            "/api/v2/core/secrets/provider-main", content=b""
        )
        assert missing.status_code == 400
        invalid_json = client.put(
            "/api/v2/core/secrets/provider-main",
            content=b'{"schema":',
            headers={"content-type": "application/json"},
        )
        assert invalid_json.status_code == 400

        no_trusted_path = adapters.http.handle(
            "model-secret.put", _secret_command(), path_params=None
        )
        assert no_trusted_path[0] == 400
        extra_trusted_path = adapters.http.handle(
            "model-secret.put",
            _secret_command(),
            path_params={"secret_id": "provider-main", "workspace_id": "extra"},
        )
        assert extra_trusted_path[0] == 400
        with repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_local_secret_value"
            ).fetchone()[0] == 0
    finally:
        client.close()
        repository.close()


@pytest.mark.parametrize(
    "value",
    (
        "Request",
        "model",
        "a",
        "provider-main",
        "secret-operation-1",
        "Request is malformed.",
        "Secret value was rejected.",
    ),
)
def test_secret_http_accepts_coincidental_fixed_response_overlap(
    tmp_path, value: str
) -> None:
    repository, _, _, client = _stack(tmp_path)
    try:
        response = client.put(
            "/api/v2/core/secrets/provider-main",
            json=_secret_command(value=value),
        )
        assert response.status_code == 201
        assert response.json()["api_key_ref"] == "secret://provider-main"
    finally:
        client.close()
        repository.close()


def test_profile_http_uses_fixed_404_409_and_p0a_revision_response(tmp_path) -> None:
    repository, _, _, client = _stack(tmp_path)
    try:
        missing = client.post(
            "/api/v2/core/model-profiles/model-profile-planner/revisions",
            json=_profile_command(),
        )
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "unknown_reference"

        assert client.put(
            "/api/v2/core/secrets/provider-main", json=_secret_command()
        ).status_code == 201
        created = client.post(
            "/api/v2/core/model-profiles/model-profile-planner/revisions",
            json=_profile_command(),
        )
        assert created.status_code == 201
        revision = created.json()["revision"]
        assert revision["schema"] == "model-profile-revision/v1"
        assert revision["provider"]["options"]["max_output_tokens"] == 4096
        assert "base_url" not in revision["provider"]
        replay = client.post(
            "/api/v2/core/model-profiles/model-profile-planner/revisions",
            json=_profile_command(),
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent"] is True

        stale = client.post(
            "/api/v2/core/model-profiles/model-profile-planner/revisions",
            json=_profile_command(operation_key="profile-stale"),
        )
        assert stale.status_code == 409
        assert stale.json()["error_code"] == "stale_cas"
        assert stale.json()["message"] == "Authority compare-and-swap is stale."
    finally:
        client.close()
        repository.close()


def test_plan_get_and_start_routes_keep_p1_start_effect_free(tmp_path) -> None:
    repository, adapters, _, client = _stack(tmp_path)
    repository.create_workspace(Workspace("workspace-1", "Novel"))
    brief = _brief(repository)
    generation_id = _activate_generation(repository)
    try:
        client.put(
            "/api/v2/core/secrets/provider-main", json=_secret_command()
        )
        profile_response = client.post(
            "/api/v2/core/model-profiles/model-profile-planner/revisions",
            json=_profile_command(),
        )
        profile = profile_response.json()["revision"]
        adapters.plan_registrar.register_verified_plan_reference(
            generation_id=generation_id,
            plan_revision_id="plan-revision-1",
            plan_revision_hash=PLAN_HASH,
        )
        selection_command = {
            "schema": "workspace-plan-selection-command/v2",
            "operation_key": "plan-operation-1",
            "workspace_id": "workspace-1",
            "expected_workspace_revision": 0,
            "expected_current_plan_revision_id": None,
            "expected_current_plan_revision_hash": None,
            "selection_mode": "explicit",
            "plan_revision_id": "plan-revision-1",
            "plan_revision_hash": PLAN_HASH,
            "model_profile_revision_id": profile["revision_id"],
            "model_profile_revision_hash": profile["revision_hash"],
            "expected_active_generation_id": generation_id,
        }
        selected = client.post(
            "/api/v2/core/workspaces/workspace-1/plans:select",
            json=selection_command,
        )
        assert selected.status_code == 200
        assert selected.json()["workspace_revision"] == 1
        replay = client.post(
            "/api/v2/core/workspaces/workspace-1/plans:select",
            json=selection_command,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent"] is True

        availability = client.get(
            "/api/v2/core/workspaces/workspace-1/project-planning"
        )
        assert availability.status_code == 200
        assert availability.json()["reason"] == "ready"

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
        start = client.post(
            "/api/v2/core/workspaces/workspace-1/project-planning",
            json={
                "schema": "project-planning-start-command/v2",
                "operation_key": "planning-operation-1",
                "workspace_id": "workspace-1",
                "requested_mode": "plugin",
                "project_brief_document_id": "document-project-brief",
                "expected_project_brief": brief,
            },
        )
        assert start.status_code == 400
        assert start.json() == {
            "schema": "workspace-planning-http-error/v2",
            "workspace_id": "workspace-1",
            "error_code": "planning_unavailable",
            "message": "Project planning is unavailable.",
            "retryable": False,
            "operation_key": "planning-operation-1",
        }
        with repository.read_connection() as connection:
            after = {
                table: connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                for table in before
            }
        assert after == before
    finally:
        client.close()
        repository.close()


def test_adapter_discards_arbitrary_exception_text_including_raw_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    repository, adapters, _, client = _stack(tmp_path)

    def fail(_command):
        raise RuntimeError(f"provider failed with {RAW}")

    monkeypatch.setattr(adapters.configuration, "put_secret", fail)
    try:
        status, body = adapters.http.handle(
            "model-secret.put",
            _secret_command(),
            path_params={"secret_id": "provider-main"},
        )
        assert status == 400
        assert body["error_code"] == "malformed_request"
        assert body["message"] == "Request is malformed."
        assert RAW not in json.dumps(body)
    finally:
        client.close()
        repository.close()
