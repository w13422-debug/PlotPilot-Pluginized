"""Durable plugin lifecycle primitives."""

from .coordinator import InstallLifecycleService
from .plans import PlanSwitchIntent, prepare_plan_switch
from .repository import (
    EventWriter,
    GenerationGuard,
    LifecycleError,
    LifecycleRepository,
    PinReleaser,
    QualificationVerifier,
    ReconciliationDecision,
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
)
from .shadow import ShadowGenerationManager, ShadowLease

__all__ = [
    "EventWriter",
    "GenerationGuard",
    "InstallLifecycleService",
    "LifecycleError",
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
    "ShadowGenerationManager",
    "ShadowLease",
    "VerifiedQualification",
    "initial_transition",
    "prepare_plan_switch",
    "require_verified_qualification",
    "utc_now",
    "validate_transition",
]
