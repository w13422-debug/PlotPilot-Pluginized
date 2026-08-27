from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from backend.plotpilot_core.api.v1.models import (
    ModelProfileCreateRequest,
    ModelProfileListResponse,
    ModelProfileResponse,
)
from backend.plotpilot_core.model import (
    InMemoryModelProfileRepository,
    ModelProfileConflictError,
    ModelProfileNotFoundError,
    ModelProfileRevision,
    ModelProfileValidationError,
    ProviderConfig,
    SQLiteModelProfileRepository,
)


CREATED_AT = "2026-08-27T12:00:00Z"


def _provider(*, model: str = "gpt-test", release: str = "1.0.0", parameters=None) -> ProviderConfig:
    return ProviderConfig(
        plugin_id="com.example.provider",
        release_id=release,
        endpoint="https://provider.example.test/v1",
        model_name=model,
        api_key_ref="secret://provider/default",
        temperature=0.2,
        parameters=parameters or {"top_p": 0.9, "nested": {"enabled": True}},
        timeout_seconds=30,
        max_retries=1,
        context_limit=32_000,
        output_limit=2_000,
        notes="offline test profile",
        tags=("test", "provider"),
    )


def _revision(
    *,
    revision_id: str = "profile-revision-1",
    revision_number: int = 1,
    parent_revision_id: str | None = None,
    provider: ProviderConfig | None = None,
    created_at: str = CREATED_AT,
) -> ModelProfileRevision:
    return ModelProfileRevision(
        profile_id="model-profile-1",
        revision_id=revision_id,
        revision_number=revision_number,
        parent_revision_id=parent_revision_id,
        provider=provider or _provider(),
        created_at=created_at,
    )


def test_revision_is_immutable_and_content_hash_round_trips_exactly():
    revision = _revision()

    assert revision.to_dict()["revision_hash"] == revision.revision_hash
    assert ModelProfileRevision.from_dict(revision.to_nested_dict()) == revision
    assert revision.provider.parameters["nested"]["enabled"] is True
    with pytest.raises(FrozenInstanceError):
        revision.revision_number = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        revision.provider.parameters["new"] = "must not mutate"  # type: ignore[index]

    with pytest.raises(ModelProfileValidationError, match="revision_hash"):
        ModelProfileRevision.from_dict({**revision.to_nested_dict(), "revision_hash": "0" * 64})


def test_provider_config_is_closed_and_never_accepts_secret_values():
    with pytest.raises(ModelProfileValidationError, match="secret value"):
        _provider(parameters={"api_key": "sk-live-secret"})
    with pytest.raises(ModelProfileValidationError, match="reference"):
        _provider().__class__(
            plugin_id="com.example.provider",
            release_id="1.0.0",
            endpoint="https://provider.example.test/v1",
            model_name="gpt-test",
            api_key_ref="sk-live-secret",
        )
    with pytest.raises(ModelProfileValidationError, match="reference"):
        _provider().__class__(
            plugin_id="com.example.provider",
            release_id="1.0.0",
            endpoint="https://provider.example.test/v1",
            model_name="gpt-test",
            api_key_ref="AIzaSyExampleRawKey",
        )
    with pytest.raises(ModelProfileValidationError, match="secret value"):
        ProviderConfig.from_dict(
            {
                "plugin_id": "com.example.provider",
                "release_id": "1.0.0",
                "endpoint": "https://provider.example.test/v1",
                "model_name": "gpt-test",
                "api_key_ref": "secret://provider/default",
                "parameters": {"headers": {"authorization": "Bearer raw-secret"}},
            }
        )
    with pytest.raises(ModelProfileValidationError, match="userinfo"):
        ProviderConfig(
            plugin_id="com.example.provider",
            release_id="1.0.0",
            endpoint="https://user:password@provider.example.test/v1",
            model_name="gpt-test",
            api_key_ref="secret://provider/default",
        )


def test_in_memory_repository_is_append_only_and_keeps_revision_chain():
    repository = InMemoryModelProfileRepository()
    first = _revision()
    assert repository.append(first) is first
    assert repository.append(first) is first

    second = _revision(
        revision_id="profile-revision-2",
        revision_number=2,
        parent_revision_id=first.revision_id,
        provider=_provider(model="gpt-test-2"),
    )
    assert repository.append(second) is second
    assert repository.get_current(first.profile_id) == second
    assert repository.list(first.profile_id) == (first, second)

    with pytest.raises(ModelProfileConflictError):
        repository.append(
            _revision(
                revision_id="profile-revision-3",
                revision_number=1,
                provider=_provider(model="different-root"),
            )
        )
    with pytest.raises(ModelProfileValidationError, match="provider identity"):
        repository.append(
            _revision(
                revision_id="profile-revision-release-change",
                revision_number=3,
                parent_revision_id=second.revision_id,
                provider=_provider(release="2.0.0"),
            )
        )
    with pytest.raises(ModelProfileNotFoundError):
        repository.get("missing-revision")


def test_sqlite_repository_round_trips_and_database_guards_are_immutable():
    connection = sqlite3.connect(":memory:")
    repository = SQLiteModelProfileRepository(connection=connection)
    first = _revision()
    repository.append(first)

    assert repository.get(first.revision_id) == first
    assert repository.get_current(first.profile_id) == first
    assert repository.list() == (first,)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute(
            "UPDATE model_profile_revision SET provider_plugin_id = provider_plugin_id WHERE revision_id = ?",
            (first.revision_id,),
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM model_profile_revision WHERE revision_id = ?", (first.revision_id,))
    assert repository.get(first.revision_id) == first
    repository.close()


def test_created_at_is_hash_bound_and_duplicate_revision_id_rejects_timestamp_drift():
    first = _revision()
    drifted = _revision(created_at="2026-08-27T12:00:01Z")
    assert first.revision_hash != drifted.revision_hash
    assert first.canonical_bytes != drifted.canonical_bytes

    memory = InMemoryModelProfileRepository()
    memory.append(first)
    with pytest.raises(ModelProfileConflictError):
        memory.append(drifted)

    sqlite = SQLiteModelProfileRepository(connection=sqlite3.connect(":memory:"))
    sqlite.append(first)
    with pytest.raises(ModelProfileConflictError):
        sqlite.append(drifted)
    sqlite.close()


def test_model_profile_api_dto_accepts_nested_provider_and_projects_only_reference():
    request = ModelProfileCreateRequest.from_dict(
        {
            "schema": "model-profile-create/v1",
            "profile_id": "model-profile-api",
            "provider": {
                "plugin_id": "com.example.provider",
                "release_id": "1.0.0",
                "endpoint": "https://provider.example.test/v1",
                "model_name": "gpt-test",
                "api_key_ref": "secret://provider/default",
                "parameters": {"temperature": 0.1},
            },
            "notes": "api boundary",
        }
    )
    revision = request.to_revision(revision_id="api-revision-1")
    response = ModelProfileResponse.from_domain(revision)
    listing = ModelProfileListResponse(items=(response,), total=1)

    assert revision.provider_plugin_id == "com.example.provider"
    assert response.to_dict()["api_key_ref"] == "secret://provider/default"
    assert "api_key" not in response.to_dict()
    assert listing.to_dict()["total"] == 1
    with pytest.raises(ValueError, match="unknown field"):
        ModelProfileCreateRequest.from_dict(
            {
                "profile_id": "model-profile-api",
                "provider_plugin_id": "com.example.provider",
                "provider_release_id": "1.0.0",
                "endpoint": "https://provider.example.test/v1",
                "model_name": "gpt-test",
                "api_key_ref": "secret://provider/default",
                "api_key": "sk-live-secret",
            }
        )
