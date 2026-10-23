"""Configured Discuss cold recovery; transport/capacity/tools are offline fixtures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime import defaults
from agent_hub.runtime.artifacts import ArtifactRepository, InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeCapabilityAvailability,
    FakeConfigService,
    FakeSecretService,
    ImmediateCapacity,
)


def discussion_context() -> TaskContext:
    return TaskContext(
        run_id=uuid4(), tenant_id=TENANT_ID, actor_id=uuid4(), actor_role=Role.OPERATOR,
        execution_token=uuid4(), mode=TaskMode.DISCUSS, request="Compare two explicit views.",
        token_budget=4096, timeout_seconds=30,
        routing_decision={"selected_agent_ids": ("analyst", "critic"), "main_agent_model": "main"},
    )


def configured_document() -> dict[str, object]:
    return {
        "models": {"main": {"deployments": [{
            "provider": "openai", "model": "offline-test", "api_base": "http://127.0.0.1:1/v1",
            "credential_ref": "secret://offline", "quota_scope_id": "offline", "max_concurrency": 2,
            "target_utilization": 0.8, "reserved_slots": 0,
            "capabilities": ["text", "tool_calling", "structured_output"],
        }]}},
        "agents": [
            {"id": "analyst", "role": "Analyst", "prompt": "Compare facts.",
             "model": "main", "skills": ["calculator"]},
            {"id": "critic", "role": "Critic", "prompt": "Check claims.",
             "model": "main", "skills": []},
        ],
    }


class OfflineCapacity(ImmediateCapacity):
    def __init__(self, redis_client: object, *, deployments: Sequence[Deployment],
                 fingerprint_resolver: object) -> None:
        del redis_client, fingerprint_resolver
        super().__init__(tuple(deployments))


def response(text: str | None, *, tools: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=tools,
                         usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2))


class OfflineTransport:
    def __init__(self, replies: list[ModelResponse]) -> None:
        self.replies = list(replies)
        self.requests: list[ModelRequest] = []
        self.completed: list[ModelRequest] = []

    async def complete(self, deployment: Deployment, request: ModelRequest,
                       api_key: str) -> ModelResponse:
        del deployment, api_key
        self.requests.append(request)
        if not self.replies:
            raise ModelTransportError("offline fixture stopped after durable message", status_code=400)
        self.completed.append(request)
        return self.replies.pop(0)


class OfflineHarness:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, HarnessToolCallRequest, UUID | None, Role | None]] = []

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        self.calls.append((tenant_id, request, user_id, role))
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status="succeeded", payload={
                                         "result": 2, "summary": "Persisted calculator evidence.",
                                     })


def configured_factory(repository: ArtifactRepository | None, transport: OfflineTransport,
                       harness: OfflineHarness) -> defaults.ConfigBackedDiscussionRuntime:
    registry = defaults.configured_runtime_registry(
        config_service=cast(Any, FakeConfigService(configured_document())),
        secret_service=cast(Any, FakeSecretService()), redis_client=object(),
        transport=transport, capability_gateway=cast(Any, FakeCapabilityAvailability({"calculator"})),
        harness_tool_gateway=harness, artifact_repository=repository,
    )
    factory = registry.get(TaskMode.DISCUSS)
    assert isinstance(factory, defaults.ConfigBackedDiscussionRuntime)
    return factory


async def exercise_discuss_repository_recovery(
    first_repository: ArtifactRepository, fresh_repository: ArtifactRepository, *, context: TaskContext,
) -> None:
    """Parent native test can inject distinct PG repositories sharing only the private database."""
    original = context.to_payload()
    first_transport = OfflineTransport([
        response("analyst"),
        response(None, tools=(ToolCall(id="offline-calculator", name="calculator",
                                     arguments={"expression": "1 + 1"}),)),
    ])
    first_harness = OfflineHarness()
    with patch.object(defaults, "CapacityPool", OfflineCapacity):
        first = configured_factory(first_repository, first_transport, first_harness)
        first_events = [event async for event in first.run(context)]
        assert first_events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert first_events[-1].reason == "partial_discussion_after_model_failure"
        saved = next((event.checkpoint for event in first_events if event.checkpoint is not None
                      and cast(Mapping[str, JsonValue], event.checkpoint.state["discussion_control"])["turns"] == 1), None)
        assert saved is not None
        frozen = saved.to_payload()
        assert saved.mode is TaskMode.DISCUSS and saved.run_id == context.run_id
        assert saved.tenant_id == context.tenant_id and saved.state["terminal"] is False
        assert saved.state["artifact_registry"]
        assert len(first_transport.completed) == 2
        assert len(first_harness.calls) == 1
        tenant, request, actor, role = first_harness.calls[0]
        assert (tenant, request.run_id, request.actor, actor, role) == (
            context.tenant_id, context.run_id, "analyst", context.actor_id, context.actor_role,
        )
        assert request.tool_name == "calculator"
        before_usage = cast(Mapping[str, JsonValue], saved.state["usage"])
        assert before_usage["tokens"] == 4

        resumed_context = context.model_copy(update={"checkpoint": saved, "execution_token": uuid4()})
        fresh_transport = OfflineTransport([response("critic"), response("[COMPLETE] Final verified view.")])
        fresh_harness = OfflineHarness()
        fresh = configured_factory(fresh_repository, fresh_transport, fresh_harness)
        await fresh.restore_checkpoint(saved)
        events = [event async for event in fresh.run(resumed_context)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
        assert events[-1].reason == "explicit_completion"
        assert len(fresh_transport.completed) == 2
        assert not fresh_harness.calls
        assert any("Persisted calculator evidence" in str(message.content)
                   for message in fresh_transport.requests[0].messages)
        assert all(request.logical_model == "main" for request in fresh_transport.requests)
        terminal = next(event.checkpoint for event in reversed(events) if event.checkpoint is not None)
        after_usage = cast(Mapping[str, JsonValue], terminal.state["usage"])
        assert after_usage["tokens"] == 8
        assert after_usage["cost_usd"] == before_usage["cost_usd"]
        assert 0 < cast(float, terminal.state["remaining_timeout_seconds"]) <= cast(
            float, saved.state["remaining_timeout_seconds"],
        )
        assert saved.to_payload() == frozen and context.to_payload() == original


async def test_configured_discuss_fresh_factory_recovers_without_model_or_tool_replay() -> None:
    repository = InMemoryArtifactRepository()
    await exercise_discuss_repository_recovery(repository, repository, context=discussion_context())


async def test_registry_passes_shared_repository_to_real_discuss_child() -> None:
    repository = InMemoryArtifactRepository()
    with patch.object(defaults, "CapacityPool", OfflineCapacity):
        factory = configured_factory(repository, OfflineTransport([]), OfflineHarness())
        planned = await factory._runtime_for(discussion_context())
    assert isinstance(planned, defaults._PlannedRuntime)
    assert isinstance(planned._child, AutoGenDiscussionRuntime)
    assert planned._child._repository is repository


async def test_discuss_repository_none_keeps_constructor_compatible() -> None:
    factory = defaults.ConfigBackedDiscussionRuntime(
        config_service=cast(Any, FakeConfigService(configured_document())),
        secret_service=cast(Any, FakeSecretService()), capacity_factory=cast(Any, None),
    )
    assert factory.mode is TaskMode.DISCUSS
