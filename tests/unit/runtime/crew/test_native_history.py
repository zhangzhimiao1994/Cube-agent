"""Native tool/result causal groups, using only synthetic private data."""
from __future__ import annotations

import json

import pytest

from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import ModelMessage, ModelResponse, ToolCall
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime


def pair(call_id: str, text: str = "value") -> tuple[ModelMessage, ...]:
    native = NativeAssistantMessage("primary", None, (NativeToolCall(call_id, "workspace_read", '{}'),),
                                    reasoning_content="PRIVATE_" + call_id)
    return (
        ModelMessage("assistant", "", native_assistant=native),
        ModelMessage("tool", text, tool_call_id=call_id),
    )


def test_compaction_preserves_native_envelope_and_result_as_one_causal_group() -> None:
    messages = (ModelMessage("system", "policy"), ModelMessage("user", "task"),
                *pair("old", "x" * 1000), *pair("latest"), ModelMessage("system", "continue"))
    compacted = CrewDispatchRuntime._compact_interaction_messages(
        messages, max_prompt_bytes=700, message_limit=5, preserve_feedback=True,
    )
    assert compacted == (*messages[:2], *messages[-3:])
    assert compacted[2].native_assistant is not None
    assert compacted[2].native_assistant.reasoning_content == "PRIVATE_latest"
    assert compacted[3].tool_call_id == "latest"


def test_native_latest_group_cannot_be_silently_truncated_to_fit() -> None:
    messages = (ModelMessage("system", "p"), ModelMessage("user", "t"), *pair("latest", "x" * 1000))
    with pytest.raises(RuntimeError, match="native.*context"):
        CrewDispatchRuntime._compact_interaction_messages(messages, max_prompt_bytes=100, message_limit=8)


@pytest.mark.parametrize("messages", [
    (ModelMessage("tool", "orphan", tool_call_id="missing"),),
    (pair("a")[0], ModelMessage("tool", "wrong", tool_call_id="b")),
    (pair("a")[0], ModelMessage("system", "between"), pair("a")[1]),
])
def test_native_compaction_rejects_orphan_or_wrong_id_messages(messages: tuple[ModelMessage, ...]) -> None:
    with pytest.raises(RuntimeError, match="native.*history"):
        CrewDispatchRuntime._compact_interaction_messages(messages)


def test_native_fields_participate_in_crew_replay_digest() -> None:
    from dataclasses import replace

    from agent_hub.models.types import ModelRequest

    first = ModelRequest("general", (ModelMessage("user", "task"), *pair("one")),
                         native_tool_continuation=True)
    second = replace(first, messages=(ModelMessage("user", "task"), *pair("two")))
    assert CrewDispatchRuntime._model_request_sha256(first) != CrewDispatchRuntime._model_request_sha256(second)


def test_partial_native_batch_reports_unexecuted_members_without_claiming_success() -> None:
    from agent_hub.runtime.crew import native_history

    native = NativeAssistantMessage("primary", None, (
        NativeToolCall("a", "workspace_read", '{"path":"a"}'),
        NativeToolCall("b", "workspace_read", '{"path":"b"}'),
    ), reasoning_content="PRIVATE_NATIVE")
    response = ModelResponse(None, tool_calls=(
        ToolCall("a", "workspace.read", {"path": "a"}),
        ToolCall("b", "workspace.read", {"path": "b"}),
    ), native_assistant=native)
    messages = native_history.tool_result_messages(response, [
        {"name": "workspace.read", "result": {"status": "rejected"}},
    ])
    assert messages[0].native_assistant is native
    assert [message.tool_call_id for message in messages[1:]] == ["a", "b"]
    assert isinstance(messages[2].content, str)
    assert json.loads(messages[2].content.split("=", 1)[1])[0]["result"] == {"status": "not_executed"}
