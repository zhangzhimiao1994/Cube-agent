"""Discussion-mode backend selection.

Discuss remains the user-facing mode name.  Backend names are implementation
details: MAF is preferred, AG2 is supported, and AutoGen is legacy compatibility.
"""

from agent_hub.runtime.discussion.registry import (
    CallableDiscussionRuntimeProvider,
    DiscussionBackend,
    DiscussionBackendSelection,
    DiscussionBackendUnavailable,
    DiscussionRuntimeProvider,
    DiscussionRuntimeRegistry,
)

__all__ = [
    "CallableDiscussionRuntimeProvider",
    "DiscussionBackend",
    "DiscussionBackendSelection",
    "DiscussionBackendUnavailable",
    "DiscussionRuntimeProvider",
    "DiscussionRuntimeRegistry",
]
