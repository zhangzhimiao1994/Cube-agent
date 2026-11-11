"""Finite issued provider400 projection and strict final-event schema boundaries."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from uuid import uuid4

import httpx
import pytest

from agent_hub.models.failure_receipt import (
    GatewayFailureAttempt,
    GatewayFailureReceipt,
    _attach_gateway_failure_receipt,
    get_gateway_failure_receipt,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.provider_diagnostics import (
    ProviderDiagnostic,
    ProviderRejection,
    RequestShape,
)
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import (
    model_provider_failure_payload,
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_diagnostic,
)
from tests.unit.models.test_provider_diagnostics_oct28 import gateway_fixture

REASON = "model gateway failed: model transport failed (status=400)"
FINITE = {
    "provider_rejection_class": "structured_output",
    "provider_parameter_family": "response_format",
    "provider_protocol": "chat_completions",
    "provider_classification_source": "structured_code",
    "provider_body_state": "available",
}


def event(payload: Mapping[str, object]) -> RunEvent:
    return RunEvent.model_validate({
        "kind": EventKind.RUNTIME_FAILED, "sequence": 1, "run_id": uuid4(), "reason": REASON, "payload": payload,
    })


def test_finite_failure_schema_roundtrip_and_legacy_status_only() -> None:
    legacy = runtime_failure_diagnostic_from_reason(REASON)
    full = event({**legacy, **FINITE})
    assert RunEvent.model_validate_json(full.model_dump_json()).payload == full.payload
    assert not any(key.startswith("provider_") for key in event(legacy).payload)


@pytest.mark.parametrize(("key", "value"), [
    ("provider_protocol", "PRIVATE"), ("provider_protocol", []),
    ("provider_rejection_class", "PRIVATE"), ("provider_parameter_family", "api_key"),
    ("provider_body_state", "absent"), ("provider_body_state", None),
    ("provider_classification_source", "status_only"), ("provider_classification_source", "exact_signature"),
    ("provider_raw_message", "PRIVATE"), ("status_code", True), ("status_code", 401), ("retryable", True),
])
def test_finite_failure_schema_rejects_forged_or_inconsistent_fields(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        event({**runtime_failure_diagnostic_from_reason(REASON), **FINITE, key: value})


@pytest.mark.parametrize("missing", list(FINITE))
def test_finite_failure_schema_requires_complete_group(missing: str) -> None:
    payload = {**runtime_failure_diagnostic_from_reason(REASON), **FINITE}
    del payload[missing]
    with pytest.raises(ValueError):
        event(payload)


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
async def test_issued_gateway_receipt_projection_cannot_be_replaced_by_wrapped_text(protocol: str) -> None:
    sends: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sends.append(request.method)
        return httpx.Response(400, json={"error": {
            "code": "invalid_json_schema", "param": "response_format",
            "message": "PRIVATE_BODY Bearer sk-PRIVATE_CREDENTIAL",
        }})

    gateway, request, _, _ = gateway_fixture(protocol, handle)
    with pytest.raises(ModelTransportError) as caught:
        await gateway.complete_with_context(request)
    expected = {**FINITE, "provider_protocol": protocol}
    projected = model_provider_failure_payload(caught.value)
    assert {key: projected[key] for key in FINITE} == expected
    shape = projected["provider_request_shape"]
    assert isinstance(shape, dict) and RequestShape.from_payload(shape).protocol == protocol
    payload = safe_runtime_failure_diagnostic(caught.value)
    assert {key: payload[key] for key in FINITE} == expected
    event(payload)
    wrapped = RuntimeError("GroupChatError invalid_json_schema PRIVATE")
    wrapped.__cause__ = caught.value
    assert model_provider_failure_payload(wrapped) == {}
    status_only = ModelTransportError("invalid_json_schema PRIVATE", status_code=400)
    status_only.provider_diagnostic = caught.value.provider_diagnostic
    assert model_provider_failure_payload(status_only) == {}
    assert "PRIVATE" not in json.dumps(payload)
    assert sends == ["POST"]


async def test_issued_rejection_without_real_shape_does_not_fabricate_one() -> None:
    gateway, request, _, _ = gateway_fixture("chat_completions", lambda _: httpx.Response(400, json={}))
    with pytest.raises(ModelTransportError) as caught:
        await gateway.complete_with_context(request)
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None
    attempt = receipt.attempts[-1]
    diagnostic = attempt.provider_diagnostic
    assert diagnostic is not None
    diagnostic = replace(diagnostic, rejections=tuple(replace(row, request_shape=None) for row in diagnostic.rejections))
    _attach_gateway_failure_receipt(caught.value, replace(receipt, attempts=(replace(attempt, provider_diagnostic=diagnostic),)))
    projected = model_provider_failure_payload(caught.value)
    assert projected["provider_protocol"] == "unknown" and "provider_request_shape" not in projected
    event({**runtime_failure_diagnostic_from_reason(REASON), **projected})


def request_shape() -> dict[str, object]:
    return RequestShape("chat_completions", 1, False, (1, 0, 1, 0, 0), 2, "named", True,
                        False, False, 0, "max_completion_tokens", 321).to_payload()


def test_real_shape_accepts_mappingproxy_and_frozen_event_roundtrip() -> None:
    shape = request_shape()
    shape["role_counts"] = MappingProxyType({"system": 1, "developer": 0, "user": 1, "assistant": 0, "tool": 0})
    payload = {**runtime_failure_diagnostic_from_reason(REASON), **FINITE,
               "provider_request_shape": MappingProxyType(shape)}
    full = event(MappingProxyType(payload))
    frozen = full.payload["provider_request_shape"]
    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["role_counts"], MappingProxyType)
    assert RunEvent.from_payload(full.to_payload()).payload == full.payload
    assert RunEvent.model_validate_json(full.model_dump_json()).payload == full.payload
    assert event(full.payload).payload == full.payload


@pytest.mark.parametrize(("key", "value"), [
    ("protocol", "responses"), ("send_ordinal", True), ("tool_choice", "PRIVATE_TOOL"),
    ("tool_count", -1), ("message_count", 99), ("schema_present", 1),
    ("role_counts", {"user": 2}), ("output_token_parameter", "PRIVATE"),
    ("output_tokens", False), ("raw_body", "PRIVATE"),
])
def test_shape_rejects_tampering_and_protocol_mismatch(key: str, value: object) -> None:
    shape = {**request_shape(), key: value}
    with pytest.raises(ValueError):
        event({**runtime_failure_diagnostic_from_reason(REASON), **FINITE, "provider_request_shape": shape})


def test_shape_cannot_replace_required_core_group_or_be_null() -> None:
    base = runtime_failure_diagnostic_from_reason(REASON)
    with pytest.raises(ValueError):
        event({**base, "provider_request_shape": request_shape()})
    with pytest.raises(ValueError):
        event({**base, **FINITE, "provider_request_shape": None})
    assert "provider_request_shape" not in event({**base, **FINITE, "provider_protocol": "unknown"}).payload


@pytest.mark.parametrize(("complete", "last_without_diagnostic"), [(False, False), (True, False), (True, True)])
def test_incomplete_or_final_undiagnosed_receipt_never_projects_old_diagnostic(
    complete: bool, last_without_diagnostic: bool,
) -> None:
    # Hooke's zero-request DTO reproduction; no gateway/client/provider invoked.
    shape = RequestShape.from_payload(request_shape())
    diagnostic = ProviderDiagnostic((ProviderRejection(
        400, "sdk_bad_request", "structured_output", "response_format", "structured_code", "available", shape,
    ),))
    attempt = GatewayFailureAttempt(1, "primary", "primary", "openai", "openai/test-model",
                                    "transport_error", 400, "missing", None, diagnostic)
    attempts = (attempt, replace(attempt, ordinal=2, provider_diagnostic=None)) if last_without_diagnostic else (attempt,)
    receipt = GatewayFailureReceipt(str(uuid4()), "primary", True, complete, ("primary",), attempts)
    error = ModelTransportError("model transport failed", status_code=400)
    _attach_gateway_failure_receipt(error, receipt)
    projected = model_provider_failure_payload(error)
    if not complete or last_without_diagnostic:
        assert projected == {}
    else:
        assert {key: projected[key] for key in FINITE} == FINITE
        assert projected["provider_request_shape"] == shape.to_payload()
    assert str(error) == "model transport failed" and error.status_code == 400
