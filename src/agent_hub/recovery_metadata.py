"""Shared safe metadata enums for self-repair classification and retry context."""

from __future__ import annotations

from types import MappingProxyType

RECOVERY_STRATEGY_BY_FAILURE_CATEGORY = MappingProxyType(
    {
        "capacity_pressure": "switch_to_available_model_and_retry",
        "model_capability_routing_unavailable": "reassign_tool_role_to_capable_model_and_retry",
        "empty_model_response": "retry_with_fallback_or_reassign_model",
        "runtime_failure": "preserve_outputs_and_retry_scope",
        "step_failure": "retry_failed_step_after_context_compaction",
        "tool_failure": "repair_tool_invocation_after_permission_check",
        "missing_failure_event": "manual_review_missing_failure_event",
    }
)
SAFE_OBSERVER_RECOMMENDATIONS = frozenset(
    {
        "switch_to_available_model_and_retry",
        "retry_with_fallback_or_reassign_model",
        "pause_for_scheduler_review",
        "preserve_outputs_and_retry_scope",
        "reassign_tool_role_to_capable_model_and_retry",
        "watch_retry_budget_before_requeue",
        "compact_context_before_next_model_call",
    }
)
SAFE_SELF_REPAIR_RECOVERY_STRATEGIES = frozenset(
    (*RECOVERY_STRATEGY_BY_FAILURE_CATEGORY.values(), *SAFE_OBSERVER_RECOMMENDATIONS)
)
SAFE_SELF_REPAIR_FAILURE_KINDS = frozenset(RECOVERY_STRATEGY_BY_FAILURE_CATEGORY)
SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS = frozenset({"retry_blocked_contract_chain"})

__all__ = [
    "RECOVERY_STRATEGY_BY_FAILURE_CATEGORY",
    "SAFE_OBSERVER_RECOMMENDATIONS",
    "SAFE_SELF_REPAIR_FAILURE_KINDS",
    "SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS",
    "SAFE_SELF_REPAIR_RECOVERY_STRATEGIES",
]
