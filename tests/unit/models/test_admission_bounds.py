"""Pure reservation math and private logical request freezing (no transport)."""

import hashlib
import json
from dataclasses import fields, replace
from decimal import Decimal, localcontext
from typing import Never, Unpack

import pytest

from agent_hub.models.admission_bounds import AdmissionBounds, TrustedContextBound
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    ToolDefinition,
    model_message_payload,
)
from tests.unit.models.admission_bounds_typed_fixture import (
    BoundsAPI,
    DeploymentOverrides,
    MutableEnumSchema,
    RequestOverrides,
    ReserveOverrides,
)


def api() -> BoundsAPI:
    from agent_hub.models import admission_bounds

    return admission_bounds


def deployment(**changes: Unpack[DeploymentOverrides]) -> Deployment:
    return replace(
        Deployment(
            id="resolved",
            logical_model="main",
            context_window_tokens=8192,
            input_per_million_usd=Decimal("0.15"),
            output_per_million_usd=Decimal("0.6"),
        ),
        **changes,
    )


def request(**changes: Unpack[RequestOverrides]) -> ModelRequest:
    return replace(
        ModelRequest("main", (ModelMessage("user", "私密 prompt"),), max_output_tokens=16),
        **changes,
    )


def trust(d: Deployment) -> TrustedContextBound:
    assert d.context_window_tokens is not None
    return api().TrustedContextBound(
        d.id, d.logical_model, d.provider_model, d.context_window_tokens
    )


def reserve(
    d: Deployment | None = None, r: ModelRequest | None = None,
    **kwargs: Unpack[ReserveOverrides],
) -> AdmissionBounds:
    d = d or deployment()
    return api().compute_admission_bounds(d, r or request(), trusted_context=trust(d), **kwargs)


def rich_request() -> ModelRequest:
    native = NativeAssistantMessage(
        "resolved",
        "private native",
        (NativeToolCall("call-1", "lookup", '{"嵌套":{"x":[1,true]}}'),),
        "hidden reasoning",
    )
    assert native.content is not None
    return request(
        messages=(
            ModelMessage(
                "user",
                (
                    {"type": "text", "text": "private text"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/private", "detail": "high"},
                    },
                ),
            ),
            ModelMessage("assistant", native.content, native),
            ModelMessage("tool", "private result", tool_call_id="call-1"),
        ),
        native_tool_continuation=True,
        required_capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
        ),
        response_schema=StructuredResponseSchema(
            "Verdict",
            {
                "type": "object",
                "properties": {"a": {"enum": ("私密", "x")}},
            },
        ),
        tools=(
            ToolDefinition(
                "lookup",
                "private description",
                {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            ),
            ToolDefinition("other", "other tool", {"type": "object"}),
        ),
        required_tool="lookup",
    )


@pytest.mark.parametrize(
    ("ctx", "inp", "out", "expected"),
    [
        (1, "0.000001", "0", "0.000001"),
        (8192, "0.15", "0.6", "0.004916"),
        (8192, "0.6", "0.15", "0.004916"),
        (1_000_000, "1000000", "0", "1000000.000000"),
        (1_000_000, "0", "1000000", "1000000.000000"),
        (10_000_000, "0", "0", "0.000000"),
    ],
)
def test_context_is_total_hold_and_cost_rounds_up(
    ctx: int, inp: str, out: str, expected: str,
) -> None:
    d = deployment(
        context_window_tokens=ctx,
        input_per_million_usd=Decimal(inp),
        output_per_million_usd=Decimal(out),
    )
    with localcontext() as c:
        c.prec = 6
        b = reserve(d, request(max_output_tokens=1))
    assert b.total_tokens == ctx
    assert b.cost_usd == Decimal(expected)
    assert b.known_zero is (inp == "0" and out == "0")


def test_missing_trust_is_unsupported_and_private() -> None:
    with pytest.raises(api().UnsupportedAdmissionBounds, match="^admission bounds unsupported$"):
        api().compute_admission_bounds(deployment(), request())


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("context_window_tokens", None),
        ("context_window_tokens", True),
        ("context_window_tokens", 0),
        ("context_window_tokens", -1),
        ("context_window_tokens", 10_000_001),
        ("context_window_tokens", 1.5),
        ("input_per_million_usd", None),
        ("output_per_million_usd", None),
        ("input_per_million_usd", Decimal("NaN")),
        ("output_per_million_usd", Decimal("Infinity")),
        ("input_per_million_usd", Decimal(-1)),
        ("input_per_million_usd", Decimal(1000001)),
        ("input_per_million_usd", Decimal("0.0000001")),
        ("output_per_million_usd", 0),
        ("id", "private invalid id"),
        ("max_output_tokens", True),
        ("max_output_tokens", 0),
    ],
)
def test_revalidates_tampered_deployment_without_io(field: str, bad: object) -> None:
    d = deployment()
    t = trust(d)
    object.__setattr__(d, field, bad)
    with pytest.raises(
        ValueError, match="^(invalid admission bounds|admission bounds unsupported)$"
    ):
        api().compute_admission_bounds(d, request(), trusted_context=t)


def test_missing_prices_are_unknown_even_when_trust_is_present() -> None:
    d = deployment(input_per_million_usd=None, output_per_million_usd=None)
    with pytest.raises(api().UnsupportedAdmissionBounds):
        reserve(d)


def test_cost_above_dao_ceiling_rejected() -> None:
    with pytest.raises(ValueError):
        reserve(deployment(context_window_tokens=1_000_001, input_per_million_usd=Decimal(1000000)))


@pytest.mark.parametrize("limit", [8, 15])
def test_selected_effective_request_must_fit_context_and_deployment_output(limit: int) -> None:
    d = deployment(context_window_tokens=limit)
    with pytest.raises(ValueError):
        reserve(d)
    d = deployment(max_output_tokens=limit)
    with pytest.raises(ValueError):
        reserve(d)
    assert reserve(d, request(max_output_tokens=limit)).total_tokens == 8192


@pytest.mark.parametrize(
    "field", ["deployment_id", "logical_model", "provider_model", "total_tokens"]
)
def test_trust_must_match_resolved_deployment_and_be_revalidated(field: str) -> None:
    d = deployment()
    t = trust(d)
    object.__setattr__(t, field, "private changed" if field != "total_tokens" else 4)
    with pytest.raises(ValueError):
        api().compute_admission_bounds(d, request(), trusted_context=t)


def test_effective_rates_override_config_and_are_owned_copies() -> None:
    d = deployment(input_per_million_usd=None, output_per_million_usd=None)
    p = api().KnownPricing(Decimal(9), Decimal(2))
    b = reserve(d, effective_pricing=p)
    assert b.cost_usd == Decimal("0.073728")
    object.__setattr__(p, "input_per_million_usd", Decimal(0))
    object.__setattr__(d, "id", "changed")
    object.__setattr__(d, "output_per_million_usd", Decimal(0))
    assert b.deployment().id == "resolved"
    assert b.pricing().input_per_million_usd == Decimal(9)
    copy = b.deployment()
    object.__setattr__(copy, "context_window_tokens", 1)
    pc = b.pricing()
    object.__setattr__(pc, "output_per_million_usd", Decimal(0))
    assert b.deployment().context_window_tokens == 8192
    assert b.pricing().output_per_million_usd == Decimal(2)


def test_fingerprint_is_full_sorted_compact_utf8_logical_json() -> None:
    r = rich_request()
    frozen = api().freeze_model_request(r)
    encoded = frozen.canonical_bytes()
    payload = json.loads(encoded)
    assert encoded == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf8")
    assert frozen.digest == hashlib.sha256(encoded).hexdigest()
    assert set(payload) == {f.name for f in fields(ModelRequest)}
    assert payload["required_capabilities"] == ["structured_output", "tool_calling"]
    assert payload["response_schema"]["strict"] is True
    assert payload["messages"][1]["native_assistant"]["reasoning_content"] == "hidden reasoning"
    assert payload["messages"][1]["native_assistant"]["deployment_id"] == "resolved"
    assert payload["messages"][2]["tool_call_id"] == "call-1"
    assert frozen.request() == r
    assert model_message_payload(frozen.request().messages[1]) == model_message_payload(
        r.messages[1]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"logical_model": "other"},
        {"timeout_seconds": 12.5},
        {"allow_fallback": False},
        {"max_output_tokens": 17},
        {"required_tool": "other"},
        {
            "required_capabilities": frozenset(
                {
                    ModelCapability.STRUCTURED_OUTPUT,
                    ModelCapability.TOOL_CALLING,
                    ModelCapability.VISION,
                }
            )
        },
    ],
)
def test_each_request_scalar_changes_digest(change: RequestOverrides) -> None:
    r = rich_request()
    assert (
        api().freeze_model_request(r).digest
        != api().freeze_model_request(replace(r, **change)).digest
    )


@pytest.mark.parametrize(
    "target",
    [
        "text",
        "image",
        "native_content",
        "reasoning",
        "arguments",
        "native_deployment",
        "call_id",
        "call_name",
        "result",
        "schema",
        "schema_name",
        "tool_name",
        "description",
        "parameters",
    ],
)
def test_nested_semantic_changes_change_digest(target: str) -> None:
    r = rich_request()
    before = api().freeze_model_request(r).digest
    if target == "text":
        object.__setattr__(r.messages[0], "content", "changed")
    elif target == "image":
        object.__setattr__(
            r.messages[0], "content", ({"type": "image_url", "image_url": {"url": "data:changed"}},)
        )
    elif target in {
        "native_content",
        "reasoning",
        "arguments",
        "native_deployment",
        "call_id",
        "call_name",
    }:
        n = r.messages[1].native_assistant
        assert n is not None
        if target == "native_content":
            object.__setattr__(n, "content", "changed")
            object.__setattr__(r.messages[1], "content", "changed")
        elif target == "reasoning":
            object.__setattr__(n, "reasoning_content", "changed")
        elif target == "arguments":
            object.__setattr__(n.tool_calls[0], "arguments", '{"changed":true}')
        elif target == "native_deployment":
            object.__setattr__(n, "deployment_id", "other")
        elif target == "call_name":
            object.__setattr__(n.tool_calls[0], "name", "other")
        else:
            object.__setattr__(n.tool_calls[0], "id", "changed")
            object.__setattr__(r.messages[2], "tool_call_id", "changed")
    elif target == "result":
        object.__setattr__(r.messages[2], "content", "changed")
    elif target == "schema":
        object.__setattr__(r.response_schema, "schema", {"type": "string"})
    elif target == "schema_name":
        object.__setattr__(r.response_schema, "name", "Other")
    else:
        field = {"tool_name": "name", "description": "description", "parameters": "parameters"}[
            target
        ]
        value = {"type": "string"} if target == "parameters" else "changed"
        object.__setattr__(r.tools[1], field, value)
    assert api().freeze_model_request(r).digest != before


def test_native_continuation_and_tool_call_id_do_not_collapse_into_wire_only_hash() -> None:
    plain = request()
    assert (
        api().freeze_model_request(plain).digest
        != api().freeze_model_request(replace(plain, native_tool_continuation=True)).digest
    )
    r = rich_request()
    object.__setattr__(r.messages[2], "tool_call_id", "unbound")
    with pytest.raises(ValueError):
        api().freeze_model_request(r)


def test_freeze_rebuilds_all_nested_objects_and_each_read_is_independent() -> None:
    r = rich_request()
    schema: MutableEnumSchema = {"type": "object", "properties": {"x": {"enum": ["before"]}}}
    params: MutableEnumSchema = {"type": "object", "properties": {"q": {"enum": ["before"]}}}
    object.__setattr__(r.response_schema, "schema", schema)
    object.__setattr__(r.tools[0], "parameters", params)
    frozen = api().freeze_model_request(r)
    before = frozen.digest
    a = frozen.request()
    b = frozen.request()
    assert a.messages[1].native_assistant is not None
    assert b.messages[1].native_assistant is not None
    assert r.messages[1].native_assistant is not None
    assert a is not b and a.messages[0] is not b.messages[0]
    assert a.response_schema is not b.response_schema
    assert a.tools[0] is not b.tools[0]
    assert a.messages[1].native_assistant is not b.messages[1].native_assistant
    assert (
        a.messages[1].native_assistant.tool_calls[0]
        is not b.messages[1].native_assistant.tool_calls[0]
    )
    schema["properties"]["x"]["enum"].append("after")
    params["properties"]["q"]["enum"].append("after")
    object.__setattr__(r, "max_output_tokens", 999)
    object.__setattr__(r.messages[1].native_assistant.tool_calls[0], "arguments", '{"evil":true}')
    object.__setattr__(a.messages[0], "content", "mutated copy")
    object.__setattr__(a.response_schema, "schema", {"type": "null"})
    object.__setattr__(a.tools[0], "parameters", {"type": "null"})
    assert frozen.digest == before
    assert frozen.request() == b
    assert frozen.request().max_output_tokens == 16
    restored = frozen.request()
    assert restored.messages[1].native_assistant is not None
    assert restored.messages[1].native_assistant.tool_calls[0].arguments == '{"嵌套":{"x":[1,true]}}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "bad"}, object(), "\ud800"])
def test_nonfinite_nonjson_and_invalid_utf8_fail_closed_with_private_error(bad: object) -> None:
    r = rich_request()
    object.__setattr__(r.tools[0], "parameters", {"private-key": bad})
    with pytest.raises(ValueError, match="^invalid model request snapshot$"):
        api().freeze_model_request(r)


def test_unknown_native_types_and_hidden_state_fail_closed() -> None:
    class ExtraNative(NativeAssistantMessage):
        pass

    r = rich_request()
    object.__setattr__(r.messages[1], "native_assistant", ExtraNative("resolved", "private", ()))
    with pytest.raises(ValueError):
        api().freeze_model_request(r)

    class ExtraSchema(StructuredResponseSchema):
        pass

    r = rich_request()
    extra = ExtraSchema("Verdict", {})
    object.__setattr__(extra, "hidden", "private")
    object.__setattr__(r, "response_schema", extra)
    with pytest.raises(ValueError):
        api().freeze_model_request(r)


def test_private_repr_contains_no_prompt_native_tools_schema_url_or_secret() -> None:
    r = rich_request()
    f = api().freeze_model_request(r)
    d = deployment(api_base="https://private-provider.invalid/v1", secret_ref="private-secret")
    b = reserve(d, r)
    shown = repr(f) + repr(b) + repr(b.pricing()) + repr(trust(d))
    for private in [
        "私密",
        "private",
        "hidden reasoning",
        "data:image",
        "Verdict",
        "lookup",
        "private-provider",
        "private-secret",
    ]:
        assert private not in shown


@pytest.mark.parametrize(
    "bad",
    [
        None,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal(-1),
        Decimal("1000000.000001"),
        Decimal("0.0000001"),
        True,
    ],
)
def test_effective_rates_revalidated_even_after_frozen_dataclass_tamper(bad: object) -> None:
    p = api().KnownPricing(Decimal(1), Decimal(2))
    object.__setattr__(p, "output_per_million_usd", bad)
    with pytest.raises(ValueError, match="^invalid admission bounds$"):
        reserve(effective_pricing=p)


def test_known_zero_effective_override_is_explicit_and_no_provider_config_enters_digest() -> None:
    r = rich_request()
    d = deployment()
    b = reserve(d, r, effective_pricing=api().KnownPricing(Decimal(0), Decimal(0)))
    other = reserve(
        replace(d, id="other", api_base="https://other.invalid/v1", secret_ref="other-secret"), r
    )
    assert b.known_zero and b.cost_usd == 0
    assert b.request_digest == other.request_digest == api().freeze_model_request(r).digest
    assert b.request() == r
    object.__setattr__(r, "max_output_tokens", 1000)
    copy = b.request()
    object.__setattr__(copy.tools[0], "description", "changed")
    assert b.request().max_output_tokens == 16
    assert b.request().tools[0].description == "private description"


def test_key_insertion_and_capability_order_do_not_change_logical_fingerprint() -> None:
    a = rich_request()
    b = rich_request()
    object.__setattr__(
        b.response_schema,
        "schema",
        {
            "properties": {"a": {"enum": ["私密", "x"]}},
            "type": "object",
        },
    )
    object.__setattr__(
        b,
        "required_capabilities",
        [
            ModelCapability.TOOL_CALLING,
            ModelCapability.STRUCTURED_OUTPUT,
        ],
    )
    assert api().freeze_model_request(a).digest == api().freeze_model_request(b).digest


def test_invalid_selected_request_and_cyclic_json_fail_closed() -> None:
    r = request()
    object.__setattr__(r, "max_output_tokens", True)
    with pytest.raises(ValueError, match="^invalid admission bounds$"):
        reserve(r=r)
    r = rich_request()
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    object.__setattr__(r.response_schema, "schema", cycle)
    with pytest.raises(ValueError, match="^invalid model request snapshot$"):
        api().freeze_model_request(r)


def test_success_and_unsupported_are_pure_zero_io(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import io
    import socket

    helper = api()
    d, r = deployment(), rich_request()
    t = trust(d)

    def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("pure helper performed I/O")

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", forbidden)
        m.setattr(io, "open", forbidden)
        m.setattr(socket, "socket", forbidden)
        result = helper.compute_admission_bounds(d, r, trusted_context=t)
        assert result.total_tokens == 8192
        assert helper.freeze_model_request(r).request() == r
        with pytest.raises(helper.UnsupportedAdmissionBounds):
            helper.compute_admission_bounds(d, r)
        object.__setattr__(d, "input_per_million_usd", None)
        object.__setattr__(d, "output_per_million_usd", None)
        with pytest.raises(helper.UnsupportedAdmissionBounds):
            helper.compute_admission_bounds(d, r, trusted_context=t)


def test_selected_gateway_clamp_is_validated_without_mutating_logical_request() -> None:
    from agent_hub.models.gateway import ModelGateway

    d = deployment(max_output_tokens=32)
    original = request(max_output_tokens=1_000_000)
    effective = ModelGateway._request_for_deployment(original, d, 128)
    assert effective.max_output_tokens == 32
    assert original.max_output_tokens == 1_000_000
    with pytest.raises(ValueError):
        reserve(d, original)
    b = reserve(d, effective)
    assert b.total_tokens == 8192
    assert b.request().max_output_tokens == 32
    assert b.request_digest != api().freeze_model_request(original).digest
