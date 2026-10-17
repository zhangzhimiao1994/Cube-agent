from dataclasses import replace

from agent_hub.models.types import ModelMessage, ModelRequest
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.instruction_context import model_request_sha256


def test_default_native_fields_do_not_change_either_fixed_legacy_hash() -> None:
    request = ModelRequest("primary", (ModelMessage("user", "synthetic"),))
    assert model_request_sha256(request) == "0951e46b97d493bc6ae248eeef0cb0a7e7acb27d62764820614999a9294a9e71"
    assert CrewDispatchRuntime._model_request_sha256(request) == "f98a496ff85c0f11e3d5a1f82d373bdd01aa89b6ced0fe662e6f963dba159963"
    explicit = replace(request, native_tool_continuation=False)
    assert model_request_sha256(explicit) == model_request_sha256(request)


def test_native_hash_binds_raw_arguments_reasoning_ids_and_opt_in() -> None:
    from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall

    call = NativeToolCall("call-1", "lookup", '{ "x":1 }')
    assistant = NativeAssistantMessage("primary-1", None, (call,), "synthetic-private")

    def request(value: NativeAssistantMessage) -> ModelRequest:
        return ModelRequest("primary", (ModelMessage("assistant", "", native_assistant=value),
            ModelMessage("tool", "synthetic result", tool_call_id=value.tool_calls[0].id)),
            native_tool_continuation=True)

    before = model_request_sha256(request(assistant))
    for changed in (replace(assistant, reasoning_content="changed"),
                    replace(assistant, tool_calls=(replace(call, arguments='{"x":1}'),)),
                    replace(assistant, tool_calls=(replace(call, id="call-2"),))):
        assert model_request_sha256(request(changed)) != before
