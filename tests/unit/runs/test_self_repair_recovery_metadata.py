from __future__ import annotations

from agent_hub.domain.runs import RunStatus
from agent_hub.recovery_metadata import (
    SAFE_SELF_REPAIR_FAILURE_KINDS,
    SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS,
    SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
)
from agent_hub.runs.self_repair import repair_context_from_proposal
from agent_hub.runs.service import _self_repair_execution_payload
from agent_hub.runtime.self_repair_context import self_repair_context_text


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
