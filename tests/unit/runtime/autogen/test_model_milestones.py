"""Named API tool choices remain real, billed, durable model requests."""

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest
from autogen_core.models import UserMessage

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import (
    GatewayChatCompletionClient,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability, _tool


class Gateway:
    def __init__(self, *, refuses: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.refuses = refuses
        self.before_followup: Callable[[], None] = lambda: None

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.requests:
            self.before_followup()
        self.requests.append(request)
        calls = ((ToolCall(id="real_provider_call", name=request.required_tool, arguments={}),)
                 if request.required_tool is not None and not self.refuses else ())
        return GatewayCompletion(response=ModelResponse(
            text="[COMPLETE] existing project is done" if not calls else None, tool_calls=calls,
            usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        ), deployment_id="shared", logical_model=request.logical_model, provider_id="test",
            provider_model="test/model", cost_usd=Decimal("0.01"))


async def test_prerequisite_is_exact_named_tool_choice_and_only_one_approved_schema() -> None:
    gateway, durable, usage = Gateway(), _durability(), DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable,
        engineering_action=lambda proposed: "project.inspect", engineering_budget=lambda: (100, 5.0))
    result = await client.create([UserMessage(content="build", source="user")],
                                tools=[_tool("project.inspect"), _tool("workspace.write_text")])
    request = gateway.requests[0]
    assert request.required_tool == "project_inspect"
    assert [tool.name for tool in request.tools] == ["project_inspect"]
    assert isinstance(result.content, list) and result.content[0].name == "project.inspect"
    assert usage.tokens == 5 and len(durable.model_entries) == 1


async def test_real_text_is_persisted_and_charged_before_one_named_followup_then_replay() -> None:
    gateway, durable, usage = Gateway(), _durability(), DiscussionUsage()

    def check_first_real_completion() -> None:
        assert len(durable.model_entries) == 2
        assert durable.model_entries[0]["status"] == "succeeded"
        assert durable.model_entries[1]["status"] == "running"
        assert durable.artifacts[0].content["text"] == "[COMPLETE] existing project is done"
        assert usage.tokens == 5

    gateway.before_followup = check_first_real_completion
    action = lambda proposed: "project.verify" if proposed else None
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable,
        engineering_action=action, engineering_budget=lambda: (100 - usage.tokens, 5.0))
    messages, tools = [UserMessage(content="build", source="user")], [_tool("project.verify")]
    result = await client.create(messages, tools=tools)
    assert [request.required_tool for request in gateway.requests] == [None, "project_verify"]
    assert len(durable.model_entries) == 2 and usage.tokens == 10
    assert result.usage.prompt_tokens + result.usage.completion_tokens == 10
    assert gateway.requests[1].max_output_tokens <= 95
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    replayed = GatewayChatCompletionClient(gateway, "shared", usage, durability=restored,
        engineering_action=action, engineering_budget=lambda: (90, 5.0))
    resumed = await replayed.create(messages, tools=tools)
    assert resumed.content == result.content
    assert len(gateway.requests) == 2 and usage.tokens == 10


async def test_provider_refusal_is_recorded_once_without_blind_retry_or_fake_call() -> None:
    gateway, durable, usage = Gateway(refuses=True), _durability(), DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable,
        engineering_action=lambda proposed: "project.inspect", engineering_budget=lambda: (100, 5.0))
    with pytest.raises(RuntimeExecutionError, match="required engineering tool"):
        await client.create([UserMessage(content="build", source="user")], tools=[_tool("project.inspect")])
    assert len(gateway.requests) == 1 and len(durable.model_entries) == 1
    assert durable.model_entries[0]["status"] == "succeeded" and usage.tokens == 5


@pytest.mark.parametrize("budget", [(0, 5.0), (100, 0.0)])
async def test_no_extra_model_request_when_existing_budget_is_exhausted(budget: tuple[int, float]) -> None:
    gateway = Gateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        engineering_action=lambda proposed: "project.inspect", engineering_budget=lambda: budget)
    with pytest.raises(RuntimeExecutionError, match="engineering action budget"):
        await client.create([UserMessage(content="build", source="user")], tools=[_tool("project.inspect")])
    assert not gateway.requests


async def test_required_action_cannot_add_unapproved_tool() -> None:
    gateway = Gateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        engineering_action=lambda proposed: "project.verify", engineering_budget=lambda: (100, 5.0))
    with pytest.raises(RuntimeExecutionError, match="unavailable"):
        await client.create([UserMessage(content="build", source="user")], tools=[_tool("project.inspect")])
    assert not gateway.requests


async def test_real_completion_is_retained_when_no_budget_remains_for_followup() -> None:
    gateway, durable, usage = Gateway(), _durability(), DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable,
        engineering_action=lambda proposed: "project.verify" if proposed else None,
        engineering_budget=lambda: (5 - usage.tokens, 5.0))
    with pytest.raises(RuntimeExecutionError, match="engineering action budget"):
        await client.create([UserMessage(content="build", source="user")], tools=[_tool("project.verify")])
    assert len(gateway.requests) == 1 and usage.tokens == 5
    assert durable.model_entries[0]["status"] == "succeeded"


@pytest.mark.parametrize("status", ["running", "UNKNOWN"])
async def test_unknown_forced_model_is_never_replayed(status: str) -> None:
    gateway, durable = Gateway(), _durability()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=durable,
        engineering_action=lambda proposed: "project.inspect", engineering_budget=lambda: (100, 5.0))
    messages, tools = [UserMessage(content="build", source="user")], [_tool("project.inspect")]
    await client.create(messages, tools=tools)
    durable.model_entries[0]["status"] = status
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=restored,
        engineering_action=lambda proposed: "project.inspect", engineering_budget=lambda: (100, 5.0))
    with pytest.raises(ModelOutcomeUncertain if status == "running" else RuntimeExecutionError):
        await resumed.create(messages, tools=tools)
    assert len(gateway.requests) == 1


def test_required_name_binds_hash_without_changing_none_legacy_hash() -> None:
    from agent_hub.models.types import ModelCapability, ToolDefinition

    request = ModelRequest(logical_model="shared", messages=(), required_capabilities=frozenset({ModelCapability.TOOL_CALLING}), tools=(ToolDefinition(
        name="project_inspect", description="Inspect", parameters={"type": "object"}),))
    assert _DiscussionDurability.request_hash(request) == _DiscussionDurability.request_hash(
        replace(request, required_tool=None))
    assert _DiscussionDurability.request_hash(request) != _DiscussionDurability.request_hash(
        replace(request, required_tool="project_inspect"))
