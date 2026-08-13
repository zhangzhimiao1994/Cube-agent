from __future__ import annotations

from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import ConversationContextItem
from agent_hub.runs.service import (
    _conversation_history_artifact,
    _conversation_history_token_budget,
    _runtime_timeout_seconds,
    _runtime_token_budget,
)


def test_runtime_timeout_policy_uses_configured_production_window_for_dispatch() -> None:
    assert _runtime_timeout_seconds(TaskMode.DISPATCH, configured_seconds=300.0) == 300.0


def test_runtime_timeout_policy_clamps_to_runtime_contract_limit() -> None:
    assert _runtime_timeout_seconds(TaskMode.HYBRID, configured_seconds=7200.0) == 3600.0


def test_runtime_token_budget_policy_uses_configured_complex_budget() -> None:
    assert _runtime_token_budget(TaskMode.HYBRID, configured_tokens=1_000_000) == 1_000_000


def test_runtime_token_budget_policy_clamps_to_runtime_contract_limit() -> None:
    assert _runtime_token_budget(TaskMode.DISPATCH, configured_tokens=99_000_000) == 10_000_000


def test_conversation_history_budget_uses_main_agent_context_window() -> None:
    assert (
        _conversation_history_token_budget(
            runtime_token_budget=1_000_000,
            main_agent_context_window_tokens=4096,
        )
        == 1024
    )


def test_conversation_history_stays_full_when_inside_budget() -> None:
    artifact = _conversation_history_artifact(
        conversation_id="conv-short",
        current_request="continue",
        context_items=(
            ConversationContextItem(
                run_id=uuid4(),
                request="first request",
                artifacts=(
                    {
                        "producer": "main_agent",
                        "content": {"text": "first answer"},
                    },
                ),
            ),
        ),
        history_token_budget=4096,
    )

    assert artifact is not None
    assert artifact.producer == "conversation_history"
    assert artifact.content["context_policy"] == "full_history"
    text = artifact.content["text"]
    assert isinstance(text, str)
    assert "first request" in text
    assert "first answer" in text


def test_conversation_history_is_auto_compacted_when_over_model_budget() -> None:
    old_noise = "old implementation detail " * 2000
    latest_decision = "latest important conclusion: use framework-level context compression"

    artifact = _conversation_history_artifact(
        conversation_id="conv-long",
        current_request="continue the work",
        context_items=(
            ConversationContextItem(
                run_id=uuid4(),
                request=old_noise,
                artifacts=(
                    {
                        "producer": "main_agent",
                        "content": {"text": old_noise},
                    },
                ),
            ),
            ConversationContextItem(
                run_id=uuid4(),
                request="what was the final decision?",
                artifacts=(
                    {
                        "producer": "main_agent",
                        "content": {"text": latest_decision},
                    },
                ),
            ),
        ),
        history_token_budget=256,
    )

    assert artifact is not None
    assert artifact.producer == "conversation_history_compacted"
    assert artifact.content["context_policy"] == "auto_compacted"
    original_tokens = artifact.content["original_estimated_tokens"]
    history_budget = artifact.content["history_token_budget"]
    text = artifact.content["text"]
    assert type(original_tokens) is int
    assert type(history_budget) is int
    assert isinstance(text, str)
    assert original_tokens > history_budget
    assert latest_decision in text
