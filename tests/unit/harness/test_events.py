from decimal import Decimal
from uuid import UUID

import pytest

from agent_hub.harness.events import (
    gateway_completion_events,
    provider_events_to_run_events,
)
from agent_hub.harness.provider import NormalizedProviderEvent
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall

RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


def test_provider_events_project_to_runtime_extension_events_with_sequences() -> None:
    events = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(kind="model.text_delta", payload={"text": "hello"}),
                NormalizedProviderEvent(
                    kind="tool.requested",
                    payload={
                        "id": "call_1",
                        "name": "workspace_read",
                        "arguments": {"path": "README.md"},
                    },
                ),
            ),
            run_id=RUN_ID,
            start_sequence=5,
        )
    )

    assert [event.sequence for event in events] == [5, 6]
    assert [event.kind for event in events] == ["model.text_delta", "tool.requested"]
    assert events[0].run_id == RUN_ID
    assert events[0].payload == {"text": "hello"}
    assert events[1].payload["arguments"] == {"path": "README.md"}
    assert events[0].actor is None
    assert events[0].message is None


def test_gateway_completion_projects_safe_model_completed_event() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(
            text="final answer",
            usage=TokenUsage(prompt_tokens=7, completion_tokens=11, total_tokens=18),
            provider_metadata={"request_id": "req_123", "finish_reason": "stop"},
        ),
        deployment_id="deepseek-main",
        logical_model="main",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-chat",
        cost_usd=Decimal("0.000123"),
    )

    (event,) = gateway_completion_events(
        completion,
        run_id=RUN_ID,
        start_sequence=9,
        actor="main_agent",
    )

    assert event.kind == "model.completed"
    assert event.sequence == 9
    assert event.payload == {
        "actor": "main_agent",
        "logical_model": "main",
        "deployment_id": "deepseek-main",
        "provider_id": "deepseek",
        "provider_model": "deepseek/deepseek-chat",
        "input_tokens": 7,
        "output_tokens": 11,
        "total_tokens": 18,
        "cost_usd": "0.000123",
        "text_bytes": len(b"final answer"),
        "tool_calls": (),
        "metadata": {"request_id": "req_123", "finish_reason": "stop"},
    }


def test_gateway_completion_projection_rejects_sensitive_tool_arguments() -> None:
    completion = GatewayCompletion(
        response=ModelResponse(
            text=None,
            tool_calls=(
                ToolCall(
                    id="call_1",
                    name="workspace_read",
                    arguments={"api_key": "placeholder"},
                ),
            ),
        ),
        deployment_id="deepseek-main",
        logical_model="main",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-chat",
    )

    with pytest.raises(ValueError, match="sensitive"):
        tuple(gateway_completion_events(completion, run_id=RUN_ID, start_sequence=1))
