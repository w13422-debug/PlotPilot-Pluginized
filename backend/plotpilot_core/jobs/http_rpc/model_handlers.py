"""Attempt-scoped Host adapter for the durable Core Model Broker."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from backend.plotpilot_core.model.broker import ModelBroker
from backend.plotpilot_core.model.invocation import HOST_MODEL_METHOD
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    parse_json_bytes,
)

from .dispatcher import HostRpcHandler, PreparedHostRpcResult

MODEL_HOST_METHODS = (HOST_MODEL_METHOD,)


class ModelHostHandler:
    def __init__(self, broker: ModelBroker) -> None:
        if not isinstance(broker, ModelBroker):
            raise TypeError("model Host handler requires ModelBroker")
        self.broker = broker

    def __call__(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        prepared = self.broker.prepare_host_invocation(request)
        expected = parse_json_bytes(canonical_bytes(dict(prepared.result)))
        result = parse_json_bytes(canonical_bytes(dict(prepared.result)))
        if not isinstance(expected, dict) or not isinstance(result, dict):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Model Broker Host result is not an object",
            )

        def commit() -> None:
            if canonical_bytes(result) != canonical_bytes(expected):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Model Broker Host result drifted before ACK",
                )
            prepared.commit()

        return PreparedHostRpcResult(
            MappingProxyType(result),
            commit,
            prepared.abort,
        )


def build_model_host_handlers(
    broker: ModelBroker,
) -> Mapping[str, HostRpcHandler]:
    return MappingProxyType({HOST_MODEL_METHOD: ModelHostHandler(broker)})


__all__ = [
    "MODEL_HOST_METHODS",
    "ModelHostHandler",
    "build_model_host_handlers",
]
