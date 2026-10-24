"""Unsatisfiable token reservations must not spend a model's whole deadline."""
import asyncio
from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.models.capacity import (
    CapacityRequestTooLarge,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import ModelGateway, get_gateway_scope_diagnostic
from agent_hub.models.registry import ModelRegistry
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.direct import _retryable_gateway_failure
from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    StreamingTransportStub,
    TransportStub,
    deployment,
    lease,
    request,
)


@pytest.mark.parametrize("streaming", [False, True])
async def test_request_token_limit_skips_to_configured_fallback(streaming: bool) -> None:
    primary, backup = deployment("primary-key"), deployment("backup-key", "backup")
    capacity = CapacityStub([CapacityRequestTooLarge("private scope"), lease("backup-key")])
    transport = (StreamingTransportStub(capacity.events, [{"choices": [{"delta": {"content": "ok"}}]}])
                 if streaming else TransportStub(capacity.events))
    gateway = ModelGateway(ModelRegistry([primary, backup]), capacity, SecretStub(capacity.events),
                           transport, fallbacks={"primary": "backup"})
    async with asyncio.timeout(0.5):
        if streaming:
            events = [event async for event in gateway.stream_openai_compatible_events(request())]
            assert any(event.kind == "model.text_delta" for event in events)
            assert isinstance(transport, StreamingTransportStub)
            assert [call[0].id for call in transport.stream_calls] == ["backup-key"]
        else:
            result = await gateway.complete_with_context(request())
            assert result.logical_model == "backup" and result.fallback_used
            assert result.fallback_reason == "capacity_unavailable"
            assert [call[0].id for call in transport.calls] == ["backup-key"]
    assert len([event for event in capacity.events
                if cast(tuple[object, ...], event)[0] == "acquire"]) == 2
    assert [value.deployment_id for value in capacity.releases] == ["backup-key"]


@pytest.mark.parametrize("streaming", [False, True])
async def test_all_request_token_limits_fail_without_retrying_capacity(streaming: bool) -> None:
    capacity = CapacityStub([CapacityRequestTooLarge("private scope")])
    transport = StreamingTransportStub(capacity.events, []) if streaming else TransportStub(capacity.events)
    gateway = ModelGateway(ModelRegistry([deployment("primary-key")]), capacity,
                           SecretStub(capacity.events), transport)
    async with asyncio.timeout(0.5):
        with pytest.raises(CapacityRequestTooLarge) as failure:
            if streaming:
                _events = [event async for event in gateway.stream_openai_compatible_events(request())]
            else:
                await gateway.complete_with_context(request())
    assert len([event for event in capacity.events
                if cast(tuple[object, ...], event)[0] == "acquire"]) == 1
    assert not transport.calls and not capacity.releases
    if not streaming:
        scope = get_gateway_scope_diagnostic(failure.value)
        assert scope is not None and scope.phase.value == "pretransport_capacity"
        assert scope.transport_entered_count == 0


@pytest.mark.parametrize("streaming", [False, True])
async def test_busy_fallback_is_not_misclassified_as_permanent_token_limit(streaming: bool) -> None:
    capacity = CapacityStub([CapacityRequestTooLarge("private scope"), CapacityWaitTimeout("busy")])
    transport = StreamingTransportStub(capacity.events, []) if streaming else TransportStub(capacity.events)
    gateway = ModelGateway(ModelRegistry([deployment("primary-key"), deployment("backup-key", "backup")]),
                           capacity, SecretStub(capacity.events), transport, fallbacks={"primary": "backup"})
    model_request = replace(request(), timeout_seconds=0.02)
    async with asyncio.timeout(0.5):
        with pytest.raises(CapacityUnavailable):
            if streaming:
                _events = [event async for event in gateway.stream_openai_compatible_events(model_request)]
            else:
                await gateway.complete_with_context(model_request)
    assert not transport.calls and not capacity.releases


def test_capacity_token_limit_has_safe_nonretryable_diagnostic() -> None:
    diagnostic = safe_runtime_failure_diagnostic(CapacityRequestTooLarge("Authorization: Bearer private scope"))
    assert diagnostic["error_code"] == "model.capacity_request_too_large"
    assert diagnostic["error_stage"] == "model_capacity"
    assert diagnostic["retryable"] is False
    assert not _retryable_gateway_failure(CapacityRequestTooLarge())
    assert "private" not in str(diagnostic) and "Authorization" not in str(diagnostic)
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                     reason=str(diagnostic["error_summary"]), payload=diagnostic)
    assert RunEvent.from_payload(event.to_payload()) == event


@pytest.mark.parametrize("streaming", [False, True])
async def test_request_token_limit_does_not_override_disabled_fallback(streaming: bool) -> None:
    capacity = CapacityStub([CapacityRequestTooLarge("private scope"), lease("backup-key")])
    transport = StreamingTransportStub(capacity.events, []) if streaming else TransportStub(capacity.events)
    gateway = ModelGateway(ModelRegistry([deployment("primary-key"), deployment("backup-key", "backup")]),
                           capacity, SecretStub(capacity.events), transport, fallbacks={"primary": "backup"})
    with pytest.raises(CapacityRequestTooLarge):
        if streaming:
            _events = [event async for event in gateway.stream_openai_compatible_events(request(allow_fallback=False))]
        else:
            await gateway.complete_with_context(request(allow_fallback=False))
    assert len([event for event in capacity.events
                if cast(tuple[object, ...], event)[0] == "acquire"]) == 1
    assert not transport.calls and not capacity.releases
