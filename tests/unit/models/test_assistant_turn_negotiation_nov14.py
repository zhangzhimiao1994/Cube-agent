"""Real OpenAI SDK / synthetic HTTP only; no provider or paid acceptance calls."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import cast

import httpx
import pytest

from agent_hub.models import litellm_client as transport
from agent_hub.models.litellm_client import (
    LiteLLMClient,
    ModelNegotiatedCancellation,
    ModelNegotiatedResponseError,
    ModelNegotiationExhausted,
    ModelTransportError,
)
from agent_hub.models.native_messages import NativeAssistantMessage
from agent_hub.models.provider_diagnostics import get_provider_diagnostic
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    TokenUsage,
    ToolDefinition,
)

TURN_REJECTION = (
    "The last message of deepseek-reasoner must be a user message, or an assistant "
    "message with prefix mode on (refer to https://api-docs.deepseek.com/guides/chat_prefix_completion)."
)
CAPABILITIES = frozenset({
    ModelCapability.TEXT, ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT,
})


def deployment() -> Deployment:
    return Deployment(id="selected", logical_model="primary", provider_model="fixture/model",
                      request_model="model", api_base="https://provider.invalid/v1",
                      capabilities=CAPABILITIES)


def request() -> ModelRequest:
    return ModelRequest(
        logical_model="primary",
        messages=(ModelMessage(role="user", content="PRIVATE_ORIGINAL_TASK"),
                  ModelMessage(role="assistant", content="PRIVATE_PARTIAL_ANSWER")),
        required_capabilities=CAPABILITIES,
        timeout_seconds=2, max_output_tokens=24576,
        tools=(ToolDefinition("write_file", "Write the authorized file", {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ("path",), "additionalProperties": False,
        }),),
        required_tool="write_file",
        response_schema=StructuredResponseSchema("Result", {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ("ok",), "additionalProperties": False,
        }),
    )


def rejection(message: str = TURN_REJECTION, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {
        "code": "invalid_request_error", "type": "invalid_request_error",
        "param": None, "message": message,
    }})


def success(*, required: bool = True, usage: bool = True) -> httpx.Response:
    message: dict[str, object] = {"role": "assistant", "content": '{"ok":true}'}
    if required:
        message["tool_calls"] = [{"id": "call_one", "type": "function", "function": {
            "name": "write_file", "arguments": '{"path":"authorized.txt"}',
        }}]
    body: dict[str, object] = {
        "id": "response_fixture", "object": "chat.completion", "created": 1, "model": "model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    return httpx.Response(200, json=body)


def wire(req: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(req.content))


def sdk(handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]) -> LiteLLMClient:
    return LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handler))


@pytest.mark.parametrize("kind,body_state", [
    ("duplicate_message", "invalid"), ("duplicate_envelope", "invalid"),
    ("duplicate_unrelated", "invalid"), ("oversized_envelope", "oversized"),
])
async def test_sdk_normalization_cannot_authorize_correction_from_invalid_received_bytes(
    kind: str, body_state: str,
) -> None:
    exact = json.dumps({"code": "invalid_request_error", "type": "invalid_request_error",
                        "param": None, "message": TURN_REJECTION})
    if kind == "duplicate_message":
        error = '{"message":"unrecognized",' + exact[1:]
        raw = '{"error":' + error + '}'
    elif kind == "duplicate_envelope":
        raw = '{"error":{"message":"unrecognized"},"error":' + exact + '}'
    elif kind == "duplicate_unrelated":
        raw = '{"error":' + exact + ',"unrelated":0,"unrelated":1}'
    else:
        raw = json.dumps({"error": json.loads(exact), "unrelated": "x" * 20000})
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, content=raw.encode(), headers={"content-type": "application/json"})

    with pytest.raises(ModelTransportError) as caught:
        await sdk(handler).complete(deployment(), request(), "fixture-key")
    assert calls == 1 and type(caught.value) is ModelTransportError
    diagnostic = get_provider_diagnostic(caught.value)
    assert diagnostic is not None and len(diagnostic.rejections) == 1
    evidence = diagnostic.rejections[0]
    assert evidence.exception_family == "sdk_bad_request"
    assert evidence.rejection_class == "unknown" and evidence.classification_source == "status_only"
    assert evidence.body_state == body_state


async def test_known_rejection_appends_once_without_changing_authority_or_original_messages() -> None:
    wires: list[dict[str, object]] = []
    urls: list[httpx.URL] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        wires.append(wire(req))
        urls.append(req.url)
        return rejection() if len(wires) == 1 else success()

    original = request()
    result = await sdk(handler).complete(deployment(), original, "fixture-key")
    assert result.usage == TokenUsage(2, 3, 5)
    assert len(wires) == 2 and urls[0] == urls[1]
    before, after = wires
    messages = cast(list[dict[str, object]], before["messages"])
    corrected = cast(list[dict[str, object]], after["messages"])
    assert corrected[:-1] == messages and len(corrected) == len(messages) + 1
    assert corrected[-1]["role"] == "user" and type(corrected[-1]["content"]) is str
    assert corrected[-1]["content"] not in {"PRIVATE_ORIGINAL_TASK", "PRIVATE_PARTIAL_ANSWER"}
    assert {k: v for k, v in before.items() if k != "messages"} == {
        k: v for k, v in after.items() if k != "messages"
    }
    assert original.messages[-1].content == "PRIVATE_PARTIAL_ANSWER"
    assert original.max_output_tokens == 24576 and original.required_tool == "write_file"


@pytest.mark.parametrize("message,status", [
    ("PRIVATE_UNKNOWN_ERROR", 400), (TURN_REJECTION + " PRIVATE_SUFFIX", 400),
    (TURN_REJECTION, 401), (TURN_REJECTION, 500),
])
async def test_unknown_or_non400_never_negotiates(message: str, status: int) -> None:
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return rejection(message, status)

    with pytest.raises(ModelTransportError):
        await sdk(handler).complete(deployment(), request(), "fixture-key")
    assert calls == 1


@pytest.mark.parametrize("kind", ["native", "prefix", "pending_calls", "parts", "empty"])
async def test_ambiguous_or_native_tail_never_changes_messages(
    kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = request()
    if kind == "native":
        req = replace(req, native_tool_continuation=True, messages=(req.messages[0], ModelMessage(
            role="assistant", content="PRIVATE_PARTIAL_ANSWER",
            native_assistant=NativeAssistantMessage("selected", "PRIVATE_PARTIAL_ANSWER", ()),
        )))
    elif kind == "parts":
        req = replace(req, messages=(req.messages[0], ModelMessage(
            role="assistant", content=({"type": "text", "text": "PRIVATE_PARTIAL_ANSWER"},),
        )))
    elif kind == "empty":
        req = replace(req, messages=(req.messages[0], ModelMessage(role="assistant", content="")))
    if kind in {"prefix", "pending_calls"}:
        original = transport._chat_completion_create_kwargs

        def final_wire(d: Deployment, r: ModelRequest, *, stream: bool) -> dict[str, object]:
            body = original(d, r, stream=stream)
            last = cast(list[dict[str, object]], body["messages"])[-1]
            if kind == "prefix":
                last["prefix"] = True
            else:
                last["tool_calls"] = [{"id": "pending", "type": "function", "function": {
                    "name": "write_file", "arguments": "{}",
                }}]
            return body

        monkeypatch.setattr(transport, "_chat_completion_create_kwargs", final_wire)
    calls = 0

    async def handler(http: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return rejection()

    with pytest.raises(ModelTransportError):
        await sdk(handler).complete(deployment(), req, "fixture-key")
    assert calls == 1


@pytest.mark.parametrize("second_message", [
    TURN_REJECTION, "PRIVATE_SECOND_UNKNOWN", "unsupported parameter max_completion_tokens",
])
async def test_second_rejection_is_negotiation_exhausted_without_another_dialect_retry(
    second_message: str,
) -> None:
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return rejection(TURN_REJECTION if calls == 1 else second_message)

    with pytest.raises(ModelNegotiationExhausted) as caught:
        await sdk(handler).complete(deployment(), request(), "fixture-key")
    assert calls == 2
    diagnostic = get_provider_diagnostic(caught.value)
    assert diagnostic is not None and len(diagnostic.rejections) == 2


async def test_corrected_success_still_enforces_required_tool_and_preserves_usage() -> None:
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return rejection() if calls == 1 else success(required=False)

    with pytest.raises(ModelNegotiatedResponseError) as caught:
        await sdk(handler).complete(deployment(), request(), "fixture-key")
    assert calls == 2
    assert caught.value.evidence is not None and caught.value.evidence.usage == TokenUsage(2, 3, 5)


async def test_second_send_shares_original_deadline_and_is_cancelled_at_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    cancelled = asyncio.Event()
    read_timeouts: list[float] = []
    wire_times: list[float] = []
    guards: list[asyncio.Timeout] = []
    deadlines: list[float] = []
    loop = asyncio.get_running_loop()
    actual_timeout_at = asyncio.timeout_at

    def observe_timeout_at(deadline: float | None) -> asyncio.Timeout:
        assert deadline is not None
        deadlines.append(deadline)
        guard = actual_timeout_at(deadline)
        guards.append(guard)
        return guard

    monkeypatch.setattr(asyncio, "timeout_at", observe_timeout_at)

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        wire_times.append(loop.time())
        timeout = cast(dict[str, object], req.extensions["timeout"])["read"]
        assert isinstance(timeout, int | float)
        read_timeouts.append(float(timeout))
        if calls == 1:
            return rejection()
        try:
            # Expire the real timeout guard only after the second send enters.
            # SDK setup and suite load cannot consume a tiny wall-clock fixture.
            assert len(guards) == 1
            guards[0].reschedule(loop.time())
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    started = loop.time()
    with pytest.raises(ModelNegotiationExhausted):
        async with asyncio.timeout(5):
            await sdk(handler).complete(deployment(), replace(request(), timeout_seconds=30), "fixture-key")
    assert calls == 2 and cancelled.is_set()
    # Windows' monotonic clock may report the same tick for both fast sends.
    assert 0 < read_timeouts[1] <= read_timeouts[0] == 30
    assert len(deadlines) == 1 and started + 30 <= deadlines[0] <= wire_times[0] + 30


async def test_expired_original_deadline_cannot_start_correction() -> None:
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return rejection()

    with pytest.raises(ModelTransportError) as caught:
        await sdk(handler).complete(deployment(), replace(request(), timeout_seconds=0.01), "fixture-key")
    assert calls == 1 and type(caught.value) is ModelTransportError


async def test_cancellation_during_second_send_is_terminal_negotiated_cancellation() -> None:
    calls = 0
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return rejection()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    pending = asyncio.create_task(sdk(handler).complete(deployment(), request(), "fixture-key"))
    waiting = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait((pending, waiting), return_when=asyncio.FIRST_COMPLETED, timeout=1)
        if pending.done():
            await pending
        assert entered.is_set()
        pending.cancel()
        with pytest.raises(ModelNegotiatedCancellation):
            await pending
    finally:
        waiting.cancel()
    assert calls == 2 and cancelled.is_set()


@pytest.mark.parametrize("outcome", ["success", "missing_usage", "second_rejection", "cancel"])
async def test_physical_accounting_keeps_first_unknown_and_distinct_corrected_permit(
    outcome: str,
) -> None:
    from agent_hub.models import physical_budget
    from tests.unit.models.test_physical_budget import configured, gateway

    context, store, selected, _ = configured()
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1 or outcome == "second_rejection":
            return rejection()
        if outcome == "cancel":
            raise asyncio.CancelledError
        return success(usage=outcome != "missing_usage")

    selected = replace(selected, capabilities=CAPABILITIES)
    gw, capacity = gateway([selected], handler)
    with physical_budget._bind_run_budget(context):
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await gw.complete_with_context(request())
        elif outcome == "second_rejection":
            with pytest.raises(ModelNegotiationExhausted):
                await gw.complete_with_context(request())
        else:
            result = await gw.complete_with_context(request())
            assert result.response.usage == (None if outcome == "missing_usage" else TokenUsage(2, 3, 5))
    assert calls == 2 and len(store.attempts) == 2
    assert [attempt.state for attempt in store.attempts.values()] == [
        "unknown", "completed" if outcome == "success" else "unknown",
    ]
    assert store.charged == (10005 if outcome == "success" else 20000)
    assert len(capacity.releases) == 1
