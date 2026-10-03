import asyncio
from dataclasses import FrozenInstanceError, replace
from typing import Any

import httpx
import pytest

from agent_hub.models import gateway as gateway_module
from agent_hub.models.capacity import CapacityBackendError, CapacityQueueFull
from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import ModelGatewayError
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelResponse, TokenUsage
from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
from tests.unit.models.test_gateway import CapacityStub, SecretStub, TransportStub, lease, request


def diagnostic(value: object) -> Any:
    getter = getattr(gateway_module, "get_gateway_scope_diagnostic", None)
    assert callable(getter), "trusted gateway scope diagnostic is missing"
    return getter(value)


@pytest.mark.parametrize("stage,phase,reason,entered,attempts", [
    ("capacity", "pretransport_capacity", "capacity_backend_failure", 0, 0),
    ("queue", "pretransport_capacity", "capacity_unavailable", 0, 0),
    ("credential", "pretransport_credentials", "credential_resolution_failed", 0, 0),
    ("credential_timeout", "outer_deadline", "deadline_exhausted", 0, 0),
    ("transport_timeout", "outer_deadline", "deadline_exhausted", 1, 0),
    ("release", "cleanup", "release_failed", 1, 1),
    ("record", "recorder", "outcome_recording_failed", 1, 1),
    ("unknown", "unknown_adapter", "unknown_failure", 1, 0),
    ("missing_status", "transport", "missing_status", 1, 1),
    ("invalid_usage", "recorder", "invalid_usage", 1, 1),
])
async def test_gateway_classifies_local_gap_without_raw_error_text(
    stage: str, phase: str, reason: str, entered: int, attempts: int,
) -> None:
    private = "Authorization: secret https://private.invalid/private prompt"
    capacity = CapacityStub([lease("primary")])
    if stage in {"capacity", "queue"}:
        capacity.outcomes = [CapacityBackendError(private) if stage == "capacity"
                             else CapacityQueueFull(private)]
    if stage == "release":
        capacity.release_error = CapacityBackendError(private)
    if stage == "record":
        capacity.record_error = CapacityBackendError(private)
    block = asyncio.Event()
    secret = SecretStub(capacity.events,
                        failure=RuntimeError(private) if stage == "credential" else None,
                        block=block if stage == "credential_timeout" else None)
    transport = TransportStub(
        capacity.events,
        response=ModelResponse(text="", usage=TokenUsage(1, 2, 99) if stage == "invalid_usage"
                               else TokenUsage(2, 0, 2)),
        failure=RuntimeError(private) if stage == "unknown" else
        ModelTransportError(private) if stage == "missing_status" else None,
        block=block if stage == "transport_timeout" else None,
    )
    timeout = 0.03 if stage in {"credential_timeout", "transport_timeout"} else 1
    with pytest.raises(Exception) as caught:
        await make_gateway(transport, capacity, secret=secret).complete_with_context(
            replace(request(allow_fallback=False), timeout_seconds=timeout),
        )
    result = diagnostic(caught.value)
    assert result is not None
    assert result.phase.value == phase and result.reason.value == reason
    assert result.transport_entered_count == entered
    assert result.failure_attempt_count == attempts
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is None or not receipt.history_complete
    with pytest.raises(FrozenInstanceError):
        result.reason = "private body"


async def test_first_capacity_gap_survives_fallback_and_later_cleanup_failure() -> None:
    capacity = CapacityStub([CapacityQueueFull("private"), lease("backup")])
    capacity.release_error = CapacityBackendError("private release")
    with pytest.raises(ModelGatewayError) as caught:
        await make_gateway(OutcomesTransport([ModelResponse(text="")]), capacity,
                           models=("primary", "backup")).complete_with_context(request())
    result = diagnostic(caught.value)
    assert result.phase.value == "pretransport_capacity"
    assert result.reason.value == "capacity_unavailable"
    assert result.transport_entered_count == result.failure_attempt_count == 1
    assert not get_gateway_failure_receipt(caught.value).history_complete  # type: ignore[union-attr]


async def test_successful_fallback_keeps_incomplete_diagnostic_without_changing_response() -> None:
    completion = await make_gateway(
        OutcomesTransport([ModelResponse(text="accepted")]),
        CapacityStub([CapacityQueueFull("private"), lease("backup")]),
        models=("primary", "backup"),
    ).complete_with_context(request())
    assert completion.response.text == "accepted"
    result = diagnostic(completion)
    assert result.phase.value == "pretransport_capacity"
    assert result.transport_entered_count == 1 and result.failure_attempt_count == 0


async def test_provider_408_is_complete_transport_evidence_not_local_deadline() -> None:
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(OutcomesTransport([ModelTransportError("deadline exhausted", status_code=408)]),
                           CapacityStub([lease("primary")])).complete_with_context(request())
    assert diagnostic(caught.value) is None
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None and receipt.history_complete


async def test_gateway_cancellation_is_diagnostic_only_and_remains_cancelled() -> None:
    capacity = CapacityStub([lease("primary")])
    started = asyncio.Event()

    class BlockingTransport(TransportStub):
        async def complete(self, *args: Any) -> ModelResponse:
            started.set()
            return await super().complete(*args)

    transport = BlockingTransport(capacity.events, block=asyncio.Event())
    pending = asyncio.create_task(make_gateway(transport, capacity).complete_with_context(request()))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await pending
    result = diagnostic(caught.value)
    assert result.phase.value == "cancellation" and result.reason.value == "cancelled"
    assert get_gateway_failure_receipt(caught.value) is None


def test_diagnostic_rejects_forged_attributes_and_hostile_properties() -> None:
    error = ModelTransportError("outer deadline cleanup recorder", status_code=408)
    error.scope_diagnostic = {"phase": "outer_deadline"}  # type: ignore[attr-defined]
    error._gateway_scope_diagnostic = (object(), error, {"phase": "cleanup"})  # type: ignore[attr-defined]
    assert diagnostic(error) is None

    class Hostile(RuntimeError):
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError("adapter properties must not be consulted")

    assert diagnostic(Hostile()) is None


async def test_sdk_http_status_keeps_existing_receipt_semantics() -> None:
    upstream = httpx.Response(408, request=httpx.Request("POST", "https://private.invalid"))
    failure = httpx.HTTPStatusError("private request", request=upstream.request, response=upstream)
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(OutcomesTransport([failure]), CapacityStub([lease("primary")])) \
            .complete_with_context(request())
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None and receipt.history_complete
    assert diagnostic(caught.value) is None


async def test_replaced_diagnostic_and_replayed_exception_binding_are_not_issued() -> None:
    with pytest.raises(Exception) as caught:
        await make_gateway(OutcomesTransport([]), CapacityStub([CapacityQueueFull("private")])) \
            .complete_with_context(request())
    issued = diagnostic(caught.value)
    forged = replace(issued, reason=gateway_module.ScopeIncompleteReason.UNKNOWN_FAILURE)
    completion = gateway_module.GatewayCompletion(
        response=ModelResponse(text="accepted"), deployment_id="primary", logical_model="primary",
        provider_id="provider", provider_model="provider/primary", scope_diagnostic=forged,
    )
    assert diagnostic(completion) is None
    other = RuntimeError("private")
    other.__dict__.update(caught.value.__dict__)
    assert diagnostic(other) is None


async def test_receipt_issuance_failure_has_bounded_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_issuance(*args: object) -> None:
        raise ValueError("private receipt stack")

    monkeypatch.setattr(gateway_module, "_attach_gateway_failure_receipt", fail_issuance)
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(OutcomesTransport([ModelTransportError("private", status_code=408)]),
                           CapacityStub([lease("primary")])).complete_with_context(request())
    result = diagnostic(caught.value)
    assert result.phase.value == "recorder" and result.reason.value == "receipt_issuance_failed"
    assert get_gateway_failure_receipt(caught.value) is None


@pytest.mark.parametrize("stage", ["record", "release"])
async def test_post_response_failure_does_not_invent_failure_attempt(stage: str) -> None:
    capacity = CapacityStub([lease("primary")])
    if stage == "record":
        capacity.record_error = CapacityBackendError("private")
    else:
        capacity.release_error = CapacityBackendError("private")
    with pytest.raises(ModelGatewayError) as caught:
        await make_gateway(OutcomesTransport([ModelResponse(text="accepted")]), capacity) \
            .complete_with_context(request())
    result = diagnostic(caught.value)
    assert result.phase.value == ("recorder" if stage == "record" else "cleanup")
    assert result.transport_entered_count == 1 and result.failure_attempt_count == 0
    assert get_gateway_failure_receipt(caught.value) is None
