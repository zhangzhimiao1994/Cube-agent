from __future__ import annotations

from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.recovery_metadata import (
    ORCHESTRATION_CONTRACT_RECOVERY_HINT,
    SAFE_SELF_REPAIR_FAILURE_KINDS,
    SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS,
    SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
)
from agent_hub.runs.self_repair import (
    SelfRepairPolicy,
    classify_terminal_run,
    repair_context_from_proposal,
    repair_proposal_projection,
)
from agent_hub.runs.service import _self_repair_execution_payload
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import RECOVERY_BLOCKED_FAILURE_REASON
from agent_hub.runtime.self_repair_context import (
    self_repair_context_text,
    self_repair_recovery_plan_payload,
)


def test_orchestration_contract_recovery_hint_is_safe_for_self_repair() -> None:
    assert ORCHESTRATION_CONTRACT_RECOVERY_HINT in SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS


def test_safe_recovery_metadata_allowlists_stay_consistent_across_boundaries() -> None:
    for recovery_strategy in SAFE_SELF_REPAIR_RECOVERY_STRATEGIES:
        proposal = {
            "kind": "self_repair",
            "failure_kind": "runtime_failure",
            "source_run_id": "run_1",
            "source_event_sequence": 1,
            "attempt": 1,
            "max_attempts": 1,
            "fingerprint": "fp",
            "recovery_strategy": recovery_strategy,
        }

        repair_context = repair_context_from_proposal(proposal)
        assert repair_context["recovery_strategy"] == recovery_strategy
        routing_decision = {"source": "self_repair", "self_repair_context": repair_context}
        assert recovery_strategy in self_repair_context_text(routing_decision)
        audit_payload = _self_repair_execution_payload(
            routing_decision,
            status=RunStatus.RUNNING,
        )
        assert audit_payload["recovery_strategy"] == recovery_strategy

    for recovery_hint in SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS:
        repair_context = repair_context_from_proposal(
            {
                "kind": "self_repair",
                "failure_kind": "runtime_failure",
                "orchestration_recovery_hint": recovery_hint,
            }
        )
        routing_decision = {"source": "self_repair", "self_repair_context": repair_context}
        assert recovery_hint in self_repair_context_text(routing_decision)
        audit_payload = _self_repair_execution_payload(
            routing_decision,
            status=RunStatus.RUNNING,
        )
        assert audit_payload["orchestration_recovery_hint"] == recovery_hint

    for failure_kind in SAFE_SELF_REPAIR_FAILURE_KINDS:
        routing_decision = {
            "source": "self_repair",
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": failure_kind,
            },
        }
        assert failure_kind in self_repair_context_text(routing_decision)
        audit_payload = _self_repair_execution_payload(
            routing_decision,
            status=RunStatus.RUNNING,
        )
        assert audit_payload["failure_kind"] == failure_kind


def test_blocked_contract_ids_stay_internal_bounded_and_prompt_visible() -> None:
    repair_context = repair_context_from_proposal(
        {
            "kind": "self_repair",
            "failure_kind": "step_failure",
            "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
            "orchestration_recovery_hint": "retry_blocked_contract_chain",
            "blocked_contract_ids": (
                "draft-to-final_response",
                "draft-to-final_response",
                "unsafe secret://token",
                "x" * 97,
                "research-to-final_response",
            ),
        }
    )
    assert repair_context["blocked_contract_ids"] == (
        "draft-to-final_response",
        "research-to-final_response",
    )

    routing_decision = {"source": "self_repair", "self_repair_context": repair_context}
    prompt_context = self_repair_context_text(routing_decision)
    assert "draft-to-final_response" in prompt_context
    assert "research-to-final_response" in prompt_context
    assert "secret://token" not in prompt_context

    recovery_plan = self_repair_recovery_plan_payload(routing_decision)
    assert recovery_plan is not None
    assert recovery_plan["blocked_contract_ids"] == (
        "draft-to-final_response",
        "research-to-final_response",
    )

    audit_payload = _self_repair_execution_payload(
        routing_decision,
        status=RunStatus.RUNNING,
    )
    assert audit_payload["blocked_contract_ids"] == (
        "draft-to-final_response",
        "research-to-final_response",
    )
    assert "secret://token" not in repr(audit_payload)


def test_blocked_contract_ids_are_hidden_without_contract_recovery_gate() -> None:
    repair_context = repair_context_from_proposal(
        {
            "kind": "self_repair",
            "failure_kind": "runtime_failure",
            "recovery_strategy": "switch_to_available_model_and_retry",
            "orchestration_recovery_hint": "retry_blocked_contract_chain",
            "blocked_contract_ids": ("draft-to-final_response",),
        }
    )
    routing_decision = {"source": "self_repair", "self_repair_context": repair_context}

    assert repair_context["blocked_contract_ids"] == ("draft-to-final_response",)
    assert "draft-to-final_response" not in self_repair_context_text(routing_decision)
    assert self_repair_recovery_plan_payload(routing_decision) is None


def test_inferred_blocked_contract_ids_drive_recovery_plan_payload() -> None:
    repair_context = repair_context_from_proposal(
        {
            "kind": "self_repair",
            "failure_kind": "step_failure",
            "source_run_id": "run_1",
            "source_event_sequence": 2,
            "attempt": 1,
            "max_attempts": 1,
            "fingerprint": "fp",
            "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
            "orchestration_recovery_hint": "retry_blocked_contract_chain",
            "blocked_contract_ids": ("draft-to-final_response",),
        }
    )
    routing_decision = {"source": "self_repair", "self_repair_context": repair_context}

    recovery_plan = self_repair_recovery_plan_payload(routing_decision)

    assert recovery_plan is not None
    assert recovery_plan["retry_blocked_contracts_only"] is True
    assert recovery_plan["blocked_contract_ids"] == ("draft-to-final_response",)


def test_model_capability_reassignment_metadata_is_bounded_and_structured() -> None:
    repair_context = repair_context_from_proposal(
        {
            "kind": "self_repair",
            "failure_kind": "model_capability_routing_unavailable",
            "source_run_id": "run_1",
            "source_event_sequence": 2,
            "attempt": 1,
            "max_attempts": 1,
            "fingerprint": "fp",
            "recovery_strategy": "reassign_tool_role_to_capable_model_and_retry",
            "role_capability_requirements": (
                {
                    "role_id": "scheduler",
                    "required_capabilities": (
                        "text",
                        "structured_output",
                        "tool_calling",
                        "secret://token",
                    ),
                },
                {
                    "role_id": "../unsafe",
                    "required_capabilities": ("tool_calling",),
                },
            ),
        }
    )
    assert repair_context["role_capability_requirements"] == (
        {
            "role_id": "scheduler",
            "required_capabilities": (
                "text",
                "structured_output",
                "tool_calling",
            ),
        },
    )

    routing_decision = {"source": "self_repair", "self_repair_context": repair_context}
    recovery_plan = self_repair_recovery_plan_payload(routing_decision)

    assert recovery_plan is not None
    assert recovery_plan["replan_scope"] == "model_capability_roles"
    assert recovery_plan["role_capability_requirements"] == (
        {
            "role_id": "scheduler",
            "required_capabilities": (
                "text",
                "structured_output",
                "tool_calling",
            ),
        },
    )
    assert "secret://token" not in repr(recovery_plan)


def test_policy_without_approval_marks_repair_as_automatic_execution() -> None:
    run_id = uuid4()
    decision = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=TaskMode.DISPATCH,
        routing_decision={"source": "manual"},
        events=(
            RunEvent(
                kind=EventKind.STEP_FAILED,
                sequence=1,
                run_id=run_id,
                step_id="final_response",
                actor="final_response",
                reason="blocked contract chain needs one bounded replay",
                payload={
                    "orchestration_recovery_hint": "retry_blocked_contract_chain",
                    "blocked_contract_ids": ("draft-to-final_response",),
                },
            ),
        ),
        policy=SelfRepairPolicy(requires_approval=False),
    )

    assert decision is not None
    assert decision.requires_approval is False
    assert decision.automatic_execution is True
    proposal = decision.to_proposal(run_id=run_id)
    assert proposal is not None
    assert proposal["requires_approval"] is False
    assert proposal["automatic_execution"] is True

    repair_context = repair_context_from_proposal(proposal)
    assert repair_context["requires_approval"] is False
    assert repair_context["automatic_execution"] is True
    routing_decision = {"source": "self_repair", "self_repair_context": repair_context}
    recovery_plan = self_repair_recovery_plan_payload(routing_decision)
    assert recovery_plan is not None
    assert recovery_plan["replan_scope"] == "blocked_contract_chain"
    assert recovery_plan["automatic_execution"] is True
    audit_payload = _self_repair_execution_payload(
        routing_decision,
        status=RunStatus.RUNNING,
    )
    assert audit_payload["requires_approval"] is False
    assert audit_payload["automatic_execution"] is True


def test_repair_projection_rejects_spoofed_automatic_execution_with_approval() -> None:
    projected = repair_proposal_projection(
        {
            "kind": "self_repair",
            "requires_approval": True,
            "automatic_execution": True,
        }
    )

    assert projected is not None
    assert projected["requires_approval"] is True
    assert projected["automatic_execution"] is False


@pytest.mark.parametrize(
    ("reason", "expected_category", "expected_strategy"),
    [
        (
            "model.provider_auth_failed secret://model-token",
            "model_credential_unavailable",
            "manual_review_model_credentials",
        ),
        (
            "model credential resolution failed secret://model-token",
            "model_credential_unavailable",
            "manual_review_model_credentials",
        ),
        (
            "model.provider_quota_or_billing_failed secret://model-token",
            "model_quota_or_billing_unavailable",
            "manual_review_model_quota_or_billing",
        ),
        (
            "model.provider_model_not_found secret://model-token",
            "model_deployment_unavailable",
            "manual_review_model_deployment",
        ),
        (
            "model.provider_bad_request secret://model-token",
            "model_request_contract_invalid",
            "manual_review_model_request_contract",
        ),
        (
            "Plugin credential unavailable secret://plugin-token",
            "plugin_credential_unavailable",
            "manual_review_plugin_credentials",
        ),
        (
            "Plugin arguments do not match input schema: invalid type secret://plugin-token",
            "plugin_invalid_arguments",
            "manual_review_plugin_arguments",
        ),
        (
            "Plugin result does not match output schema: invalid type secret://plugin-token",
            "plugin_invalid_result",
            "manual_review_plugin_result_contract",
        ),
        (
            "Plugin sandbox profile unsupported secret://plugin-token",
            "plugin_sandbox_unsupported",
            "manual_review_plugin_sandbox",
        ),
        (
            "MCP tool unavailable secret://plugin-token",
            "mcp_tool_unavailable",
            "manual_review_mcp_configuration",
        ),
        (
            "mcp_server_not_discovered secret://plugin-token",
            "mcp_server_not_discovered",
            "manual_review_mcp_configuration",
        ),
    ],
)
def test_non_retryable_model_plugin_and_mcp_failures_force_manual_repair_approval(
    reason: str,
    expected_category: str,
    expected_strategy: str,
) -> None:
    run_id = uuid4()
    decision = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=TaskMode.HYBRID,
        routing_decision={"source": "manual"},
        events=(
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=1,
                run_id=run_id,
                reason=reason,
            ),
        ),
        policy=SelfRepairPolicy(requires_approval=False),
    )

    assert decision is not None
    assert decision.failure_category == expected_category
    assert decision.recovery_strategy == expected_strategy
    assert decision.requires_approval is True
    assert decision.automatic_execution is False
    proposal = decision.to_proposal(run_id=run_id)
    assert proposal is not None
    assert proposal["requires_approval"] is True
    assert proposal["automatic_execution"] is False
    assert "secret://plugin-token" not in repr(proposal)


def test_self_repair_failure_injection_matrix_classifies_common_failures() -> None:
    base_run_id = uuid4()
    cases = (
        (
            (
                RunEvent(
                    kind=EventKind.TOOL_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    actor="tool_runner",
                    tool_call_id="call_read_file",
                    tool_name="filesystem.read_file",
                    reason="tool failed after timeout secret://token",
                ),
            ),
            "tool_failure",
            "repair_tool_invocation_after_permission_check",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.STEP_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    step_id="builder_step",
                    actor="builder",
                    reason="step crashed after context compaction Authorization: Bearer token",
                ),
            ),
            "step_failure",
            "retry_failed_step_after_context_compaction",
        ),
        (
            (),
            "missing_failure_event",
            "manual_review_missing_failure_event",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason=RECOVERY_BLOCKED_FAILURE_REASON,
                ),
            ),
            "runtime_recovery_blocked",
            "manual_review_recovery_checkpoint",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="model.provider_rate_limited",
                ),
            ),
            "capacity_pressure",
            "switch_to_available_model_and_retry",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="model.provider_unavailable",
                ),
            ),
            "capacity_pressure",
            "switch_to_available_model_and_retry",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="model.provider_transient_failed",
                ),
            ),
            "capacity_pressure",
            "switch_to_available_model_and_retry",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="Plugin endpoint unavailable",
                ),
            ),
            "plugin_runtime_unavailable",
            "repair_plugin_endpoint_or_adapter_and_retry",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="Plugin tool timed out",
                ),
            ),
            "plugin_runtime_unavailable",
            "repair_plugin_endpoint_or_adapter_and_retry",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="mcp_server_timeout",
                ),
            ),
            "mcp_runtime_unavailable",
            "repair_mcp_server_or_adapter_and_retry",
        ),
        (
            (
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=base_run_id,
                    reason="MCP tool timed out",
                ),
            ),
            "mcp_runtime_unavailable",
            "repair_mcp_server_or_adapter_and_retry",
        ),
    )

    for events, expected_category, expected_strategy in cases:
        run_id = uuid4()
        normalized_events = tuple(
            event.model_copy(update={"run_id": run_id}) for event in events
        )
        decision = classify_terminal_run(
            status=RunStatus.FAILED,
            mode=TaskMode.HYBRID,
            routing_decision={"source": "manual"},
            events=normalized_events,
            policy=SelfRepairPolicy(requires_approval=False),
        )

        assert decision is not None
        assert decision.kind == "repair.classified"
        assert decision.failure_category == expected_category
        assert decision.recovery_strategy == expected_strategy
        assert decision.requires_approval is False
        assert decision.automatic_execution is True
        proposal = decision.to_proposal(run_id=run_id)
        assert proposal is not None
        assert proposal["failure_kind"] == expected_category
        assert proposal["recovery_strategy"] == expected_strategy
        context = repair_context_from_proposal(proposal)
        assert context["failure_kind"] == expected_category
        assert context["recovery_strategy"] == expected_strategy
        serialized = repr({"proposal": proposal, "context": context})
        assert "secret://token" not in serialized
        assert "Authorization: Bearer" not in serialized
