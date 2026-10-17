"""Independent no-tool continuation regression; all data and execution are synthetic."""
from __future__ import annotations

import json
from dataclasses import replace

from agent_hub.models.native_messages import NativeAssistantMessage
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    _workspace_delivery_bundle_response,
    _workspace_delivery_sequence_context,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
    FINAL,
    Observations,
    offline_only,  # noqa: F401
    write,
)
from tests.unit.runtime.crew.test_native_tool_continuation import native, subject


class ChatGateway(WorkspaceDeliverySequenceGateway):
    def supports_native_tool_continuation(self, request: ModelRequest) -> bool:
        return True


async def test_delivery_correction_retains_native_assistant_without_tool_calls() -> None:
    private = NativeAssistantMessage(
        "primary", "Premature synthetic summary", (),
        reasoning_content="SYNTHETIC_PRIVATE_NO_TOOL_REASONING",
    )
    first = replace(FINAL, text=private.content, native_assistant=private)
    gateway = ChatGateway((first, native(write()), native(_workspace_delivery_bundle_response()), FINAL))
    harness = Observations()
    runtime = subject(gateway, harness, InMemoryProviderContinuationRepository())

    events = [event async for event in runtime.run(_workspace_delivery_sequence_context())]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    assert all(request.native_tool_continuation for request in gateway.requests)
    second = gateway.requests[1]
    assert any(message.content.startswith("WORKSPACE_DELIVERY_CONTINUATION:")
               for message in second.messages if isinstance(message.content, str))
    assistants = [message for message in second.messages if message.native_assistant is not None]
    assert len(assistants) == 1
    assert assistants[0].native_assistant == private
    assert assistants[0].role == "assistant" and assistants[0].content == first.text
    assert assistants[0].native_assistant.tool_calls == ()
    assert not any(message.tool_call_id is not None for message in second.messages)
    assert [call.tool_name for call in harness.calls] == ["workspace.write_text", "workspace.bundle"]
    assert "SYNTHETIC_PRIVATE_NO_TOOL_REASONING" not in json.dumps(
        (await runtime.save_checkpoint()).to_payload(),
    )
    assert all("SYNTHETIC_PRIVATE_NO_TOOL_REASONING" not in json.dumps(event.to_payload())
               for event in events)
