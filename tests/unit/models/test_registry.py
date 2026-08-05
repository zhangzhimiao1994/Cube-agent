from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredResponseSchema,
    TokenUsage,
    ToolCall,
)


def test_deployment_has_safe_litellm_defaults() -> None:
    deployment = Deployment(id="primary-1", logical_model="primary")

    assert deployment.provider_model == "openai/gpt-4o-mini"
    assert deployment.api_base == "http://litellm:4000/v1"
    assert deployment.secret_ref == "litellm-internal-key"
    assert deployment.capabilities == frozenset({ModelCapability.TEXT})


def test_registry_filters_by_all_required_capabilities() -> None:
    text = Deployment(id="text", logical_model="primary")
    vision = Deployment(
        id="vision",
        logical_model="primary",
        capabilities={ModelCapability.TEXT, ModelCapability.VISION},  # type: ignore[arg-type]
    )
    registry = ModelRegistry([vision, text])

    assert registry.candidates("primary", {ModelCapability.VISION}) == (vision,)
    assert registry.candidates("primary", frozenset()) == (text, vision)


def test_registry_returns_the_same_deployment_for_repeated_agent_role_lookups() -> None:
    deployment = Deployment(id="shared", logical_model="primary")
    registry = ModelRegistry([deployment])

    researcher_candidate = registry.candidates("primary")
    writer_candidate = registry.candidates("primary")

    assert researcher_candidate[0] is writer_candidate[0] is deployment


def test_registry_rejects_duplicate_deployment_ids() -> None:
    with pytest.raises(ValueError, match="duplicate deployment id: 'shared'"):
        ModelRegistry([
            Deployment(id="shared", logical_model="first"),
            Deployment(id="shared", logical_model="second"),
        ])


def test_registry_raises_safe_error_when_no_deployment_has_capabilities() -> None:
    registry = ModelRegistry([Deployment(id="text", logical_model="primary")])

    with pytest.raises(
        NoCapableDeployment,
        match="^no capable deployment for logical model 'primary': vision$",
    ):
        registry.candidates("primary", {ModelCapability.VISION})


@pytest.mark.parametrize("logical_model", ["", " padded", "UPPER", "a" * 129])
def test_registry_rejects_invalid_logical_model_lookup(logical_model: str) -> None:
    registry = ModelRegistry([Deployment(id="text", logical_model="primary")])

    with pytest.raises(ValueError, match="logical_model must be a safe identifier"):
        registry.candidates(logical_model)


def test_registry_orders_candidates_by_id_independent_of_input_order() -> None:
    alpha = Deployment(id="alpha", logical_model="primary", weight=1)
    zulu = Deployment(id="zulu", logical_model="primary", weight=999)

    assert ModelRegistry([zulu, alpha]).candidates("primary") == (alpha, zulu)


def test_frozen_contracts_deeply_normalize_caller_collections() -> None:
    capabilities = {ModelCapability.TEXT}
    parts: list[dict[str, object]] = [{"type": "text", "text": "hello"}]
    deployment = Deployment(
        id="primary", logical_model="primary", capabilities=capabilities  # type: ignore[arg-type]
    )
    message = ModelMessage(role="user", content=parts)  # type: ignore[arg-type]
    request = ModelRequest(
        logical_model="primary",
        messages=[message],  # type: ignore[arg-type]
        required_capabilities=capabilities,  # type: ignore[arg-type]
    )

    capabilities.add(ModelCapability.VISION)
    parts[0]["text"] = "changed"

    assert deployment.capabilities == frozenset({ModelCapability.TEXT})
    assert request.required_capabilities == frozenset({ModelCapability.TEXT})
    assert request.messages == (message,)
    assert message.content[0]["text"] == "hello"  # type: ignore[index]
    with pytest.raises(TypeError):
        message.content[0]["text"] = "nope"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        deployment.weight = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        request.messages = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("id", "UPPER"),
        ("id", " padded"),
        ("id", "a" * 129),
        ("logical_model", ""),
        ("logical_model", "not safe"),
        ("provider_model", " openai/model"),
        ("secret_ref", "   "),
        ("quota_scope_id", "account "),
        ("api_base", "ftp://proxy.example.com/v1"),
        ("api_base", "proxy.example.com/v1"),
        ("api_base", "https://user:password@proxy.example.com/v1"),
    ],
)
def test_deployment_rejects_invalid_strings(field: str, value: str) -> None:
    values: dict[str, object] = {"id": "primary", "logical_model": "primary"}
    values[field] = value

    with pytest.raises(ValueError):
        Deployment(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_concurrency": 0},
        {"max_concurrency": 1001},
        {"target_utilization": 0.49},
        {"target_utilization": 0.91},
        {"target_utilization": nan},
        {"target_utilization": inf},
        {"target_utilization": "0.8"},
        {"reserved_slots": -1},
        {"max_concurrency": 2, "reserved_slots": 2},
        {"rpm": 0},
        {"tpm": -1},
        {"weight": 0},
        {"weight": True},
        {"max_concurrency": True},
        {"reserved_slots": True},
        {"rpm": 1.5},
        {"tpm": True},
        {"capabilities": set()},
    ],
)
def test_deployment_enforces_numeric_and_collection_boundaries(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        Deployment(id="primary", logical_model="primary", **overrides)  # type: ignore[arg-type]


def test_deployment_accepts_inclusive_capacity_boundaries() -> None:
    minimum = Deployment(
        id="minimum",
        logical_model="primary",
        max_concurrency=1,
        target_utilization=0.5,
        reserved_slots=0,
        rpm=1,
        tpm=1,
        weight=1,
    )
    maximum = Deployment(
        id="maximum",
        logical_model="primary",
        max_concurrency=1000,
        target_utilization=0.9,
        reserved_slots=999,
    )

    assert minimum.target_utilization == 0.5
    assert maximum.target_utilization == 0.9


def test_deployment_repr_omits_secret_reference() -> None:
    deployment = Deployment(
        id="primary", logical_model="primary", secret_ref="secret-reference-sentinel"
    )

    assert "secret-reference-sentinel" not in repr(deployment)


def test_api_base_enforces_config_schema_length_boundary() -> None:
    prefix = "https://proxy.example.com/"
    maximum = prefix + "a" * (2048 - len(prefix))

    assert len(maximum) == 2048
    assert Deployment(id="maximum", logical_model="primary", api_base=maximum).api_base == maximum
    with pytest.raises(ValueError, match="api_base"):
        Deployment(id="too-long", logical_model="primary", api_base=maximum + "a")


@pytest.mark.parametrize(
    "api_base",
    [
        "https://proxy.example.com/\nadmin",
        "https://proxy.example.com/\x00admin",
        "https://proxy.example.com/\x7fadmin",
        "https://proxy example.com/v1",
        "https://proxy.example.com:not-a-port/v1",
        "https://[broken/v1",
    ],
)
def test_api_base_rejects_control_characters_and_malformed_authority(api_base: str) -> None:
    with pytest.raises(ValueError, match="api_base"):
        Deployment(id="invalid-url", logical_model="primary", api_base=api_base)


@pytest.mark.parametrize("logical_model", ["", "UPPER", " padded", "a" * 129])
def test_request_rejects_invalid_logical_model(logical_model: str) -> None:
    with pytest.raises(ValueError):
        ModelRequest(logical_model=logical_model, messages=())


@pytest.mark.parametrize("timeout", [0, -1, nan, inf, True])
def test_request_timeout_must_be_positive_finite_number(timeout: float) -> None:
    with pytest.raises(ValueError):
        ModelRequest(logical_model="primary", messages=(), timeout_seconds=timeout)


@pytest.mark.parametrize("value", [b"bytes", {"set"}, iter(["generator"])])
def test_structured_schema_rejects_non_json_iterables(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        StructuredResponseSchema(name="strict_json", schema={"value": value})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_structured_schema_rejects_nonfinite_numbers_at_any_depth(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        StructuredResponseSchema(name="strict_json", schema={"nested": [value]})  # type: ignore[dict-item]


def test_structured_schema_preserves_json_boolean_type() -> None:
    schema = StructuredResponseSchema(name="strict_json", schema={"flag": True})

    assert schema.schema["flag"] is True
    assert type(schema.schema["flag"]) is bool


def test_structured_schema_rejects_json_scalar_subclasses() -> None:
    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    class FloatSubclass(float):
        pass

    for value in (StringSubclass("value"), IntegerSubclass(1), FloatSubclass(1.0)):
        with pytest.raises(TypeError):
            StructuredResponseSchema(name="strict_json", schema={"value": value})


@pytest.mark.parametrize("value", [True, False, 1.5, nan, inf, -1])
def test_token_usage_requires_exact_nonnegative_integers(value: object) -> None:
    with pytest.raises(ValueError):
        TokenUsage(prompt_tokens=value, completion_tokens=0, total_tokens=0)  # type: ignore[arg-type]


def test_token_usage_rejects_integer_subclasses() -> None:
    class IntegerSubclass(int):
        pass

    with pytest.raises(ValueError):
        TokenUsage(IntegerSubclass(1), 0, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"text": 1},
        {"tool_calls": [object()]},
        {"usage": {"prompt_tokens": 1}},
        {"provider_metadata": {"unknown": "value"}},
        {"provider_metadata": {"model": ["mutable"]}},
        {"provider_metadata": {"created": True}},
        {"provider_metadata": {"created": 1.5}},
        {"provider_metadata": {"created": 2**63}},
        {"provider_metadata": {"model": "x" * 257}},
    ],
)
def test_model_response_rejects_invalid_nested_contracts(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {"text": None}
    values.update(overrides)
    with pytest.raises((TypeError, ValueError)):
        ModelResponse(**values)  # type: ignore[arg-type]


def test_model_response_metadata_rejects_scalar_subclasses() -> None:
    class IntegerSubclass(int):
        pass

    class StringSubclass(str):
        pass

    with pytest.raises(ValueError):
        ModelResponse(text=None, provider_metadata={"created": IntegerSubclass(1)})
    with pytest.raises(ValueError):
        ModelResponse(text=None, provider_metadata={"model": StringSubclass("safe")})


def test_model_response_copies_and_freezes_input_collections() -> None:
    tool = ToolCall(id="call_safe", name="lookup", arguments={"query": "safe"})
    tool_calls = [tool]
    metadata: dict[str, object] = {"model": "openai/safe-model", "created": 1}

    response = ModelResponse(
        text="safe",
        tool_calls=tool_calls,  # type: ignore[arg-type]
        usage=TokenUsage(1, 2, 3),
        provider_metadata=metadata,  # type: ignore[arg-type]
    )
    tool_calls.clear()
    metadata["model"] = "changed"

    assert response.tool_calls == (tool,)
    assert dict(response.provider_metadata) == {"model": "openai/safe-model", "created": 1}
    with pytest.raises(TypeError):
        response.provider_metadata["model"] = "changed"  # type: ignore[index]


def test_content_bearing_value_objects_are_explicitly_unhashable() -> None:
    message = ModelMessage(role="user", content="safe")
    schema = StructuredResponseSchema(name="safe", schema={})
    request = ModelRequest(logical_model="primary", messages=(message,))
    tool = ToolCall(id="call_safe", name="lookup", arguments={})
    response = ModelResponse(text="safe")

    for value in (message, schema, request, tool, response):
        with pytest.raises(TypeError):
            hash(value)


def test_message_rejects_noncanonical_or_empty_multimodal_content() -> None:
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=())
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=("not-an-object",))  # type: ignore[arg-type]


def test_message_rejects_empty_and_nonempty_one_shot_iterables() -> None:
    empty = iter(())
    populated = iter(({"type": "text", "text": "hello"},))

    with pytest.raises(TypeError, match="list or tuple"):
        ModelMessage(role="user", content=empty)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="list or tuple"):
        ModelMessage(role="user", content=populated)  # type: ignore[arg-type]


def test_request_rejects_non_model_message_elements() -> None:
    class MutableMessage:
        def __init__(self) -> None:
            self.role = "user"
            self.content = "mutable"

    for invalid in ({"role": "user", "content": "dictionary"}, MutableMessage()):
        with pytest.raises(ValueError, match="messages must contain only ModelMessage"):
            ModelRequest(
                logical_model="primary",
                messages=[invalid],  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("allow_fallback", ["false", 0, 1, None, [], {}])
def test_request_requires_exact_boolean_allow_fallback(allow_fallback: object) -> None:
    with pytest.raises(ValueError, match="allow_fallback must be a boolean"):
        ModelRequest(
            logical_model="primary",
            messages=(),
            allow_fallback=allow_fallback,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("response_schema", [object(), {}, "schema"])
def test_request_rejects_invalid_response_schema(response_schema: object) -> None:
    with pytest.raises(ValueError, match="response_schema"):
        ModelRequest(
            logical_model="primary",
            messages=(),
            response_schema=response_schema,  # type: ignore[arg-type]
        )


def test_models_package_exposes_the_public_contract() -> None:
    from agent_hub.models import Deployment as PublicDeployment
    from agent_hub.models import LiteLLMClient as PublicClient
    from agent_hub.models import ModelRegistry as PublicRegistry

    assert PublicDeployment is Deployment
    assert PublicClient.__name__ == "LiteLLMClient"
    assert PublicRegistry is ModelRegistry
