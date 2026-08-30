"""Application-facing Job command/query adapters.

The shared ``plotpilot_core.jobs`` package remains the durable authority
boundary.  These adapters only delegate commands to the accepted
``ExecutionAuthority`` and project committed rows through its repository.
"""

from .command_query import (
    AttemptStartBinding,
    AttemptStartPort,
    JobCheckpointBinding,
    JobCommandQueryAdapter,
    JobCommandResult,
    JobSnapshotExtensionReader,
    JobSnapshotExtensions,
    JobStreamHighWaterBinding,
)

__all__ = [
    "AttemptStartBinding",
    "AttemptStartPort",
    "JobCheckpointBinding",
    "JobCommandQueryAdapter",
    "JobCommandResult",
    "JobSnapshotExtensionReader",
    "JobSnapshotExtensions",
    "JobStreamHighWaterBinding",
]
