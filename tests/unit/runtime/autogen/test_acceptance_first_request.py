"""The bootstrap request narrows an existing grant, never creates a file itself."""

from collections.abc import Mapping
from decimal import Decimal

import pytest
from autogen_core.models import UserMessage

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import GatewayChatCompletionClient, RuntimeExecutionError
from agent_hub.runtime.autogen.termination import DiscussionUsage
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability, _tool


class Gateway:
    def __init__(self, path: str = "ACCEPTANCE.json") -> None:
        self.path = path
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(response=ModelResponse(
            text=None, tool_calls=(ToolCall(id="bootstrap", name=request.required_tool,
                arguments={"path": self.path, "content": "model authored acceptance"}),),
            usage=TokenUsage(2, 3, 5)), deployment_id="test", logical_model="shared",
            provider_id="test", provider_model="test/model", cost_usd=Decimal("0.01"))


async def test_bootstrap_narrows_only_model_authored_acceptance_write() -> None:
    gateway, durable = Gateway(), _durability()
    tool = _tool("workspace.write_text")
    original = dict(tool)
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        durability=durable, acceptance_first=True,
        engineering_action=lambda proposed: "workspace.write_text",
        engineering_budget=lambda: (100, 10.0))
    result = await client.create([UserMessage(content="Build user requirements", source="user")],
                                tools=[tool, _tool("workspace.bundle")])
    request = gateway.requests[0]
    assert request.required_tool == "workspace_write_text"
    assert len(request.tools) == 1
    schema = request.tools[0].parameters
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    assert properties["path"]["enum"] == ("ACCEPTANCE.json",)
    assert set(schema["required"]) == {"path", "content"}
    assert any("ACCEPTANCE.json" in message.content for message in request.messages)
    assert dict(tool) == original
    assert isinstance(result.content, list) and result.content[0].name == "workspace.write_text"
    assert durable.model_entries[0]["status"] == "succeeded"


@pytest.mark.parametrize("path", ["src/app.cjs", "../ACCEPTANCE.json", "acceptance.json"])
async def test_provider_cannot_change_bootstrap_target_and_usage_is_retained(path: str) -> None:
    gateway, durable, usage = Gateway(path), _durability(), DiscussionUsage()
    client = GatewayChatCompletionClient(gateway, "shared", usage, durability=durable,
        acceptance_first=True, engineering_action=lambda proposed: "workspace.write_text",
        engineering_budget=lambda: (100, 10.0))
    with pytest.raises(RuntimeExecutionError, match="acceptance"):
        await client.create([UserMessage(content="Build", source="user")],
                            tools=[_tool("workspace.write_text")])
    assert len(gateway.requests) == 1 and usage.tokens == 5
    assert durable.model_entries[0]["status"] == "succeeded"
    assert not durable.tool_entries


async def test_legacy_write_request_is_not_narrowed() -> None:
    gateway = Gateway("src/app.cjs")
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        engineering_action=lambda proposed: "workspace.write_text",
        engineering_budget=lambda: (100, 10.0))
    await client.create([UserMessage(content="Build", source="user")],
                        tools=[_tool("workspace.write_text")])
    assert not any("ACCEPTANCE.json" in message.content for message in gateway.requests[0].messages)
