"""Owned, offline incremental read rejection and mutation boundary fixtures."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import urllib.request
from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CapabilityOutcomeUncertain,
    CrewDispatchRuntime,
    CrewRunStream,
    RuntimeExecutionError,
    _incremental_read_rejection,
    _ToolLedger,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FakeCapabilities,
    FastFactory,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_sequence_context,
)

UNAVAILABLE = "workspace read denied or scoped file unavailable"
PATH = "owned.txt"
SECRET = "OWN-FAILED-READ-CONTENT-NOT-FOR-FEEDBACK"
READ_NAMES = ("workspace.read", "workspace_read")


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch, _function_scoped_runner: asyncio.Runner) -> None:
    _function_scoped_runner.get_loop()

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("owned fixture cannot dispatch network traffic")

    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", denied)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", denied)


def response(name: str, *, path: str = PATH) -> ModelResponse:
    arguments: dict[str, str] = (
        {"path": path, "content": "owned fixture"}
        if name == "workspace.write_text"
        else {}
        if name == "workspace.bundle"
        else {"path": path}
    )
    return ModelResponse(
        text=None,
        tool_calls=(ToolCall(id="owned-call", name=name, arguments=arguments),),
        usage=TokenUsage(1, 1, 2),
    )


FINAL = ModelResponse(text="Owned workspace bundle delivered.", usage=TokenUsage(1, 1, 2))


class Gateway:
    def __init__(
        self,
        responses: tuple[ModelResponse, ...],
        *,
        repeat: bool = False,
        gate_after: int | None = None,
    ) -> None:
        self.responses = responses
        self.repeat = repeat
        self.gate_after = gate_after
        self.gate = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        index = len(self.requests) - 1
        assert index < 12, "bounded fixture must not loop indefinitely"
        if self.gate_after is not None and index >= self.gate_after:
            await self.gate.wait()
        if self.repeat:
            value = self.responses[0]
        else:
            assert index < len(self.responses), "unexpected extra model call"
            value = self.responses[index]
        return GatewayCompletion(
            response=value,
            deployment_id="owned-deployment",
            logical_model=request.logical_model,
            provider_id="owned-provider",
            provider_model="owned-provider/owned-model",
            cost_usd=Decimal("0.001"),
        )


class Harness(WorkspaceDeliverySequenceHarness):
    def __init__(
        self,
        backend: str = "failed_result",
        *,
        reject_write: bool = False,
        deny_after_write: bool = False,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.reject_write = reject_write
        self.deny_after_write = deny_after_write

    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult:
        if request.tool_name not in READ_NAMES and not (
            self.reject_write
            and request.tool_name == "workspace.write_text"
            and request.arguments.get("path") == ".hidden/owned.txt"
        ):
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request)
        assert user_id is not None and role is Role.OPERATOR
        path = request.arguments.get("path")
        if self.backend == "uncertain":
            raise CapabilityOutcomeUncertain("owned unconfirmed read")
        if (
            not self.deny_after_write
            and request.tool_name in READ_NAMES
            and isinstance(path, str)
            and path in self.files
        ):
            return HarnessToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="succeeded",
                payload={"text": self.files[path]},
            )
        reason = (
            "workspace path must not contain hidden files"
            if request.tool_name == "workspace.write_text"
            else UNAVAILABLE
        )
        if self.backend == "exception":
            raise RuntimeCapabilityError(reason)
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            payload={"path": "OWN-PRIVATE-PATH", "stderr": SECRET},
            failure_reason=reason,
        )


def plan(
    name: str = "workspace.read", *, incremental: bool = True, listing: bool = False
) -> DispatchPlan:
    tools = (name, "workspace.write_text", "workspace.bundle") + (
        ("workspace.list",) if listing else ()
    )
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="implementer",
                role="Implementer",
                goal="Build the owned project",
                logical_model="general",
                allowed_tools=tools,
            ),
        ),
        steps=(
            DispatchStep(
                id="implementer_step",
                agent="implementer",
                task="Project workspace delivery contract: build owned files and a bundle."
                if incremental
                else "Inspect an owned workspace.",
                tools=tools,
                final_synthesizer=True,
                token_budget=10_000,
                cost_budget_usd=Decimal(1),
            ),
        ),
        allowed_tools=tools,
        total_token_budget=10_000,
        total_cost_usd=Decimal(1),
    )


def runtime(
    gateway: Gateway,
    harness: Harness,
    *,
    selected: DispatchPlan | None = None,
    repository: InMemoryArtifactRepository | None = None,
) -> CrewDispatchRuntime:
    return CrewDispatchRuntime(
        gateway,
        selected or plan(),
        capability_gateway=FakeCapabilities(),
        harness_tool_gateway=harness,
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )


def context(checkpoint: RuntimeCheckpoint | None = None) -> TaskContext:
    return _workspace_delivery_sequence_context().model_copy(update={"checkpoint": checkpoint})


def feedback(request: ModelRequest) -> str:
    return "\n".join(
        message.content
        for message in request.messages
        if message.role in {"system", "user"}
        and isinstance(message.content, str)
        and (
            "CAPABILITY_ARGUMENT_CORRECTION" in message.content
            or "UNTRUSTED_CAPABILITY_REJECTIONS_JSON=" in message.content
        )
    )


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
@pytest.mark.parametrize("name", READ_NAMES)
async def test_incremental_repeated_read_continues_without_reinvoking_or_fake_success(
    backend: str,
    name: str,
) -> None:
    gateway = Gateway(
        (
            response(name),
            response(name),
            response("workspace.write_text"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    harness = Harness(backend)
    runner = runtime(gateway, harness, selected=plan(name, listing=True))
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        name,
        "workspace.write_text",
        "workspace.bundle",
    ]
    assert not any(
        event.kind is EventKind.TOOL_COMPLETED and event.tool_name == name for event in events
    )
    assert len(gateway.requests) == 5
    assert SECRET not in feedback(gateway.requests[2])
    assert "OWN-PRIVATE-PATH" not in feedback(gateway.requests[2])
    assert "workspace.list" in feedback(gateway.requests[2])
    checkpoint = await runner.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 10, "cost_usd": "0.005"}
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping)
    assert (
        sum(isinstance(item, Mapping) and item["status"] == "rejected" for item in tools.values())
        == 2
    )


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
async def test_successful_write_invalidates_read_rejection_but_reauthorizes(backend: str) -> None:
    gateway = Gateway(
        (
            response("workspace.read"),
            response("workspace.read"),
            response("workspace.write_text"),
            response("workspace.read"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    harness = Harness(backend)
    runner = runtime(gateway, harness)
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.read",
        "workspace.write_text",
        "workspace.read",
        "workspace.bundle",
    ]
    reads = [call for call in harness.calls if call.tool_name == "workspace.read"]
    assert reads[0].arguments == reads[1].arguments
    assert reads[0].idempotency_key != reads[1].idempotency_key
    assert reads[1].sandbox == "read_only"
    assert (
        len(
            [
                event
                for event in events
                if event.kind is EventKind.TOOL_COMPLETED and event.tool_name == "workspace.read"
            ]
        )
        == 1
    )
    assert (await runner.save_checkpoint()).state["usage"] == {"tokens": 12, "cost_usd": "0.006"}


async def test_write_does_not_authorize_denied_read_or_permanently_reset_rejection() -> None:
    gateway = Gateway(
        (
            response("workspace.read"),
            response("workspace.write_text"),
            response("workspace.read"),
            response("workspace.read"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    harness = Harness(deny_after_write=True)
    runner = runtime(gateway, harness)
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.read",
        "workspace.write_text",
        "workspace.read",
        "workspace.bundle",
    ]
    assert not any(
        event.kind is EventKind.TOOL_COMPLETED and event.tool_name == "workspace.read"
        for event in events
    )
    assert len(gateway.requests) == 6


async def test_generic_read_and_incremental_write_repeats_still_fail_closed() -> None:
    for selected, name, path, reject_write in (
        (plan(incremental=False), "workspace.read", PATH, False),
        (plan(), "workspace.write_text", ".hidden/owned.txt", True),
    ):
        gateway = Gateway((response(name, path=path), response(name, path=path)))
        harness = Harness(reject_write=reject_write)
        runner = runtime(gateway, harness, selected=selected)
        with pytest.raises(RuntimeExecutionError, match="repeated rejected request"):
            _ = [event async for event in runner.run(context())]
        assert len(harness.calls) == 1
        assert len(gateway.requests) == 2


async def test_incremental_hidden_write_feedback_does_not_claim_read_rejection() -> None:
    gateway = Gateway(
        (
            response("workspace.write_text", path=".hidden/owned.txt"),
            response("workspace.write_text"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    harness = Harness(reject_write=True)
    runner = runtime(gateway, harness, selected=plan(listing=True))
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    correction = feedback(gateway.requests[1])
    assert "invalid_workspace_path" in correction
    assert "the read is unavailable or denied" not in correction
    assert "workspace_read_unavailable" not in correction
    assert "already authorized workspace.list" not in correction
    assert [call.tool_name for call in harness.calls] == [
        "workspace.write_text",
        "workspace.write_text",
        "workspace.bundle",
    ]


async def test_incremental_cached_read_cannot_extend_round_budget() -> None:
    gateway, harness = Gateway((response("workspace.read"),), repeat=True), Harness()
    runner = runtime(gateway, harness)
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="round limit"):
        async for event in runner.run(context()):
            events.append(event)
    assert len(harness.calls) == 1
    assert 2 < len(gateway.requests) <= 9
    assert not any(event.kind is EventKind.TOOL_COMPLETED for event in events)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_cached_read_does_not_authorize_list_or_extend_token_budget() -> None:
    gateway = Gateway(
        (response("workspace.read"), response("workspace.read"), response("workspace.list"))
    )
    harness = Harness()
    runner = runtime(gateway, harness)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runner.run(context())]
    assert len(harness.calls) == 1
    assert len(gateway.requests) == 3
    assert "workspace.list" not in feedback(gateway.requests[2])
    budget_gateway = Gateway((response("workspace.read"),), repeat=True)
    budget_harness = Harness()
    base = plan()
    small = base.model_copy(
        update={
            "steps": (base.steps[0].model_copy(update={"token_budget": 4}),),
            "total_token_budget": 4,
        }
    )
    limited = runtime(budget_gateway, budget_harness, selected=small)
    with pytest.raises(RuntimeExecutionError, match="budget"):
        _ = [event async for event in limited.run(context().model_copy(update={"token_budget": 4}))]
    assert len(budget_gateway.requests) == 3
    assert len(budget_harness.calls) == 1
    exhausted = await limited.save_checkpoint()
    assert exhausted.state["usage"] == {"tokens": 6, "cost_usd": "0.003"}
    assert exhausted.state["phase"] == "budget_exhausted"


@pytest.mark.parametrize("write_before_restore", (False, True))
async def test_cached_read_json_checkpoint_and_terminal_restore_do_not_rebill(
    write_before_restore: bool,
) -> None:
    responses = (
        response("workspace.read"),
        response("workspace.read"),
        response("workspace.write_text"),
    )
    count = 3 if write_before_restore else 2
    gateway = Gateway(responses, gate_after=count)
    harness, repository = Harness(), InMemoryArtifactRepository()
    runner = runtime(gateway, harness, repository=repository)
    checkpoint: RuntimeCheckpoint | None = None
    async for event in runner.run(context()):
        if event.kind is EventKind.CHECKPOINT_SAVED and event.checkpoint is not None:
            tools = event.checkpoint.state["tools"]
            assert isinstance(tools, Mapping)
            if len(tools) == count and all(
                isinstance(value, Mapping) and value.get("status") in {"rejected", "succeeded"}
                for value in tools.values()
            ):
                checkpoint = event.checkpoint
                break
    await runner.cancel()
    assert checkpoint is not None
    checkpoint = RuntimeCheckpoint.from_payload(json.loads(json.dumps(checkpoint.to_payload())))
    remaining = (
        (response("workspace.read"), response("workspace.bundle"), FINAL)
        if write_before_restore
        else (
            response("workspace.write_text"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    resumed_gateway = Gateway(remaining)
    resumed_harness = Harness()
    resumed_harness.files = harness.files
    resumed = runtime(resumed_gateway, resumed_harness, repository=repository)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(context(checkpoint))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(resumed_gateway.requests) == 3
    assert [call.tool_name for call in resumed_harness.calls] == (
        ["workspace.read", "workspace.bundle"]
        if write_before_restore
        else ["workspace.write_text", "workspace.bundle"]
    )
    completed = await resumed.save_checkpoint()
    assert completed.state["usage"] == {
        "tokens": 12 if write_before_restore else 10,
        "cost_usd": "0.006" if write_before_restore else "0.005",
    }
    completed = RuntimeCheckpoint.from_payload(json.loads(json.dumps(completed.to_payload())))
    terminal_gateway, terminal_harness = Gateway(()), Harness()
    terminal = runtime(terminal_gateway, terminal_harness, repository=repository)
    await terminal.restore_checkpoint(completed)
    assert [event.kind async for event in terminal.run(context(completed))] == [
        EventKind.RUNTIME_COMPLETED
    ]
    assert terminal_gateway.requests == [] and terminal_harness.calls == []


async def test_legacy_continuation_flag_does_not_enable_new_read_cache() -> None:
    gateway, harness, repository = (
        Gateway((response("workspace.read"),), gate_after=1),
        Harness(),
        InMemoryArtifactRepository(),
    )
    runner = runtime(gateway, harness, repository=repository)
    checkpoint: RuntimeCheckpoint | None = None
    stream = runner.run(context())
    assert isinstance(stream, CrewRunStream)
    stream._state.workspace_delivery_continuation = False
    async for event in stream:
        if event.checkpoint is not None:
            tools = event.checkpoint.state["tools"]
            assert isinstance(tools, Mapping)
            if any(
                isinstance(value, Mapping) and value.get("status") == "rejected"
                for value in tools.values()
            ):
                checkpoint = event.checkpoint
                break
    await runner.cancel()
    assert checkpoint is not None
    payload = json.loads(json.dumps(checkpoint.to_payload()))
    del payload["state"]["workspace_delivery_continuation"]
    payload["state_sha256"] = ""
    legacy = RuntimeCheckpoint.from_payload(payload)
    next_gateway, next_harness = Gateway((response("workspace.read"),)), Harness()
    restored = runtime(next_gateway, next_harness, repository=repository)
    await restored.restore_checkpoint(legacy)
    with pytest.raises(RuntimeExecutionError, match="repeated rejected request"):
        _ = [event async for event in restored.run(context(legacy))]
    assert len(next_gateway.requests) == 1 and next_harness.calls == []


async def test_uncertain_read_is_not_negative_cached_or_replayed() -> None:
    gateway, harness, repository = (
        Gateway((response("workspace.read"),)),
        Harness("uncertain"),
        InMemoryArtifactRepository(),
    )
    runner = runtime(gateway, harness, repository=repository)
    with pytest.raises(CapabilityOutcomeUncertain):
        _ = [event async for event in runner.run(context())]
    checkpoint = await runner.save_checkpoint()
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping)
    assert all(
        isinstance(value, Mapping) and value.get("status") == "uncertain"
        for value in tools.values()
    )
    next_gateway, next_harness = Gateway(()), Harness()
    restored = runtime(next_gateway, next_harness, repository=repository)
    await restored.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in restored.run(context(checkpoint))]
    assert next_gateway.requests == [] and next_harness.calls == []


@pytest.mark.parametrize("backend", ("exception", "failed_result"))
async def test_cached_feedback_preserves_two_distinct_backend_correction_slots(
    backend: str,
) -> None:
    gateway = Gateway(
        (
            response("workspace.read"),
            response("workspace.read"),
            response("workspace.read", path="another-owned.txt"),
            response("workspace.write_text"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    harness = Harness(backend)
    runner = runtime(gateway, harness)
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.read",
        "workspace.read",
        "workspace.write_text",
        "workspace.bundle",
    ]
    assert len(gateway.requests) == 6
    checkpoint = await runner.save_checkpoint()
    assert checkpoint.state["usage"] == {"tokens": 12, "cost_usd": "0.006"}


async def test_cached_read_preserves_invalid_write_correction_slot() -> None:
    gateway = Gateway(
        (
            response("workspace.read"),
            response("workspace.read"),
            response("workspace.write_text", path=".hidden/owned.txt"),
            response("workspace.write_text"),
            response("workspace.bundle"),
            FINAL,
        )
    )
    harness = Harness(reject_write=True)
    runner = runtime(gateway, harness, selected=plan(listing=True))
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [call.tool_name for call in harness.calls] == [
        "workspace.read",
        "workspace.write_text",
        "workspace.write_text",
        "workspace.bundle",
    ]
    # Earlier read feedback is historical; the current write correction is not read evidence.
    current_correction = [
        message.content
        for message in gateway.requests[3].messages
        if message.role == "system"
        and isinstance(message.content, str)
        and "CAPABILITY_ARGUMENT_CORRECTION" in message.content
    ][-1]
    assert "the read is unavailable or denied" not in current_correction
    assert "already authorized workspace.list" not in current_correction
    assert len(gateway.requests) == 6


async def test_cached_read_does_not_expand_two_actual_rejection_limit() -> None:
    gateway = Gateway(
        (
            response("workspace.read"),
            response("workspace.read"),
            response("workspace.read", path="second.txt"),
            response("workspace.read", path="third.txt"),
        )
    )
    harness = Harness()
    runner = runtime(gateway, harness)
    with pytest.raises(RuntimeExecutionError, match="capability execution failed"):
        _ = [event async for event in runner.run(context())]
    assert len(harness.calls) == 3 and len(gateway.requests) == 4
    checkpoint = await runner.save_checkpoint()
    tools = checkpoint.state["tools"]
    assert isinstance(tools, Mapping)
    assert (
        sum(
            isinstance(value, Mapping) and value.get("status") == "rejected"
            for value in tools.values()
        )
        == 3
    )


def ledger_entry(
    ledger: _ToolLedger,
    position: tuple[int, int, int],
    *,
    write: bool = False,
) -> Artifact:
    name = "workspace.write_text" if write else "workspace.read"
    source_id = str(uuid4())
    artifact = Artifact(
        id=uuid4(),
        type="tool_result",
        producer="implementer",
        content={
            "agent_id": "implementer",
            "tool_name": name,
            "arguments_sha256": "a" * 64,
            "result": {"artifact_origin": "tool_workspace_write"}
            if write
            else {
                "status": "rejected",
                "error_code": "workspace_read_unavailable",
                "message": UNAVAILABLE,
                "tool_name": name,
            },
        },
        source_ids=(source_id,),
    )
    state: dict[str, JsonValue] = {
        "status": "succeeded" if write else "rejected",
        "step_id": "implementer_step",
        "attempt": position[0],
        "round": position[1],
        "tool_index": position[2],
        "name": name,
        "arguments_sha256": "a" * 64,
        "trigger_model_artifact_id": source_id,
        "replay_safe": False,
        "artifact_id": str(artifact.id),
        "sha256": artifact.content_sha256,
    }
    key = str(artifact.id)
    ledger.states[key], ledger.artifacts[key] = state, artifact
    return artifact


def test_future_rejection_cannot_replace_past_or_hide_causal_write() -> None:
    ledger = _ToolLedger()
    past = ledger_entry(ledger, (0, 0, 0))
    ledger_entry(ledger, (0, 4, 0))
    assert (
        _incremental_read_rejection(
            ledger,
            step=plan().steps[0],
            name="workspace.read",
            arguments_sha256="a" * 64,
            position=(0, 3, 0),
        )
        is past
    )
    ledger_entry(ledger, (0, 1, 0), write=True)
    assert (
        _incremental_read_rejection(
            ledger,
            step=plan().steps[0],
            name="workspace.read",
            arguments_sha256="a" * 64,
            position=(0, 3, 0),
        )
        is None
    )


def test_future_only_rejection_is_not_cache_authority() -> None:
    ledger = _ToolLedger()
    ledger_entry(ledger, (0, 4, 0))
    with pytest.raises(RuntimeExecutionError, match="rejection artifact"):
        _incremental_read_rejection(
            ledger,
            step=plan().steps[0],
            name="workspace.read",
            arguments_sha256="a" * 64,
            position=(0, 3, 0),
        )
