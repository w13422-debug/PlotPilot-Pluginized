from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from plotpilot_plugin_sdk.fake_provider import FakeProvider  # noqa: E402
from plotpilot_plugin_sdk.framing import FrameDecoder, decode_frame, encode_frame  # noqa: E402
from plotpilot_plugin_sdk.rpc import ChunkUploadLedger, OperationLedger  # noqa: E402
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode  # noqa: E402


def test_frame_is_incremental_and_rejects_trailing_bytes() -> None:
    frame = encode_frame({"jsonrpc": "2.0", "method": "runtime.heartbeat"})
    decoder = FrameDecoder()
    assert decoder.feed(frame[:10]) == []
    assert decoder.feed(frame[10:]) == [{"jsonrpc": "2.0", "method": "runtime.heartbeat"}]
    with pytest.raises(ContractError):
        decode_frame(frame + b"x")


def test_operation_ledger_replays_exact_frame_and_rejects_payload_drift() -> None:
    ledger = OperationLedger()
    calls = 0

    def action() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"accepted": True, "value": "stable"}

    first = ledger.apply_frame("attempt-1", "host.job.event/v1", "op-1", {"x": 1}, action, response_id="123e4567-e89b-12d3-a456-426614174000")
    second = ledger.apply_frame("attempt-1", "host.job.event/v1", "op-1", {"x": 1}, action, response_id="different")
    assert first == second
    assert calls == 1
    with pytest.raises(ContractError) as caught:
        ledger.apply("attempt-1", "host.job.event/v1", "op-1", {"x": 2}, action)
    assert caught.value.code == ErrorCode.DUPLICATE_REQUEST


def test_chunk_upload_covers_final_hash_and_status_recovery() -> None:
    ledger = ChunkUploadLedger()
    content = b"hello"
    digest = hashlib.sha256(content).hexdigest()
    result = ledger.create(operation_key="op-1", upload_id="upload-1", offset=0, total_size=5, expected_hash=digest, chunk_hash=digest, base64_chunk="aGVsbG8=", final=True)
    assert result["completed"] is True
    assert ledger.status("upload-1", digest)["asset_id"] == "asset-upload-upload-1"
    with pytest.raises(ContractError) as caught:
        ledger.create(operation_key="op-2", upload_id="upload-1", offset=0, total_size=5, expected_hash=digest, chunk_hash=digest, base64_chunk="aGVsbG8=", final=False)
    assert caught.value.code == ErrorCode.INVALID_TRANSITION


def test_fake_provider_stream_pause_resume_cancel_and_receipt_are_deterministic() -> None:
    provider = FakeProvider()
    run = provider.start("invocation-1", "request", chunks=("a", "b"))
    assert [chunk.text for chunk in provider.stream(run.run_id)] == ["a", "ab"]
    receipt = provider.receipt(run.run_id)
    assert receipt.state == "succeeded"
    assert receipt.response_hash == hashlib.sha256(b"ab").hexdigest()
