"""Private, browser-only application facades.

These objects are composition inputs for the local WebUI.  Importing this
package never mounts routes or starts a worker process.
"""

from .private_generation_facade import (
    BindingAvailability,
    BindingUnavailable,
    ChapterGenerationRequest,
    PrivateChapterGenerationFacade,
    UnavailableChapterRunSnapshotBinding,
    VerifiedChapterRunSnapshotBinding,
)

__all__ = [
    "BindingAvailability",
    "BindingUnavailable",
    "ChapterGenerationRequest",
    "PrivateChapterGenerationFacade",
    "UnavailableChapterRunSnapshotBinding",
    "VerifiedChapterRunSnapshotBinding",
]
