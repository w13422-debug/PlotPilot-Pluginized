"""Deterministic, Core-only Prompt/Skill Runtime domain.

The package intentionally stops at immutable package identity, ordered chain
materialization and local history rules.  It does not import a Core database,
Provider, Job implementation, Candidate repository or a P1/P3 adapter.  Those
effects are represented by the explicit port gates in :mod:`ports` and are
owned by the integration layer.
"""

from .chain import (
    AssetRef,
    ChainAnchor,
    ChainExecution,
    PatchEvidence,
    SkillExecution,
    SkillStep,
    build_chain_result,
    build_receipt,
    execute_skill_chain,
    make_patch_evidence,
    replay_patches,
    run_skill_chain,
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
from .ports import PortGate, required_port_gates, verify_port_gates
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
    "ChainAnchor",
    "ChainExecution",
    "LegacyReadOnlyRecord",
    "PatchEvidence",
    "PortGate",
    "PromptPackage",
    "SkillExecution",
    "SkillIdentity",
    "SkillPackage",
    "SkillReleaseHistory",
    "SkillStep",
    "VersionDecision",
    "build_chain_result",
    "build_receipt",
    "calculate_skill_identity",
    "execute_skill_chain",
    "load_skill_package",
    "make_patch_evidence",
    "protect_active_version",
    "replay_patches",
    "required_port_gates",
    "run_skill_chain",
    "sync_active_version",
    "verify_chain",
    "verify_golden_skill",
    "verify_port_gates",
    "verify_receipt",
    "verify_skill_golden",
]


def main() -> None:
    """Reserved worker entrypoint; execution is wired by the real Host port."""

    raise RuntimeError(
        "Prompt/Skill Runtime has no standalone entrypoint; use Core Host ports"
    )
