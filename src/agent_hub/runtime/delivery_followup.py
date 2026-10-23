"""Versioned delivery guidance; no execution, verification or permission authority."""

from agent_hub.runtime.contracts import TaskContext

DELIVERY_FOLLOWUP_POLICY = "delivery_followup_version"
DELIVERY_FOLLOWUP_VERSION = 1
_GUIDANCE = (
    "Delivery follow-up (v1): In the existing final answer or summary field, include one short, "
    "natural paragraph stating what was delivered and the limits of the actual verification evidence. "
    "Give startup/use or re-verification steps only when supported by the delivered files or observed "
    "tool results; otherwise state that the steps are not yet available. "
    "Do not invent commands, URLs, test passes, or turn failures into success. "
    "Keep required response schemas unchanged and do not paste source code. "
    "Leave the user a natural way to report results or request changes in the same project, "
    "without a mandatory question, a menu of choices, or an automatic new task. "
    "This instruction grants no tools or permissions, adds no provider calls, "
    "and does not authorize starting a preview."
)


def delivery_followup_guidance(context: TaskContext) -> str:
    version = context.routing_decision.get(DELIVERY_FOLLOWUP_POLICY)
    return _GUIDANCE if type(version) is int and version == DELIVERY_FOLLOWUP_VERSION else ""
