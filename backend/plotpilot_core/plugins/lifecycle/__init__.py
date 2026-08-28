"""Durable plugin lifecycle primitives."""

from .coordinator import InstallLifecycleService
from .plans import PlanSwitchIntent, prepare_plan_switch
from .repository import (
    LIFECYCLE_MIGRATIONS,
    EventWriter,
    GenerationGuard,
    LifecycleError,
    LifecycleMigration,
    LifecycleRepository,
    PinReleaser,
    QualificationVerifier,
    ReconciliationDecision,
    TransactionFactory,
    VerifiedQualification,
    initial_transition,
    require_verified_qualification,
    utc_now,
    validate_transition,
)
from .retirement import (
    PackageRemover,
    PublicationBarrier,
    PublicationBarrierDecision,
    RetirementEventWriter,
    RetirementManager,
    RetirementOperationAuthority,
)
from .shadow import ShadowGenerationManager, ShadowLease

__all__ = [
    "LIFECYCLE_MIGRATIONS",
    "EventWriter",
    "GenerationGuard",
    "InstallLifecycleService",
    "LifecycleError",
    "LifecycleMigration",
    "LifecycleRepository",
    "PackageRemover",
    "PinReleaser",
    "PlanSwitchIntent",
    "PublicationBarrier",
    "PublicationBarrierDecision",
    "QualificationVerifier",
    "ReconciliationDecision",
    "RetirementEventWriter",
    "RetirementManager",
    "RetirementOperationAuthority",
    "ShadowGenerationManager",
    "ShadowLease",
    "TransactionFactory",
    "VerifiedQualification",
    "initial_transition",
    "prepare_plan_switch",
    "require_verified_qualification",
    "utc_now",
    "validate_transition",
]
