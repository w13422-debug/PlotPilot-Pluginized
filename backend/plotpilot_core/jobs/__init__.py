"""Durable execution primitives for PlotPilot jobs."""
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode

from .ledger import AuthoritativeConnection, DurableOperationLedger
from .ports import JobAuthorityPort, resolve_request

_PRODUCTION_EXPORTS = frozenset(
    {
        "ProductionAttemptRegistry",
        "ProductionCapabilityRuntime",
        "ProductionCapabilitySelection",
        "ProductionIngressGate",
        "ProductionJobControlResolver",
        "ProductionJobStartResolver",
        "ProductionProvenanceReceiptResolver",
        "ProductionStreamCommitPolicyResolver",
        "ProductionWorkerLauncher",
        "build_production_capability_broker",
    }
)


def __getattr__(name: str):
    if name not in _PRODUCTION_EXPORTS:
        raise AttributeError(name)
    from . import production

    return getattr(production, name)

__all__ = [
    "AuthoritativeConnection",
    "ContractError",
    "DurableOperationLedger",
    "ErrorCode",
    "JobAuthorityPort",
    "ProductionAttemptRegistry",
    "ProductionCapabilityRuntime",
    "ProductionCapabilitySelection",
    "ProductionIngressGate",
    "ProductionJobControlResolver",
    "ProductionJobStartResolver",
    "ProductionProvenanceReceiptResolver",
    "ProductionStreamCommitPolicyResolver",
    "ProductionWorkerLauncher",
    "build_production_capability_broker",
    "resolve_request",
]
