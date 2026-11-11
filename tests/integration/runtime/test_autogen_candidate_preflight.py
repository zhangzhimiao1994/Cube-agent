"""Real client/runtime/gateway preflight failures must survive framework wrapping."""
from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from autogen_core.models import UserMessage

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import ModelCapability, ModelRequest
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    GatewayChatCompletionClient,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, TaskContext
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
from tests.unit.models.test_gateway import CapacityStub, SecretStub, TransportStub, deployment


def _durability(context: TaskContext) -> _DiscussionDurability:
    async def publish(*args: object) -> None:
        raise AssertionError("preflight must not start a model ledger entry")

    async def store(artifact: Artifact) -> UUID:
        raise AssertionError("preflight must not publish a model result")

    async def abort(write_id: UUID) -> bool:
        raise AssertionError("preflight must not reserve an artifact")

    return _DiscussionDurability(
        publish, store, abort, lambda write_id: None,
        run_id=context.run_id, tenant_id=context.tenant_id,
    )


def _client(gateway: ModelGateway) -> tuple[GatewayChatCompletionClient, _DiscussionDurability, str]:
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISCUSS,
        request="PRIVATE_USER_PROMPT must not enter diagnostics",
    )
    runtime = AutoGenDiscussionRuntime(
        gateway,
        DiscussionPlan(
            participants=(
                DiscussionParticipant(id="engineer", role="Engineer", goal="Implement", logical_model="qwen"),
                DiscussionParticipant(id="reviewer", role="Reviewer", goal="Verify", logical_model="qwen"),
            ),
            selector_model="qwen",
        ),
    )
    durable = _durability(context)

    def project(request: ModelRequest, names: Any, positions: tuple[int, ...], actor: str | None) -> ModelRequest:
        return runtime._fit_handoff_request(
            request, context, tool_names=names, durability=durable,
            owned_task_positions=positions, actor=actor,
        )

    client = GatewayChatCompletionClient(
        gateway, "qwen", DiscussionUsage(), supports_tool_calls=True,
        durability=durable, input_projection=project, actor="engineer",
    )
    return client, durable, runtime._task_text(context)


TOOL = {
    "name": "workspace.read_text",
    "description": "Read the authorized project input",
    "parameters": {"type": "object", "properties": {}},
    "strict": False,
}


@pytest.mark.parametrize("alias", ["qwen", "reasoning"])
async def test_real_autogen_failure_event_keeps_candidate_diagnostic_and_roundtrips(alias: str) -> None:
    capacity = CapacityStub([])
    secrets = SecretStub(capacity.events)
    transport = TransportStub(capacity.events)
    gateway = ModelGateway(
        ModelRegistry([deployment("deepseek_text", "deepseek")]),
        capacity, secrets, transport,
    )
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISCUSS,
        request="Compare the permitted project evidence.", token_budget=1000,
    )
    runtime = AutoGenDiscussionRuntime(
        gateway,
        DiscussionPlan(
            participants=(
                DiscussionParticipant(id="engineer", role="Engineer", goal="Implement", logical_model=alias),
                DiscussionParticipant(id="reviewer", role="Reviewer", goal="Verify", logical_model=alias),
            ),
            selector_model=alias, wall_time_seconds=5, token_budget=1000,
        ),
    )
    events = [event async for event in runtime.run(context)]
    failure = events[-1]
    assert failure.kind is EventKind.RUNTIME_FAILED
    assert failure.payload["error_code"] == "model.no_capable_deployment"
    assert failure.payload["model_preflight_logical_model"] == alias
    assert failure.payload["model_preflight_required_capabilities"] == "text"
    assert RunEvent.model_validate_json(failure.model_dump_json()).payload == failure.payload
    assert transport.calls == [] and secrets.references == [] and capacity.events == []


async def test_client_records_registry_preflight_before_autogen_wraps() -> None:
    # Real registry has qwen TEXT but cannot fulfill the real client's tool request.
    capacity = CapacityStub([])
    secrets = SecretStub(capacity.events)
    transport = TransportStub(capacity.events)
    gateway = ModelGateway(
        ModelRegistry([deployment("qwen_text", "qwen", capabilities=frozenset({ModelCapability.TEXT}))]),
        capacity, secrets, transport,
    )
    client, durable, task = _client(gateway)

    with pytest.raises(NoCapableDeployment):
        await client.create([UserMessage(content=task, source="user")], tools=[cast(Any, TOOL)])

    assert durable.failure_reason == "model gateway failed: no capable deployment"
    assert durable.failure_payload == {
        "model_preflight_error_type": "NoCapableDeployment",
        "model_preflight_error_code": "model.no_capable_deployment",
        "model_preflight_phase": "candidate_selection",
        "model_preflight_logical_model": "qwen",
        "model_preflight_required_capabilities": "text, tool_calling",
    }
    assert runtime_failure_diagnostic_from_reason(durable.failure_reason)["error_code"] == "model.no_capable_deployment"
    assert durable.model_entries == []
    assert transport.calls == [] and secrets.references == [] and capacity.events == []
    assert "PRIVATE_USER_PROMPT" not in str(durable.failure_payload)
    assert client.total_usage().prompt_tokens == client.total_usage().completion_tokens == 0


@pytest.mark.parametrize("typed", [True, False])
async def test_client_does_not_leak_or_misclassify_preflight_exception_text(typed: bool) -> None:
    error = (NoCapableDeployment if typed else ValueError)("password=PRIVATE_PROVIDER_SECRET prompt=PRIVATE")

    class BrokenLookupGateway:
        def context_window_tokens(self, request: ModelRequest) -> None:
            raise error

        async def complete_with_context(self, request: ModelRequest) -> None:
            raise AssertionError("preflight rejection must not enter a paid call")

    client, durable, task = _client(cast(ModelGateway, BrokenLookupGateway()))
    with pytest.raises(type(error)) as raised:
        await client.create([UserMessage(content=task, source="user")], tools=[cast(Any, TOOL)])
    assert raised.value is error
    assert "PRIVATE" not in str(durable.failure_payload)
    if typed:
        assert durable.failure_payload["model_preflight_error_type"] == "NoCapableDeployment"
        assert durable.failure_payload["model_preflight_logical_model"] == "qwen"
    else:
        assert durable.failure_reason is None and durable.failure_payload == {}
    assert durable.model_entries == []
