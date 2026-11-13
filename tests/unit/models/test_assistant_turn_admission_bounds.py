"""Gateway accounting for a single provider-proven assistant-turn correction.

No provider is called. Reuse the author-owned exact rejection helper and original
physical budget fixture; UNKNOWN never becomes a refund or replay grant.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace

import httpx
import pytest
from openai import BadRequestError

from agent_hub.models import physical_budget as budget
from agent_hub.models.gateway import GatewayRejectedOutput
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.physical_admission import _PhysicalAdmissionStopped
from agent_hub.models.provider_diagnostics import capture_error, make_request_shape
from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest, ToolDefinition
from tests.unit.models.physical_budget_typed_fixture import wire_object
from tests.unit.models.test_assistant_turn_negotiation_nov14 import (
    TURN_REJECTION,
)
from tests.unit.models.test_assistant_turn_negotiation_nov14 import (
    rejection as turn_rejection,
)
from tests.unit.models.test_physical_budget import configured, gateway, logical_request, response


async def test_unknown_assistant_last_400_keeps_full_hold_without_retry() -> None:
    context, store, selected, backup = configured()
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(400, json={"error": {
            "message": "unknown provider validation failure",
            "type": "invalid_request_error",
        }})

    gw, capacity = gateway(
        [selected, backup], handle, fallbacks={"primary": "reviewer"},
    )
    request = logical_request(messages=(
        ModelMessage("user", "Original authorized task"),
        ModelMessage("assistant", "Ordinary assistant text"),
    ))
    with budget._bind_run_budget(context), pytest.raises(ModelTransportError) as caught:
        await gw.complete_with_context(request)
    assert caught.value.status_code == 400
    assert len(wire) == 1
    assert [name for name, _ in store.calls] == ["reserve", "issued", "finish"]
    assert len(store.attempts) == 1
    attempt = next(iter(store.attempts.values()))
    assert attempt.state == "unknown"
    assert attempt.token_hold == selected.context_window_tokens == store.charged
    assert len(capacity.releases) == 1


def _required_tool_success() -> httpx.Response:
    body = response()
    choices = body["choices"]
    assert isinstance(choices, list) and isinstance(choices[0], dict)
    choices[0]["message"] = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_exact", "type": "function", "function": {
            "name": "project_inspect", "arguments": "{}",
        },
    }]}
    return httpx.Response(200, json=body)


def _assistant_last_request() -> ModelRequest:
    return logical_request(
        messages=(ModelMessage("user", "Original authorized task"),
                  ModelMessage("assistant", "Ordinary assistant text")),
        required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}),
        required_tool="project_inspect",
        tools=(ToolDefinition("project_inspect", "Inspect original authorized project",
                              {"type": "object", "additionalProperties": False}),),
        max_output_tokens=128, timeout_seconds=3,
    )


async def test_assistant_turn_cue_preserves_authority_and_reserves_each_physical_send() -> None:
    context, store, selected, backup = configured()
    selected = replace(selected, capabilities=_assistant_last_request().required_capabilities)
    backup = replace(backup, capabilities=selected.capabilities)
    wire: list[httpx.Request] = []

    async def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            await asyncio.sleep(0.01)
            return turn_rejection()
        assert len(wire) == 2
        assert [name for name, _ in store.calls] == [
            "reserve", "issued", "finish", "reserve", "issued",
        ]
        assert store.charged == 20000
        return _required_tool_success()

    gw, capacity = gateway([selected, backup], handle, fallbacks={"primary": "reviewer"})
    request = _assistant_last_request()
    original_deadline = context.deadline
    original_input = context.evidence.message_sha256
    with budget._bind_run_budget(context):
        result = await gw.complete_with_context(request)
    assert len(wire) == 2
    first, second = [wire_object(req.content) for req in wire]
    old_messages, new_messages = first["messages"], second["messages"]
    assert isinstance(old_messages, list) and isinstance(new_messages, list)
    assert new_messages[:-1] == old_messages
    cue = new_messages[-1]
    assert isinstance(cue, dict) and set(cue) == {"role", "content"}
    assert cue["role"] == "user" and isinstance(cue["content"], str) and cue["content"]
    assert second == {**first, "messages": new_messages}
    assert second["max_completion_tokens"] == 128
    assert context.deadline == original_deadline
    assert context.evidence.message_sha256 == original_input
    first_timeout, second_timeout = wire[0].extensions["timeout"], wire[1].extensions["timeout"]
    assert isinstance(first_timeout, dict) and isinstance(second_timeout, dict)
    assert 0 < second_timeout["read"] < first_timeout["read"] <= request.timeout_seconds
    reservations = [call[1] for call in store.calls if call[0] == "reserve"]
    issues = [call[1] for call in store.calls if call[0] == "issued"]
    assert len(reservations) == len(issues) == 2
    assert reservations[0]["attempt_id"] != reservations[1]["attempt_id"]
    assert issues[0]["physical_transport_id"] != issues[1]["physical_transport_id"]
    assert all(args["token_hold"] == selected.context_window_tokens == 10000
               for args in reservations)
    assert all(args["deployment"].deployment_id == selected.id for args in reservations)
    assert sorted(a.state for a in store.attempts.values()) == ["completed", "unknown"]
    assert store.charged == 10000 + 5
    assert result.response.tool_calls[0].name == request.required_tool
    assert not result.fallback_used and len(capacity.releases) == 1


async def test_assistant_turn_second_send_requires_remaining_budget_after_unknown_400() -> None:
    context, store, selected, _ = configured()
    selected = replace(selected, capabilities=_assistant_last_request().required_capabilities)
    store.grant = 10000
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return turn_rejection()

    gw, _ = gateway([selected], handle)
    with budget._bind_run_budget(context), pytest.raises(_PhysicalAdmissionStopped):
        await gw.complete_with_context(_assistant_last_request())
    assert len(wire) == 1
    assert [name for name, _ in store.calls] == ["reserve", "issued", "finish", "reserve"]
    assert len(store.attempts) == 1
    assert next(iter(store.attempts.values())).state == "unknown"
    assert store.charged == store.grant == 10000


@pytest.mark.parametrize("second", ["400", "network", "wrong_tool"])
async def test_assistant_turn_second_failure_never_third_send_or_fallback(second: str) -> None:
    context, store, selected, backup = configured()
    selected = replace(selected, capabilities=_assistant_last_request().required_capabilities)
    backup = replace(backup, capabilities=selected.capabilities)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1 or second == "400":
            return turn_rejection()
        if second == "network":
            raise httpx.ConnectError("synthetic lost response", request=req)
        return httpx.Response(200, json=response())

    gw, capacity = gateway([selected, backup], handle, fallbacks={"primary": "reviewer"})
    with budget._bind_run_budget(context), pytest.raises(
        GatewayRejectedOutput if second == "wrong_tool" else ModelTransportError,
    ):
        await gw.complete_with_context(_assistant_last_request())
    assert len(wire) == 2
    assert len(store.attempts) == 2
    assert all(a.deployment.deployment_id == selected.id for a in store.attempts.values())
    assert len(capacity.releases) == 1
    assert store.charged == 10000 + (5 if second == "wrong_tool" else 10000)


async def test_assistant_turn_cancellation_keeps_both_holds_without_fallback() -> None:
    context, store, selected, backup = configured()
    selected = replace(selected, capabilities=_assistant_last_request().required_capabilities)
    backup = replace(backup, capabilities=selected.capabilities)
    wire: list[httpx.Request] = []
    second_entered = asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if len(wire) == 1:
            return turn_rejection()
        second_entered.set()
        await asyncio.Future[None]()
        raise AssertionError("cancelled second send cannot finish")

    gw, capacity = gateway([selected, backup], handle, fallbacks={"primary": "reviewer"})
    with budget._bind_run_budget(context):
        pending = asyncio.create_task(gw.complete_with_context(_assistant_last_request()))
        waiter = asyncio.create_task(second_entered.wait())
        try:
            await asyncio.wait({pending, waiter}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
            assert second_entered.is_set()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            pending.cancel()
            waiter.cancel()
            await asyncio.gather(pending, waiter, return_exceptions=True)
    assert len(wire) == 2 and len(store.attempts) == 2
    assert all(a.state == "unknown" for a in store.attempts.values())
    assert store.charged == 20000
    assert len(capacity.releases) == 1


@pytest.mark.parametrize("raw_state", ["absent", "unavailable", "inconsistent"])
def test_sdk_assistant_turn_cannot_override_missing_or_inconsistent_received_bytes(
    raw_state: str,
) -> None:
    # The SDK body claims an exact signature, but the original bytes are the
    # authority. In particular, never consume a response stream to recover them.
    request = httpx.Request("POST", "https://fixture.invalid/v1/chat/completions")
    if raw_state == "unavailable":
        class NeverRead(httpx.SyncByteStream):
            def __iter__(self) -> Iterator[bytes]:
                raise AssertionError("diagnostic capture must not read a body stream")

        response = httpx.Response(400, stream=NeverRead(), request=request)
    elif raw_state == "absent":
        response = httpx.Response(400, content=b"", request=request)
    else:
        response = httpx.Response(400, json={"error": {
            "code": "content_policy_violation", "type": "invalid_request_error",
            "message": "Synthetic policy refusal",
        }}, request=request)
    sdk_body = {"message": TURN_REJECTION, "code": "invalid_request_error",
                "type": "invalid_request_error", "param": None}
    error = BadRequestError("synthetic SDK rejection", response=response, body=sdk_body)
    shape = make_request_shape({"messages": [
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "Ordinary partial reply"},
    ]}, "chat_completions", send_ordinal=1)
    assert shape is not None
    diagnostic = capture_error(error, shape)
    assert diagnostic is not None
    rejected = diagnostic.rejections[-1]
    assert rejected.exception_family == "sdk_bad_request"
    assert rejected.request_shape == shape
    assert rejected.rejection_class == ("policy" if raw_state == "inconsistent" else "unknown")
    assert rejected.body_state == ("available" if raw_state == "inconsistent" else raw_state)
    assert rejected.classification_source != "exact_signature"
    if raw_state == "unavailable":
        assert not response.is_stream_consumed
