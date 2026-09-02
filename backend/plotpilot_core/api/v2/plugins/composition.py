"""Small P2-owned lifecycle composition seam for the P0 app owner."""
from __future__ import annotations

from dataclasses import dataclass

from plotpilot_core.supervisor.job_control import JobControl, ProcessSupervisorPort

from .ports import PluginLifecycleFacade
from .router import create_lifecycle_router


@dataclass(frozen=True, slots=True)
class LifecycleRuntimeComposition:
    """The unmounted router plus its process-only request controller."""

    jobs: JobControl
    facade: PluginLifecycleFacade

    def router(self):
        return create_lifecycle_router(self.facade)


def compose_lifecycle_runtime(supervisor: ProcessSupervisorPort) -> LifecycleRuntimeComposition:
    """Construct a local slice without mounting routes or selecting releases."""

    jobs = JobControl(supervisor)
    return LifecycleRuntimeComposition(jobs=jobs, facade=PluginLifecycleFacade(jobs))


__all__ = ["LifecycleRuntimeComposition", "compose_lifecycle_runtime"]
