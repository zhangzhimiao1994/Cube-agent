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
