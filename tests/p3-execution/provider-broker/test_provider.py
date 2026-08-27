from __future__ import annotations

import dataclasses
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SRC = REPOSITORY_ROOT / "first-party-plugins" / "provider-openai-compatible" / "backend" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from backend.plotpilot_plugin_sdk import hash_jcs  # noqa: E402
from plotpilot_provider_openai_compatible import (  # noqa: E402
    CancellationToken,
    DeterministicTransport,
    InvalidProfileError,
    ModelReceipt,
    OpenAICompatibleProvider,
    ProviderInvocation,
    ProviderError,
    ProviderTransportError,
    TransportResponse,
)


SECRET = "sk-live-provider-secret"
PROFILE = {
    "profile_revision_id": "profile-revision-1",
    "provider_plugin_id": "com.example.provider",
    "provider_release_id": "release-1",
    "endpoint": "https://api.example.test/v1",
    "model_name": "gpt-test",
    "api_key_ref": "secret://provider/default",
    "temperature": 0.2,
    "parameters": {"top_p": 0.9},
}
MESSAGES = ({"role": "user", "content": "hello"},)


def _provider(transport: DeterministicTransport, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        transport,
        secret_provider={"secret://provider/default": SECRET},
        **kwargs,
    )


def test_stream_request_is_deterministic_frozen_and_secret_safe():
    transport = DeterministicTransport(chunks=("hello", " world"))
    provider = _provider(transport)
    result = provider.invoke(
        PROFILE,
        MESSAGES,
        invocation_id="invocation-1",
        invocation_key="invoke-key-1",
        input_context={"workspace_id": "workspace-1", "display": "safe"},
    )

    assert result.state == "receipted"
    assert result.text == "hello world"
    assert [chunk.prefix for chunk in result.chunks] == ["hello", "hello world"]
    assert [chunk.seq for chunk in result.chunks] == [1, 2]
    assert result.receipt.state == "receipted"
    assert result.receipt.request_hash == result.invocation.request_hash
    assert "receipted" in result.receipt.lifecycle
    assert transport.network_calls == 1
    assert transport.last_request is not None
    assert transport.last_request.headers["Authorization"] == "<redacted>"
    assert SECRET not in repr(transport.last_request)
    assert SECRET not in repr(result)
    assert SECRET not in repr(result.receipt)
    assert SECRET not in str(result.receipt.as_dict())
    assert result.invocation.profile_revision["api_key_ref"] == PROFILE["api_key_ref"]

    expected_request_hash = hash_jcs(
        "openai-compatible-request/v1",
        {
            "endpoint": result.invocation.endpoint,
            "model": result.invocation.model,
            "body": result.invocation.as_dict()["body"],
            "profile_revision_id": result.invocation.profile_revision_id,
            "provider_plugin_id": result.invocation.provider_plugin_id,
            "provider_release_id": result.invocation.provider_release_id,
        },
    )
    assert result.invocation.request_hash == expected_request_hash


def test_non_stream_response_usage_and_response_persister_are_bound():
    response = {
        "id": "completion-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4, "cost": "0.001"},
    }
    transport = DeterministicTransport(response=response, stream=False)
    persisted = []
    result = _provider(transport).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-non-stream",
        stream=False,
        response_persister=lambda value, **_: persisted.append(value) or {"asset_id": "response-asset-1"},
    )

    assert result.state == "receipted"
    assert result.text == "answer"
    assert result.receipt.prompt_tokens == 3
    assert result.receipt.completion_tokens == 1
    assert result.receipt.total_tokens == 4
    assert result.receipt.cost == "0.001"
    assert result.receipt.response_asset_id == "response-asset-1"
    assert persisted and persisted[0]["text"] == "answer"
    assert result.receipt.stream_termination == "response"


def test_cancel_before_send_and_cancel_during_stream_are_terminal_without_retry():
    pre_cancel_transport = DeterministicTransport()
    pre_cancel_token = CancellationToken()
    pre_cancel_token.cancel()
    pre_cancel = _provider(pre_cancel_transport).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-pre-cancel",
        cancel_token=pre_cancel_token,
    )
    assert pre_cancel.state == "cancelled"
    assert pre_cancel_transport.send_calls == 0

    stream_transport = DeterministicTransport(chunks=("one", "two", "three"), cancel_after=1)
    cancelled = _provider(stream_transport).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-stream-cancel",
    )
    assert cancelled.state == "cancelled"
    assert cancelled.text == "one"
    assert stream_transport.send_calls == 1
    assert stream_transport.cancel_calls == 1
    assert cancelled.receipt.stream_termination == "cancelled"


def test_pre_send_failure_is_failed_and_post_send_failure_is_uncertain_without_query():
    before = _provider(DeterministicTransport(fail_before_send=SECRET))
    failed = before.invoke(PROFILE, MESSAGES, invocation_key="invoke-before-failure")
    assert failed.state == "failed"
    assert "before send" in (failed.error or "")
    assert SECRET not in str(failed.as_dict())

    after_transport = DeterministicTransport(
        fail_after_send=ProviderTransportError(SECRET, send_state="sent")
    )
    uncertain = _provider(after_transport).invoke(PROFILE, MESSAGES, invocation_key="invoke-after-failure")
    assert uncertain.state == "uncertain"
    assert "sent" in uncertain.receipt.lifecycle
    assert "uncertain" in uncertain.receipt.lifecycle
    assert after_transport.send_calls == 1
    assert after_transport.query_calls == 0
    assert SECRET not in str(uncertain.as_dict())


def test_query_capable_recovery_uses_same_key_and_never_sends_a_second_request():
    query_response = {
        "id": "recovered-completion",
        "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
    }
    transport = DeterministicTransport(
        fail_after_send=ProviderTransportError(SECRET, send_state="sent"),
        supports_query=True,
        query_response=query_response,
        stream=False,
    )
    result = _provider(transport).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-recover",
        stream=False,
        replay_policy="manual_if_unknown",
    )

    assert result.state == "receipted"
    assert result.text == "recovered"
    assert result.receipt.recovered is True
    assert "querying" in result.receipt.lifecycle
    assert transport.send_calls == 1
    assert transport.query_calls == 1


def test_http_error_malformed_response_and_persistence_failure_scrub_secrets():
    error_transport = DeterministicTransport(
        response={"status_code": 401, "body": {"error": {"message": f"bad credential {SECRET}"}}},
        stream=False,
    )
    error_result = _provider(error_transport).invoke(PROFILE, MESSAGES, invocation_key="invoke-http-error", stream=False)
    assert error_result.state == "failed"
    assert SECRET not in str(error_result.as_dict())
    assert error_result.receipt.response_hash is not None

    malformed = _provider(DeterministicTransport(malformed=True)).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-malformed",
    )
    assert malformed.state == "uncertain"
    assert "uncertain" in malformed.receipt.lifecycle

    persistence_failed = _provider(DeterministicTransport()).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-persist-failure",
        response_persister=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(SECRET)),
    )
    assert persistence_failed.state == "uncertain"
    assert SECRET not in str(persistence_failed.as_dict())


def test_receipt_sink_failure_is_uncertain_and_hashes_reject_external_drift():
    sink_calls = []

    def failing_sink(*args, **kwargs):
        sink_calls.append((args, kwargs))
        raise RuntimeError(SECRET)

    sink_result = _provider(DeterministicTransport()).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-receipt-failure",
        receipt_sink=failing_sink,
    )
    assert sink_result.state == "uncertain"
    assert "receipt persistence failed" in (sink_result.error or "")
    assert len(sink_calls) == 1
    assert "receipt_persistence_failed" in sink_result.receipt.lifecycle
    assert "receipted" not in sink_result.receipt.lifecycle
    assert SECRET not in str(sink_result.as_dict())

    provider = _provider(DeterministicTransport())
    invocation = provider.prepare(PROFILE, MESSAGES, invocation_key="invoke-hash")
    with pytest.raises(ValueError, match="request_hash"):
        dataclasses.replace(invocation, request_hash="0" * 64)
    result = provider.invoke(invocation)
    with pytest.raises(ValueError, match="receipt_hash"):
        dataclasses.replace(result.receipt, receipt_hash="0" * 64)


def test_nested_profiles_and_raw_secret_fields_are_handled_fail_closed():
    nested_profile = {
        "profile_revision_id": "profile-revision-nested",
        "provider": {
            "plugin_id": "com.example.provider",
            "release_id": "release-1",
            "endpoint": "https://api.example.test/v1",
            "model_name": "gpt-test",
            "api_key_ref": "secret://provider/default",
            "temperature": 0.1,
            "parameters": {"top_p": 0.8},
        },
    }
    prepared = _provider(DeterministicTransport()).prepare(nested_profile, MESSAGES)
    assert prepared.provider_plugin_id == "com.example.provider"
    assert prepared.provider_release_id == "release-1"
    assert prepared.model == "gpt-test"
    assert prepared.parameters["temperature"] == 0.1
    assert prepared.parameters["top_p"] == 0.8

    with pytest.raises(InvalidProfileError, match="secret-bearing"):
        _provider(DeterministicTransport()).prepare(
            {**nested_profile, "provider": {**nested_profile["provider"], "api_key": SECRET}},
            MESSAGES,
        )
    with pytest.raises(InvalidProfileError, match="reference"):
        _provider(DeterministicTransport()).prepare(
            {**PROFILE, "api_key_ref": SECRET},
            MESSAGES,
        )
    with pytest.raises(InvalidProfileError, match="reference"):
        _provider(DeterministicTransport()).prepare(
            {**PROFILE, "api_key_ref": "AIzaSyExampleRawKey"},
            MESSAGES,
        )


def test_no_real_network_transport_is_created_by_default():
    with pytest.raises(TypeError, match="transport is mandatory"):
        OpenAICompatibleProvider(None)  # type: ignore[arg-type]

    transport = DeterministicTransport()
    result = _provider(transport).invoke(PROFILE, MESSAGES, invocation_key="invoke-offline")
    assert result.state == "receipted"
    assert transport.network_calls == 1


def test_v1_replay_policy_retries_only_explicit_transient_pre_send_failure():
    class RetryOnceTransport:
        supports_query = False

        def __init__(self):
            self.calls = []

        def send(self, request):
            self.calls.append(request.redacted())
            if len(self.calls) == 1:
                raise ProviderTransportError(
                    "temporary", send_state="not_sent", transient=True
                )
            return TransportResponse(
                status_code=200,
                body={"choices": [{"message": {"content": "retried"}, "finish_reason": "stop"}]},
            )

    transport = RetryOnceTransport()
    result = _provider(transport).invoke(
        {**PROFILE, "max_retries": 1},
        MESSAGES,
        invocation_key="invoke-retry-once",
        stream=False,
        replay_policy="idempotent_auto",
    )
    assert result.state == "receipted"
    assert result.receipt.retry_count == 1
    assert len(transport.calls) == 2
    assert transport.calls[0] == transport.calls[1]
    assert transport.calls[0].invocation_key == "invoke-retry-once"

    no_retry = RetryOnceTransport()
    failed = _provider(no_retry).invoke(
        {**PROFILE, "max_retries": 3},
        MESSAGES,
        invocation_key="invoke-never-replay",
        stream=False,
        replay_policy="never_replay",
    )
    assert failed.state == "failed"
    assert failed.receipt.retry_count == 0
    assert len(no_retry.calls) == 1
    with pytest.raises(ProviderError, match="unsupported replay_policy"):
        _provider(DeterministicTransport()).prepare(
            PROFILE,
            MESSAGES,
            replay_policy="query",
        )


def test_unknown_or_conflicting_send_evidence_never_retries():
    class UnknownTransientError(RuntimeError):
        transient = True
        sent = False  # legacy bool is deliberately not proof of not_sent

    class UnknownTransport:
        supports_query = False

        def __init__(self):
            self.calls = 0

        def send(self, request):
            self.calls += 1
            raise UnknownTransientError("boundary unavailable")

    unknown = UnknownTransport()
    unknown_result = _provider(unknown).invoke(
        {**PROFILE, "max_retries": 3},
        MESSAGES,
        invocation_key="invoke-unknown-boundary",
        replay_policy="idempotent_auto",
    )
    assert unknown_result.state == "uncertain"
    assert unknown_result.receipt.retry_count == 0
    assert "send_unknown" in unknown_result.receipt.lifecycle
    assert unknown.calls == 1

    class ConflictingTransport:
        supports_query = False

        def __init__(self):
            self.calls = 0
            self.sent_keys = set()

        def send(self, request):
            self.calls += 1
            self.sent_keys.add(request.invocation_key)
            raise ProviderTransportError(
                "conflicting boundary",
                send_state="not_sent",
                transient=True,
            )

    conflicting = ConflictingTransport()
    conflict_result = _provider(conflicting).invoke(
        {**PROFILE, "max_retries": 3},
        MESSAGES,
        invocation_key="invoke-conflicting-boundary",
        replay_policy="idempotent_auto",
    )
    assert conflict_result.state == "uncertain"
    assert conflict_result.receipt.retry_count == 0
    assert conflicting.calls == 1


def test_stream_delta_is_always_appended_across_arbitrary_sse_fragments():
    payload = (
        'data: {"choices":[{"delta":{"content":"ab"}}]}\r\n\r\n'
        'data: {"choices":[{"delta":{"content":"ab"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"a"}}]}\n\n\n'
        'data: [DONE]\n\n'
    ).encode("utf-8")
    cuts = (1, 7, 19, 31, 48, 73, 101, len(payload))
    fragments = []
    start = 0
    for end in cuts:
        fragments.append(payload[start:end])
        start = end
    transport = DeterministicTransport(
        response=TransportResponse(status_code=200, body=fragments),
        stream=True,
    )
    result = _provider(transport).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-fragmented-sse",
    )
    assert result.state == "receipted"
    assert result.text == "ababa"
    assert [chunk.delta for chunk in result.chunks] == ["ab", "ab", "a"]
    assert [chunk.prefix for chunk in result.chunks] == ["ab", "abab", "ababa"]


def test_stripped_iter_lines_restores_blank_sse_frames():
    class IterLinesResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        body = None

        @staticmethod
        def iter_lines():
            return iter(
                (
                    b'data: {"choices":[{"delta":{"content":"a"}}]}',
                    b"",
                    b'data: {"choices":[{"delta":{"content":"a"}}]}',
                    b"",
                    b"data: [DONE]",
                    b"",
                )
            )

    class IterLinesTransport:
        supports_query = False

        @staticmethod
        def send(_request):
            return IterLinesResponse()

    result = _provider(IterLinesTransport()).invoke(
        PROFILE,
        MESSAGES,
        invocation_key="invoke-iter-lines",
    )
    assert result.state == "receipted"
    assert result.text == "aa"
    assert [chunk.delta for chunk in result.chunks] == ["a", "a"]
