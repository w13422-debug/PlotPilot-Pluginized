"""Application-facing Job command/query adapters.

The shared ``plotpilot_core.jobs`` package remains the durable authority
boundary.  These adapters only delegate commands to the accepted
``ExecutionAuthority`` and project committed rows through its repository.
"""

from .command_query import (
    JobCommandQueryAdapter,
    JobCommandResult,
    JobSnapshotExtensionReader,
    JobSnapshotExtensions,
)

__all__ = [
    "JobCommandQueryAdapter",
    "JobCommandResult",
    "JobSnapshotExtensionReader",
    "JobSnapshotExtensions",
]
