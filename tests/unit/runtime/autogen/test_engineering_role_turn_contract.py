"""First-send engineering role turns preserve history and explicit harness intent."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from autogen_core import FunctionCall
from autogen_core.models import (
    AssistantMessage,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    LLMMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import ToolSchema

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.autogen.adapter import (
    GatewayChatCompletionClient,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _DiscussionDurability,
    _LedgerEntry,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact


class RecordingGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(text="Role response", usage=TokenUsage(
                prompt_tokens=2, completion_tokens=3, total_tokens=5)),
            deployment_id="local", logical_model=request.logical_model,
            provider_id="local", provider_model="local/model", cost_usd=Decimal(0),
        )


def role_history() -> list[LLMMessage]:
    messages: list[LLMMessage] = [SystemMessage(content="Approved engineering role")]
    for index in range(5):
        messages.extend([UserMessage(content=f"Existing evidence {index}", source="agent_hub"),
                         AssistantMessage(content=f"Role report {index}", source="project_engineer")])
    return messages


def tools() -> list[ToolSchema]:
    return [{"name": f"approved_tool_{i}", "description": "Existing approved tool",
             "parameters": {"type": "object", "properties": {}}} for i in range(6)]


async def send(messages: list[LLMMessage], *, actor: str = "project_engineer",
               engineering: bool = True) -> tuple[RecordingGateway, ModelRequest]:
    gateway = RecordingGateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), actor=actor,
        engineering_context_compaction=engineering, engineering_role_turn=engineering, max_output_tokens=24576)
    await client.create(messages, tools=tools())
    assert len(gateway.requests) == 1
    return gateway, gateway.requests[0]


async def test_role_message_tail_has_explicit_first_send_harness_continuation() -> None:
    history = role_history()
    original = list(history)
    _, request = await send(history)
    assert request.messages[-1].role == "user"
    assert tuple(GatewayChatCompletionClient._message(m) for m in original) == request.messages[:-1]
    assert history == original
    assert len(request.messages) == 12
    assert len(request.tools) == 6 and request.required_tool is None
    assert request.max_output_tokens == 24576
    assert "authorization" in request.messages[-1].content


async def test_failed_verify_then_role_report_is_a_new_ordinary_turn() -> None:
    history: list[LLMMessage] = [
        UserMessage(content="Original CLI-only task", source="user"),
        AssistantMessage(content=[FunctionCall(id="verify_1", name="project.verify", arguments="{}")],
                         source="project_engineer"),
        FunctionExecutionResultMessage(content=[FunctionExecutionResult(
            call_id="verify_1", name="project.verify", content="python_project_checks_failed", is_error=True)]),
        AssistantMessage(content="Revise using the existing verification feedback", source="project_engineer"),
    ]
    _, request = await send(history)
    assert request.messages[-1].role == "user"
    assert request.messages[-2].content == history[-1].content
    assert request.messages[:-1] == tuple(GatewayChatCompletionClient._message(m) for m in history)


@pytest.mark.parametrize("variant", ["other_actor", "nonengineering", "reasoning", "pending_tool", "user_tail"])
async def test_ambiguous_or_existing_continuation_history_unchanged(variant: str) -> None:
    history = role_history()
    actor, engineering = "project_engineer", True
    if variant == "other_actor":
        actor = "reviewer"
    elif variant == "nonengineering":
        engineering = False
    elif variant == "reasoning":
        history[-1] = AssistantMessage(content="Report", thought="Native reasoning", source=actor)
    elif variant == "pending_tool":
        history.insert(-1, AssistantMessage(content=[FunctionCall(id="pending", name="project.verify", arguments="{}")], source=actor))
    else:
        history.append(UserMessage(content="Existing orchestration", source="agent_hub"))
    _, request = await send(history, actor=actor, engineering=engineering)
    assert request.messages == tuple(GatewayChatCompletionClient._message(m) for m in history)

async def test_actual_assistant_agent_tool_text_thought_roundtrip_can_continue() -> None:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.messages import TextMessage
    from autogen_core import CancellationToken
    from autogen_core.tools import FunctionTool

    from agent_hub.models.types import ToolCall

    class ToolThenRoleGateway(RecordingGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            first = len(self.requests) == 1
            return GatewayCompletion(
                response=ModelResponse(
                    text="Ordinary accompanying tool text" if first else "Role report after verification",
                    tool_calls=(ToolCall(id="verify_actual", name="project_verify", arguments={}),) if first else (),
                    usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5)),
                deployment_id="local", logical_model=request.logical_model,
                provider_id="local", provider_model="local/model", cost_usd=Decimal(0))

    async def project_verify() -> str:
        return "python_project_checks_failed"

    gateway = ToolThenRoleGateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
        actor="project_engineer", engineering_context_compaction=True, engineering_role_turn=True, supports_tool_calls=True)
    agent = AssistantAgent("project_engineer", model_client=client,
        tools=[FunctionTool(project_verify, description="Controlled local verification feedback")],
        reflect_on_tool_use=True)
    await agent.on_messages([TextMessage(content="Original authorized task", source="user")], CancellationToken())
    before = tuple(await agent._model_context.get_messages())
    assert any(isinstance(m, AssistantMessage) and type(m.content) is list
               and m.thought == "Ordinary accompanying tool text" for m in before)
    assert isinstance(before[-1], AssistantMessage) and type(before[-1].content) is str
    count = len(gateway.requests)
    await agent.on_messages([], CancellationToken())
    assert len(gateway.requests) == count + 1
    request = gateway.requests[-1]
    assert request.messages[-1].role == "user"
    # The system message is supplied by AssistantAgent separately from context.
    assert request.messages[1:-1] == tuple(client._message(m) for m in before)


async def test_turn_cue_does_not_overflow_exact_message_limit() -> None:
    from agent_hub.runtime.autogen.adapter import RuntimeExecutionError, _MessageCompactionWindow

    gateway = RecordingGateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), actor="project_engineer",
        engineering_context_compaction=True, engineering_role_turn=True,
        compaction_window=_MessageCompactionWindow(watermark=20, target=16, allowed_messages=11, absolute_safety_limit=20))
    with pytest.raises(RuntimeExecutionError, match="message history"):
        await client.create(role_history(), tools=tools())
    assert not gateway.requests


def durable_history(*, entries: tuple[_LedgerEntry, ...] = (),
                    artifacts: tuple[Artifact, ...] = ()) -> _DiscussionDurability:
    async def publish(artifacts: tuple[Artifact, ...], models: tuple[_LedgerEntry, ...],
                      tools: tuple[_LedgerEntry, ...]) -> None:
        pass

    async def store(artifact: Artifact) -> UUID:
        return uuid4()

    async def abort(write_id: UUID) -> bool:
        return True

    return _DiscussionDurability(publish, store, abort, lambda write_id: None,
                                run_id=uuid4(), model_entries=entries, artifacts=artifacts)


@pytest.mark.parametrize("status", ["completed", "running", "UNKNOWN"])
async def test_legacy_ledger_hash_and_unknown_are_not_rewritten(status: str) -> None:
    gateway = RecordingGateway()
    durable = durable_history()
    old = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=durable)
    messages = role_history()
    first = await old.create(messages, tools=tools())
    entry = dict(durable.model_entries[0])
    if status != "completed":
        entry["status"] = status
    restored = durable_history(entries=(entry,), artifacts=durable.artifacts)
    current = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=restored,
        actor="project_engineer", engineering_context_compaction=True, engineering_role_turn=True)
    if status == "completed":
        assert await current.create(messages, tools=tools()) == first
    else:
        expected = ModelOutcomeUncertain if status == "running" else RuntimeExecutionError
        with pytest.raises(expected):
            await current.create(messages, tools=tools())
    assert len(gateway.requests) == 1
    assert restored.model_entries[0]["request_hash"] == entry["request_hash"]

async def test_nonengineering_handoff_without_explicit_role_flag_does_not_append_cue() -> None:
    gateway = RecordingGateway()
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), actor="project_engineer",
        engineering_context_compaction=True)
    history = role_history()
    await client.create(history, tools=tools())
    assert gateway.requests[0].messages == tuple(client._message(m) for m in history)

@pytest.mark.parametrize("engineering", [True, False])
async def test_actual_runtime_factory_passes_only_trusted_engineering_identity(
    monkeypatch: pytest.MonkeyPatch, engineering: bool,
) -> None:
    from collections.abc import Sequence

    from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
    from tests.unit.runtime.autogen.test_engineering_completion import (
        Capabilities,
        Gateway,
        context,
        plan,
    )

    seen: list[tuple[str | None, bool, bool]] = []
    original = GatewayChatCompletionClient._engineering_turn_messages

    def capture(self: GatewayChatCompletionClient, messages: Sequence[LLMMessage]) -> Sequence[LLMMessage]:
        seen.append((self._actor, self._engineering_role_turn, self._engineering_context_compaction))
        return original(self, messages)

    monkeypatch.setattr(GatewayChatCompletionClient, "_engineering_turn_messages", capture)
    ctx = context(1 if engineering else 0)
    runtime = AutoGenDiscussionRuntime(Gateway(["[COMPLETE] role report"]), plan(),
        capability_gateway=Capabilities())
    events = [event async for event in runtime.run(ctx)]
    assert events
    participants = [row for row in seen if row[0] in {"engineer", "reviewer"}]
    assert participants and all(row[1] is engineering for row in participants)
    assert all(row[2] is True for row in participants)  # handoff v1 also applies outside engineering
    assert all(flag is False for actor, flag, _ in seen if actor is None)  # selector

async def test_new_cued_request_replays_completed_result_without_another_send() -> None:
    gateway = RecordingGateway()
    durable = durable_history()
    first = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=durable,
        actor="project_engineer", engineering_context_compaction=True, engineering_role_turn=True)
    history = role_history()
    expected = await first.create(history, tools=tools())
    restored = durable_history(entries=tuple(durable.model_entries), artifacts=durable.artifacts)
    resumed = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(), durability=restored,
        actor="project_engineer", engineering_context_compaction=True, engineering_role_turn=True)
    assert await resumed.create(history, tools=tools()) == expected
    assert len(gateway.requests) == 1
