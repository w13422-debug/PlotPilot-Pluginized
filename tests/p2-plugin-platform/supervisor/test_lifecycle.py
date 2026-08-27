from __future__ import annotations

import json
import platform
from dataclasses import replace

import pytest
from conftest import RELEASE_B, FakeProcess
from plotpilot_core.supervisor import AttemptFence, InstallFence, WorkerState
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
    assert len(harness.processes.processes) == 1
    complete_handshake(harness, ticket)

    harness.supervisor.release(ticket)
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
    def start(route, *, on_stdout, on_stderr, on_exit):
        del route, on_stdout
        process = FakeProcess(lambda data: None, on_stderr, on_exit)
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
    assert "EOF protocol failure" in (status.failure or "")
    assert not harness.authority.holds(harness.fence, ticket.lifecycle_id)
