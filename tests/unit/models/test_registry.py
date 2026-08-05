from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import Deployment, ModelCapability, ModelMessage, ModelRequest


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


@pytest.mark.parametrize("logical_model", ["", "UPPER", " padded", "a" * 129])
def test_request_rejects_invalid_logical_model(logical_model: str) -> None:
    with pytest.raises(ValueError):
        ModelRequest(logical_model=logical_model, messages=())


@pytest.mark.parametrize("timeout", [0, -1, nan, inf])
def test_request_timeout_must_be_positive_and_finite(timeout: float) -> None:
    with pytest.raises(ValueError):
        ModelRequest(logical_model="primary", messages=(), timeout_seconds=timeout)


def test_message_rejects_noncanonical_or_empty_multimodal_content() -> None:
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=())
    with pytest.raises(ValueError):
        ModelMessage(role="user", content=("not-an-object",))  # type: ignore[arg-type]


def test_models_package_exposes_the_public_contract() -> None:
    from agent_hub.models import Deployment as PublicDeployment
    from agent_hub.models import LiteLLMClient as PublicClient
    from agent_hub.models import ModelRegistry as PublicRegistry

    assert PublicDeployment is Deployment
    assert PublicClient.__name__ == "LiteLLMClient"
    assert PublicRegistry is ModelRegistry
