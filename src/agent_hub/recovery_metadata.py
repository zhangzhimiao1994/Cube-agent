"""Shared safe metadata enums for self-repair classification and retry context."""

from __future__ import annotations

from types import MappingProxyType

RECOVERY_STRATEGY_BY_FAILURE_CATEGORY = MappingProxyType(
    {
        "capacity_pressure": "switch_to_available_model_and_retry",
        "model_capability_routing_unavailable": "reassign_tool_role_to_capable_model_and_retry",
        "model_credential_unavailable": "manual_review_model_credentials",
        "model_quota_or_billing_unavailable": "manual_review_model_quota_or_billing",
        "model_deployment_unavailable": "manual_review_model_deployment",
        "model_request_contract_invalid": "manual_review_model_request_contract",
        "plugin_runtime_unavailable": "repair_plugin_endpoint_or_adapter_and_retry",
        "plugin_adapter_unavailable": "manual_review_plugin_adapter",
        "plugin_credential_unavailable": "manual_review_plugin_credentials",
        "plugin_invalid_arguments": "manual_review_plugin_arguments",
        "plugin_invalid_result": "manual_review_plugin_result_contract",
        "plugin_sandbox_unsupported": "manual_review_plugin_sandbox",
        "mcp_runtime_unavailable": "repair_mcp_server_or_adapter_and_retry",
        "mcp_tool_unavailable": "manual_review_mcp_configuration",
        "mcp_server_not_discovered": "manual_review_mcp_configuration",
        "empty_model_response": "retry_with_fallback_or_reassign_model",
        "runtime_recovery_blocked": "manual_review_recovery_checkpoint",
        "runtime_failure": "preserve_outputs_and_retry_scope",
        "step_failure": "retry_failed_step_after_context_compaction",
        "tool_failure": "repair_tool_invocation_after_permission_check",
        "missing_failure_event": "manual_review_missing_failure_event",
    }
)
ORCHESTRATION_CONTRACT_RECOVERY_HINT = "retry_blocked_contract_chain"
ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY = "retry_blocked_contract_chain_after_replanning"
RECOVERY_STRATEGY_BY_ORCHESTRATION_RECOVERY_HINT = MappingProxyType(
    {
        ORCHESTRATION_CONTRACT_RECOVERY_HINT: ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY,
    }
)
SAFE_OBSERVER_RECOMMENDATIONS = frozenset(
    {
        "switch_to_available_model_and_retry",
        "retry_with_fallback_or_reassign_model",
        "pause_for_scheduler_review",
        "preserve_outputs_and_retry_scope",
        "reassign_tool_role_to_capable_model_and_retry",
        "repair_plugin_endpoint_or_adapter_and_retry",
        "repair_mcp_server_or_adapter_and_retry",
        "watch_retry_budget_before_requeue",
        "compact_context_before_next_model_call",
    }
)
SAFE_SELF_REPAIR_RECOVERY_STRATEGIES = frozenset(
    (
        *RECOVERY_STRATEGY_BY_FAILURE_CATEGORY.values(),
        *RECOVERY_STRATEGY_BY_ORCHESTRATION_RECOVERY_HINT.values(),
        *SAFE_OBSERVER_RECOMMENDATIONS,
    )
)
SAFE_SELF_REPAIR_FAILURE_KINDS = frozenset(RECOVERY_STRATEGY_BY_FAILURE_CATEGORY)
SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS = frozenset(
    {ORCHESTRATION_CONTRACT_RECOVERY_HINT}
)

__all__ = [
    "ORCHESTRATION_CONTRACT_RECOVERY_HINT",
    "ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY",
    "RECOVERY_STRATEGY_BY_FAILURE_CATEGORY",
    "RECOVERY_STRATEGY_BY_ORCHESTRATION_RECOVERY_HINT",
    "SAFE_OBSERVER_RECOMMENDATIONS",
    "SAFE_SELF_REPAIR_FAILURE_KINDS",
    "SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS",
    "SAFE_SELF_REPAIR_RECOVERY_STRATEGIES",
]
