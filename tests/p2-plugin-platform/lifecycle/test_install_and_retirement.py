from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest
from plotpilot_core.plugins.install import InstallStager, InstallState
from plotpilot_core.plugins.lifecycle import (
    InstallLifecycleService,
    LifecycleError,
    LifecycleRepository,
    PublicationBarrierDecision,
    RetirementManager,
    VerifiedQualification,
)
from plotpilot_core.plugins.package import verify_package
from plotpilot_core.plugins.store import PackageStore
from plotpilot_plugin_sdk.errors import ErrorCode
from plotpilot_plugin_sdk.package import build_files_sha256


def _write_event(*_args) -> None:
    pass


def _verify_qualification(_connection, evidence, generation) -> VerifiedQualification:
    assert evidence["generation_id"] == generation["generation_id"]
    assert evidence["health_result_asset_id"] == generation["health_result_asset_id"]
    assert evidence["passed"] is True
    return VerifiedQualification(
        qualification_id=evidence["qualification_id"],
        generation_id=generation["generation_id"],
        health_result_asset_id=generation["health_result_asset_id"],
        passed=True,
    )


def _generation(package, generation_id: str, base: str | None) -> dict:
    member = {
        "plugin_id": package.plugin_id,
        "release_id": package.release_id,
        "package_hash": package.package_hash,
        "data_generation_id": None,
        "ui_bundle_hash": None,
        "global_settings_revision_id": None,
        "settings_schema_hash": None,
        "data_bundle_asset_id": None,
    }
    return {
        "schema": "plugin-generation/v1",
        "generation_id": generation_id,
        "core_api_version": "1.2.0",
        "members": [member],
        "created_reason": "install test",
        "created_at": "2026-08-28T02:00:00Z",
        "health_result_asset_id": f"asset-health-{generation_id}",
        "parent_generation_id": base,
        "base_generation_id": base,
    }


def test_install_replay_recovers_publish_window_and_promotes_lkg(
    tmp_path: Path, package_files
) -> None:
    package = verify_package(package_files)
    store = PackageStore(tmp_path / "packages")
    stager = InstallStager(store)

    # Crash window: immutable package and staging snapshot exist while the
    # authoritative lifecycle row has not advanced past selected/staged.
    staged = stager.stage(
        package_files, install_operation_id="install-1", base_generation_id=None
    )
    store.publish(staged.package)
    assert stager.load("install-1").state == InstallState.STAGED

    connection = sqlite3.connect(tmp_path / "core.sqlite", isolation_level=None)
    repository = LifecycleRepository(connection)
    service = InstallLifecycleService(repository, stager)
    attempt = service.begin(
        package_files,
        install_operation_id="install-1",
        target_generation_id="generation-1",
    )
    assert attempt["state"] == "package_published"
    assert stager.load("install-1").state == InstallState.PACKAGE_PUBLISHED
    assert (
        service.begin(
            package_files,
            install_operation_id="install-1",
            target_generation_id="generation-1",
        )
        == attempt
    )

    generation = _generation(package, "generation-1", None)
    service.mark_environment_prepared("install-1")
    service.mark_no_shadow_required("install-1")
    service.mark_settings_validated("install-1", generation=generation, revisions={})
    service.qualify(
        "install-1",
        generation=generation,
        evidence={
            "qualification_id": "qualification-1",
            "generation_id": "generation-1",
            "health_result_asset_id": "asset-health-generation-1",
            "passed": True,
        },
        qualification_verifier=_verify_qualification,
    )
    assert repository.get_attempt("install-1")["qualification_id"] is None
    service.commit_current(
        "install-1", generation=generation, event_writer=_write_event
    )
    with pytest.raises(RuntimeError):
        service.promote_lkg(
            "install-1",
            evidence={
                "qualification_id": "qualification-lkg-failed",
                "generation_id": "generation-1",
                "health_result_asset_id": "asset-health-generation-1",
                "passed": True,
            },
            qualification_verifier=lambda *_: (_ for _ in ()).throw(
                RuntimeError("qualification authority unavailable")
            ),
        )
    assert repository.get_attempt("install-1")["state"] == "lkg_pending"
    assert service.retirement.active_pins(package.release_id)
    promoted = service.promote_lkg(
        "install-1",
        evidence={
            "qualification_id": "qualification-lkg-1",
            "generation_id": "generation-1",
            "health_result_asset_id": "asset-health-generation-1",
            "passed": True,
        },
        qualification_verifier=_verify_qualification,
    )
    assert promoted["state"] == "lkg_promoted"
    state = repository.generation_state()
    assert (
        state.current["generation_id"] == state.lkg["generation_id"] == "generation-1"
    )
    assert repository.exit_safe_mode(generation_id="generation-1").safe_mode is False
    assert service.retirement.active_pins(package.release_id) == ()


def test_same_install_operation_rejects_different_verified_input(
    tmp_path: Path, package_files
) -> None:
    store = PackageStore(tmp_path / "packages")
    service = InstallLifecycleService(
        LifecycleRepository(sqlite3.connect(":memory:", isolation_level=None)),
        InstallStager(store),
    )
    service.begin(
        package_files,
        install_operation_id="install-stable",
        target_generation_id="generation-1",
    )
    changed = copy.deepcopy(package_files)
    changed["data/rules.json"] = b'{"message":"different"}\n'
    ordinary = {key: value for key, value in changed.items() if key != "files.sha256"}
    changed["files.sha256"] = build_files_sha256(ordinary)
    with pytest.raises(LifecycleError) as error:
        service.begin(
            changed,
            install_operation_id="install-stable",
            target_generation_id="generation-1",
        )
    assert error.value.code == ErrorCode.DUPLICATE_REQUEST


def test_retire_pin_race_barrier_and_tombstone_delete(
    tmp_path: Path, package_files
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    repository = LifecycleRepository(connection)
    retirement = RetirementManager(repository)
    package = verify_package(package_files)
    store = PackageStore(tmp_path / "packages")
    store.publish(package)
    retirement.register_installed(package.release_id)
    pin = retirement.acquire_pin(
        package.release_id,
        pin_kind="attempt",
        owner_id="attempt-recoverable",
        pin_id="pin-recoverable",
        at="2026-08-28T02:00:00Z",
    )
    with pytest.raises(LifecycleError) as blocked:
        retirement.start_retirement(
            package.release_id,
            operation_id="retire-1",
            event_writer=_write_event,
            expected_epoch=pin["retire_epoch"],
        )
    assert blocked.value.code == ErrorCode.RELEASE_RETIRING

    retirement.release_pin("pin-recoverable", at="2026-08-28T02:00:01Z")
    with pytest.raises(RuntimeError):
        retirement.start_retirement(
            package.release_id,
            operation_id="retire-1",
            event_writer=lambda *_: (_ for _ in ()).throw(
                RuntimeError("event authority unavailable")
            ),
            expected_epoch=pin["retire_epoch"],
            at="2026-08-28T02:00:02Z",
        )
    assert retirement.get(package.release_id)["state"] == "installed"
    retiring = retirement.start_retirement(
        package.release_id,
        operation_id="retire-1",
        event_writer=_write_event,
        expected_epoch=pin["retire_epoch"],
        at="2026-08-28T02:00:02Z",
    )
    assert retiring["state"] == "retiring"
    with pytest.raises(LifecycleError) as fenced:
        retirement.acquire_pin(
            package.release_id, pin_kind="worker", owner_id="worker-late"
        )
    assert fenced.value.code == ErrorCode.RELEASE_RETIRING

    with pytest.raises(LifecycleError) as untrusted_boolean:
        retirement.complete_retirement(
            package.release_id,
            package_remover=lambda: (_ for _ in ()).throw(
                AssertionError("must not delete")
            ),
            publication_barrier=lambda _connection, _release_id: True,
            expected_epoch=retiring["retire_epoch"],
        )
    assert untrusted_boolean.value.code == ErrorCode.RESULT_CONTRACT_MISMATCH

    blocked_completion = retirement.complete_retirement(
        package.release_id,
        package_remover=lambda: (_ for _ in ()).throw(
            AssertionError("must not delete")
        ),
        publication_barrier=lambda _connection, release_id: PublicationBarrierDecision(
            release_id, False, ("candidate-1",)
        ),
        expected_epoch=retiring["retire_epoch"],
        at="2026-08-28T02:00:03Z",
    )
    assert blocked_completion["state"] == "retiring"
    assert retirement.attention(package.release_id) is not None

    def remove() -> bool:
        return store.remove_package_content(
            package.plugin_id,
            package.version,
            package.package_hash,
            expected_release_id=package.release_id,
        )

    retired = retirement.complete_retirement(
        package.release_id,
        package_remover=remove,
        publication_barrier=lambda _connection, release_id: PublicationBarrierDecision(
            release_id, True
        ),
        expected_epoch=retiring["retire_epoch"],
        at="2026-08-28T02:00:04Z",
    )
    assert retired["state"] == "retired" and retired["package_present"] is False
    assert remove() is True
    with pytest.raises(LifecycleError):
        retirement.require_post_delete_publication(
            package.release_id,
            publication_verifier=lambda _connection, release_id: (
                PublicationBarrierDecision(release_id, False, ("candidate-2",))
            ),
        )
    retirement.require_post_delete_publication(
        package.release_id,
        publication_verifier=lambda _connection, release_id: PublicationBarrierDecision(
            release_id, True
        ),
    )


def test_pending_generation_pins_cover_every_member_and_epoch() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    repository = LifecycleRepository(connection)
    retirement = RetirementManager(repository)
    release_a, release_b = "a" * 64, "b" * 64
    retirement.register_installed(release_a)
    retirement.register_installed(release_b)
    generation = {
        "members": [
            {"release_id": release_a},
            {"release_id": release_b},
        ]
    }
    pins = retirement.acquire_generation_pins(
        [generation],
        owner_id="install-generation",
    )
    assert {pin["release_id"] for pin in pins} == {release_a, release_b}
    retirement.require_generation_executable(
        connection,
        generation,
        "install-generation",
    )
    with pytest.raises(LifecycleError) as blocked:
        retirement.start_retirement(
            release_b,
            operation_id="retire-b",
            event_writer=_write_event,
        )
    assert blocked.value.code == ErrorCode.RELEASE_RETIRING

    retirement.release_owner_pins("install-generation")
    with pytest.raises(LifecycleError) as missing_pin:
        retirement.require_generation_executable(
            connection,
            generation,
            "install-generation",
        )
    assert missing_pin.value.code == ErrorCode.RELEASE_RETIRING
