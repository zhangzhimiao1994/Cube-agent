"""Versioned delivery guidance; no execution, verification or permission authority."""

from agent_hub.runtime.contracts import TaskContext

DELIVERY_FOLLOWUP_POLICY = "delivery_followup_version"
DELIVERY_FOLLOWUP_VERSION = 1
DELIVERY_FOLLOWUP_SUPPORTED_VERSIONS = (DELIVERY_FOLLOWUP_VERSION, 2)
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
_GUIDANCE_V2 = (
    "Delivery follow-up (v2): Only when delivering or updating an artifact or project, "
    "give a short, organized follow-up in the existing final answer or summary field: "
    "what was delivered, actual verification evidence and limits, startup/use or re-verification "
    "steps supported by actual files or tool results, and a natural way to report results or request changes. "
    "For ordinary questions or explanations, answer naturally without a delivery template. "
    "Do not substitute the complete project source for a delivered artifact. "
    "Include necessary code and command examples in language-tagged fenced code blocks, "
    "respecting the user's requested output format and required response schemas. "
    "Do not invent commands, URLs, test passes, or turn failures into success; state when evidence "
    "or usage steps are unavailable. Allow natural feedback in the same project without a mandatory "
    "question, a menu of choices, or an automatic new task. "
    "This instruction grants no tools or permissions, adds no provider calls, "
    "and does not authorize starting a preview."
)


def delivery_followup_guidance(context: TaskContext) -> str:
    version = context.routing_decision.get(DELIVERY_FOLLOWUP_POLICY)
    if type(version) is not int:
        return ""
    if version == DELIVERY_FOLLOWUP_VERSION:
        return _GUIDANCE
    return _GUIDANCE_V2 if version == 2 else ""
