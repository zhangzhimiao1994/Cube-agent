"""Real SDK/HTTP boundary proofs; every network delegate is local MockTransport."""
import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from agent_hub.models.litellm_client import LiteLLMClient, ModelResponseError, ModelTransportError
from agent_hub.models.physical_admission import _bind_physical_admission, _PhysicalAdmissionStopped
from agent_hub.models.types import TokenUsage
from tests.contracts import test_litellm_client as legacy
from tests.contracts.test_required_tool_transport import (
    deployment,
    provider_response,
    request,
    transport,
)
from tests.unit.models.test_physical_admission import Admission, admission_scope


@pytest.mark.parametrize("kind", ["legacy", "root", "tool_choice"])
async def test_one_attempt_scope_blocks_compatibility_at_physical_delegate(kind: str) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        message = {
            "legacy": "Unsupported parameter: max_completion_tokens",
            "root": "Not found",
            "tool_choice": "The tool_choice parameter does not support being set to required or object in thinking mode",
        }[kind]
        return httpx.Response(404 if kind == "root" else 400, json={"error": {
            "message": message, "type": "invalid_request_error", "code": "invalid_parameter_error",
        }}) if len(wire) == 1 else httpx.Response(200, json=provider_response("chat_completions"))

    admission = Admission()
    scope = admission_scope(admission)
    binding = _bind_physical_admission(scope)
    delegates: list[httpx.MockTransport] = []

    def factory() -> httpx.MockTransport:
        delegate = httpx.MockTransport(handle)
        delegates.append(delegate)
        return delegate

    client = LiteLLMClient(_network_delegate_factory=factory)
    selected = deployment("chat_completions")
    if kind == "root":
        selected = replace(selected, api_base="https://mock.example")
    with binding, pytest.raises(_PhysicalAdmissionStopped):
        await client.complete(selected, request("chat_completions", required_tool="project_verify"), "mock-key")
    assert len(wire) == 1, "a budget scope must stop the second physical HTTP delegate"
    assert len(delegates) == (2 if kind == "root" else 1)
    assert len(admission.receipts) == 1
    assert admission.receipts[0][1].http_status == (404 if kind == "root" else 400)
    assert admission.receipts[0][1].usage is None


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
async def test_tool_free_review_real_sdk_receipt_and_no_budget_wire_fields(protocol: str) -> None:
    admission = Admission()
    scope = admission_scope(admission)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        assert len(admission.metadata) == 1  # reserve/issued was awaited before network
        payload = provider_response(protocol, None) if protocol != "messages" else {
            "id": "msg_model", "type": "message", "model": "test-model",
            "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }
        return httpx.Response(200, json=payload)

    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    selected = deployment("responses" if protocol == "responses" else "chat_completions")
    if protocol == "messages":
        selected = replace(selected, api_base="https://mock.example/v1/messages")
    req = request(protocol, tools=(), required_tool=None, allow_fallback=False)
    with _bind_physical_admission(scope):
        result = await client.complete(selected, req, "mock-key")
    assert result.usage == TokenUsage(2, 3, 5)
    body = json.loads(wire[0].content)
    assert not body.get("tools")
    assert "tool_choice" not in body
    assert all(key not in body for key in ("purpose", "deadline", "permit", "request_sha256"))
    assert len(admission.receipts) == 1
    permit, receipt = admission.receipts[0]
    assert permit == scope.attempts[0].permit
    assert receipt.usage == TokenUsage(2, 3, 5) and receipt.outcome == "completed"
    assert receipt.usage_status == "known" and receipt.http_status == 200
    assert admission.metadata[0].request_sha256 == "a" * 64
    assert admission.metadata[0].purpose == "review"
    assert admission.deadlines == [scope.deadline, scope.deadline]


@pytest.mark.parametrize("streaming", [False, True])
async def test_lazy_capture_revoked_before_first_await_cannot_escape(streaming: bool) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        pytest.fail("revoked capture must never reach HTTP")

    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    with _bind_physical_admission(admission_scope()):
        pending = (client.stream_openai_compatible_chunks(deployment("chat_completions"),
                   request("chat_completions"), "mock-key") if streaming else
                   client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key"))
    with pytest.raises(_PhysicalAdmissionStopped):
        if streaming:
            await anext(pending)
        else:
            await pending


@pytest.mark.parametrize("streaming", [False, True])
async def test_unguardable_custom_factory_refused_before_creation(streaming: bool) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        pytest.fail("custom factory must not send in budget mode")

    client = transport(handle)
    with _bind_physical_admission(admission_scope()), pytest.raises(_PhysicalAdmissionStopped):
        if streaming:
            await anext(client.stream_openai_compatible_chunks(
                deployment("chat_completions"), request("chat_completions"), "mock-key"))
        else:
            await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")


@pytest.mark.parametrize("status", [307, 429, 503])
async def test_controlled_http_never_follows_redirect_or_sdk_retry(status: int) -> None:
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(status, headers={"location": "https://mock.example/elsewhere"},
                              json={"error": {"message": "failed"}})

    scope = admission_scope()
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    with _bind_physical_admission(scope), pytest.raises(ModelTransportError):
        await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert len(wire) == 1


async def test_receipt_failure_keeps_permit_and_blocks_next_call() -> None:
    admission = Admission()
    admission.receipt_failure = True
    scope = admission_scope(admission)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(200, json=provider_response("chat_completions"))

    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    with _bind_physical_admission(scope):
        for _ in range(2):
            with pytest.raises(_PhysicalAdmissionStopped):
                await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert len(wire) == 1
    assert len(scope.attempts) == 1
    assert scope.attempts[0].receipt.usage == TokenUsage(2, 3, 5)
    assert not scope.attempts[0].delivered


async def test_rejected_usage_is_delivered_before_cleanup() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    body = provider_response("chat_completions", None)
    body["choices"][0]["finish_reason"] = "length"
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(
        lambda r: httpx.Response(200, json=body)))
    with _bind_physical_admission(scope), pytest.raises(ModelResponseError):
        await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert admission.receipts[0][1].usage == TokenUsage(2, 3, 5)
    assert admission.receipts[0][1].outcome == "rejected"


async def test_stream_known_usage_survives_cancel_and_scope_is_shared() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    chunk = {"id": "chunk", "object": "chat.completion.chunk", "created": 1,
             "model": "test-model", "choices": [],
             "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    entered = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            entered.set()
            await asyncio.Future()

    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())))
    with _bind_physical_admission(scope):
        chunks = client.stream_openai_compatible_chunks(deployment("chat_completions"),
                                                       request("chat_completions"), "mock-key")
        await anext(chunks)
        task = asyncio.create_task(anext(chunks))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(admission.receipts) == 1
    assert admission.receipts[0][1].outcome == "cancelled"
    assert admission.receipts[0][1].usage == TokenUsage(2, 3, 5)


@pytest.mark.parametrize("streaming", [False, True])
async def test_two_competing_real_sdk_calls_share_one_scope(streaming: bool) -> None:
    admission = Admission()
    scope = admission_scope(admission)
    wire: list[httpx.Request] = []
    factories: list[httpx.MockTransport] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        if streaming:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="data: [DONE]\n\n")
        return httpx.Response(200, json=provider_response("chat_completions"))

    def factory() -> httpx.MockTransport:
        delegate = httpx.MockTransport(handle)
        factories.append(delegate)
        return delegate

    async def invoke() -> object:
        client = LiteLLMClient(_network_delegate_factory=factory)
        if streaming:
            return [x async for x in client.stream_openai_compatible_chunks(
                deployment("chat_completions"), request("chat_completions"), "mock-key")]
        return await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")

    with _bind_physical_admission(scope):
        results = await asyncio.gather(invoke(), invoke(), return_exceptions=True)
    assert len(wire) == 1 and len(factories) == 2
    assert sum(isinstance(r, _PhysicalAdmissionStopped) for r in results) == 1
    assert len(admission.metadata) == 1


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
async def test_missing_or_invalid_usage_cannot_become_zero_or_retry(protocol: str) -> None:
    admission = Admission()
    scope = admission_scope(admission, purpose="primary", max_attempts=2)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        payload = provider_response(protocol, None) if protocol != "messages" else {
            "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn",
        }
        payload.pop("usage", None)
        return httpx.Response(200, json=payload)

    selected = deployment("responses" if protocol == "responses" else "chat_completions")
    if protocol == "messages":
        selected = replace(selected, api_base="https://mock.example/v1/messages")
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    req = request(protocol, tools=(), required_tool=None)
    with _bind_physical_admission(scope):
        try:
            await client.complete(selected, req, "mock-key")
        except ModelResponseError:
            pass
        with pytest.raises(_PhysicalAdmissionStopped):
            await client.complete(selected, req, "mock-key")
    assert len(wire) == 1
    assert admission.receipts[0][1].usage is None
    assert admission.receipts[0][1].usage_status == "missing"


@pytest.mark.parametrize("protocol", ["chat_completions", "messages"])
async def test_invalid_usage_receipt_is_not_known_zero(protocol: str) -> None:
    admission = Admission()
    scope = admission_scope(admission)
    selected = deployment("chat_completions")
    payload = provider_response(protocol, None)
    if protocol == "messages":
        selected = replace(selected, api_base="https://mock.example/v1/messages")
        payload = {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
    payload["usage"] = {"prompt_tokens": -1, "completion_tokens": 3, "total_tokens": 2}
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(
        lambda r: httpx.Response(200, json=payload)))
    with _bind_physical_admission(scope):
        try:
            await client.complete(selected, request(protocol, tools=()), "mock-key")
        except ModelResponseError:
            pass
    receipt = admission.receipts[0][1]
    assert receipt.usage is None and receipt.usage_status == "invalid"


@pytest.mark.parametrize("streaming", [False, True])
async def test_receipt_delivery_failure_still_closes_controlled_client(streaming: bool) -> None:
    admission = Admission()
    admission.receipt_failure = True
    scope = admission_scope(admission)
    closes: list[bool] = []

    class Delegate(httpx.MockTransport):
        async def aclose(self) -> None:
            closes.append(True)

    def handle(req: httpx.Request) -> httpx.Response:
        if streaming:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="data: [DONE]\n\n")
        return httpx.Response(200, json=provider_response("chat_completions"))

    client = LiteLLMClient(_network_delegate_factory=lambda: Delegate(handle))
    with _bind_physical_admission(scope), pytest.raises(_PhysicalAdmissionStopped):
        if streaming:
            _ = [x async for x in client.stream_openai_compatible_chunks(
                deployment("chat_completions"), request("chat_completions"), "mock-key")]
        else:
            await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert closes == [True]
    assert scope.attempts[0].receipt is not None and not scope.attempts[0].delivered


async def test_issued_network_unknown_keeps_identity_and_primary_error() -> None:
    admission = Admission()
    scope = admission_scope(admission, purpose="primary", max_attempts=2)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        raise httpx.ReadError("private transport failure")

    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    with _bind_physical_admission(scope), pytest.raises(ModelTransportError):
        await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
        with pytest.raises(_PhysicalAdmissionStopped):
            await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert len(wire) == 1
    assert admission.receipts[0][0] == scope.attempts[0].permit
    assert admission.receipts[0][1].outcome == "error"
    assert admission.receipts[0][1].http_status is None
    assert admission.receipts[0][1].delegated


@pytest.mark.parametrize("streaming", [False, True])
async def test_absolute_deadline_covers_admission_and_request(streaming: bool) -> None:
    entered = asyncio.Event()

    async def handle(req: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    admission = Admission()
    scope = admission_scope(admission, deadline=asyncio.get_running_loop().time() + 0.3)
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    with _bind_physical_admission(scope), pytest.raises((_PhysicalAdmissionStopped, asyncio.CancelledError)):
        if streaming:
            await anext(client.stream_openai_compatible_chunks(deployment("chat_completions"),
                        request("chat_completions"), "mock-key"))
        else:
            await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert entered.is_set()
    assert len(scope.attempts) == 1 and scope.attempts[0].receipt is not None
    assert scope.attempts[0].receipt.usage is None


@pytest.mark.parametrize("streaming", [False, True])
async def test_lazy_default_capture_does_not_adopt_later_scope(streaming: bool) -> None:
    client = transport(lambda r: httpx.Response(200, json=provider_response("chat_completions")))
    if streaming:
        # A custom legacy stream factory is allowed only because creation captured None.
        client = transport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                    content="data: [DONE]\n\n"))
        pending = client.stream_openai_compatible_chunks(deployment("chat_completions"),
                                                        request("chat_completions"), "mock-key")
    else:
        pending = client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    admission = Admission()
    with _bind_physical_admission(admission_scope(admission)):
        if streaming:
            assert [x async for x in pending] == []
        else:
            await pending
    assert admission.metadata == []


async def test_gateway_selected_sha_and_pricing_handoff_captured_at_call_creation() -> None:
    from agent_hub.models import physical_admission as physical

    admission = Admission()
    scope = admission_scope(admission)
    actual = replace(deployment("chat_completions"), input_per_million_usd=Decimal("1.5"),
                     output_per_million_usd=Decimal(3), context_window_tokens=1000)
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(
        lambda r: httpx.Response(200, json=provider_response("chat_completions"))))
    binding = physical._bind_physical_selection(physical._PhysicalSelection(
        request_sha256="b" * 64, deployment=actual, token_hold=1000,
        cost_hold_usd=Decimal("0.003000")))
    with _bind_physical_admission(scope):
        with binding:
            pending = client.complete(actual, request("chat_completions", max_output_tokens=10), "mock-key")
        await pending
    assert admission.metadata[0].request_sha256 == "b" * 64
    selected = admission.metadata[0].selection
    assert selected.deployment == actual
    assert selected.token_hold == 1000 and selected.cost_hold_usd == Decimal("0.003000")
    assert "selection=" not in repr(admission.metadata[0])


async def test_admission_stop_does_not_retain_sdk_request_or_secret_traceback() -> None:
    admission = Admission()
    admission.failure = RuntimeError(legacy.API_KEY + legacy.PROMPT)
    scope = admission_scope(admission)
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(
        lambda r: pytest.fail("admission failure must not delegate")))
    with _bind_physical_admission(scope), pytest.raises(_PhysicalAdmissionStopped) as caught:
        await client.complete(deployment("chat_completions"), legacy.sensitive_request(), legacy.API_KEY)
    rendered = legacy.captured_traceback(caught.value)
    assert legacy.API_KEY not in rendered and legacy.PROMPT not in rendered
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("kind", ["legacy", "root"])
async def test_stream_start_compatibility_cannot_reissue(kind: str) -> None:
    wire: list[httpx.Request] = []
    admission = Admission()
    scope = admission_scope(admission)

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(404 if kind == "root" else 400, json={"error": {
            "message": "Not found" if kind == "root" else "Unsupported parameter: max_completion_tokens",
        }})

    selected = deployment("chat_completions")
    if kind == "root":
        selected = replace(selected, api_base="https://mock.example")
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))
    with _bind_physical_admission(scope), pytest.raises(_PhysicalAdmissionStopped):
        await anext(client.stream_openai_compatible_chunks(selected, request("chat_completions"), "mock-key"))
    assert len(wire) == 1 and len(admission.receipts) == 1
    assert admission.receipts[0][1].usage is None


async def test_primary_known_receipts_allow_distinct_bounded_permits() -> None:
    admission = Admission()
    scope = admission_scope(admission, purpose="primary", max_attempts=2)
    client = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(
        lambda r: httpx.Response(200, json=provider_response("chat_completions"))))
    with _bind_physical_admission(scope):
        for _ in range(2):
            await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
        with pytest.raises(_PhysicalAdmissionStopped):
            await client.complete(deployment("chat_completions"), request("chat_completions"), "mock-key")
    assert [p.attempt_id.int for p, _ in admission.receipts] == [1, 2]
    assert [m.ordinal for m in admission.metadata] == [1, 2]


async def test_stream_eof_accounts_known_usage_before_bounded_close_failure() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    chunk = {"id": "chunk", "object": "chat.completion.chunk", "created": 1,
             "model": "test-model", "choices": [],
             "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    closed: list[bool] = []

    class Delegate(httpx.MockTransport):
        async def aclose(self) -> None:
            assert admission.receipts[0][1].usage == TokenUsage(2, 3, 5)
            closed.append(True)
            raise TimeoutError("private close error")

    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    client = LiteLLMClient(_network_delegate_factory=lambda: Delegate(handle))
    with _bind_physical_admission(scope):
        chunks = [x async for x in client.stream_openai_compatible_chunks(
            deployment("chat_completions"), request("chat_completions"), "mock-key")]
    assert len(chunks) == 1 and closed == [True]
    assert len(admission.receipts) == 1 and admission.receipts[0][1].outcome == "completed"


async def test_complete_external_cancel_retains_issued_unknown_and_closes() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Delegate(httpx.MockTransport):
        async def aclose(self) -> None:
            assert len(admission.receipts) == 1
            closed.set()

    async def handle(req: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = LiteLLMClient(_network_delegate_factory=lambda: Delegate(handle))
    with _bind_physical_admission(scope):
        task = asyncio.create_task(client.complete(deployment("chat_completions"),
                                                   request("chat_completions"), "mock-key"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    receipt = admission.receipts[0][1]
    assert receipt.outcome == "cancelled" and receipt.usage is None and receipt.delegated


async def test_pending_cancel_before_stream_advance_delivers_receipt_and_closes() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    closed = asyncio.Event()
    chunk = {"id": "chunk", "object": "chat.completion.chunk", "created": 1,
             "model": "test-model", "choices": [],
             "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

    class Delegate(httpx.MockTransport):
        async def aclose(self) -> None:
            closed.set()

    client = LiteLLMClient(_network_delegate_factory=lambda: Delegate(
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")))
    with _bind_physical_admission(scope):
        chunks = client.stream_openai_compatible_chunks(deployment("chat_completions"),
                                                       request("chat_completions"), "mock-key")
        await anext(chunks)

        async def advance_already_cancelled() -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await anext(chunks)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(advance_already_cancelled())
    assert closed.is_set()
    assert admission.receipts[0][1].outcome == "cancelled"
    assert admission.receipts[0][1].usage == TokenUsage(2, 3, 5)
