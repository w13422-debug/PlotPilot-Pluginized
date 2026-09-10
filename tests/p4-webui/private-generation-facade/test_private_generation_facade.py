"""P1-P11, P13-P14 proof for the private generation facade."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from backend.plotpilot_core.webui import PrivateChapterGenerationFacade


def _table_count(stack, table: str) -> int:
    with stack.repository.read_connection() as connection:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _fence(stack):
    return stack.repository.read_chapter_generation_writer_fence(
        workspace_id="ws-1",
        chapter_document_id="chapter-1",
        operation="generate",
    )


def test_p1_default_unavailable_binding_has_zero_side_effect(facade_stack):
    facade = PrivateChapterGenerationFacade(
        facade_stack.repository, facade_stack.assets, facade_stack.runtime
    )
    before_assets = tuple(facade_stack.assets.metadata.iterdir())
    before_revisions = _table_count(facade_stack, "revision")

    status, body = facade.start("ws-1", "chapter-1", facade_stack.command())

    assert status == 409
    assert body == {
        "schema": "webui-chapter-generation-unavailable/v1",
        "workspace_id": "ws-1",
        "chapter_document_id": "chapter-1",
        "status": "unavailable",
        "reason_code": "verified_plugin_binding_unavailable",
        "reason": "No verified server-owned chapter RunSnapshot binding is configured.",
    }
    assert tuple(facade_stack.assets.metadata.iterdir()) == before_assets
    assert _table_count(facade_stack, "chapter_generation_writer_fence") == 0
    assert _table_count(facade_stack, "execution_job") == 0
    assert _table_count(facade_stack, "candidate") == 0
    assert _table_count(facade_stack, "revision") == before_revisions
    assert facade_stack.http.process_starts == 0


def test_p2_unverified_plugin_or_snapshot_fails_closed_before_fence(facade_stack):
    facade_stack.bind()
    facade_stack.capability_runtime.enabled = False

    status, body = facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )

    assert (status, body["schema"], body["reason_code"]) == (
        409,
        "webui-chapter-generation-unavailable/v1",
        "verified_plugin_unavailable",
    )
    assert _fence(facade_stack) is None
    assert facade_stack.http.process_starts == 0


def test_p2_missing_snapshot_asset_fails_closed_before_fence(facade_stack):
    request = facade_stack.request()
    facade_stack.bind()
    facade_stack.binding.resolutions[request.fingerprint] = replace(
        facade_stack.binding.resolutions[request.fingerprint],
        run_snapshot_asset_id="asset-sha256-" + "f" * 64,
    )

    status, body = facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )

    assert (status, body["reason_code"]) == (409, "verified_snapshot_unavailable")
    assert _fence(facade_stack) is None
    assert facade_stack.http.process_starts == 0


def test_p3_legacy_mode_is_explicitly_unavailable(facade_stack):
    status, body = facade_stack.facade.start(
        "ws-1",
        "chapter-1",
        facade_stack.command(requested_mode="legacy"),
    )

    assert (status, body["schema"], body["reason_code"]) == (
        409,
        "webui-chapter-generation-unavailable/v1",
        "legacy_generation_deferred",
    )
    assert _fence(facade_stack) is None
    assert facade_stack.http.process_starts == 0


def test_p4_verified_binding_delegates_exact_jobs_v2_start(facade_stack):
    binding = facade_stack.bind()

    status, body = facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )

    assert status == 201
    assert body["schema"] == "webui-chapter-generation-result/v1"
    assert body["run_snapshot_hash"] == binding.snapshot_hash
    assert body["job_result"]["schema"] == "job-command-result/v2"
    command = next(iter(facade_stack.http.commands.values()))
    assert command == {
        "schema": "job-start-command/v2",
        "operation_key": body["writer_fence"]["job_start_operation_key"],
        "workspace_id": "ws-1",
        "job_id": body["job_id"],
        "capability_id": binding.capability_id,
        "run_snapshot_asset_id": binding.run_snapshot_asset_id,
        "writer_epoch": 1,
    }


def test_p5_concurrent_starts_converge_on_one_fence_job_and_process(facade_stack):
    facade_stack.bind()
    command = facade_stack.command()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _index: facade_stack.facade.start(
                    "ws-1", "chapter-1", command
                ),
                range(2),
            )
        )

    assert results[0] == results[1]
    assert results[0][0] == 201
    assert _table_count(facade_stack, "chapter_generation_writer_fence") == 1
    assert len(facade_stack.http.commands) == 1
    assert facade_stack.http.process_starts == 1


def test_p6_same_operation_replays_exact_private_and_job_result(facade_stack):
    facade_stack.bind()
    command = facade_stack.command()

    first = facade_stack.facade.start("ws-1", "chapter-1", command)
    replay = facade_stack.facade.start("ws-1", "chapter-1", command)

    assert replay == first
    assert facade_stack.http.process_starts == 1
    assert _table_count(facade_stack, "chapter_generation_writer_fence") == 1


def test_p7_same_operation_with_outline_or_binding_drift_conflicts(facade_stack):
    first_command = facade_stack.command()
    facade_stack.bind(first_command)
    assert facade_stack.facade.start("ws-1", "chapter-1", first_command)[0] == 201

    drifted = facade_stack.command(outline="A different beat")
    facade_stack.bind(
        drifted, snapshot_id="snapshot-2", run_intent_id="run-intent-2"
    )
    status, body = facade_stack.facade.start("ws-1", "chapter-1", drifted)

    assert (status, body["error_code"]) == (409, "operation_payload_conflict")
    assert facade_stack.http.process_starts == 1
    assert _table_count(facade_stack, "chapter_generation_writer_fence") == 1


def test_p7_stale_expected_base_is_a_distinct_conflict(facade_stack):
    status, body = facade_stack.facade.start(
        "ws-1",
        "chapter-1",
        facade_stack.command(content_hash="b" * 64),
    )

    assert (status, body["error_code"]) == (409, "stale_base")
    assert _fence(facade_stack) is None
    assert facade_stack.http.process_starts == 0


def test_p8_uncertain_identity_is_not_replayed_and_new_identity_needs_durable_release(
    facade_stack,
):
    facade_stack.http.fail_uncertain = True
    command = facade_stack.command()
    facade_stack.bind(command)

    first = facade_stack.facade.start("ws-1", "chapter-1", command)
    replay = facade_stack.facade.start("ws-1", "chapter-1", command)

    assert first[1]["error_code"] == replay[1]["error_code"] == "uncertain_start"
    assert facade_stack.http.process_starts == 1
    old_fence = _fence(facade_stack)
    assert old_fence is not None and old_fence.state == "active"

    new_command = facade_stack.command("browser-op-2")
    facade_stack.bind(
        new_command, snapshot_id="snapshot-2", run_intent_id="run-intent-2"
    )
    status, body = facade_stack.facade.start("ws-1", "chapter-1", new_command)
    assert (status, body["error_code"]) == (409, "writer_active")

    facade_stack.http.job_states[old_fence.job_id] = "failed"
    assert facade_stack.facade.get("ws-1", "chapter-1")[0] == 200
    assert _fence(facade_stack).state == "released"

    facade_stack.http.fail_uncertain = False
    status, body = facade_stack.facade.start("ws-1", "chapter-1", new_command)
    assert status == 201
    assert body["writer_fence"]["chapter_writer_epoch"] == 2
    assert facade_stack.http.commands[
        body["writer_fence"]["job_start_operation_key"]
    ]["writer_epoch"] == 1


def test_p9_success_never_creates_candidate_or_revision(facade_stack):
    facade_stack.bind()
    before = {
        table: _table_count(facade_stack, table)
        for table in ("candidate", "revision")
    }

    assert facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )[0] == 201

    assert {
        table: _table_count(facade_stack, table)
        for table in ("candidate", "revision")
    } == before


def test_p10_facade_has_no_publication_or_direct_revision_shortcut(
    facade_stack, monkeypatch
):
    facade_stack.bind()

    def forbidden_publication(**_kwargs):
        raise AssertionError("facade attempted to publish a Core Revision")

    monkeypatch.setattr(facade_stack.repository, "publish_revision", forbidden_publication)
    assert not hasattr(facade_stack.facade, "accept")
    assert not hasattr(facade_stack.facade, "publish")
    assert facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )[0] == 201


def test_p11_malformed_job_success_never_masquerades_as_complete(facade_stack):
    facade_stack.bind()
    facade_stack.http.malformed_success = True

    status, body = facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )

    assert (status, body["error_code"]) == (409, "job_result_invalid")
    assert body["schema"] == "webui-chapter-generation-error/v1"


def test_p13_unknown_and_cross_workspace_chapters_are_indistinguishable(facade_stack):
    missing_status, missing = facade_stack.facade.get("ws-1", "missing")
    cross_status, cross = facade_stack.facade.get("ws-1", "chapter-other")

    assert missing_status == cross_status == 404
    assert tuple(missing) == tuple(cross)
    assert {
        key: value
        for key, value in missing.items()
        if key != "chapter_document_id"
    } == {
        key: value for key, value in cross.items() if key != "chapter_document_id"
    }


def test_p14_get_projects_active_job_then_releases_only_durable_terminal(
    facade_stack,
):
    facade_stack.bind()
    status, started = facade_stack.facade.start(
        "ws-1", "chapter-1", facade_stack.command()
    )
    assert status == 201

    status, active = facade_stack.facade.get("ws-1", "chapter-1")
    assert status == 200
    assert active["status"] == "active"
    assert active["current_job"]["job_id"] == started["job_id"]
    assert _fence(facade_stack).state == "active"

    facade_stack.http.job_states[started["job_id"]] = "succeeded"
    status, settled = facade_stack.facade.get("ws-1", "chapter-1")
    assert status == 200
    assert settled["status"] == "available"
    assert settled["writer_fence"] is None
    assert _fence(facade_stack).state == "released"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda command: {**command, "extra": True},
        lambda command: {**command, "schema": "wrong/v1"},
        lambda command: {**command, "requested_mode": "automatic"},
        lambda command: {**command, "expected_base": {}},
    ],
)
def test_closed_private_command_rejects_malformed_values_without_effect(
    facade_stack, mutation
):
    status, body = facade_stack.facade.start(
        "ws-1", "chapter-1", mutation(facade_stack.command())
    )

    assert (status, body["error_code"]) == (400, "malformed_request")
    assert _fence(facade_stack) is None
    assert facade_stack.http.process_starts == 0
