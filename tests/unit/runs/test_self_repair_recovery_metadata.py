from __future__ import annotations

from agent_hub.domain.runs import RunStatus
from agent_hub.recovery_metadata import (
    ORCHESTRATION_CONTRACT_RECOVERY_HINT,
    SAFE_SELF_REPAIR_FAILURE_KINDS,
    SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS,
    SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
)
from agent_hub.runs.self_repair import repair_context_from_proposal
from agent_hub.runs.service import _self_repair_execution_payload
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
