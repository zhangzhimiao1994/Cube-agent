from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from agent_hub.config.schema import DeploymentDefinition, PlatformConfig
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.routing_policy import (
    DeploymentRoutingConstraint,
    constrain_deployments_for_routing,
)
from agent_hub.runtime.defaults import (
    _assign_models_to_roles,
    _deployments,
    _dispatch_parallelism,
    _logical_model_capacity,
    _role_model_routing_matrix_payload,
)
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose


def _deployment(scope: str, slots: int, *, model: str = "test-model") -> dict[str, object]:
    return {
        "provider": "test",
        "model": model,
        "credential_ref": f"{scope}-key",
        "quota_scope_id": scope,
        "max_concurrency": slots,
        "target_utilization": 0.8,
    }


def _config(models: dict[str, list[dict[str, object]]]) -> PlatformConfig:
    return PlatformConfig.model_validate({
        "models": {name: {"deployments": deployments} for name, deployments in models.items()},
        "agents": [],
    })


def _role(model: str) -> RoleAssignment:
    return RoleAssignment(
        id=model,
        role=model,
        purpose=RolePurpose.EXECUTE,
        mission="Complete the assigned work.",
        must_answer=("result",),
        allowed_tools=(),
        forbidden_actions=("no external requests",),
        skills=(),
        output_schema={"summary": "string"},
        model=model,
    )


@pytest.mark.parametrize("capacity", [_logical_model_capacity, _dispatch_parallelism])
def test_shared_scope_aliases_do_not_add_dispatch_slots(capacity: Callable[..., int]) -> None:
    config = _config({"main": [_deployment("shared", 1), _deployment("shared", 1)]})

    assert capacity(config, "main") == 1


def test_roles_on_different_models_share_one_scope_limit() -> None:
    config = _config({
        "main": [_deployment("shared", 1)],
        "review": [_deployment("shared", 1)],
    })

    assert _dispatch_parallelism(config, "main", (_role("main"), _role("review"))) == 1


@pytest.mark.parametrize("capacity", [_logical_model_capacity, _dispatch_parallelism])
def test_unselected_model_can_tighten_a_shared_scope_policy(capacity: Callable[..., int]) -> None:
    config = _config({
        "main": [_deployment("shared", 10)],
        "review": [_deployment("shared", 1)],
    })

    assert capacity(config, "main") == 1


def test_only_independent_scopes_add_dispatch_slots() -> None:
    config = _config({
        "main": [_deployment("shared", 10), _deployment("independent", 5)],
        "review": [_deployment("shared", 3)],
        "unused": [_deployment("unused", 10)],
    })

    assert _dispatch_parallelism(config, "main", (_role("main"), _role("review"))) == 6


@pytest.mark.parametrize("capacity", [_logical_model_capacity, _dispatch_parallelism])
def test_harness_constraint_excludes_unselected_deployment_policy(
    capacity: Callable[..., int],
) -> None:
    config = _config({"main": [
        _deployment("shared", 1, model="excluded"),
        _deployment("shared", 5, model="selected"),
    ]})
    constraint = DeploymentRoutingConstraint("main", "test", "selected")

    assert capacity(config, "main", deployment_constraint=constraint) == 4


@pytest.mark.parametrize("capacity", [_logical_model_capacity, _dispatch_parallelism])
def test_constraint_preserves_other_models_shared_scope_policy(capacity: Callable[..., int]) -> None:
    config = _config({
        "main": [_deployment("shared", 10, model="selected")],
        "review": [_deployment("shared", 1)],
    })
    constraint = DeploymentRoutingConstraint("main", "test", "selected")

    assert capacity(config, "main", deployment_constraint=constraint) == 1


class CountingDeployments(list[DeploymentDefinition]):
    visits: int = 0

    def __iter__(self) -> Iterator[DeploymentDefinition]:
        for deployment in super().__iter__():
            self.visits += 1
            yield deployment


@pytest.mark.parametrize("model_count", [2, 8])
@pytest.mark.parametrize("consumer", ["assignment", "routing_matrix"])
def test_capacity_table_visits_catalog_once_per_planning_round(
    model_count: int,
    consumer: str,
) -> None:
    config = _config({
        f"model{index}": [_deployment("shared", 10), _deployment(f"scope{index}", 5)]
        for index in range(model_count)
    })
    counted = []
    for definition in config.models.values():
        deployments = CountingDeployments(definition.deployments)
        definition.deployments = deployments
        counted.append(deployments)

    # Empty roles isolate catalog preparation from per-role capability ranking.
    for round_number in (1, 2):
        if consumer == "assignment":
            assert _assign_models_to_roles(
                (), config, default_model="model0", task="Summarize the result.",
            ) == ()
        else:
            assert _role_model_routing_matrix_payload(
                (), (), config, default_model="model0", task="Summarize the result.",
            ) == ((), False)

        assert sum(deployments.visits for deployments in counted) == round_number * model_count * 2


@pytest.mark.parametrize("capacity", [_logical_model_capacity, _dispatch_parallelism])
def test_capacity_recomputes_after_mutating_the_same_config(capacity: Callable[..., int]) -> None:
    config = _config({
        "main": [_deployment("shared", 10)],
        "review": [_deployment("shared", 1)],
    })

    assert capacity(config, "main") == 1

    config.models["review"].deployments[0].max_concurrency = 5

    assert capacity(config, "main") == 4


@pytest.mark.parametrize("constrained", [False, True])
@pytest.mark.parametrize("rpm,tpm", [(None, None), (30, None), (None, 1000), (30, 1000)])
@pytest.mark.parametrize(
    "slots,utilization,reserved,shared_limit",
    [
        pytest.param(9, 0.5, 0, 4, id="utilization-rounds-down"),
        pytest.param(10, 0.9, 8, 2, id="reserved-slots-dominate"),
        pytest.param(1, 0.5, 0, 1, id="one-slot-floor"),
    ],
)
def test_capacity_matches_pool_policy_with_utilization_reserves_and_optional_rates(
    slots: int,
    utilization: float,
    reserved: int,
    shared_limit: int,
    rpm: int | None,
    tpm: int | None,
    constrained: bool,
) -> None:
    restrictive = {
        **_deployment("shared", slots),
        "target_utilization": utilization,
        "reserved_slots": reserved,
        "rpm": rpm,
        "tpm": tpm,
    }
    config = _config({
        "main": [
            _deployment("shared", 10),
            _deployment("shared", 1, model="excluded"),
            _deployment("independent", 5),
        ],
        "review": [restrictive],
        "unused": [_deployment("unused", 100)],
    })
    constraint = (
        DeploymentRoutingConstraint("main", "TEST", "test/test-model")
        if constrained else None
    )
    catalog = constrain_deployments_for_routing(_deployments(config), constraint)
    pool = CapacityPool(object(), deployments=catalog)
    shared_policy = pool._scope_policies["shared"]
    independent_policy = pool._scope_policies["independent"]
    expected_shared_limit = shared_limit if constrained else 1

    assert (shared_policy.base_limit, shared_policy.rpm, shared_policy.tpm) == (
        expected_shared_limit, rpm, tpm,
    )
    assert independent_policy.base_limit == 4
    assert _logical_model_capacity(
        config, "main", deployment_constraint=constraint,
    ) == shared_policy.base_limit + independent_policy.base_limit
    assert _logical_model_capacity(
        config, "review", deployment_constraint=constraint,
    ) == shared_policy.base_limit
    assert _dispatch_parallelism(
        config, "main", (_role("main"), _role("review")), deployment_constraint=constraint,
    ) == shared_policy.base_limit + independent_policy.base_limit
