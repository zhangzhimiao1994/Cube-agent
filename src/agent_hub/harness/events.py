from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from uuid import UUID

from agent_hub.harness.provider import NormalizedProviderEvent
from agent_hub.harness.types import HarnessToolCallRequest, JsonValue
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ToolCall
from agent_hub.runtime.contracts import RunEvent

_SENSITIVE_TEXT = re.compile(
    r"(?:sk-[a-z0-9_-]{8,}|bearer\s+|authorization|password|secret|token)",
    re.IGNORECASE,
)
_MAX_PROFILE_ITEMS = 6
_MAX_PROFILE_TEXT = 160


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


def harness_started_event(
    *,
    routing_decision: Mapping[str, object],
    run_id: UUID,
    start_sequence: int,
) -> RunEvent | None:
    """Project the selected harness profile into a public lifecycle event."""

    decision = routing_decision.get("harness_decision")
    if not isinstance(decision, Mapping):
        return None
    provider = _safe_text(decision.get("selected_provider"))
    model = _safe_text(decision.get("selected_model"))
    logical_model = _safe_text(decision.get("selected_logical_model"))
    mode = _safe_text(decision.get("mode"))
    requires_approval = decision.get("requires_approval")
    if (
        provider is None
        or model is None
        or logical_model is None
        or mode is None
        or type(requires_approval) is not bool
    ):
        return None
    return RunEvent(
        kind="harness.started",
        sequence=start_sequence,
        run_id=run_id,
        payload={
            "schema_version": 1,
            "phase": "started",
            "mode": mode,
            "provider": provider,
            "model": model,
            "logical_model": logical_model,
            "requires_approval": requires_approval,
            "capabilities": _safe_text_tuple(decision.get("capability_reasons")),
            "policy": _safe_text_tuple(decision.get("policy_reasons")),
            "context": _safe_text_tuple(decision.get("context_reasons")),
            "fallbacks": _safe_text_tuple(decision.get("fallbacks_considered")),
        },
    )


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


def _safe_text(value: object) -> str | None:
    if type(value) is not str:
        return None
    text = value.strip()
    if not text or _SENSITIVE_TEXT.search(text):
        return None
    if len(text) > _MAX_PROFILE_TEXT:
        return f"{text[:_MAX_PROFILE_TEXT]}..."
    return text


def _safe_text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    result: list[str] = []
    for item in value:
        text = _safe_text(item)
        if text is None:
            continue
        result.append(text)
        if len(result) >= _MAX_PROFILE_ITEMS:
            break
    return tuple(result)


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
    "harness_started_event",
    "provider_events_to_run_events",
]
