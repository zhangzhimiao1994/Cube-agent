"""Legacy AutoGen AgentChat discussion adapter.

AutoGen is retained for explicit compatibility only.  Default Discuss runtime
selection goes through :mod:`agent_hub.runtime.discussion` and prefers MAF/AG2.
"""

from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)

AutoGenLegacyDiscussionRuntime = AutoGenDiscussionRuntime

__all__ = [
    "AutoGenDiscussionRuntime",
    "AutoGenLegacyDiscussionRuntime",
    "DiscussionParticipant",
    "DiscussionPlan",
]
