"""SDK-only OpenAI-compatible Provider package.

The package exposes the provider domain objects and the transport boundary,
but intentionally does not create a transport.  Hosts must inject a
transport explicitly; tests can use :class:`DeterministicTransport` without
network access.
"""

from .provider import (
    CancellationToken,
    CompletionChunk,
    CompletionResult,
    InvalidProfileError,
    Invocation,
    InvocationReceipt,
    InvocationResult,
    MalformedResponseError,
    ModelReceipt,
    OpenAICompatibleModelProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderError,
    ProviderInvocation,
    ProviderInvocationResult,
    ProviderReceipt,
    ProviderResult,
    ProviderStreamChunk,
    ProviderTransport,
    Receipt,
    SecretProvider,
    StreamChunk,
    UnsupportedReplayError,
)
from .transport import (
    DeterministicTransport,
    OfflineTransport,
    OpenAICompatibleTransport,
    ProviderTransportError,
    TestTransport,
    TransportError,
    TransportRequest,
    TransportResponse,
)


__all__ = [
    "CancellationToken",
    "CompletionChunk",
    "CompletionResult",
    "DeterministicTransport",
    "InvalidProfileError",
    "Invocation",
    "InvocationReceipt",
    "InvocationResult",
    "MalformedResponseError",
    "ModelReceipt",
    "OfflineTransport",
    "OpenAICompatibleModelProvider",
    "OpenAICompatibleProvider",
    "OpenAICompatibleTransport",
    "OpenAIProvider",
    "ProviderError",
    "ProviderInvocation",
    "ProviderInvocationResult",
    "ProviderReceipt",
    "ProviderResult",
    "ProviderStreamChunk",
    "ProviderTransport",
    "ProviderTransportError",
    "Receipt",
    "SecretProvider",
    "StreamChunk",
    "TestTransport",
    "TransportError",
    "TransportRequest",
    "TransportResponse",
    "UnsupportedReplayError",
]
