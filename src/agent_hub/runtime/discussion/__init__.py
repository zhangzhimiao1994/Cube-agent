"""Discussion-mode backend selection.

Discuss remains the user-facing mode name.  Backend names are implementation
details: MAF is preferred, AG2 is supported, and AutoGen is legacy compatibility.
"""

from agent_hub.runtime.discussion.decision import (
    AgentPosition,
    DecisionCriterion,
    DecisionResolver,
    DecisionStatus,
    DisagreementKind,
    ResolutionRequest,
    ResolutionResult,
)
from agent_hub.runtime.discussion.registry import (
    CallableDiscussionRuntimeProvider,
    DiscussionBackend,
    DiscussionBackendSelection,
    DiscussionBackendUnavailable,
    DiscussionRuntimeProvider,
    DiscussionRuntimeRegistry,
)

__all__ = [
    "AgentPosition",
    "CallableDiscussionRuntimeProvider",
    "DecisionCriterion",
    "DecisionResolver",
    "DecisionStatus",
    "DisagreementKind",
    "DiscussionBackend",
    "DiscussionBackendSelection",
    "DiscussionBackendUnavailable",
    "DiscussionRuntimeProvider",
    "DiscussionRuntimeRegistry",
    "ResolutionRequest",
    "ResolutionResult",
]
