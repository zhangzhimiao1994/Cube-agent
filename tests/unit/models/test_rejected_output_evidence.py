from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI

from agent_hub.models import gateway as gateway_module
from agent_hub.models import litellm_client as transport_module
from agent_hub.models import types as model_types
from agent_hub.models.gateway import (
    DeploymentPricing,
    GatewayCompletion,
    GatewayRejectedOutput,
    ModelGateway,
)
from agent_hub.models.litellm_client import LiteLLMClient, ModelResponseError, OpenAIClientFactory
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    ModelResponse,
    RejectedOutputEvidence,
    StructuredResponseSchema,
    TokenUsage,
)
from tests.contracts.test_responses_client import deployment, request, setup_client
from tests.unit.models.test_gateway import CapacityStub, SecretStub, TransportStub, lease

PRIVATE = "private-invalid-final-text"


def wire(text: str = PRIVATE, **updates: Any) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "untrusted-provider-model",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [{
            "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {"input_tokens": 12, "output_tokens": 117, "total_tokens": 129},
        **updates,
    }


def harness(
    payload: dict[str, Any], *, pricing: dict[str, DeploymentPricing] | None = None,
    wait: asyncio.Event | None = None,
    close_started: asyncio.Event | None = None,
    close_gate: asyncio.Event | None = None,
    capacity_wait_timeout: float = 5,
) -> tuple[ModelGateway, CapacityStub, list[httpx.Request], list[httpx.AsyncClient], asyncio.Event]:
    calls: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []
    entered = asyncio.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        entered.set()
        if wait is not None:
            await wait.wait()
        return httpx.Response(200, json=payload)

    class ClosingClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            if close_started is not None:
                close_started.set()
            if close_gate is not None:
                await close_gate.wait()
            await super().aclose()

    def factory(**kwargs: Any) -> AsyncOpenAI:
        client = ClosingClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return AsyncOpenAI(http_client=client, **kwargs)

    primary = deployment(quota_scope_id="scope-native")
    fallback = deployment(id="backup", logical_model="backup", quota_scope_id="scope-backup")
    capacity = CapacityStub([lease("native"), lease("backup")])
    gateway = ModelGateway(
        registry=ModelRegistry([primary, fallback]), capacity_pool=capacity,
        secret_resolver=SecretStub(capacity.events),
        transport=LiteLLMClient(client_factory=cast(OpenAIClientFactory, factory)),
        fallbacks={"primary": "backup"}, pricing=pricing,
        capacity_wait_timeout=capacity_wait_timeout,
    )
    return gateway, capacity, calls, clients, entered


@pytest.mark.parametrize("text,reason", [(PRIVATE, "invalid_json"), ('{"verdict":"wrong"}', "schema_mismatch")])
async def test_wire_rejection_preserves_private_evidence_and_never_falls_back(
    text: str, reason: str, caplog: pytest.LogCaptureFixture,
) -> None:
    gateway, capacity, calls, clients, _ = harness(wire(text), pricing={
        "native": DeploymentPricing(Decimal(1), Decimal(2)),
    })
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request(allow_fallback=True))
    error = caught.value
    assert isinstance(error, GatewayRejectedOutput)
    assert type(error).__name__ == "GatewayRejectedOutput"
    assert not isinstance(error, GatewayCompletion)
    evidence = getattr(error, "evidence", None)
    assert evidence is not None and evidence.final_text == text
    assert evidence.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert evidence.usage == TokenUsage(12, 117, 129)
    assert evidence.usage_status == "known" and evidence.status == "completed"
    assert evidence.reason == reason and evidence.correction_eligible is True
    assert error.deployment_id == "native" and error.logical_model == "primary"
    assert error.provider_id == "provider" and error.provider_model == "provider/original"
    assert error.cost_usd == Decimal("0.000246")
    assert error.attempted_logical_models == ("primary",) and not error.fallback_used
    assert len(calls) == 1 and calls[0].url.path == "/v1/responses"
    assert len(capacity.releases) == 1 and len(capacity.records) == 1
    assert capacity.records[0][-1] is False
    assert all(client.is_closed for client in clients)
    assert text not in str(error) + repr(error) + repr(evidence) + caplog.text
    assert "key-for-" not in caplog.text


@pytest.mark.parametrize("raw_usage,state", [
    (None, "missing"), ({"input_tokens": 12}, "invalid"),
    ({"input_tokens": -1, "output_tokens": 2, "total_tokens": 1}, "invalid"),
    ({"input_tokens": 12, "output_tokens": 117, "total_tokens": 999}, "invalid"),
])
async def test_rejected_unknown_usage_is_not_fabricated(raw_usage: Any, state: str) -> None:
    gateway, _, calls, _, _ = harness(wire(usage=raw_usage))
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request())
    evidence = getattr(caught.value, "evidence", None)
    assert evidence is not None and evidence.usage_status == state
    assert evidence.correction_eligible is False
    assert isinstance(caught.value, GatewayRejectedOutput)
    assert evidence.usage is None and caught.value.cost_usd is None
    assert len(calls) == 1


@pytest.mark.parametrize("pricing,expected", [
    (None, None), ({"native": DeploymentPricing(Decimal(0), Decimal(0))}, Decimal(0)),
])
async def test_rejection_price_unknown_is_distinct_from_explicit_zero(
    pricing: dict[str, DeploymentPricing] | None, expected: Decimal | None,
) -> None:
    gateway, _, _, _, _ = harness(wire(), pricing=pricing)
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request())
    assert type(caught.value).__name__ == "GatewayRejectedOutput"
    assert isinstance(caught.value, GatewayRejectedOutput)
    assert caught.value.cost_usd == expected


@pytest.mark.parametrize("updates", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    {"output": [{"id": "m", "type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "refusal", "refusal": "private refusal"}]}]},
    {"output": [{"type": "function_call", "call_id": "bad", "name": "unknown", "arguments": "{"}]},
    {"output": [{"type": "reasoning", "summary": []}]},
])
async def test_ineligible_rejection_retains_known_consumption(updates: dict[str, Any]) -> None:
    gateway, _, calls, _, _ = harness(wire(**updates))
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request())
    evidence = getattr(caught.value, "evidence", None)
    assert evidence is not None and not evidence.correction_eligible
    assert evidence.usage == TokenUsage(12, 117, 129) and evidence.usage_status == "known"
    assert evidence.final_text is None and len(calls) == 1


async def test_oversized_final_text_is_not_retained_or_truncated_into_candidate() -> None:
    gateway, _, _, _, _ = harness(wire("x" * 65_537))
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request())
    evidence = getattr(caught.value, "evidence", None)
    assert evidence is not None and evidence.final_text is None
    assert evidence.text_sha256 is None and not evidence.correction_eligible
    assert evidence.usage == TokenUsage(12, 117, 129)


def test_evidence_is_immutable_and_digest_cannot_be_supplied() -> None:
    evidence_type = getattr(model_types, "RejectedOutputEvidence", None)
    assert evidence_type is not None
    evidence = evidence_type(final_text=PRIVATE, usage=TokenUsage(12, 117, 129),
                             usage_status="known", status="completed", reason="invalid_json")
    with pytest.raises(FrozenInstanceError):
        evidence.final_text = "changed"
    with pytest.raises(TypeError):
        evidence_type(final_text=PRIVATE, usage=None, usage_status="missing",
                      status="completed", reason="invalid_json", text_sha256="forged")
    with pytest.raises(ValueError):
        evidence_type(final_text="x" * 65_537, usage=None, usage_status="missing",
                      status="completed", reason="invalid_json")
    with pytest.raises(ValueError):
        evidence_type(final_text=PRIVATE, usage=None, usage_status="known",
                      status="completed", reason="invalid_json")


@pytest.mark.parametrize("status_code", [None, 429, 503])
async def test_all_response_errors_without_evidence_bypass_network_fallback(status_code: int | None) -> None:
    primary = deployment(quota_scope_id="scope-native")
    backup = deployment(id="backup", logical_model="backup", quota_scope_id="scope-backup")
    capacity = CapacityStub([lease("native"), lease("backup")])
    transport = TransportStub(capacity.events, failure=ModelResponseError("private SDK error", status_code=status_code))
    gateway = ModelGateway(registry=ModelRegistry([primary, backup]), capacity_pool=capacity,
                           secret_resolver=SecretStub(capacity.events), transport=transport,
                           fallbacks={"primary": "backup"})
    with pytest.raises(Exception) as caught:
        await gateway.complete_with_context(request())
    assert type(caught.value).__name__ == "GatewayRejectedOutput"
    assert isinstance(caught.value, GatewayRejectedOutput)
    assert caught.value.evidence is None and caught.value.cost_usd is None
    assert len(transport.calls) == 1 and len(capacity.releases) == 1
    assert "private SDK error" not in str(caught.value) + repr(caught.value)
    assert getattr(gateway_module, "GatewayRejectedOutput", None) is type(caught.value)


async def test_sdk_cancellation_closes_client_and_releases_without_rejection() -> None:
    gateway, capacity, calls, clients, entered = harness(wire(), wait=asyncio.Event())
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1 and len(capacity.releases) == 1
    assert all(client.is_closed for client in clients)
    assert not capacity.records


@pytest.mark.parametrize("status_code,message", [
    (None, "bad output"), (400, "unsupported parameter max_completion_tokens"), (404, "bad output"),
])
async def test_stream_response_contract_error_is_never_compatibility_or_gateway_retry(
    status_code: int | None, message: str,
) -> None:
    client, factory, _, chat, close = setup_client()
    chat.side_effect = ModelResponseError(message, status_code=status_code)
    primary = deployment(api_base="https://provider.example", quota_scope_id="scope-native")
    backup = deployment(id="backup", logical_model="backup", quota_scope_id="scope-backup")
    capacity = CapacityStub([lease("native"), lease("backup")])
    gateway = ModelGateway(registry=ModelRegistry([primary, backup]), capacity_pool=capacity,
                           secret_resolver=SecretStub(capacity.events), transport=client,
                           fallbacks={"primary": "backup"})
    with pytest.raises(GatewayRejectedOutput) as caught:
        _ = [event async for event in gateway.stream_openai_compatible_events(request(response_schema=None))]
    assert caught.value.evidence is None
    assert caught.value.deployment_id == "native"
    assert chat.await_count == 1 and factory.call_count == 1
    close.assert_awaited_once()
    assert len(capacity.releases) == 1


@pytest.mark.parametrize("text", [
    '```json\n{"verdict":"approve"}\n```', '<think>private</think>{"verdict":"approve"}',
    '{"verdict":"approve","verdict":"approve"}', '{"value":NaN}', '{"value":1e999}',
])
async def test_strict_json_rejection_retains_original_without_extraction(text: str) -> None:
    gateway, _, calls, _, _ = harness(wire(text))
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    evidence = caught.value.evidence
    assert evidence is not None and evidence.final_text == text
    assert evidence.reason == "invalid_json" and evidence.correction_eligible
    assert len(calls) == 1


async def test_mixed_tool_and_text_is_not_a_repair_candidate() -> None:
    payload = wire()
    payload["output"].append({"type": "function_call", "call_id": "call1", "name": "read", "arguments": "{}"})
    gateway, _, _, _, _ = harness(payload)
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    assert caught.value.evidence is not None
    assert not caught.value.evidence.correction_eligible
    assert caught.value.evidence.final_text is None


async def test_rejection_after_capacity_fallback_reports_actual_selected_identity() -> None:
    from agent_hub.models.capacity import CapacityWaitTimeout

    gateway, capacity, calls, _, _ = harness(wire())
    capacity.outcomes = [CapacityWaitTimeout("busy"), lease("backup")]
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    assert caught.value.deployment_id == "backup" and caught.value.logical_model == "backup"
    assert caught.value.fallback_used and caught.value.fallback_from_logical_model == "primary"
    assert caught.value.fallback_reason == "capacity_unavailable"
    assert caught.value.attempted_logical_models == ("primary", "backup")
    assert len(calls) == 1


async def test_rejection_uses_deployment_pricing_when_gateway_override_is_absent() -> None:
    from dataclasses import replace

    gateway, _, _, _, _ = harness(wire())
    gateway._registry = ModelRegistry([
        replace(dep, input_per_million_usd=Decimal(1), output_per_million_usd=Decimal(2))
        for dep in gateway._registry.deployments
    ])
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    assert caught.value.cost_usd == Decimal("0.000246")


async def test_valid_json_with_missing_usage_remains_rejected_and_not_repairable() -> None:
    gateway, _, _, _, _ = harness(wire('{"verdict":"approve"}', usage=None))
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    evidence = caught.value.evidence
    assert evidence is not None and evidence.usage_status == "missing"
    assert evidence.reason == "usage_missing" and not evidence.correction_eligible
    assert caught.value.cost_usd is None


async def test_cancellation_during_rejected_lease_cleanup_cannot_offer_correction() -> None:
    gateway, capacity, calls, clients, _ = harness(wire())
    capacity.release_block = asyncio.Event()
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(capacity.release_started.wait(), timeout=5)
    task.cancel()
    capacity.release_block.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1 and len(capacity.releases) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("status", [None, "failed", "queued", "cancelled"])
async def test_unknown_outcome_is_not_reported_as_completed_or_incomplete(status: str | None) -> None:
    gateway, _, _, _, _ = harness(wire(status=status))
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    evidence = caught.value.evidence
    assert evidence is not None and evidence.status == "unknown"
    assert evidence.final_text is None and not evidence.correction_eligible
    assert evidence.usage == TokenUsage(12, 117, 129)


def test_evidence_bearing_error_does_not_render_original_message() -> None:
    evidence_type = model_types.RejectedOutputEvidence
    evidence = evidence_type(final_text=PRIVATE, usage=None, usage_status="missing",
                             status="completed", reason="invalid_json")
    error = ModelResponseError(PRIVATE, evidence=evidence)
    assert error.evidence is evidence
    assert PRIVATE not in str(error) + repr(error) + repr(evidence)


async def test_cancellation_during_rejected_outcome_recording_cannot_offer_correction() -> None:
    gateway, capacity, calls, _, _ = harness(wire())
    entered = asyncio.Event()

    async def record(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Future()

    capacity.record_outcome = record  # type: ignore[method-assign]
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1 and len(capacity.releases) == 1


async def test_rejected_outcome_recording_timeout_preserves_rejected_receipt() -> None:
    gateway, capacity, calls, _, _ = harness(wire(), capacity_wait_timeout=0.05)
    entered = asyncio.Event()

    async def record(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Future()

    capacity.record_outcome = record  # type: ignore[method-assign]
    with pytest.raises(GatewayRejectedOutput) as caught:
        await asyncio.wait_for(gateway.complete_with_context(request()), timeout=1)

    evidence = caught.value.evidence
    assert evidence is not None and evidence.final_text == PRIVATE
    assert len(calls) == 1 and len(capacity.releases) == 1
    assert entered.is_set()


async def test_rejected_outcome_release_timeout_preserves_rejected_receipt() -> None:
    gateway, capacity, calls, _, _ = harness(wire(), capacity_wait_timeout=0.05)
    capacity.release_block = asyncio.Event()

    with pytest.raises(GatewayRejectedOutput) as caught:
        await asyncio.wait_for(gateway.complete_with_context(request()), timeout=1)

    evidence = caught.value.evidence
    assert evidence is not None and evidence.final_text == PRIVATE
    assert len(calls) == 1 and len(capacity.releases) == 1


def cancellation_receipt(
    error: asyncio.CancelledError, *, valid: bool,
) -> GatewayCompletion | GatewayRejectedOutput:
    assert type(error).__name__ == "GatewayResponseCancelled"
    receipt = getattr(error, "receipt", None)
    assert isinstance(receipt, GatewayCompletion if valid else GatewayRejectedOutput)
    assert receipt.deployment_id == "native" and receipt.logical_model == "primary"
    assert receipt.provider_model == "provider/original" and receipt.provider_id == "provider"
    assert receipt.attempted_logical_models == ("primary",) and not receipt.fallback_used
    if valid:
        assert isinstance(receipt, GatewayCompletion)
        assert receipt.response.usage == TokenUsage(12, 117, 129)
        assert receipt.response.text == '{"verdict":"approve"}'
    else:
        assert isinstance(receipt, GatewayRejectedOutput)
        assert receipt.evidence is not None and receipt.evidence.usage == TokenUsage(12, 117, 129)
        assert receipt.evidence.final_text == PRIVATE
        assert receipt.evidence.text_sha256 == hashlib.sha256(PRIVATE.encode()).hexdigest()
    assert PRIVATE not in repr(error) + str(error) + repr(receipt)
    assert '{"verdict"' not in repr(error) + str(error) + repr(receipt)
    return receipt


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize("boundary", ["close", "record", "release"])
@pytest.mark.parametrize("repeat", [False, True])
async def test_cancelled_received_response_preserves_receipt_and_releases(
    valid: bool, boundary: str, repeat: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()
    gateway, capacity, calls, clients, _ = harness(
        wire('{"verdict":"approve"}' if valid else PRIVATE),
        close_started=entered if boundary == "close" else None,
        close_gate=gate if boundary == "close" else None,
    )
    if boundary == "record":
        async def record(*args: Any, **kwargs: Any) -> None:
            entered.set()
            await asyncio.Future()
        capacity.record_outcome = record  # type: ignore[method-assign]
        capacity.release_block = gate
    elif boundary == "release":
        capacity.release_block = gate
        entered = capacity.release_started
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel("private cancellation reason")
    if boundary == "record":
        await asyncio.wait_for(capacity.release_started.wait(), timeout=5)
    else:
        await asyncio.sleep(0)
    if repeat:
        task.cancel("second private cancellation")
        await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=5)
    receipt = cancellation_receipt(caught.value, valid=valid)
    assert receipt.cost_usd is None
    assert "private cancellation" not in str(caught.value) + repr(caught.value) + caplog.text
    assert len(calls) == 1 and len(capacity.releases) == 1
    if boundary != "record":
        assert len(capacity.records) == 1
        assert capacity.records[0][-1] is valid
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize("repeat", [False, True])
async def test_cancelled_heartbeat_drain_preserves_finished_transport_receipt(
    valid: bool, repeat: bool,
) -> None:
    response_gate, heartbeat_gate = asyncio.Event(), asyncio.Event()
    started, draining = asyncio.Event(), asyncio.Event()
    gateway, capacity, calls, clients, _ = harness(
        wire('{"verdict":"approve"}' if valid else PRIVATE), wait=response_gate,
    )

    async def heartbeat(*args: Any) -> ModelResponse:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            draining.set()
            await heartbeat_gate.wait()
            raise
        raise AssertionError("heartbeat must not return")

    gateway._heartbeat = heartbeat  # type: ignore[method-assign, assignment]
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(started.wait(), timeout=5)
    response_gate.set()
    await asyncio.wait_for(draining.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    if repeat:
        task.cancel()
        await asyncio.sleep(0)
    heartbeat_gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=5)
    cancellation_receipt(caught.value, valid=valid)
    assert len(calls) == 1 and len(capacity.releases) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize("price", [Decimal(0), Decimal(2)])
async def test_cancelled_receipt_prices_actual_usage_not_success_default(
    valid: bool, price: Decimal,
) -> None:
    gateway, capacity, _, _, _ = harness(
        wire('{"verdict":"approve"}' if valid else PRIVATE),
        pricing={"native": DeploymentPricing(price, price)},
    )
    capacity.release_block = asyncio.Event()
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(capacity.release_started.wait(), timeout=5)
    task.cancel()
    capacity.release_block.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    receipt = cancellation_receipt(caught.value, valid=valid)
    assert receipt.cost_usd == Decimal(129) * price / Decimal(1_000_000)


@pytest.mark.parametrize("valid", [False, True])
async def test_transport_close_cancellation_exposes_only_actual_receipt(valid: bool) -> None:
    from tests.contracts.test_responses_client import message, response

    text = '{"verdict":"approve"}' if valid else PRIVATE
    client, _, native, _, close = setup_client(response(output=[message(text)]))
    entered, gate = asyncio.Event(), asyncio.Event()

    async def closing() -> None:
        entered.set()
        await gate.wait()

    close.side_effect = closing
    task = asyncio.create_task(client.complete(deployment(), request(), "test-key"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert type(caught.value).__name__ == "ModelResponseCancelled"
    receipt = getattr(caught.value, "receipt", None)
    assert isinstance(receipt, ModelResponse if valid else RejectedOutputEvidence)
    assert receipt.usage == TokenUsage(12, 117, 129)
    assert text not in repr(caught.value) + str(caught.value)
    assert native.await_count == 1 and close.await_count == 1


def test_cancellation_interfaces_validate_receipt_types() -> None:
    transport_cancelled = getattr(transport_module, "ModelResponseCancelled", None)
    gateway_cancelled = getattr(gateway_module, "GatewayResponseCancelled", None)
    assert transport_cancelled is not None and gateway_cancelled is not None
    for cls in (transport_cancelled, gateway_cancelled):
        assert issubclass(cls, asyncio.CancelledError)
        assert not issubclass(cls, Exception)
        with pytest.raises(TypeError):
            cls(receipt="not a receipt")


async def test_response_before_cancel_is_unknown_when_provider_has_not_returned() -> None:
    gateway, capacity, calls, clients, entered = harness(wire(), wait=asyncio.Event())
    task = asyncio.create_task(gateway.complete_with_context(request()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not hasattr(caught.value, "receipt")
    assert len(calls) == 1 and len(capacity.releases) == 1
    assert capacity.records == []
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("protocol", ["chat", "messages"])
async def test_other_complete_protocols_keep_parsed_receipt_on_close_cancellation(protocol: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from tests.contracts.test_litellm_client import sdk_response

    entered, gate = asyncio.Event(), asyncio.Event()

    async def closing() -> None:
        entered.set()
        await gate.wait()

    close = AsyncMock(side_effect=closing)
    chat = AsyncMock(return_value=sdk_response(content=PRIVATE))
    http = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {
        "content": [{"type": "text", "text": PRIVATE}],
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }))
    factory = MagicMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat)), close=close,
    ))
    http_factory = MagicMock(return_value=SimpleNamespace(post=http, aclose=close))
    client = LiteLLMClient(client_factory=factory, http_client_factory=http_factory)
    dep = deployment(api_base="https://provider.example/v1/messages") if protocol == "messages" else deployment()
    task = asyncio.create_task(client.complete(dep, request(response_schema=None), "test-key"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert type(caught.value).__name__ == "ModelResponseCancelled"
    receipt = getattr(caught.value, "receipt", None)
    assert isinstance(receipt, ModelResponse) and receipt.text == PRIVATE
    assert receipt.usage == TokenUsage(2, 3, 5)
    assert chat.await_count + http.await_count == 1 and close.await_count == 1


@pytest.mark.parametrize("valid", [False, True])
async def test_cancellation_after_invocation_done_before_lease_receives_result(valid: bool) -> None:
    gateway, capacity, calls, _, _ = harness(wire('{"verdict":"approve"}' if valid else PRIVATE))
    original = gateway._invoke_with_heartbeat

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        asyncio.get_running_loop().call_soon(task.cancel)
        return result

    gateway._invoke_with_heartbeat = invoke  # type: ignore[method-assign]
    task = asyncio.create_task(gateway.complete_with_context(request()))
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    cancellation_receipt(caught.value, valid=valid)
    assert len(calls) == 1 and len(capacity.releases) == 1


async def test_local_schema_configuration_error_never_pays_for_chat_fallback() -> None:
    client, factory, native, chat, _ = setup_client()
    primary = deployment(quota_scope_id="scope-native")
    backup = deployment(id="backup", logical_model="backup", quota_scope_id="scope-backup",
                        structured_output_api="chat_completions")
    capacity = CapacityStub([lease("native"), lease("backup")])
    gateway = ModelGateway(registry=ModelRegistry([primary, backup]), capacity_pool=capacity,
                           secret_resolver=SecretStub(capacity.events), transport=client,
                           fallbacks={"primary": "backup"})
    invalid = request(response_schema=StructuredResponseSchema(
        name="Invalid", schema={"type": "string", "format": "private-format"},
    ))
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(invalid)
    assert caught.value.evidence is None and caught.value.cost_usd is None
    assert caught.value.deployment_id == "native"
    assert caught.value.attempted_logical_models == ("primary",)
    assert len(capacity.releases) == 1
    factory.assert_not_called()
    native.assert_not_called()
    chat.assert_not_called()


@pytest.mark.parametrize("branch", ["chat", "legacy", "root", "messages", "native", "stream"])
@pytest.mark.parametrize("with_evidence", [False, True])
async def test_direct_response_error_is_redacted_with_status_and_evidence_preserved(
    branch: str, with_evidence: bool,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from agent_hub.models.litellm_client import ModelTransportError
    from tests.unit.models.test_gateway import AsyncChunkStream

    evidence = RejectedOutputEvidence(
        final_text=PRIVATE, usage=TokenUsage(12, 117, 129), usage_status="known",
        status="completed", reason="invalid_json",
    ) if with_evidence else None
    failure = ModelResponseError(PRIVATE, status_code=503, evidence=evidence)
    client, factory, native, chat, close = setup_client()
    dep = deployment()
    req = request(response_schema=None)
    if branch == "messages":
        post = AsyncMock(side_effect=failure)
        client = LiteLLMClient(http_client_factory=MagicMock(return_value=SimpleNamespace(
            post=post, aclose=close,
        )))
        dep = deployment(api_base="https://provider.example/v1/messages")
    elif branch == "native":
        req = request()
        native.side_effect = failure
    elif branch == "stream":
        chat.return_value = AsyncChunkStream([failure])
    elif branch == "legacy":
        chat.side_effect = [RuntimeError("unsupported parameter max_completion_tokens"), failure]
    elif branch == "root":
        dep = deployment(api_base="https://provider.example")
        chat.side_effect = [ModelTransportError("not found", status_code=404), failure]
    else:
        chat.side_effect = failure
    with pytest.raises(ModelResponseError) as caught:
        if branch == "stream":
            _ = [item async for item in client.stream_openai_compatible_chunks(dep, req, "test-key")]
        else:
            await client.complete(dep, req, "test-key")
    assert PRIVATE not in str(caught.value) + repr(caught.value)
    assert caught.value.status_code == 503 and caught.value.evidence is evidence
    assert close.await_count >= 1
    if branch not in {"messages", "native"}:
        assert chat.await_count == (2 if branch in {"legacy", "root"} else 1)
        assert factory.call_count == (2 if branch == "root" else 1)


@pytest.mark.parametrize("valid", [False, True])
async def test_sdk_close_cancelled_receipt_records_before_release_once(valid: bool) -> None:
    from tests.contracts.test_responses_client import message, response

    client, _, native, _, close = setup_client(response(output=[
        message('{"verdict":"approve"}' if valid else PRIVATE),
    ]))
    close.side_effect = asyncio.CancelledError()
    primary = deployment(quota_scope_id="scope-native")
    capacity = CapacityStub([lease("native")])
    original_release = capacity.release

    async def release(lease: Any) -> bool:
        assert len(capacity.records) == 1
        assert capacity.records[0][-1] is valid
        return await original_release(lease)

    capacity.release = release  # type: ignore[method-assign]
    gateway = ModelGateway(registry=ModelRegistry([primary]), capacity_pool=capacity,
                           secret_resolver=SecretStub(capacity.events), transport=client)
    with pytest.raises(asyncio.CancelledError) as caught:
        await gateway.complete_with_context(request())
    cancellation_receipt(caught.value, valid=valid)
    assert len(capacity.records) == 1 and capacity.records[0][-1] is valid
    assert len(capacity.releases) == 1 and native.await_count == 1 and close.await_count == 1
