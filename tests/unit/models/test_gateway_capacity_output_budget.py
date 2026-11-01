"""Real configured factory/Gateway; explicit fake capacity and transport, no provider or Redis."""
from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityQueueFull, CapacityRequestTooLarge
from agent_hub.models.gateway import ConservativeTokenEstimator, ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelRequest
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
from agent_hub.runtime.direct import DirectRuntime
from tests.unit.models.provider_capacity_typed_fixture import (
    DeepseekDefinition,
    FixtureConfig,
    FixtureSecrets,
    acquisition,
    event_name,
    integer,
    present,
)
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    StreamingTransportStub,
    deployment,
    lease,
    request,
)


class RoundedEstimator:
    """Non-unit slope and rounding make subtracting an input estimate insufficient."""

    def estimate_input(self, value: ModelRequest) -> int:
        return 128

    def estimate(self, value: ModelRequest) -> int:
        return 128 + ((value.max_output_tokens + 6) // 7) * 11


class EstimateOnlyRounded:
    def estimate(self, value: ModelRequest) -> int:
        return RoundedEstimator().estimate(value)


def transport_for(capacity: CapacityStub) -> StreamingTransportStub:
    return StreamingTransportStub(capacity.events, [{"choices": [{"delta": {"content": "ok"}}]}])


async def invoke(gateway: ModelGateway, value: ModelRequest, streaming: bool) -> None:
    if streaming:
        assert [event async for event in gateway.stream_openai_compatible_events(value)]
    else:
        assert (await gateway.complete_with_context(value)).response.text == "ok"


def submitted(transport: StreamingTransportStub, streaming: bool) -> ModelRequest:
    return (transport.stream_calls if streaming else transport.calls)[0][1]


def reserved(capacity: CapacityStub, deployment_id: str) -> int:
    acquire = next(acquisition(event) for event in capacity.events if event_name(event) == "acquire")
    estimate = acquire[3]
    return estimate[deployment_id] if isinstance(estimate, Mapping) else estimate


@pytest.mark.parametrize("streaming", [False, True])
async def test_configured_project_request_fits_deepseek_tpm_before_fake_transport(streaming: bool) -> None:
    definition: DeepseekDefinition = {
        "provider": "deepseek", "model": "deepseek-chat", "credential_ref": "secret://fixture",
        "quota_scope_id": "fixture-deepseek", "context_window_tokens": 1_048_576,
        "max_output_tokens": 393_216, "tpm": 100_000, "rpm": 60,
        "max_concurrency": 50, "target_utilization": 0.8, "capabilities": ["text"],
    }

    capacity = CapacityStub([replace(lease("deepseek_1"), quota_scope_id="fixture-deepseek")])
    transport = transport_for(capacity)

    async def capacity_factory(tenant: UUID, deployments: tuple[Deployment, ...]) -> CapacityStub:
        return capacity

    factory = ConfigBackedDirectRuntime(config_service=FixtureConfig({"models": {"deepseek": {"deployments": [definition]}}, "agents": []}), secret_service=FixtureSecrets(),
        capacity_factory=capacity_factory, transport=transport)
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        mode=TaskMode.DIRECT, request="Build a new project.", token_budget=1_000_000,
        routing_decision={"project_delivery": "workspace", "artifact_strategy": "workspace_bundle",
                          "project_scale": "small", "direct_model": "deepseek"})
    runtime = await factory._runtime_for(context)
    assert isinstance(runtime, DirectRuntime)
    assert isinstance(runtime._gateway, ModelGateway)
    original = runtime._build_request(context).request
    assert original is not None and original.max_output_tokens > definition["tpm"]
    # Exercise the actual factory-produced request and Gateway, not a synthetic token count.
    await invoke(runtime._gateway, original, streaming)
    actual = submitted(transport, streaming)
    estimate = ConservativeTokenEstimator().estimate(actual)
    assert 0 < actual.max_output_tokens < 100_000
    assert estimate == reserved(capacity, "deepseek_1") <= 100_000
    assert ConservativeTokenEstimator().estimate(
        replace(actual, max_output_tokens=actual.max_output_tokens + 1)
    ) > 100_000
    assert actual.messages == original.messages
    assert definition["tpm"] == capacity.configured[0].tpm == 100_000
    assert definition["max_output_tokens"] == capacity.configured[0].max_output_tokens == 393_216


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("estimator_type", [RoundedEstimator, EstimateOnlyRounded])
async def test_custom_rounded_estimator_bounds_actual_request_and_reservation(streaming: bool, estimator_type: type[RoundedEstimator] | type[EstimateOnlyRounded]) -> None:
    selected = deployment("selected", tpm=300, context_window_tokens=10_000)
    capacity = CapacityStub([lease("selected")])
    transport = transport_for(capacity)
    estimator = estimator_type()
    gateway = ModelGateway(ModelRegistry([selected]), capacity, SecretStub(capacity.events), transport,
                           token_estimator=estimator)
    original = replace(request(), max_output_tokens=5_000)
    await invoke(gateway, original, streaming)
    actual = submitted(transport, streaming)
    assert actual.max_output_tokens == 105
    assert estimator.estimate(actual) == reserved(capacity, "selected") == 293
    assert estimator.estimate(replace(actual, max_output_tokens=106)) > present(selected.tpm)
    assert original.max_output_tokens == 5_000


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tpm", [None, 0])
async def test_unlimited_tpm_preserves_model_context_and_caller_limits(streaming: bool, tpm: int | None) -> None:
    selected = deployment("selected", max_output_tokens=600, context_window_tokens=700)
    # Public Deployment uses None; zero is the capacity backend's unlimited sentinel.
    if tpm == 0:
        object.__setattr__(selected, "tpm", 0)
    capacity = CapacityStub([lease("selected")])
    transport = transport_for(capacity)
    gateway = ModelGateway(ModelRegistry([selected]), capacity, SecretStub(capacity.events), transport,
                           token_estimator=RoundedEstimator())
    await invoke(gateway, replace(request(), max_output_tokens=5_000), streaming)
    assert submitted(transport, streaming).max_output_tokens == 572


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("output_limit", [1, 77])
async def test_fitting_request_output_upper_bound_is_preserved(streaming: bool, output_limit: int) -> None:
    selected = deployment("selected", tpm=300, max_output_tokens=90)
    capacity = CapacityStub([lease("selected")])
    transport = transport_for(capacity)
    gateway = ModelGateway(ModelRegistry([selected]), capacity, SecretStub(capacity.events), transport,
                           token_estimator=RoundedEstimator())
    await invoke(gateway, replace(request(), max_output_tokens=output_limit), streaming)
    assert submitted(transport, streaming).max_output_tokens == output_limit


@pytest.mark.parametrize("streaming", [False, True])
async def test_input_without_room_for_positive_output_is_typed_without_provider(streaming: bool) -> None:
    selected = deployment("selected", tpm=128)
    capacity = CapacityStub([])
    transport = transport_for(capacity)
    secrets = SecretStub(capacity.events)
    gateway = ModelGateway(ModelRegistry([selected]), capacity, secrets, transport,
                           token_estimator=RoundedEstimator())
    with pytest.raises(CapacityRequestTooLarge):
        await invoke(gateway, request(), streaming)
    assert not secrets.references and not transport.calls and not transport.stream_calls
    assert not capacity.releases


@pytest.mark.parametrize("streaming", [False, True])
async def test_incompatible_deployment_does_not_hide_fitting_peer(streaming: bool) -> None:
    blocked = deployment("blocked", tpm=128)
    fitting = deployment("fitting", tpm=300)
    capacity = CapacityStub([lease("fitting")])
    transport = transport_for(capacity)
    gateway = ModelGateway(ModelRegistry([blocked, fitting]), capacity, SecretStub(capacity.events),
                           transport, token_estimator=RoundedEstimator())
    await invoke(gateway, request(), streaming)
    acquire = next(acquisition(event) for event in capacity.events if event_name(event) == "acquire")
    assert acquire[1] == ("fitting",)
    assert submitted(transport, streaming).max_output_tokens == 105


@pytest.mark.parametrize("streaming", [False, True])
async def test_each_fitting_peer_reserves_its_own_fitted_request(streaming: bool) -> None:
    first = deployment("first", tpm=250)
    second = deployment("second", tpm=300)
    capacity = CapacityStub([lease("second")])
    transport = transport_for(capacity)
    gateway = ModelGateway(ModelRegistry([first, second]), capacity, SecretStub(capacity.events),
                           transport, token_estimator=RoundedEstimator())
    await invoke(gateway, request(), streaming)
    acquire = next(acquisition(event) for event in capacity.events if event_name(event) == "acquire")
    assert acquire[3] == {"first": 249, "second": 293}
    assert submitted(transport, streaming).max_output_tokens == 105


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("primary_block", ["input", "busy"])
async def test_fallback_uses_its_own_tpm_and_output_budget(streaming: bool, primary_block: str) -> None:
    primary = deployment("primary", tpm=128 if primary_block == "input" else 250)
    backup = deployment("backup", "backup", tpm=300)
    outcomes = ([CapacityQueueFull("fixture busy")] if primary_block == "busy" else [])
    capacity = CapacityStub([*outcomes, lease("backup")])
    transport = transport_for(capacity)
    gateway = ModelGateway(ModelRegistry([primary, backup]), capacity, SecretStub(capacity.events),
        transport, token_estimator=RoundedEstimator(), fallbacks={"primary": "backup"})
    await invoke(gateway, request(), streaming)
    actual = submitted(transport, streaming)
    assert actual.logical_model == "primary"  # Logical identity is preserved for transport routing.
    assert actual.max_output_tokens == 105
    estimates = [acquisition(event)[3] for event in capacity.events if event_name(event) == "acquire"]
    assert estimates[-1] == RoundedEstimator().estimate(actual) == 293
    if primary_block == "busy":
        assert integer(estimates[0]) <= 250
