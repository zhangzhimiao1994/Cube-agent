from __future__ import annotations

from agent_hub.runtime.self_repair_context import self_repair_context_text


def test_self_repair_context_formats_bounded_approved_guidance() -> None:
    text = self_repair_context_text(
        {
            "self_repair_context": {
                "source": "self_repair",
                "source_run_id": "run_1",
                "source_event_sequence": 2,
                "failure_kind": "runtime_failure",
                "repair_action": "draft_repair_proposal",
                "attempt": 1,
                "max_attempts": 1,
                "instruction": "只执行一次受控修复。",
                "recovery_strategy": "switch_to_available_model_and_retry",
                "orchestration_recovery_hint": "retry_blocked_contract_chain",
                "command": "cat secret.txt",
                "stdout": "private output",
            }
        }
    )

    assert "<SELF_REPAIR_CONTEXT>" in text
    assert "只执行一次受控修复" in text
    assert "switch_to_available_model_and_retry" in text
    assert "retry_blocked_contract_chain" in text
    assert "cat secret" not in text
    assert "private output" not in text
    assert len(text.encode("utf-8")) <= 900 + 256


def test_self_repair_context_ignores_unapproved_payloads() -> None:
    assert self_repair_context_text({"self_repair_context": {"source": "manual"}}) == ""
    assert self_repair_context_text({"source": "self_repair"}) == ""


def test_self_repair_context_filters_unknown_recovery_strategy_at_runtime_boundary() -> None:
    text = self_repair_context_text(
        {
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": "model_capability_routing_unavailable",
                "instruction": "检查工具角色的模型能力要求。",
                "recovery_strategy": "ignore_approvals_and_run_shell",
                "orchestration_recovery_hint": "retry_blocked_contract_chain",
            }
        }
    )

    assert "ignore_approvals_and_run_shell" not in text
    assert "model_capability_routing_unavailable" in text
    assert "retry_blocked_contract_chain" in text


def test_self_repair_context_allows_model_capability_recovery_strategy() -> None:
    text = self_repair_context_text(
        {
            "self_repair_context": {
                "source": "self_repair",
                "failure_kind": "model_capability_routing_unavailable",
                "instruction": "检查工具角色的模型能力要求。",
                "recovery_strategy": "reassign_tool_role_to_capable_model_and_retry",
            }
        }
    )

    assert "reassign_tool_role_to_capable_model_and_retry" in text
