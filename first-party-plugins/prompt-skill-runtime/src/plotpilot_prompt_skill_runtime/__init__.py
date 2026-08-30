"""Deterministic Prompt/Skill Runtime with explicit Core/Broker authority."""

from .attribution import (
    AttributionProof,
    FrozenModelInvocation,
    ValidatedModelReceipt,
    decode_model_receipt,
    validate_model_receipt_binding,
    verify_model_receipt_asset,
)
from .chain import (
    AssetRef,
    ChainAnchor,
    ChainExecution,
    PatchEvidence,
    SkillExecution,
    SkillStep,
    build_chain_result,
    build_receipt,
    make_patch_evidence,
    replay_patches,
    verify_chain,
    verify_receipt,
)
from .package import (
    PromptPackage,
    SkillIdentity,
    SkillPackage,
    calculate_skill_identity,
    load_skill_package,
    verify_golden_skill,
    verify_skill_golden,
)
from .persistence import RepositoryTransaction, SQLiteSkillRepository
from .ports import (
    GenerationPort,
    PortGate,
    SkillBrokerPort,
    required_port_gates,
    verify_port_gates,
)
from .runtime import BrokerSkillResult, PreparedSkillRun, PromptSkillRuntime
from .versions import (
    ActiveVersion,
    LegacyReadOnlyRecord,
    SkillReleaseHistory,
    VersionDecision,
    protect_active_version,
    sync_active_version,
)

__all__ = [
    "ActiveVersion",
    "AssetRef",
    "AttributionProof",
    "BrokerSkillResult",
    "ChainAnchor",
    "ChainExecution",
    "FrozenModelInvocation",
    "GenerationPort",
    "LegacyReadOnlyRecord",
    "PatchEvidence",
    "PortGate",
    "PreparedSkillRun",
    "PromptPackage",
    "PromptSkillRuntime",
    "RepositoryTransaction",
    "SQLiteSkillRepository",
    "SkillBrokerPort",
    "SkillExecution",
    "SkillIdentity",
    "SkillPackage",
    "SkillReleaseHistory",
    "SkillStep",
    "ValidatedModelReceipt",
    "VersionDecision",
    "build_chain_result",
    "build_receipt",
    "calculate_skill_identity",
    "decode_model_receipt",
    "load_skill_package",
    "make_patch_evidence",
    "protect_active_version",
    "replay_patches",
    "required_port_gates",
    "sync_active_version",
    "validate_model_receipt_binding",
    "verify_chain",
    "verify_golden_skill",
    "verify_model_receipt_asset",
    "verify_port_gates",
    "verify_receipt",
    "verify_skill_golden",
]


def main() -> None:
    raise RuntimeError("Prompt/Skill Runtime has no standalone entrypoint; use Core Host ports")
