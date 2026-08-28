from __future__ import annotations

import copy
import hashlib
import sqlite3
import threading

import pytest
from plotpilot_core.plugins.lifecycle import (
    LIFECYCLE_MIGRATIONS,
    LifecycleError,
    LifecycleRepository,
    VerifiedQualification,
    initial_transition,
)
from plotpilot_core.repositories.migrations import MigrationRunner
from plotpilot_plugin_sdk.errors import ErrorCode


def _repository(connection: sqlite3.Connection) -> LifecycleRepository:
    LifecycleRepository.initialize_standalone_schema_for_tests(connection)
    return LifecycleRepository(connection)


def test_f012_formal_migration_initializes_pointer_once() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    runner = MigrationRunner(connection)

    runner.apply(LIFECYCLE_MIGRATIONS)
    repository = LifecycleRepository(connection)
    first = repository.generation_state()
    first_row = connection.execute(
        """
        SELECT singleton,current_generation_id,lkg_generation_id,safe_mode,
               safe_mode_reason,revision
          FROM p2_plugin_generation_pointer
        """
    ).fetchall()

    connection.execute(
        """
        UPDATE p2_plugin_generation_pointer
           SET safe_mode=1,safe_mode_reason='preserve-on-reapply',revision=7
         WHERE singleton=1
        """
    )
    mutated_row = connection.execute(
        """
        SELECT singleton,current_generation_id,lkg_generation_id,safe_mode,
               safe_mode_reason,revision
          FROM p2_plugin_generation_pointer
        """
    ).fetchall()

    runner.apply(LIFECYCLE_MIGRATIONS)
    second = repository.generation_state()
    second_row = connection.execute(
        """
        SELECT singleton,current_generation_id,lkg_generation_id,safe_mode,
               safe_mode_reason,revision
          FROM p2_plugin_generation_pointer
        """
    ).fetchall()

    assert first.current is None and first.lkg is None and first.safe_mode is False
    assert first_row == [(1, None, None, 0, None, 0)]
    assert second.current is None and second.lkg is None and second.safe_mode is True
    assert second_row == mutated_row == [
        (1, None, None, 1, "preserve-on-reapply", 7)
    ]
    assert connection.execute(
        "SELECT COUNT(*),MAX(sha256) FROM schema_migration WHERE migration_id=?",
        (LIFECYCLE_MIGRATIONS[0].migration_id,),
    ).fetchone() == (1, LIFECYCLE_MIGRATIONS[0].sha256)


def _write_event(*_args) -> None:
    pass


def _allow_generation(*_args) -> None:
    pass


def _verify_qualification(_connection, evidence, generation) -> VerifiedQualification:
    return VerifiedQualification(
        qualification_id=evidence["qualification_id"],
        generation_id=generation["generation_id"],
        health_result_asset_id=generation["health_result_asset_id"],
        passed=True,
    )


def _release_pins(*_args) -> None:
    pass


def _generation(generation_id: str, base: str | None) -> dict:
    release_id = hashlib.sha256(f"release:{generation_id}".encode()).hexdigest()
    package_hash = hashlib.sha256(f"package:{generation_id}".encode()).hexdigest()
    return {
        "schema": "plugin-generation/v1",
        "generation_id": generation_id,
        "core_api_version": "1.2.0",
        "members": [
            {
                "plugin_id": "com.plotpilot.test.lifecycle",
                "release_id": release_id,
                "package_hash": package_hash,
                "data_generation_id": None,
                "ui_bundle_hash": None,
                "global_settings_revision_id": None,
                "settings_schema_hash": None,
                "data_bundle_asset_id": None,
            }
        ],
        "created_reason": "test",
        "created_at": "2026-08-28T01:00:00Z",
        "health_result_asset_id": f"asset-health-{generation_id}",
        "parent_generation_id": base,
        "base_generation_id": base,
    }


def _bound_request(target: dict, **extra) -> dict:
    member = target["members"][0]
    return {
        **extra,
        "plugin_id": member["plugin_id"],
        "release_id": member["release_id"],
        "package_hash": member["package_hash"],
        "settings_schema_hash": member["settings_schema_hash"],
    }


def _qualified(
    repository: LifecycleRepository, operation_id: str, target: dict
) -> None:
    state = repository.generation_state()
    base = None if state.current is None else state.current["generation_id"]
    lkg = None if state.lkg is None else state.lkg["generation_id"]
    repository.create_or_recover_attempt(
        initial_transition(
            operation_id,
            base_generation_id=base,
            base_lkg_generation_id=lkg,
            target_generation_id=target["generation_id"],
            created_at="2026-08-28T01:00:00Z",
        ),
        request=_bound_request(
            target,
            operation_id=operation_id,
            base=base,
            target_generation_id=target["generation_id"],
        ),
    )
    for expected, next_state, updates in (
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
        ("env_prepared", "shadow_prepared", {}),
        ("shadow_prepared", "migrated", {}),
        ("migrated", "settings_validated", {}),
    ):
        repository.advance_attempt(
            operation_id,
            expected_state=expected,
            target_state=next_state,
            updates=updates,
            at="2026-08-28T01:00:01Z",
        )
    repository.qualify_attempt(
        operation_id,
        generation=target,
        evidence={
            "qualification_id": f"precommit-{operation_id}",
            "generation_id": target["generation_id"],
            "health_result_asset_id": target["health_result_asset_id"],
        },
        qualification_verifier=_verify_qualification,
        execution_guard=_allow_generation,
        at="2026-08-28T01:00:01Z",
    )
    repository.advance_attempt(
        operation_id,
        expected_state="qualified",
        target_state="pending_apply",
        at="2026-08-28T01:00:01Z",
    )


def _install_lkg(
    repository: LifecycleRepository, operation_id: str, target: dict
) -> None:
    _qualified(repository, operation_id, target)
    repository.commit_current(
        operation_id,
        target,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
        at="2026-08-28T01:00:02Z",
    )
    repository.mark_lkg_pending(operation_id, at="2026-08-28T01:00:03Z")
    repository.promote_lkg(
        operation_id,
        evidence={
            "qualification_id": f"qualification-{operation_id}",
            "generation_id": target["generation_id"],
            "health_result_asset_id": target["health_result_asset_id"],
        },
        qualification_verifier=_verify_qualification,
        pin_releaser=_release_pins,
        at="2026-08-28T01:00:04Z",
    )


def test_restart_reconciliation_fences_execution_and_preserves_intent(tmp_path) -> None:
    path = tmp_path / "lifecycle.sqlite"
    first_connection = sqlite3.connect(path, isolation_level=None)
    first = _repository(first_connection)
    value = initial_transition(
        "install-crash",
        base_generation_id=None,
        base_lkg_generation_id=None,
        target_generation_id="generation-1",
        created_at="2026-08-28T01:00:00Z",
    )
    request = {"package_hash": "a" * 64, "target": "generation-1"}
    first.create_or_recover_attempt(value, request=request)
    first.advance_attempt(
        "install-crash",
        expected_state="selected",
        target_state="staged",
        updates={"package_store_status": "staged"},
        at="2026-08-28T01:00:01Z",
    )
    first_connection.close()

    second_connection = sqlite3.connect(path, isolation_level=None)
    recovered = _repository(second_connection)
    assert (
        recovered.create_or_recover_attempt(value, request=request)["state"] == "staged"
    )
    decision = recovered.reconcile_attempt("install-crash", package_present=True)
    assert decision.action == "resume"
    assert recovered.generation_state().safe_mode is False
    assert recovered.reconciliation_fences() == (("install-crash", "staged"),)
    with pytest.raises(LifecycleError) as duplicate:
        recovered.create_or_recover_attempt(
            value, request={"package_hash": "b" * 64, "target": "generation-1"}
        )
    assert duplicate.value.code == ErrorCode.DUPLICATE_REQUEST


def test_multiple_reconciliation_fences_resolve_independently_without_safe_mode() -> (
    None
):
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    for operation_id in ("install-a", "install-b"):
        repository.create_or_recover_attempt(
            initial_transition(
                operation_id,
                base_generation_id=None,
                base_lkg_generation_id=None,
                target_generation_id=f"generation-{operation_id}",
                created_at="2026-08-28T01:00:00Z",
            ),
            request={"operation_id": operation_id},
        )
        repository.advance_attempt(
            operation_id,
            expected_state="selected",
            target_state="staged",
            updates={"package_store_status": "staged"},
        )
        assert (
            repository.reconcile_attempt(operation_id, package_present=True).action
            == "resume"
        )
    assert repository.generation_state().safe_mode is False
    assert repository.reconciliation_fences() == (
        ("install-a", "staged"),
        ("install-b", "staged"),
    )
    repository.fail_attempt(
        "install-a",
        expected_state="staged",
        failure_code="cancelled",
        pin_releaser=_release_pins,
    )
    assert repository.reconciliation_fences() == (("install-b", "staged"),)
    repository.fail_attempt(
        "install-b",
        expected_state="staged",
        failure_code="cancelled",
        pin_releaser=_release_pins,
    )
    assert repository.reconciliation_fences() == ()


def test_concurrent_base_cas_supersedes_loser_and_event_is_atomic() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    repository = _repository(connection)
    base = _generation("generation-base", None)
    _install_lkg(repository, "install-base", base)
    left = _generation("generation-left", "generation-base")
    right = _generation("generation-right", "generation-base")
    _qualified(repository, "install-left", left)
    _qualified(repository, "install-right", right)

    events: list[tuple[str | None, str, str]] = []
    repository.commit_current(
        "install-left",
        left,
        event_writer=lambda _c, old, new, op: events.append((old, new, op)),
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    loser = repository.commit_current(
        "install-right",
        right,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    assert loser["state"] == "superseded"
    assert repository.generation_state().current["generation_id"] == "generation-left"
    assert events == [("generation-base", "generation-left", "install-left")]

    repository.mark_lkg_pending("install-left")
    repository.promote_lkg(
        "install-left",
        evidence={
            "qualification_id": "qualification-left",
            "generation_id": "generation-left",
            "health_result_asset_id": left["health_result_asset_id"],
        },
        qualification_verifier=_verify_qualification,
        pin_releaser=_release_pins,
    )

    # A failing P1 event writer rolls back both pointer and transition.
    third = _generation("generation-third", "generation-left")
    _qualified(repository, "install-third", third)
    with pytest.raises(RuntimeError):
        repository.commit_current(
            "install-third",
            third,
            event_writer=lambda *_: (_ for _ in ()).throw(
                RuntimeError("event write failed")
            ),
            execution_guard=_allow_generation,
            pin_releaser=_release_pins,
        )
    assert repository.generation_state().current["generation_id"] == "generation-left"
    assert repository.get_attempt("install-third")["state"] == "pending_apply"


def test_rollback_token_is_durable_and_consumed_once() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    repository = _repository(connection)
    base = _generation("generation-base", None)
    _install_lkg(repository, "install-base", base)
    failed = _generation("generation-failed", "generation-base")
    _qualified(repository, "install-failed", failed)
    repository.commit_current(
        "install-failed",
        failed,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    repository.mark_lkg_pending("install-failed")

    token = repository.arm_rollback("install-failed", token="rollback-stable")
    assert repository.arm_rollback("install-failed") == token
    with pytest.raises(LifecycleError) as second_token:
        repository.arm_rollback("install-failed", token="rollback-second")
    assert second_token.value.code == ErrorCode.DUPLICATE_REQUEST
    events: list[tuple[str | None, str, str]] = []
    rolled = repository.complete_rollback(
        "install-failed",
        token=token,
        event_writer=lambda _c, old, new, op: events.append((old, new, op)),
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    assert rolled["state"] == "rolled_back"
    assert (
        repository.complete_rollback(
            "install-failed",
            token=token,
            event_writer=_write_event,
            execution_guard=_allow_generation,
            pin_releaser=_release_pins,
        )
        == rolled
    )
    assert events == [("generation-failed", "generation-base", "install-failed")]
    assert repository.generation_state().current["generation_id"] == "generation-base"
    assert repository.get_attempt("install-failed")["rollback_attempt"] == 1


def test_no_rollback_target_enters_persistent_safe_mode() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    repository = _repository(connection)
    first = _generation("generation-first", None)
    _qualified(repository, "install-first", first)
    repository.commit_current(
        "install-first",
        first,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    token = repository.arm_rollback("install-first")
    result = repository.complete_rollback(
        "install-first",
        token=token,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    assert result["state"] == "safe_mode"
    assert repository.generation_state().safe_mode is True
    with pytest.raises(LifecycleError):
        repository.exit_safe_mode(generation_id="generation-first")


def test_rollback_refuses_unexecutable_base_and_emits_no_event() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    base = _generation("generation-base", None)
    _install_lkg(repository, "install-base", base)
    failed = _generation("generation-failed", "generation-base")
    _qualified(repository, "install-failed", failed)
    repository.commit_current(
        "install-failed",
        failed,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    token = repository.arm_rollback("install-failed")
    events: list[object] = []

    def unavailable(*_args) -> None:
        raise LifecycleError(
            "rollback release retired", code=ErrorCode.RELEASE_RETIRING
        )

    result = repository.complete_rollback(
        "install-failed",
        token=token,
        event_writer=lambda *_args: events.append(_args),
        execution_guard=unavailable,
        pin_releaser=_release_pins,
    )
    assert result["state"] == "safe_mode"
    assert repository.generation_state().current["generation_id"] == "generation-failed"
    assert repository.generation_state().safe_mode is True
    assert events == []


@pytest.mark.parametrize(
    "crash_state",
    [
        "selected",
        "staged",
        "package_published",
        "env_prepared",
        "shadow_prepared",
        "migrated",
        "settings_validated",
        "qualified",
        "pending_apply",
        "current_committed",
        "lkg_pending",
    ],
)
def test_each_nonterminal_crash_window_requires_reconciliation(
    crash_state: str,
) -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    target = _generation("generation-crash", None)
    repository.create_or_recover_attempt(
        initial_transition(
            "install-window",
            base_generation_id=None,
            base_lkg_generation_id=None,
            target_generation_id=target["generation_id"],
            created_at="2026-08-28T01:00:00Z",
        ),
        request=_bound_request(target, window=crash_state),
    )
    steps = [
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
        ("env_prepared", "shadow_prepared", {}),
        ("shadow_prepared", "migrated", {}),
        ("migrated", "settings_validated", {}),
        ("qualified", "pending_apply", {}),
    ]
    for expected, next_state, updates in steps:
        if repository.get_attempt("install-window")["state"] == crash_state:
            break
        if expected == "qualified" and repository.get_attempt("install-window")[
            "state"
        ] == "settings_validated":
            repository.qualify_attempt(
                "install-window",
                generation=target,
                evidence={
                    "qualification_id": "precommit-install-window",
                    "generation_id": target["generation_id"],
                    "health_result_asset_id": target["health_result_asset_id"],
                },
                qualification_verifier=_verify_qualification,
                execution_guard=_allow_generation,
                at="2026-08-28T01:00:01Z",
            )
            if crash_state == "qualified":
                break
        repository.advance_attempt(
            "install-window",
            expected_state=expected,
            target_state=next_state,
            updates=updates,
            at="2026-08-28T01:00:01Z",
        )
    if crash_state in {"current_committed", "lkg_pending"}:
        if repository.get_attempt("install-window")["state"] == "pending_apply":
            repository.commit_current(
                "install-window",
                target,
                event_writer=_write_event,
                execution_guard=_allow_generation,
                pin_releaser=_release_pins,
            )
        if crash_state == "lkg_pending":
            repository.mark_lkg_pending("install-window")
    decision = repository.reconcile_attempt("install-window", package_present=True)
    assert decision.action in {"resume", "qualify_or_rollback"}
    assert repository.generation_state().safe_mode is False
    assert repository.reconciliation_fences() == (("install-window", crash_state),)


def test_two_connections_linearize_generation_commit(tmp_path) -> None:
    database = tmp_path / "concurrent.sqlite"
    setup_connection = sqlite3.connect(database, isolation_level=None)
    setup = _repository(setup_connection)
    base = _generation("generation-base", None)
    _install_lkg(setup, "install-base", base)
    left = _generation("generation-left", "generation-base")
    right = _generation("generation-right", "generation-base")
    _qualified(setup, "install-left", left)
    _qualified(setup, "install-right", right)
    setup_connection.close()

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def commit(operation_id: str, generation: dict) -> None:
        connection = sqlite3.connect(database, isolation_level=None, timeout=5)
        try:
            repository = _repository(connection)
            barrier.wait(timeout=5)
            result = repository.commit_current(
                operation_id,
                generation,
                event_writer=_write_event,
                execution_guard=_allow_generation,
                pin_releaser=_release_pins,
            )
            with guard:
                results.append(result["state"])
        except (LifecycleError, sqlite3.Error, threading.BrokenBarrierError) as exc:
            with guard:
                errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=commit, args=("install-left", left)),
        threading.Thread(target=commit, args=("install-right", right)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert sorted(results) == ["current_committed", "superseded"]
    final_connection = sqlite3.connect(database, isolation_level=None)
    final = (
        _repository(final_connection)
        .generation_state()
        .current["generation_id"]
    )
    assert final in {"generation-left", "generation-right"}
    final_connection.close()


def test_transition_allowlist_freezes_install_identity_and_recovery_fields() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    value = initial_transition(
        "install-frozen",
        base_generation_id="generation-base",
        base_lkg_generation_id="generation-lkg",
        target_generation_id="generation-target",
        created_at="2026-08-28T01:00:00Z",
    )
    repository.create_or_recover_attempt(value, request={"operation": "frozen"})
    for field, changed in (
        ("base_generation_id", "other-base"),
        ("base_lkg_generation_id", "other-lkg"),
        ("target_generation_id", "other-target"),
        ("rollback_attempt", 1),
        ("rollback_token", "rollback-forged"),
    ):
        with pytest.raises(LifecycleError):
            repository.advance_attempt(
                "install-frozen",
                expected_state="selected",
                target_state="staged",
                updates={"package_store_status": "staged", field: changed},
            )
        assert repository.get_attempt("install-frozen") == value


def test_activated_attempt_cannot_jump_to_failure_terminal() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    target = _generation("generation-activated", None)
    _qualified(repository, "install-activated", target)
    repository.commit_current(
        "install-activated",
        target,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    with pytest.raises(LifecycleError):
        repository.advance_attempt(
            "install-activated",
            expected_state="current_committed",
            target_state="failed",
            updates={"failure_code": "forged_failure"},
        )
    assert repository.get_attempt("install-activated")["state"] == "current_committed"


def test_generic_transition_api_cannot_cross_authority_edges() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    target = _generation("generation-authority", None)
    repository.create_or_recover_attempt(
        initial_transition(
            "install-authority",
            base_generation_id=None,
            base_lkg_generation_id=None,
            target_generation_id=target["generation_id"],
        ),
        request=_bound_request(target, operation="authority"),
    )
    for expected, next_state, updates in (
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
        ("env_prepared", "shadow_prepared", {}),
        ("shadow_prepared", "migrated", {}),
        ("migrated", "settings_validated", {}),
    ):
        repository.advance_attempt(
            "install-authority",
            expected_state=expected,
            target_state=next_state,
            updates=updates,
        )
    with pytest.raises(LifecycleError):
        repository.advance_attempt(
            "install-authority",
            expected_state="settings_validated",
            target_state="qualified",
        )
    assert repository.get_attempt("install-authority")["state"] == "settings_validated"

    repository.qualify_attempt(
        "install-authority",
        generation=target,
        evidence={
            "qualification_id": "precommit-authority",
            "generation_id": target["generation_id"],
            "health_result_asset_id": target["health_result_asset_id"],
        },
        qualification_verifier=_verify_qualification,
        execution_guard=_allow_generation,
    )
    repository.advance_attempt(
        "install-authority",
        expected_state="qualified",
        target_state="pending_apply",
    )
    with pytest.raises(LifecycleError):
        repository.advance_attempt(
            "install-authority",
            expected_state="pending_apply",
            target_state="current_committed",
        )
    assert repository.generation_state().current is None
    repository.commit_current(
        "install-authority",
        target,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    repository.mark_lkg_pending("install-authority")
    with pytest.raises(LifecycleError):
        repository.advance_attempt(
            "install-authority",
            expected_state="lkg_pending",
            target_state="lkg_promoted",
        )
    assert repository.generation_state().lkg is None
    assert repository.get_attempt("install-authority")["qualification_id"] is None


def test_f001_frozen_generation_binding_and_payload_are_immutable() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    target = _generation("generation-f001", None)
    repository.create_or_recover_attempt(
        initial_transition(
            "install-f001",
            base_generation_id=None,
            base_lkg_generation_id=None,
            target_generation_id=target["generation_id"],
        ),
        request=_bound_request(target),
    )
    for expected, next_state, updates in (
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
        ("env_prepared", "shadow_prepared", {}),
        ("shadow_prepared", "migrated", {}),
        ("migrated", "settings_validated", {}),
    ):
        repository.advance_attempt(
            "install-f001",
            expected_state=expected,
            target_state=next_state,
            updates=updates,
        )
    unrelated = copy.deepcopy(target)
    unrelated["members"][0]["package_hash"] = "f" * 64
    with pytest.raises(LifecycleError):
        repository.qualify_attempt(
            "install-f001",
            generation=unrelated,
            evidence={
                "qualification_id": "qualification-f001-bad",
                "generation_id": target["generation_id"],
                "health_result_asset_id": target["health_result_asset_id"],
            },
            qualification_verifier=_verify_qualification,
            execution_guard=_allow_generation,
        )
    assert repository.get_attempt("install-f001")["state"] == "settings_validated"
    repository.qualify_attempt(
        "install-f001",
        generation=target,
        evidence={
            "qualification_id": "qualification-f001",
            "generation_id": target["generation_id"],
            "health_result_asset_id": target["health_result_asset_id"],
        },
        qualification_verifier=_verify_qualification,
        execution_guard=_allow_generation,
    )
    repository.advance_attempt(
        "install-f001", expected_state="qualified", target_state="pending_apply"
    )
    swapped = copy.deepcopy(target)
    swapped["created_reason"] = "payload swap"
    with pytest.raises(LifecycleError):
        repository.commit_current(
            "install-f001",
            swapped,
            event_writer=_write_event,
            execution_guard=_allow_generation,
            pin_releaser=_release_pins,
        )
    assert repository.get_generation(target["generation_id"]) == target
    assert repository.generation_state().current is None


def test_f003_post_activation_owner_and_rollback_replay_converge() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    base = _generation("generation-f003-base", None)
    _install_lkg(repository, "install-f003-base", base)
    first = _generation("generation-f003-first", base["generation_id"])
    _qualified(repository, "install-f003-first", first)
    repository.commit_current(
        "install-f003-first",
        first,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    successor = _generation("generation-f003-successor", first["generation_id"])
    _qualified(repository, "install-f003-successor", successor)
    with pytest.raises(LifecycleError):
        repository.commit_current(
            "install-f003-successor",
            successor,
            event_writer=_write_event,
            execution_guard=_allow_generation,
            pin_releaser=_release_pins,
        )
    token = repository.arm_rollback("install-f003-first", token="rollback-f003")
    repository.put_generation(successor)
    repository.connection.execute(
        "UPDATE p2_plugin_generation_pointer SET current_generation_id=? WHERE singleton=1",
        (successor["generation_id"],),
    )
    first_result = repository.complete_rollback(
        "install-f003-first",
        token=token,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    second_result = repository.complete_rollback(
        "install-f003-first",
        token=token,
        event_writer=_write_event,
        execution_guard=_allow_generation,
        pin_releaser=_release_pins,
    )
    assert first_result == second_result
    assert first_result["state"] == "superseded"
    assert repository.reconcile_attempt(
        "install-f003-first", package_present=True
    ).action == "none"


def test_f009_semantic_postconditions_reject_empty_stage_and_direct_commit() -> None:
    repository = _repository(sqlite3.connect(":memory:", isolation_level=None))
    target = _generation("generation-f009", None)
    value = initial_transition(
        "install-f009",
        base_generation_id=None,
        base_lkg_generation_id=None,
        target_generation_id=target["generation_id"],
    )
    repository.create_or_recover_attempt(value, request=_bound_request(target))
    with pytest.raises(LifecycleError):
        repository.advance_attempt(
            "install-f009", expected_state="selected", target_state="staged"
        )
    assert repository.get_attempt("install-f009") == value


def test_f012_foreign_transaction_is_rejected_and_constructors_do_not_ddl() -> None:
    empty = sqlite3.connect(":memory:", isolation_level=None)
    with pytest.raises(LifecycleError):
        LifecycleRepository(empty)
    connection = sqlite3.connect(":memory:", isolation_level=None)
    LifecycleRepository.initialize_standalone_schema_for_tests(connection)
    repository = LifecycleRepository(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(LifecycleError):
            repository.create_or_recover_attempt(
                initial_transition(
                    "install-f012",
                    base_generation_id=None,
                    base_lkg_generation_id=None,
                    target_generation_id="generation-f012",
                ),
                request={"operation": "foreign"},
            )
    finally:
        connection.rollback()
    assert repository.list_attempts() == ()
