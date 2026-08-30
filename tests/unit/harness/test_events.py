import json
from decimal import Decimal
from uuid import UUID

import pytest

from agent_hub.harness.events import (
    gateway_completion_events,
    provider_events_to_run_events,
    safe_tool_event_payload,
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
    assert events[0].payload == {
        "schema_version": 1,
        "delta_kind": "visible_text",
        "text_bytes": len(b"hello"),
    }
    assert events[1].payload == {
        "id": "call_1",
        "name": "workspace_read",
        "argument_keys": ("path",),
        "argument_key_count": 1,
        "redacted_argument_key_count": 0,
        "argument_bytes": len(b'{"path":"README.md"}'),
    }
    assert events[0].actor is None
    assert events[0].message is None


def test_provider_text_delta_projection_does_not_expose_stream_text() -> None:
    (event,) = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(
                    kind="model.text_delta",
                    payload={"text": "partial answer with private context", "chunk_index": 4, "phase": "draft"},
                ),
            ),
            run_id=RUN_ID,
            start_sequence=1,
        )
    )

    serialized = json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True)
    assert "partial answer with private context" not in serialized
    assert event.payload == {
        "schema_version": 1,
        "delta_kind": "visible_text",
        "text_bytes": len(b"partial answer with private context"),
        "chunk_index": 4,
        "phase": "draft",
    }


def test_provider_reasoning_delta_projection_does_not_expose_hidden_reasoning() -> None:
    (event,) = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(
                    kind="model.reasoning_delta",
                    payload={"text": "hidden chain of thought", "chunk_index": 2, "phase": "analysis"},
                ),
            ),
            run_id=RUN_ID,
            start_sequence=1,
        )
    )

    serialized = json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True)
    assert "hidden chain of thought" not in serialized
    assert event.payload == {
        "schema_version": 1,
        "delta_kind": "reasoning",
        "text_bytes": len(b"hidden chain of thought"),
        "chunk_index": 2,
        "phase": "analysis",
    }


def test_provider_delta_projection_ignores_unknown_phase_text() -> None:
    (event,) = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(
                    kind="model.reasoning_delta",
                    payload={"text": "hidden chain of thought", "phase": "user asked about private roadmap"},
                ),
            ),
            run_id=RUN_ID,
            start_sequence=1,
        )
    )

    serialized = json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True)
    assert "user asked about private roadmap" not in serialized
    assert "phase" not in event.payload


@pytest.mark.parametrize("chunk_index", [True, False, -1, "2"])
def test_provider_delta_projection_ignores_invalid_chunk_indexes(chunk_index: bool | int | str) -> None:
    (event,) = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(
                    kind="model.reasoning_delta",
                    payload={"text": "hidden chain of thought", "chunk_index": chunk_index},
                ),
            ),
            run_id=RUN_ID,
            start_sequence=1,
        )
    )

    assert "chunk_index" not in event.payload


def test_provider_tool_event_projection_does_not_expose_argument_values() -> None:
    (event,) = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(
                    kind="tool.requested",
                    payload={
                        "id": "call_1",
                        "name": "external_message",
                        "arguments": {"body": "secret user content"},
                    },
                ),
            ),
            run_id=RUN_ID,
            start_sequence=1,
        )
    )

    serialized = json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True)
    assert "secret user content" not in serialized
    assert "arguments_sha256" not in event.payload
    assert event.payload["argument_keys"] == ("body",)


def test_provider_tool_event_projection_redacts_sensitive_argument_keys() -> None:
    (event,) = tuple(
        provider_events_to_run_events(
            (
                NormalizedProviderEvent(
                    kind="tool.requested",
                    payload={
                        "id": "call_1",
                        "name": "external_message",
                        "arguments": {"api_key": "sk-secret123", "path": "README.md"},
                    },
                ),
            ),
            run_id=RUN_ID,
            start_sequence=1,
        )
    )

    serialized = json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True)
    assert "sk-secret123" not in serialized
    assert "api_key" not in serialized
    assert "README.md" not in serialized
    assert event.payload["argument_keys"] == ("path",)
    assert event.payload["argument_key_count"] == 2
    assert event.payload["redacted_argument_key_count"] == 1


def test_safe_tool_event_payload_projects_terminal_result_metadata_only() -> None:
    payload = safe_tool_event_payload(
        name="run_safe_command",
        status="succeeded",
        result={
            "exit_code": 0,
            "stdout": "private terminal output",
            "stderr": "secret warning",
            "result": {"ok": True},
        },
        artifact_id="artifact-tool-result",
    )

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["operation_kind"] == "terminal"
    assert payload["exit_code"] == 0
    assert payload["stdout_bytes"] == len(b"private terminal output")
    assert payload["stderr_bytes"] == len(b"secret warning")
    assert payload["output_bytes"] == len(b"private terminal outputsecret warning")
    assert payload["result_bytes"] > 0
    assert "private terminal output" not in serialized
    assert "secret warning" not in serialized
    assert '"result"' not in serialized


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
