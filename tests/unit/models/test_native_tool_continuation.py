from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agent_hub.models.gateway import ConservativeTokenEstimator, ModelGateway
from agent_hub.models.litellm_client import (
    ModelResponseError,
    ModelTransportError,
    _chat_completion_create_kwargs,
    _parse_response,
)
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest, ToolDefinition
from tests.contracts.test_litellm_client import API_KEY, deployment, mock_transport, request
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease

if TYPE_CHECKING:
    from agent_hub.models.native_messages import NativeAssistantMessage

PRIVATE = "synthetic-private-continuation"
RAW_ARGS = '{ "path" : "synthetic.txt" }'
CAPABILITIES = frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING})


def sdk(*, reasoning: object = PRIVATE, arguments: str = RAW_ARGS) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
            role="assistant", content=None, reasoning_content=reasoning,
            tool_calls=[SimpleNamespace(id="call-1", type="function", function=SimpleNamespace(
                name="workspace_read", arguments=arguments))]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def native(*, reasoning: str | None = PRIVATE, deployment_id: str = "primary-1") -> NativeAssistantMessage:
    from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall

    return NativeAssistantMessage(deployment_id, None,
                                  (NativeToolCall("call-1", "workspace_read", RAW_ARGS),), reasoning)


def continued(**overrides: Any) -> ModelRequest:
    assistant = native()
    return request(messages=(ModelMessage("user", "synthetic"),
        ModelMessage("assistant", "", native_assistant=assistant),
        ModelMessage("tool", '{"content":"synthetic result"}', tool_call_id="call-1")),
        native_tool_continuation=True, required_capabilities=CAPABILITIES, **overrides)


async def test_two_turn_fake_sdk_preserves_native_wire_only_when_opted_in() -> None:
    client, _, create, _ = mock_transport(result=sdk())
    selected = deployment(capabilities=CAPABILITIES)
    first = await client.complete(selected, request(native_tool_continuation=True), API_KEY)
    assert first.native_assistant is not None
    assert first.native_assistant.tool_calls[0].arguments == RAW_ARGS
    second = continued()
    await client.complete(selected, second, API_KEY)
    wire = create.call_args_list[-1].kwargs["messages"]
    assert wire[1] == first.native_assistant.to_wire()
    assert wire[1]["reasoning_content"] == PRIVATE
    assert wire[2]["role"] == "tool" and wire[2]["tool_call_id"] == "call-1"
    assert create.await_count == 2


def test_default_parser_does_not_capture_reasoning_or_infer_native_from_json() -> None:
    value = _parse_response(sdk(), "primary-1", ())
    assert getattr(value, "native_assistant", None) is None
    raw = sdk()
    raw.choices[0].message.content = json.dumps({"reasoning_content": PRIVATE, "tool_calls": []})
    raw.choices[0].message.tool_calls = None
    value = _parse_response(raw, "primary-1", (), native_tool_continuation=True)
    assert value.native_assistant is not None and value.native_assistant.reasoning_content == PRIVATE
    assert value.native_assistant.tool_calls == ()  # No calls are inferred from content JSON.
    raw.choices[0].message.reasoning_content = None
    value = _parse_response(raw, "primary-1", (), native_tool_continuation=True)
    assert value.native_assistant is not None and value.native_assistant.reasoning_content is None


def test_dto_roundtrip_raw_arguments_and_repr_are_private() -> None:
    from agent_hub.models.native_messages import NativeAssistantMessage

    value = native()
    payload = value.to_payload()
    assert NativeAssistantMessage.from_payload(payload) == value
    assert payload["protocol"] == "chat_completions"
    assert "deployment_id" not in value.to_wire()
    assert value.to_wire()["tool_calls"][0]["function"]["arguments"] == RAW_ARGS
    assert PRIVATE not in repr(value) and RAW_ARGS not in repr(value.tool_calls[0])
    with pytest.raises(FrozenInstanceError):
        value.content = "changed"  # type: ignore[misc]
    for mutated in ({**payload, "protocol": "responses"}, {**payload, "unexpected": PRIVATE},
                    {**payload, "tool_calls": [payload["tool_calls"][0]] * 2}):
        with pytest.raises((TypeError, ValueError)):
            NativeAssistantMessage.from_payload(mutated)


@pytest.mark.parametrize("arguments", ('[]', '{"a":NaN}', '{"a":1,"a":2}', '{broken'))
def test_native_arguments_reject_ambiguous_or_nonobject_json(arguments: str) -> None:
    from agent_hub.models.native_messages import NativeToolCall

    with pytest.raises(ValueError):
        NativeToolCall("call-1", "workspace_read", arguments)


@pytest.mark.parametrize("reasoning", ([], 1, True))
def test_bad_native_sdk_fields_preserve_usage_and_fixed_errors(reasoning: object) -> None:
    with pytest.raises(ModelResponseError) as caught:
        _parse_response(sdk(reasoning=reasoning), "primary-1", (), native_tool_continuation=True)
    assert caught.value.evidence is not None
    assert caught.value.evidence.usage_status == "known"
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize("bad", (None, 0, 1, "true"))
def test_opt_in_is_strict_bool(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        request(native_tool_continuation=bad)


def test_native_fields_cannot_be_used_in_legacy_or_wrong_role() -> None:
    with pytest.raises(ValueError):
        ModelMessage("user", "", native_assistant=native())
    with pytest.raises(ValueError):
        ModelMessage("assistant", "synthetic", tool_call_id="call-1")
    with pytest.raises(ValueError):
        replace(continued(), native_tool_continuation=False)


def test_shared_message_payload_and_native_accounting_include_all_fields() -> None:
    from agent_hub.models.types import model_message_payload

    value = native()
    assert model_message_payload(ModelMessage("assistant", "", native_assistant=value)) == value.to_wire()
    assert model_message_payload(ModelMessage("user", "synthetic")) == {"role": "user", "content": "synthetic"}
    estimator = ConservativeTokenEstimator()
    short = continued()
    long_native = replace(value, reasoning_content=PRIVATE * 100)
    long = replace(short, messages=(short.messages[0],
        ModelMessage("assistant", "", native_assistant=long_native), short.messages[2]))
    assert estimator.estimate_input(long) > estimator.estimate_input(short)
    tool = ToolDefinition("workspace_read", "synthetic description", {"type": "object"})
    assert estimator.estimate_input(replace(short, tools=(tool,))) > estimator.estimate_input(short)


@pytest.mark.parametrize("kind", ("foreign", "responses", "messages"))
async def test_direct_client_rejects_incompatible_continuation_before_client_creation(kind: str) -> None:
    selected = deployment(capabilities=CAPABILITIES)
    if kind == "foreign": selected = replace(selected, id="foreign")
    if kind == "responses": selected = replace(selected, structured_output_api="responses")
    if kind == "messages": selected = replace(selected, api_base="https://example.invalid/messages")
    client, factory, create, _ = mock_transport(result=sdk())
    with pytest.raises(ValueError):
        await client.complete(selected, continued(), API_KEY)
    assert factory.call_count == create.await_count == 0


@pytest.mark.parametrize("kind", ("foreign", "responses", "messages"))
async def test_gateway_filters_native_candidates_before_capacity_or_secret(kind: str) -> None:
    selected = deployment(capabilities=CAPABILITIES)
    if kind == "foreign": selected = replace(selected, id="foreign")
    if kind == "responses": selected = replace(selected, structured_output_api="responses")
    if kind == "messages": selected = replace(selected, api_base="https://example.invalid/messages")
    client, factory, _, _ = mock_transport(result=sdk())
    capacity = CapacityStub([])
    secrets = SecretStub(capacity.events)
    gateway = ModelGateway(ModelRegistry([selected]), capacity, secrets, client)
    with pytest.raises(NoCapableDeployment):
        await gateway.complete_with_context(continued())
    assert not capacity.events and not secrets.references and factory.call_count == 0


@pytest.mark.parametrize("kind", ("orphan", "missing", "duplicate", "unbound_tool"))
def test_native_request_requires_complete_unique_call_result_groups(kind: str) -> None:
    value = continued()
    messages: tuple[ModelMessage, ...]
    if kind == "orphan": messages = (value.messages[2],)
    elif kind == "missing": messages = value.messages[:2]
    elif kind == "duplicate": messages = (*value.messages, value.messages[2])
    else: messages = (ModelMessage("tool", "synthetic result"),)
    with pytest.raises(ValueError):
        replace(value, messages=messages)


def test_native_parser_requires_real_wire_function_type_not_content_claim() -> None:
    raw = sdk()
    raw.choices[0].message.tool_calls[0].type = "custom"
    with pytest.raises(ModelResponseError) as caught:
        _parse_response(raw, "primary-1", (), native_tool_continuation=True)
    assert caught.value.evidence is not None and caught.value.evidence.usage_status == "known"
    assert getattr(_parse_response(raw, "primary-1", ()), "native_assistant", None) is None


async def test_gateway_sends_only_exact_native_deployment_and_preserves_capture() -> None:
    selected = deployment(capabilities=CAPABILITIES)
    capacity = CapacityStub([lease(selected.id, selected.quota_scope_id)])
    client, _, create, _ = mock_transport(result=sdk())
    registry = ModelRegistry([selected, replace(selected, id="a-foreign")])
    gateway = ModelGateway(registry, capacity, SecretStub(capacity.events), client)
    result = await gateway.complete_with_context(continued())
    assert result.response.native_assistant is not None
    assert result.response.native_assistant.deployment_id == result.deployment_id == selected.id
    acquire = next(row for row in capacity.events if isinstance(row, tuple) and row[0] == "acquire")
    assert acquire[1] == (selected.id,) and create.await_count == 1


async def test_private_native_history_never_falls_back_after_transport_failure() -> None:
    selected = deployment(capabilities=CAPABILITIES)
    backup = deployment(id="backup", logical_model="backup", capabilities=CAPABILITIES,
                        provider_model="other/vendor")
    capacity = CapacityStub([lease(selected.id, selected.quota_scope_id), lease(backup.id)])
    secrets = SecretStub(capacity.events)
    client, factory, create, _ = mock_transport(error=ModelTransportError("synthetic unavailable"))
    gateway = ModelGateway(ModelRegistry([selected, backup]), capacity, secrets, client,
                           fallbacks={"primary": "backup"})
    with pytest.raises(ModelTransportError):
        await gateway.complete_with_context(continued())
    assert factory.call_count == create.await_count == 1
    assert secrets.references == [selected.secret_ref]
    assert len(capacity.outcomes) == 1  # Backup lease was never requested.


def test_native_stream_is_rejected_before_sdk_and_pure_kwargs_cannot_bypass_guard() -> None:
    client, factory, _, _ = mock_transport(result=sdk())
    selected = deployment(capabilities=CAPABILITIES)
    with pytest.raises(ValueError):
        client.stream_openai_compatible_chunks(selected, continued(), API_KEY)
    with pytest.raises(ValueError):
        _chat_completion_create_kwargs(replace(selected, id="foreign"), continued(), stream=False)
    assert factory.call_count == 0


def test_legacy_parser_ignores_bad_private_extension_and_mapping_keeps_native() -> None:
    assert _parse_response(sdk(reasoning={"bad": True}), "primary-1", ()).native_assistant is None
    value = _parse_response(sdk(), "primary-1", (), native_tool_continuation=True)
    call = replace(value.tool_calls[0], name="workspace.read")
    mapped = replace(value, tool_calls=(call,))
    assert mapped.native_assistant is value.native_assistant
    assert mapped.native_assistant is not None
    assert mapped.native_assistant.tool_calls[0].name == "workspace_read"
    assert PRIVATE not in repr(mapped) and PRIVATE not in repr(continued())
