from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from autogen_agentchat.teams import SelectorGroupChat
from autogen_core.models import UserMessage
from opentelemetry import trace

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse, TokenUsage
from agent_hub.models.types import ToolCall as GatewayToolCall
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)


class ScriptedGateway:
    def __init__(self, replies: list[tuple[str, int, Decimal | None]]) -> None:
        self.replies = list(replies)
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text, tokens, cost = self.replies.pop(0)
        return GatewayCompletion(
            response=ModelResponse(
                text=text,
                usage=TokenUsage(
                    prompt_tokens=max(tokens - 1, 0),
                    completion_tokens=min(tokens, 1),
                    total_tokens=tokens,
                ),
            ),
            deployment_id="shared",
            logical_model=request.logical_model,
            provider_id="openai",
            provider_model="openai/test",
            cost_usd=cost,
        )


def plan(
    *,
    participants: tuple[DiscussionParticipant, ...] | None = None,
    selector_model: str = "shared",
    max_turns: int = 4,
    wall_time_seconds: float = 5.0,
    token_budget: int = 100,
    cost_budget_usd: Decimal = Decimal(1),
    consensus_votes: int = 2,
) -> DiscussionPlan:
    configured_participants = (
        participants
        if participants is not None
        else (
            DiscussionParticipant(
                id="analyst", role="Analyst", goal="Analyze facts", logical_model="shared"
            ),
            DiscussionParticipant(
                id="critic", role="Critic", goal="Check claims", logical_model="shared"
            ),
        )
    )
    return DiscussionPlan(
        participants=configured_participants,
        selector_model=selector_model,
        max_turns=max_turns,
        wall_time_seconds=wall_time_seconds,
        token_budget=token_budget,
        cost_budget_usd=cost_budget_usd,
        consensus_votes=consensus_votes,
    )


def context(*, token_budget: int = 100) -> TaskContext:
    return TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="Compare the evidence.",
        token_budget=token_budget,
    )


async def collect(runtime: AutoGenDiscussionRuntime, ctx: TaskContext) -> list[Any]:
    return [event async for event in runtime.run(ctx)]


def terminal_events(events: list[Any]) -> list[Any]:
    return [
        event
        for event in events
        if event.kind
        in {
            EventKind.RUNTIME_COMPLETED,
            EventKind.RUNTIME_FAILED,
            EventKind.RUNTIME_CANCELLED,
        }
    ]


def test_discussion_plan_rejects_invalid_participants_and_limits() -> None:
    with pytest.raises(ValueError, match="2.*8"):
        DiscussionPlan(participants=(plan().participants[0],), selector_model="shared")
    with pytest.raises(ValueError, match="unique"):
        DiscussionPlan(
            participants=(plan().participants[0], plan().participants[0]),
            selector_model="shared",
        )
    with pytest.raises(ValueError):
        plan(max_turns=0)


async def test_real_selector_group_chat_uses_gateway_for_shared_roles() -> None:
    gateway = ScriptedGateway(
        [
            ("analyst", 1, Decimal("0.01")),
            ("Facts are A.", 2, Decimal("0.01")),
            ("critic", 1, Decimal("0.01")),
            ("[COMPLETE] Facts are verified.", 2, Decimal("0.01")),
        ]
    )
    runtime = AutoGenDiscussionRuntime(gateway, plan())

    events = await collect(runtime, context())

    assert isinstance(runtime.last_team, SelectorGroupChat)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].kind is EventKind.DISCUSSION_STARTED
    assert [event.actor for event in events if event.kind is EventKind.MESSAGE_CREATED] == [
        "analyst",
        "critic",
    ]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "explicit_completion"
    assert len(terminal_events(events)) == 1
    assert len(gateway.requests) == 4
    assert {request.logical_model for request in gateway.requests} == {"shared"}
    assert all("reasoning" not in repr(request).casefold() for request in gateway.requests)


async def test_discussion_gateway_transport_failure_records_safe_diagnostic() -> None:
    class FailingGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise ModelTransportError("Authorization: Bearer sk-secret", status_code=401)

    events = await collect(AutoGenDiscussionRuntime(FailingGateway([]), plan()), context())

    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "model gateway failed: model transport failed (status=401)"


async def test_participants_can_use_different_logical_models() -> None:
    gateway = ScriptedGateway(
        [
            ("analyst", 1, Decimal("0.01")),
            ("First view", 2, Decimal("0.01")),
            ("critic", 1, Decimal("0.01")),
            ("[COMPLETE] Second view", 2, Decimal("0.01")),
        ]
    )
    multi = plan(
        participants=(
            DiscussionParticipant(
                id="analyst", role="Analyst", goal="Analyze", logical_model="model_a"
            ),
            DiscussionParticipant(
                id="critic", role="Critic", goal="Critique", logical_model="model_b"
            ),
        ),
        selector_model="selector",
    )

    await collect(AutoGenDiscussionRuntime(gateway, multi), context())

    assert [request.logical_model for request in gateway.requests] == [
        "selector",
        "model_a",
        "selector",
        "model_b",
    ]


@pytest.mark.parametrize(
    ("overrides", "replies", "expected"),
    [
        (
            {"max_turns": 1},
            [("analyst", 1, Decimal("0.01")), ("No conclusion", 1, Decimal("0.01"))],
            "max_turns",
        ),
        (
            {"token_budget": 2},
            [("analyst", 1, Decimal("0.01")), ("No conclusion", 2, Decimal("0.01"))],
            "budget_exhausted",
        ),
        (
            {"cost_budget_usd": Decimal("0.01")},
            [("analyst", 1, Decimal("0.01")), ("No conclusion", 1, Decimal("0.01"))],
            "budget_exhausted",
        ),
    ],
)
async def test_discussion_stops_with_stable_reason(
    overrides: dict[str, object],
    replies: list[tuple[str, int, Decimal | None]],
    expected: str,
) -> None:
    events = await collect(
        AutoGenDiscussionRuntime(ScriptedGateway(replies), plan(**cast(Any, overrides))),
        context(),
    )
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == expected
    assert len(terminal_events(events)) == 1


async def test_consensus_requires_distinct_participants() -> None:
    gateway = ScriptedGateway(
        [
            ("analyst", 1, Decimal(0)),
            ("[CONSENSUS] approve", 1, Decimal(0)),
            ("critic", 1, Decimal(0)),
            ("[CONSENSUS] approve", 1, Decimal(0)),
        ]
    )
    events = await collect(AutoGenDiscussionRuntime(gateway, plan()), context())
    assert events[-1].reason == "consensus"
    assert len(terminal_events(events)) == 1


async def test_unpriced_but_token_accounted_usage_does_not_block_discussion() -> None:
    gateway = ScriptedGateway(
        [
            ("analyst", 1, None),
            ("analyst reply", 1, None),
            ("critic", 1, None),
            ("critic reply", 1, None),
            ("analyst", 1, None),
            ("analyst follow-up", 1, None),
        ]
    )
    events = await collect(AutoGenDiscussionRuntime(gateway, plan()), context())
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "max_turns"
    assert len(terminal_events(events)) == 1


async def test_cancel_propagates_and_runtime_is_reusable() -> None:
    started = asyncio.Event()

    class BlockingGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    runtime = AutoGenDiscussionRuntime(BlockingGateway([]), plan())
    stream = runtime.run(context())
    first = await anext(stream)
    assert first.kind is EventKind.DISCUSSION_STARTED
    observed = [first]
    pending: asyncio.Task[Any] = asyncio.ensure_future(cast(Awaitable[Any], anext(stream)))
    await started.wait()
    await runtime.cancel()
    cancelled = await pending
    observed.append(cancelled)
    if cancelled.kind is EventKind.CHECKPOINT_SAVED:
        cancelled = await anext(stream)
        observed.append(cancelled)
    assert cancelled.kind is EventKind.RUNTIME_CANCELLED
    assert len(terminal_events(observed)) == 1
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)

    runtime = AutoGenDiscussionRuntime(
        ScriptedGateway([("analyst", 1, Decimal(0)), ("[COMPLETE] done", 1, Decimal(0))]),
        plan(),
    )
    assert (await collect(runtime, context()))[-1].reason == "explicit_completion"


async def test_wall_time_is_a_stable_single_terminal_and_cancels_gateway() -> None:
    class SlowGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__([])
            self.started = asyncio.Event()
            self.cancelled = False

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    gateway = SlowGateway()
    ctx = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="Time bounded discussion",
        timeout_seconds=0.25,
    )
    events = await collect(AutoGenDiscussionRuntime(gateway, plan()), ctx)
    assert gateway.cancelled
    assert len(terminal_events(events)) == 1
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "wall_time"


async def test_stream_backpressure_defers_framework_and_gateway_work_until_demand() -> None:
    gateway = ScriptedGateway([("analyst", 1, Decimal(0)), ("[COMPLETE] done", 1, Decimal(0))])
    runtime = AutoGenDiscussionRuntime(gateway, plan())
    stream = runtime.run(context())
    assert (await anext(stream)).kind is EventKind.DISCUSSION_STARTED
    await asyncio.sleep(0)
    assert gateway.requests == []
    remaining = [event async for event in stream]
    assert remaining[-1].kind is EventKind.RUNTIME_COMPLETED


async def test_runtime_instance_reuse_does_not_leak_transcript_or_timer_state() -> None:
    gateway = ScriptedGateway(
        [
            ("analyst", 1, Decimal(0)),
            ("[COMPLETE] first", 1, Decimal(0)),
            ("analyst", 1, Decimal(0)),
            ("[COMPLETE] second", 1, Decimal(0)),
        ]
    )
    runtime = AutoGenDiscussionRuntime(gateway, plan())
    first_context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="FIRST-RUN",
        timeout_seconds=0.2,
    )
    second_context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISCUSS,
        request="SECOND-RUN",
        timeout_seconds=0.2,
    )
    first_events = await collect(runtime, first_context)
    second_events = await collect(runtime, second_context)

    assert first_events[-1].reason == "explicit_completion"
    assert second_events[-1].reason == "explicit_completion"
    second_requests = gateway.requests[2:]
    assert second_requests
    assert all(
        "FIRST-RUN" not in str(message.content)
        for request in second_requests
        for message in request.messages
    )


async def test_concurrent_runtimes_keep_tenant_artifacts_and_prompts_isolated() -> None:
    class TenantGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            prompt = "\n".join(str(message.content) for message in request.messages)
            marker = "TENANT-A" if "TENANT-A" in prompt else "TENANT-B"
            text = "analyst" if request.logical_model == "selector" else f"[COMPLETE] {marker}"
            return GatewayCompletion(
                response=ModelResponse(
                    text=text,
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    repository = InMemoryArtifactRepository()
    gateway = TenantGateway([])
    tenant_plan = plan(selector_model="selector")
    contexts = (
        TaskContext(
            run_id=uuid4(),
            tenant_id=uuid4(),
            mode=TaskMode.DISCUSS,
            request="TENANT-A",
        ),
        TaskContext(
            run_id=uuid4(),
            tenant_id=uuid4(),
            mode=TaskMode.DISCUSS,
            request="TENANT-B",
        ),
    )
    results = await asyncio.gather(
        collect(
            AutoGenDiscussionRuntime(gateway, tenant_plan, artifact_repository=repository),
            contexts[0],
        ),
        collect(
            AutoGenDiscussionRuntime(gateway, tenant_plan, artifact_repository=repository),
            contexts[1],
        ),
    )
    for index, marker in enumerate(("TENANT-A", "TENANT-B")):
        messages = [
            event.message for event in results[index] if event.kind is EventKind.MESSAGE_CREATED
        ]
        assert messages == [f"[COMPLETE] {marker}"]
        artifact = next(
            event.artifact
            for event in results[index]
            if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
        )
        reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
        assert await repository.get_many(
            contexts[index].tenant_id, contexts[index].run_id, (reference,)
        ) == (artifact,)
        other = contexts[1 - index]
        with pytest.raises(ArtifactRepositoryError):
            await repository.get_many(other.tenant_id, other.run_id, (reference,))


async def test_autogen_runtime_uses_isolated_noop_tracing_provider() -> None:
    external_provider = trace.get_tracer_provider()
    runtime = AutoGenDiscussionRuntime(
        ScriptedGateway([("analyst", 1, Decimal(0)), ("[COMPLETE] isolated", 1, Decimal(0))]),
        plan(),
    )
    events = await collect(runtime, context())
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert trace.get_tracer_provider() is external_provider


async def test_completed_checkpoint_resumes_without_paid_calls() -> None:
    repository = InMemoryArtifactRepository()
    first_gateway = ScriptedGateway(
        [("analyst", 1, Decimal(0)), ("[COMPLETE] durable", 1, Decimal(0))]
    )
    ctx = context()
    first = AutoGenDiscussionRuntime(first_gateway, plan(), artifact_repository=repository)
    events = await collect(first, ctx)
    checkpoint = await first.save_checkpoint()
    assert any(event.kind is EventKind.ARTIFACT_CREATED for event in events)

    second_gateway = ScriptedGateway([])
    second = AutoGenDiscussionRuntime(second_gateway, plan(), artifact_repository=repository)
    await second.restore_checkpoint(checkpoint)
    resumed = TaskContext(
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        mode=ctx.mode,
        request=ctx.request,
        checkpoint=checkpoint,
    )
    resumed_events = await collect(second, resumed)
    assert second_gateway.requests == []
    assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert resumed_events[-1].reason == "explicit_completion"


async def test_autogen_tool_calls_use_only_capability_gateway() -> None:
    class ToolGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            index = len(self.requests)
            if index == 1:
                response = ModelResponse(
                    text="analyst",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            elif index == 2:
                response = ModelResponse(
                    text=None,
                    tool_calls=(
                        GatewayToolCall(
                            id="call_1", name="web.search", arguments={"query": "evidence"}
                        ),
                    ),
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            else:
                response = ModelResponse(
                    text="[COMPLETE] verified",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            return GatewayCompletion(
                response=response,
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    class Capabilities:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def is_replay_safe(self, name: str) -> bool:
            return name == "web.search"

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
            self.calls.append(
                {
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "actor": actor,
                    "name": name,
                    "arguments": arguments,
                    "idempotency_key": idempotency_key,
                }
            )
            return {"items": ("source-a",)}

    participant = DiscussionParticipant(
        id="analyst",
        role="Analyst",
        goal="Search",
        logical_model="shared",
        allowed_tools=("web.search",),
    )
    capabilities = Capabilities()
    gateway = ToolGateway([])
    runtime = AutoGenDiscussionRuntime(
        gateway,
        plan(participants=(participant, plan().participants[1])),
        capability_gateway=capabilities,
    )

    events = await collect(runtime, context())

    assert len(capabilities.calls) == 1
    assert capabilities.calls[0]["name"] == "web.search"
    assert capabilities.calls[0]["actor"] == "analyst"
    assert any(event.kind is EventKind.TOOL_STARTED for event in events)
    assert any(event.kind is EventKind.TOOL_COMPLETED for event in events)
    assert ModelCapability.TOOL_CALLING in gateway.requests[1].required_capabilities
    assert events[-1].reason == "explicit_completion"


async def test_excessive_tool_calls_are_rejected_before_capability_side_effect() -> None:
    calls = tuple(
        GatewayToolCall(id=f"call_{index}", name="web.search", arguments={"q": index})
        for index in range(17)
    )

    class ToolFloodGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            response = (
                ModelResponse(
                    text="analyst",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
                if len(self.requests) == 1
                else ModelResponse(
                    text=None,
                    tool_calls=calls,
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            )
            return GatewayCompletion(
                response=response,
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    class NoCapabilities:
        calls = 0

        def is_replay_safe(self, name: str) -> bool:
            return True

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
            self.calls += 1
            return {}

    capabilities = NoCapabilities()
    participant = DiscussionParticipant(
        id="analyst",
        role="Analyst",
        goal="Search",
        logical_model="shared",
        allowed_tools=("web.search",),
    )
    events = await collect(
        AutoGenDiscussionRuntime(
            ToolFloodGateway([]),
            plan(participants=(participant, plan().participants[1])),
            capability_gateway=capabilities,
        ),
        context(),
    )
    assert capabilities.calls == 0
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "discussion_failed"
    assert len(terminal_events(events)) == 1


@pytest.mark.parametrize("malformation", ["mixed", "empty", "unknown_tool"])
async def test_malformed_provider_output_fails_closed_with_one_terminal(
    malformation: str,
) -> None:
    class MalformedGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            if len(self.requests) == 1:
                response = ModelResponse(
                    text="analyst",
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            else:
                calls = (
                    GatewayToolCall(
                        id="call_1",
                        name="other.tool" if malformation == "unknown_tool" else "web.search",
                        arguments={},
                    ),
                )
                response = ModelResponse(
                    text="invalid" if malformation == "mixed" else None,
                    tool_calls=() if malformation == "empty" else calls,
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            return GatewayCompletion(
                response=response,
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    class NoCapabilities:
        calls = 0

        def is_replay_safe(self, name: str) -> bool:
            return True

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
            self.calls += 1
            return {}

    capabilities = NoCapabilities()
    participant = DiscussionParticipant(
        id="analyst",
        role="Analyst",
        goal="Search",
        logical_model="shared",
        allowed_tools=("web.search",),
    )
    events = await collect(
        AutoGenDiscussionRuntime(
            MalformedGateway([]),
            plan(participants=(participant, plan().participants[1])),
            capability_gateway=capabilities,
        ),
        context(),
    )
    assert capabilities.calls == 0
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "discussion_failed"
    assert len(terminal_events(events)) == 1


async def test_partial_checkpoint_resumes_from_explicit_transcript_without_repeating_calls() -> (
    None
):
    repository = InMemoryArtifactRepository()
    ctx = context()
    first_gateway = ScriptedGateway(
        [("analyst", 1, Decimal(0)), ("First explicit view", 1, Decimal(0))]
    )
    first = AutoGenDiscussionRuntime(first_gateway, plan(), artifact_repository=repository)
    stream = first.run(ctx)
    checkpoint = None
    while checkpoint is None:
        event = await anext(stream)
        if event.kind is EventKind.CHECKPOINT_SAVED:
            checkpoint = event.checkpoint
    await cast(Any, stream).aclose()
    assert checkpoint is not None
    # SelectorGroupChat may prefetch the next selector turn before the consumer
    # observes our checkpoint.  The durable boundary is the explicit message,
    # not the framework's internal scheduling point.
    assert len(first_gateway.requests) >= 2

    second_gateway = ScriptedGateway(
        [("critic", 1, Decimal(0)), ("[COMPLETE] Final view", 1, Decimal(0))]
    )
    second = AutoGenDiscussionRuntime(second_gateway, plan(), artifact_repository=repository)
    await second.restore_checkpoint(checkpoint)
    resumed = TaskContext(
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        mode=ctx.mode,
        request=ctx.request,
        checkpoint=checkpoint,
    )
    events = await collect(second, resumed)

    assert len(second_gateway.requests) == 2
    assert any(
        "First explicit view" in str(message.content)
        for message in second_gateway.requests[0].messages
    )
    assert events[-1].reason == "explicit_completion"


async def test_checkpoint_artifact_hash_and_tenant_are_verified_before_resume() -> None:
    repository = InMemoryArtifactRepository()
    ctx = context()
    first = AutoGenDiscussionRuntime(
        ScriptedGateway([("analyst", 1, Decimal(0)), ("[COMPLETE] done", 1, Decimal(0))]),
        plan(),
        artifact_repository=repository,
    )
    await collect(first, ctx)
    checkpoint = await first.save_checkpoint()
    tampered = checkpoint.to_payload()
    state = cast(dict[str, object], tampered["state"])
    registry = cast(dict[str, str], state["artifact_registry"])
    artifact_id = next(iter(registry))
    registry[artifact_id] = "0" * 64
    tampered["state_sha256"] = ""
    bad = RuntimeCheckpoint.from_payload(tampered)

    second = AutoGenDiscussionRuntime(ScriptedGateway([]), plan(), artifact_repository=repository)
    await second.restore_checkpoint(bad)
    bad_context = TaskContext(
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        mode=ctx.mode,
        request=ctx.request,
        checkpoint=bad,
    )
    events = await collect(second, bad_context)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == "runtime checkpoint artifacts are unavailable"


async def test_exact_tool_call_limit_is_accepted_by_gateway_client() -> None:
    calls = tuple(
        GatewayToolCall(id=f"call_{index}", name="web.search", arguments={"q": index})
        for index in range(16)
    )

    class ExactGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            return GatewayCompletion(
                response=ModelResponse(
                    text=None,
                    tool_calls=calls,
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
                deployment_id="shared",
                logical_model=request.logical_model,
                provider_id="openai",
                provider_model="openai/test",
                cost_usd=Decimal(0),
            )

    from agent_hub.runtime.autogen.adapter import GatewayChatCompletionClient
    from agent_hub.runtime.autogen.termination import DiscussionUsage

    client = GatewayChatCompletionClient(
        ExactGateway([]), "shared", DiscussionUsage(), supports_tool_calls=True
    )
    tool_schema = {
        "name": "web.search",
        "description": "search",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        "strict": False,
    }
    result = await client.create(
        [UserMessage(content="search", source="user")],
        tools=[cast(Any, tool_schema)],
    )
    assert isinstance(result.content, list)
    assert len(result.content) == 16


async def test_cumulative_message_limit_rejects_before_gateway_side_effect() -> None:
    class NeverGateway(ScriptedGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise AssertionError("message preflight must run before the gateway")

    from agent_hub.runtime.autogen.adapter import GatewayChatCompletionClient
    from agent_hub.runtime.autogen.termination import DiscussionUsage

    gateway = NeverGateway([])
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())
    messages = [UserMessage(content=f"message-{index}", source="user") for index in range(129)]
    with pytest.raises(RuntimeExecutionError, match="history"):
        await client.create(messages)
    assert gateway.requests == []


async def test_repository_put_failure_aborts_reserved_artifact_write() -> None:
    class FailingRepository(InMemoryArtifactRepository):
        def __init__(self) -> None:
            super().__init__()
            self.operations: list[str] = []

        async def reserve_write(self, *args: object, **kwargs: object) -> None:
            self.operations.append("reserve")
            await super().reserve_write(*cast(Any, args), **cast(Any, kwargs))

        async def put(self, *args: object, **kwargs: object) -> None:
            artifact = cast(Artifact, args[2])
            self.operations.append("put")
            if artifact.type != "text":
                await super().put(*cast(Any, args), **cast(Any, kwargs))
                return
            raise ArtifactRepositoryError("injected")

        async def abort_write(self, *args: object, **kwargs: object) -> bool:
            self.operations.append("abort")
            return await super().abort_write(*cast(Any, args), **cast(Any, kwargs))

    repository = FailingRepository()
    runtime = AutoGenDiscussionRuntime(
        ScriptedGateway([("analyst", 1, Decimal(0)), ("message", 1, Decimal(0))]),
        plan(max_turns=2),
        artifact_repository=repository,
    )
    events = await collect(runtime, context())
    assert repository.operations == [
        "reserve",
        "put",
        "reserve",
        "put",
        "reserve",
        "put",
        "abort",
    ]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(terminal_events(events)) == 1
    assert runtime._pending_artifact_writes == {}


async def test_post_write_pre_return_cancel_aborts_artifact_and_clears_pending() -> None:
    class BlockingRepository(InMemoryArtifactRepository):
        def __init__(self) -> None:
            super().__init__()
            self.written = asyncio.Event()
            self.release = asyncio.Event()
            self.artifact: Artifact | None = None

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            if artifact.type != "text":
                await super().put(tenant_id, run_id, artifact, write_id=write_id)
                return
            self.artifact = artifact
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            self.written.set()
            await self.release.wait()

    repository = BlockingRepository()
    runtime = AutoGenDiscussionRuntime(
        ScriptedGateway([("analyst", 1, Decimal(0)), ("message", 1, Decimal(0))]),
        plan(max_turns=2),
        artifact_repository=repository,
    )
    ctx = context()
    stream = runtime.run(ctx)
    assert (await anext(stream)).kind is EventKind.DISCUSSION_STARTED
    pending: asyncio.Task[Any] = asyncio.ensure_future(cast(Awaitable[Any], anext(stream)))
    await repository.written.wait()
    await runtime.cancel()
    cancelled = await pending
    if cancelled.kind is EventKind.CHECKPOINT_SAVED:
        cancelled = await anext(stream)
    assert cancelled.kind is EventKind.RUNTIME_CANCELLED
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    assert repository.artifact is not None
    reference = ArtifactReference(
        id=repository.artifact.id, sha256=repository.artifact.content_sha256
    )
    with pytest.raises(ArtifactRepositoryError):
        await repository.get_many(ctx.tenant_id, ctx.run_id, (reference,))
    assert runtime._pending_artifact_writes == {}


async def test_late_put_that_swallows_cancel_remains_fenced_and_cleanup_finishes() -> None:
    class LateRepository(InMemoryArtifactRepository):
        def __init__(self) -> None:
            super().__init__()
            self.written = asyncio.Event()
            self.swallowed = asyncio.Event()
            self.release = asyncio.Event()
            self.artifact: Artifact | None = None

        async def put(
            self,
            tenant_id: UUID,
            run_id: UUID,
            artifact: Artifact,
            *,
            write_id: UUID | None = None,
        ) -> None:
            if artifact.type != "text":
                await super().put(tenant_id, run_id, artifact, write_id=write_id)
                return
            self.artifact = artifact
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            self.written.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.swallowed.set()
                await self.release.wait()

    repository = LateRepository()
    runtime = AutoGenDiscussionRuntime(
        ScriptedGateway([("analyst", 1, Decimal(0)), ("message", 1, Decimal(0))]),
        plan(max_turns=2),
        artifact_repository=repository,
    )
    ctx = context()
    stream = runtime.run(ctx)
    await anext(stream)
    pending: asyncio.Task[Any] = asyncio.ensure_future(cast(Awaitable[Any], anext(stream)))
    await repository.written.wait()
    await runtime.cancel()
    await repository.swallowed.wait()
    repository.release.set()
    cancelled = await pending
    if cancelled.kind is EventKind.CHECKPOINT_SAVED:
        cancelled = await anext(stream)
    assert cancelled.kind is EventKind.RUNTIME_CANCELLED
    with pytest.raises(asyncio.CancelledError):
        await anext(stream)
    await asyncio.sleep(0)
    assert repository.artifact is not None
    reference = ArtifactReference(
        id=repository.artifact.id, sha256=repository.artifact.content_sha256
    )
    with pytest.raises(ArtifactRepositoryError):
        await repository.get_many(ctx.tenant_id, ctx.run_id, (reference,))
    assert runtime._pending_artifact_writes == {}
    assert not runtime._cleanup_tasks
