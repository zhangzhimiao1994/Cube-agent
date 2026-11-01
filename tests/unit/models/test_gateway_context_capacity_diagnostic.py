"""Context admission and local congestion must remain distinguishable."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from agent_hub.models import gateway as gateway_module
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityRequestTooLarge,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import (
    ConservativeTokenEstimator,
    ModelContextWindowExceeded,
    ModelGateway,
    ModelGatewayError,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ToolDefinition,
)
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    StreamingTransportStub,
    deployment,
    lease,
    request,
)


class FixedEstimator:
    def estimate_input(self, model_request: ModelRequest) -> int:
        return 100

    def estimate(self, model_request: ModelRequest) -> int:
        return 100 + model_request.max_output_tokens


def fixture(
    candidates: tuple[Deployment, ...], outcomes: list[Exception] | None = None,
) -> tuple[ModelGateway, CapacityStub, SecretStub, StreamingTransportStub]:
    capacity = CapacityStub(outcomes or [])
    secrets = SecretStub(capacity.events)
    transport = StreamingTransportStub(capacity.events, [])
    fallbacks = {"primary": "backup"} if any(item.logical_model == "backup" for item in candidates) else {}
    gateway = ModelGateway(ModelRegistry(candidates), capacity, secrets, transport,
                           fallbacks=fallbacks, token_estimator=FixedEstimator(),
                           capacity_wait_timeout=0.1)
    return gateway, capacity, secrets, transport


async def invoke(gateway: ModelGateway, value: ModelRequest, streaming: bool) -> None:
    if streaming:
        _events = [event async for event in gateway.stream_openai_compatible_events(value)]
    else:
        await gateway.complete_with_context(value)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("window", [99, 100])
async def test_all_context_incompatible_is_typed_without_io(streaming: bool, window: int) -> None:
    gateway, capacity, secrets, transport = fixture((
        deployment("primary", context_window_tokens=window),
        deployment("backup", "backup", context_window_tokens=window),
    ))
    with pytest.raises(Exception) as caught:
        await invoke(gateway, request(), streaming)
    assert type(caught.value).__name__ == "ModelContextWindowExceeded"
    assert isinstance(caught.value, ModelGatewayError)
    assert str(caught.value) == "model context window exceeded"
    assert not capacity.events and not secrets.references
    assert not transport.calls and not transport.stream_calls
    if not streaming:
        scope = get_gateway_scope_diagnostic(caught.value)
        assert scope is not None
        assert scope.phase.value == "pretransport_context"
        assert scope.reason.value == "context_window_exceeded"
        assert scope.transport_entered_count == scope.failure_attempt_count == 0


@pytest.mark.parametrize("streaming", [False, True])
async def test_compatible_fallback_still_executes(streaming: bool) -> None:
    gateway, capacity, _, transport = fixture((
        deployment("primary", context_window_tokens=100),
        deployment("backup", "backup", context_window_tokens=101),
    ))
    capacity.outcomes = [lease("backup")]
    transport.stream._chunks = [{"choices": [{"delta": {"content": "ok"}}]}]
    await invoke(gateway, request(), streaming)
    calls = transport.stream_calls if streaming else transport.calls
    assert [call[0].id for call in calls] == ["backup"]


@pytest.mark.parametrize("streaming", [False, True])
async def test_context_error_does_not_enable_disabled_fallback(streaming: bool) -> None:
    gateway, capacity, secrets, _ = fixture((deployment("primary", context_window_tokens=100),
                                           deployment("backup", "backup", context_window_tokens=200)))
    with pytest.raises(ModelGatewayError) as caught:
        await invoke(gateway, request(allow_fallback=False), streaming)
    assert type(caught.value).__name__ == "ModelContextWindowExceeded"
    assert not capacity.events and not secrets.references


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("kind", [CapacityQueueFull, CapacityWaitTimeout])
async def test_pretransport_congestion_retains_specific_type(
    streaming: bool, kind: type[Exception], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Classify completed admission failures independently of host scheduling jitter.
    # Real deadline/window behavior is covered by test_gateway's admission tests.
    gateway, capacity, secrets, transport = fixture((deployment("primary"), deployment("backup", "backup")))
    acquire = AsyncMock(side_effect=kind("PRIVATE"))
    monkeypatch.setattr(gateway, "_acquire_capacity", acquire)
    with pytest.raises(kind) as caught:
        await invoke(gateway, request(), streaming)
    assert acquire.await_count == 2
    assert str(caught.value) == "model capacity unavailable"
    assert not secrets.references and not transport.calls and not transport.stream_calls
    assert not capacity.releases


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status", [429, 503])
async def test_real_transport_error_wins_over_later_congestion(streaming: bool, status: int) -> None:
    gateway, capacity, _, transport = fixture((deployment("primary"), deployment("backup", "backup")))
    capacity.outcomes = [lease("primary"), *[CapacityQueueFull("PRIVATE") for _ in range(10)]]
    transport.failure = ModelTransportError("PRIVATE", status_code=status)
    transport.stream._chunks = [ModelTransportError("PRIVATE", status_code=status)]
    with pytest.raises(ModelTransportError) as caught:
        await invoke(gateway, request(), streaming)
    assert caught.value.status_code == status
    assert len(transport.stream_calls if streaming else transport.calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("kind", [CapacityBackendError, CapacityConfigurationError])
async def test_backend_or_configuration_failure_is_not_replaced(streaming: bool, kind: type[Exception]) -> None:
    gateway, _, secrets, transport = fixture((deployment("primary"), deployment("backup", "backup")),
        [CapacityQueueFull("PRIVATE"), kind("PRIVATE")])
    with pytest.raises(kind):
        await invoke(gateway, request(), streaming)
    assert not secrets.references and not transport.calls and not transport.stream_calls


@pytest.mark.parametrize("streaming", [False, True])
async def test_mixed_token_limit_and_busy_fallback_is_not_context_error(streaming: bool) -> None:
    gateway, _, _, _ = fixture((deployment("primary"), deployment("backup", "backup")),
        [CapacityRequestTooLarge(), *[CapacityQueueFull("PRIVATE") for _ in range(10)]])
    with pytest.raises(CapacityQueueFull):
        await invoke(gateway, request(), streaming)


@pytest.mark.parametrize("allow_fallback,expected", [(False, 200), (True, 100)])
def test_context_window_query_uses_only_authorized_candidate_chain(allow_fallback: bool, expected: int) -> None:
    gateway, capacity, secrets, transport = fixture((
        deployment("primary", context_window_tokens=200),
        deployment("backup", "backup", context_window_tokens=100),
        deployment("unrelated", "other", context_window_tokens=1),
    ))
    assert gateway.context_window_tokens(request(allow_fallback=allow_fallback)) == expected
    assert not capacity.events and not secrets.references and not transport.calls


@pytest.mark.parametrize("windows,expected", [((None, None), None), ((None, 200), 200), ((99, 200), 99)])
def test_context_window_query_includes_oversized_candidates(
    windows: tuple[int | None, int | None], expected: int | None,
) -> None:
    gateway, _, _, _ = fixture((deployment("primary", context_window_tokens=windows[0]),
                                deployment("backup", "backup", context_window_tokens=windows[1])))
    assert gateway.context_window_tokens(request()) == expected


def test_context_window_query_filters_required_capabilities() -> None:
    tools = frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING})
    gateway, _, _, _ = fixture((deployment("text", context_window_tokens=1),
                                deployment("tools", capabilities=tools, context_window_tokens=200)))
    assert gateway.context_window_tokens(request(required=tools)) == 200


@pytest.mark.parametrize("kind", ["foreign", "responses", "messages"])
def test_context_window_query_filters_native_protocol_and_origin(kind: str) -> None:
    from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall

    tools = frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING})
    good = deployment("good", capabilities=tools, context_window_tokens=200)
    bad = replace(good, id="bad", context_window_tokens=1)
    if kind == "responses":
        bad = replace(bad, structured_output_api="responses")
    elif kind == "messages":
        bad = replace(bad, api_base="https://example.invalid/messages")
    assistant = NativeAssistantMessage("good", None, (NativeToolCall("call-1", "read", "{}"),), None)
    value = replace(request(required=tools), native_tool_continuation=True, messages=(
        ModelMessage("user", "PRIVATE"), ModelMessage("assistant", "", native_assistant=assistant),
        ModelMessage("tool", "PRIVATE", tool_call_id="call-1")))
    if kind != "foreign":
        value = replace(value, messages=(ModelMessage("user", "PRIVATE"),))
    gateway, capacity, secrets, transport = fixture((good, bad))
    assert gateway.context_window_tokens(value) == 200
    assert not capacity.events and not secrets.references and not transport.calls


def test_context_window_query_does_not_hide_missing_capabilities() -> None:
    gateway, _, _, _ = fixture((deployment("primary"),))
    with pytest.raises(NoCapableDeployment):
        gateway.context_window_tokens(request(required=frozenset({ModelCapability.TOOL_CALLING})))


def test_context_exception_has_fixed_default() -> None:
    error_type = getattr(gateway_module, "ModelContextWindowExceeded", None)
    assert error_type is not None
    assert issubclass(error_type, ModelGatewayError)
    assert str(error_type()) == "model context window exceeded"


def schema_heavy_request(*, native: bool) -> ModelRequest:
    tool = ToolDefinition("project_write", "Required project writer", {
        "type": "object", "required": ("content",), "additionalProperties": False,
        "properties": {"content": {"type": "string", "description": "schema field " * 6000}},
    })
    return replace(request(required=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING})),
        messages=(ModelMessage("system", "Keep the required tools."), ModelMessage("user", "Build.")),
        tools=(tool,), required_tool=tool.name, native_tool_continuation=native)


@pytest.mark.parametrize("native", [False, True])
def test_estimator_counts_large_required_tool_schema_for_shim_and_native(native: bool) -> None:
    value = schema_heavy_request(native=native)
    unchanged = replace(value)
    estimator = ConservativeTokenEstimator()
    without_tools = replace(value, tools=(), required_tool=None)
    assert estimator.estimate_input(without_tools) < 5000
    assert estimator.estimate_input(value) > 5000
    assert estimator.estimate(value) == estimator.estimate_input(value) + value.max_output_tokens
    assert estimator.estimate_input(value) == estimator.estimate_input(replace(value, native_tool_continuation=not native))
    assert value == unchanged and value.tools is unchanged.tools


@pytest.mark.parametrize("native", [False, True])
def test_estimator_counts_every_available_tool_not_only_required_tool(native: bool) -> None:
    value = schema_heavy_request(native=native)
    other = ToolDefinition("project_verify", "Verifier", value.tools[0].parameters)
    both = replace(value, tools=(*value.tools, other))
    estimator = ConservativeTokenEstimator()
    increment = estimator.estimate_input(both) - estimator.estimate_input(value)
    assert increment > 5000
    assert both.tools == (value.tools[0], other) and both.required_tool == value.required_tool


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("native", [False, True])
async def test_required_schema_overflow_fails_before_capacity_or_transport(native: bool, streaming: bool) -> None:
    value = schema_heavy_request(native=native)
    unchanged = replace(value)
    capacity = CapacityStub([lease("primary")])
    secrets = SecretStub(capacity.events)
    transport = StreamingTransportStub(capacity.events, [{"choices": [{"delta": {"content": "ok"}}]}])
    gateway = ModelGateway(ModelRegistry([deployment("primary", context_window_tokens=5000,
        capabilities=value.required_capabilities)]), capacity, secrets, transport)
    assert gateway.context_window_tokens(value) == 5000
    with pytest.raises(ModelContextWindowExceeded):
        await invoke(gateway, value, streaming)
    assert value == unchanged and value.tools is unchanged.tools
    assert not capacity.events and not secrets.references
    assert not transport.calls and not transport.stream_calls and not capacity.releases


@pytest.mark.parametrize("native", [False, True])
def test_history_fit_cannot_drop_required_schema_to_fake_fit(native: bool) -> None:
    from agent_hub.runtime.autogen.context_budget import ContextBudgetExceeded, fit_request_history

    value = schema_heavy_request(native=native)
    unchanged = replace(value)
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(value, context_window_tokens=5000)
    assert value == unchanged and value.tools is unchanged.tools
