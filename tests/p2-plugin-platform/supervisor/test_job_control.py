from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

import pytest
from conftest import RELEASE_A
from plotpilot_core.supervisor import AttemptFence, WorkerState, WorkerTicket
from plotpilot_core.supervisor.job_control import JobControl, RunSnapshotWorker
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.framing import decode_frame, encode_frame

RELEASE_B = "b" * 64


@dataclass
class FakeSupervisor:
    tickets: list[WorkerTicket]
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    released: list[WorkerTicket] = field(default_factory=list)

    def acquire(self, worker_id: str, *, expected_release_id: str | None = None) -> WorkerTicket:
        self.requests.append((worker_id, expected_release_id))
        return self.tickets.pop(0)

    def release(self, ticket: WorkerTicket) -> None:
        self.released.append(ticket)


def _snapshot() -> RunSnapshotWorker:
    return RunSnapshotWorker(
        run_snapshot_id="run-snapshot-1",
        worker_id="worker-1",
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
    )


def _ticket(*, lifecycle_id: str, release_id: str = RELEASE_A) -> WorkerTicket:
    return WorkerTicket(
        worker_id="worker-1",
        lifecycle_id=lifecycle_id,
        retain_id=f"retain-{lifecycle_id}",
        release_id=release_id,
        pin_epoch=1,
        retire_epoch=1,
    )


def _lose_authority_after_process_start(
    harness,
    monkeypatch,
    *,
    terminate_raises: bool = False,
    poll_results: list[int | None] | None = None,
):
    trace: list[str] = []
    pending_poll_results = list(poll_results or ())
    original_start = harness.processes.start
    original_holds = harness.authority.holds
    original_release = harness.authority.release

    def start_and_lose_authority(route, **callbacks):
        process = original_start(route, **callbacks)
        original_terminate = process.terminate
        original_kill = process.kill
        original_poll = process.poll

        def terminate():
            trace.append("terminate")
            if terminate_raises:
                process.terminations += 1
                raise RuntimeError("injected terminate failure")
            original_terminate()

        def kill():
            trace.append("kill")
            original_kill()

        def poll():
            trace.append("poll")
            if pending_poll_results:
                return pending_poll_results.pop(0)
            return original_poll()

        process.terminate = terminate
        process.kill = kill
        process.poll = poll
        return process

    def holds(fence, lifecycle_id):
        if harness.processes.processes:
            return False
        return original_holds(fence, lifecycle_id)

    def release(fence, lifecycle_id):
        trace.append("release")
        return original_release(fence, lifecycle_id)

    monkeypatch.setattr(harness.processes, "start", start_and_lose_authority)
    monkeypatch.setattr(harness.authority, "holds", holds)
    monkeypatch.setattr(harness.authority, "release", release)
    return trace


def _ready_host_request(harness):
    ticket = harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_A)
    process = harness.processes.processes[-1]
    handshake = decode_frame(process.writes[-1])
    process.emit(
        encode_frame(
            {
                "jsonrpc": "2.0",
                "id": handshake["id"],
                "result": {
                    "plugin_protocol": "1",
                    "plugin_id": "com.plotpilot.test",
                    "release_id": RELEASE_A,
                    "capabilities": ["fixture.echo/v1"],
                    "worker_instance_id": "worker-instance-1",
                },
            }
        )
    )
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 2, ticket.lifecycle_id)
    harness.authority.attempts[(harness.fence.pin_id, ticket.lifecycle_id)] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    request_id = "123e4567-e89b-12d3-a456-426614174099"
    request_frame = encode_frame(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "host.log/v1",
            "meta": {
                "protocol_version": "1",
                "generation_id": "generation-1",
                "plugin_release_id": RELEASE_A,
                "deadline_at": "2026-08-28T00:01:00Z",
                "context": "attempt",
                "operation_id": "operation-log",
                "job_id": "job-1",
                "step_id": "step-1",
                "attempt_id": "attempt-1",
                "lease_epoch": 2,
            },
            "params": {
                "level": "info",
                "message": "ready",
                "fields_asset_id": None,
                "local_seq": 1,
            },
        }
    )
    process.emit(request_frame)
    return ticket, process, request_id, request_frame


def test_request_worker_passes_the_immutable_release_to_process_only_supervisor() -> None:
    supervisor = FakeSupervisor([_ticket(lifecycle_id="lifecycle-1")])
    snapshot = _snapshot()

    ticket = JobControl(supervisor).request_worker(snapshot)

    assert ticket.release_id == snapshot.release_id
    assert supervisor.requests == [(snapshot.worker_id, snapshot.release_id)]
    assert supervisor.released == []


def test_same_release_restart_keeps_the_original_runsnapshot_binding() -> None:
    supervisor = FakeSupervisor(
        [_ticket(lifecycle_id="lifecycle-1"), _ticket(lifecycle_id="lifecycle-2")]
    )
    control = JobControl(supervisor)
    snapshot = _snapshot()

    first = control.request_worker(snapshot)
    control.release_worker(first)
    second = control.request_worker(snapshot)

    assert first.lifecycle_id != second.lifecycle_id
    assert first.release_id == second.release_id == snapshot.release_id
    assert supervisor.requests == [("worker-1", RELEASE_A), ("worker-1", RELEASE_A)]
    assert supervisor.released == [first]


def test_release_substitution_is_released_and_rejected() -> None:
    substituted = _ticket(lifecycle_id="lifecycle-1", release_id=RELEASE_B)
    supervisor = FakeSupervisor([substituted])

    with pytest.raises(ContractError) as caught:
        JobControl(supervisor).request_worker(_snapshot())

    assert caught.value.code == ErrorCode.STALE_LEASE
    assert supervisor.released == [substituted]


def test_supervisor_rejects_wrong_expected_release_before_process_start(harness) -> None:
    with pytest.raises(ContractError) as caught:
        harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_B)

    assert caught.value.code == ErrorCode.STALE_LEASE
    assert harness.processes.processes == []


def test_stale_same_release_lifecycle_is_not_retained(harness) -> None:
    first = harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_A)
    first_process = harness.processes.processes[-1]
    harness.authority.claims.clear()

    second = harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_A)

    assert second.lifecycle_id != first.lifecycle_id
    assert second.release_id == first.release_id == RELEASE_A
    assert first_process.terminations == 1
    assert len(harness.processes.processes) == 2


def test_post_start_claim_loss_terminates_without_publishing_a_ticket(harness, monkeypatch) -> None:
    trace = _lose_authority_after_process_start(harness, monkeypatch)

    with pytest.raises(ContractError) as caught:
        harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_A)

    assert caught.value.code == ErrorCode.STALE_LEASE
    assert harness.supervisor.status("worker-1") is None
    assert len(harness.processes.processes) == 1
    process = harness.processes.processes[0]
    assert process.terminations == 1
    assert process.writes == []
    assert trace == ["terminate"]
    assert harness.authority.releases == []
    assert len(harness.authority.claims) == 1
    (fence, lifecycle_id), = harness.authority.claims.values()
    cleanup = harness.supervisor._records[(fence.worker_id, lifecycle_id)]
    assert cleanup.process is process
    assert cleanup.state == WorkerState.TERMINATING

    process.code = -15
    harness.supervisor.tick()

    assert trace == ["terminate", "poll", "release"]
    assert harness.authority.releases == [(fence, lifecycle_id)]
    assert harness.authority.claims == {}
    assert harness.supervisor._records == {}


def test_terminate_exception_keeps_cleanup_watchdog_visible_across_repeated_ticks(harness, monkeypatch) -> None:
    trace = _lose_authority_after_process_start(harness, monkeypatch, terminate_raises=True)

    with pytest.raises(ContractError) as caught:
        harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_A)

    assert caught.value.code == ErrorCode.STALE_LEASE
    process = harness.processes.processes[0]
    assert process.writes == []
    assert process.terminations == 1
    assert process.kills == 0
    assert harness.authority.releases == []
    (fence, lifecycle_id), = harness.authority.claims.values()
    cleanup_key = (fence.worker_id, lifecycle_id)
    cleanup = harness.supervisor._records[cleanup_key]
    assert cleanup.process is process
    assert cleanup.state == WorkerState.TERMINATING
    assert "terminate failed: injected terminate failure" in (cleanup.failure or "")

    harness.supervisor.tick()
    harness.clock.advance(2)
    harness.supervisor.tick()
    harness.clock.advance(2)
    harness.supervisor.tick()

    assert trace == ["terminate", "poll", "poll", "kill", "poll"]
    assert process.kills == 1
    assert harness.authority.releases == []
    assert harness.supervisor._records[cleanup_key].process is process

    process.code = -9
    harness.supervisor.tick()

    assert trace == ["terminate", "poll", "poll", "kill", "poll", "poll", "release"]
    assert harness.authority.releases == [(fence, lifecycle_id)]
    assert harness.authority.claims == {}
    assert cleanup_key not in harness.supervisor._records


def test_cleanup_exit_callback_cannot_release_claim_before_poll_confirmation(harness, monkeypatch) -> None:
    trace = _lose_authority_after_process_start(harness, monkeypatch, poll_results=[None, -9])

    with pytest.raises(ContractError):
        harness.supervisor.acquire("worker-1", expected_release_id=RELEASE_A)

    process = harness.processes.processes[0]
    (fence, lifecycle_id), = harness.authority.claims.values()
    process.exit(-9)

    assert harness.authority.releases == []
    assert harness.supervisor._records[(fence.worker_id, lifecycle_id)].state == WorkerState.TERMINATING

    harness.supervisor.tick()

    assert trace == ["terminate", "poll"]
    assert harness.authority.releases == []
    assert harness.authority.claims == {fence.pin_id: (fence, lifecycle_id)}

    harness.supervisor.tick()

    assert trace == ["terminate", "poll", "poll", "release"]
    assert harness.authority.releases == [(fence, lifecycle_id)]
    assert harness.authority.claims == {}


def test_host_event_without_staged_success_is_retained(harness) -> None:
    ticket, _, request_id, _ = _ready_host_request(harness)

    control = JobControl(harness.supervisor)
    with pytest.raises(ContractError) as caught:
        control.dispose_host_event(ticket, request_id)

    assert caught.value.code == ErrorCode.RESULT_CONTRACT_MISMATCH
    assert [event.message["id"] for event in control.peek_host_events(ticket)] == [request_id]


def test_host_events_are_non_destructive_until_exact_disposition(harness) -> None:
    ticket, process, request_id, request_frame = _ready_host_request(harness)
    control = JobControl(harness.supervisor)
    harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    process.emit(request_frame)

    assert [event.message["id"] for event in control.peek_host_events(ticket)] == [request_id]
    control.dispose_host_event(ticket, request_id)
    assert control.peek_host_events(ticket) == ()
    with pytest.raises(ContractError) as caught:
        control.dispose_host_event(ticket, request_id)
    assert caught.value.code == ErrorCode.RESULT_CONTRACT_MISMATCH


def test_stale_ticket_cannot_dispose_the_current_host_event(harness) -> None:
    ticket, _, request_id, _ = _ready_host_request(harness)
    stale = replace(ticket, lifecycle_id="stale-lifecycle")
    control = JobControl(harness.supervisor)

    with pytest.raises(ContractError) as caught:
        control.dispose_host_event(stale, request_id)

    assert caught.value.code == ErrorCode.STALE_LEASE
    assert [event.message["id"] for event in control.peek_host_events(ticket)] == [request_id]


def test_terminating_lifecycle_cannot_dispose_a_staged_host_event(harness) -> None:
    ticket, _, request_id, _ = _ready_host_request(harness)
    harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    harness.authority.claims.clear()
    harness.supervisor.tick()
    control = JobControl(harness.supervisor)

    with pytest.raises(ContractError) as caught:
        control.dispose_host_event(ticket, request_id)

    assert caught.value.code == ErrorCode.STALE_LEASE
    assert [event.message["id"] for event in control.peek_host_events(ticket)] == [request_id]


def test_concurrent_disposition_consumes_the_host_event_exactly_once(harness) -> None:
    ticket, _, request_id, _ = _ready_host_request(harness)
    harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    control = JobControl(harness.supervisor)

    def dispose() -> int | None:
        try:
            control.dispose_host_event(ticket, request_id)
        except ContractError as exc:
            return exc.code
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: dispose(), range(2)))

    assert results.count(None) == 1
    assert results.count(ErrorCode.RESULT_CONTRACT_MISMATCH) == 1
    assert control.peek_host_events(ticket) == ()
