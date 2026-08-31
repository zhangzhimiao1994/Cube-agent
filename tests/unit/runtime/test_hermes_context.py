from __future__ import annotations

from agent_hub.runtime.hermes_context import hermes_memory_context_text


def test_hermes_memory_context_is_bounded_auxiliary_prompt_guidance() -> None:
    text = hermes_memory_context_text(
        {
            "hermes": {
                "injected_memories": [
                    {
                        "summary": "用户希望调度卡片只显示摘要，详情放抽屉。",
                        "memory_type": "ui_rule",
                        "target": "frontend",
                        "reason": "命中已确认的 UI 记忆。",
                    },
                    {"summary": "x" * 2_000, "memory_type": "project_fact"},
                    {"summary": "第三条"},
                    {"summary": "第四条不应进入 prompt"},
                ]
            }
        }
    )

    assert "HERMES_MEMORY_CONTEXT" in text
    assert "only as bounded guidance" in text
    assert "Current user instructions override them" in text
    assert "用户希望调度卡片只显示摘要" in text
    assert "第四条不应进入 prompt" not in text
    assert len(text.encode("utf-8")) <= 1_200


def test_hermes_memory_context_ignores_missing_or_invalid_payloads() -> None:
    assert hermes_memory_context_text({}) == ""
    assert hermes_memory_context_text({"hermes": {"injected_memories": "raw"}}) == ""
    assert hermes_memory_context_text({"hermes": {"injected_memories": [{"summary": ""}]}}) == ""
