from __future__ import annotations

from collections.abc import Iterable, Mapping
from uuid import UUID

from agent_hub.harness.provider import NormalizedProviderEvent
from agent_hub.harness.types import HarnessToolCallRequest, JsonValue
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ToolCall
from agent_hub.runtime.contracts import RunEvent


def provider_events_to_run_events(
    events: Iterable[NormalizedProviderEvent],
    *,
    run_id: UUID,
    start_sequence: int,
) -> Iterable[RunEvent]:
    """Project normalized provider stream events onto the shared runtime event log."""

    sequence = start_sequence
    for event in events:
        if not isinstance(event, NormalizedProviderEvent):
            raise TypeError("provider events must be NormalizedProviderEvent values")
        yield RunEvent(
            kind=event.kind,
            sequence=sequence,
            run_id=run_id,
            payload=event.payload,
        )
        sequence += 1


def gateway_completion_events(
    completion: GatewayCompletion,
    *,
    run_id: UUID,
    start_sequence: int,
    actor: str = "main_agent",
) -> Iterable[RunEvent]:
    """Project a completed non-streaming gateway response into harness events."""

    if not isinstance(completion, GatewayCompletion):
        raise TypeError("completion must be GatewayCompletion")
    yield RunEvent(
        kind="model.completed",
        sequence=start_sequence,
        run_id=run_id,
        payload=_completion_payload(completion, actor=actor),
    )


def _completion_payload(
    completion: GatewayCompletion,
    *,
    actor: str,
) -> Mapping[str, JsonValue]:
    response = completion.response
    payload: dict[str, JsonValue] = {
        "actor": actor,
        "logical_model": completion.logical_model,
        "deployment_id": completion.deployment_id,
        "provider_id": completion.provider_id,
        "provider_model": completion.provider_model,
        "cost_usd": None if completion.cost_usd is None else str(completion.cost_usd),
        "text_bytes": 0 if response.text is None else len(response.text.encode("utf-8")),
        "tool_calls": tuple(_tool_call_payload(call) for call in response.tool_calls),
        "metadata": {
            key: value
            for key, value in response.provider_metadata.items()
        },
    }
    if response.usage is not None:
        payload.update(
            {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        )
    return payload


def _tool_call_payload(call: ToolCall) -> Mapping[str, JsonValue]:
    request = HarnessToolCallRequest(
        run_id=UUID("00000000-0000-4000-8000-000000000000"),
        actor="model",
        tool_name=call.name,
        arguments=call.arguments,
        approval_required=True,
        sandbox="restricted",
        idempotency_key=call.id,
        call_id=call.id,
    )
    return {
        "id": request.call_id,
        "name": request.tool_name,
        "arguments": request.arguments,
    }


__all__ = [
    "gateway_completion_events",
    "provider_events_to_run_events",
]
