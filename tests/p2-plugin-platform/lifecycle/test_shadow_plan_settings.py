from __future__ import annotations

import copy
import sqlite3

import pytest
from plotpilot_core.plugins.install import InstallStager
from plotpilot_core.plugins.lifecycle import (
    InstallLifecycleService,
    LifecycleError,
    LifecycleRepository,
    RetirementManager,
    ShadowGenerationManager,
    initial_transition,
    prepare_plan_switch,
)
from plotpilot_core.plugins.settings import migrate_settings_payload
from plotpilot_core.plugins.store import PackageStore
from plotpilot_plugin_sdk.canonical import hash_jcs
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode


def _repository(connection: sqlite3.Connection) -> LifecycleRepository:
    LifecycleRepository.initialize_standalone_schema_for_tests(connection)
    return LifecycleRepository(connection)


def _attempt_at_env(repository: LifecycleRepository) -> RetirementManager:
    repository.create_or_recover_attempt(
        initial_transition(
            "install-shadow",
            base_generation_id=None,
            base_lkg_generation_id=None,
            target_generation_id="generation-shadow",
            created_at="2026-08-28T03:00:00Z",
        ),
        request={"operation": "install-shadow", "release_id": "a" * 64},
    )
    for expected, target, updates in (
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
    ):
        repository.advance_attempt(
            "install-shadow",
            expected_state=expected,
            target_state=target,
            updates=updates,
            at="2026-08-28T03:00:00Z",
        )
    retirement = RetirementManager(repository)
    retirement.register_installed("a" * 64)
    retirement.acquire_pin(
        "a" * 64,
        pin_kind="install",
        owner_id="install-shadow",
        pin_id="pin-install-shadow",
    )
    return retirement


def test_shadow_migration_uses_fresh_epoch_and_only_qualified_can_commit() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    retirement = _attempt_at_env(repository)
    shadow = ShadowGenerationManager(repository, retirement.require_install_pin)
    first = shadow.prepare(
        install_operation_id="install-shadow",
        shadow_data_generation_id="data-shadow-1",
        release_id="a" * 64,
        owner_instance_id="worker-old",
        ttl_seconds=1,
        now="2026-08-28T03:00:00Z",
    )
    with pytest.raises(LifecycleError) as early:
        shadow.require_qualified("data-shadow-1")
    assert early.value.code == ErrorCode.MIGRATION_FAILED
    fresh = shadow.acquire(
        "data-shadow-1",
        owner_instance_id="worker-new",
        ttl_seconds=30,
        now="2026-08-28T03:00:02Z",
    )
    assert fresh.db_lease_epoch == first.db_lease_epoch + 1
    for stale_mutation in (shadow.fail, shadow.release):
        with pytest.raises(LifecycleError) as stale_write:
            stale_mutation(
                "data-shadow-1",
                db_lease_id=first.db_lease_id,
                db_lease_epoch=first.db_lease_epoch,
                owner_instance_id="worker-old",
                now="2026-08-28T03:00:02Z",
            )
        assert stale_write.value.code == ErrorCode.STALE_LEASE
    with pytest.raises(LifecycleError) as stale:
        shadow.advance(
            "data-shadow-1",
            expected_state="prepared",
            target_state="applying",
            db_lease_id=first.db_lease_id,
            db_lease_epoch=first.db_lease_epoch,
            owner_instance_id="worker-old",
            now="2026-08-28T03:00:02Z",
        )
    assert stale.value.code == ErrorCode.STALE_LEASE
    current = fresh
    for expected, target in (
        ("prepared", "applying"),
        ("applying", "applied"),
        ("applied", "verified"),
        ("verified", "qualified"),
    ):
        current = shadow.advance(
            "data-shadow-1",
            expected_state=expected,
            target_state=target,
            db_lease_id=current.db_lease_id,
            db_lease_epoch=current.db_lease_epoch,
            owner_instance_id=current.owner_instance_id,
            now="2026-08-28T03:00:03Z",
        )
    assert shadow.require_qualified("data-shadow-1").state == "qualified"
    attempt = repository.get_attempt("install-shadow")
    assert attempt["state"] == "migrated"
    assert attempt["shadow_data_generation_id"] == "data-shadow-1"


def _member(plugin_id: str, release_id: str) -> dict:
    return {
        "plugin_id": plugin_id,
        "release_id": release_id,
        "package_hash": release_id,
        "data_generation_id": None,
        "ui_bundle_hash": None,
        "global_settings_revision_id": None,
        "settings_schema_hash": None,
        "data_bundle_asset_id": None,
    }


def _no_data_bundle(_asset_id: str) -> None:
    return None


def _data_bundle(*, plugin_id: str, release_id: str, package_hash: str) -> dict:
    value = {
        "schema": "plugin-data-bundle/v1",
        "bundle_id": "data-bundle-1",
        "data_plugin_id": plugin_id,
        "data_release_id": release_id,
        "package_hash": package_hash,
        "format_id": "plotpilot.rules/v1",
        "root_path": "data/rules.json",
        "files": [
            {
                "path": "data/rules.json",
                "asset_id": "asset-data-rules-1",
                "sha256": "d" * 64,
                "mime": "application/json",
                "size": 2,
            }
        ],
    }
    value["bundle_hash"] = hash_jcs("plugin-data-bundle/v1", value)
    return value


def test_plan_switch_is_explicit_preserves_multiple_plugins_and_user_order() -> None:
    release_a, release_b = "a" * 64, "b" * 64
    generation = {
        "schema": "plugin-generation/v1",
        "generation_id": "generation-plan",
        "core_api_version": "1.2.0",
        "members": [
            _member("com.example.a", release_a),
            _member("com.example.b", release_b),
        ],
        "created_reason": "plan test",
        "created_at": "2026-08-28T03:00:00Z",
        "health_result_asset_id": "asset-health-plan",
        "parent_generation_id": None,
        "base_generation_id": None,
    }
    plan = {
        "schema": "plugin-plan/v1",
        "plan_id": "plan-user",
        "revision": 4,
        "name": "User order",
        "description": "No recommendation",
        "bindings": [
            {
                "binding_id": "binding-a",
                "capability_id": "writing.draft/v1",
                "plugin_id": "com.example.a",
                "release_requirement": "1.0.0",
                "order": 10,
                "enabled": True,
                "required": True,
                "propagate_cancel": True,
                "parameters_asset_id": None,
            },
            {
                "binding_id": "binding-b",
                "capability_id": "writing.draft/v1",
                "plugin_id": "com.example.b",
                "release_requirement": "2.0.0",
                "order": 20,
                "enabled": True,
                "required": True,
                "propagate_cancel": True,
                "parameters_asset_id": None,
            },
        ],
        "data_bindings": [],
        "skill_preset_revision_id": None,
        "result_mode": "separate",
        "synthesizer": None,
        "model_profile_revision_id": None,
        "ui_defaults": [],
    }
    catalog = {
        "com.example.a": {
            "release_id": release_a,
            "version": "1.0.0",
            "capabilities": ["writing.draft/v1"],
        },
        "com.example.b": {
            "release_id": release_b,
            "version": "2.0.0",
            "capabilities": ["writing.draft/v1"],
        },
    }
    with pytest.raises(ContractError) as implicit:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-1",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=generation,
            release_catalog=catalog,
            data_bundle_resolver=_no_data_bundle,
            user_confirmed=False,
        )
    assert implicit.value.code == ErrorCode.INVALID_TRANSITION
    intent = prepare_plan_switch(
        workspace_id="workspace-1",
        operation_id="switch-1",
        expected_workspace_revision=7,
        expected_current_plan_revision_id="plan-revision-old",
        target_plan_revision_id="plan-revision-target",
        plan=plan,
        generation=generation,
        release_catalog=catalog,
        data_bundle_resolver=_no_data_bundle,
        user_confirmed=True,
    )
    assert [item["binding"]["plugin_id"] for item in intent.resolution] == [
        "com.example.a",
        "com.example.b",
    ]
    assert (
        intent.intent_hash
        == prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-1",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=generation,
            release_catalog=catalog,
            data_bundle_resolver=_no_data_bundle,
            user_confirmed=True,
        ).intent_hash
    )
    intent.verify_integrity()
    with pytest.raises(TypeError):
        intent.plan["bindings"][0]["plugin_id"] = "com.example.mutated"
    with pytest.raises(AttributeError):
        intent.plan["bindings"].append({})
    plan["bindings"][0]["plugin_id"] = "com.example.changed-after-prepare"
    assert intent.plan["bindings"][0]["plugin_id"] == "com.example.a"

    conflict = copy.deepcopy(plan)
    conflict["bindings"][0]["release_requirement"] = "9.0.0"
    with pytest.raises(ContractError) as mismatch:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-2",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=conflict,
            generation=generation,
            release_catalog=catalog,
            data_bundle_resolver=_no_data_bundle,
            user_confirmed=True,
        )
    assert mismatch.value.code == ErrorCode.INCOMPATIBLE_GENERATION


def test_shadow_prepare_rejects_wrong_state_or_frozen_release() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    repository.create_or_recover_attempt(
        initial_transition(
            "install-invalid-shadow",
            base_generation_id=None,
            base_lkg_generation_id=None,
            target_generation_id="generation-shadow",
            created_at="2026-08-28T03:00:00Z",
        ),
        request={"release_id": "a" * 64},
    )
    retirement = RetirementManager(repository)
    retirement.register_installed("a" * 64)
    retirement.acquire_pin(
        "a" * 64,
        pin_kind="install",
        owner_id="install-invalid-shadow",
        pin_id="pin-install-invalid-shadow",
    )
    shadow = ShadowGenerationManager(repository, retirement.require_install_pin)
    with pytest.raises(LifecycleError):
        shadow.prepare(
            install_operation_id="install-invalid-shadow",
            shadow_data_generation_id="data-shadow-invalid",
            release_id="a" * 64,
            owner_instance_id="worker",
        )
    count = repository.connection.execute(
        "SELECT count(*) FROM p2_plugin_shadow_generation"
    ).fetchone()[0]
    assert count == 0

    for expected, target, updates in (
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
    ):
        repository.advance_attempt(
            "install-invalid-shadow",
            expected_state=expected,
            target_state=target,
            updates=updates,
        )
    with pytest.raises(LifecycleError) as mismatch:
        shadow.prepare(
            install_operation_id="install-invalid-shadow",
            shadow_data_generation_id="data-shadow-invalid",
            release_id="b" * 64,
            owner_instance_id="worker",
        )
    assert mismatch.value.code == ErrorCode.DUPLICATE_REQUEST
    assert (
        repository.connection.execute(
            "SELECT count(*) FROM p2_plugin_shadow_generation"
        ).fetchone()[0]
        == 0
    )

    retirement.release_pin("pin-install-invalid-shadow")
    with pytest.raises(LifecycleError) as unpinned:
        shadow.prepare(
            install_operation_id="install-invalid-shadow",
            shadow_data_generation_id="data-shadow-unpinned",
            release_id="a" * 64,
            owner_instance_id="worker",
        )
    assert unpinned.value.code == ErrorCode.RELEASE_RETIRING
    assert (
        repository.connection.execute(
            "SELECT count(*) FROM p2_plugin_shadow_generation"
        ).fetchone()[0]
        == 0
    )


def test_data_binding_requires_exact_generation_bundle() -> None:
    interpreter_release, data_release = "a" * 64, "c" * 64
    interpreter = _member("com.example.interpreter", interpreter_release)
    data_member = _member("com.example.data", data_release)
    data_member["data_generation_id"] = "data-generation-1"
    data_member["data_bundle_asset_id"] = "asset-data-bundle-1"
    generation = {
        "schema": "plugin-generation/v1",
        "generation_id": "generation-data-plan",
        "core_api_version": "1.2.0",
        "members": [data_member, interpreter],
        "created_reason": "data plan test",
        "created_at": "2026-08-28T03:00:00Z",
        "health_result_asset_id": "asset-health-data-plan",
        "parent_generation_id": None,
        "base_generation_id": None,
    }
    plan = {
        "schema": "plugin-plan/v1",
        "plan_id": "plan-data",
        "revision": 1,
        "name": "Data",
        "description": "Exact bundle",
        "bindings": [
            {
                "binding_id": "binding-interpreter",
                "capability_id": "data.interpret/v1",
                "plugin_id": "com.example.interpreter",
                "release_requirement": "1.0.0",
                "order": 1,
                "enabled": True,
                "required": True,
                "propagate_cancel": True,
                "parameters_asset_id": None,
            }
        ],
        "data_bindings": [
            {
                "data_binding_id": "data-binding-1",
                "data_plugin_id": "com.example.data",
                "release_requirement": "2.0.0",
                "format_id": "plotpilot.rules/v1",
                "interpreter_binding_id": "binding-interpreter",
                "order": 1,
                "enabled": True,
                "parameters_asset_id": None,
            }
        ],
        "skill_preset_revision_id": None,
        "result_mode": "separate",
        "synthesizer": None,
        "model_profile_revision_id": None,
        "ui_defaults": [],
    }
    catalog = {
        "com.example.interpreter": {
            "release_id": interpreter_release,
            "version": "1.0.0",
            "capabilities": ["data.interpret/v1"],
            "accepted_data_formats": ["plotpilot.rules/v1"],
        },
        "com.example.data": {
            "release_id": data_release,
            "version": "2.0.0",
            "capabilities": [],
            "data_bundle_asset_id": "asset-data-bundle-1",
        },
    }
    bundle = _data_bundle(
        plugin_id="com.example.data",
        release_id=data_release,
        package_hash=data_member["package_hash"],
    )
    bundles = {"asset-data-bundle-1": bundle}
    intent = prepare_plan_switch(
        workspace_id="workspace-1",
        operation_id="switch-data",
        expected_workspace_revision=7,
        expected_current_plan_revision_id="plan-revision-old",
        target_plan_revision_id="plan-revision-target",
        plan=plan,
        generation=generation,
        release_catalog=catalog,
        data_bundle_resolver=bundles.get,
        user_confirmed=True,
    )
    assert intent.resolution[-1]["data_bundle_asset_id"] == "asset-data-bundle-1"
    assert intent.resolution[-1]["bundle_hash"] == bundle["bundle_hash"]

    missing = copy.deepcopy(generation)
    missing["members"][0]["data_bundle_asset_id"] = None
    with pytest.raises(ContractError) as absent:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-data-missing",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=missing,
            release_catalog=catalog,
            data_bundle_resolver=bundles.get,
            user_confirmed=True,
        )
    assert absent.value.code == ErrorCode.DATA_INTERPRETER_UNAVAILABLE

    conflicting = copy.deepcopy(catalog)
    conflicting["com.example.data"]["data_bundle_asset_id"] = "asset-other"
    with pytest.raises(ContractError) as mismatch:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-data-conflict",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=generation,
            release_catalog=conflicting,
            data_bundle_resolver=bundles.get,
            user_confirmed=True,
        )
    assert mismatch.value.code == ErrorCode.INCOMPATIBLE_GENERATION

    with pytest.raises(ContractError) as absent_asset:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-data-asset-absent",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=generation,
            release_catalog=catalog,
            data_bundle_resolver={}.get,
            user_confirmed=True,
        )
    assert absent_asset.value.code == ErrorCode.ASSET_ERROR

    tampered = copy.deepcopy(bundle)
    tampered["format_id"] = "wrong/v1"
    with pytest.raises(ContractError) as invalid_hash:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-data-asset-tampered",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=generation,
            release_catalog=catalog,
            data_bundle_resolver=lambda _asset_id: tampered,
            user_confirmed=True,
        )
    assert invalid_hash.value.code == ErrorCode.RESULT_CONTRACT_MISMATCH

    wrong_identity = copy.deepcopy(bundle)
    wrong_identity["data_plugin_id"] = "com.example.other"
    wrong_identity.pop("bundle_hash")
    wrong_identity["bundle_hash"] = hash_jcs("plugin-data-bundle/v1", wrong_identity)
    with pytest.raises(ContractError) as identity_mismatch:
        prepare_plan_switch(
            workspace_id="workspace-1",
            operation_id="switch-data-asset-identity",
            expected_workspace_revision=7,
            expected_current_plan_revision_id="plan-revision-old",
            target_plan_revision_id="plan-revision-target",
            plan=plan,
            generation=generation,
            release_catalog=catalog,
            data_bundle_resolver=lambda _asset_id: wrong_identity,
            user_confirmed=True,
        )
    assert identity_mismatch.value.code == ErrorCode.INCOMPATIBLE_GENERATION


def test_closed_settings_migration_is_nonreplacing_and_deterministic() -> None:
    manifest = {
        "schema": "settings-migration-manifest/v1",
        "from_schema_hash": "a" * 64,
        "to_schema_hash": "b" * 64,
        "steps": [
            {
                "operation": "rename",
                "from_pointer": "/old",
                "to_pointer": "/new",
                "value_asset_id": None,
            },
            {
                "operation": "set_default",
                "from_pointer": None,
                "to_pointer": "/mode",
                "value_asset_id": "asset-default",
            },
        ],
    }
    source = {"old": {"enabled": True}}
    migrated = migrate_settings_payload(
        source,
        manifest,
        current_schema_hash="a" * 64,
        target_schema_hash="b" * 64,
        read_value_asset=lambda asset_id: (
            b'"strict"' if asset_id == "asset-default" else b"null"
        ),
    )
    assert source == {"old": {"enabled": True}}
    assert migrated == {"new": {"enabled": True}, "mode": "strict"}
    with pytest.raises(ContractError) as mismatch:
        migrate_settings_payload(
            source,
            manifest,
            current_schema_hash="c" * 64,
            target_schema_hash="b" * 64,
            read_value_asset=lambda _: None,
        )
    assert mismatch.value.code == ErrorCode.MIGRATION_FAILED

    occupied = copy.deepcopy(source)
    occupied["new"] = "keep"
    with pytest.raises(ContractError):
        migrate_settings_payload(
            occupied,
            manifest,
            current_schema_hash="a" * 64,
            target_schema_hash="b" * 64,
            read_value_asset=lambda _: "strict",
        )


def test_f002_qualified_shadow_is_terminal_and_releases_lease() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    retirement = _attempt_at_env(repository)
    shadow = ShadowGenerationManager(repository, retirement.require_install_pin)
    lease = shadow.prepare(
        install_operation_id="install-shadow",
        shadow_data_generation_id="data-shadow-terminal",
        release_id="a" * 64,
        owner_instance_id="worker-terminal",
        now="2026-08-28T03:00:00Z",
    )
    current = lease
    for expected, target in (
        ("prepared", "applying"),
        ("applying", "applied"),
        ("applied", "verified"),
        ("verified", "qualified"),
    ):
        current = shadow.advance(
            current.shadow_data_generation_id,
            expected_state=expected,
            target_state=target,
            db_lease_id=current.db_lease_id,
            db_lease_epoch=current.db_lease_epoch,
            owner_instance_id=current.owner_instance_id,
            now="2026-08-28T03:00:01Z",
        )
    assert current.state == "qualified" and current.lease_state == "released"
    with pytest.raises(LifecycleError):
        shadow.acquire(
            current.shadow_data_generation_id,
            owner_instance_id="worker-new",
            now="2026-08-28T03:00:02Z",
        )
    with pytest.raises(LifecycleError):
        shadow.fail(
            current.shadow_data_generation_id,
            db_lease_id=current.db_lease_id,
            db_lease_epoch=current.db_lease_epoch,
            owner_instance_id=current.owner_instance_id,
            now="2026-08-28T03:00:02Z",
        )
    assert shadow.require_qualified(current.shadow_data_generation_id) == current


def test_f006_settings_validation_requires_p1_authority(tmp_path) -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    _attempt_at_env(repository)
    repository.advance_attempt(
        "install-shadow",
        expected_state="env_prepared",
        target_state="shadow_prepared",
    )
    repository.advance_attempt(
        "install-shadow",
        expected_state="shadow_prepared",
        target_state="migrated",
    )
    generation = {
        "schema": "plugin-generation/v1",
        "generation_id": "generation-shadow",
        "core_api_version": "1.2.0",
        "members": [
            {
                "plugin_id": "com.plotpilot.settings.a",
                "release_id": "a" * 64,
                "package_hash": "b" * 64,
                "data_generation_id": None,
                "ui_bundle_hash": None,
                "global_settings_revision_id": "settings-a",
                "settings_schema_hash": "c" * 64,
                "data_bundle_asset_id": None,
            }
        ],
        "created_reason": "settings authority test",
        "created_at": "2026-08-28T03:00:00Z",
        "health_result_asset_id": "asset-health-settings",
        "parent_generation_id": None,
        "base_generation_id": None,
    }
    service = InstallLifecycleService(
        repository,
        InstallStager(PackageStore(tmp_path / "packages")),
        retirement=RetirementManager(repository),
    )
    with pytest.raises(LifecycleError) as error:
        service.mark_settings_validated("install-shadow", generation=generation)
    assert error.value.code == ErrorCode.SETTINGS_INVALID
    assert repository.get_attempt("install-shadow")["state"] == "migrated"
    observed: list[bool] = []

    def cross_plugin_reader(connection, _expected_plugin_id, revision_id):
        observed.append(connection.in_transaction)
        return (
            {
                "settings_revision_id": revision_id,
                "plugin_id": "com.plotpilot.settings.b",
            },
            {},
        )

    cross_plugin = InstallLifecycleService(
        repository,
        InstallStager(PackageStore(tmp_path / "packages-cross")),
        retirement=RetirementManager(repository),
        settings_authority_reader=cross_plugin_reader,
    )
    with pytest.raises(LifecycleError):
        cross_plugin.mark_settings_validated(
            "install-shadow", generation=generation
        )
    assert observed == [True]
    assert repository.get_attempt("install-shadow")["state"] == "migrated"


@pytest.mark.parametrize("token", ["-1", "+1", "01", "-", "１"])
def test_f014_array_indices_are_canonical_and_append_is_not_authorized(token) -> None:
    manifest = {
        "schema": "settings-migration-manifest/v1",
        "from_schema_hash": "a" * 64,
        "to_schema_hash": "b" * 64,
        "steps": [
            {
                "operation": "remove",
                "from_pointer": f"/items/{token}",
                "to_pointer": None,
                "value_asset_id": None,
            }
        ],
    }
    source = {"items": ["zero", "one"]}
    with pytest.raises(ContractError) as error:
        migrate_settings_payload(
            source,
            manifest,
            current_schema_hash="a" * 64,
            target_schema_hash="b" * 64,
            read_value_asset=lambda _: None,
        )
    assert error.value.code == ErrorCode.MIGRATION_FAILED
    assert source == {"items": ["zero", "one"]}
