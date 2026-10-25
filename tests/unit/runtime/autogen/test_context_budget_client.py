"""Context fitting must precede model intent and survive durable restoration."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from typing import cast

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

from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    GatewayCompletion,
    ModelContextWindowExceeded,
)
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.autogen.adapter import (
    GatewayChatCompletionClient,
    RuntimeExecutionError,
    _compact_llm_messages,
    _MessageCompactionWindow,
    _model_tool_definitions,
)
from agent_hub.runtime.autogen.context_budget import fit_request_history
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import JsonValue
from tests.unit.runtime.autogen.test_tool_correction_client import TOOLS, Journal, Model, request


def large_request() -> ModelRequest:
    messages = [ModelMessage(role="system", content="Preserve the engineering policy."),
                ModelMessage(role="user", content="Build and test the integer CLI.")]
    for index in range(9):
        call_id = f"write_{index}"
        arguments = json.dumps({"path": f"source{index}.cjs", "content": "function f(){return [1,2,3];}\n" * 70})
        messages.append(ModelMessage(role="assistant", content="EXPLICIT_CAPABILITY_CALLS_JSON=" +
            json.dumps([{"id": call_id, "name": "workspace_write_text", "arguments": arguments}])))
        messages.append(ModelMessage(role="user", content="UNTRUSTED_CAPABILITY_RESULTS_JSON=" +
            json.dumps([{"call_id": call_id, "name": "workspace_write_text",
                         "content": "{\"result\":{\"status\":\"written\"}}", "is_error": False}])))
    return replace(request(), messages=tuple(messages))


class WindowModel(Model):
    def __init__(self, journal: Journal, *, reject: bool = False, enforce: bool = True,
                 forbidden: bool = False) -> None:
        super().__init__(journal, forbidden=forbidden)
        self.window = 5000
        self.reject, self.enforce = reject, enforce
        self.window_queries = 0
        self.forbid_window_query = False

    def context_window_tokens(self, request: ModelRequest) -> int:
        assert not self.forbid_window_query
        self.window_queries += 1
        return self.window

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.enforce:
            assert ConservativeTokenEstimator().estimate_input(request) < self.window
        if self.reject:
            return await super().complete_with_context(request)
        assert not self.forbidden
        self.requests.append(request)
        assert self.journal.snapshots[-1][-1]["request_hash"] == self.journal.durability().request_hash(
            request, tool_names=self.tool_names)
        return GatewayCompletion(response=ModelResponse(text=None, usage=TokenUsage(2, 1, 3),
            tool_calls=(ToolCall(id="list_call", name="workspace_list", arguments={}),)),
            logical_model=request.logical_model, deployment_id="shared", provider_id="test",
            provider_model="test/model", cost_usd=Decimal("0.001"))


def client(model: WindowModel, journal: Journal, usage: DiscussionUsage, *, enabled: bool = True,
           models: tuple[dict[str, JsonValue], ...] = ()) -> GatewayChatCompletionClient:
    return GatewayChatCompletionClient(model, "shared", usage,
        durability=journal.durability(models=models), supports_tool_calls=True,
        tool_correction_enabled=True, engineering_budget=lambda: (100 - usage.tokens, 5.0),
        engineering_context_compaction=enabled)


async def test_large_history_is_fitted_before_its_durable_intent() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model, original = WindowModel(journal), large_request()
    assert ConservativeTokenEstimator().estimate_input(original) > model.window
    await client(model, journal, usage)._complete_model_request(original, TOOLS, None)
    fitted = model.requests[0]
    assert fitted != original and len(fitted.messages) < len(original.messages)
    assert fitted.messages[:2] == original.messages[:2]
    assert fitted.messages[-2:] == original.messages[-2:]
    assert replace(fitted, messages=original.messages) == original
    assert any(message.content.startswith("COMPRESSED_ENGINEERING_HISTORY_JSON=") for message in fitted.messages)
    assert usage.tokens == 3


async def test_fitted_known_result_restores_without_provider_or_recounting() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = WindowModel(journal)
    await client(model, journal, usage)._complete_model_request(large_request(), TOOLS, None)
    restored = WindowModel(journal, forbidden=True)
    rows = tuple(dict(row) for row in journal.snapshots[-1])
    await client(restored, journal, usage, models=rows)._complete_model_request(large_request(), TOOLS, None)
    assert not restored.requests and usage.tokens == 3


async def test_legacy_known_hash_is_not_rewritten_by_new_context_policy() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = WindowModel(journal, enforce=False)
    await client(model, journal, usage, enabled=False)._complete_model_request(large_request(), TOOLS, None)
    assert model.requests[0] == large_request() and model.window_queries == 0
    restored = WindowModel(journal, forbidden=True)
    restored.forbid_window_query = True
    rows = tuple(dict(row) for row in journal.snapshots[-1])
    await client(restored, journal, usage, models=rows)._complete_model_request(large_request(), TOOLS, None)
    assert not restored.requests and usage.tokens == 3


async def test_fitted_running_intent_remains_unknown_and_cannot_repeat() -> None:
    journal, usage = Journal(), DiscussionUsage()
    await client(WindowModel(journal), journal, usage)._complete_model_request(large_request(), TOOLS, None)
    intent = tuple(dict(row) for row in journal.snapshots[0])
    restored = WindowModel(journal, forbidden=True)
    with pytest.raises(RuntimeExecutionError, match="model outcome is uncertain"):
        await client(restored, journal, usage, models=intent)._complete_model_request(large_request(), TOOLS, None)
    assert not restored.requests


async def test_tool_correction_is_bound_to_the_fitted_original() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = WindowModel(journal, reject=True)
    await client(model, journal, usage)._complete_model_request(large_request(), TOOLS, None)
    assert len(model.requests) == 2
    first, corrected = model.requests
    assert corrected.messages[:len(first.messages)] == first.messages
    assert journal.snapshots[-1][1]["correction_for"] == 0
    assert usage.tokens == 13


async def test_unfit_pinned_input_stops_before_intent_or_provider() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = WindowModel(journal)
    original = replace(request(), messages=(ModelMessage(role="system", content="policy " * 10000),
        ModelMessage(role="user", content="Keep the full task.")))
    with pytest.raises(ModelContextWindowExceeded, match="context window"):
        await client(model, journal, usage)._complete_model_request(original, TOOLS, None)
    assert not journal.snapshots and not model.requests and usage.tokens == 0


async def test_unavailable_gateway_context_query_keeps_legacy_behavior() -> None:
    journal, usage = Journal(), DiscussionUsage()
    model = WindowModel(journal, enforce=False)
    model.context_window_tokens = cast(object, None)  # type: ignore[method-assign,assignment]
    await client(model, journal, usage)._complete_model_request(large_request(), TOOLS, None)
    assert model.requests[0] == large_request()


async def test_issued_context_scope_is_retained_without_backend_failure_details() -> None:
    from agent_hub.models.gateway import ModelGateway
    from agent_hub.models.registry import ModelRegistry
    from agent_hub.runtime.failure_reason import (
        gateway_scope_failure_payload,
        safe_model_gateway_failure_reason,
    )
    from tests.unit.models.test_gateway import CapacityStub, SecretStub, TransportStub, deployment

    events: list[object] = []
    gateway = ModelGateway(ModelRegistry([replace(deployment("primary"), logical_model="shared",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}), context_window_tokens=100)]),
        CapacityStub([]), SecretStub(events), TransportStub(events))
    with pytest.raises(ModelContextWindowExceeded) as caught:
        await gateway.complete_with_context(large_request())
    expected = gateway_scope_failure_payload(caught.value, include_noncapacity=True)
    assert expected and expected["gateway_scope_transport_entered_count"] == 0
    durable = Journal().durability()
    durable.fail_model(safe_model_gateway_failure_reason(caught.value), source=caught.value)
    assert durable.failure_payload == expected


def framework_history() -> list[LLMMessage]:
    messages: list[LLMMessage] = [SystemMessage(content="policy"), UserMessage(content="original task", source="user")]
    for index in range(2):
        messages.extend((
            AssistantMessage(content=[FunctionCall(id=f"call{index}", name="workspace.list", arguments="{}")],
                             source="engineer"),
            FunctionExecutionResultMessage(content=[FunctionExecutionResult(
                call_id=f"call{index}", name="workspace.list", content="{}", is_error=False)]),
        ))
    return messages


def test_count_compaction_keeps_the_call_paired_with_its_retained_result() -> None:
    messages = framework_history()
    compacted = _compact_llm_messages(messages, window=_MessageCompactionWindow(
        watermark=4, target=4, allowed_messages=8, absolute_safety_limit=100), preserve_tool_pairs=True)
    assert compacted[:2] == tuple(messages[:2])
    assert compacted[-2:] == tuple(messages[-2:])
    assert len(compacted) <= 8


def test_old_compression_receipt_does_not_replace_the_latest_user_instruction() -> None:
    original = large_request()
    instruction = ModelMessage(role="user", content="Never modify protected configuration.")
    old_receipt = ModelMessage(role="user", content="COMPRESSED_DISCUSSION_HISTORY_JSON=" +
        json.dumps({"compacted_messages": 8, "sha256": "a" * 64}))
    original = replace(original, messages=(*original.messages[:2], instruction, old_receipt,
                                         *original.messages[2:]))
    fitted = fit_request_history(original, context_window_tokens=5000)
    assert instruction in fitted.messages


@pytest.mark.parametrize("running", [False, True])
async def test_old_count_compaction_public_create_restores_original_hash_without_replay(running: bool) -> None:
    history = framework_history()
    omitted = history[2:-1]
    removed = [{"type": type(message).__name__, "source": getattr(message, "source", ""),
                "content": str(message.content)} for message in omitted]
    digest = hashlib.sha256(json.dumps(removed, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    receipt = UserMessage(source="user", content="COMPRESSED_DISCUSSION_HISTORY_JSON=" + json.dumps({
        "compacted_messages": len(omitted), "sha256": digest,
        "recent_samples": [{"type": type(message).__name__, "source": getattr(message, "source", ""),
                            "preview": str(message.content)} for message in omitted],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    legacy = (*history[:2], receipt, history[-1])
    schemas = [{"name": "workspace.list", "description": "List", "parameters": {
        "type": "object", "properties": {}, "additionalProperties": False}}]
    definitions, names = _model_tool_definitions(schemas)
    legacy_request = ModelRequest(logical_model="shared", max_output_tokens=100,
        messages=tuple(GatewayChatCompletionClient._message(message) for message in legacy),
        tools=definitions, required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}))
    journal, usage = Journal(), DiscussionUsage()
    first = WindowModel(journal, enforce=False)
    first.tool_names = names
    await client(first, journal, usage, enabled=False)._complete_model_request(legacy_request, names, None)
    rows = tuple(dict(row) for row in journal.snapshots[0 if running else -1])
    restored = WindowModel(journal, forbidden=True)
    restored.forbid_window_query = True
    restored.tool_names = names
    public = GatewayChatCompletionClient(restored, "shared", usage, max_output_tokens=100,
        supports_tool_calls=True, durability=journal.durability(models=rows),
        engineering_context_compaction=True,
        compaction_window=_MessageCompactionWindow(4, 4, 8, 100))
    if running:
        with pytest.raises(RuntimeExecutionError, match="model outcome is uncertain"):
            await public.create(history, tools=schemas)
    else:
        result = await public.create(history, tools=schemas)
        assert result.finish_reason == "function_calls"
    assert not restored.requests and usage.tokens == 3
