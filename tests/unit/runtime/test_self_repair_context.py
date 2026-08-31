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
                "command": "cat secret.txt",
                "stdout": "private output",
            }
        }
    )

    assert "<SELF_REPAIR_CONTEXT>" in text
    assert "只执行一次受控修复" in text
    assert "cat secret" not in text
    assert "private output" not in text
    assert len(text.encode("utf-8")) <= 900 + 256


def test_self_repair_context_ignores_unapproved_payloads() -> None:
    assert self_repair_context_text({"self_repair_context": {"source": "manual"}}) == ""
    assert self_repair_context_text({"source": "self_repair"}) == ""
