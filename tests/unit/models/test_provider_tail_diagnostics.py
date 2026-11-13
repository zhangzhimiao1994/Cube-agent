"""Final-wire tail metadata and closed provider signatures; no provider calls."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import ClassVar

import httpx
import pytest
from openai import BadRequestError

from agent_hub.models.provider_diagnostics import (
    ProviderDiagnostic,
    RequestShape,
    capture_error,
    capture_response,
    make_request_shape,
)

TURN_REJECTION = (
    "The last message of deepseek-reasoner must be a user message, or an assistant "
    "message with prefix mode on (refer to https://api-docs.deepseek.com/guides/chat_prefix_completion)."
)


def wire(content: object = "PRIVATE_TEXT", **extra: object) -> dict[str, object]:
    return {"messages": [{"role": "user", "content": "PRIVATE_TASK"},
                         {"role": "assistant", "content": content, **extra}],
            "max_completion_tokens": 24576}


def legacy() -> dict[str, object]:
    return {"protocol": "chat_completions", "send_ordinal": 1, "stream": False,
            "message_count": 2, "role_counts": {"system": 0, "developer": 0, "user": 1, "assistant": 1, "tool": 0},
            "tool_count": 0, "tool_choice": "none", "schema_present": False,
            "native_continuation": False, "native_reasoning_present": False, "native_tool_call_count": 0,
            "output_token_parameter": "max_completion_tokens", "output_tokens": 24576}


def test_legacy_shape_round_trip_does_not_rewrite_historical_evidence() -> None:
    assert RequestShape.from_payload(legacy()).to_payload() == legacy()
    evidence = {"schema_version": 1, "wire_history_complete": True, "rejections": [{
        "status_code": 400, "exception_family": "http_status", "rejection_class": "unknown",
        "parameter_family": "unknown", "classification_source": "status_only", "body_state": "available",
        "request_shape": legacy(),
    }]}
    assert ProviderDiagnostic.from_payload(evidence).to_payload() == evidence


@pytest.mark.parametrize("content,extra,kind,empty,prefix", [
    ("PRIVATE_TEXT", {}, "text", False, False),
    ("", {}, "text", True, False),
    (" ", {}, "text", False, False),
    (None, {}, "null", True, False),
    ([], {}, "parts", True, False),
    ([{"type": "text", "text": "PRIVATE_TEXT"}], {}, "parts", False, False),
    ("PRIVATE_TEXT", {"prefix": True}, "text", False, True),
    ("PRIVATE_TEXT", {"prefix": 1}, "text", False, None),
])
def test_final_payload_tail_is_finite_and_does_not_guess_prefix(
    content: object, extra: dict[str, object], kind: str, empty: bool, prefix: bool | None,
) -> None:
    shape = make_request_shape(wire(content, **extra), "chat_completions", send_ordinal=1)
    assert shape is not None
    value = shape.to_payload()
    expected = {**legacy(), "last_role": "assistant", "last_content_kind": kind, "last_content_empty": empty,
                "empty_content_role_counts": {"system": 0, "developer": 0, "user": 0, "assistant": int(empty), "tool": 0},
                "native_reasoning_field_count": 0, "empty_native_reasoning_field_count": 0}
    if prefix is not None:
        expected["last_assistant_prefix"] = prefix
    else:
        expected = legacy()
    assert value == expected
    assert RequestShape.from_payload(value) == shape
    assert "PRIVATE" not in json.dumps(value) + repr(shape)


def test_missing_content_and_no_messages_are_not_reported_as_plain_text() -> None:
    shape = make_request_shape({"messages": [{"role": "assistant"}]}, "chat_completions", send_ordinal=1)
    assert shape is not None
    assert shape.to_payload()["last_content_kind"] == "absent"
    assert shape.to_payload()["last_content_empty"] is True
    empty = make_request_shape({"messages": []}, "chat_completions", send_ordinal=1)
    assert empty is not None
    assert not any(k.startswith("last_") for k in empty.to_payload())


@pytest.mark.parametrize("key,value", [
    ("last_role", "PRIVATE"), ("last_role", 1),
    ("last_content_kind", "PRIVATE"), ("last_content_kind", []),
    ("last_content_empty", 1), ("last_assistant_prefix", "true"),
    ("last_content", "PRIVATE"),
])
def test_tail_dto_rejects_unknown_enums_text_and_bool_substitutes(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        RequestShape.from_payload({**legacy(), key: value})


def test_exact_http400_signature_survives_receipt_round_trip_without_body() -> None:
    shape = make_request_shape(wire(), "chat_completions", send_ordinal=1)
    body = {"error": {"code": "invalid_request_error", "type": "invalid_request_error",
                      "param": None, "message": TURN_REJECTION}}
    response = httpx.Response(400, json=body, request=httpx.Request("POST", "https://fixture.invalid"))
    error = BadRequestError("PRIVATE_EXCEPTION", response=response, body=body)
    for evidence in (capture_error(error, shape), capture_response(response, shape)):
        assert evidence is not None
        rejection = evidence.rejections[-1]
        assert (rejection.rejection_class, rejection.parameter_family, rejection.classification_source) == (
            "assistant_turn", "messages", "exact_signature")
        assert ProviderDiagnostic.from_payload(evidence.to_payload()) == evidence
        exported = json.dumps(evidence.to_payload()) + repr(evidence)
        assert "PRIVATE" not in exported and TURN_REJECTION not in exported


@pytest.mark.parametrize("status,body", [
    (400, {"error": {"code": "invalid_request_error", "message": "PRIVATE_GENERIC"}}),
    (400, {"error": {"code": "invalid_request_error", "message": TURN_REJECTION + " PRIVATE_SUFFIX"}}),
    (400, {"error": {"code": "invalid_request_error", "message": "PRIVATE_PREFIX " + TURN_REJECTION}}),
    (400, {"error": {"code": "invalid_request_error", "message": TURN_REJECTION[:-1]}}),
    (400, {"error": {"code": "invalid_request_error", "message": TURN_REJECTION + "X" * 17000}}),
    (401, {"error": {"code": "invalid_request_error", "message": TURN_REJECTION}}),
    (500, {"error": {"code": "invalid_request_error", "message": TURN_REJECTION}}),
])
def test_similar_unknown_or_non400_body_is_not_assistant_turn(status: int, body: object) -> None:
    evidence = capture_response(httpx.Response(status, json=body), None)
    assert evidence is not None
    assert evidence.rejections[-1].rejection_class == "unknown"
    assert "PRIVATE" not in json.dumps(evidence.to_payload())


def test_untrusted_exception_cannot_supply_message_order_evidence() -> None:
    class Forged(Exception):
        body: ClassVar[dict[str, object]] = {"error": {"message": TURN_REJECTION}}
        status_code = 400

    assert capture_error(Forged(), None) is None


@pytest.mark.parametrize("changed", [
    {"status_code": 500}, {"parameter_family": "tools"}, {"classification_source": "structured_code"},
    {"body_state": "absent"},
])
def test_assistant_turn_dto_requires_exact400_available_messages_evidence(changed: dict[str, object]) -> None:
    rejection = {"status_code": 400, "exception_family": "http_status", "rejection_class": "assistant_turn",
                 "parameter_family": "messages", "classification_source": "exact_signature", "body_state": "available",
                 "request_shape": None, **changed}
    with pytest.raises(ValueError):
        ProviderDiagnostic.from_payload({"schema_version": 1, "wire_history_complete": False, "rejections": [rejection]})


def test_new_shape_and_signature_survive_issued_receipt_runtime_and_public_projection() -> None:
    from uuid import uuid4

    from agent_hub.models.failure_receipt import (
        GatewayFailureAttempt,
        GatewayFailureReceipt,
        _attach_gateway_failure_receipt,
    )
    from agent_hub.models.litellm_client import ModelTransportError
    from agent_hub.runs.repository import _public_event_payload
    from agent_hub.runtime.contracts import RunEvent
    from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic

    diagnostic = capture_response(httpx.Response(400, json={"error": {
        "code": "invalid_request_error", "type": "invalid_request_error", "param": None, "message": TURN_REJECTION,
    }}), make_request_shape(wire(), "chat_completions", send_ordinal=1))
    assert diagnostic is not None
    attempt = GatewayFailureAttempt(1, "primary", "primary", "deepseek", "deepseek/deepseek-reasoner",
                                    "transport_error", 400, "missing", None, diagnostic)
    receipt = GatewayFailureReceipt(str(uuid4()), "primary", False, True, ("primary",), (attempt,))
    assert GatewayFailureReceipt.from_payload(receipt.to_payload()) == receipt
    error = ModelTransportError("model transport failed (status=400)", status_code=400)
    _attach_gateway_failure_receipt(error, receipt)
    payload = safe_runtime_failure_diagnostic(error)
    assert payload["provider_rejection_class"] == "assistant_turn"
    event = RunEvent.model_validate({"kind": "runtime.failed", "sequence": 106, "run_id": uuid4(),
                                    "reason": "model transport failed (status=400)", "payload": payload})
    public = _public_event_payload(json.loads(event.model_dump_json()))
    restored = RunEvent.from_payload(public)
    assert restored.payload == event.payload
    shape = restored.payload["provider_request_shape"]
    assert isinstance(shape, Mapping)
    assert shape["last_role"] == "assistant" and shape["last_assistant_prefix"] is False
    assert "PRIVATE" not in json.dumps(public) and TURN_REJECTION not in json.dumps(public)


@pytest.mark.parametrize("message", [
    {"role": "user", "content": "PRIVATE", "prefix": True},
    {"role": "assistant", "content": {"PRIVATE": "PRIVATE"}},
])
def test_ambiguous_tail_never_claims_plain_nonempty_unprefixed_assistant(message: dict[str, object]) -> None:
    shape = make_request_shape({"messages": [message]}, "chat_completions", send_ordinal=1)
    if shape is not None:
        assert shape.last_content_kind is None and shape.last_content_empty is None
        assert shape.empty_content_role_counts is None


def test_malformed_json_or_other_rejection_cannot_be_promoted_by_embedded_signature() -> None:
    duplicate = json.dumps({"error": {"message": TURN_REJECTION}})[:-1] + ',"error": {}}'
    evidence = capture_response(httpx.Response(400, content=duplicate), None)
    assert evidence is not None and evidence.rejections[-1].body_state == "invalid"
    for code, kind, param in [("content_policy_violation", "invalid_request_error", None),
                              ("invalid_request_error", "authentication_error", None),
                              ("invalid_request_error", "invalid_request_error", "tools")]:
        evidence = capture_response(httpx.Response(400, json={"error": {
            "message": TURN_REJECTION, "code": code, "type": kind, "param": param,
        }}), None)
        assert evidence is not None and evidence.rejections[-1].rejection_class != "assistant_turn"


def test_whole_history_counts_middle_empty_fields_not_only_the_final_turn() -> None:
    shape = make_request_shape({"messages": [
        {"role": "system", "content": ""},
        {"role": "developer", "content": None},
        {"role": "user", "content": "PRIVATE_TASK"},
        {"role": "assistant", "content": "", "reasoning_content": ""},
        {"role": "assistant", "content": None, "reasoning_content": None},
        {"role": "tool", "content": []},
        {"role": "assistant", "content": "PRIVATE_REPLY"},
        {"role": "assistant", "content": " ", "reasoning_content": " "},
        {"role": "assistant", "content": "PRIVATE_REPLY", "reasoning_content": "PRIVATE_COT"},
        {"role": "user", "content": "PRIVATE_NEXT"},
    ]}, "chat_completions", send_ordinal=1)
    assert shape is not None
    value = shape.to_payload()
    assert value["last_role"] == "user" and value["last_content_empty"] is False
    assert value["empty_content_role_counts"] == {"system": 1, "developer": 1, "user": 0, "assistant": 2, "tool": 1}
    assert value["native_reasoning_field_count"] == 4
    assert value["empty_native_reasoning_field_count"] == 2
    assert RequestShape.from_payload(value) == shape
    assert "PRIVATE" not in json.dumps(value) + repr(shape)


def test_missing_reasoning_is_not_an_empty_present_reasoning_field() -> None:
    shape = make_request_shape({"messages": [
        {"role": "assistant"},
        {"role": "user", "content": "PRIVATE", "reasoning_content": ""},
        {"role": "assistant", "content": "PRIVATE"},
    ]}, "chat_completions", send_ordinal=1)
    assert shape is not None
    value = shape.to_payload()
    assert value["empty_content_role_counts"] == {"system": 0, "developer": 0, "user": 0, "assistant": 1, "tool": 0}
    assert value["native_reasoning_field_count"] == 0
    assert value["empty_native_reasoning_field_count"] == 0


def complete_profile() -> dict[str, object]:
    return {**legacy(), "last_role": "assistant", "last_content_kind": "text",
            "last_content_empty": False, "last_assistant_prefix": False,
            "empty_content_role_counts": {"system": 0, "developer": 0, "user": 0, "assistant": 0, "tool": 0},
            "native_reasoning_field_count": 0, "empty_native_reasoning_field_count": 0}


@pytest.mark.parametrize("key,value", [
    ("empty_content_role_counts", {"system": 0, "developer": 0, "user": 0, "assistant": 2, "tool": 0}),
    ("empty_content_role_counts", {"system": 0, "developer": 0, "user": 0, "assistant": True, "tool": 0}),
    ("empty_content_role_counts", {"system": 0, "developer": 0, "user": -1, "assistant": 0, "tool": 0}),
    ("empty_content_role_counts", {"system": 0, "developer": 0, "user": 0, "assistant": 1, "tool": 0}),
    ("empty_content_role_counts", {"assistant": 0}),
    ("native_reasoning_field_count", 2), ("native_reasoning_field_count", True),
    ("empty_native_reasoning_field_count", 1), ("empty_native_reasoning_field_count", False),
])
def test_aggregate_profile_rejects_impossible_or_noninteger_counts(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        RequestShape.from_payload({**complete_profile(), key: value})


@pytest.mark.parametrize("missing", ["last_role", "last_content_kind", "last_content_empty", "last_assistant_prefix",
                                     "empty_content_role_counts", "native_reasoning_field_count", "empty_native_reasoning_field_count"])
def test_new_profile_requires_all_seven_fields_while_legacy_remains_valid(missing: str) -> None:
    value = complete_profile()
    del value[missing]
    with pytest.raises(ValueError):
        RequestShape.from_payload(value)
    assert RequestShape.from_payload(legacy()).to_payload() == legacy()
