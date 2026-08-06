import asyncio
import hashlib
import importlib
import json
import os
import time
from collections.abc import Mapping
from contextvars import Context
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.gateway import DeploymentPricing, GatewayCompletion
from agent_hub.models.gateway import ModelGateway as LeasedModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CapabilityGateway,
    CapabilityOutcomeUncertain,
    CrewAgentDefinition,
    CrewAIObjectFactory,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewObjectFactory,
    CrewRunStream,
    CrewTaskDefinition,
    ModelOutcomeUncertain,
    RuntimeBusy,
    RuntimeExecutionError,
)
from agent_hub.runtime.crew.adapter import (
    ModelGateway as RuntimeModelGateway,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep

RUN_ID = UUID("00000000-0000-4000-8000-000000000021")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000022")


class DelegatingArtifactRepository:
    delegate: InMemoryArtifactRepository

    async def reserve_write(
        self,
        tenant_id: UUID,
        run_id: UUID,
        reference: ArtifactReference,
        *,
        write_id: UUID,
    ) -> None:
        await self.delegate.reserve_write(
            tenant_id, run_id, reference, write_id=write_id
        )

    async def abort_write(
        self,
        tenant_id: UUID,
        run_id: UUID,
        reference: ArtifactReference,
        *,
        write_id: UUID,
    ) -> bool:
        return await self.delegate.abort_write(
            tenant_id, run_id, reference, write_id=write_id
        )


class FakeGateway:
    def __init__(self, *, barrier: int = 1, reviews: list[str] | None = None) -> None:
        self.requests: list[ModelRequest] = []
        self.active = 0
        self.maximum_observed_concurrency = 0
        self.barrier = barrier
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.reviews = list(reviews or [])
        if barrier == 1:
            self.release.set()

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        self.active += 1
        self.maximum_observed_concurrency = max(self.maximum_observed_concurrency, self.active)
        if self.active >= self.barrier:
            self.started.set()
            self.release.set()
        try:
            await self.release.wait()
            prompt = " ".join(cast(str, message.content) for message in request.messages)
            if "REVIEWER" in prompt:
                text = self.reviews.pop(0) if self.reviews else '{"verdict":"approve"}'
            else:
                text = f"safe result {len(self.requests)}"
            return GatewayCompletion(
                response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=Decimal(0),
            )
        finally:
            self.active -= 1


class ToolGateway(FakeGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-call", name="web.search", arguments={"q": "safe"}),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="tool-grounded answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class ParallelToolGateway(ToolGateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(id="provider-a", name="web.search", arguments={"q": "a"}),
                    ToolCall(id="provider-b", name="web.search", arguments={"q": "b"}),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if len(self.requests) == 1
            else ModelResponse(text="combined answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


class CostGateway(FakeGateway):
    def __init__(self, cost_usd: Decimal, *, reviews: list[str] | None = None) -> None:
        super().__init__(reviews=reviews)
        self.cost_usd = cost_usd

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        completion = await super().complete_with_context(request)
        return GatewayCompletion(
            response=completion.response,
            deployment_id=completion.deployment_id,
            logical_model=completion.logical_model,
            provider_id=completion.provider_id,
            provider_model=completion.provider_model,
            cost_usd=self.cost_usd,
        )


class FakeCapabilities:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def execute(  # type: ignore[no-untyped-def]
        self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
    ):
        del tenant_id, run_id, arguments, idempotency_key
        self.calls.append((actor, name))
        return {"items": ["safe result"]}

    def is_replay_safe(self, name: str) -> bool:
        return name == "web.search"


class FastGeneration:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, agent_id, storage_scope
        return await bridge.complete([{"role": "system", "content": prompt}])


class FastFactory:
    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> FastGeneration:
        del agents, tasks
        assert share_crew is False
        assert telemetry_disabled is True
        return FastGeneration()


def plan(*, review: bool = False, max_parallelism: int = 2) -> DispatchPlan:
    agents = (
        AgentSpec(id="researcher", role="researcher", goal="Research", logical_model="general"),
        AgentSpec(id="critic", role="critic", goal="Review", logical_model="general"),
        AgentSpec(id="writer", role="writer", goal="Synthesize", logical_model="general"),
    )
    return DispatchPlan(
        agents=agents,
        steps=(
            DispatchStep(
                id="left",
                agent="researcher",
                task="Research left",
                reviewer="critic" if review else None,
                reviewer_retries=1 if review else 0,
                token_budget=100,
            ),
            DispatchStep(id="right", agent="researcher", task="Research right", token_budget=100),
            DispatchStep(
                id="final",
                agent="writer",
                task="Synthesize",
                depends_on=("left", "right"),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        max_parallelism=max_parallelism,
        total_token_budget=1000,
    )


def one_step_plan(*, tools: tuple[str, ...] = ()) -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=tools,
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=tools,
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=tools,
        total_token_budget=100,
    )


def chain_plan(step_count: int, *, review: bool = False) -> DispatchPlan:
    agents = [
        AgentSpec(id="writer", role="writer", goal="Write", logical_model="general")
    ]
    if review:
        agents.append(
            AgentSpec(id="critic", role="critic", goal="Review", logical_model="general")
        )
    return DispatchPlan(
        agents=tuple(agents),
        steps=tuple(
            DispatchStep(
                id=f"step-{index}",
                agent="writer",
                task=f"Write part {index}",
                depends_on=() if index == 0 else (f"step-{index - 1}",),
                reviewer="critic" if review else None,
                reviewer_retries=1 if review else 0,
                final_synthesizer=index == step_count - 1,
                token_budget=100,
                timeout_seconds=1,
            )
            for index in range(step_count)
        ),
        max_parallelism=1,
        total_token_budget=step_count * 100 * (4 if review else 1),
    )


def context(  # type: ignore[no-untyped-def]
    *, checkpoint=None, artifacts=(), token_budget: int = 1000
) -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Do bounded research",
        checkpoint=checkpoint,
        artifacts=artifacts,
        token_budget=token_budget,
    )


async def collect(runtime: CrewDispatchRuntime, ctx: TaskContext) -> list[RunEvent]:
    return [event async for event in runtime.run(ctx)]


def make_runtime(
    gateway: RuntimeModelGateway,
    dispatch_plan: DispatchPlan | None = None,
    *,
    capability_gateway: CapabilityGateway | None = None,
    crew_factory: CrewObjectFactory | None = None,
    artifact_repository: ArtifactRepository | None = None,
) -> CrewDispatchRuntime:
    kwargs: dict[str, object] = {
        "capability_gateway": capability_gateway,
        "crew_factory": crew_factory or FastFactory(),
    }
    if artifact_repository is not None:
        kwargs["artifact_repository"] = artifact_repository
    return CrewDispatchRuntime(gateway, dispatch_plan or plan(), **kwargs)  # type: ignore[arg-type]


async def test_ready_steps_execute_in_parallel_and_emit_framework_neutral_events() -> None:
    gateway = FakeGateway(barrier=2)
    events = await collect(make_runtime(gateway), context())

    assert gateway.maximum_observed_concurrency == 2
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len([event for event in events if event.kind is EventKind.STEP_COMPLETED]) == 3
    artifacts = [event.artifact for event in events if event.kind is EventKind.ARTIFACT_CREATED]
    final = artifacts[-1]
    assert final is not None and len(final.source_ids) == 3
    assert all("crewai" not in type(event).__module__ for event in events)


async def test_plan_parallelism_caps_fanout_even_when_gateway_has_capacity() -> None:
    gateway = FakeGateway()
    await collect(make_runtime(gateway, plan(max_parallelism=1)), context())
    assert gateway.maximum_observed_concurrency == 1


async def test_real_model_gateway_queues_agents_sharing_one_quota_scope() -> None:
    class SharedCapacity:
        def __init__(self) -> None:
            self.semaphore = asyncio.Semaphore(1)
            self.active = 0
            self.maximum_active = 0
            self.next_candidate = 0
            self.scopes: list[str] = []

        async def initialize(self) -> None:
            return None

        def validate_configuration(self, deployments):  # type: ignore[no-untyped-def]
            assert {item.quota_scope_id for item in deployments} == {"shared-deepseek"}

        async def acquire(  # type: ignore[no-untyped-def]
            self, candidates, wait_timeout, *, estimated_tokens
        ):
            del wait_timeout, estimated_tokens
            await self.semaphore.acquire()
            selected = candidates[self.next_candidate % len(candidates)]
            self.next_candidate += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.scopes.append(selected.quota_scope_id)
            return CapacityLease(
                id=str(uuid4()),
                deployment_id=selected.id,
                quota_scope_id=selected.quota_scope_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=60),
                renew_after_seconds=10,
            )

        async def release(self, lease: CapacityLease) -> bool:
            del lease
            self.active -= 1
            self.semaphore.release()
            return True

        async def renew(self, lease: CapacityLease) -> CapacityLease:
            return lease

        async def record_outcome(self, quota_scope_id, **kwargs):  # type: ignore[no-untyped-def]
            del quota_scope_id, kwargs

    class Secrets:
        async def resolve(self, secret_ref: str) -> str:
            del secret_ref
            return "test-key"

    class Transport:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0

        async def complete(self, deployment, request, api_key):  # type: ignore[no-untyped-def]
            del deployment, request, api_key
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return ModelResponse(text="leased result", usage=TokenUsage(1, 1, 2))

    deployments = tuple(
        Deployment(
            id=f"deepseek-{index}",
            logical_model="general",
            provider_model="deepseek/chat",
            secret_ref=f"secret://deepseek-{index}",
            quota_scope_id="shared-deepseek",
        )
        for index in range(2)
    )
    capacity = SharedCapacity()
    transport = Transport()
    gateway = LeasedModelGateway(
        ModelRegistry(deployments),
        capacity,
        Secrets(),
        transport,
        pricing={
            deployment.id: DeploymentPricing(
                input_per_million_usd=Decimal(0),
                output_per_million_usd=Decimal(0),
            )
            for deployment in deployments
        },
    )

    events = await collect(make_runtime(gateway), context())

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert capacity.maximum_active == 1
    assert transport.maximum_active == 1
    assert set(capacity.scopes) == {"shared-deepseek"}


async def test_reviewer_feedback_causes_only_bounded_retry() -> None:
    gateway = FakeGateway(
        reviews=['{"verdict":"revise","feedback":"fix it"}', '{"verdict":"approve"}']
    )
    events = await collect(make_runtime(gateway, plan(review=True)), context())
    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == 1
    assert any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    review_index = next(
        index for index, event in enumerate(events) if event.kind is EventKind.REVIEW_COMPLETED
    )
    assert events[review_index + 1].kind is EventKind.CHECKPOINT_SAVED
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_reviewer_feedback_artifact_survives_crash_resume() -> None:
    class CrashOnRetryGeneration(FastGeneration):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.prompts: list[str] = []

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            self.prompts.append(prompt)
            if "untrusted_reviewer_feedback" in prompt:
                self.started.set()
                await asyncio.Event().wait()
            return await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )

    generation = CrashOnRetryGeneration()

    class Factory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return generation

    first = make_runtime(
        FakeGateway(reviews=['{"verdict":"revise","feedback":"persist me"}']),
        plan(review=True),
        crew_factory=Factory(),
    )
    first_events: list[RunEvent] = []

    async def consume() -> None:
        async for event in first.run(context()):
            first_events.append(event)

    task = asyncio.create_task(consume())
    await generation.started.wait()
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    checkpoint = next(
        event.checkpoint
        for event in reversed(first_events)
        if event.kind is EventKind.CHECKPOINT_SAVED
        and event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
    )
    assert "persist me" not in json.dumps(checkpoint.to_payload())

    referenced_ids = {
        reference["id"]
        for field in ("artifact_refs", "review_refs")
        for reference in cast(Mapping[str, Mapping[str, str]], checkpoint.state[field]).values()
    }
    referenced_ids.update(
        cast(str, model_state["artifact_id"])
        for model_state in cast(
            Mapping[str, Mapping[str, object]], checkpoint.state["models"]
        ).values()
        if model_state["status"] == "succeeded"
    )
    artifacts = tuple(event.artifact for event in first_events if event.artifact is not None)
    assert referenced_ids <= {str(artifact.id) for artifact in artifacts}

    class CapturingGeneration(FastGeneration):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            self.prompts.append(prompt)
            return await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )

    resumed_generation = CapturingGeneration()

    class ResumedFactory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return resumed_generation

    resumed = make_runtime(
        FakeGateway(reviews=['{"verdict":"approve"}']),
        plan(review=True),
        crew_factory=ResumedFactory(),
    )
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(resumed, context(checkpoint=checkpoint, artifacts=artifacts))

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any("persist me" in prompt for prompt in resumed_generation.prompts)


async def test_review_retry_exhaustion_prevents_synthesis_and_redacts_output() -> None:
    sentinel = "model-secret-sentinel"
    gateway = FakeGateway(
        reviews=[
            f'{{"verdict":"revise","feedback":"{sentinel}"}}',
            f'{{"verdict":"revise","feedback":"{sentinel}"}}',
        ]
    )
    runtime = make_runtime(gateway, plan(review=True))
    with pytest.raises(RuntimeExecutionError) as caught:
        await collect(runtime, context())
    assert sentinel not in str(caught.value)
    assert not any(
        "SYNTHESIZER" in cast(str, request.messages[0].content) for request in gateway.requests
    )


async def test_checkpoint_can_resume_completed_run_without_model_calls() -> None:
    first = make_runtime(FakeGateway())
    events = await collect(first, context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.kind is EventKind.CHECKPOINT_SAVED
    )
    assert checkpoint is not None
    second_gateway = FakeGateway()
    second = make_runtime(second_gateway)
    await second.restore_checkpoint(checkpoint)
    stored_artifacts = tuple(
        event.artifact
        for event in events
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
    )
    resumed = await collect(second, context(checkpoint=checkpoint, artifacts=stored_artifacts))
    assert [event.kind for event in resumed] == [EventKind.RUNTIME_COMPLETED]
    assert not second_gateway.requests


async def test_succeeded_model_call_resumes_before_final_artifact_without_replay() -> None:
    class BlockAfterCompletionGeneration(FastGeneration):
        def __init__(self) -> None:
            self.completed = asyncio.Event()

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            result = await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )
            self.completed.set()
            await asyncio.Event().wait()
            return result

    generation = BlockAfterCompletionGeneration()

    class BlockingFactory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return generation

    first_gateway = FakeGateway()
    first = make_runtime(
        first_gateway,
        one_step_plan(),
        crew_factory=BlockingFactory(),
    )
    first_events: list[RunEvent] = []

    async def consume() -> None:
        async for event in first.run(context()):
            first_events.append(event)

    consumer = asyncio.create_task(consume())
    await generation.completed.wait()
    checkpoint = await first.save_checkpoint()
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert len(first_gateway.requests) == 1
    assert "safe result 1" not in json.dumps(checkpoint.to_payload())
    assert checkpoint.state["models"]
    model_artifacts = tuple(
        event.artifact
        for event in first_events
        if event.artifact is not None and event.artifact.type == "model_response"
    )
    assert len(model_artifacts) == 1

    resumed_gateway = FakeGateway()
    resumed = make_runtime(resumed_gateway, one_step_plan())
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(
        resumed,
        context(checkpoint=checkpoint, artifacts=model_artifacts),
    )

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert resumed_gateway.requests == []


async def test_changed_model_request_hash_never_reuses_succeeded_response() -> None:
    class ChangedGeneration(FastGeneration):
        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            del step_id, agent_id, storage_scope
            return await bridge.complete(
                [{"role": "system", "content": prompt + " changed-after-restore"}]
            )

    class ChangedFactory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            return ChangedGeneration()

    first_gateway = FakeGateway()
    first = make_runtime(first_gateway, one_step_plan())
    events = await collect(first, context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    state["completed"] = []
    state["artifact_refs"] = {}
    state["frontier"] = ["final"]
    state["phase"] = "running"
    state["terminal"] = False
    payload["state_sha256"] = ""
    resumable = RuntimeCheckpoint.from_payload(payload)
    gateway = FakeGateway()
    resumed = make_runtime(gateway, one_step_plan(), crew_factory=ChangedFactory())
    await resumed.restore_checkpoint(resumable)

    with pytest.raises(RuntimeExecutionError, match="model request"):
        await collect(resumed, context(checkpoint=resumable, artifacts=artifacts))
    assert gateway.requests == []


async def test_running_model_checkpoint_requires_confirmation_without_replay() -> None:
    gateway = FakeGateway(barrier=99)
    first = make_runtime(gateway, one_step_plan())
    consumer = asyncio.create_task(collect(first, context()))
    while not gateway.requests:
        await asyncio.sleep(0)
    checkpoint = await first.save_checkpoint()
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    model_states = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert {state["status"] for state in model_states.values()} == {"running"}
    resumed_gateway = FakeGateway()
    resumed = make_runtime(resumed_gateway, one_step_plan())
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match="model outcome requires confirmation"):
        await collect(resumed, context(checkpoint=checkpoint))
    assert resumed_gateway.requests == []

    checkpoint_2 = await resumed.save_checkpoint()
    assert checkpoint_2.state["models"] == checkpoint.state["models"]
    assert checkpoint_2.state["usage"] == checkpoint.state["usage"]
    third_gateway = FakeGateway()
    third = make_runtime(third_gateway, one_step_plan())
    await third.restore_checkpoint(checkpoint_2)
    with pytest.raises(RuntimeExecutionError, match="model outcome requires confirmation"):
        await collect(third, context(checkpoint=checkpoint_2))
    checkpoint_3 = await third.save_checkpoint()

    assert checkpoint_3.state["models"] == checkpoint.state["models"]
    assert third_gateway.requests == []


@pytest.mark.parametrize("failure", ("factory", "budget"))
async def test_pre_hydrate_failure_never_replaces_uncertain_checkpoint(failure: str) -> None:
    first_gateway = FakeGateway(barrier=99)
    first = make_runtime(first_gateway, one_step_plan())
    consumer = asyncio.create_task(collect(first, context()))
    while not first_gateway.requests:
        await asyncio.sleep(0)
    checkpoint_1 = await first.save_checkpoint()
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    class FailingFactory(FastFactory):
        def build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise RuntimeError("temporary factory failure")

    failed_gateway = FakeGateway()
    failed = make_runtime(
        failed_gateway,
        one_step_plan(),
        crew_factory=FailingFactory() if failure == "factory" else FastFactory(),
    )
    await failed.restore_checkpoint(checkpoint_1)
    with pytest.raises(RuntimeExecutionError):
        await collect(
            failed,
            context(
                checkpoint=checkpoint_1,
                token_budget=1 if failure == "budget" else 1000,
            ),
        )
    checkpoint_2 = await failed.save_checkpoint()

    assert checkpoint_2.state["models"] == checkpoint_1.state["models"]
    assert checkpoint_2.state["tools"] == checkpoint_1.state["tools"]
    assert checkpoint_2.state["usage"] == checkpoint_1.state["usage"]
    assert failed_gateway.requests == []

    recovered_gateway = FakeGateway()
    recovered = make_runtime(recovered_gateway, one_step_plan())
    await recovered.restore_checkpoint(checkpoint_2)
    with pytest.raises(ModelOutcomeUncertain):
        await collect(recovered, context(checkpoint=checkpoint_2))

    assert recovered_gateway.requests == []


async def test_model_tool_and_final_artifacts_form_a_traceable_graph() -> None:
    events = await collect(
        make_runtime(
            ToolGateway(),
            one_step_plan(tools=("web.search",)),
            capability_gateway=FakeCapabilities(),
        ),
        context(),
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    models = tuple(artifact for artifact in artifacts if artifact.type == "model_response")
    tool = next(artifact for artifact in artifacts if artifact.type == "tool_result")
    final = next(artifact for artifact in artifacts if artifact.type == "text")

    assert len(models) == 2
    assert tool.source_ids == (str(models[0].id),)
    assert final.source_ids == tuple(str(artifact.id) for artifact in (models[0], tool, models[1]))


async def test_parallel_tool_calls_share_trigger_and_feed_next_model_in_order() -> None:
    events = await collect(
        make_runtime(
            ParallelToolGateway(),
            one_step_plan(tools=("web.search",)),
            capability_gateway=FakeCapabilities(),
        ),
        context(),
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    models = tuple(artifact for artifact in artifacts if artifact.type == "model_response")
    tools = tuple(artifact for artifact in artifacts if artifact.type == "tool_result")
    final = next(artifact for artifact in artifacts if artifact.type == "text")

    assert len(models) == 2
    assert len(tools) == 2
    assert {tool.source_ids for tool in tools} == {(str(models[0].id),)}
    evidence_ids = tuple(str(artifact.id) for artifact in (models[0], *tools))
    assert models[1].source_ids == evidence_ids
    assert final.source_ids == (*evidence_ids, str(models[1].id))
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    tool_states = cast(Mapping[str, Mapping[str, object]], checkpoint.state["tools"])
    assert {state["attempt"] for state in tool_states.values()} == {0}
    assert {state["round"] for state in tool_states.values()} == {0}
    assert {state["tool_index"] for state in tool_states.values()} == {0, 1}
    assert {state["trigger_model_artifact_id"] for state in tool_states.values()} == {
        str(models[0].id)
    }


@pytest.mark.parametrize("tamper", ("round", "tool_index", "trigger"))
async def test_checkpoint_rejects_tampered_tool_call_coordinates(tamper: str) -> None:
    dispatch_plan = one_step_plan(tools=("web.search",))
    events = await collect(
        make_runtime(
            ToolGateway(),
            dispatch_plan,
            capability_gateway=FakeCapabilities(),
        ),
        context(),
    )
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    tool_states = cast(dict[str, dict[str, object]], state["tools"])
    old_key, tool_state = next(iter(tool_states.items()))
    model_states = cast(dict[str, dict[str, object]], state["models"])
    later_model_id = next(
        cast(str, model_state["artifact_id"])
        for model_state in model_states.values()
        if model_state["call_index"] == 1
    )
    if tamper == "round":
        tool_state["round"] = 1
        tool_state["trigger_model_artifact_id"] = later_model_id
    elif tamper == "tool_index":
        tool_state["tool_index"] = 1
    else:
        tool_state["trigger_model_artifact_id"] = later_model_id
    if tamper != "trigger":
        material = (
            f"{RUN_ID}:{tool_state['step_id']}:{tool_state['attempt']}:"
            f"{tool_state['round']}:{tool_state['tool_index']}:{tool_state['name']}:"
            f"{tool_state['arguments_sha256']}"
        )
        new_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        tool_states[new_key] = tool_states.pop(old_key)
    payload["state_sha256"] = ""
    tampered = RuntimeCheckpoint.from_payload(payload)
    resumed = make_runtime(
        FakeGateway(),
        dispatch_plan,
        capability_gateway=FakeCapabilities(),
    )

    try:
        await resumed.restore_checkpoint(tampered)
    except RuntimeExecutionError:
        return
    with pytest.raises(RuntimeExecutionError, match="artifact"):
        await collect(
            resumed,
            context(checkpoint=tampered, artifacts=artifacts),
        )


async def test_completed_parallel_tool_graph_restores_without_replay() -> None:
    dispatch_plan = one_step_plan(tools=("web.search",))
    events = await collect(
        make_runtime(
            ParallelToolGateway(),
            dispatch_plan,
            capability_gateway=FakeCapabilities(),
        ),
        context(),
    )
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    gateway = FakeGateway()
    capabilities = FakeCapabilities()
    resumed = make_runtime(
        gateway,
        dispatch_plan,
        capability_gateway=capabilities,
    )
    await resumed.restore_checkpoint(checkpoint)

    restored_events = await collect(
        resumed,
        context(checkpoint=checkpoint, artifacts=artifacts),
    )

    assert [event.kind for event in restored_events] == [EventKind.RUNTIME_COMPLETED]
    assert gateway.requests == []
    assert capabilities.calls == []


async def test_dependency_and_synthesis_artifacts_form_a_traceable_graph() -> None:
    events = await collect(make_runtime(FakeGateway(barrier=2)), context())
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    by_id = {str(artifact.id): artifact for artifact in artifacts}
    texts = tuple(artifact for artifact in artifacts if artifact.type == "text")
    final = next(artifact for artifact in texts if artifact.producer == "writer")

    assert len(final.source_ids) == 3
    dependencies = tuple(by_id[source_id] for source_id in final.source_ids[:2])
    synthesis_model = by_id[final.source_ids[2]]
    assert all(
        artifact.type == "text" and artifact.producer == "researcher" for artifact in dependencies
    )
    assert synthesis_model.type == "model_response"
    assert synthesis_model.producer == "writer"
    assert tuple(synthesis_model.source_ids[:2]) == tuple(
        str(artifact.id) for artifact in dependencies
    )


async def test_review_feedback_records_candidate_and_reviewer_model_sources() -> None:
    gateway = FakeGateway(
        reviews=['{"verdict":"revise","feedback":"fix it"}', '{"verdict":"approve"}']
    )
    events = await collect(make_runtime(gateway, plan(review=True)), context())
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    by_id = {str(artifact.id): artifact for artifact in artifacts}
    feedback = next(artifact for artifact in artifacts if artifact.type == "review_feedback")

    assert len(feedback.source_ids) == 2
    candidate = by_id[feedback.source_ids[0]]
    reviewer_model = by_id[feedback.source_ids[1]]
    assert candidate.type == "text"
    assert candidate.producer == "researcher"
    assert reviewer_model.type == "model_response"
    assert reviewer_model.producer == "critic"
    assert reviewer_model.source_ids == (str(candidate.id),)
    revised = next(
        artifact
        for artifact in artifacts
        if artifact.type == "text" and artifact.producer == "researcher" and artifact.version == 2
    )
    assert revised.source_ids[0] == str(feedback.id)
    revised_model = by_id[revised.source_ids[1]]
    assert revised_model.type == "model_response"
    assert revised_model.source_ids[0] == str(feedback.id)


@pytest.mark.parametrize("omit_source", (False, True))
async def test_checkpoint_rejects_review_feedback_with_false_lineage(
    omit_source: bool,
) -> None:
    gateway = FakeGateway(
        reviews=['{"verdict":"revise","feedback":"fix it"}', '{"verdict":"approve"}']
    )
    events = await collect(make_runtime(gateway, plan(review=True)), context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = [event.artifact for event in events if event.artifact is not None]
    feedback_index = next(
        index for index, artifact in enumerate(artifacts) if artifact.type == "review_feedback"
    )
    feedback = artifacts[feedback_index]
    false_source = next(artifact for artifact in artifacts if artifact.producer == "writer")
    tampered_feedback = Artifact(
        id=feedback.id,
        version=feedback.version,
        type=feedback.type,
        producer=feedback.producer,
        content=feedback.content,
        source_ids=(feedback.source_ids[0],) if omit_source else (str(false_source.id),),
        provenance=feedback.provenance,
    )
    artifacts[feedback_index] = tampered_feedback
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    review_refs = cast(dict[str, dict[str, object]], state["review_refs"])
    review_refs["left"]["sha256"] = tampered_feedback.content_sha256
    payload["state_sha256"] = ""
    tampered = RuntimeCheckpoint.from_payload(payload)
    resumed = make_runtime(FakeGateway(), plan(review=True))
    await resumed.restore_checkpoint(tampered)

    with pytest.raises(RuntimeExecutionError, match="review artifact"):
        await collect(
            resumed,
            context(checkpoint=tampered, artifacts=tuple(artifacts)),
        )


@pytest.mark.parametrize(
    "target",
    (
        "final",
        "final_omit",
        "model",
        "model_omit",
        "tool",
        "tool_omit",
        "retry_model",
        "retry_model_omit",
    ),
)
async def test_checkpoint_rejects_rehashed_artifact_with_false_lineage(target: str) -> None:
    if target.startswith("tool"):
        dispatch_plan = one_step_plan(tools=("web.search",))
        events = await collect(
            make_runtime(
                ToolGateway(),
                dispatch_plan,
                capability_gateway=FakeCapabilities(),
            ),
            context(),
        )
    elif target.startswith("retry_model"):
        dispatch_plan = plan(review=True)
        events = await collect(
            make_runtime(
                FakeGateway(
                    reviews=[
                        '{"verdict":"revise","feedback":"fix it"}',
                        '{"verdict":"approve"}',
                    ]
                ),
                dispatch_plan,
            ),
            context(),
        )
    else:
        dispatch_plan = plan()
        events = await collect(make_runtime(FakeGateway(barrier=2), dispatch_plan), context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = [event.artifact for event in events if event.artifact is not None]
    by_id = {str(artifact.id): artifact for artifact in artifacts}
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])

    if target.startswith("final"):
        artifact = next(
            item for item in artifacts if item.type == "text" and item.producer == "writer"
        )
        false_sources = (
            artifact.source_ids[:-1]
            if target.endswith("omit")
            else (artifact.source_ids[1], artifact.source_ids[0], *artifact.source_ids[2:])
        )
    elif target.startswith("model"):
        artifact = next(
            item
            for item in artifacts
            if item.type == "model_response" and item.producer == "writer"
        )
        false_sources = (
            artifact.source_ids[:-1]
            if target.endswith("omit")
            else (artifact.source_ids[1], artifact.source_ids[0])
        )
    elif target.startswith("tool"):
        artifact = next(item for item in artifacts if item.type == "tool_result")
        later_model = next(
            item
            for item in artifacts
            if item.type == "model_response" and str(item.id) != artifact.source_ids[0]
        )
        false_sources = () if target.endswith("omit") else (str(later_model.id),)
    else:
        feedback = next(item for item in artifacts if item.type == "review_feedback")
        artifact = next(
            item
            for item in artifacts
            if item.type == "model_response"
            and item.producer == "researcher"
            and item.source_ids
            and item.source_ids[0] == str(feedback.id)
        )
        rejected_candidate = by_id[feedback.source_ids[0]]
        false_sources = (
            artifact.source_ids[:-1]
            if target.endswith("omit")
            else (str(rejected_candidate.id), *artifact.source_ids[1:])
        )

    tampered_artifact = Artifact(
        id=artifact.id,
        version=artifact.version,
        type=artifact.type,
        producer=artifact.producer,
        content=artifact.content,
        source_ids=false_sources,
        provenance=artifact.provenance,
    )
    artifacts[artifacts.index(artifact)] = tampered_artifact
    if target.startswith("final"):
        refs = cast(dict[str, dict[str, object]], state["artifact_refs"])
        refs["final"]["sha256"] = tampered_artifact.content_sha256
    elif target.startswith("tool"):
        tool_states = cast(dict[str, dict[str, object]], state["tools"])
        tool_state = next(
            item for item in tool_states.values() if item["artifact_id"] == str(artifact.id)
        )
        tool_state["sha256"] = tampered_artifact.content_sha256
    else:
        model_states = cast(dict[str, dict[str, object]], state["models"])
        model_state = next(
            item for item in model_states.values() if item["artifact_id"] == str(artifact.id)
        )
        model_state["sha256"] = tampered_artifact.content_sha256
    payload["state_sha256"] = ""
    tampered = RuntimeCheckpoint.from_payload(payload)
    resumed = make_runtime(FakeGateway(), dispatch_plan, capability_gateway=FakeCapabilities())
    await resumed.restore_checkpoint(tampered)

    with pytest.raises(RuntimeExecutionError, match="artifact"):
        await collect(
            resumed,
            context(checkpoint=tampered, artifacts=tuple(artifacts)),
        )


async def test_completed_reviewed_dag_restores_without_replay() -> None:
    dispatch_plan = plan(review=True)
    events = await collect(
        make_runtime(
            FakeGateway(
                reviews=[
                    '{"verdict":"revise","feedback":"fix it"}',
                    '{"verdict":"approve"}',
                ]
            ),
            dispatch_plan,
        ),
        context(),
    )
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    gateway = FakeGateway()
    resumed = make_runtime(gateway, dispatch_plan)
    await resumed.restore_checkpoint(checkpoint)

    restored_events = await collect(
        resumed,
        context(checkpoint=checkpoint, artifacts=artifacts),
    )

    assert [event.kind for event in restored_events] == [EventKind.RUNTIME_COMPLETED]
    assert gateway.requests == []


@pytest.mark.parametrize(
    "tamper",
    ("artifact_id", "sha256", "call_index", "provenance"),
)
async def test_checkpoint_rejects_tampered_model_ledger(tamper: str) -> None:
    events = await collect(make_runtime(FakeGateway(), one_step_plan()), context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    artifacts = tuple(event.artifact for event in events if event.artifact is not None)
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    models = cast(dict[str, dict[str, object]], state["models"])
    model_state = next(iter(models.values()))
    if tamper == "artifact_id":
        model_state["artifact_id"] = str(uuid4())
    elif tamper == "sha256":
        model_state["sha256"] = "0" * 64
    elif tamper == "call_index":
        model_state["call_index"] = 1
    else:
        provenance = cast(dict[str, object], model_state["provenance"])
        provenance["provider_id"] = "other"
        provenance["provider_model"] = "other/chat"
    payload["state_sha256"] = ""
    tampered = RuntimeCheckpoint.from_payload(payload)
    gateway = FakeGateway()
    resumed = make_runtime(gateway, one_step_plan())

    if tamper == "call_index":
        with pytest.raises(RuntimeExecutionError):
            await resumed.restore_checkpoint(tampered)
    else:
        await resumed.restore_checkpoint(tampered)
        with pytest.raises(RuntimeExecutionError, match="model artifacts"):
            await collect(
                resumed,
                context(checkpoint=tampered, artifacts=artifacts),
            )
    assert gateway.requests == []


async def test_checkpoint_rejects_mismatched_step_usage_ledger() -> None:
    first = make_runtime(FakeGateway())
    events = await collect(first, context())
    checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload["state"])
    step_usage = cast(dict[str, dict[str, object]], state["step_usage"])
    step_usage["left"]["tokens"] = 999
    payload["state_sha256"] = ""
    tampered = RuntimeCheckpoint.from_payload(payload)
    resumed = make_runtime(FakeGateway())
    with pytest.raises(RuntimeExecutionError):
        await resumed.restore_checkpoint(tampered)


async def test_parallel_model_failure_preserves_sibling_and_requires_confirmation() -> None:
    left_completed = asyncio.Event()

    class PartialGateway(FakeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            prompt = " ".join(cast(str, message.content) for message in request.messages)
            if "Research left" in prompt:
                left_completed.set()
            elif "Research right" in prompt:
                await left_completed.wait()
                raise RuntimeError("provider detail must be redacted")
            return await super().complete_with_context(request)

    first = make_runtime(PartialGateway())
    first_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in first.run(context()):
            first_events.append(event)
    checkpoint = await first.save_checkpoint()
    assert checkpoint.state["completed"] == ()
    model_states = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert any(
        model_state["step_id"] == "left" and model_state["status"] == "succeeded"
        for model_state in model_states.values()
    )

    stored_artifacts = tuple(
        event.artifact
        for event in first_events
        if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
    )
    gateway = FakeGateway()
    resumed_runtime = make_runtime(gateway)
    await resumed_runtime.restore_checkpoint(checkpoint)
    with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
        await collect(
            resumed_runtime,
            context(checkpoint=checkpoint, artifacts=stored_artifacts),
        )
    assert gateway.requests == []


async def test_cancel_and_early_close_clean_siblings_and_allow_reuse() -> None:
    gateway = FakeGateway(barrier=99)
    runtime = make_runtime(gateway)
    task = asyncio.create_task(collect(runtime, context()))
    while len(gateway.requests) < 2:
        await asyncio.sleep(0)
    await runtime.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    gateway.barrier = 1
    gateway.release.set()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED

    gateway.barrier = 99
    gateway.release.clear()
    stream = cast(CrewRunStream, runtime.run(context()))
    await anext(stream)
    await stream.aclose()
    gateway.barrier = 1
    gateway.release.set()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_noncooperative_generation_is_detached_and_runtime_cancelled_is_emitted() -> None:
    class NoncooperativeGeneration(FastGeneration):
        def __init__(self) -> None:
            self.block = True
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            if self.block:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                return "detached result"
            return await super().execute(
                step_id,
                prompt,
                bridge,
                agent_id=agent_id,
                storage_scope=storage_scope,
            )

    class Factory(FastFactory):
        def __init__(self, generation: NoncooperativeGeneration) -> None:
            self.generation = generation

        def build(
            self,
            agents: tuple[CrewAgentDefinition, ...],
            tasks: tuple[CrewTaskDefinition, ...],
            *,
            share_crew: bool,
            telemetry_disabled: bool,
        ) -> NoncooperativeGeneration:
            del agents, tasks, share_crew, telemetry_disabled
            return self.generation

    generation = NoncooperativeGeneration()
    runtime = make_runtime(FakeGateway(), crew_factory=Factory(generation))
    events: list[RunEvent] = []

    async def consume() -> None:
        async for event in runtime.run(context()):
            events.append(event)

    task = asyncio.create_task(consume())
    await generation.started.wait()
    await asyncio.wait_for(runtime.cancel(), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert EventKind.RUNTIME_CANCELLED in [event.kind for event in events]

    generation.block = False
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED
    terminal_checkpoint = await runtime.save_checkpoint()
    generation.release.set()
    await asyncio.sleep(0)
    assert (await runtime.save_checkpoint()).id == terminal_checkpoint.id


async def test_runtime_revalidates_constructed_plan_before_gateway() -> None:
    unsafe = plan().model_copy()
    object.__setattr__(unsafe.steps[0], "id", "../secret")
    gateway = FakeGateway()
    runtime = make_runtime(gateway, unsafe)
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime, context())
    assert not gateway.requests


async def test_constructed_context_failure_has_no_secret_exception_chain() -> None:
    sentinel = "raw-api-key-sentinel"
    unsafe = TaskContext.model_construct(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request=f"bad\x00{sentinel}",
        artifacts=(),
        checkpoint=None,
        timeout_seconds=60.0,
        token_budget=True,
    )
    with pytest.raises(RuntimeExecutionError) as caught:
        make_runtime(FakeGateway()).run(unsafe)
    assert sentinel not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


async def test_single_stream_consumer_and_mode_boundary() -> None:
    runtime = make_runtime(FakeGateway(barrier=99))
    stream = cast(CrewRunStream, runtime.run(context()))
    await anext(stream)

    async def consume_other() -> RunEvent:
        return await anext(stream)

    with pytest.raises(RuntimeBusy):
        await asyncio.create_task(consume_other())
    await stream.aclose()
    bad = context().model_copy(update={"mode": TaskMode.DIRECT})
    with pytest.raises(RuntimeExecutionError, match="mode"):
        runtime.run(bad)


async def test_unstarted_stream_close_releases_runtime_reservation() -> None:
    runtime = make_runtime(FakeGateway())
    stream = cast(CrewRunStream, runtime.run(context()))
    await stream.aclose()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_tool_calls_only_cross_the_capability_gateway() -> None:
    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    capabilities = FakeCapabilities()
    events = await collect(
        make_runtime(ToolGateway(), tool_plan, capability_gateway=capabilities),
        context(),
    )
    assert capabilities.calls == [("writer", "web.search")]
    assert [event.kind for event in events if str(event.kind).startswith("tool.")] == [
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
    ]
    tool_completed_index = next(
        index for index, event in enumerate(events) if event.kind is EventKind.TOOL_COMPLETED
    )
    assert events[tool_completed_index + 1].kind is EventKind.CHECKPOINT_SAVED


async def test_succeeded_tool_is_preserved_when_following_model_is_uncertain() -> None:
    class FailingAfterToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if self.requests:
                raise RuntimeError("provider detail")
            return await super().complete_with_context(request)

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    first_capabilities = FakeCapabilities()
    first = make_runtime(
        FailingAfterToolGateway(), tool_plan, capability_gateway=first_capabilities
    )
    first_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in first.run(context()):
            first_events.append(event)
    checkpoint = await first.save_checkpoint()
    persisted_artifacts = tuple(
        event.artifact for event in first_events if event.artifact is not None
    )
    tool_artifacts = tuple(
        artifact for artifact in persisted_artifacts if artifact.type == "tool_result"
    )
    assert len(tool_artifacts) == 1

    second_capabilities = FakeCapabilities()
    resumed = make_runtime(ToolGateway(), tool_plan, capability_gateway=second_capabilities)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(ModelOutcomeUncertain, match="model outcome requires confirmation"):
        await collect(
            resumed,
            context(checkpoint=checkpoint, artifacts=persisted_artifacts),
        )

    assert second_capabilities.calls == []
    checkpoint_2 = await resumed.save_checkpoint()
    assert checkpoint_2.state["models"] == checkpoint.state["models"]
    assert checkpoint_2.state["tools"] == checkpoint.state["tools"]
    assert checkpoint_2.state["usage"] == checkpoint.state["usage"]


async def test_replay_safe_running_tool_reuses_stable_idempotency_key() -> None:
    class RecordingCapabilities(FakeCapabilities):
        def __init__(self, *, block: bool) -> None:
            super().__init__()
            self.block = block
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.keys: list[str] = []

        async def execute(  # type: ignore[no-untyped-def]
            self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
        ):
            del tenant_id, run_id, arguments
            self.calls.append((actor, name))
            self.keys.append(idempotency_key)
            self.started.set()
            if self.block:
                await self.release.wait()
            return {"items": ["safe result"]}

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    first_capabilities = RecordingCapabilities(block=True)
    gateway = ToolGateway()
    first = make_runtime(gateway, tool_plan, capability_gateway=first_capabilities)
    first_events: list[RunEvent] = []

    async def consume() -> None:
        async for event in first.run(context()):
            first_events.append(event)

    task = asyncio.create_task(consume())
    await first_capabilities.started.wait()
    checkpoint = await first.save_checkpoint()
    tool_states = checkpoint.state["tools"]
    assert isinstance(tool_states, Mapping)
    tool_state = next(iter(tool_states.values()))
    assert isinstance(tool_state, Mapping)
    assert tool_state["status"] == "running"
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    second_capabilities = RecordingCapabilities(block=False)
    resumed = make_runtime(gateway, tool_plan, capability_gateway=second_capabilities)
    await resumed.restore_checkpoint(checkpoint)
    persisted_artifacts = tuple(
        event.artifact for event in first_events if event.artifact is not None
    )
    events = await collect(
        resumed,
        context(checkpoint=checkpoint, artifacts=persisted_artifacts),
    )

    assert second_capabilities.keys == first_capabilities.keys
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_detached_old_tool_cannot_overwrite_new_run_checkpoint() -> None:
    class LateToolCapabilities(FakeCapabilities):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.returned = asyncio.Event()

        async def execute(  # type: ignore[no-untyped-def]
            self, *, tenant_id, run_id, actor, name, arguments, idempotency_key
        ):
            del tenant_id, run_id, arguments, idempotency_key
            self.calls.append((actor, name))
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            self.returned.set()
            return {"items": ["late result"]}

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    capabilities = LateToolCapabilities()
    runtime = make_runtime(ToolGateway(), tool_plan, capability_gateway=capabilities)
    old_task = asyncio.create_task(collect(runtime, context()))
    await capabilities.started.wait()
    await runtime.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task

    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED
    current_checkpoint = await runtime.save_checkpoint()
    assert current_checkpoint.state["phase"] == "completed"

    capabilities.release.set()
    await capabilities.returned.wait()
    for _ in range(10):
        await asyncio.sleep(0)

    assert (await runtime.save_checkpoint()).id == current_checkpoint.id


async def test_uncertain_tool_checkpoint_fails_closed_without_replay() -> None:
    class UncertainCapabilities(FakeCapabilities):
        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            raise RuntimeError("commit status unavailable")

        def is_replay_safe(self, name: str) -> bool:
            del name
            return False

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("account.update",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("account.update",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("account.update",),
        total_token_budget=100,
    )

    class RestrictedToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if completion.response.tool_calls:
                response = ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-call",
                            name="account.update",
                            arguments={"value": "safe"},
                        ),
                    ),
                    usage=completion.response.usage,
                )
                return GatewayCompletion(
                    response=response,
                    deployment_id=completion.deployment_id,
                    logical_model=completion.logical_model,
                    provider_id=completion.provider_id,
                    provider_model=completion.provider_model,
                    cost_usd=Decimal(0),
                )
            return completion

    first = make_runtime(
        RestrictedToolGateway(),
        tool_plan,
        capability_gateway=UncertainCapabilities(),
    )
    first_events: list[RunEvent] = []
    with pytest.raises(CapabilityOutcomeUncertain):
        async for event in first.run(context()):
            first_events.append(event)
    checkpoint = await first.save_checkpoint()
    persisted_artifacts = tuple(
        event.artifact for event in first_events if event.artifact is not None
    )
    second_capabilities = UncertainCapabilities()
    resumed = make_runtime(
        RestrictedToolGateway(),
        tool_plan,
        capability_gateway=second_capabilities,
    )
    await resumed.restore_checkpoint(checkpoint)

    with pytest.raises(CapabilityOutcomeUncertain):
        await collect(
            resumed,
            context(checkpoint=checkpoint, artifacts=persisted_artifacts),
        )

    checkpoint_2 = await resumed.save_checkpoint()
    assert checkpoint_2.state["models"] == checkpoint.state["models"]
    assert checkpoint_2.state["tools"] == checkpoint.state["tools"]
    third_gateway = RestrictedToolGateway()
    third_capabilities = UncertainCapabilities()
    third = make_runtime(
        third_gateway,
        tool_plan,
        capability_gateway=third_capabilities,
    )
    await third.restore_checkpoint(checkpoint_2)
    with pytest.raises(CapabilityOutcomeUncertain):
        await collect(
            third,
            context(checkpoint=checkpoint_2, artifacts=persisted_artifacts),
        )

    assert third_gateway.requests == []
    assert third_capabilities.calls == []


async def test_tool_idempotency_key_changes_when_canonical_arguments_change() -> None:
    class ArgumentGateway(ToolGateway):
        def __init__(self, query: str) -> None:
            super().__init__()
            self.query = query

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            id="provider-call",
                            name="web.search",
                            arguments={"q": self.query},
                        ),
                    ),
                    usage=TokenUsage(1, 1, 2),
                )
                if len(self.requests) == 1
                else ModelResponse(text="done", usage=TokenUsage(1, 1, 2))
            )
            return GatewayCompletion(
                response=response,
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=Decimal(0),
            )

    class KeyCapabilities(FakeCapabilities):
        def __init__(self) -> None:
            super().__init__()
            self.keys: list[str] = []

        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            self.keys.append(kwargs["idempotency_key"])
            return await super().execute(**kwargs)  # type: ignore[no-untyped-call]

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
    )
    capabilities = KeyCapabilities()
    await collect(
        make_runtime(
            ArgumentGateway("first"),
            tool_plan,
            capability_gateway=capabilities,
        ),
        context(),
    )
    await collect(
        make_runtime(
            ArgumentGateway("second"),
            tool_plan,
            capability_gateway=capabilities,
        ),
        context(),
    )

    assert len(capabilities.keys) == 2
    assert capabilities.keys[0] != capabilities.keys[1]


@pytest.mark.parametrize("limit_source", ["step", "run"])
async def test_one_monotonic_deadline_bounds_cumulative_step_work(
    limit_source: str,
) -> None:
    class SlowGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            await asyncio.sleep(0.06)
            return await super().complete_with_context(request)

    class SlowCapabilities(FakeCapabilities):
        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.06)
            return await super().execute(**kwargs)  # type: ignore[no-untyped-call]

    deadline = 0.1
    step_timeout = deadline if limit_source == "step" else 0.5
    run_timeout = deadline if limit_source == "run" else 0.5
    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
                timeout_seconds=step_timeout,
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
        total_timeout_seconds=0.5,
    )
    ctx = context().model_copy(update={"timeout_seconds": run_timeout})
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError):
        await collect(
            make_runtime(
                SlowGateway(),
                tool_plan,
                capability_gateway=SlowCapabilities(),
            ),
            ctx,
        )

    assert time.monotonic() - started < 0.2


async def test_trusted_cost_is_atomic_in_parallel_checkpoint_and_retry() -> None:
    priced_steps = tuple(
        step.model_copy(update={"cost_budget_usd": Decimal("0.10")})
        for step in plan(review=True).steps
    )
    priced_plan = plan(review=True).model_copy(
        update={
            "steps": priced_steps,
            "total_cost_usd": Decimal("1.00"),
        }
    )
    gateway = CostGateway(
        Decimal("0.01"),
        reviews=['{"verdict":"revise","feedback":"retry"}', '{"verdict":"approve"}'],
    )
    events = await collect(make_runtime(gateway, priced_plan), context())
    final_checkpoint = next(
        event.checkpoint for event in reversed(events) if event.checkpoint is not None
    )

    cost_events = [event for event in events if event.kind is EventKind.COST_RECORDED]
    assert len(cost_events) == 6
    assert all(event.cost_usd == Decimal("0.01") for event in cost_events)
    assert final_checkpoint.state["usage"] == {
        "tokens": 12,
        "cost_usd": "0.06",
    }
    assert final_checkpoint.state["step_usage"] == {
        "final": {"tokens": 2, "cost_usd": "0.01"},
        "left": {"tokens": 8, "cost_usd": "0.04"},
        "right": {"tokens": 2, "cost_usd": "0.01"},
    }


async def test_trusted_cost_fails_closed_at_step_budget() -> None:
    base = plan(max_parallelism=1)
    priced_plan = base.model_copy(
        update={
            "steps": tuple(
                step.model_copy(update={"cost_budget_usd": Decimal("0.01")}) for step in base.steps
            ),
            "total_cost_usd": Decimal(1),
        }
    )
    with pytest.raises(RuntimeExecutionError):
        await collect(
            make_runtime(CostGateway(Decimal("0.02")), priced_plan),
            context(),
        )


async def test_budget_exhaustion_is_audited_in_stable_terminal_checkpoint() -> None:
    priced_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
                cost_budget_usd=Decimal("0.01"),
            ),
        ),
        total_token_budget=100,
        total_cost_usd=Decimal("0.01"),
    )
    runtime = make_runtime(CostGateway(Decimal("0.02")), priced_plan)
    first_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in runtime.run(context()):
            first_events.append(event)
    checkpoint = await runtime.save_checkpoint()

    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["phase"] == "budget_exhausted"
    assert checkpoint.state["usage"] == {
        "tokens": 2,
        "cost_usd": "0.02",
    }
    assert checkpoint.state["step_usage"] == {"final": {"tokens": 2, "cost_usd": "0.02"}}
    assert cast(int, checkpoint.state["next_sequence"]) > max(
        event.sequence for event in first_events
    )
    resumed_gateway = CostGateway(Decimal("0.02"))
    resumed = make_runtime(resumed_gateway, priced_plan)
    await resumed.restore_checkpoint(checkpoint)
    resumed_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in resumed.run(context(checkpoint=checkpoint)):
            resumed_events.append(event)
    assert resumed_gateway.requests == []
    assert resumed_events[0].sequence > max(event.sequence for event in first_events)


async def test_audit_overflow_is_saturated_terminal_and_never_replayed() -> None:
    class OverflowGateway(FakeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text="bounded output",
                    usage=TokenUsage(100_000_001, 0, 100_000_001),
                ),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=Decimal(0),
            )

    overflow_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )
    gateway = OverflowGateway()
    runtime = make_runtime(gateway, overflow_plan)
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime, context())
    checkpoint = await runtime.save_checkpoint()

    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["phase"] == "audit_overflow"
    assert checkpoint.state["usage"] == {"tokens": 100_000_000, "cost_usd": "0"}
    assert checkpoint.state["audit_overflow"] == {
        "tokens": True,
        "cost_usd": False,
        "step_tokens": ("final",),
        "step_cost_usd": (),
    }

    resumed_gateway = OverflowGateway()
    resumed = make_runtime(resumed_gateway, overflow_plan)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError):
        await collect(resumed, context(checkpoint=checkpoint))
    assert resumed_gateway.requests == []


async def test_token_overage_is_audited_before_budget_failure() -> None:
    token_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=1,
            ),
        ),
        total_token_budget=1,
    )
    ctx = context().model_copy(update={"token_budget": 1})
    runtime = make_runtime(FakeGateway(), token_plan)

    with pytest.raises(RuntimeExecutionError):
        await collect(runtime, ctx)
    checkpoint = await runtime.save_checkpoint()

    assert checkpoint.state["phase"] == "budget_exhausted"
    assert checkpoint.state["usage"] == {"tokens": 2, "cost_usd": "0"}
    assert checkpoint.state["step_usage"] == {"final": {"tokens": 2, "cost_usd": "0"}}


async def test_restored_step_usage_prevents_cost_cap_bypass_without_replay() -> None:
    class CostToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            return GatewayCompletion(
                response=completion.response,
                deployment_id=completion.deployment_id,
                logical_model=completion.logical_model,
                provider_id=completion.provider_id,
                provider_model=completion.provider_model,
                cost_usd=Decimal("0.08"),
            )

    class BlockingCapabilities(FakeCapabilities):
        def __init__(self, *, block: bool) -> None:
            super().__init__()
            self.block = block
            self.started = asyncio.Event()

        async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            return await super().execute(**kwargs)  # type: ignore[no-untyped-call]

    tool_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
                allowed_tools=("web.search",),
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                tools=("web.search",),
                final_synthesizer=True,
                token_budget=100,
                cost_budget_usd=Decimal("0.10"),
            ),
        ),
        allowed_tools=("web.search",),
        total_token_budget=100,
        total_cost_usd=Decimal("0.10"),
    )
    first_capabilities = BlockingCapabilities(block=True)
    first = make_runtime(
        CostToolGateway(),
        tool_plan,
        capability_gateway=first_capabilities,
    )
    first_events: list[RunEvent] = []

    async def consume_first() -> None:
        async for event in first.run(context()):
            first_events.append(event)

    first_task = asyncio.create_task(consume_first())
    await first_capabilities.started.wait()
    checkpoint = await first.save_checkpoint()
    await first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert checkpoint.state["step_usage"] == {"final": {"tokens": 2, "cost_usd": "0.08"}}

    resumed_gateway = CostToolGateway()
    resumed_capabilities = BlockingCapabilities(block=False)
    resumed = make_runtime(
        resumed_gateway,
        tool_plan,
        capability_gateway=resumed_capabilities,
    )
    await resumed.restore_checkpoint(checkpoint)
    persisted_artifacts = tuple(
        event.artifact for event in first_events if event.artifact is not None
    )
    with pytest.raises(RuntimeExecutionError):
        await collect(
            resumed,
            context(checkpoint=checkpoint, artifacts=persisted_artifacts),
        )
    exhausted = await resumed.save_checkpoint()

    assert len(resumed_gateway.requests) == 1
    assert resumed_capabilities.calls == [("writer", "web.search")]
    assert exhausted.state["phase"] == "budget_exhausted"
    assert exhausted.state["step_usage"] == {"final": {"tokens": 4, "cost_usd": "0.16"}}


async def test_unaccounted_gateway_completion_fails_closed_with_audit_checkpoint() -> None:
    class UnknownCostGateway(FakeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            return GatewayCompletion(
                response=completion.response,
                deployment_id=completion.deployment_id,
                logical_model=completion.logical_model,
                provider_id=completion.provider_id,
                provider_model=completion.provider_model,
                cost_usd=None,
            )

    single_plan = DispatchPlan(
        agents=(
            AgentSpec(
                id="writer",
                role="writer",
                goal="Write",
                logical_model="general",
            ),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
            ),
        ),
        total_token_budget=100,
    )
    runtime = make_runtime(UnknownCostGateway(), single_plan)
    with pytest.raises(RuntimeExecutionError):
        await collect(runtime, context())
    checkpoint = await runtime.save_checkpoint()

    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["phase"] == "unaccounted"
    assert checkpoint.state["usage"] == {"tokens": 2, "cost_usd": "0"}
    assert checkpoint.state["step_usage"] == {"final": {"tokens": 2, "cost_usd": "0"}}


async def test_private_factory_receives_locked_down_framework_generation() -> None:
    captured: dict[str, object] = {}

    class Factory:
        def build(self, agents, tasks, *, share_crew, telemetry_disabled):  # type: ignore[no-untyped-def]
            captured.update(
                agents=agents,
                tasks=tasks,
                share_crew=share_crew,
                telemetry_disabled=telemetry_disabled,
            )
            return FastGeneration()

    runtime = make_runtime(FakeGateway(), crew_factory=Factory())
    await collect(runtime, context())
    assert captured["share_crew"] is False
    assert captured["telemetry_disabled"] is True
    agents = captured["agents"]
    assert isinstance(agents, tuple)
    assert all(
        not item.allow_delegation and not item.memory and not item.code_execution for item in agents
    )


async def test_real_crewai_akickoff_uses_only_model_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_root = tmp_path / "crewai"
    CrewAIObjectFactory(storage_dir=storage_root).build(
        (), (), share_crew=False, telemetry_disabled=True
    )
    crewai_module = importlib.import_module("crewai")
    real_akickoff = crewai_module.Crew.akickoff
    native_calls: list[str] = []

    async def tracked_akickoff(crew, *args, **kwargs):  # type: ignore[no-untyped-def]
        native_calls.append("akickoff")
        return await real_akickoff(crew, *args, **kwargs)

    def forbidden_sync_kickoff(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("Agent Hub must use CrewAI's native async kickoff")

    monkeypatch.setattr(crewai_module.Crew, "akickoff", tracked_akickoff)
    monkeypatch.setattr(crewai_module.Crew, "kickoff", forbidden_sync_kickoff)
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "true")
    scoped_storage = storage_root / "agent-hub" / str(TENANT_ID) / str(RUN_ID)
    scoped_storage.mkdir(parents=True)
    (scoped_storage / ".crewai_user.json").write_text(
        json.dumps({"first_execution_done": True, "trace_consent": True}),
        encoding="utf-8",
    )

    def forbidden_executor(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("Agent Hub must not submit CrewAI work to an executor")

    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", forbidden_executor)
    gateway = FakeGateway()
    events = await collect(
        make_runtime(
            gateway,
            crew_factory=CrewAIObjectFactory(storage_dir=storage_root),
        ),
        context(),
    )
    tracing_utils = importlib.import_module("crewai.events.listeners.tracing.utils")

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    assert native_calls == ["akickoff", "akickoff", "akickoff"]
    assert tracing_utils.is_tracing_enabled_in_context() is False


async def test_real_crewai_cancellation_reaches_gateway_without_residual_work_and_reuses(
    tmp_path: Path,
) -> None:
    class CancellableGateway:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.block = True
            self.requests: list[ModelRequest] = []
            self.cancelled = 0
            self.in_flight: set[asyncio.Task[object]] = set()

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            current = asyncio.current_task()
            assert current is not None
            task = cast(asyncio.Task[object], current)
            self.in_flight.add(task)
            self.requests.append(request)
            self.started.set()
            try:
                if self.block:
                    await self.release.wait()
                return GatewayCompletion(
                    response=ModelResponse(text="safe result", usage=TokenUsage(1, 1, 2)),
                    deployment_id="primary",
                    logical_model=request.logical_model,
                    provider_id="deepseek",
                    provider_model="deepseek/chat",
                    cost_usd=Decimal(0),
                )
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.in_flight.discard(task)

    gateway = CancellableGateway()
    runtime = make_runtime(
        gateway,
        crew_factory=CrewAIObjectFactory(storage_dir=tmp_path / "cancel-crewai"),
    )

    for _ in range(2):
        gateway.started = asyncio.Event()
        consumer = asyncio.create_task(collect(runtime, context()))
        await asyncio.wait_for(gateway.started.wait(), timeout=15)
        await asyncio.wait_for(runtime.cancel(), timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await consumer
        request_count = len(gateway.requests)
        await asyncio.sleep(0.05)
        assert gateway.in_flight == set()
        assert len(gateway.requests) == request_count

    assert gateway.cancelled >= 2
    gateway.block = False
    gateway.release.set()
    assert (await collect(runtime, context()))[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_real_crewai_storage_is_scoped_without_global_pollution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdirs_module = importlib.import_module("appdirs")
    forbidden_calls: list[tuple[object, ...]] = []

    def forbidden_default_storage(*args, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        forbidden_calls.append(args)
        raise AssertionError("default user storage must not be consulted")

    monkeypatch.setattr(appdirs_module, "user_data_dir", forbidden_default_storage)
    before_function = appdirs_module.user_data_dir
    before_environment = dict(os.environ)
    before_cwd = os.getcwd()
    storage_root = tmp_path / "controlled-crewai"

    events = await collect(
        make_runtime(
            FakeGateway(),
            crew_factory=CrewAIObjectFactory(storage_dir=storage_root),
        ),
        context(),
    )

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert forbidden_calls == []
    assert appdirs_module.user_data_dir is before_function
    assert dict(os.environ) == before_environment
    assert os.getcwd() == before_cwd
    files = tuple(path for path in storage_root.rglob("*") if path.is_file())
    assert files
    assert all(str(path).startswith(str(storage_root)) for path in files)


async def test_real_crewai_concurrent_runs_use_distinct_storage_scopes(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "concurrent-crewai"
    factory = CrewAIObjectFactory(storage_dir=storage_root)
    first_context = context().model_copy(update={"run_id": uuid4(), "tenant_id": uuid4()})
    second_context = context().model_copy(update={"run_id": uuid4(), "tenant_id": uuid4()})

    first_events, second_events = await asyncio.gather(
        collect(
            make_runtime(FakeGateway(), crew_factory=factory),
            first_context,
        ),
        collect(
            make_runtime(FakeGateway(), crew_factory=factory),
            second_context,
        ),
    )

    assert first_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert second_events[-1].kind is EventKind.RUNTIME_COMPLETED
    for ctx in (first_context, second_context):
        scope = storage_root / "agent-hub" / str(ctx.tenant_id) / str(ctx.run_id)
        assert scope.is_dir()
        assert any(path.is_file() for path in scope.rglob("*"))


def test_crewai_storage_router_preserves_external_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    sentinel = "external-crewai-storage"
    monkeypatch.setattr(
        adapter_module,
        "_CREWAI_DEFAULT_STORAGE_PATH",
        lambda: sentinel,
    )

    assert adapter_module._contextual_crewai_storage_path() == sentinel


def test_crewai_routers_capture_latest_external_overrides_on_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    core_paths = importlib.import_module("crewai_core.paths")
    factory = CrewAIObjectFactory(storage_dir=tmp_path / "router-rebuild")
    factory.build((), (), share_crew=False, telemetry_disabled=True)
    trace_module = importlib.import_module("crewai.events.listeners.tracing.trace_listener")
    telemetry_module = importlib.import_module("crewai.telemetry.telemetry")
    for name in (
        "_CREWAI_DEFAULT_STORAGE_PATH",
        "_CREWAI_DEFAULT_TRACE_SETUP",
        "_CREWAI_DEFAULT_TELEMETRY_CHECK",
    ):
        monkeypatch.setattr(adapter_module, name, getattr(adapter_module, name))
    storage_calls: list[str] = []
    trace_calls: list[str] = []
    telemetry_calls: list[str] = []

    def external_storage() -> str:
        storage_calls.append("storage")
        return "external-new-storage"

    def external_trace(listener: object, event_bus: object) -> None:
        del listener, event_bus
        trace_calls.append("trace")

    def external_telemetry(instance: object) -> bool:
        del instance
        telemetry_calls.append("telemetry")
        return True

    monkeypatch.setattr(core_paths, "db_storage_path", external_storage)
    monkeypatch.setattr(
        trace_module.TraceCollectionListener,
        "setup_listeners",
        external_trace,
    )
    monkeypatch.setattr(
        telemetry_module.Telemetry,
        "_should_execute_telemetry",
        external_telemetry,
    )

    factory.build((), (), share_crew=False, telemetry_disabled=True)

    assert core_paths.db_storage_path() == "external-new-storage"
    trace_module.TraceCollectionListener.setup_listeners(object(), object())
    assert telemetry_module.Telemetry()._should_execute_telemetry() is True
    assert storage_calls == ["storage"]
    assert trace_calls == ["trace"]
    assert telemetry_calls == ["telemetry"]


async def test_preimported_ready_telemetry_is_disabled_only_inside_agent_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = CrewAIObjectFactory(storage_dir=tmp_path / "preimported-telemetry")
    factory.build((), (), share_crew=False, telemetry_disabled=True)
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(
        adapter_module,
        "_CREWAI_DEFAULT_TELEMETRY_CHECK",
        adapter_module._CREWAI_DEFAULT_TELEMETRY_CHECK,
    )
    telemetry_module = importlib.import_module("crewai.telemetry.telemetry")
    telemetry = telemetry_module.Telemetry()
    telemetry.ready = True
    outbound_attempts: list[str] = []

    def external_telemetry(instance: object) -> bool:
        del instance
        outbound_attempts.append("attempt")
        return True

    monkeypatch.setattr(
        telemetry_module.Telemetry,
        "_should_execute_telemetry",
        external_telemetry,
    )
    factory.build((), (), share_crew=False, telemetry_disabled=True)

    events = await collect(
        make_runtime(FakeGateway(), crew_factory=factory),
        context(),
    )

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert outbound_attempts == []
    assert telemetry._should_execute_telemetry() is True
    assert outbound_attempts == ["attempt"]


async def test_missing_executor_context_fails_closed_without_external_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    core_paths = importlib.import_module("crewai_core.paths")
    factory = CrewAIObjectFactory(storage_dir=tmp_path / "executor-context")
    factory.build((), (), share_crew=False, telemetry_disabled=True)
    external_calls: list[str] = []
    trace_calls: list[str] = []
    telemetry_calls: list[str] = []

    def external_storage() -> str:
        external_calls.append("external")
        return "must-not-be-used"

    def external_trace(listener: object, event_bus: object) -> None:
        del listener, event_bus
        trace_calls.append("external")

    def external_telemetry(instance: object) -> bool:
        del instance
        telemetry_calls.append("external")
        return True

    monkeypatch.setattr(adapter_module, "_CREWAI_DEFAULT_STORAGE_PATH", external_storage)
    monkeypatch.setattr(
        adapter_module,
        "_CREWAI_DEFAULT_TRACE_SETUP",
        external_trace,
    )
    monkeypatch.setattr(
        adapter_module,
        "_CREWAI_DEFAULT_TELEMETRY_CHECK",
        external_telemetry,
    )
    scope = tmp_path / "executor-context" / "scope"
    with adapter_module._active_crewai_scope(scope):
        empty_context = Context()
        with pytest.raises(RuntimeError, match="context propagation"):
            empty_context.run(core_paths.db_storage_path)
        empty_context.run(adapter_module._contextual_crewai_trace_setup, object(), object())
        assert (
            empty_context.run(adapter_module._contextual_crewai_telemetry_check, object()) is False
        )

    assert external_calls == []
    assert trace_calls == []
    assert telemetry_calls == []
    assert await asyncio.get_running_loop().run_in_executor(
        None,
        adapter_module._call_in_crewai_scope,
        scope,
        core_paths.db_storage_path,
    ) == str(scope)
    assert (
        await asyncio.get_running_loop().run_in_executor(
            None,
            adapter_module._call_in_crewai_scope,
            scope,
            adapter_module._contextual_crewai_telemetry_check,
            object(),
        )
        is False
    )
    await asyncio.get_running_loop().run_in_executor(
        None,
        adapter_module._call_in_crewai_scope,
        scope,
        adapter_module._contextual_crewai_trace_setup,
        object(),
        object(),
    )
    inner_context = Context()
    with pytest.raises(RuntimeError, match="context propagation"):
        await asyncio.get_running_loop().run_in_executor(
            None,
            adapter_module._call_in_crewai_scope,
            scope,
            inner_context.run,
            core_paths.db_storage_path,
        )
    await asyncio.get_running_loop().run_in_executor(
        None,
        adapter_module._call_in_crewai_scope,
        scope,
        inner_context.run,
        adapter_module._contextual_crewai_trace_setup,
        object(),
        object(),
    )
    assert (
        await asyncio.get_running_loop().run_in_executor(
            None,
            adapter_module._call_in_crewai_scope,
            scope,
            inner_context.run,
            adapter_module._contextual_crewai_telemetry_check,
            object(),
        )
        is False
    )
    assert external_calls == []
    assert trace_calls == []
    assert telemetry_calls == []


async def test_inner_empty_context_uses_invocation_identity_without_affecting_external_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    external_calls: list[str] = []

    def external_storage() -> str:
        external_calls.append("storage")
        return "external-storage"

    def external_trace(listener: object, event_bus: object) -> None:
        del listener, event_bus
        external_calls.append("trace")

    def external_telemetry(instance: object) -> bool:
        del instance
        external_calls.append("telemetry")
        return True

    monkeypatch.setattr(adapter_module, "_CREWAI_DEFAULT_STORAGE_PATH", external_storage)
    monkeypatch.setattr(adapter_module, "_CREWAI_DEFAULT_TRACE_SETUP", external_trace)
    monkeypatch.setattr(
        adapter_module,
        "_CREWAI_DEFAULT_TELEMETRY_CHECK",
        external_telemetry,
    )
    scope = tmp_path / "task-identity" / "scope"

    async def external_empty_context() -> tuple[str, bool]:
        storage = adapter_module._contextual_crewai_storage_path()
        adapter_module._contextual_crewai_trace_setup(object(), object())
        telemetry = adapter_module._contextual_crewai_telemetry_check(object())
        return storage, telemetry

    with adapter_module._active_crewai_scope(scope):
        empty_context = Context()
        with pytest.raises(RuntimeError, match="context propagation"):
            empty_context.run(adapter_module._contextual_crewai_storage_path)
        empty_context.run(
            adapter_module._contextual_crewai_trace_setup,
            object(),
            object(),
        )
        assert (
            empty_context.run(adapter_module._contextual_crewai_telemetry_check, object()) is False
        )
        external_result = await asyncio.create_task(
            external_empty_context(),
            context=Context(),
        )

    assert external_result == ("external-storage", True)
    assert external_calls == ["storage", "trace", "telemetry"]


@pytest.mark.parametrize(
    "failure_module",
    [
        "crewai_core.paths",
        "crewai.events.listeners.tracing.trace_listener",
    ],
)
async def test_crewai_router_initialization_failure_recovers_under_concurrency(
    failure_module: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    real_import = adapter_module.importlib.import_module
    failed_once = False

    def flaky_import(name: str, package: str | None = None):  # type: ignore[no-untyped-def]
        nonlocal failed_once
        if name == failure_module and not failed_once:
            failed_once = True
            raise RuntimeError("synthetic initialization failure")
        return real_import(name, package)

    monkeypatch.setattr(adapter_module.importlib, "import_module", flaky_import)
    before_environment = dict(os.environ)
    factory = CrewAIObjectFactory(storage_dir=tmp_path / "init-recovery")

    results = await asyncio.gather(
        asyncio.to_thread(
            factory.build,
            (),
            (),
            share_crew=False,
            telemetry_disabled=True,
        ),
        asyncio.to_thread(
            factory.build,
            (),
            (),
            share_crew=False,
            telemetry_disabled=True,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert dict(os.environ) == before_environment


@pytest.mark.parametrize("step_count", (33, 64))
async def test_shared_repository_recovers_long_chain_without_context_artifacts(
    step_count: int,
) -> None:
    repository = InMemoryArtifactRepository()
    dispatch_plan = chain_plan(step_count)
    first = make_runtime(
        FakeGateway(), dispatch_plan, artifact_repository=repository
    )
    await collect(first, context(token_budget=dispatch_plan.total_token_budget))
    checkpoint = await first.save_checkpoint()
    registry = cast(Mapping[str, str], checkpoint.state["artifact_registry"])
    assert len(registry) == step_count * 2

    gateway = FakeGateway()
    resumed = make_runtime(gateway, dispatch_plan, artifact_repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    events = await collect(
        resumed,
        context(checkpoint=checkpoint, token_budget=dispatch_plan.total_token_budget),
    )

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert gateway.requests == []


async def test_repository_recovers_more_than_64_review_retry_artifacts() -> None:
    step_count = 11
    reviews = [item for _ in range(step_count) for item in (
        '{"verdict":"revise","feedback":"tighten"}',
        '{"verdict":"approve"}',
    )]
    repository = InMemoryArtifactRepository()
    dispatch_plan = chain_plan(step_count, review=True)
    first = make_runtime(
        FakeGateway(reviews=reviews),
        dispatch_plan,
        artifact_repository=repository,
    )
    await collect(first, context(token_budget=dispatch_plan.total_token_budget))
    checkpoint = await first.save_checkpoint()
    registry = cast(Mapping[str, str], checkpoint.state["artifact_registry"])
    assert len(registry) > 64

    gateway = FakeGateway()
    resumed = make_runtime(gateway, dispatch_plan, artifact_repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    assert (
        await collect(
            resumed,
            context(checkpoint=checkpoint, token_budget=dispatch_plan.total_token_budget),
        )
    )[-1].kind is EventKind.RUNTIME_COMPLETED
    assert gateway.requests == []


async def test_missing_repository_artifacts_preserve_restored_checkpoint() -> None:
    repository = InMemoryArtifactRepository()
    first = make_runtime(FakeGateway(), one_step_plan(), artifact_repository=repository)
    await collect(first, context(token_budget=100))
    checkpoint = await first.save_checkpoint()
    resumed = make_runtime(
        FakeGateway(),
        one_step_plan(),
        artifact_repository=InMemoryArtifactRepository(),
    )
    await resumed.restore_checkpoint(checkpoint)

    with pytest.raises(RuntimeExecutionError, match="artifacts are unavailable"):
        await collect(resumed, context(checkpoint=checkpoint, token_budget=100))

    assert (await resumed.save_checkpoint()).id == checkpoint.id


async def test_tampered_repository_hash_preserves_restored_checkpoint() -> None:
    repository = InMemoryArtifactRepository()
    first = make_runtime(FakeGateway(), one_step_plan(), artifact_repository=repository)
    await collect(first, context(token_budget=100))
    payload = (await first.save_checkpoint()).to_payload()
    state = cast(dict[str, object], payload["state"])
    registry = cast(dict[str, object], state["artifact_registry"])
    registry[next(iter(registry))] = "0" * 64
    payload["state_sha256"] = ""
    tampered = RuntimeCheckpoint.from_payload(payload)
    resumed = make_runtime(FakeGateway(), one_step_plan(), artifact_repository=repository)
    await resumed.restore_checkpoint(tampered)

    with pytest.raises(RuntimeExecutionError, match="artifacts are unavailable"):
        await collect(resumed, context(checkpoint=tampered, token_budget=100))

    assert (await resumed.save_checkpoint()).id == tampered.id


async def test_known_checkpoint_metadata_overflow_fails_before_gateway() -> None:
    dispatch_plan = chain_plan(64, review=True)
    gateway = FakeGateway()

    with pytest.raises(RuntimeExecutionError, match="metadata budget"):
        await collect(
            make_runtime(gateway, dispatch_plan),
            context(token_budget=dispatch_plan.total_token_budget),
        )

    assert gateway.requests == []


async def test_partial_repository_write_recovers_from_last_durable_boundary() -> None:
    class FailAfterOnePut(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.puts = 0

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            if self.puts >= 1:
                raise ArtifactRepositoryError("artifact repository write failed")
            self.puts += 1
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

    failing = FailAfterOnePut()
    first = make_runtime(FakeGateway(), one_step_plan(), artifact_repository=failing)
    with pytest.raises(RuntimeExecutionError):
        await collect(first, context(token_budget=100))
    checkpoint = await first.save_checkpoint()
    models = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert next(iter(models.values()))["status"] == "succeeded"

    gateway = FakeGateway()
    resumed = make_runtime(
        gateway, one_step_plan(), artifact_repository=failing.delegate
    )
    await resumed.restore_checkpoint(checkpoint)
    assert (
        await collect(resumed, context(checkpoint=checkpoint, token_budget=100))
    )[-1].kind is EventKind.RUNTIME_COMPLETED
    assert gateway.requests == []


async def test_cancelled_repository_hydration_never_replaces_checkpoint() -> None:
    class BlockingGetRepository(DelegatingArtifactRepository):
        def __init__(self, delegate: InMemoryArtifactRepository) -> None:
            self.delegate = delegate
            self.started = asyncio.Event()

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            self.started.set()
            await asyncio.Event().wait()
            return await self.delegate.get_many(tenant_id, run_id, references)

    repository = InMemoryArtifactRepository()
    first = make_runtime(FakeGateway(), one_step_plan(), artifact_repository=repository)
    await collect(first, context(token_budget=100))
    checkpoint = await first.save_checkpoint()
    blocking = BlockingGetRepository(repository)
    resumed = make_runtime(FakeGateway(), one_step_plan(), artifact_repository=blocking)
    await resumed.restore_checkpoint(checkpoint)
    task = asyncio.create_task(
        collect(resumed, context(checkpoint=checkpoint, token_budget=100))
    )
    await blocking.started.wait()

    await resumed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await resumed.save_checkpoint()).id == checkpoint.id


async def test_failed_model_artifact_put_preserves_running_boundary_and_zero_usage() -> None:
    class RejectingRepository:
        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            del tenant_id, run_id, reference, write_id

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            del tenant_id, run_id, artifact, write_id
            raise ArtifactRepositoryError("artifact repository write failed")

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            del tenant_id, run_id
            assert references == ()
            return ()

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            del tenant_id, run_id, reference, write_id
            return False

    repository = RejectingRepository()
    gateway = FakeGateway()
    first = make_runtime(gateway, one_step_plan(), artifact_repository=repository)

    with pytest.raises(RuntimeExecutionError):
        await collect(first, context(token_budget=100))

    checkpoint = await first.save_checkpoint()
    models = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert len(gateway.requests) == 1
    assert {item["status"] for item in models.values()} == {"running"}
    assert checkpoint.state["usage"] == {"tokens": 0, "cost_usd": "0"}
    assert checkpoint.state["step_usage"] == {}
    assert checkpoint.state["artifact_registry"] == {}

    for _ in range(2):
        resumed_gateway = FakeGateway()
        resumed = make_runtime(
            resumed_gateway,
            one_step_plan(),
            artifact_repository=repository,
        )
        await resumed.restore_checkpoint(checkpoint)
        with pytest.raises(ModelOutcomeUncertain, match="requires confirmation"):
            await collect(resumed, context(checkpoint=checkpoint, token_budget=100))
        assert resumed_gateway.requests == []
        checkpoint = await resumed.save_checkpoint()


class BatchedToolGateway(FakeGateway):
    def __init__(self, batches: tuple[int, ...]) -> None:
        super().__init__()
        self.batches = list(batches)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        count = self.batches.pop(0) if self.batches else 0
        response = (
            ModelResponse(
                text=None,
                tool_calls=tuple(
                    ToolCall(
                        id=f"provider-{len(self.requests)}-{index}",
                        name="web.search",
                        arguments={"q": str(index)},
                    )
                    for index in range(count)
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if count
            else ModelResponse(text="bounded answer", usage=TokenUsage(1, 1, 2))
        )
        return GatewayCompletion(
            response=response,
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek/chat",
            cost_usd=Decimal(0),
        )


@pytest.mark.parametrize("call_count", (17, 180))
async def test_tool_call_response_limit_fails_before_capability_and_terminates(
    call_count: int,
) -> None:
    gateway = BatchedToolGateway((call_count,))
    capabilities = FakeCapabilities()
    runtime = make_runtime(
        gateway,
        one_step_plan(tools=("web.search",)),
        capability_gateway=capabilities,
    )

    with pytest.raises(RuntimeExecutionError, match="tool call limit"):
        await asyncio.wait_for(collect(runtime, context(token_budget=100)), timeout=2)

    assert len(gateway.requests) == 1
    assert capabilities.calls == []
    gateway.batches = [0]
    assert (
        await collect(runtime, context(token_budget=100))
    )[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_tool_call_response_at_limit_executes_normally() -> None:
    gateway = BatchedToolGateway((16, 0))
    capabilities = FakeCapabilities()

    events = await collect(
        make_runtime(
            gateway,
            one_step_plan(tools=("web.search",)),
            capability_gateway=capabilities,
        ),
        context(token_budget=100),
    )

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(capabilities.calls) == 16


@pytest.mark.parametrize(
    ("last_batch", "succeeds", "expected_calls"),
    ((11, True, 59), (12, False, 48)),
)
async def test_cumulative_tool_evidence_budget_is_preflighted_before_batch(
    last_batch: int,
    succeeds: bool,
    expected_calls: int,
) -> None:
    gateway = BatchedToolGateway((16, 16, 16, last_batch, 0))
    capabilities = FakeCapabilities()
    runtime = make_runtime(
        gateway,
        one_step_plan(tools=("web.search",)),
        capability_gateway=capabilities,
    )

    if succeeds:
        assert (
            await collect(runtime, context(token_budget=100))
        )[-1].kind is EventKind.RUNTIME_COMPLETED
    else:
        with pytest.raises(RuntimeExecutionError):
            await asyncio.wait_for(collect(runtime, context(token_budget=100)), timeout=2)
        checkpoint = await runtime.save_checkpoint()
        models = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
        latest = max(models.values(), key=lambda item: cast(int, item["call_index"]))
        assert latest["status"] == "running"
        assert checkpoint.state["usage"] == {"tokens": 6, "cost_usd": "0"}
    assert len(capabilities.calls) == expected_calls


async def test_checkpoint_serialization_failure_still_terminates_and_allows_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(FakeGateway(), one_step_plan())
    original = runtime._make_checkpoint

    def fail_checkpoint(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise RuntimeError("synthetic checkpoint failure")

    monkeypatch.setattr(runtime, "_make_checkpoint", fail_checkpoint)
    with pytest.raises(RuntimeExecutionError):
        await asyncio.wait_for(collect(runtime, context(token_budget=100)), timeout=2)

    monkeypatch.setattr(runtime, "_make_checkpoint", original)
    assert (
        await collect(runtime, context(token_budget=100))
    )[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_failure_event_exception_still_delivers_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    original = adapter_module._Sequence.event

    async def fail_runtime_failed(self, **values):  # type: ignore[no-untyped-def]
        if values.get("kind") is EventKind.RUNTIME_FAILED:
            raise RuntimeError("synthetic event failure")
        return await original(self, **values)

    class RejectGateway(FakeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            del request
            raise RuntimeError("synthetic gateway failure")

    monkeypatch.setattr(adapter_module._Sequence, "event", fail_runtime_failed)
    with pytest.raises(RuntimeExecutionError):
        await asyncio.wait_for(
            collect(make_runtime(RejectGateway(), one_step_plan()), context(token_budget=100)),
            timeout=2,
        )


@pytest.mark.parametrize("call_count", (17, 180))
def test_restored_model_artifact_rejects_oversized_tool_call_batch(
    call_count: int,
) -> None:
    completion = GatewayCompletion(
        response=ModelResponse(
            text=None,
            tool_calls=tuple(
                ToolCall(id=f"call-{index}", name="web.search", arguments={"q": "x"})
                for index in range(call_count)
            ),
            usage=TokenUsage(1, 1, 2),
        ),
        deployment_id="primary",
        logical_model="general",
        provider_id="deepseek",
        provider_model="deepseek/chat",
        cost_usd=Decimal(0),
    )
    artifact = CrewDispatchRuntime._model_artifact(completion, "writer", ())

    with pytest.raises(RuntimeExecutionError, match="artifact is invalid"):
        CrewDispatchRuntime._completion_from_model_artifact(artifact)


@pytest.mark.parametrize("mode", ("missing", "wrong"))
async def test_repository_verification_failure_keeps_model_running_and_terminates(
    mode: str,
) -> None:
    class UntrustedRepository:
        def __init__(self) -> None:
            self.stored: Artifact | None = None
            self.write_id: UUID | None = None

        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            del tenant_id, run_id, reference, write_id

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            del tenant_id, run_id
            self.stored = artifact
            self.write_id = write_id

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            del tenant_id, run_id, references
            if mode == "missing":
                return ()
            assert self.stored is not None
            return (
                Artifact(
                    id=self.stored.id,
                    type="model_response",
                    producer=self.stored.producer,
                    content={"text": "wrong"},
                    source_ids=self.stored.source_ids,
                    provenance=self.stored.provenance,
                ),
            )

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            del tenant_id, run_id
            if (
                self.stored is None
                or self.write_id != write_id
                or self.stored.id != reference.id
                or self.stored.content_sha256 != reference.sha256
            ):
                return False
            self.stored = None
            self.write_id = None
            return True

    gateway = FakeGateway()
    repository = UntrustedRepository()
    runtime = make_runtime(
        gateway,
        one_step_plan(),
        artifact_repository=repository,
    )

    with pytest.raises(RuntimeExecutionError):
        await asyncio.wait_for(collect(runtime, context(token_budget=100)), timeout=2)

    checkpoint = await runtime.save_checkpoint()
    models = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert {item["status"] for item in models.values()} == {"running"}
    assert checkpoint.state["artifact_registry"] == {}
    assert len(gateway.requests) == 1
    assert repository.stored is None


async def test_candidate_checkpoint_failure_keeps_last_running_boundary() -> None:
    gateway = FakeGateway()
    runtime = make_runtime(gateway, one_step_plan())
    original = runtime._make_checkpoint
    calls = 0

    def fail_candidate(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic candidate checkpoint failure")
        return original(*args, **kwargs)

    runtime._make_checkpoint = fail_candidate  # type: ignore[method-assign]
    with pytest.raises(RuntimeExecutionError):
        await asyncio.wait_for(collect(runtime, context(token_budget=100)), timeout=2)

    checkpoint = await runtime.save_checkpoint()
    models = cast(Mapping[str, Mapping[str, object]], checkpoint.state["models"])
    assert {item["status"] for item in models.values()} == {"running"}
    assert checkpoint.state["usage"] == {"tokens": 0, "cost_usd": "0"}
    assert checkpoint.state["artifact_registry"] == {}
    assert len(gateway.requests) == 1


async def test_unexpected_repository_exception_still_delivers_terminal() -> None:
    class ExplodingRepository:
        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            del tenant_id, run_id, reference, write_id

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            del tenant_id, run_id, artifact, write_id
            raise ValueError("synthetic repository defect")

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            del tenant_id, run_id, references
            return ()

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            del tenant_id, run_id, reference, write_id
            return False

    runtime = make_runtime(
        FakeGateway(),
        one_step_plan(),
        artifact_repository=ExplodingRepository(),
    )

    with pytest.raises(RuntimeExecutionError, match="execution failed"):
        await asyncio.wait_for(collect(runtime, context(token_budget=100)), timeout=2)


async def test_pre_capability_checkpoints_never_publish_provisional_success() -> None:
    class BlockingCapabilities(FakeCapabilities):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def execute(
            self,
            *,
            tenant_id: UUID,
            run_id: UUID,
            actor: str,
            name: str,
            arguments: Mapping[str, JsonValue],
            idempotency_key: str,
        ) -> Mapping[str, JsonValue]:
            del tenant_id, run_id, actor, name, arguments, idempotency_key
            self.started.set()
            await asyncio.Event().wait()
            return {}  # pragma: no cover

    repository = InMemoryArtifactRepository()
    capabilities = BlockingCapabilities()
    runtime = make_runtime(
        ToolGateway(),
        one_step_plan(tools=("web.search",)),
        capability_gateway=capabilities,
        artifact_repository=repository,
    )
    events: list[RunEvent] = []
    checkpoint_seen = asyncio.Event()

    async def consume() -> None:
        async for event in runtime.run(context(token_budget=100)):
            events.append(event)
            if event.kind is EventKind.CHECKPOINT_SAVED:
                checkpoint_seen.set()

    task = asyncio.create_task(consume())
    await capabilities.started.wait()
    await checkpoint_seen.wait()
    checkpoints = tuple(
        event.checkpoint
        for event in events
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    )
    assert checkpoints
    for checkpoint in checkpoints:
        tools = cast(Mapping[str, Mapping[str, object]], checkpoint.state["tools"])
        assert all(tool["status"] != "succeeded" for tool in tools.values())
        registry = cast(Mapping[str, str], checkpoint.state["artifact_registry"])
        references = tuple(
            ArtifactReference(id=UUID(artifact_id), sha256=sha256)
            for artifact_id, sha256 in registry.items()
        )
        assert len(
            await repository.get_many(TENANT_ID, RUN_ID, references)
        ) == len(references)

    await runtime.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "terminal_phase",
    ("budget_exhausted", "unaccounted", "audit_overflow"),
)
async def test_stable_terminal_checkpoint_is_emitted_and_durably_restorable(
    terminal_phase: str,
) -> None:
    class TerminalGateway(FakeGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            usage = (
                TokenUsage(100_000_001, 0, 100_000_001)
                if terminal_phase == "audit_overflow"
                else TokenUsage(1, 1, 2)
            )
            return GatewayCompletion(
                response=ModelResponse(text="bounded", usage=usage),
                deployment_id="primary",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/chat",
                cost_usd=(
                    None
                    if terminal_phase == "unaccounted"
                    else Decimal("0.02")
                    if terminal_phase == "budget_exhausted"
                    else Decimal(0)
                ),
            )

    terminal_plan = DispatchPlan(
        agents=(
            AgentSpec(id="writer", role="writer", goal="Write", logical_model="general"),
        ),
        steps=(
            DispatchStep(
                id="final",
                agent="writer",
                task="Answer",
                final_synthesizer=True,
                token_budget=100,
                cost_budget_usd=(
                    Decimal("0.01")
                    if terminal_phase == "budget_exhausted"
                    else Decimal(0)
                ),
            ),
        ),
        total_token_budget=100,
        total_cost_usd=(
            Decimal("0.01")
            if terminal_phase == "budget_exhausted"
            else Decimal(0)
        ),
    )
    repository = InMemoryArtifactRepository()
    first_gateway = TerminalGateway()
    first = make_runtime(
        first_gateway,
        terminal_plan,
        artifact_repository=repository,
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in first.run(context(token_budget=100)):
            events.append(event)
    checkpoint = await first.save_checkpoint()

    assert checkpoint.state["phase"] == terminal_phase
    assert checkpoint.id in {
        event.checkpoint.id
        for event in events
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None
    }

    resumed_gateway = FakeGateway()
    capabilities = FakeCapabilities()
    resumed = make_runtime(
        resumed_gateway,
        terminal_plan,
        capability_gateway=capabilities,
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError):
        await collect(
            resumed,
            context(checkpoint=checkpoint, token_budget=100),
        )
    assert resumed_gateway.requests == []
    assert capabilities.calls == []


async def test_cancel_during_artifact_commit_leaves_no_commit_task_and_reuses_runtime() -> None:
    class BlockingRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.block = True
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            if self.block:
                self.started.set()
                await self.release.wait()
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

    repository = BlockingRepository()
    runtime = make_runtime(
        FakeGateway(),
        one_step_plan(),
        artifact_repository=repository,
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()

    await asyncio.wait_for(runtime.cancel(), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await consumer
    pending_commits = tuple(
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and "usage_boundary" in repr(task.get_coro())
    )
    try:
        assert pending_commits == ()
    finally:
        repository.release.set()
        await asyncio.gather(*pending_commits, return_exceptions=True)

    repository.block = False
    assert (
        await collect(runtime, context(token_budget=100))
    )[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_early_close_with_full_event_queue_is_bounded_and_runtime_reusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    queue_type = asyncio.Queue
    monkeypatch.setattr(
        adapter_module.asyncio,
        "Queue",
        lambda maxsize=0: queue_type(maxsize=1),
    )
    runtime = make_runtime(FakeGateway(), one_step_plan())
    stream = cast(CrewRunStream, runtime.run(context(token_budget=100)))

    await anext(stream)
    await asyncio.wait_for(stream.aclose(), timeout=2)

    assert (
        await asyncio.wait_for(
            collect(runtime, context(token_budget=100)),
            timeout=2,
        )
    )[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_cancel_rolls_back_a_repository_write_completed_before_put_returns() -> None:
    class WrittenThenBlockingRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.block = True
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.stored: Artifact | None = None

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            self.stored = artifact
            if write_id is None:
                await self.delegate.put(tenant_id, run_id, artifact)
            else:
                await self.delegate.put(
                    tenant_id, run_id, artifact, write_id=write_id
                )
            if self.block:
                self.started.set()
                await self.release.wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

    repository = WrittenThenBlockingRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()

    await asyncio.wait_for(runtime.cancel(), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert repository.stored is not None
    reference = ArtifactReference(
        id=repository.stored.id,
        sha256=repository.stored.content_sha256,
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))
    assert (await runtime.save_checkpoint()).state["artifact_registry"] == {}
    assert not any(
        not task.done() and "usage_boundary" in repr(task.get_coro())
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )

    repository.block = False
    assert (
        await collect(runtime, context(token_budget=100))
    )[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.parametrize("rollback_mode", ("error", "timeout"))
async def test_failed_artifact_rollback_is_an_explicit_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    rollback_mode: str,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(
        adapter_module,
        "_ARTIFACT_CLEANUP_DEADLINE_SECONDS",
        0.05,
        raising=False,
    )

    class FailedRollbackRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            if write_id is None:
                await self.delegate.put(tenant_id, run_id, artifact)
            else:
                await self.delegate.put(
                    tenant_id, run_id, artifact, write_id=write_id
                )
            self.started.set()
            await asyncio.Event().wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            del tenant_id, run_id, reference, write_id
            if rollback_mode == "error":
                raise ArtifactRepositoryError("synthetic rollback failure")
            await asyncio.Event().wait()
            return False  # pragma: no cover

    repository = FailedRollbackRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()

    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await asyncio.wait_for(runtime.cancel(), timeout=2)
    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await consumer


async def test_runtime_cancel_preserves_cancelled_error_for_a_paused_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    queue_type = asyncio.Queue
    monkeypatch.setattr(
        adapter_module.asyncio,
        "Queue",
        lambda maxsize=0: queue_type(maxsize=1),
    )
    runtime = make_runtime(FakeGateway(), one_step_plan())
    stream = cast(CrewRunStream, runtime.run(context(token_budget=100)))
    while (await anext(stream)).kind is not EventKind.STEP_STARTED:
        pass

    await asyncio.wait_for(runtime.cancel(), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert not any(
        not task.done() and "CrewDispatchRuntime" in repr(task.get_coro())
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )
    assert (
        await asyncio.wait_for(
            collect(runtime, context(token_budget=100)),
            timeout=2,
        )
    )[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_rollback_cancelled_error_is_reported_as_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_DEADLINE_SECONDS", 0.05)

    class CancelledRollbackRepository:
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()

        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            await self.delegate.reserve_write(
                tenant_id, run_id, reference, write_id=write_id
            )

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )
            self.started.set()
            await asyncio.Event().wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            del tenant_id, run_id, reference, write_id
            raise asyncio.CancelledError

    repository = CancelledRollbackRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()

    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await asyncio.wait_for(runtime.cancel(), timeout=0.5)
    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await consumer


async def test_rollback_that_swallows_one_cancel_is_cancelled_again_and_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(
        adapter_module,
        "_ARTIFACT_CLEANUP_HARD_GRACE_SECONDS",
        0.05,
    )

    class SwallowOnceRepository:
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()
            self.cancel_count = 0

        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            await self.delegate.reserve_write(
                tenant_id, run_id, reference, write_id=write_id
            )

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )
            self.started.set()
            await asyncio.Event().wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            del tenant_id, run_id, reference, write_id
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancel_count += 1
                    if self.cancel_count >= 2:
                        raise

    repository = SwallowOnceRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()

    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await asyncio.wait_for(runtime.cancel(), timeout=0.5)
    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await consumer
    assert repository.cancel_count >= 2
    assert not any(
        not task.done() and "rollback_pending" in repr(task.get_coro())
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


async def test_non_cooperative_rollback_is_detached_at_hard_deadline_and_runtime_reuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(
        adapter_module,
        "_ARTIFACT_CLEANUP_HARD_GRACE_SECONDS",
        0.02,
    )

    class NonCooperativeRepository:
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.block_put = True
            self.cancel_count = 0

        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            await self.delegate.reserve_write(
                tenant_id, run_id, reference, write_id=write_id
            )

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )
            if self.block_put:
                self.started.set()
                await asyncio.Event().wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_count += 1
            return await self.delegate.abort_write(
                tenant_id, run_id, reference, write_id=write_id
            )

    repository = NonCooperativeRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()
    cancel_task = asyncio.create_task(runtime.cancel())
    done, _ = await asyncio.wait({cancel_task}, timeout=0.3)
    try:
        assert done == {cancel_task}
        with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
            cancel_task.result()
        with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
            await consumer
        detached_rollbacks = tuple(
            task for task in runtime._cleanup_tasks if not task.done()
        )
        assert len(detached_rollbacks) == 1
        repository.block_put = False
        assert (
            await asyncio.wait_for(
                collect(runtime, context(token_budget=100)),
                timeout=0.5,
            )
        )[-1].kind is EventKind.RUNTIME_COMPLETED
    finally:
        repository.release.set()
        if not cancel_task.done():
            cancel_task.cancel()
        await asyncio.gather(cancel_task, consumer, return_exceptions=True)
        await asyncio.sleep(0)
        assert not any(not task.done() for task in runtime._cleanup_tasks)


async def test_aborted_write_fence_rejects_a_put_that_finishes_after_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_TASK_CANCELLATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        adapter_module,
        "_ARTIFACT_CLEANUP_HARD_GRACE_SECONDS",
        0.05,
    )

    class LatePutRepository:
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.late_done = asyncio.Event()
            self.block_put = True
            self.cancel_count = 0
            self.stored: Artifact | None = None
            self.late_rejected = False

        async def reserve_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> None:
            await self.delegate.reserve_write(
                tenant_id, run_id, reference, write_id=write_id
            )

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            self.stored = artifact
            if self.block_put:
                self.started.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self.cancel_count += 1
            try:
                await self.delegate.put(
                    tenant_id, run_id, artifact, write_id=write_id
                )
            except ArtifactRepositoryError:
                self.late_rejected = True
                raise
            finally:
                self.late_done.set()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            return await self.delegate.abort_write(
                tenant_id, run_id, reference, write_id=write_id
            )

    repository = LatePutRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()
    cancel_task = asyncio.create_task(runtime.cancel())
    done, _ = await asyncio.wait({cancel_task}, timeout=0.3)
    try:
        assert done == {cancel_task}
        with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
            cancel_task.result()
        with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
            await consumer
        detached_commits = tuple(
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and "usage_boundary" in repr(task.get_coro())
        )
        assert len(detached_commits) == 1
        assert set(detached_commits) <= runtime._cleanup_tasks
        repository.release.set()
        await asyncio.wait_for(repository.late_done.wait(), timeout=0.3)
        assert repository.cancel_count >= 2
        assert repository.late_rejected
        assert repository.stored is not None
        reference = ArtifactReference(
            id=repository.stored.id,
            sha256=repository.stored.content_sha256,
        )
        with pytest.raises(ArtifactRepositoryError, match="unavailable"):
            await repository.get_many(TENANT_ID, RUN_ID, (reference,))
        await asyncio.sleep(0)
        assert not any(
            not task.done() and "usage_boundary" in repr(task.get_coro())
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

        repository.block_put = False
        assert (
            await asyncio.wait_for(
                collect(runtime, context(token_budget=100)),
                timeout=0.5,
            )
        )[-1].kind is EventKind.RUNTIME_COMPLETED
    finally:
        repository.release.set()
        if not cancel_task.done():
            cancel_task.cancel()
        await asyncio.gather(cancel_task, consumer, return_exceptions=True)


def test_public_cancel_deadline_has_explicit_scheduling_margin() -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")

    assert adapter_module._RUNTIME_CANCEL_SCHEDULING_MARGIN_SECONDS >= 0.5
    assert adapter_module._RUNTIME_CANCEL_TIMEOUT_SECONDS >= (
        adapter_module._TASK_CANCELLATION_GRACE_SECONDS
        + adapter_module._ARTIFACT_CLEANUP_DEADLINE_SECONDS
        + adapter_module._ARTIFACT_CLEANUP_HARD_GRACE_SECONDS
        + adapter_module._RUNTIME_CANCEL_SCHEDULING_MARGIN_SECONDS
    )


async def test_compliant_abort_gets_the_full_rollback_window_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_HARD_GRACE_SECONDS", 0.25)

    class SlowFenceRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()
            self.abort_cancel_count = 0
            self.fence_applied = 0
            self.stored: Artifact | None = None

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            self.stored = artifact
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )
            self.started.set()
            await asyncio.Event().wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                self.abort_cancel_count += 1
                raise
            self.fence_applied += 1
            return await self.delegate.abort_write(
                tenant_id, run_id, reference, write_id=write_id
            )

    repository = SlowFenceRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()
    stream = runtime._active_stream
    assert stream is not None

    await asyncio.wait_for(runtime.cancel(), timeout=0.5)
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert repository.abort_cancel_count == 0
    assert repository.fence_applied == 1
    assert repository.stored is not None
    reference = ArtifactReference(
        id=repository.stored.id,
        sha256=repository.stored.content_sha256,
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))
    assert stream._state.pending_artifact_writes == {}
    assert not runtime._cleanup_tasks


async def test_abort_is_cancelled_only_after_full_window_then_collected_in_hard_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_HARD_GRACE_SECONDS", 0.25)

    class GraceFenceRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()
            self.abort_cancelled_after: float | None = None
            self.fence_applied = 0
            self.stored: Artifact | None = None

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            self.stored = artifact
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )
            self.started.set()
            await asyncio.Event().wait()

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            started_at = asyncio.get_running_loop().time()
            try:
                await asyncio.sleep(0.06)
            except asyncio.CancelledError:
                self.abort_cancelled_after = (
                    asyncio.get_running_loop().time() - started_at
                )
            self.fence_applied += 1
            return await self.delegate.abort_write(
                tenant_id, run_id, reference, write_id=write_id
            )

    repository = GraceFenceRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()
    stream = runtime._active_stream
    assert stream is not None

    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await asyncio.wait_for(runtime.cancel(), timeout=0.5)
    with pytest.raises(RuntimeExecutionError, match="artifact rollback failed"):
        await consumer
    assert repository.abort_cancelled_after is not None
    assert repository.abort_cancelled_after >= 0.045
    assert repository.fence_applied == 1
    assert stream._state.pending_artifact_writes == {}
    assert not runtime._cleanup_tasks


async def test_64_aborts_share_one_full_rollback_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_module = importlib.import_module("agent_hub.runtime.crew.adapter")
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(adapter_module, "_ARTIFACT_CLEANUP_HARD_GRACE_SECONDS", 0.25)

    class ConcurrentAbortRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.active = 0
            self.maximum_active = 0

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

        async def abort_write(
            self,
            tenant_id: UUID,
            run_id: UUID,
            reference: ArtifactReference,
            *,
            write_id: UUID,
        ) -> bool:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return await self.delegate.abort_write(
                    tenant_id, run_id, reference, write_id=write_id
                )
            finally:
                self.active -= 1

    repository = ConcurrentAbortRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    state = adapter_module._RunState(token=adapter_module._RunToken(1))
    for index in range(64):
        stored = Artifact(
            id=uuid4(),
            type="text",
            producer="writer",
            content={"text": f"artifact-{index}"},
        )
        reference = ArtifactReference(id=stored.id, sha256=stored.content_sha256)
        write_id = uuid4()
        await repository.reserve_write(
            TENANT_ID, RUN_ID, reference, write_id=write_id
        )
        await repository.put(TENANT_ID, RUN_ID, stored, write_id=write_id)
        state.pending_artifact_writes[write_id] = reference

    started_at = asyncio.get_running_loop().time()
    cleanup_succeeded = await runtime._abort_frozen_artifact_writes(
        context(token_budget=100),
        state,
        tuple(state.pending_artifact_writes.items()),
    )
    elapsed = asyncio.get_running_loop().time() - started_at

    assert cleanup_succeeded
    assert repository.maximum_active == 64
    assert elapsed < 0.1
    assert state.pending_artifact_writes == {}
    assert not runtime._cleanup_tasks


async def test_cleanup_batch_collects_64_tasks_under_one_absolute_deadline() -> None:
    runtime = make_runtime(FakeGateway(), one_step_plan())
    cancel_counts = [0] * 64

    async def block(index: int) -> None:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_counts[index] += 1
                if cancel_counts[index] >= 2:
                    raise

    tasks = tuple(asyncio.create_task(block(index)) for index in range(64))
    await asyncio.sleep(0)
    deadline = asyncio.get_running_loop().time() + 0.2
    try:
        pending = await runtime._cancel_cleanup_tasks(
            tasks,
            deadline=deadline,
        )
        assert pending == ()
        assert all(task.done() for task in tasks)
        assert min(cancel_counts) >= 2
        assert asyncio.get_running_loop().time() <= deadline
    finally:
        for _ in range(2):
            for task in tasks:
                task.cancel()
            await asyncio.sleep(0)
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_late_put_completed_during_cancel_grace_keeps_frozen_abort_obligation() -> None:
    class CompletesOnSecondCancelRepository(DelegatingArtifactRepository):
        def __init__(self) -> None:
            self.delegate = InMemoryArtifactRepository()
            self.started = asyncio.Event()
            self.cancel_count = 0
            self.stored: Artifact | None = None

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            self.stored = artifact
            self.started.set()
            while self.cancel_count < 2:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancel_count += 1
            await self.delegate.put(
                tenant_id, run_id, artifact, write_id=write_id
            )

        async def get_many(
            self,
            tenant_id: UUID,
            run_id: UUID,
            references: tuple[ArtifactReference, ...],
        ) -> tuple[Artifact, ...]:
            return await self.delegate.get_many(tenant_id, run_id, references)

    repository = CompletesOnSecondCancelRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    consumer = asyncio.create_task(collect(runtime, context(token_budget=100)))
    await repository.started.wait()

    await asyncio.wait_for(runtime.cancel(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert repository.cancel_count >= 2
    assert repository.stored is not None
    reference = ArtifactReference(
        id=repository.stored.id,
        sha256=repository.stored.content_sha256,
    )
    with pytest.raises(ArtifactRepositoryError, match="unavailable"):
        await repository.get_many(TENANT_ID, RUN_ID, (reference,))
    assert (await runtime.save_checkpoint()).state["artifact_registry"] == {}


async def test_every_emitted_checkpoint_is_immediately_restorable_from_shared_repository() -> None:
    repository = InMemoryArtifactRepository()
    runtime = make_runtime(
        FakeGateway(), one_step_plan(), artifact_repository=repository
    )
    checkpoint_count = 0

    async for event in runtime.run(context(token_budget=100)):
        if event.kind is not EventKind.CHECKPOINT_SAVED or event.checkpoint is None:
            continue
        checkpoint_count += 1
        checkpoint = event.checkpoint
        probe = make_runtime(
            FakeGateway(), one_step_plan(), artifact_repository=repository
        )
        await probe.restore_checkpoint(checkpoint)
        try:
            await collect(
                probe,
                context(checkpoint=checkpoint, token_budget=100),
            )
        except ModelOutcomeUncertain:
            pass

    assert checkpoint_count > 1
