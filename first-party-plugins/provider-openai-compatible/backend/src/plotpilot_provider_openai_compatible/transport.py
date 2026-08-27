"""Small transport boundary for the OpenAI-compatible provider.

The provider deliberately does not own an HTTP client.  A Host supplies an
implementation of :class:`OpenAICompatibleTransport`; tests use the
deterministic implementation in this module.  Keeping the boundary as a
plain protocol also makes it possible for a host to enforce its own network,
proxy and credential policy without giving this package Core database access.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


def freeze_value(value: Any) -> Any:
    """Return a recursively immutable JSON-shaped view.

    ``TransportRequest`` is handed to code outside this package.  Freezing
    the request prevents a caller from changing the model, messages or
    parameters after the request hash has been calculated.
    """

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, (str, int, float, bool, bytes)) or value is None:
        return value
    raise TypeError(f"unsupported transport value: {type(value).__name__}")


def thaw_value(value: Any) -> Any:
    """Convert an immutable request view to ordinary JSON-compatible values."""

    if isinstance(value, Mapping):
        return {str(key): thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_value(item) for item in value]
    if isinstance(value, bytes):
        return value
    return value


_SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key", "api_key"}
)


@dataclass(frozen=True, slots=True, repr=False)
class TransportRequest(Mapping[str, Any]):
    """A single, ephemeral OpenAI-compatible request.

    ``headers`` may contain a one-shot bearer token while the request is in
    the transport call.  The custom representation and ``redacted`` view
    never expose that value.  The provider does not retain this object after
    the invocation completes.
    """

    endpoint: str
    model: str
    messages: tuple[Mapping[str, Any], ...]
    parameters: Mapping[str, Any]
    headers: Mapping[str, str]
    stream: bool
    invocation_key: str
    request_hash: str
    body: Mapping[str, Any]
    cancel_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(freeze_value(item) for item in self.messages))
        object.__setattr__(self, "parameters", freeze_value(self.parameters))
        object.__setattr__(self, "headers", MappingProxyType({str(k): str(v) for k, v in self.headers.items()}))
        object.__setattr__(self, "body", freeze_value(self.body))

    def _mapping(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "messages": self.messages,
            "parameters": self.parameters,
            "headers": self.headers,
            "stream": self.stream,
            "invocation_key": self.invocation_key,
            "request_hash": self.request_hash,
            "body": self.body,
        }

    def __getitem__(self, key: str) -> Any:
        return self._mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())

    def to_mapping(self) -> Mapping[str, Any]:
        """Return the transport-facing immutable mapping (headers included)."""

        return MappingProxyType(self._mapping())

    def redacted(self) -> "TransportRequest":
        """Return a copy safe for deterministic request inspection."""

        headers = {
            key: "<redacted>" if key.casefold() in _SENSITIVE_HEADER_NAMES else value
            for key, value in self.headers.items()
        }
        return TransportRequest(
            endpoint=self.endpoint,
            model=self.model,
            messages=self.messages,
            parameters=self.parameters,
            headers=headers,
            stream=self.stream,
            invocation_key=self.invocation_key,
            request_hash=self.request_hash,
            body=self.body,
            cancel_token=self.cancel_token,
        )

    def __repr__(self) -> str:
        safe_headers = {
            key: "<redacted>" if key.casefold() in _SENSITIVE_HEADER_NAMES else value
            for key, value in self.headers.items()
        }
        return (
            "TransportRequest("
            f"endpoint={self.endpoint!r}, model={self.model!r}, stream={self.stream!r}, "
            f"invocation_key={self.invocation_key!r}, request_hash={self.request_hash!r}, "
            f"headers={safe_headers!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TransportResponse:
    """Minimal response envelope accepted by the provider."""

    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.status_code) is not int:
            raise TypeError("status_code must be an int")
        object.__setattr__(self, "headers", MappingProxyType({str(k): str(v) for k, v in self.headers.items()}))
        if isinstance(self.metadata, Mapping):
            object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def __repr__(self) -> str:
        body_type = type(self.body).__name__
        return f"TransportResponse(status_code={self.status_code!r}, body_type={body_type!r})"


class ProviderTransportError(RuntimeError):
    """A transport failure with an explicit three-state send boundary."""

    def __init__(
        self,
        message: str,
        *,
        send_state: str = "unknown",
        transient: bool = False,
    ) -> None:
        if send_state not in {"sent", "not_sent", "unknown"}:
            raise ValueError("send_state must be sent, not_sent, or unknown")
        self.send_state = send_state
        self.transient = bool(transient)
        super().__init__(str(message))


TransportError = ProviderTransportError


@runtime_checkable
class OpenAICompatibleTransport(Protocol):
    """Host-supplied transport required at the provider boundary.

    ``supports_query`` must be explicitly ``True`` before the provider will
    call ``query`` for an uncertain invocation.  A provider never infers
    query safety merely from the presence of a method.
    """

    supports_query: bool

    def send(self, request: TransportRequest) -> TransportResponse | Iterable[Any]:
        """Send one request; the method must not silently retry it."""

    def cancel(self, invocation_key: str) -> None:
        """Stop an in-flight request when the transport supports cancellation."""

    def query(self, invocation_key: str) -> TransportResponse | Iterable[Any]:
        """Query the already submitted invocation by the same key."""


class DeterministicTransport:
    """Offline transport used by unit tests and local deterministic paths.

    It creates OpenAI-shaped JSON/SSE responses and never opens a socket.
    Failure knobs make the pre-send/post-send uncertainty boundary explicit.
    """

    supports_query = False

    def __init__(
        self,
        chunks: Iterable[Any] | None = None,
        *,
        response: Any = None,
        response_body: Any = None,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        stream: bool = True,
        fail_before_send: BaseException | str | None = None,
        fail_after_send: BaseException | str | None = None,
        raise_before_send: BaseException | str | None = None,
        raise_after_send: BaseException | str | None = None,
        error: BaseException | str | None = None,
        malformed: bool = False,
        supports_query: bool = False,
        query_response: Any = None,
        query_result: Any = None,
        cancel_after: int | None = None,
    ) -> None:
        if chunks is None:
            chunks = ("deterministic", " output")
        self.chunks = tuple(chunks)
        self.response = response if response is not None else response_body
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.stream = bool(stream)
        self.fail_before_send = fail_before_send if fail_before_send is not None else raise_before_send
        self.fail_after_send = fail_after_send if fail_after_send is not None else raise_after_send
        self.error = error
        self.malformed = bool(malformed)
        self.supports_query = bool(supports_query)
        self.supports_idempotency_query = self.supports_query
        self.query_response = query_response if query_response is not None else query_result
        self.cancel_after = cancel_after
        if cancel_after is not None and (type(cancel_after) is not int or cancel_after < 1):
            raise ValueError("cancel_after must be a positive integer or None")

        self.requests: list[TransportRequest] = []
        self.last_request: TransportRequest | None = None
        self.sent_keys: set[str] = set()
        self.cancelled_keys: set[str] = set()
        self.send_calls = 0
        self.query_calls = 0
        self.cancel_calls = 0

    @property
    def request_count(self) -> int:
        return self.send_calls

    @property
    def network_calls(self) -> int:
        """Always the offline send count; useful for no-network assertions."""

        return self.send_calls

    def _failure(self, value: BaseException | str | None, *, send_state: str) -> BaseException | None:
        if value is None:
            return None
        if isinstance(value, BaseException):
            if isinstance(value, ProviderTransportError):
                return value
            return ProviderTransportError(str(value), send_state=send_state)
        return ProviderTransportError(str(value), send_state=send_state)

    def send(self, request: TransportRequest) -> TransportResponse | Iterable[Any]:
        self.send_calls += 1
        self.last_request = request.redacted()
        self.requests.append(self.last_request)

        failure = self._failure(self.fail_before_send or self.error, send_state="not_sent")
        if failure is not None:
            raise failure

        self.sent_keys.add(request.invocation_key)
        failure = self._failure(self.fail_after_send, send_state="sent")
        if failure is not None:
            raise failure

        if self.response is not None:
            return self._coerce_configured_response(self.response)
        if self.malformed:
            return TransportResponse(
                status_code=self.status_code,
                headers=self.headers,
                body=b"{malformed-json",
            )
        if self.stream:
            return TransportResponse(
                status_code=self.status_code,
                headers={"content-type": "text/event-stream", **self.headers},
                body=self._stream_body(request),
            )
        return TransportResponse(
            status_code=self.status_code,
            headers={"content-type": "application/json", **self.headers},
            body=self._completion_body(),
        )

    def _coerce_configured_response(self, value: Any) -> Any:
        if isinstance(value, TransportResponse):
            return value
        if isinstance(value, Mapping) and any(key in value for key in ("status_code", "status", "body")):
            status = value.get("status_code", value.get("status", self.status_code))
            body = value.get("body", value.get("json", value.get("data")))
            headers = value.get("headers", self.headers)
            return TransportResponse(status_code=status, headers=headers, body=body)
        return TransportResponse(status_code=self.status_code, headers=self.headers, body=value)

    def _stream_body(self, request: TransportRequest) -> Iterator[Any]:
        emitted = 0
        for item in self.chunks:
            if request.invocation_key in self.cancelled_keys or _token_cancelled(request.cancel_token):
                return
            emitted += 1
            yield self._stream_item(item)
            if self.cancel_after is not None and emitted >= self.cancel_after:
                self.cancel(request.invocation_key)
                if request.cancel_token is not None and hasattr(request.cancel_token, "cancel"):
                    request.cancel_token.cancel()
                return
        if request.invocation_key not in self.cancelled_keys and not _token_cancelled(request.cancel_token):
            yield "data: [DONE]\n\n"

    @staticmethod
    def _stream_item(item: Any) -> Any:
        if isinstance(item, Mapping):
            return item
        if isinstance(item, bytes):
            return item
        if isinstance(item, str) and (item.startswith("data:") or item.strip() == "[DONE]"):
            return item
        payload = {"choices": [{"delta": {"content": str(item)}}]}
        return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"

    def _completion_body(self) -> dict[str, Any]:
        text = "".join(str(item) for item in self.chunks if not isinstance(item, Mapping))
        return {
            "id": "deterministic-completion",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        }

    def cancel(self, invocation_key: str) -> None:
        self.cancel_calls += 1
        self.cancelled_keys.add(str(invocation_key))

    def query(self, invocation_key: str) -> TransportResponse | Iterable[Any]:
        self.query_calls += 1
        if not self.supports_query:
            raise ProviderTransportError("transport query is not supported", send_state="sent")
        if self.query_response is not None:
            return self._coerce_configured_response(self.query_response)
        if self.response is not None:
            return self._coerce_configured_response(self.response)
        # A query with no configured result is deliberately not treated as a
        # successful replay.  The caller remains in the uncertain window.
        raise ProviderTransportError(
            f"no deterministic query result for {invocation_key}",
            send_state="sent",
        )

    def __repr__(self) -> str:
        return (
            "DeterministicTransport("
            f"send_calls={self.send_calls}, query_calls={self.query_calls}, "
            f"cancel_calls={self.cancel_calls}, supports_query={self.supports_query})"
        )


class TestTransport(DeterministicTransport):
    """Natural test spelling retained as a distinct, discoverable class."""


OfflineTransport = DeterministicTransport


def _token_cancelled(token: Any) -> bool:
    if token is None:
        return False
    value = getattr(token, "cancelled", None)
    if isinstance(value, bool):
        return value
    method = getattr(token, "is_cancelled", None)
    if callable(method):
        return bool(method())
    is_set = getattr(token, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(token):
        return bool(token())
    return bool(token)


__all__ = [
    "DeterministicTransport",
    "OfflineTransport",
    "OpenAICompatibleTransport",
    "ProviderTransportError",
    "TestTransport",
    "TransportError",
    "TransportRequest",
    "TransportResponse",
    "freeze_value",
    "thaw_value",
]
