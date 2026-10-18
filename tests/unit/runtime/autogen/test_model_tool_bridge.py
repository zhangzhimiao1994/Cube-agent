"""AutoGen model requests expose only approved tools and accept mixed replies."""

from dataclasses import replace
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import UserMessage
from autogen_core.tools import FunctionTool, Tool, ToolSchema

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse, TokenUsage
from agent_hub.models.types import ToolCall as GatewayToolCall
from agent_hub.runtime.autogen.adapter import (
    GatewayChatCompletionClient,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    UnaccountedUsage,
    _description_with_failure_codes,
    _DiscussionDurability,
    _LedgerEntry,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact


class _Gateway:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=self.response,
            deployment_id="shared",
            logical_model=request.logical_model,
            provider_id="test",
            provider_model="test/model",
            cost_usd=Decimal("0.003"),
        )


def _response(text: str | None = None, *, name: str = "web_search") -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=(GatewayToolCall(id="call_1", name=name, arguments={"query": "facts"}),),
        usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
    )


def _tool(name: str = "web.search") -> ToolSchema:
    return {
        "name": name,
        "description": "Search approved sources",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }


async def test_schema_reaches_gateway_and_provider_alias_maps_back_to_framework() -> None:
    gateway = _Gateway(_response())
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())

    result = await client.create([UserMessage(content="search", source="user")], tools=[_tool()])

    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert len(request.tools) == 1
    assert request.tools[0].name == "web_search"
    assert request.tools[0].description == "Search approved sources"
    assert request.tools[0].parameters == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ("query",),
        "additionalProperties": False,
    }
    assert request.required_capabilities == {ModelCapability.TEXT, ModelCapability.TOOL_CALLING}
    assert result.content == [FunctionCall(id="call_1", name="web.search", arguments='{"query":"facts"}')]


@pytest.mark.parametrize("text", [None, "", "I will search the approved sources."])
async def test_tool_calls_with_optional_text_preserve_usage_and_text(text: str | None) -> None:
    gateway = _Gateway(_response(text, name="web.search"))
    usage = DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage)

    result = await client.create([UserMessage(content="search", source="user")], tools=[_tool()])

    assert result.finish_reason == "function_calls"
    assert result.content == [FunctionCall(id="call_1", name="web.search", arguments='{"query":"facts"}')]
    assert result.thought == (text or None)
    assert result.usage.prompt_tokens == 2
    assert result.usage.completion_tokens == 3
    assert client.actual_usage() == result.usage
    assert client.total_usage() == result.usage
    assert usage.tokens == 5
    assert usage.cost_usd == Decimal("0.003")
    assert len(gateway.requests) == 1


async def test_function_tool_schema_is_forwarded_without_execution() -> None:
    def search(query: str) -> str:
        raise AssertionError("the model bridge must not execute tools")

    tool = FunctionTool(search, "Search approved sources", name="web.search")
    gateway = _Gateway(_response())
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())

    result = await client.create([UserMessage(content="search", source="user")], tools=[tool])

    assert gateway.requests[0].tools[0].parameters["required"] == ("query",)
    assert isinstance(result.content, list)
    assert result.content[0].name == "web.search"


async def test_none_choice_removes_tools_and_rejects_unexpected_calls() -> None:
    gateway = _Gateway(_response())
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())

    with pytest.raises(RuntimeExecutionError, match="unavailable capability"):
        await client.create(
            [UserMessage(content="answer only", source="user")], tools=[_tool()], tool_choice="none"
        )

    assert gateway.requests[0].tools == ()
    assert gateway.requests[0].required_capabilities == {ModelCapability.TEXT}


@pytest.mark.parametrize("choice", ["required", FunctionTool(lambda: None, "A specific tool", name="specific")])
async def test_unrepresentable_choice_fails_before_model_side_effect(
    choice: Tool | Literal["required"],
) -> None:
    gateway = _Gateway(_response())
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())

    with pytest.raises(RuntimeExecutionError, match="unsupported AutoGen tool choice"):
        await client.create([UserMessage(content="search", source="user")], tools=[_tool()], tool_choice=choice)

    assert gateway.requests == []


async def test_alias_collision_is_rejected_before_model_side_effect() -> None:
    gateway = _Gateway(_response())
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())

    with pytest.raises(RuntimeExecutionError, match="tool names are ambiguous"):
        await client.create(
            [UserMessage(content="search", source="user")],
            tools=[_tool(), _tool("web_search")],
        )

    assert gateway.requests == []


async def test_long_approved_description_with_failure_codes_reaches_model() -> None:
    tool = _tool()
    tool['description'] = _description_with_failure_codes('x' * 1000, ('execution_failed', 'verification_required'))
    gateway = _Gateway(_response())
    await GatewayChatCompletionClient(gateway, 'shared', DiscussionUsage()).create(
        [UserMessage(content='search', source='user')], tools=[tool])
    description = gateway.requests[0].tools[0].description
    assert len(description) <= 1024
    assert description.endswith('verification_required.')


async def test_long_framework_description_is_bounded_without_losing_schema() -> None:
    tool = _tool()
    tool['description'] = 'x' * 2000
    gateway = _Gateway(_response())
    await GatewayChatCompletionClient(gateway, 'shared', DiscussionUsage()).create(
        [UserMessage(content='search', source='user')], tools=[tool])
    assert len(gateway.requests[0].tools[0].description) == 1024
    assert gateway.requests[0].tools[0].parameters['additionalProperties'] is False


@pytest.mark.parametrize("text", [None, "", "I will use this tool."])
async def test_unavailable_calls_still_fail_without_retry(text: str | None) -> None:
    gateway = _Gateway(_response(text, name="unapproved_tool"))
    usage = DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage)

    with pytest.raises(RuntimeExecutionError, match="unavailable capability"):
        await client.create([UserMessage(content="search", source="user")], tools=[_tool()])

    assert len(gateway.requests) == 1
    assert usage.tokens == 5


async def test_missing_usage_stays_unaccounted_without_retry() -> None:
    gateway = _Gateway(replace(_response("Search"), usage=None))
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage())

    with pytest.raises(UnaccountedUsage):
        await client.create([UserMessage(content="search", source="user")], tools=[_tool()])

    assert len(gateway.requests) == 1


def _durability(
    *,
    entries: tuple[_LedgerEntry, ...] = (),
    artifacts: tuple[Artifact, ...] = (),
) -> _DiscussionDurability:
    async def publish(
        artifacts: tuple[Artifact, ...], models: tuple[_LedgerEntry, ...], tools: tuple[_LedgerEntry, ...]
    ) -> None:
        pass

    async def store(artifact: Artifact) -> UUID:
        return uuid4()

    async def abort(write_id: UUID) -> bool:
        return True

    return _DiscussionDurability(
        publish, store, abort, lambda write_id: None,
        run_id=uuid4(), model_entries=entries, artifacts=artifacts,
    )


async def test_completed_mixed_reply_replays_without_rebilling_or_model_call() -> None:
    gateway = _Gateway(_response("Search"))
    usage = DiscussionUsage()
    durable = _durability()
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable)
    messages = [UserMessage(content="search", source="user")]
    first = await client.create(messages, tools=[_tool()])
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", usage, durability=restored)

    replay = await resumed.create(messages, tools=[_tool()])

    assert replay == first
    assert len(gateway.requests) == 1
    assert usage.tokens == 5
    assert usage.cost_usd == Decimal("0.003")


@pytest.mark.parametrize("change", ["parameters", "internal_name"])
async def test_changed_tool_schema_cannot_reuse_completed_model_result(change: str) -> None:
    gateway = _Gateway(_response(name="web.search"))
    durable = _durability()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=durable)
    messages = [UserMessage(content="search", source="user")]
    await client.create(messages, tools=[_tool()])
    restored = _durability(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=restored)
    changed = _tool()
    if change == "parameters":
        changed["parameters"] = {"type": "object", "properties": {"url": {"type": "string"}}}
    else:
        changed["name"] = "web_search"

    with pytest.raises(RuntimeExecutionError, match="durable model request mismatch"):
        await resumed.create(messages, tools=[changed])

    assert len(gateway.requests) == 1


@pytest.mark.parametrize("status", ["running", "UNKNOWN"])
async def test_uncertain_model_result_is_not_replayed_or_retried(status: str) -> None:
    gateway = _Gateway(_response(name="web.search"))
    durable = _durability()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=durable)
    messages = [UserMessage(content="search", source="user")]
    await client.create(messages, tools=[_tool()])
    entry = dict(durable.model_entries[0])
    entry["status"] = status
    restored = _durability(entries=(entry,), artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=restored)

    error = ModelOutcomeUncertain if status == "running" else RuntimeExecutionError
    with pytest.raises(error):
        await resumed.create(messages, tools=[_tool()])

    assert len(gateway.requests) == 1
