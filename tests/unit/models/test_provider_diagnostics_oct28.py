"""Finite provider diagnostics: synthetic SDK/HTTP only, never a live model."""
from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest
from openai import BadRequestError

from agent_hub.models import provider_diagnostics
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.types import ModelRequest
from tests.unit.models.provider_capacity_typed_fixture import (
    DiagnosticsAPI,
    HTTPHandler,
    at,
    mock_client,
    mutable,
    present,
    set_field,
)
from tests.unit.models.provider_capacity_typed_fixture import rows as object_rows
from tests.unit.models.test_gateway import CapacityStub, SecretStub


def api() -> DiagnosticsAPI:
    name = "agent_hub.models.provider_diagnostics"
    assert importlib.util.find_spec(name) is not None, "finite provider diagnostics missing"
    return provider_diagnostics


def payload() -> dict[str, object]:
    return {
        "messages": [{"role": "system", "content": "PRIVATE_PROMPT"},
                     {"role": "assistant", "content": "PRIVATE_BODY", "reasoning_content": "PRIVATE_REASONING",
                      "tool_calls": [{"function": {"name": "private_tool", "arguments": "PRIVATE_ARGS"}}]},
                     {"role": "tool", "content": "PRIVATE_RESULT", "tool_call_id": "PRIVATE_ID"}],
        "tools": [{"type": "function", "function": {"parameters": {"PRIVATE_SCHEMA": "PRIVATE_DESCRIPTION"}}}],
        "tool_choice": {"type": "function", "function": {"name": "PRIVATE_TOOL"}},
        "response_format": {"type": "json_schema", "json_schema": {"PRIVATE_SCHEMA": True}},
        "max_completion_tokens": 321, "stream": False,
    }


def bad(body: object, status: int = 400) -> BadRequestError:
    return BadRequestError("PRIVATE_EXCEPTION_API_KEY", body=body,
                           response=httpx.Response(status, request=httpx.Request("POST", "https://fixture.invalid/v1")))


def test_final_shape_counts_do_not_retain_request_contents() -> None:
    a = api()
    original = payload()
    shape = a.make_request_shape(original, "chat_completions", send_ordinal=1, native_continuation=True)
    assert shape is not None
    value = shape.to_payload()
    assert value == {
        "protocol": "chat_completions", "send_ordinal": 1, "stream": False,
        "message_count": 3, "role_counts": {"system": 1, "developer": 0, "user": 0, "assistant": 1, "tool": 1},
        "tool_count": 1, "tool_choice": "named", "schema_present": True,
        "native_continuation": True, "native_reasoning_present": True, "native_tool_call_count": 1,
        "output_token_parameter": "max_completion_tokens", "output_tokens": 321,
    }
    original["max_completion_tokens"] = 999
    assert shape.output_tokens == 321
    assert "PRIVATE" not in json.dumps(value)
    with pytest.raises(FrozenInstanceError):
        set_field(shape, "output_tokens", 999)


@pytest.mark.parametrize("body,category,parameter,source,state", [
    ({"error": {"code": "unsupported_parameter", "param": "max_completion_tokens", "message": "PRIVATE_PROMPT"}},
     "unsupported_parameter", "max_completion_tokens", "structured_code", "available"),
    ({"code": "context_length_exceeded", "param": "messages[42].content"},
     "context_window", "messages", "structured_code", "available"),
    ({"code": "PRIVATE_API_KEY", "param": "PRIVATE_PROMPT", "message": "unsupported max_completion_tokens PRIVATE"},
     "unknown", "unknown", "status_only", "available"),
    ({"message": "PRIVATE" * 10000}, "unknown", "unknown", "status_only", "oversized"),
    (None, "unknown", "unknown", "status_only", "absent"),
])
def test_only_structured_allowlisted_classes_survive(
    body: object, category: str, parameter: str, source: str, state: str,
) -> None:
    a = api()
    shape = a.make_request_shape(payload(), "chat_completions", send_ordinal=1)
    diagnostic = a.capture_error(bad(body), shape)
    assert diagnostic is not None
    value = present(diagnostic).to_payload()
    rejection = object_rows(value["rejections"])[0]
    assert rejection["status_code"] == 400
    assert rejection["exception_family"] == "sdk_bad_request"
    assert (rejection["rejection_class"], rejection["parameter_family"], rejection["classification_source"],
            rejection["body_state"]) == (category, parameter, source, state)
    assert "PRIVATE" not in json.dumps(value) + repr(diagnostic)
    assert a.ProviderDiagnostic.from_payload(value) == diagnostic


def test_unknown_or_hostile_exception_is_not_claimed_as_provider_evidence() -> None:
    a = api()

    class Hostile(Exception):
        @property
        def body(self) -> object:
            pytest.fail("untrusted exception property accessed")

    assert a.capture_error(Hostile("PRIVATE"), None) is None

    class Subclass(BadRequestError):
        pass

    original = bad({"code": "context_length_exceeded"})
    assert a.capture_error(Subclass("PRIVATE", response=original.response, body=original.body), None) is None


def test_arbitrary_exception_cannot_spoof_an_issued_diagnostic_by_public_attribute() -> None:
    a = api()
    issued = a.capture_error(bad({"code": "context_length_exceeded"}), None)
    foreign = RuntimeError("PRIVATE")
    set_field(foreign, "provider_diagnostic", issued)
    assert a.get_provider_diagnostic(foreign) is None
    assert a.capture_error(foreign, None) is None


def test_diagnostic_cross_fields_cannot_claim_protocol_or_signature_not_observed() -> None:
    a = api()
    value = present(a.capture_error(bad({"code": "context_length_exceeded"}),
                            a.make_request_shape(payload(), "chat_completions", send_ordinal=1))).to_payload()
    for key, replacement in (("protocol", "responses"), ("classification_source", "exact_signature")):
        changed = copy.deepcopy(value)
        target = mutable(at(changed, "rejections", 0, "request_shape")) if key == "protocol" else object_rows(changed["rejections"])[0]
        target[key] = replacement
        with pytest.raises(ValueError):
            a.ProviderDiagnostic.from_payload(changed)


def test_strict_payload_rejects_extra_text_unknown_enums_and_mutated_frozen_values() -> None:
    a = api()
    diagnostic = a.capture_error(bad({"code": "unsupported_parameter"}),
                                 a.make_request_shape(payload(), "chat_completions", send_ordinal=1))
    value = present(diagnostic).to_payload()
    mutations: list[tuple[list[str | int], str, object]] = [([], "message", "PRIVATE"), (["rejections", 0], "body", "PRIVATE"),
                            (["rejections", 0], "rejection_class", "PRIVATE"),
                            (["rejections", 0, "request_shape"], "output_tokens", True)]
    for path, key, item in mutations:
        changed = copy.deepcopy(value)
        node: object = changed
        for part in path:
            node = at(node, part)
        mutable(node)[key] = item
        with pytest.raises(ValueError):
            a.ProviderDiagnostic.from_payload(changed)


def test_direct_http_error_has_bounded_json_without_message_echo() -> None:
    a = api()
    response = httpx.Response(400, json={"error": {"code": "invalid_json_schema", "param": "response_format",
                                                  "message": "PRIVATE_PROMPT PRIVATE_API_KEY"}})
    diagnostic = a.capture_response(response, a.make_request_shape(payload(), "chat_completions", send_ordinal=1))
    assert at(present(diagnostic).to_payload(), "rejections", 0, "rejection_class") == "structured_output"
    assert "PRIVATE" not in json.dumps(present(diagnostic).to_payload())
    assert at(present(a.capture_response(httpx.Response(400, content=b"x" * 20000), None)).to_payload(), "rejections", 0, "body_state") == "oversized"


def test_receipt_contract_keeps_legacy_shape_and_strictly_versions_diagnostics() -> None:
    from dataclasses import replace

    from agent_hub.models.failure_receipt import GatewayFailureAttempt, GatewayFailureReceipt
    a = api()
    attempt = GatewayFailureAttempt(1, "primary", "primary", "provider", "provider/model", "transport_error", 400)
    receipt = GatewayFailureReceipt("00000000-0000-0000-0000-000000000001", "primary", False, True, ("primary",), (attempt,))
    legacy = receipt.to_payload()
    assert legacy["schema_version"] == 1 and "provider_diagnostic" not in object_rows(legacy["attempts"])[0]
    diagnostic = a.capture_error(bad({"code": "context_length_exceeded"}),
                                 a.make_request_shape(payload(), "chat_completions", send_ordinal=1))
    receipt = replace(receipt, attempts=(replace(attempt, provider_diagnostic=diagnostic),))
    value = receipt.to_payload()
    assert value["schema_version"] == 2
    assert GatewayFailureReceipt.from_payload(value).to_payload() == value
    assert GatewayFailureReceipt.from_payload(legacy).to_payload() == legacy
    for mutation in ("downgrade", "raw", "bool_version"):
        broken = copy.deepcopy(value)
        if mutation == "downgrade":
            broken["schema_version"] = 1
        elif mutation == "raw":
            mutable(at(broken, "attempts", 0, "provider_diagnostic"))["message"] = "PRIVATE"
        else:
            broken["schema_version"] = True
        with pytest.raises(ValueError):
            GatewayFailureReceipt.from_payload(broken)


def gateway_fixture(protocol: str, handle: HTTPHandler) -> tuple[ModelGateway, ModelRequest, CapacityStub, SecretStub]:
    from dataclasses import replace

    from agent_hub.models.gateway import ModelGateway
    from agent_hub.models.registry import ModelRegistry
    from agent_hub.models.types import ModelCapability
    from tests.contracts.test_required_tool_transport import deployment, request
    from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease

    selected = replace(deployment("responses" if protocol == "responses" else "chat_completions"),
                       secret_ref="secret://fixture", quota_scope_id="scope-primary")
    if protocol == "messages":
        selected = replace(selected, api_base="https://fixture.invalid/v1/messages",
                           capabilities=selected.capabilities | {ModelCapability.TEXT})
    backup = replace(selected, id="backup", logical_model="backup")
    capacity = CapacityStub([lease("primary"), lease("backup")])
    secret = SecretStub(capacity.events)

    client = mock_client(handle)
    gateway = ModelGateway(ModelRegistry([selected, backup]), capacity, secret, client,
                           fallbacks={"primary": "backup"})
    req = request("responses" if protocol == "responses" else "chat_completions", max_output_tokens=321)
    if protocol == "messages":
        req = replace(req, required_capabilities=frozenset({ModelCapability.TEXT}), tools=())
    return gateway, req, capacity, secret


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages", "stream"])
async def test_real_gateway_failure_retains_class_final_shape_and_no_extra_requests(protocol: str) -> None:
    from agent_hub.models.failure_receipt import GatewayFailureReceipt, get_gateway_failure_receipt
    from agent_hub.models.litellm_client import ModelTransportError, safe_model_client_error
    wire: list[dict[str, object]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(mutable(json.loads(req.content)))
        return httpx.Response(400, json={"error": {"code": "invalid_json_schema", "param": "response_format",
                                                   "message": "PRIVATE_PROMPT PRIVATE_REASONING PRIVATE_API_KEY"}})

    gateway, req, capacity, secret = gateway_fixture(protocol, handle)
    with pytest.raises(ModelTransportError) as caught:
        if protocol == "stream":
            _ = [item async for item in gateway.stream_openai_compatible_events(req)]
        else:
            await gateway.complete_with_context(req)
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None
    value = receipt.to_payload()
    assert value["schema_version"] == 2
    rejection = object_rows(at(value, "attempts", 0, "provider_diagnostic", "rejections"))[-1]
    assert rejection["rejection_class"] == "structured_output"
    shape = mutable(rejection["request_shape"])
    assert shape["protocol"] == ("chat_completions" if protocol == "stream" else protocol)
    token_field = {"chat_completions": "max_completion_tokens", "stream": "max_completion_tokens",
                   "responses": "max_output_tokens", "messages": "max_tokens"}[protocol]
    assert shape["output_token_parameter"] == token_field and shape["output_tokens"] == wire[0][token_field]
    assert shape["stream"] is (protocol == "stream")
    assert len(wire) == 1 and len(capacity.releases) == 1 and len(secret.references) == 1
    assert receipt.attempted_logical_models == ("primary",)
    assert "PRIVATE" not in json.dumps(value)
    assert GatewayFailureReceipt.from_payload(value).to_payload() == value
    wrapped = safe_model_client_error("primary", caught.value, ())
    assert api().get_provider_diagnostic(wrapped) == api().get_provider_diagnostic(caught.value)


@pytest.mark.parametrize("negotiation", ["tool_choice", "max_tokens"])
async def test_existing_negotiation_keeps_both_actual_shapes_without_third_send(negotiation: str) -> None:
    from dataclasses import replace

    from agent_hub.models.failure_receipt import get_gateway_failure_receipt
    from agent_hub.models.litellm_client import ModelTransportError
    wire: list[dict[str, object]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(mutable(json.loads(req.content)))
        if len(wire) == 1:
            if negotiation == "tool_choice":
                message = "The tool_choice parameter does not support being set to required or object in thinking mode"
                return httpx.Response(400, json={"error": {"message": message, "code": "invalid_parameter_error"}})
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: max_completion_tokens",
                                                       "code": "unsupported_parameter", "param": "max_completion_tokens"}})
        return httpx.Response(400, json={"error": {"message": "PRIVATE_SECOND_BODY", "code": "unknown"}})

    gateway, req, _, _ = gateway_fixture("chat_completions", handle)
    if negotiation == "tool_choice":
        req = replace(req, required_tool="project_inspect")
    with pytest.raises(ModelTransportError) as caught:
        await gateway.complete_with_context(req)
    receipt = get_gateway_failure_receipt(caught.value)
    assert receipt is not None and len(wire) == 2
    rows = object_rows(at(receipt.to_payload(), "attempts", 0, "provider_diagnostic", "rejections"))
    assert [mutable(r["request_shape"])["send_ordinal"] for r in rows] == [1, 2]
    if negotiation == "tool_choice":
        assert [mutable(r["request_shape"])["tool_choice"] for r in rows] == ["named", "auto"]
    else:
        assert [mutable(r["request_shape"])["output_token_parameter"] for r in rows] == ["max_completion_tokens", "max_tokens"]
    assert rows[-1]["rejection_class"] == "unknown"


@pytest.mark.parametrize("capture_failure", ["raise", "wrong_type", "wrong_status"])
async def test_capture_failure_preserves_the_original_400_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, capture_failure: str,
) -> None:
    from agent_hub.models.litellm_client import ModelTransportError
    wire: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req.url.path)
        return httpx.Response(400, json={"error": {"message": "PRIVATE"}})

    wrong_status = api().capture_error(bad({"code": "unsupported_parameter"}, status=401), None)

    def broken(*args: object, **kwargs: object) -> object:
        if capture_failure == "raise":
            raise ValueError("PRIVATE_CAPTURE_FAILURE")
        return object() if capture_failure == "wrong_type" else wrong_status

    monkeypatch.setattr(api(), "capture_error", broken)
    gateway, req, _, _ = gateway_fixture("chat_completions", handle)
    with pytest.raises(ModelTransportError) as caught:
        await gateway.complete_with_context(req)
    assert caught.value.status_code == 400 and len(wire) == 1
    assert api().get_provider_diagnostic(caught.value) is None
    assert "PRIVATE" not in str(caught.value)
