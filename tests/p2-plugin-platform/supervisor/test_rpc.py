from __future__ import annotations

import uuid

import pytest
from conftest import RELEASE_A
from plotpilot_core.supervisor import AttemptFence, InstallFence
from plotpilot_core.supervisor.rpc import FramedRpcSession
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.framing import (
    BODY_LIMIT,
    FrameDecoder,
    decode_frame,
    encode_frame,
)


def new_session() -> tuple[FramedRpcSession, dict]:
    ids = iter(
        [
            "123e4567-e89b-12d3-a456-426614174000",
            "operation-1",
        ]
    )
    session = FramedRpcSession(
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        expected_capabilities=("fixture.echo/v1",),
        id_factory=lambda: next(ids),
    )
    request = decode_frame(session.begin_handshake(data_generation_id=None, deadline_at="2026-08-28T00:00:10Z"))
    return session, request


def handshake_response(request: dict, **overrides) -> bytes:
    result = {
        "plugin_protocol": "1",
        "plugin_id": "com.plotpilot.test",
        "release_id": request["meta"]["plugin_release_id"],
        "capabilities": ["fixture.echo/v1"],
        "worker_instance_id": "worker-instance-1",
    }
    result.update(overrides)
    return encode_frame({"jsonrpc": "2.0", "id": request["id"], "result": result})


def heartbeat(*, seq: int = 1, lease: int = 2, method: str = "runtime.heartbeat") -> bytes:
    return encode_frame(
        {
            "jsonrpc": "2.0",
            "method": method,
            "meta": {
                "protocol_version": "1",
                "generation_id": "generation-1",
                "plugin_release_id": RELEASE_A,
                "deadline_at": "2026-08-28T00:01:00Z",
                "context": "attempt",
                "operation_id": "operation-heartbeat",
                "job_id": "job-1",
                "step_id": "step-1",
                "attempt_id": "attempt-1",
                "lease_epoch": lease,
            },
            "params": {
                "worker_instance_id": "worker-instance-1",
                "observed_at": "2026-08-28T00:00:01Z",
                "local_seq": seq,
            },
        }
    )


def test_incremental_decoder_handles_fragmented_and_sticky_frames() -> None:
    first = encode_frame({"one": 1})
    second = encode_frame({"two": 2})
    decoder = FrameDecoder()
    assert decoder.feed(first[:7]) == []
    assert decoder.feed(first[7:] + second) == [{"one": 1}, {"two": 2}]


def test_decoder_rejects_oversized_and_unknown_frames_closed() -> None:
    decoder = FrameDecoder()
    with pytest.raises(ContractError):
        decoder.feed(f"Content-Length: {BODY_LIMIT + 1}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n".encode())

    session, request = new_session()
    session.feed(handshake_response(request))
    unknown = encode_frame({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "runtime.unknown", "meta": {}, "params": {}})
    with pytest.raises(ContractError):
        session.feed(unknown)
    with pytest.raises(ContractError, match="closed"):
        session.feed(heartbeat())


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"plugin_protocol": "2"}, ErrorCode.INCOMPATIBLE_GENERATION),
        ({"plugin_id": "com.plotpilot.other"}, ErrorCode.INCOMPATIBLE_GENERATION),
        ({"release_id": "b" * 64}, ErrorCode.INCOMPATIBLE_GENERATION),
    ],
)
def test_handshake_mismatch_is_rejected(override: dict, code: ErrorCode) -> None:
    session, request = new_session()
    with pytest.raises(ContractError) as caught:
        session.feed(handshake_response(request, **override))
    assert caught.value.code == code


def test_heartbeat_is_lease_worker_and_sequence_fenced() -> None:
    session, request = new_session()
    assert session.feed(handshake_response(request))[0].kind == "handshake"
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1")
    session.bind_attempt(attempt)
    assert session.feed(heartbeat())[0].kind == "heartbeat"
    with pytest.raises(ContractError) as caught:
        session.feed(heartbeat(seq=2, lease=1))
    assert caught.value.code == ErrorCode.STALE_LEASE


def test_heartbeat_rejects_wrong_worker_identity_independently() -> None:
    session, request = new_session()
    session.feed(handshake_response(request))
    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1"))
    message = decode_frame(heartbeat())
    message["params"]["worker_instance_id"] = "worker-instance-other"
    with pytest.raises(ContractError) as caught:
        session.feed(encode_frame(message))
    assert caught.value.code == ErrorCode.STALE_LEASE


def test_heartbeat_rejects_duplicate_sequence_independently() -> None:
    session, request = new_session()
    session.feed(handshake_response(request))
    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1"))
    session.feed(heartbeat(seq=5))
    with pytest.raises(ContractError) as caught:
        session.feed(heartbeat(seq=5))
    assert caught.value.code == ErrorCode.INVALID_TRANSITION


def test_host_request_success_is_closed_and_bound_to_pending_id() -> None:
    session, request = new_session()
    session.feed(handshake_response(request))
    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1"))
    host_request = {
        "jsonrpc": "2.0",
        "id": "123e4567-e89b-12d3-a456-426614174099",
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
        "params": {"level": "info", "message": "ready", "fields_asset_id": None, "local_seq": 1},
    }
    event = session.feed(encode_frame(host_request))[0]
    assert event.kind == "host_request"
    response = decode_frame(session.build_host_success(host_request["id"], {"accepted": True, "dropped": False}))
    assert response == {
        "jsonrpc": "2.0",
        "id": host_request["id"],
        "result": {"accepted": True, "dropped": False},
    }
    repeated = session.build_host_success(host_request["id"], {"accepted": True, "dropped": False})
    assert repeated == session.host_replay_frame(host_request["id"])
    with pytest.raises(ContractError) as caught:
        session.build_host_success(host_request["id"], {"accepted": False, "dropped": False})
    assert caught.value.code == ErrorCode.DUPLICATE_REQUEST


@pytest.mark.parametrize(
    ("field", "value"),
    [("job_id", "job-other"), ("step_id", "step-other"), ("attempt_id", "attempt-other")],
)
def test_same_epoch_other_attempt_is_stale(field: str, value: str) -> None:
    session, request = new_session()
    session.feed(handshake_response(request))
    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1"))
    other = decode_frame(heartbeat())
    other["meta"][field] = value
    with pytest.raises(ContractError) as caught:
        session.feed(encode_frame(other))
    assert caught.value.code == ErrorCode.STALE_LEASE


def test_install_epoch_is_independent_from_attempt_epoch() -> None:
    session, request = new_session()
    session.feed(handshake_response(request))
    install = InstallFence("install-1", 7, "installer-1")
    session.bind_install(install)
    install_log = {
        "jsonrpc": "2.0",
        "id": "123e4567-e89b-12d3-a456-426614174098",
        "method": "host.log/v1",
        "meta": {
            "protocol_version": "1",
            "generation_id": "generation-1",
            "plugin_release_id": RELEASE_A,
            "deadline_at": "2026-08-28T00:01:00Z",
            "context": "install",
            "operation_id": "operation-install-log",
            "install_operation_id": "install-1",
            "install_lease_epoch": 7,
        },
        "params": {"level": "info", "message": "install", "fields_asset_id": None, "local_seq": 1},
    }
    assert session.feed(encode_frame(install_log))[0].kind == "host_request"


@pytest.mark.parametrize(
    "tail",
    [
        b"Content-Len",
        b"Content-Length: 10\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{}",
    ],
)
def test_eof_rejects_every_incomplete_or_trailing_frame(tail: bytes) -> None:
    session, _ = new_session()
    try:
        session.feed(tail)
    except ContractError:
        return
    with pytest.raises(ContractError) as caught:
        session.finish()
    assert caught.value.code == ErrorCode.ASSET_ERROR


def test_eof_rejects_garbage_after_complete_frame() -> None:
    session, request = new_session()
    events = session.feed(handshake_response(request) + b"x")
    assert events[0].kind == "handshake"
    with pytest.raises(ContractError) as caught:
        session.finish()
    assert caught.value.code == ErrorCode.ASSET_ERROR


def test_outbound_duplicate_id_never_overwrites_first_pending_request() -> None:
    ids = iter(["123e4567-e89b-12d3-a456-426614174000"] * 3)
    session = FramedRpcSession(
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        expected_capabilities=("fixture.echo/v1",),
        id_factory=lambda: next(ids),
    )
    request = decode_frame(session.begin_handshake(data_generation_id=None, deadline_at="2026-08-28T00:00:10Z"))
    session.feed(handshake_response(request))
    first = decode_frame(session.build_shutdown(reason="idle", deadline_at="2026-08-28T00:01:00Z"))
    with pytest.raises(ContractError) as caught:
        session.build_shutdown(reason="other", deadline_at="2026-08-28T00:02:00Z")
    assert caught.value.code == ErrorCode.DUPLICATE_REQUEST
    assert session.pending_count == 1
    response = {"jsonrpc": "2.0", "id": first["id"], "result": {"accepted": True}}
    assert session.feed(encode_frame(response))[0].kind == "response"


def test_pending_and_inbound_limits_are_hard_and_recover_after_drain() -> None:
    ids = iter(
        [
            "123e4567-e89b-12d3-a456-426614174000",
            "123e4567-e89b-12d3-a456-426614174001",
            "123e4567-e89b-12d3-a456-426614174002",
            "123e4567-e89b-12d3-a456-426614174003",
        ]
    )
    session = FramedRpcSession(
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        expected_capabilities=("fixture.echo/v1",),
        id_factory=lambda: next(ids),
        pending_limit=1,
        inbound_limit=1,
    )
    request = decode_frame(session.begin_handshake(data_generation_id=None, deadline_at="2026-08-28T00:00:10Z"))
    session.feed(handshake_response(request))
    shutdown = decode_frame(session.build_shutdown(reason="idle", deadline_at="2026-08-28T00:01:00Z"))
    with pytest.raises(ContractError):
        session.build_shutdown(reason="idle", deadline_at="2026-08-28T00:01:01Z")
    assert session.pending_count == 1
    session.feed(encode_frame({"jsonrpc": "2.0", "id": shutdown["id"], "result": {"accepted": True}}))
    assert session.pending_count == 0
    session.build_shutdown(reason="idle", deadline_at="2026-08-28T00:01:02Z")

    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1"))
    first = decode_frame(heartbeat())
    first.update({"id": "123e4567-e89b-12d3-a456-426614174010", "method": "host.log/v1"})
    first["params"] = {"level": "info", "message": "one", "fields_asset_id": None, "local_seq": 1}
    assert session.feed(encode_frame(first))[0].kind == "host_request"
    second = dict(first)
    second["id"] = "123e4567-e89b-12d3-a456-426614174011"
    second["params"] = {**first["params"], "message": "two"}
    with pytest.raises(ContractError):
        session.feed(encode_frame(second))


def test_written_inbound_capacity_is_reusable_and_exact_replay_bypasses_consumed_attempt() -> None:
    ledger: dict[bytes, bytes] = {}
    session = FramedRpcSession(
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        expected_capabilities=("fixture.echo/v1",),
        id_factory=lambda: "123e4567-e89b-12d3-a456-426614174000",
        inbound_limit=1,
        durable_host_replay=lambda message: ledger.get(encode_frame(dict(message))),
    )
    request = decode_frame(session.begin_handshake(data_generation_id=None, deadline_at="2026-08-28T00:00:10Z"))
    session.feed(handshake_response(request))
    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1"))
    first = decode_frame(heartbeat())
    first.update({"id": "123e4567-e89b-12d3-a456-426614174010", "method": "host.log/v1"})
    first["params"] = {"level": "info", "message": "one", "fields_asset_id": None, "local_seq": 1}
    frame = encode_frame(first)
    session.feed(frame)
    response = session.build_host_success(first["id"], {"accepted": True, "dropped": False})
    ledger[frame] = response
    session.mark_host_success_written(first["id"], response)
    session.unbind_attempt(session.attempt_fence)
    assert session.feed(frame)[0].kind == "host_replay"
    session.bind_attempt(AttemptFence("job-1", "step-1", "attempt-2", 3, "connection-1"))
    second = dict(first)
    second["id"] = "123e4567-e89b-12d3-a456-426614174011"
    second["meta"] = {**first["meta"], "attempt_id": "attempt-2", "lease_epoch": 3}
    second["params"] = {**first["params"], "message": "two", "local_seq": 2}
    assert session.feed(encode_frame(second))[0].kind == "host_request"
    assert session.inbound_count == 1
    second_response = session.build_host_success(second["id"], {"accepted": True, "dropped": False})
    ledger[encode_frame(second)] = second_response
    session.mark_host_success_written(second["id"], second_response)
    replay = session.feed(frame)[0]
    assert replay.kind == "host_replay"
    assert session.host_replay_frame(first["id"]) == response


def test_durable_replay_handles_duplicate_after_commit_before_local_response_staging() -> None:
    ledger: dict[bytes, bytes] = {}
    session = FramedRpcSession(
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        expected_capabilities=("fixture.echo/v1",),
        id_factory=lambda: "123e4567-e89b-12d3-a456-426614174000",
        durable_host_replay=lambda message: ledger.get(encode_frame(dict(message))),
    )
    handshake = decode_frame(session.begin_handshake(data_generation_id=None, deadline_at="2026-08-28T00:00:10Z"))
    session.feed(handshake_response(handshake))
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1")
    session.bind_attempt(attempt)
    request = decode_frame(heartbeat())
    request.update({"id": "123e4567-e89b-12d3-a456-426614174010", "method": "host.log/v1"})
    request["params"] = {"level": "info", "message": "one", "fields_asset_id": None, "local_seq": 1}
    frame = encode_frame(request)
    assert session.feed(frame)[0].kind == "host_request"
    canonical = encode_frame(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"accepted": True, "dropped": False},
        }
    )
    ledger[frame] = canonical
    session.unbind_attempt(attempt)
    assert session.feed(frame)[0].kind == "host_replay"
    assert session.host_replay_frame(request["id"]) == canonical


@pytest.mark.parametrize("variant", ["payload", "method", "meta"])
def test_durable_replay_rejects_same_id_with_nonidentical_exact_request(variant: str) -> None:
    ledger: dict[bytes, bytes] = {}
    session = FramedRpcSession(
        plugin_id="com.plotpilot.test",
        generation_id="generation-1",
        release_id=RELEASE_A,
        expected_capabilities=("fixture.echo/v1",),
        id_factory=lambda: "123e4567-e89b-12d3-a456-426614174000",
        durable_host_replay=lambda message: ledger.get(encode_frame(dict(message))),
    )
    handshake = decode_frame(session.begin_handshake(data_generation_id=None, deadline_at="2026-08-28T00:00:10Z"))
    session.feed(handshake_response(handshake))
    attempt = AttemptFence("job-1", "step-1", "attempt-1", 2, "connection-1")
    session.bind_attempt(attempt)
    request = decode_frame(heartbeat())
    request.update({"id": "123e4567-e89b-12d3-a456-426614174010", "method": "host.log/v1"})
    request["params"] = {"level": "info", "message": "one", "fields_asset_id": None, "local_seq": 1}
    exact_frame = encode_frame(request)
    ledger[exact_frame] = encode_frame(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"accepted": True, "dropped": False},
        }
    )
    session.unbind_attempt(attempt)
    stale = dict(request)
    if variant == "payload":
        stale["params"] = {**request["params"], "message": "different-payload"}
    elif variant == "method":
        stale["method"] = "host.job.complete/v1"
    else:
        stale["meta"] = {**request["meta"], "operation_id": "different-operation"}
    with pytest.raises(ContractError) as caught:
        session.feed(encode_frame(stale))
    assert caught.value.code in {ErrorCode.STALE_LEASE, ErrorCode.RESULT_CONTRACT_MISMATCH}
