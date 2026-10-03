import asyncio
import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError, replace
from itertools import pairwise
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.models.capacity import CapacityBackendError, CapacityWaitTimeout
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayResponseCancelled,
    ModelGateway,
    ModelGatewayError,
)
from agent_hub.models.litellm_client import ModelResponseCancelled, ModelTransportError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelCapability, ModelResponse, TokenUsage
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    StreamingTransportStub,
    TransportStub,
    deployment,
    lease,
    request,
)


def receipt_api() -> Any:
    name = "agent_hub.models.failure_receipt"
    assert importlib.util.find_spec(name) is not None, "Gateway failure receipt API is missing"
    return importlib.import_module(name)


def make_gateway(
    transport: Any, capacity: CapacityStub, *, secret: SecretStub | None = None,
    models: tuple[str, ...] = ("primary",),
) -> ModelGateway:
    return ModelGateway(
        ModelRegistry([
            deployment(model, model, provider_model=f"provider/{model}") for model in models
        ]),
        capacity,
        secret or SecretStub(capacity.events),
        transport,
        fallbacks=dict(pairwise(models)),
    )


def assert_no_complete_receipt(error: BaseException) -> None:
    receipt = receipt_api().get_gateway_failure_receipt(error)
    assert receipt is None or receipt.history_complete is False


@pytest.mark.parametrize("text", [None, "", " \n"])
@pytest.mark.parametrize("usage", [None, TokenUsage(7, 0, 7), TokenUsage(1, 2, 99)])
async def test_empty_response_retains_actual_scope_and_only_valid_usage(
    text: str | None, usage: TokenUsage | None,
) -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary")])
    transport = TransportStub(capacity.events, response=ModelResponse(text=text, usage=usage))
    gateway = make_gateway(transport, capacity)
    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete_with_context(request(allow_fallback=False))
    assert type(caught.value) is ModelGatewayError
    assert str(caught.value) == (
        "model response is empty" if text is None else "model response text is empty"
    )
    receipt = api.get_gateway_failure_receipt(caught.value)
    assert type(receipt) is api.GatewayFailureReceipt
    payload = receipt.to_payload()
    assert set(payload) == {
        "schema_version", "source", "call_id", "requested_logical_model", "allow_fallback",
        "disposition", "history_complete", "attempted_logical_models", "attempts",
    }
    assert payload["schema_version"] == 1
    assert payload["source"] == "model_gateway"
    assert str(UUID(payload["call_id"])) == payload["call_id"]
    assert payload["requested_logical_model"] == "primary"
    assert payload["allow_fallback"] is False
    assert payload["disposition"] == "failed"
    assert payload["attempted_logical_models"] == ["primary"]
    valid = usage is not None and usage.total_tokens == 7
    assert payload["history_complete"] is (usage is None or valid)
    assert payload["attempts"] == [{
        "ordinal": 1,
        "provenance": {
            "logical_model": "primary", "deployment_id": "primary",
            "provider_id": "provider", "provider_model": "provider/primary",
        },
        "transport_state": "entered", "outcome": "empty_response", "status_code": 200,
        "usage_status": "missing" if usage is None else "known" if valid else "invalid",
        "usage": {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7} if valid else None,
    }]
    assert len(capacity.releases) == 1
    assert len(capacity.records) == 1 and capacity.records[0][3] is False
    encoded = json.dumps(payload)
    assert "private prompt" not in encoded and "secret://" not in encoded
    assert "key-for-" not in encoded and "provider_metadata" not in encoded


@pytest.mark.parametrize("status", [408, 401, None])
async def test_transport_failure_preserves_exception_and_scope_without_response(status: int | None) -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary")])
    transport = TransportStub(
        capacity.events, failure=ModelTransportError("Authorization: private-body", status_code=status),
    )
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(transport, capacity).complete_with_context(request())
    assert type(caught.value) is ModelTransportError
    assert str(caught.value) == "model transport failed"
    assert caught.value.status_code == status
    receipt = api.get_gateway_failure_receipt(caught.value)
    assert receipt is not None
    payload = receipt.to_payload()
    assert payload["history_complete"] is (status is not None)
    assert payload["attempts"][0]["outcome"] == "transport_error"
    assert payload["attempts"][0]["status_code"] == status
    assert payload["attempts"][0]["usage"] is None
    assert payload["attempts"][0]["usage_status"] == "missing"
    assert "private-body" not in json.dumps(payload)
    assert len(capacity.releases) == 1 and capacity.records[0][3] is False


class OutcomesTransport:
    def __init__(self, outcomes: list[ModelResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.models: list[str] = []

    async def complete(self, selected: Any, model_request: Any, api_key: str) -> ModelResponse:
        self.models.append(selected.logical_model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_failed_fallback_receipt_retains_all_actual_and_foreign_attempts() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary"), lease("backup")])
    transport = OutcomesTransport([
        ModelResponse(text="", usage=TokenUsage(4, 0, 4)),
        ModelTransportError("private upstream failure", status_code=408),
    ])
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(transport, capacity, models=("primary", "backup")).complete_with_context(request())
    payload = api.get_gateway_failure_receipt(caught.value).to_payload()
    assert payload["history_complete"] is True
    assert payload["allow_fallback"] is True
    assert payload["attempted_logical_models"] == ["primary", "backup"]
    assert [attempt["ordinal"] for attempt in payload["attempts"]] == [1, 2]
    assert [attempt["provenance"]["logical_model"] for attempt in payload["attempts"]] == ["primary", "backup"]
    assert [attempt["outcome"] for attempt in payload["attempts"]] == ["empty_response", "transport_error"]
    assert len(capacity.releases) == 2 and [record[3] for record in capacity.records] == [False, False]


async def test_successful_fallback_behavior_is_unchanged() -> None:
    receipt_api()
    capacity = CapacityStub([lease("primary"), lease("backup")])
    transport = OutcomesTransport([ModelResponse(text=""), ModelResponse(text="accepted")])
    completion = await make_gateway(
        transport, capacity, models=("primary", "backup"),
    ).complete_with_context(request())
    assert completion.response.text == "accepted"
    assert completion.attempted_logical_models == ("primary", "backup")
    assert completion.fallback_reason == "empty_response"
    assert [record[3] for record in capacity.records] == [False, True]


async def test_conservative_capability_history_is_not_misrepresented_as_transport_calls() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("backup")])
    gateway = ModelGateway(
        ModelRegistry([
            deployment("primary", provider_model="provider/primary", capabilities=frozenset({ModelCapability.VISION})),
            deployment("backup", "backup", provider_model="provider/backup"),
        ]),
        capacity, SecretStub(capacity.events),
        TransportStub(capacity.events, response=ModelResponse(text="")),
        fallbacks={"primary": "backup"},
    )
    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete_with_context(request(required=frozenset({ModelCapability.TEXT})))
    payload = api.get_gateway_failure_receipt(caught.value).to_payload()
    assert payload["attempted_logical_models"] == ["primary", "backup"]
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["provenance"]["logical_model"] == "backup"


@pytest.mark.parametrize("stage", ["credential_error", "credential_timeout", "transport_timeout", "unknown", "capacity", "release", "accounting"])
async def test_unknown_or_pretransport_failures_never_supply_complete_evidence(stage: str) -> None:
    receipt_api()
    capacity = CapacityStub([lease("primary")])
    block = asyncio.Event()
    secret = SecretStub(
        capacity.events,
        failure=RuntimeError("private credential body") if stage == "credential_error" else None,
        block=block if stage == "credential_timeout" else None,
    )
    transport = TransportStub(
        capacity.events,
        response=ModelResponse(text=""),
        block=block if stage == "transport_timeout" else None,
        failure=RuntimeError("private response body") if stage == "unknown" else None,
    )
    if stage == "capacity":
        capacity.outcomes = [CapacityBackendError("safe capacity failure")]
    if stage == "release":
        capacity.release_error = CapacityBackendError("safe release failure")
    if stage == "accounting":
        capacity.record_error = CapacityBackendError("safe accounting failure")
    with pytest.raises((ModelGatewayError, ModelTransportError, CapacityBackendError)) as caught:
        await make_gateway(transport, capacity, secret=secret).complete_with_context(
            replace(request(), timeout_seconds=0.05),
        )
    assert_no_complete_receipt(caught.value)
    if stage.startswith("credential"):
        assert transport.calls == []
    if stage.endswith("timeout"):
        assert type(caught.value) is ModelTransportError
        assert str(caught.value) == "model request deadline exhausted"
        assert caught.value.status_code == 408
    if stage != "capacity":
        assert len(capacity.releases) == 1


async def test_later_capacity_failure_does_not_hide_an_incomplete_fallback_history() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary"), CapacityWaitTimeout("busy")])
    transport = OutcomesTransport([ModelTransportError("failed", status_code=408)])
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(transport, capacity, models=("primary", "backup")).complete_with_context(request())
    receipt = api.get_gateway_failure_receipt(caught.value)
    assert receipt is not None and receipt.history_complete is False
    assert receipt.to_payload()["attempted_logical_models"] == ["primary", "backup"]


@pytest.mark.parametrize("after_response", [False, True])
async def test_cancellation_keeps_accounting_and_cleanup_but_has_no_failure_exemption(after_response: bool) -> None:
    receipt_api()
    capacity = CapacityStub([lease("primary")])
    started = asyncio.Event()

    class Transport:
        async def complete(self, selected: Any, model_request: Any, api_key: str) -> ModelResponse:
            started.set()
            if after_response:
                raise ModelResponseCancelled(receipt=ModelResponse(text="private receipt", usage=TokenUsage(3, 1, 4)))
            await asyncio.Future()
            raise AssertionError("unreachable")

    task = asyncio.create_task(make_gateway(Transport(), capacity).complete_with_context(request()))
    await started.wait()
    if not after_response:
        task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert_no_complete_receipt(caught.value)
    assert len(capacity.releases) == 1
    if after_response:
        assert isinstance(caught.value, GatewayResponseCancelled)
        assert isinstance(caught.value.receipt, GatewayCompletion)
        assert caught.value.receipt.response.usage == TokenUsage(3, 1, 4)
        assert capacity.records[0][3] is True


async def test_concurrent_calls_have_separate_receipts_and_call_ids() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary"), lease("primary")])
    gateway = make_gateway(TransportStub(capacity.events, response=ModelResponse(text="")), capacity)
    outcomes = await asyncio.gather(
        gateway.complete_with_context(request()), gateway.complete_with_context(request()),
        return_exceptions=True,
    )
    receipts = [api.get_gateway_failure_receipt(error) for error in outcomes]
    assert all(receipt is not None and len(receipt.attempts) == 1 for receipt in receipts)
    assert receipts[0].call_id != receipts[1].call_id


async def test_streaming_failure_remains_unsupported() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary")])
    gateway = make_gateway(StreamingTransportStub(capacity.events, []), capacity)
    with pytest.raises(ModelGatewayError) as caught:
        await anext(gateway.stream_openai_compatible_events(request()))
    assert api.get_gateway_failure_receipt(caught.value) is None


async def test_receipt_history_overflow_is_bounded_and_not_complete() -> None:
    api = receipt_api()
    models = tuple(f"model_{index}" for index in range(api.MAX_GATEWAY_FAILURE_ATTEMPTS + 1))
    capacity = CapacityStub([lease(model) for model in models])
    transport = OutcomesTransport([ModelResponse(text="") for _ in models])
    with pytest.raises(ModelGatewayError) as caught:
        await make_gateway(transport, capacity, models=models).complete_with_context(
            replace(request(), logical_model=models[0]),
        )
    receipt = api.get_gateway_failure_receipt(caught.value)
    assert receipt is not None and receipt.history_complete is False
    assert len(receipt.attempts) <= api.MAX_GATEWAY_FAILURE_ATTEMPTS
    assert len(receipt.attempted_logical_models) <= api.MAX_GATEWAY_FAILURE_ATTEMPTS
    assert len(capacity.releases) == len(models)


def valid_attempt(api: Any) -> Any:
    return api.GatewayFailureAttempt(
        ordinal=1, logical_model="primary", deployment_id="primary", provider_id="provider",
        provider_model="provider/primary", outcome="transport_error", status_code=408,
    )


@pytest.mark.parametrize("change", [
    {"ordinal": True}, {"ordinal": 0}, {"logical_model": "private body"},
    {"deployment_id": "a" * 129}, {"provider_id": "foreign"},
    {"provider_model": "provider/model\nprivate"}, {"provider_model": "provider/https://secret"},
    {"status_code": True}, {"status_code": 600}, {"outcome": "completed"},
    {"usage_status": "known"}, {"usage_status": "invalid", "usage": TokenUsage(1, 1, 2)},
    {"usage_status": "known", "usage": TokenUsage(1, 2, 8)},
    {"usage_status": "known", "usage": TokenUsage(10**15, 0, 10**15)},
])
def test_attempt_validation_rejects_invalid_or_sensitive_evidence(change: dict[str, Any]) -> None:
    api = receipt_api()
    with pytest.raises((ValueError, TypeError)) as caught:
        replace(valid_attempt(api), **change)
    assert "private" not in str(caught.value) and "secret" not in str(caught.value)


@pytest.mark.parametrize("change", [
    {"call_id": "invalid"}, {"call_id": str(uuid4()).upper()}, {"allow_fallback": 1},
    {"history_complete": 1}, {"requested_logical_model": "bad scope"},
    {"attempted_logical_models": ()}, {"attempted_logical_models": ("foreign",)},
])
def test_receipt_validation_requires_consistent_bounded_scope(change: dict[str, Any]) -> None:
    api = receipt_api()
    receipt = api.GatewayFailureReceipt(
        call_id=str(uuid4()), requested_logical_model="primary", allow_fallback=False,
        history_complete=True, attempted_logical_models=("primary",), attempts=(valid_attempt(api),),
    )
    with pytest.raises((ValueError, TypeError)):
        replace(receipt, **change)


async def test_receipt_is_deeply_immutable_and_payload_is_a_fresh_projection() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary")])
    with pytest.raises(ModelGatewayError) as caught:
        await make_gateway(TransportStub(capacity.events, response=ModelResponse(text="")), capacity).complete(request())
    receipt = api.get_gateway_failure_receipt(caught.value)
    before = receipt.to_payload()
    with pytest.raises(FrozenInstanceError):
        receipt.history_complete = False
    with pytest.raises(FrozenInstanceError):
        receipt.attempts[0].provider_model = "foreign/model"
    payload = receipt.to_payload()
    payload["attempts"][0]["provenance"]["logical_model"] = "foreign"
    payload["attempted_logical_models"].append("foreign")
    assert receipt.to_payload() == before


def test_helper_fails_closed_without_hostile_attribute_access_or_forged_receipts() -> None:
    api = receipt_api()

    class HostileError(Exception):
        def __getattribute__(self, name: str) -> Any:
            raise RuntimeError("Authorization: private credential")

        @property
        def _gateway_failure_receipt(self) -> Any:
            raise KeyboardInterrupt("private response")

    assert api.get_gateway_failure_receipt(HostileError()) is None
    assert api.get_gateway_failure_receipt(object()) is None
    assert api.get_gateway_failure_receipt(asyncio.CancelledError()) is None
    forged = RuntimeError("private body")
    object.__setattr__(forged, "_gateway_failure_receipt", {"history_complete": True})
    assert api.get_gateway_failure_receipt(forged) is None


async def test_helper_revalidates_corrupted_issued_receipt_without_leaking_errors() -> None:
    api = receipt_api()
    capacity = CapacityStub([lease("primary")])
    with pytest.raises(ModelGatewayError) as caught:
        await make_gateway(TransportStub(capacity.events, response=ModelResponse(text="")), capacity).complete(request())
    receipt = api.get_gateway_failure_receipt(caught.value)
    object.__setattr__(receipt.attempts[0], "provider_model", "private\nAuthorization")
    assert api.get_gateway_failure_receipt(caught.value) is None


def valid_payload(api: Any) -> dict[str, Any]:
    return cast(dict[str, Any], api.GatewayFailureReceipt(
        call_id=str(uuid4()), requested_logical_model="primary", allow_fallback=False,
        history_complete=True, attempted_logical_models=("primary",), attempts=(valid_attempt(api),),
    ).to_payload())


def test_from_payload_round_trips_into_independent_immutable_evidence() -> None:
    api = receipt_api()
    payload = valid_payload(api)
    parsed = api.GatewayFailureReceipt.from_payload(payload)
    assert parsed.to_payload() == payload
    payload["attempts"][0]["provenance"]["logical_model"] = "foreign"
    assert parsed.attempts[0].logical_model == "primary"
    error = RuntimeError()
    object.__setattr__(error, "_gateway_failure_receipt", parsed)
    assert api.get_gateway_failure_receipt(error) is None


@pytest.mark.parametrize("tamper", [
    "extra", "missing", "version_bool", "source", "disposition", "tuple_models",
    "tuple_attempts", "attempt_extra", "attempt_missing", "provenance_extra",
    "provenance_missing", "transport_state", "usage_extra", "usage_bool",
    "usage_sum", "usage_list", "unknown_counts", "gap", "duplicate", "foreign",
    "overflow", "complete_unknown", "complete_invalid",
])
def test_from_payload_rejects_inexact_or_inconsistent_nested_schema(tamper: str) -> None:
    api = receipt_api()
    payload = valid_payload(api)
    attempt = payload["attempts"][0]
    if tamper == "extra":
        payload["private_response"] = "private input"
    elif tamper == "missing":
        payload.pop("source")
    elif tamper == "version_bool":
        payload["schema_version"] = True
    elif tamper in {"source", "disposition"}:
        payload[tamper] = "private input"
    elif tamper == "tuple_models":
        payload["attempted_logical_models"] = ("primary",)
    elif tamper == "tuple_attempts":
        payload["attempts"] = (attempt,)
    elif tamper == "attempt_extra":
        attempt["private_response"] = "private input"
    elif tamper == "attempt_missing":
        attempt.pop("outcome")
    elif tamper == "provenance_extra":
        attempt["provenance"]["private_response"] = "private input"
    elif tamper == "provenance_missing":
        attempt["provenance"].pop("provider_id")
    elif tamper == "transport_state":
        attempt["transport_state"] = "unknown"
    elif tamper.startswith("usage_"):
        attempt.update(outcome="empty_response", status_code=200, usage_status="known")
        attempt["usage"] = {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}
        if tamper == "usage_extra":
            attempt["usage"]["private_response"] = "private input"
        elif tamper == "usage_bool":
            attempt["usage"]["prompt_tokens"] = True
        elif tamper == "usage_sum":
            attempt["usage"]["total_tokens"] = 3
        elif tamper == "usage_list":
            attempt["usage"] = [1, 0, 1]
    elif tamper == "unknown_counts":
        attempt["usage"] = {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}
    elif tamper == "gap":
        attempt["ordinal"] = 2
    elif tamper == "duplicate":
        payload["attempts"].append(attempt)
    elif tamper == "foreign":
        attempt["provenance"]["logical_model"] = "foreign"
    elif tamper == "overflow":
        payload["attempts"] = [attempt] * (api.MAX_GATEWAY_FAILURE_ATTEMPTS + 1)
    elif tamper == "complete_unknown":
        attempt["status_code"] = None
    elif tamper == "complete_invalid":
        attempt.update(outcome="empty_response", status_code=200, usage_status="invalid")
    with pytest.raises(ValueError, match="^gateway failure receipt payload is invalid$"):
        api.GatewayFailureReceipt.from_payload(payload)


def test_from_payload_rejects_hostile_objects_without_visiting_them() -> None:
    api = receipt_api()

    class HostileDict(dict[str, Any]):
        def __iter__(self) -> Any:
            raise KeyboardInterrupt("private input")

        def __getitem__(self, key: object) -> Any:
            raise RuntimeError("private input")

    for payload in (HostileDict(), object(), None, {"private_response": "private input"}):
        with pytest.raises(ValueError, match="^gateway failure receipt payload is invalid$"):
            api.GatewayFailureReceipt.from_payload(payload)
