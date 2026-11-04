"""A diagnostic must not turn an unknown provider outcome into a retry receipt."""
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from agent_hub.models.capacity import CapacityQueueFull
from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import (
    GatewayScopeDiagnostic,
    get_gateway_scope_diagnostic,
    preserve_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import ModelTransportError, safe_model_client_error
from agent_hub.runtime.model_scope import ModelScopeTracker
from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
from tests.unit.models.test_gateway import CapacityStub, lease, request
from tests.unit.runtime.test_model_scope import receive


@pytest.mark.parametrize("error_type,kind", [
    (httpx.ReadTimeout, "read_timeout"),
    (httpx.ConnectTimeout, "connect_timeout"),
    (httpx.WriteTimeout, "write_timeout"),
    (httpx.PoolTimeout, "pool_timeout"),
    (httpx.ConnectError, "connect_error"),
    (httpx.ReadError, "read_error"),
    (httpx.RemoteProtocolError, "remote_protocol_error"),
])
@pytest.mark.parametrize("already_sanitized", [False, True])
async def test_statusless_transport_keeps_kind_but_never_completes_scope(
    error_type: type[Exception], kind: str, already_sanitized: bool,
) -> None:
    private = "Authorization: private-secret https://private.invalid/source"
    raw = error_type(private)
    error = safe_model_client_error("primary", raw, ()) if already_sanitized else raw
    capacity = CapacityStub([lease("primary")])
    transport = OutcomesTransport([error])
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    receive(tracker)
    index = tracker.begin()
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(transport, capacity).complete_with_context(
            replace(request(), allow_fallback=False))
    tracker.failed(index, caught.value)
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None and scope.reason.value == "missing_status"
    detail = getattr(scope, "transport_failure", None)
    assert detail is not None, "statusless transport cause was discarded"
    assert detail.kind.value == kind
    assert tracker.diagnostic_payload["first_incomplete_transport_kind"] == kind
    assert tracker.diagnostic_payload["first_incomplete_call"] == 2
    assert tracker.diagnostic_payload["recorded_call_count"] == 1
    assert tracker.incomplete and not tracker.artifacts()
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None and not receipt.history_complete
    assert receipt.attempts[0].status_code is None and receipt.attempts[0].usage is None
    assert caught.value.status_code is None
    assert caught.value.args == ("model transport failed",)
    assert transport.models == ["primary"]
    assert len(capacity.records) == len(capacity.releases) == 1
    wrapper = RuntimeError("existing runtime failure")
    preserve_gateway_scope_diagnostic(wrapper, caught.value)
    assert get_gateway_scope_diagnostic(wrapper) is scope
    assert get_gateway_failure_receipt(wrapper) is None
    assert "private-secret" not in repr(scope)
    assert "private-secret" not in repr(tracker.diagnostic_payload)
    assert "private.invalid" not in repr(scope)


async def test_unknown_statusless_cause_does_not_guess_from_message_or_attributes() -> None:
    error = ModelTransportError("read_timeout ConnectTimeout")
    error.__dict__["transport_failure"] = {"kind": "read_timeout"}
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(OutcomesTransport([error]), CapacityStub([lease("primary")])) \
            .complete_with_context(replace(request(), allow_fallback=False))
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None and getattr(scope, "transport_failure", None) is None
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    tracker.failed(tracker.begin(), caught.value)
    assert "first_incomplete_transport_kind" not in tracker.diagnostic_payload


async def test_earlier_capacity_failure_cannot_be_misattributed_to_later_network_error() -> None:
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(OutcomesTransport([httpx.ReadTimeout("private")]),
                           CapacityStub([CapacityQueueFull("private"), lease("backup")]),
                           models=("primary", "backup")).complete_with_context(request())
    scope = get_gateway_scope_diagnostic(caught.value)
    assert scope is not None and scope.phase.value == "pretransport_capacity"
    assert getattr(scope, "transport_failure", None) is None


async def test_parallel_statusless_failures_do_not_exchange_diagnostics() -> None:
    import asyncio

    async def fail(error: Exception) -> GatewayScopeDiagnostic:
        with pytest.raises(ModelTransportError) as caught:
            await make_gateway(OutcomesTransport([error]), CapacityStub([lease("primary")])) \
                .complete_with_context(replace(request(), allow_fallback=False))
        diagnostic = get_gateway_scope_diagnostic(caught.value)
        assert diagnostic is not None
        return diagnostic

    read, protocol = await asyncio.gather(fail(httpx.ReadTimeout("private-a")),
                                          fail(httpx.RemoteProtocolError("private-b")))
    assert read.transport_failure is not None and read.transport_failure.kind.value == "read_timeout"
    assert protocol.transport_failure is not None
    assert protocol.transport_failure.kind.value == "remote_protocol_error"


@pytest.mark.parametrize("error_type,kind", [
    (httpx.ReadTimeout, "read_timeout"),
    (httpx.RemoteProtocolError, "remote_protocol_error"),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_real_sdk_boundary_retains_unknown_hold_and_does_not_retry(
    error_type: type[Exception], kind: str, stream: bool,
) -> None:
    from agent_hub.models import physical_budget as budget
    from tests.unit.models.test_physical_budget import configured, gateway, logical_request

    context, store, selected, _ = configured()
    wires = 0

    def fail_wire(request: httpx.Request) -> httpx.Response:
        nonlocal wires
        wires += 1
        raise error_type("private provider or project body")

    gw, capacity = gateway([selected], fail_wire)
    with budget._bind_run_budget(context), pytest.raises(ModelTransportError) as caught:
        if stream:
            events = gw.stream_openai_compatible_events(logical_request(allow_fallback=False))
            try:
                await anext(events)
            finally:
                await events.aclose()
        else:
            await gw.complete_with_context(logical_request(allow_fallback=False))
    assert wires == 1 and len(store.attempts) == 1
    attempt = next(iter(store.attempts.values()))
    assert attempt.state == "unknown" and attempt.physical_transport_id is not None
    assert store.charged == attempt.token_hold and not store.closed
    assert len(capacity.releases) == 1
    scope = get_gateway_scope_diagnostic(caught.value)
    if not stream:
        assert scope is not None and scope.reason.value == "missing_status"
        assert scope.transport_failure is not None and scope.transport_failure.kind.value == kind
    from agent_hub.models.transport_diagnostics import get_transport_diagnostic

    detail = get_transport_diagnostic(caught.value)
    assert detail is not None and detail.kind.value == kind
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None and not receipt.history_complete
