from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.checkpoints import SQLiteOrchestrationOwnerStore


def test_workspace_has_one_live_orchestration_owner_and_expired_epoch_replaces_it(execution_stack):
    owners = execution_stack["authority"].orchestration_owners
    start = "2026-08-30T00:00:00Z"
    first = owners.acquire(
        "ws-1",
        "orchestrator-1",
        owner_token="owner-token-1",
        lease_ttl_seconds=30,
        now=start,
    )
    assert first.acquired and first.lease_epoch == 1

    with pytest.raises(ContractError) as caught:
        owners.acquire(
            "ws-1",
            "orchestrator-2",
            owner_token="owner-token-2",
            lease_ttl_seconds=30,
            now="2026-08-30T00:00:10Z",
        )
    assert caught.value.code == int(ErrorCode.STALE_LEASE)

    checked = owners.assert_owner(
        "ws-1",
        "orchestrator-1",
        "owner-token-1",
        1,
        now="2026-08-30T00:00:10Z",
    )
    assert checked.owner_instance_id == "orchestrator-1"

    replaced = owners.acquire(
        "ws-1",
        "orchestrator-2",
        owner_token="owner-token-2",
        lease_ttl_seconds=30,
        now="2026-08-30T00:01:00Z",
    )
    assert replaced.owner_instance_id == "orchestrator-2"
    assert replaced.lease_epoch == 2
    with pytest.raises(ContractError) as caught:
        owners.assert_owner(
            "ws-1",
            "orchestrator-1",
            "owner-token-1",
            1,
            now="2026-08-30T00:01:00Z",
        )
    assert caught.value.code == int(ErrorCode.STALE_LEASE)

    row = execution_stack["repository"]._connection.execute(
        "SELECT workspace_id,owner_instance_id,lease_epoch FROM execution_orchestration_owner"
    ).fetchall()
    assert [tuple(item) for item in row] == [("ws-1", "orchestrator-2", 2)]


def test_owner_release_is_compare_and_set_and_does_not_release_a_new_epoch(execution_stack):
    owners = execution_stack["authority"].orchestration_owners
    first = owners.acquire(
        "ws-1",
        "orchestrator-1",
        owner_token="owner-token-1",
        lease_ttl_seconds=30,
        now="2026-08-30T00:00:00Z",
    )
    released = owners.release(
        "ws-1",
        "orchestrator-1",
        first.owner_token,
        first.lease_epoch,
        now="2026-08-30T00:00:05Z",
    )
    assert released.lease_expires_at == "2026-08-30T00:00:05Z"
    second = owners.acquire(
        "ws-1",
        "orchestrator-2",
        owner_token="owner-token-2",
        lease_ttl_seconds=30,
        now="2026-08-30T00:00:06Z",
    )
    assert second.lease_epoch == 2
    with pytest.raises(ContractError) as caught:
        owners.release(
            "ws-1",
            "orchestrator-1",
            first.owner_token,
            first.lease_epoch,
            now="2026-08-30T00:00:07Z",
        )
    assert caught.value.code == int(ErrorCode.STALE_LEASE)
    assert execution_stack["authority"].orchestration_owners.get("ws-1").owner_instance_id == "orchestrator-2"


def test_owner_tokenless_live_claim_and_independent_connection_races_are_fenced(
    execution_stack,
):
    database = execution_stack["database"]
    first_store = execution_stack["authority"].orchestration_owners
    second_repository = CoreAuthorityRepository(database)
    second_store = SQLiteOrchestrationOwnerStore(second_repository)
    try:
        first = first_store.acquire(
            "ws-1",
            "orchestrator-1",
            owner_token="owner-token-1",
            lease_ttl_seconds=1,
            now="2026-08-30T00:00:00Z",
        )

        with pytest.raises(ContractError) as caught:
            second_store.acquire(
                "ws-1",
                "orchestrator-1",
                now="2026-08-30T00:00:00Z",
            )
        assert caught.value.code == int(ErrorCode.STALE_LEASE)

        def take_over(store, instance):
            try:
                return ("ok", store.acquire(
                    "ws-1",
                    instance,
                    owner_token=f"token-{instance}",
                    now="2026-08-30T00:00:02Z",
                ))
            except ContractError as exc:
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda item: take_over(*item),
                ((first_store, "orchestrator-2"), (second_store, "orchestrator-3")),
            ))
        successes = [value for status, value in outcomes if status == "ok"]
        failures = [value for status, value in outcomes if status == "error"]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == int(ErrorCode.STALE_LEASE)
        winner = successes[0]
        assert winner.lease_epoch == 2

        with pytest.raises(ContractError) as caught:
            first_store.renew(
                "ws-1",
                first.owner_instance_id,
                first.owner_token,
                first.lease_epoch,
                now="2026-08-30T00:00:03Z",
            )
        assert caught.value.code == int(ErrorCode.STALE_LEASE)

        with pytest.raises(ContractError) as caught:
            first_store.release(
                "ws-1",
                first.owner_instance_id,
                first.owner_token,
                first.lease_epoch,
                now="2026-08-30T00:00:03Z",
            )
        assert caught.value.code == int(ErrorCode.STALE_LEASE)

        with pytest.raises(ContractError) as caught:
            second_store.acquire(
                "ws-1",
                winner.owner_instance_id,
                now="2026-08-30T00:00:03Z",
            )
        assert caught.value.code == int(ErrorCode.STALE_LEASE)

        renewed = second_store.renew(
            "ws-1",
            winner.owner_instance_id,
            winner.owner_token,
            winner.lease_epoch,
            lease_ttl_seconds=30,
            now="2026-08-30T00:00:03Z",
        )
        assert renewed.lease_epoch == 2
        assert renewed.revision == 3
    finally:
        second_repository.close()
