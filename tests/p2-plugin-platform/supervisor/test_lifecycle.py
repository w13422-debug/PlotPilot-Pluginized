from __future__ import annotations

import json
import math
import platform
import threading
from dataclasses import replace

import pytest
from conftest import RELEASE_A, RELEASE_B, FakeProcess
from plotpilot_core.supervisor import (
    AttemptFence,
    InstallFence,
    SupervisorConfig,
    WorkerState,
)
from plotpilot_plugin_sdk.errors import ContractError
from plotpilot_plugin_sdk.framing import decode_frame, encode_frame
from test_rpc import handshake_response, heartbeat


def complete_handshake(harness, ticket, process_index: int = -1) -> None:
    process = harness.processes.processes[process_index]
    request = decode_frame(process.writes[0])
    harness.supervisor.feed_stdout(ticket, handshake_response(request))
    assert harness.supervisor.status(ticket.worker_id).state == WorkerState.READY


def test_lazy_start_retain_idle_race_and_graceful_stop(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    same = harness.supervisor.acquire("worker-1")
    assert same.lifecycle_id == ticket.lifecycle_id
    assert same.retain_id != ticket.retain_id
    assert len(harness.processes.processes) == 1
    complete_handshake(harness, ticket)

    harness.supervisor.release(ticket)
    harness.supervisor.release(ticket)
    assert harness.supervisor.status("worker-1").clients == 1
    harness.supervisor.release(same)
    harness.clock.advance(19)
    harness.supervisor.tick()
    revived = harness.supervisor.acquire("worker-1")
    harness.clock.advance(2)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.READY

    harness.supervisor.release(revived)
    harness.clock.advance(20)
    harness.supervisor.tick()
    process = harness.processes.processes[0]
    assert harness.supervisor.status("worker-1").state == WorkerState.STOPPING
    assert decode_frame(process.writes[-1])["method"] == "runtime.shutdown"
    harness.clock.advance(3)
    harness.supervisor.tick()
    assert process.terminations == 1
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert harness.authority.holds(harness.fence, ticket.lifecycle_id)
    process.exit(0)
    assert harness.supervisor.status("worker-1").state == WorkerState.STOPPED
    assert not harness.authority.holds(harness.fence, ticket.lifecycle_id)


def test_retain_factory_collision_cannot_alias_late_release(harness) -> None:
    harness.supervisor._retain_id_factory = lambda: "collision"
    first = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, first)
    harness.supervisor.release(first)
    second = harness.supervisor.acquire("worker-1")
    assert first.retain_id != second.retain_id
    harness.supervisor.release(first)
    assert harness.supervisor.status("worker-1").clients == 1


def test_heartbeat_loss_terminates_only_timed_out_worker(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[(harness.fence.pin_id, ticket.lifecycle_id)] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    process = harness.processes.processes[0]
    harness.clock.advance(10)
    harness.supervisor.tick()
    assert process.terminations == 1
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert harness.authority.interruptions == [(harness.fence, attempt, ticket.lifecycle_id, "runtime.heartbeat timeout")]
    process.exit(-1)
    assert harness.supervisor.status("worker-1").state == WorkerState.FAILED


def test_stale_lease_and_release_callbacks_cannot_affect_replacement(harness) -> None:
    old_ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, old_ticket)
    old_process = harness.processes.processes[0]

    new_fence = replace(harness.fence, pin_id="pin-worker-2", release_id=RELEASE_B, pin_epoch=2, retire_epoch=2)
    new_package_hash = "d" * 64
    new_venv = harness.route.venv_root.parent / new_package_hash
    new_python = new_venv / "Scripts" / "python.exe"
    new_python.parent.mkdir(parents=True)
    new_python.write_bytes(b"python-b")
    (new_venv / "plotpilot-venv.json").write_text(
        json.dumps(
            {
                "schema": "plotpilot-venv/v1",
                "plugin_id": new_fence.plugin_id,
                "release_id": RELEASE_B,
                "package_hash": new_package_hash,
                "python_implementation": "cpython",
                "python_version": platform.python_version(),
            }
        ),
        encoding="utf-8",
    )
    new_route = replace(
        harness.route,
        release_id=RELEASE_B,
        package_hash=new_package_hash,
        generation_id=new_fence.generation_id,
        retire_epoch=2,
        venv_root=new_venv,
        python_executable=new_python,
    )
    harness.authority.values["worker-1"] = new_fence
    harness.routes.values[("worker-1", new_fence.generation_id, RELEASE_B, 2)] = new_route
    harness.packages.package.release_id = RELEASE_B
    harness.packages.package.package_hash = new_package_hash
    new_ticket = harness.supervisor.acquire("worker-1")
    assert old_process.terminations == 1
    complete_handshake(harness, new_ticket)
    replacement = harness.processes.processes[1]

    old_process.emit(heartbeat(seq=1))
    old_process.exit(9)
    harness.supervisor.release(old_ticket)
    assert replacement.terminations == 0
    assert harness.supervisor.status("worker-1").state == WorkerState.READY
    assert harness.supervisor.status("worker-1").clients == 1
    assert harness.authority.claims[new_fence.pin_id][1] == new_ticket.lifecycle_id


def test_crash_is_observed_and_next_acquire_restarts_under_same_authority(harness) -> None:
    first_ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, first_ticket)
    harness.processes.processes[0].exit(7)
    assert harness.supervisor.status("worker-1").state == WorkerState.CRASHED
    assert harness.authority.releases[-1] == (harness.fence, first_ticket.lifecycle_id)

    replacement_ticket = harness.supervisor.acquire("worker-1")
    assert replacement_ticket.lifecycle_id != first_ticket.lifecycle_id
    assert len(harness.processes.processes) == 2
    complete_handshake(harness, replacement_ticket)


def test_protocol_failure_is_scoped_to_exact_worker_process(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    process = harness.processes.processes[0]
    process.emit(encode_frame({"jsonrpc": "2.0", "id": "123e4567-e89b-12d3-a456-426614174099", "result": {}}))
    assert process.terminations == 1
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    process.exit(2)
    assert harness.supervisor.status("worker-1").state == WorkerState.FAILED


def test_termination_waits_for_exit_then_kills_and_stderr_is_bounded(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    process = harness.processes.processes[0]
    process.emit_stderr(b"x" * 2048 + b"TRACEBACK")
    assert len(harness.supervisor.status("worker-1").stderr_tail) == 1024
    assert harness.supervisor.status("worker-1").stderr_tail.endswith(b"TRACEBACK")

    attempt = AttemptFence("job-1", "step-1", "attempt-1", 4, ticket.lifecycle_id)
    harness.authority.attempts[(harness.fence.pin_id, ticket.lifecycle_id)] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    harness.clock.advance(10)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    harness.clock.advance(2)
    harness.supervisor.tick()
    assert process.kills == 1
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    process.exit(-9)
    assert harness.supervisor.status("worker-1").state == WorkerState.FAILED


def test_clean_exit_with_partial_frame_is_protocol_failure(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    process = harness.processes.processes[0]
    process.emit(b"Content-Len")
    process.exit(0)
    status = harness.supervisor.status("worker-1")
    assert status.state == WorkerState.FAILED
    assert "EOF protocol failure" in (status.failure or "")


def test_attempt_owner_and_claim_release_retry_are_exact(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    with pytest.raises(ContractError):
        harness.supervisor.bind_attempt(
            ticket,
            AttemptFence("job-1", "step-1", "attempt-1", 1, "other-connection"),
        )

    harness.authority.release_failures = 1
    process = harness.processes.processes[0]
    process.exit(7)
    assert harness.authority.holds(harness.fence, ticket.lifecycle_id)
    harness.supervisor.tick()
    assert not harness.authority.holds(harness.fence, ticket.lifecycle_id)
    releases = len(harness.authority.releases)
    process.exit(7)
    assert len(harness.authority.releases) == releases


def test_core_authority_must_hold_exact_attempt_and_install_fences(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)

    attempt = AttemptFence("job-1", "step-1", "attempt-1", 5, ticket.lifecycle_id)
    with pytest.raises(ContractError):
        harness.supervisor.bind_attempt(ticket, attempt)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    harness.authority.attempts[key] = replace(attempt, job_id="job-2")
    with pytest.raises(ContractError):
        harness.supervisor.unbind_attempt(ticket, attempt)

    install = InstallFence("install-1", 9, "installer-1")
    with pytest.raises(ContractError):
        harness.supervisor.bind_install(ticket, install)
    harness.authority.installs[key] = install
    harness.supervisor.bind_install(ticket, install)
    harness.authority.installs[key] = replace(install, install_lease_epoch=10)
    with pytest.raises(ContractError):
        harness.supervisor.unbind_install(ticket, install)


def test_bound_attempt_is_rechecked_against_core_on_each_heartbeat(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 5, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    harness.authority.attempts[key] = replace(attempt, step_id="step-2")
    harness.processes.processes[0].emit(heartbeat(seq=1, lease=5))
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING


@pytest.mark.parametrize("failure", ["marker", "package"])
def test_acquire_rejects_bad_release_identity_before_process_start(harness, failure: str) -> None:
    if failure == "marker":
        marker = harness.route.venv_root / "plotpilot-venv.json"
        value = json.loads(marker.read_text(encoding="utf-8"))
        value["package_hash"] = "e" * 64
        marker.write_text(json.dumps(value), encoding="utf-8")
    else:
        harness.packages.package.release_id = "e" * 64

    with pytest.raises(ContractError):
        harness.supervisor.acquire("worker-1")
    assert harness.processes.processes == []
    assert not harness.authority.claims


def test_synchronous_startup_callbacks_are_replayed_after_record_registration(harness, monkeypatch) -> None:
    def start(route, *, on_stdout, on_stderr, on_exit, on_transport_error):
        del route, on_stdout
        process = FakeProcess(lambda data: None, on_stderr, on_exit, on_transport_error)
        harness.processes.processes.append(process)
        on_stderr(b"early traceback")
        on_exit(7)
        return process

    monkeypatch.setattr(harness.processes, "start", start)
    with pytest.raises(ContractError):
        harness.supervisor.acquire("worker-1")
    status = harness.supervisor.status("worker-1")
    assert status is not None
    assert status.state == WorkerState.CRASHED
    assert status.stderr_tail == b"early traceback"
    assert not harness.authority.claims


def test_synchronous_startup_transport_failure_never_writes_handshake_or_returns_ticket(harness, monkeypatch) -> None:
    def start(route, *, on_stdout, on_stderr, on_exit, on_transport_error):
        del route
        process = FakeProcess(on_stdout, on_stderr, on_exit, on_transport_error)
        harness.processes.processes.append(process)
        on_transport_error("early broken pipe")
        return process

    monkeypatch.setattr(harness.processes, "start", start)
    with pytest.raises(ContractError):
        harness.supervisor.acquire("worker-1")
    process = harness.processes.processes[0]
    assert process.writes == []
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING


def test_poll_exit_waits_for_drained_callback_before_eof_and_release(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 6, ticket.lifecycle_id)
    harness.authority.attempts[(harness.fence.pin_id, ticket.lifecycle_id)] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    harness.clock.advance(10)
    harness.supervisor.tick()
    process = harness.processes.processes[0]
    process.code = -9
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert harness.authority.holds(harness.fence, ticket.lifecycle_id)
    process.emit(b"Content-Len")
    process.exit(-9)
    status = harness.supervisor.status("worker-1")
    assert status.state == WorkerState.FAILED
    assert status.failure == "runtime.heartbeat timeout"
    assert not harness.authority.holds(harness.fence, ticket.lifecycle_id)


def test_late_handshake_cannot_revive_timed_out_lifecycle(harness) -> None:
    harness.supervisor.acquire("worker-1")
    process = harness.processes.processes[0]
    request = decode_frame(process.writes[0])
    harness.clock.advance(5)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    process.emit(handshake_response(request))
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    harness.clock.advance(2)
    harness.supervisor.tick()
    assert process.kills == 1


def test_replacement_keeps_old_lifecycle_tracked_through_kill_deadline(harness) -> None:
    old_ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, old_ticket)
    old_process = harness.processes.processes[0]
    new_fence = replace(harness.fence, pin_id="pin-replacement", pin_epoch=2, retire_epoch=2)
    new_route = replace(harness.route, retire_epoch=2)
    harness.authority.values["worker-1"] = new_fence
    harness.routes.values[("worker-1", new_fence.generation_id, new_fence.release_id, 2)] = new_route
    new_ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, new_ticket)
    harness.clock.advance(2)
    harness.supervisor.tick()
    assert old_process.kills == 1
    assert harness.processes.processes[1].kills == 0
    assert harness.supervisor.status("worker-1").lifecycle_id == new_ticket.lifecycle_id
    assert harness.supervisor.status("worker-1").state == WorkerState.READY


def test_replacement_prunes_old_record_when_old_process_exits_before_current_swap(harness, monkeypatch) -> None:
    old_ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, old_ticket)
    old_process = harness.processes.processes[0]
    new_fence = replace(harness.fence, pin_id="pin-replacement", pin_epoch=2, retire_epoch=2)
    harness.authority.values["worker-1"] = new_fence
    harness.routes.values[("worker-1", new_fence.generation_id, new_fence.release_id, 2)] = replace(
        harness.route,
        retire_epoch=2,
    )
    original_start = harness.processes.start

    def exit_old_then_start(*args, **kwargs):
        old_process.exit(0)
        return original_start(*args, **kwargs)

    monkeypatch.setattr(harness.processes, "start", exit_old_then_start)
    new_ticket = harness.supervisor.acquire("worker-1")
    assert (old_ticket.worker_id, old_ticket.lifecycle_id) not in harness.supervisor._records
    assert (new_ticket.worker_id, new_ticket.lifecycle_id) in harness.supervisor._records
    assert harness.authority.releases.count((harness.fence, old_ticket.lifecycle_id)) == 1
    assert (new_fence, new_ticket.lifecycle_id) not in harness.authority.releases


@pytest.mark.parametrize("stage", ["lookup", "start"])
@pytest.mark.parametrize("release_mode", ["false", "exception"])
def test_prespawn_release_failure_is_retried_without_masking_original(harness, monkeypatch, stage: str, release_mode: str) -> None:
    if release_mode == "false":
        harness.authority.release_failures = 1
    else:
        harness.authority.release_exceptions = 1
    if stage == "lookup":
        harness.packages.package.release_id = "e" * 64
        expected = ContractError
    else:
        monkeypatch.setattr(harness.processes, "start", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("spawn sentinel")))
        expected = RuntimeError
    with pytest.raises(expected):
        harness.supervisor.acquire("worker-1")
    assert harness.authority.claims
    harness.supervisor.tick()
    assert not harness.authority.claims


@pytest.mark.parametrize("release_mode", ["false", "exception"])
def test_retain_id_failure_retries_exact_claim_before_any_spawn(harness, monkeypatch, release_mode: str) -> None:
    original = RuntimeError("retain-id sentinel")

    def fail_retain_id() -> str:
        raise original

    monkeypatch.setattr(harness.supervisor, "_retain_id_factory", fail_retain_id)
    if release_mode == "false":
        harness.authority.release_failures = 1
    else:
        harness.authority.release_exceptions = 1

    with pytest.raises(RuntimeError) as caught:
        harness.supervisor.acquire("worker-1")

    assert caught.value is original
    assert harness.processes.processes == []
    assert harness.supervisor.status("worker-1") is None
    claimed_fence, lifecycle_id = harness.authority.claims[harness.fence.pin_id]
    assert claimed_fence == harness.fence
    assert harness.authority.releases == [(harness.fence, lifecycle_id)]
    assert harness.supervisor._pending_releases[(harness.fence.worker_id, lifecycle_id)] == (
        harness.fence,
        lifecycle_id,
    )

    harness.supervisor.tick()
    assert harness.authority.releases == [
        (harness.fence, lifecycle_id),
        (harness.fence, lifecycle_id),
    ]
    assert not harness.authority.holds(harness.fence, lifecycle_id)
    assert harness.processes.processes == []


def test_attempt_and_install_are_busy_retains_until_last_unbind(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    install = InstallFence("install-1", 4, "installer-1")
    harness.authority.attempts[key] = attempt
    harness.authority.installs[key] = install
    harness.supervisor.bind_attempt(ticket, attempt)
    harness.supervisor.bind_install(ticket, install)
    harness.supervisor.release(ticket)
    for seq in range(1, 5):
        harness.clock.advance(9)
        harness.processes.processes[0].emit(heartbeat(seq=seq, lease=3))
        harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.READY
    harness.supervisor.unbind_attempt(ticket, attempt)
    harness.clock.advance(40)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.READY
    harness.supervisor.unbind_install(ticket, install)
    harness.clock.advance(19)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.READY
    harness.clock.advance(1)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.STOPPING


@pytest.mark.parametrize("failure_mode", ["false", "exception"])
def test_crash_attempt_reconciliation_precedes_claim_release_and_retries(harness, failure_mode: str) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    if failure_mode == "false":
        harness.authority.interrupt_failures = 1
    else:
        harness.authority.interrupt_exceptions = 1
    harness.processes.processes[0].exit(7)
    assert harness.authority.holds(harness.fence, ticket.lifecycle_id)
    assert not harness.authority.releases
    harness.supervisor.tick()
    assert not harness.authority.holds(harness.fence, ticket.lifecycle_id)
    assert harness.authority.interruptions[-1][:3] == (harness.fence, attempt, ticket.lifecycle_id)


def test_concurrent_terminal_cleanup_cannot_recreate_pending_release(harness, monkeypatch) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    entered = threading.Event()
    allow = threading.Event()
    original = harness.authority.release

    def blocking_release(fence, owner_id):
        entered.set()
        assert allow.wait(2)
        return original(fence, owner_id)

    monkeypatch.setattr(harness.authority, "release", blocking_release)
    exiting = threading.Thread(target=lambda: harness.processes.processes[0].exit(7))
    exiting.start()
    assert entered.wait(1)
    ticking = threading.Thread(target=harness.supervisor.tick)
    ticking.start()
    allow.set()
    exiting.join(1)
    ticking.join(1)
    assert not exiting.is_alive() and not ticking.is_alive()
    assert harness.authority.releases == [(harness.fence, ticket.lifecycle_id)]
    harness.supervisor.tick()
    assert harness.authority.releases == [(harness.fence, ticket.lifecycle_id)]


def test_termination_barrier_rejects_late_bind_and_unbind_without_losing_attempt(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    install = InstallFence("install-1", 4, "installer-1")
    harness.authority.attempts[key] = attempt
    harness.authority.installs[key] = install
    harness.supervisor.bind_attempt(ticket, attempt)
    harness.processes.processes[0].emit(
        encode_frame({"jsonrpc": "2.0", "id": "123e4567-e89b-12d3-a456-426614174099", "result": {}})
    )
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    with pytest.raises(ContractError):
        harness.supervisor.unbind_attempt(ticket, attempt)
    with pytest.raises(ContractError):
        harness.supervisor.bind_install(ticket, install)
    harness.processes.processes[0].exit(2)
    assert harness.authority.interruptions[-1][:3] == (harness.fence, attempt, ticket.lifecycle_id)
    assert not harness.authority.holds(harness.fence, ticket.lifecycle_id)


def test_revoked_bound_install_is_fenced_without_inbound_rpc(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    install = InstallFence("install-1", 7, "installer-1")
    harness.authority.installs[key] = install
    harness.supervisor.bind_install(ticket, install)
    harness.authority.installs[key] = replace(install, install_lease_epoch=8)
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert harness.processes.processes[0].terminations == 1


def _host_log(request_id: str, *, seq: int = 1) -> bytes:
    message = decode_frame(heartbeat(seq=seq, lease=3))
    message["id"] = request_id
    message["method"] = "host.log/v1"
    message["params"] = {"level": "info", "message": "ready", "fields_asset_id": None, "local_seq": seq}
    return encode_frame(message)


def _host_complete(request_id: str) -> bytes:
    message = decode_frame(heartbeat(seq=1, lease=3))
    message["id"] = request_id
    message["method"] = "host.job.complete/v1"
    message["params"] = {
        "operation_key": "complete-op-1",
        "worker_run_id": "worker-run-1",
        "outcome": "succeeded",
        "result_bundle_asset_id": None,
        "candidate_stage_operation_key": None,
        "terminal_detail_asset_id": None,
        "local_seq": 1,
    }
    return encode_frame(message)


def _host_complete_result() -> dict[str, object]:
    return {
        "accepted": True,
        "attempt_state": "succeeded",
        "step_state": "succeeded",
        "job_state": "succeeded",
        "provenance_receipt_id": "receipt-1",
        "job_event_seq": 1,
        "core_event_high_water": 1,
    }


def _host_install_release(request_id: str) -> bytes:
    return encode_frame(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "host.migration.lease.release/v1",
            "meta": {
                "protocol_version": "1",
                "generation_id": "generation-1",
                "plugin_release_id": RELEASE_A,
                "deadline_at": "2026-08-28T00:01:00Z",
                "context": "install",
                "operation_id": "release-op-1",
                "install_operation_id": "install-1",
                "install_lease_epoch": 7,
            },
            "params": {
                "operation_key": "release-op-1",
                "db_lease_id": "db-lease-1",
                "db_lease_epoch": 7,
                "owner_instance_id": "installer-1",
                "reason": "complete",
            },
        }
    )


def test_host_response_survives_terminal_authority_consumption_and_write_retry(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    request_id = "123e4567-e89b-12d3-a456-426614174099"
    harness.processes.processes[0].emit(_host_log(request_id))
    del harness.authority.attempts[key]
    process = harness.processes.processes[0]
    process.write_failures = 1
    with pytest.raises(BrokenPipeError):
        harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    first = process.writes[-1]
    harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    assert process.writes[-1] == first
    process.emit(_host_log(request_id))
    assert process.writes[-1] == first
    with pytest.raises(ContractError):
        harness.supervisor.respond_host_request(ticket, request_id, {"accepted": False, "dropped": False})


def test_terminal_commit_drains_exact_ack_before_termination_and_allows_terminal_unbind(harness) -> None:
    durable: dict[bytes, bytes] = {}
    harness.supervisor._durable_host_replay = lambda message: durable.get(encode_frame(dict(message)))
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    request_id = "123e4567-e89b-12d3-a456-426614174099"
    process = harness.processes.processes[0]
    terminal_request = _host_complete(request_id)
    process.emit(terminal_request)
    written_callbacks = []

    def delayed_write(data: bytes, *, on_written=None) -> None:
        process.writes.append(bytes(data))
        if on_written is not None:
            written_callbacks.append(on_written)

    process.write = delayed_write
    terminal_result = _host_complete_result()
    durable[terminal_request] = encode_frame({"jsonrpc": "2.0", "id": request_id, "result": terminal_result})
    del harness.authority.attempts[key]
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert process.terminations == 0
    assert decode_frame(process.writes[-1])["result"] == terminal_result
    harness.supervisor.respond_host_request(ticket, request_id, terminal_result)
    assert process.writes[-1] == process.writes[-2]
    harness.supervisor.unbind_attempt(ticket, attempt)
    for callback in written_callbacks:
        callback()
    assert process.terminations == 1
    process.exit(0)
    assert harness.authority.interruptions == []


def test_terminal_install_release_drains_durable_ack_before_exact_unbind(harness) -> None:
    durable: dict[bytes, bytes] = {}
    harness.supervisor._durable_host_replay = lambda message: durable.get(encode_frame(dict(message)))
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    install = InstallFence("install-1", 7, "installer-1")
    harness.authority.installs[key] = install
    harness.supervisor.bind_install(ticket, install)
    request_id = "123e4567-e89b-12d3-a456-426614174098"
    terminal_request = _host_install_release(request_id)
    process = harness.processes.processes[0]
    process.emit(terminal_request)
    callbacks = []

    def delayed_write(data: bytes, *, on_written=None) -> None:
        process.writes.append(bytes(data))
        if on_written is not None:
            callbacks.append(on_written)

    process.write = delayed_write
    result = {"accepted": True, "state": "released"}
    durable[terminal_request] = encode_frame({"jsonrpc": "2.0", "id": request_id, "result": result})
    del harness.authority.installs[key]
    harness.supervisor.tick()
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert process.terminations == 0
    harness.supervisor.unbind_install(ticket, install)
    callbacks.pop()()
    assert decode_frame(process.writes[-1])["result"] == result
    assert process.terminations == 1


def test_terminal_unbind_then_exit_before_ack_callback_never_reinterrupts_or_rereleases(harness) -> None:
    durable: dict[bytes, bytes] = {}
    harness.supervisor._durable_host_replay = lambda message: durable.get(encode_frame(dict(message)))
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    request_id = "123e4567-e89b-12d3-a456-426614174097"
    terminal_request = _host_complete(request_id)
    process = harness.processes.processes[0]
    process.emit(terminal_request)
    callbacks = []

    def delayed_write(data: bytes, *, on_written=None) -> None:
        process.writes.append(bytes(data))
        if on_written is not None:
            callbacks.append(on_written)

    process.write = delayed_write
    result = _host_complete_result()
    durable[terminal_request] = encode_frame({"jsonrpc": "2.0", "id": request_id, "result": result})
    del harness.authority.attempts[key]
    harness.supervisor.tick()
    harness.supervisor.unbind_attempt(ticket, attempt)
    process.exit(7)
    releases = tuple(harness.authority.releases)
    callbacks.pop()()
    assert harness.authority.interruptions == []
    assert tuple(harness.authority.releases) == releases
    assert releases.count((harness.fence, ticket.lifecycle_id)) == 1
    assert process.terminations == 0


def test_terminal_ack_callback_deadline_kills_only_deferred_lifecycle(harness) -> None:
    durable: dict[bytes, bytes] = {}
    harness.supervisor._durable_host_replay = lambda message: durable.get(encode_frame(dict(message)))
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    request_id = "123e4567-e89b-12d3-a456-426614174096"
    terminal_request = _host_complete(request_id)
    process = harness.processes.processes[0]
    process.emit(terminal_request)
    process.write = lambda data, *, on_written=None: process.writes.append(bytes(data))
    result = _host_complete_result()
    durable[terminal_request] = encode_frame({"jsonrpc": "2.0", "id": request_id, "result": result})
    del harness.authority.attempts[key]
    harness.supervisor.tick()
    assert process.terminations == 0
    harness.clock.advance(2)
    harness.supervisor.tick()
    assert process.kills == 1
    assert harness.authority.interruptions == []


def test_blocked_worker_write_does_not_hold_global_supervisor_lock(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    request_id = "123e4567-e89b-12d3-a456-426614174099"
    process = harness.processes.processes[0]
    process.emit(_host_log(request_id))
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_write(data: bytes, *, on_written=None) -> None:
        del data
        entered.set()
        assert unblock.wait(2)
        if on_written is not None:
            on_written()

    process.write = blocked_write
    responder = threading.Thread(
        target=lambda: harness.supervisor.respond_host_request(
            ticket,
            request_id,
            {"accepted": True, "dropped": False},
        )
    )
    responder.start()
    assert entered.wait(1)
    status_done = threading.Event()
    tick_done = threading.Event()
    threading.Thread(target=lambda: (harness.supervisor.status("worker-1"), status_done.set())).start()
    threading.Thread(target=lambda: (harness.supervisor.tick(), tick_done.set())).start()
    assert status_done.wait(1)
    assert tick_done.wait(1)
    unblock.set()
    responder.join(1)
    assert not responder.is_alive()


def test_heartbeats_do_not_consume_bounded_event_capacity(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    for seq in range(1, 2001):
        harness.processes.processes[0].emit(heartbeat(seq=seq, lease=3))
    assert harness.supervisor.drain_events(ticket) == ()
    assert harness.supervisor.status("worker-1").state == WorkerState.READY


def test_event_capacity_overflow_terminates_only_the_exact_lifecycle(harness) -> None:
    ticket = harness.supervisor.acquire("worker-1")
    complete_handshake(harness, ticket)
    key = (harness.fence.pin_id, ticket.lifecycle_id)
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 3, ticket.lifecycle_id)
    harness.authority.attempts[key] = attempt
    harness.supervisor.bind_attempt(ticket, attempt)
    process = harness.processes.processes[0]
    for index in range(64):
        request_id = f"123e4567-e89b-12d3-a456-{index:012x}"
        process.emit(_host_log(request_id, seq=index + 1))
        harness.supervisor.respond_host_request(ticket, request_id, {"accepted": True, "dropped": False})
    assert harness.supervisor.status("worker-1").state == WorkerState.READY
    process.emit(_host_log("123e4567-e89b-12d3-c456-426614174099", seq=65))
    assert harness.supervisor.status("worker-1").state == WorkerState.TERMINATING
    assert process.terminations == 1


@pytest.mark.parametrize("name", ["handshake_timeout", "heartbeat_timeout", "idle_timeout", "shutdown_timeout", "termination_timeout"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_timeout_configuration_rejects_every_nonfinite_value(name: str, value: float) -> None:
    with pytest.raises(ValueError):
        SupervisorConfig(**{name: value})
