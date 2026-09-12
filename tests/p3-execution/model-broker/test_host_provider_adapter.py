from __future__ import annotations

import dataclasses
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_model_broker import (
    PROVIDER_PACKAGE_HASH,
    PROVIDER_PLUGIN_ID,
    PROVIDER_RELEASE_ID,
    RAW_VALUE,
    FakeFactoryPort,
    FakeProviderPort,
    _adapter,
    _build_authority,
    _host_request,
)

from backend.plotpilot_core.bootstrap.host_provider_adapter import (
    HostProviderAdapterError,
    HostProviderPreparation,
    HostTransportError,
    HostVerifiedProviderExchange,
    HttpxChatCompletionsTransport,
    ProviderFactoryBinding,
    ProviderFactoryRequest,
    build_host_provider_adapter,
    project_chat_completions_options,
)
from backend.plotpilot_core.model import ModelBroker, ProviderInvocationExchange
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode


def _asset_inventory(stack) -> set[Path]:
    return {
        path.relative_to(stack.assets.root)
        for path in stack.assets.root.rglob("*")
        if path.is_file()
    }


def _ledger_count(stack) -> int:
    with stack.repository.read_connection() as connection:
        return int(
            connection.execute("SELECT count(*) FROM model_invocation").fetchone()[0]
        )


def _command(stack):
    broker = ModelBroker(stack.execution, None)
    request, meta, params = broker._host_parts(_host_request(stack))
    with stack.repository.transaction() as connection:
        authority = broker._current_authority(connection, request, meta, params)
    return broker, request, authority, authority.command(request), authority.reservation(request)


def _dispatch_without_opening(stack, adapter):
    broker, request, authority, command, reservation = _command(stack)
    preparation = adapter.prepare(command)
    provider_request = broker._bind_provider_request(
        authority, request, adapter.provider_request(preparation)
    )
    with stack.repository.transaction() as connection:
        broker.ledger.reserve(connection, reservation)
    with stack.repository.transaction() as connection:
        broker.ledger.mark_dispatching(connection, reservation, provider_request)
    sealed = adapter.invoke(preparation)
    return command, preparation, sealed


def test_broker_boundary_has_no_resolver_or_value_classifier_and_adapter_is_sole_caller():
    broker_path = Path("backend/plotpilot_core/model/broker.py")
    adapter_path = Path("backend/plotpilot_core/bootstrap/host_provider_adapter.py")
    broker_source = broker_path.read_text(encoding="utf-8")
    adapter_source = adapter_path.read_text(encoding="utf-8")

    for forbidden in (
        "ModelConfigurationAuthority",
        "resolve_secret_value",
        "resolved_value",
        "local_value",
        "local-value",
    ):
        assert forbidden not in broker_source
    production_callers = []
    for path in Path("backend/plotpilot_core").rglob("*.py"):
        count = path.read_text(encoding="utf-8").count(".resolve_secret_value(")
        production_callers.extend([path.as_posix()] * count)
    assert production_callers == [adapter_path.as_posix()]
    assert adapter_source.count(".resolve_secret_value(") == 1
    assert "sys.path" not in adapter_source
    assert "importlib" not in adapter_source


@pytest.mark.parametrize(
    "mode", ("missing", "provider_package_drift", "missing_verifier")
)
def test_missing_or_drifted_factory_fails_1001_before_t1_call_or_asset(
    tmp_path: Path, mode: str
) -> None:
    stack = _build_authority(tmp_path, f"factory-{mode}")
    port = FakeProviderPort(stack)
    factory = FakeFactoryPort(stack, port, mode=mode)
    adapter = _adapter(stack, port, factory=factory)
    before_assets = _asset_inventory(stack)
    try:
        with pytest.raises(ContractError) as rejected:
            ModelBroker(stack.execution, adapter).prepare_host_invocation(
                _host_request(stack)
            )
        assert rejected.value.code == int(ErrorCode.INCOMPATIBLE_GENERATION)
        assert _ledger_count(stack) == 0
        assert port.prepare_calls == port.invoke_calls == 0
        assert factory.factory_calls == 0
        assert _asset_inventory(stack) == before_assets
    finally:
        stack.close()


def test_factory_request_binds_exact_generation_provider_package_and_verifier(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "factory-exact")
    port = FakeProviderPort(stack)
    factory = FakeFactoryPort(stack, port)
    adapter = _adapter(stack, port, factory=factory)
    try:
        prepared = ModelBroker(stack.execution, adapter).prepare_host_invocation(
            _host_request(stack)
        )
        prepared.abort()
        assert factory.bind_calls == factory.factory_calls == 1
        assert len(factory.requests) == 1
        request = factory.requests[0]
        assert request.provider_plugin_id == PROVIDER_PLUGIN_ID
        assert request.provider_release_id == PROVIDER_RELEASE_ID
        assert request.provider_package_hash == PROVIDER_PACKAGE_HASH
        assert request.verifier_plugin_id == "com.plotpilot.prompt-skill-runtime"
        assert request.verifier_release_id == "c" * 64
        assert request.verifier_package_hash == "d" * 64
    finally:
        stack.close()


def test_resolver_runs_once_after_durable_t2_outside_transaction_then_provider_once(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "resolver-order")
    port = FakeProviderPort(stack)
    factory = FakeFactoryPort(stack, port)
    adapter = _adapter(stack, port, factory=factory)
    phases: list[str] = []
    resolver_states: list[tuple[bool, str]] = []
    original = stack.configuration.resolve_secret_value

    def resolve(reference: str) -> str:
        with stack.repository.read_connection() as connection:
            state = str(
                connection.execute(
                    "SELECT state FROM model_invocation"
                ).fetchone()[0]
            )
        resolver_states.append((stack.repository._connection.in_transaction, state))
        assert phases[-1] == "after_t2"
        return original(reference)

    stack.configuration.resolve_secret_value = resolve  # type: ignore[method-assign]
    before_metadata = len(list(stack.assets.metadata.glob("*.json")))
    try:
        prepared = ModelBroker(
            stack.execution,
            adapter,
            phase_hook=phases.append,
        ).prepare_host_invocation(_host_request(stack))
        prepared.commit()
        assert resolver_states == [(False, "dispatching")]
        assert port.prepare_calls == port.invoke_calls == 1
        assert port.transaction_states == [False]
        assert factory.bind_calls == factory.factory_calls == 1
        assert phases == [
            "after_t1",
            "after_t2",
            "after_provider",
            "after_response_asset",
            "after_receipt_asset",
            "before_t3",
            "after_t3",
        ]
        assert len(list(stack.assets.metadata.glob("*.json"))) == before_metadata + 2
    finally:
        stack.close()


def test_preparation_nonce_rejects_forged_cross_runtime_and_repeat_without_second_call(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "preparation-seal")
    port = FakeProviderPort(stack)
    first = _adapter(stack, port)
    second = _adapter(stack, port)
    broker, request, authority, command, reservation = _command(stack)
    preparation = first.prepare(command)
    provider_request = broker._bind_provider_request(
        authority, request, first.provider_request(preparation)
    )
    with stack.repository.transaction() as connection:
        broker.ledger.reserve(connection, reservation)
        broker.ledger.mark_dispatching(connection, reservation, provider_request)
    try:
        forged = object.__new__(HostProviderPreparation)
        with pytest.raises(HostProviderAdapterError):
            first.invoke(forged)
        with pytest.raises(HostProviderAdapterError):
            second.invoke(preparation)
        sealed = first.invoke(preparation)
        assert isinstance(sealed, HostVerifiedProviderExchange)
        with pytest.raises(HostProviderAdapterError):
            first.invoke(preparation)
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_verified_exchange_is_runtime_local_frozen_slotted_single_use_and_not_duck_typed(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "exchange-seal")
    port = FakeProviderPort(stack)
    first = _adapter(stack, port)
    second = _adapter(stack, port)
    before_assets = _asset_inventory(stack)
    try:
        _command_value, _preparation, sealed = _dispatch_without_opening(stack, first)
        assert isinstance(sealed, HostVerifiedProviderExchange)
        assert dataclasses.is_dataclass(sealed)
        assert type(sealed).__dataclass_params__.frozen
        assert isinstance(type(sealed).__slots__, tuple)
        with pytest.raises(dataclasses.FrozenInstanceError):
            sealed.digest = "0" * 64  # type: ignore[misc]
        with pytest.raises(TypeError):
            second.open_verified_exchange(sealed)
        with pytest.raises(TypeError):
            first.open_verified_exchange(SimpleNamespace(**{
                name: getattr(sealed, name) for name in type(sealed).__slots__
            }))
        with pytest.raises(TypeError):
            first.open_verified_exchange(object())
        exchange = first.open_verified_exchange(sealed)
        assert isinstance(exchange, ProviderInvocationExchange)
        assert callable(exchange.receipt_verifier)
        with pytest.raises(TypeError):
            first.open_verified_exchange(sealed)
        assert port.invoke_calls == 1
        assert _asset_inventory(stack) == before_assets
    finally:
        stack.close()


def test_verified_exchange_payload_replacement_is_rejected(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "exchange-replacement")
    port = FakeProviderPort(stack)
    adapter = _adapter(stack, port)
    try:
        _command_value, _preparation, sealed = _dispatch_without_opening(stack, adapter)
        replacement = ProviderInvocationExchange(
            {"jsonrpc": "2.0", "id": None, "error": {"code": 1001}},
            None,
            None,
        )
        object.__setattr__(sealed, "payload", replacement)
        with pytest.raises(TypeError, match="replaced"):
            adapter.open_verified_exchange(sealed)
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_chat_option_projection_keeps_timeout_and_retry_out_of_wire_body(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "option-projection")
    try:
        _broker, _request, _authority, command, _reservation = _command(stack)
        stream, parameters = project_chat_completions_options(command)
        assert stream is False
        assert dict(parameters) == {
            "temperature": 0.0,
            "top_p": 0.9,
            "max_tokens": 4096,
        }
        assert not {
            "timeout_seconds",
            "max_retries",
            "max_output_tokens",
            "max_completion_tokens",
        }.intersection(parameters)
    finally:
        stack.close()


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}
    content = b'{"choices":[]}'


class _RecordingClient:
    def __init__(self, owner, kwargs, *, failure: BaseException | None = None) -> None:
        self.owner = owner
        self.kwargs = kwargs
        self.failure = failure
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.owner.posts.append((url, kwargs))
        if self.failure is not None:
            raise self.failure
        return _Response()

    def close(self) -> None:
        self.closed = True


class _RecordingClientFactory:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls: list[dict[str, Any]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.clients: list[_RecordingClient] = []

    def __call__(self, **kwargs: Any) -> _RecordingClient:
        self.calls.append(kwargs)
        client = _RecordingClient(self, kwargs, failure=self.failure)
        self.clients.append(client)
        return client


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    (
        (
            "https://models.example.test/v1",
            "https://models.example.test/v1/chat/completions",
        ),
        (
            "https://models.example.test/v1/chat/completions",
            "https://models.example.test/v1/chat/completions",
        ),
    ),
)
def test_httpx_transport_is_one_direct_post_without_env_redirect_query_or_fallback(
    endpoint: str, expected: str
) -> None:
    clients = _RecordingClientFactory()
    transport = HttpxChatCompletionsTransport(37, client_factory=clients)
    request = SimpleNamespace(
        endpoint=endpoint,
        invocation_key="provider-call-1",
        body={"model": "planner-1", "messages": [], "max_tokens": 7},
        headers={"Authorization": "Bearer transient"},
    )

    assert transport.send(request).__class__ is _Response
    assert clients.calls == [
        {"trust_env": False, "follow_redirects": False, "timeout": 37}
    ]
    assert clients.posts == [
        (
            expected,
            {
                "json": {
                    "model": "planner-1",
                    "messages": [],
                    "max_tokens": 7,
                },
                "headers": {"Authorization": "Bearer transient"},
            },
        )
    ]
    assert clients.clients[0].closed
    assert transport.supports_query is False
    assert not hasattr(transport, "query")
    assert transport.send_state("provider-call-1") == "sent"
    assert transport.sent_keys == {"provider-call-1"}


def test_httpx_transport_does_not_retry_and_preserves_explicit_pre_send_classification():
    clients = _RecordingClientFactory(
        failure=HostTransportError(
            "fixture",
            send_state="not_sent",
            transient=True,
        )
    )
    transport = HttpxChatCompletionsTransport(11, client_factory=clients)
    request = SimpleNamespace(
        endpoint="https://models.example.test/v1",
        invocation_key="provider-call-failed",
        body={"model": "planner-1", "messages": []},
        headers={},
    )

    with pytest.raises(HostTransportError) as failed:
        transport.send(request)
    assert failed.value.send_state == "not_sent"
    assert failed.value.transient is True
    assert len(clients.calls) == len(clients.posts) == 1
    assert clients.clients[0].closed
    assert transport.sent_keys == set()


def test_builder_returns_no_adapter_without_factory_and_public_types_are_closed(
    tmp_path: Path,
) -> None:
    assert inspect.signature(build_host_provider_adapter).parameters[
        "factory_port"
    ].default is inspect.Parameter.empty
    annotations = ProviderFactoryRequest.__annotations__
    assert tuple(annotations) == (
        "generation_id",
        "provider_plugin_id",
        "provider_release_id",
        "provider_package_hash",
        "verifier_plugin_id",
        "verifier_release_id",
        "verifier_package_hash",
    )
    assert dataclasses.is_dataclass(ProviderFactoryBinding)
    stack = _build_authority(tmp_path, "builder-fail-closed")
    try:
        assert build_host_provider_adapter(stack.configuration, None) is None
    finally:
        stack.close()
