"""Independent, zero-transport regressions for runtime native capture eligibility."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_hub.models.gateway import ModelGateway
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import Deployment, ModelCapability, ModelMessage, ModelRequest
from tests.unit.models.test_gateway import CapacityStub, SecretStub, TransportStub, deployment

TOOLS = frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING})
REQUEST = ModelRequest(
    "primary", (ModelMessage("user", "Synthetic workspace task"),),
    required_capabilities=TOOLS,
)
CHAT = deployment("chat", capabilities=TOOLS)
RESPONSES = deployment("responses", capabilities=TOOLS, structured_output_api="responses")


@pytest.mark.parametrize(("deployments", "expected"), [
    ((CHAT,), True),
    ((RESPONSES,), False),
    ((CHAT, RESPONSES), False),
    ((replace(CHAT, capabilities=frozenset({ModelCapability.TEXT})),), False),
    ((), False),
    ((CHAT, replace(RESPONSES, capabilities=frozenset({ModelCapability.TEXT}))), True),
])
def test_capture_predicate_uses_all_and_only_real_capable_candidates(
    deployments: tuple[Deployment, ...], expected: bool,
) -> None:
    events: list[object] = []
    capacity, secrets, transport = CapacityStub(()), SecretStub(events), TransportStub(events)
    gateway = ModelGateway(ModelRegistry(deployments), capacity, secrets, transport)

    assert gateway.supports_native_tool_continuation(REQUEST) is expected
    assert not capacity.initialized and capacity.events == []
    assert secrets.references == [] and transport.calls == [] and events == []


@pytest.mark.parametrize("allow_fallback", [True, False])
def test_responses_fallback_prevents_capture_unless_fallback_is_disabled(allow_fallback: bool) -> None:
    gateway = ModelGateway(
        ModelRegistry((CHAT, replace(RESPONSES, logical_model="fallback"))),
        CapacityStub(()), SecretStub([]), TransportStub([]),
        fallbacks={"primary": "fallback"},
    )
    request = replace(REQUEST, allow_fallback=allow_fallback)
    assert gateway.supports_native_tool_continuation(request) is (not allow_fallback)
    groups, _, _, _ = gateway._capable_candidate_groups(request)
    assert [name for name, _ in groups] == (["primary", "fallback"] if allow_fallback else ["primary"])


def test_explicit_native_request_still_rejects_responses_only_registry() -> None:
    gateway = ModelGateway(ModelRegistry((RESPONSES,)), CapacityStub(()), SecretStub([]), TransportStub([]))
    with pytest.raises(NoCapableDeployment):
        gateway._capable_candidate_groups(replace(REQUEST, native_tool_continuation=True))


def test_existing_native_history_cannot_downgrade_or_move_to_foreign_deployment() -> None:
    native = NativeAssistantMessage("chat", None, (NativeToolCall("one", "workspace_read", "{}"),))
    request = replace(REQUEST, native_tool_continuation=True, messages=(
        *REQUEST.messages, ModelMessage("assistant", "", native_assistant=native),
        ModelMessage("tool", "Synthetic result", tool_call_id="one"),
    ))
    gateway = ModelGateway(
        ModelRegistry((CHAT, RESPONSES, replace(CHAT, id="foreign", logical_model="fallback"))),
        CapacityStub(()), SecretStub([]), TransportStub([]), fallbacks={"primary": "fallback"},
    )
    assert gateway.supports_native_tool_continuation(request) is True
    groups, _, _, _ = gateway._capable_candidate_groups(request)
    assert [(name, tuple(item.id for item in candidates)) for name, candidates in groups] == [
        ("primary", ("chat",)),
    ]
