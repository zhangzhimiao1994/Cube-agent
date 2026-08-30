from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from uuid import UUID

from agent_hub.harness.provider import NormalizedProviderEvent
from agent_hub.harness.types import HarnessToolCallRequest, JsonValue
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ToolCall
from agent_hub.runtime.contracts import RunEvent

_SENSITIVE_TEXT = re.compile(
    r"(?:api[_ -]?key|sk-[a-z0-9_-]{8,}|bearer\s+|authorization|password|secret|token)",
    re.IGNORECASE,
)
_MAX_PROFILE_ITEMS = 6
_MAX_PROFILE_TEXT = 160
_PUBLIC_DELTA_PHASES = frozenset({"analysis", "draft", "final"})


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
            payload=_provider_event_payload(event),
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


def _provider_event_payload(event: NormalizedProviderEvent) -> Mapping[str, JsonValue]:
    if event.kind in {"model.text_delta", "model.reasoning_delta"}:
        return _provider_delta_payload(event)
    if event.kind != "tool.requested":
        return event.payload
    identifier = _safe_text(event.payload.get("id"))
    name = _safe_text(event.payload.get("name"))
    arguments = event.payload.get("arguments")
    if identifier is None or name is None or not isinstance(arguments, Mapping):
        return {}
    canonical_arguments = json.dumps(
        _mutable_json(arguments),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    argument_keys = tuple(sorted(key for key in arguments if isinstance(key, str)))
    safe_argument_keys = tuple(key for key in argument_keys if _safe_text(key) is not None)
    return {
        "id": identifier,
        "name": name,
        "argument_keys": safe_argument_keys,
        "argument_key_count": len(argument_keys),
        "redacted_argument_key_count": len(argument_keys) - len(safe_argument_keys),
        "argument_bytes": len(canonical_arguments),
    }


def _provider_delta_payload(event: NormalizedProviderEvent) -> Mapping[str, JsonValue]:
    text = event.payload.get("text")
    text_bytes = len(text.encode("utf-8")) if isinstance(text, str) else 0
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "delta_kind": "reasoning" if event.kind == "model.reasoning_delta" else "visible_text",
        "text_bytes": text_bytes,
    }
    chunk_index = event.payload.get("chunk_index")
    if type(chunk_index) is int and chunk_index >= 0:
        payload["chunk_index"] = chunk_index
    phase = _safe_text(event.payload.get("phase"))
    if phase in _PUBLIC_DELTA_PHASES:
        payload["phase"] = phase
    return payload


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


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
