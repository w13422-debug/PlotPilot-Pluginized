from __future__ import annotations

import copy
import json
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
    initial_transition,
)
from plotpilot_core.plugins.package import verify_package
from plotpilot_core.plugins.store import PackageStore
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode


def _repository(connection: sqlite3.Connection) -> LifecycleRepository:
    LifecycleRepository.initialize_standalone_schema_for_tests(connection)
    return LifecycleRepository(connection)


class _OperationAuthority:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], tuple[str, dict]] = {}

    def begin(self, _connection, *, method, release_id, operation_id, request_hash):
        saved = self.values.get((method, release_id, operation_id))
        if saved is None:
            return None
        if saved[0] != request_hash:
            raise LifecycleError(
                "operation replay payload differs", code=ErrorCode.DUPLICATE_REQUEST
            )
        return copy.deepcopy(saved[1])

    def finish(
        self,
        _connection,
        *,
        method,
        release_id,
        operation_id,
        request_hash,
        result,
    ):
        key = (method, release_id, operation_id)
        value = (request_hash, copy.deepcopy(dict(result)))
        saved = self.values.get(key)
        if saved is not None and saved != value:
            raise LifecycleError(
                "operation result replay differs", code=ErrorCode.DUPLICATE_REQUEST
            )
        self.values[key] = value
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
    repository = _repository(connection)
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
    service.mark_settings_validated("install-1", generation=generation)
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
        _repository(sqlite3.connect(":memory:", isolation_level=None)),
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
    repository = _repository(connection)
    retirement = RetirementManager(repository, _OperationAuthority())
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
            operation_id="complete-untrusted",
            package_remover=lambda: (_ for _ in ()).throw(
                AssertionError("must not delete")
            ),
            publication_barrier=lambda _connection, _release_id: True,
            expected_epoch=retiring["retire_epoch"],
        )
    assert untrusted_boolean.value.code == ErrorCode.RESULT_CONTRACT_MISMATCH

    blocked_completion = retirement.complete_retirement(
        package.release_id,
        operation_id="complete-blocked",
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
        operation_id="complete-delete",
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
    repository = _repository(connection)
    retirement = RetirementManager(repository, _OperationAuthority())
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


def test_f004_failed_staging_marker_never_becomes_published(
    tmp_path: Path, package_files
) -> None:
    store = PackageStore(tmp_path / "packages")
    stager = InstallStager(store)
    staged = stager.stage(
        package_files,
        install_operation_id="install-f004",
        base_generation_id=None,
    )
    changed = copy.deepcopy(package_files)
    changed["data/rules.json"] = b'{"message":"registry winner"}\n'
    ordinary = {key: value for key, value in changed.items() if key != "files.sha256"}
    changed["files.sha256"] = build_files_sha256(ordinary)
    store.publish(verify_package(changed))
    with pytest.raises(ContractError):
        stager.publish(staged)
    assert stager.load("install-f004").state == InstallState.FAILED
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    result = InstallLifecycleService(repository, stager).begin(
        package_files,
        install_operation_id="install-f004",
        target_generation_id="generation-f004",
    )
    assert result["state"] == "failed"
    with pytest.raises(KeyError):
        RetirementManager(repository).get(staged.package.release_id)


def test_f005_retired_release_cannot_be_rematerialized(
    tmp_path: Path, package_files
) -> None:
    store = PackageStore(tmp_path / "packages")
    package = verify_package(package_files)
    store.publish(package)
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    authority = _OperationAuthority()
    retirement = RetirementManager(repository, authority)
    retirement.register_installed(package.release_id)
    retiring = retirement.start_retirement(
        package.release_id,
        operation_id="start-f005",
        event_writer=_write_event,
        expected_epoch=1,
    )
    retirement.complete_retirement(
        package.release_id,
        operation_id="complete-f005",
        package_remover=lambda: store.remove_package_content(
            package.plugin_id,
            package.version,
            package.package_hash,
            expected_release_id=package.release_id,
        ),
        publication_barrier=lambda _c, release_id: PublicationBarrierDecision(
            release_id, True
        ),
        expected_epoch=retiring["retire_epoch"],
    )
    stager = InstallStager(store)
    with pytest.raises(LifecycleError):
        InstallLifecycleService(
            repository, stager, retirement=retirement
        ).begin(
            package_files,
            install_operation_id="install-f005-new",
            target_generation_id="generation-f005",
        )
    assert list(stager.staging_root.iterdir()) == []
    assert retirement.get(package.release_id)["state"] == "retired"


def test_f007_manifest_settings_namespace_is_bound_before_staging(
    tmp_path: Path, package_files
) -> None:
    files = copy.deepcopy(package_files)
    manifest = json.loads(files["plugin.json"])
    manifest["settings"] = {
        "namespace": "plugin.com.example.other",
        "schema": "settings/schema.json",
        "defaults": "settings/defaults.json",
        "migration_manifest": None,
    }
    files["plugin.json"] = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    files["settings/schema.json"] = b'{"type":"object"}\n'
    files["settings/defaults.json"] = b"{}\n"
    ordinary = {key: value for key, value in files.items() if key != "files.sha256"}
    files["files.sha256"] = build_files_sha256(ordinary)
    stager = InstallStager(PackageStore(tmp_path / "packages"))
    with pytest.raises(ContractError):
        stager.stage(files, install_operation_id="install-f007")
    assert list(stager.staging_root.iterdir()) == []


def test_f008_selected_attempt_blocks_retirement_without_a_pin() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    authority = _OperationAuthority()
    retirement = RetirementManager(repository, authority)
    release_id = "8" * 64
    retirement.register_installed(release_id)
    repository.create_or_recover_attempt(
        initial_transition(
            "install-f008",
            base_generation_id=None,
            base_lkg_generation_id=None,
            target_generation_id="generation-f008",
        ),
        request={"release_id": release_id},
    )
    with pytest.raises(LifecycleError) as blocked:
        retirement.start_retirement(
            release_id,
            operation_id="retire-f008",
            event_writer=_write_event,
            expected_epoch=1,
        )
    assert blocked.value.code == ErrorCode.RELEASE_RETIRING
    assert retirement.get(release_id)["state"] == "installed"


def test_f010_retirement_replay_is_operation_and_payload_bound() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    authority = _OperationAuthority()
    retirement = RetirementManager(repository, authority)
    release_id = "1" * 64
    retirement.register_installed(release_id)
    first = retirement.start_retirement(
        release_id,
        operation_id="retire-f010",
        event_writer=_write_event,
        expected_epoch=1,
    )
    assert retirement.start_retirement(
        release_id,
        operation_id="retire-f010",
        event_writer=lambda *_: (_ for _ in ()).throw(AssertionError("replay event")),
        expected_epoch=1,
    ) == first
    with pytest.raises(LifecycleError):
        retirement.start_retirement(
            release_id,
            operation_id="retire-f010-other",
            event_writer=_write_event,
            expected_epoch=999,
        )
    with pytest.raises(LifecycleError) as changed:
        retirement.start_retirement(
            release_id,
            operation_id="retire-f010",
            event_writer=_write_event,
            expected_epoch=2,
        )
    assert changed.value.code == ErrorCode.DUPLICATE_REQUEST


def test_f011_public_operation_id_uses_stable_hashed_path(
    tmp_path: Path, package_files
) -> None:
    stager = InstallStager(PackageStore(tmp_path / "packages"))
    operation_id = "install:valid/contract"
    staged = stager.stage(package_files, install_operation_id=operation_id)
    assert staged.install_operation_id == operation_id
    assert ":" not in staged.stage_dir.name and "/" not in staged.stage_dir.name
    assert stager.load(operation_id).stage_dir == staged.stage_dir
    marker = json.loads((staged.stage_dir / "install.json").read_text("utf-8"))
    assert marker["install_operation_id"] == operation_id


def test_f013_contradictory_publication_barrier_never_calls_remover() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    authority = _OperationAuthority()
    retirement = RetirementManager(repository, authority)
    release_id = "3" * 64
    retirement.register_installed(release_id)
    retiring = retirement.start_retirement(
        release_id,
        operation_id="start-f013",
        event_writer=_write_event,
        expected_epoch=1,
    )
    calls: list[str] = []
    with pytest.raises(LifecycleError) as mismatch:
        retirement.complete_retirement(
            release_id,
            operation_id="complete-f013",
            package_remover=lambda: calls.append("removed") or True,
            publication_barrier=lambda _c, rid: PublicationBarrierDecision(
                rid, True, ("candidate-blocker",)
            ),
            expected_epoch=retiring["retire_epoch"],
        )
    assert mismatch.value.code == ErrorCode.RESULT_CONTRACT_MISMATCH
    assert calls == []
    assert retirement.get(release_id)["state"] == "retiring"
