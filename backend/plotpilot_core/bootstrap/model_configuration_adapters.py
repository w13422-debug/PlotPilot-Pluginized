"""Composition root for the Core model-configuration authority slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..api.v2.configuration import (
    ConfigurationHttpAdapter,
    build_configuration_router,
)
from ..configuration import ModelConfigurationAuthority, WorkspacePlanAuthority
from ..repositories.authority import CoreAuthorityRepository


@dataclass(frozen=True, slots=True)
class ModelConfigurationAdapters:
    """One repository-bound graph for local values, profiles and Plans."""

    configuration: ModelConfigurationAuthority
    planning: WorkspacePlanAuthority
    http: ConfigurationHttpAdapter

    @property
    def repository(self) -> CoreAuthorityRepository:
        return self.configuration.repository

    @property
    def plan_registrar(self) -> WorkspacePlanAuthority:
        return self.planning

    def router(self) -> Any:
        return build_configuration_router(self.http)


def build_model_configuration_adapters(
    repository: CoreAuthorityRepository,
) -> ModelConfigurationAdapters:
    """Compose P1 on the exact existing Core transaction owner."""

    repository.ensure_model_configuration_schema()
    configuration = ModelConfigurationAuthority(repository)
    planning = WorkspacePlanAuthority(repository)
    http = ConfigurationHttpAdapter(configuration, planning)
    return ModelConfigurationAdapters(configuration, planning, http)


build_configuration_adapters = build_model_configuration_adapters


__all__ = [
    "ModelConfigurationAdapters",
    "build_configuration_adapters",
    "build_model_configuration_adapters",
]
