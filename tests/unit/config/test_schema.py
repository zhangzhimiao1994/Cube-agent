from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hub.config.schema import (
    AgentDefinition,
    DeploymentDefinition,
    LogicalModelDefinition,
    PlatformConfig,
)


def deployment(**overrides: object) -> DeploymentDefinition:
    values: dict[str, object] = {
        "provider": "openai",
        "model": "gpt-5",
        "secret_ref": "OPENAI_API_KEY",
        "quota_scope_id": "primary",
    }
    values.update(overrides)
    return DeploymentDefinition.model_validate(values)


def logical_model(*, fallback_model: str | None = None) -> LogicalModelDefinition:
    return LogicalModelDefinition(deployments=[deployment()], fallback_model=fallback_model)


def agent(identifier: str = "researcher", model: str = "primary") -> AgentDefinition:
    return AgentDefinition(
        id=identifier,
        role="researcher",
        prompt="Research carefully.",
        model=model,
    )


def test_agent_requires_existing_logical_model() -> None:
    with pytest.raises(ValidationError, match="unknown logical model"):
        PlatformConfig(models={}, agents=[agent(model="missing")])


def test_agent_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate agent id"):
        PlatformConfig(
            models={"primary": logical_model()},
            agents=[agent(), agent()],
        )


@pytest.mark.parametrize(
    ("models", "message"),
    [
        (
            {"primary": logical_model(fallback_model="missing")},
            "unknown fallback model",
        ),
        (
            {"primary": logical_model(fallback_model="primary")},
            "cannot fall back to itself",
        ),
    ],
)
def test_fallback_must_reference_another_existing_model(
    models: dict[str, LogicalModelDefinition], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        PlatformConfig(models=models, agents=[])


@pytest.mark.parametrize("identifier", ["", "Upper", "_hidden", "has space", "a/b"])
def test_agent_id_must_be_a_safe_identifier(identifier: str) -> None:
    with pytest.raises(ValidationError):
        agent(identifier=identifier)


@pytest.mark.parametrize("identifier", ["", "Upper", "_hidden", "has space", "a/b"])
def test_logical_model_key_must_be_a_safe_identifier(identifier: str) -> None:
    with pytest.raises(ValidationError, match="logical model key"):
        PlatformConfig(models={identifier: logical_model()}, agents=[])


@pytest.mark.parametrize("field", ["provider", "model", "secret_ref", "quota_scope_id"])
def test_deployment_rejects_blank_critical_strings(field: str) -> None:
    with pytest.raises(ValidationError):
        deployment(**{field: "   "})


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_concurrency": 0},
        {"max_concurrency": 1001},
        {"target_utilization": 0.49},
        {"target_utilization": 0.91},
        {"reserved_slots": -1},
        {"max_concurrency": 2, "reserved_slots": 2},
        {"rpm": 0},
        {"tpm": -1},
    ],
)
def test_deployment_enforces_capacity_boundaries(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        deployment(**overrides)


def test_mutable_defaults_are_independent() -> None:
    first_deployment = deployment()
    second_deployment = deployment()
    first_deployment.capabilities.add("vision")

    first_agent = agent("first")
    second_agent = agent("second")
    first_agent.skills.append("browser")

    assert first_deployment.capabilities == {"text", "vision"}
    assert second_deployment.capabilities == {"text"}
    assert first_agent.skills == ["browser"]
    assert second_agent.skills == []


@pytest.mark.parametrize(
    ("validator", "value"),
    [
        (DeploymentDefinition, {**deployment().model_dump(), "unknown": True}),
        (
            LogicalModelDefinition,
            {"deployments": [deployment()], "unknown": True},
        ),
        (AgentDefinition, {**agent().model_dump(), "unknown": True}),
        (PlatformConfig, {"models": {}, "agents": [], "unknown": True}),
    ],
)
def test_configuration_models_reject_unknown_fields(
    validator: type[DeploymentDefinition]
    | type[LogicalModelDefinition]
    | type[AgentDefinition]
    | type[PlatformConfig],
    value: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validator.model_validate(value)


@pytest.mark.parametrize("capabilities", [set(), {"audio"}])
def test_capabilities_are_non_empty_and_known(capabilities: set[str]) -> None:
    with pytest.raises(ValidationError):
        deployment(capabilities=capabilities)


def test_deployment_accepts_generation_capabilities() -> None:
    definition = deployment(
        capabilities={"text", "image_generation", "video_generation", "audio_generation"}
    )
    runtime = definition.to_deployment(deployment_id="primary-1", logical_model="primary")

    assert definition.capabilities == {
        "text",
        "image_generation",
        "video_generation",
        "audio_generation",
    }
    assert {str(capability) for capability in runtime.capabilities} == {
        "text",
        "image_generation",
        "video_generation",
        "audio_generation",
    }


@pytest.mark.parametrize(
    "skills",
    [
        [""],
        [" browser"],
        ["browser "],
        ["has space"],
        ["browser", "browser"],
        ["a" * 129],
        [f"skill-{index}" for index in range(129)],
    ],
)
def test_skills_are_bounded_unique_safe_identifiers(skills: list[str]) -> None:
    with pytest.raises(ValidationError):
        AgentDefinition(
            id="researcher",
            role="researcher",
            prompt="Research carefully.",
            model="primary",
            skills=skills,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", " openai"),
        ("model", "gpt-5 "),
        ("secret_ref", " OPENAI_API_KEY"),
        ("quota_scope_id", "primary "),
        ("api_base", " "),
        ("api_base", "https://proxy.example.com "),
    ],
)
def test_deployment_strings_reject_edge_whitespace(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        deployment(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", " assistant"), ("prompt", "Help. "), ("model", " primary")],
)
def test_agent_strings_reject_edge_whitespace(field: str, value: str) -> None:
    values = agent().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(values)


def test_fallback_rejects_edge_whitespace() -> None:
    with pytest.raises(ValidationError):
        LogicalModelDefinition(deployments=[deployment()], fallback_model=" backup")


def test_identifiers_have_a_length_limit() -> None:
    with pytest.raises(ValidationError):
        agent(identifier="a" * 129)
    with pytest.raises(ValidationError, match="logical model key"):
        PlatformConfig(models={"a" * 129: logical_model()}, agents=[])


@pytest.mark.parametrize("target", [float("nan"), float("inf"), float("-inf")])
def test_target_utilization_must_be_finite(target: float) -> None:
    with pytest.raises(ValidationError):
        deployment(target_utilization=target)


def test_configuration_collections_have_upper_bounds() -> None:
    with pytest.raises(ValidationError):
        LogicalModelDefinition(deployments=[deployment() for _ in range(129)])
    with pytest.raises(ValidationError):
        PlatformConfig(
            models={f"model-{index}": logical_model() for index in range(257)},
            agents=[],
        )
    with pytest.raises(ValidationError):
        PlatformConfig(
            models={"primary": logical_model()},
            agents=[agent(identifier=f"agent-{index}") for index in range(1025)],
        )


def test_deployment_pricing_round_trips_and_builds_runtime_deployment() -> None:
    definition = deployment(
        input_per_million_usd="0.150000",
        output_per_million_usd=Decimal(0),
    )

    restored = DeploymentDefinition.model_validate_json(definition.model_dump_json())
    runtime = restored.to_deployment(deployment_id="primary-1", logical_model="primary")

    assert restored.input_per_million_usd == Decimal("0.150000")
    assert restored.output_per_million_usd == Decimal(0)
    assert runtime.input_per_million_usd == Decimal("0.150000")
    assert runtime.output_per_million_usd == Decimal(0)
    assert runtime.provider_model == "openai/gpt-5"
    assert runtime.request_model == "gpt-5"


def test_deployment_pricing_distinguishes_unknown_from_explicit_free() -> None:
    unknown = deployment()
    free = deployment(input_per_million_usd="0", output_per_million_usd="0")

    assert unknown.input_per_million_usd is None
    assert unknown.output_per_million_usd is None
    assert free.input_per_million_usd == Decimal(0)
    assert free.output_per_million_usd == Decimal(0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_per_million_usd": "0", "output_per_million_usd": None},
        {"input_per_million_usd": None, "output_per_million_usd": "0"},
        {"input_per_million_usd": "NaN", "output_per_million_usd": "0"},
        {"input_per_million_usd": "-0.01", "output_per_million_usd": "0"},
        {"input_per_million_usd": "0.0000001", "output_per_million_usd": "0"},
        {"input_per_million_usd": 0.1, "output_per_million_usd": "0"},
    ],
)
def test_deployment_pricing_rejects_partial_or_invalid_decimals(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        deployment(**overrides)
