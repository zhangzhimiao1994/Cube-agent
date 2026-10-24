"""Soft admission cannot cancel a model/tool operation already admitted."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import _builtin_input_schema
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    CapabilityGateway,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
)
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from tests.unit.runtime.autogen.test_draft_action_progress import (
    action_context,
    draft_case_for_context,
)

POLICY = "engineering_soft_admission_version"
TOOLS = ("project.inspect", "workspace.list", "workspace.write_text", "project.verify", "workspace.bundle")


class Journey:
    def __init__(self, *, policy: JsonValue = 1, source: str = "project_scale_soft_budget",
                 blocked: bool = False, slow_selector: bool = False, slow_inspect: bool = False) -> None:
        original = action_context()
        self.context = original.model_copy(update={"timeout_seconds": 0.3, "routing_decision": {
            **original.routing_decision, POLICY: policy,
            "engineering_progress_budget_version": 1, "engineering_checkpoint_ack_version": 1,
            "engineering_writer_continuation_version": 1, "runtime_timeout_source": source,
            "runtime_timeout_soft_seconds": 0.3, "runtime_timeout_absolute_seconds": 2.0,
        }})
        self.blocked, self.slow_selector = blocked, slow_selector
        self.slow_inspect = slow_inspect
        self.requests: list[ModelRequest] = []
        self.calls: list[HarnessToolCallRequest] = []
        self.entered = asyncio.Event()
        self.runner = AutoGenDiscussionRuntime(self, DiscussionPlan(participants=(
            DiscussionParticipant(id="implementer", role="Engineer", goal="Build", logical_model="writer",
                                  allowed_tools=TOOLS),
            DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="reviewer"),
        ), selector_model="selector", max_turns=8, token_budget=500, wall_time_seconds=0.3),
            capability_gateway=cast(CapabilityGateway, self), harness_tool_gateway=self)

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return tenant_id == self.context.tenant_id and name in TOOLS

    def is_replay_safe(self, name: str) -> bool:
        return False

    def manifest(self, tenant_id: UUID) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple({"name": name, "input_schema": _builtin_input_schema(name)} for name in TOOLS)

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id and user_id == self.context.actor_id
        assert role == self.context.actor_role
        self.calls.append(request)
        if request.tool_name == "project.inspect":
            payload = draft_case_for_context(self.context)
        elif request.tool_name == "workspace.list":
            payload = {"workspace_files": ()}
        else:
            assert request.tool_name == "workspace.write_text"
            content = request.arguments["content"]
            assert isinstance(content, str)
            payload = {"workspace_files": ({"path": request.arguments["path"],
                "sha256": hashlib.sha256(content.encode()).hexdigest(), "size_bytes": len(content.encode())},)}
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                    status="succeeded", payload=payload)

    async def cross_soft(self) -> None:
        budget = self.runner._wall_budget
        assert budget is not None
        await asyncio.sleep(max(0.0, budget.deadline - asyncio.get_running_loop().time()) + 0.03)

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == "selector":
            if self.slow_selector:
                await self.cross_soft()
            response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
        elif request.required_tool in {"project_inspect", "workspace_list"}:
            if self.slow_inspect and request.required_tool == "project_inspect":
                await self.cross_soft()
            response = ModelResponse(text=None, tool_calls=(ToolCall(
                id="bootstrap-" + request.required_tool, name=request.required_tool, arguments={}),),
                usage=TokenUsage(1, 1, 2))
        else:
            assert request.logical_model == "writer" and request.required_tool == "workspace_write_text"
            self.entered.set()
            if self.blocked:
                await asyncio.Event().wait()
            await self.cross_soft()
            response = ModelResponse(text=None, tool_calls=(ToolCall(id="accepted-write",
                name="workspace_write_text", arguments={"path": "ACCEPTANCE.json", "content": "model criteria"}),),
                usage=TokenUsage(1, 1, 2))
        return GatewayCompletion(response=response, deployment_id="test", logical_model=request.logical_model,
                                 provider_id="test", provider_model="test/model", cost_usd=Decimal(0))


async def run_to_write(case: Journey) -> tuple[list[RunEvent], RuntimeCheckpoint | None]:
    events: list[RunEvent] = []
    saved = None
    stream = cast(AsyncGenerator[RunEvent, None], case.runner.run(case.context))
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                events.append(event)
                if (event.checkpoint is not None
                    and event.checkpoint.state.get("engineering_tool_prefix") == 3):
                    saved = event.checkpoint
                    break
    finally:
        await stream.aclose()
    return events, saved


async def test_admitted_model_tool_and_ack_cross_soft_without_raising_absolute_budget() -> None:
    case = Journey()
    events, saved = await run_to_write(case)
    assert saved is not None, {"last": [(e.kind, e.reason) for e in events[-2:]],
                               "calls": [c.tool_name for c in case.calls]}
    assert [call.tool_name for call in case.calls] == ["project.inspect", "workspace.list", "workspace.write_text"]
    assert case.requests[-1].timeout_seconds > case.context.timeout_seconds
    assert 0 < cast(float, saved.state["remaining_timeout_seconds"]) <= 0.3
    assert 0 < cast(float, saved.state["remaining_absolute_timeout_seconds"]) < 1.8
    assert saved.state[POLICY] == 1
    case.runner._validate_checkpoint(saved, case.context)
    incomplete = dict(saved.state)
    incomplete.pop("remaining_absolute_timeout_seconds")
    missing_hard_budget = RuntimeCheckpoint(id=saved.id, runtime_type=saved.runtime_type,
        runtime_version=saved.runtime_version, run_id=saved.run_id, tenant_id=saved.tenant_id,
        mode=saved.mode, state=incomplete)
    with pytest.raises(RuntimeExecutionError, match="incompatible"):
        case.runner._validate_checkpoint(missing_hard_budget, case.context)
    for version in (None, True, "1", 2):
        route = {**case.context.routing_decision, POLICY: version}
        with pytest.raises(RuntimeExecutionError, match="incompatible"):
            case.runner._validate_checkpoint(saved, case.context.model_copy(update={"routing_decision": route}))
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events), "a write is not delivery"


@pytest.mark.parametrize("policy,source", [(None, "project_scale_soft_budget"),
    (True, "project_scale_soft_budget"), ("1", "project_scale_soft_budget"),
    (2, "project_scale_soft_budget"), (1, "explicit")])
async def test_old_invalid_or_explicit_policy_still_cancels_at_its_existing_deadline(
    policy: JsonValue, source: str,
) -> None:
    case = Journey(policy=policy, source=source)
    events, saved = await run_to_write(case)
    assert saved is None
    assert not any(call.tool_name == "workspace.write_text" for call in case.calls)
    failure = next(e for e in events if e.kind is EventKind.RUNTIME_FAILED)
    assert failure.payload["runtime_timeout_kind"] == ("explicit" if source == "explicit" else "soft")


async def test_late_selector_cannot_admit_another_model_or_write() -> None:
    case = Journey(slow_selector=True)
    events, saved = await run_to_write(case)
    assert saved is None and len(case.requests) == 1
    assert case.requests[0].logical_model == "selector"
    assert not any(call.tool_name == "workspace.write_text" for call in case.calls)
    failure = next(e for e in events if e.kind is EventKind.RUNTIME_FAILED)
    assert failure.reason == "engineering_delivery_missing"
    assert failure.payload["runtime_timeout_kind"] == "soft"


async def test_inflight_operation_still_stops_at_the_unchanged_absolute_fuse() -> None:
    case = Journey(blocked=True)
    events, saved = await run_to_write(case)
    assert saved is None
    failure = next(e for e in events if e.kind is EventKind.RUNTIME_FAILED)
    assert failure.payload["runtime_timeout_kind"] == "absolute"
    assert cast(float, failure.payload["remaining_absolute_timeout_seconds"]) <= 0.02
    assert not any(call.tool_name == "workspace.write_text" for call in case.calls)
    assert len(case.requests) == 4


async def test_missing_ack_cannot_enable_soft_admission() -> None:
    case = Journey()
    route = dict(case.context.routing_decision)
    route.pop("engineering_checkpoint_ack_version")
    case.context = case.context.model_copy(update={"routing_decision": route})
    events, saved = await run_to_write(case)
    assert saved is None and not any(c.tool_name == "workspace.write_text" for c in case.calls)
    failure = next(e for e in events if e.kind is EventKind.RUNTIME_FAILED)
    assert failure.payload["runtime_timeout_kind"] == "soft"


async def test_delayed_running_checkpoint_ack_does_not_admit_a_late_transport() -> None:
    case = Journey()
    events: list[RunEvent] = []
    stream = cast(AsyncGenerator[RunEvent, None], case.runner.run(case.context))
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                events.append(event)
                if event.checkpoint is not None:
                    rows = event.checkpoint.state["model_ledger"]
                    if (isinstance(rows, tuple) and rows and isinstance(rows[-1], Mapping)
                        and rows[-1].get("status") == "running"
                        and rows[-1].get("required_tool") == "workspace_write_text"):
                        assert len(case.requests) == 3
                        await case.cross_soft()
    finally:
        await stream.aclose()
    assert len(case.requests) == 3 and not any(c.tool_name == "workspace.write_text" for c in case.calls)
    failure = next(e for e in events if e.kind is EventKind.RUNTIME_FAILED)
    assert failure.payload["runtime_timeout_kind"] == "soft"


async def test_user_can_cancel_an_admitted_operation_after_soft_expiry() -> None:
    case = Journey(blocked=True)
    events: list[RunEvent] = []

    async def consume() -> None:
        try:
            async for event in case.runner.run(case.context):
                events.append(event)
        except asyncio.CancelledError:
            pass

    pending = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(case.entered.wait(), timeout=2)
        await case.cross_soft()
        await case.runner.cancel()
        await asyncio.wait_for(pending, timeout=3)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    terminal = [e for e in events if e.kind in {EventKind.RUNTIME_CANCELLED, EventKind.RUNTIME_FAILED}]
    assert len(terminal) == 1 and terminal[0].kind is EventKind.RUNTIME_CANCELLED
    assert not any(c.tool_name == "workspace.write_text" for c in case.calls)


async def test_inflight_unknown_checkpoint_never_reenters_provider_on_restore() -> None:
    case = Journey(blocked=True)
    events: list[RunEvent] = []
    stream = cast(AsyncGenerator[RunEvent, None], case.runner.run(case.context))
    saved = None
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                events.append(event)
                if case.entered.is_set():
                    saved = next(e.checkpoint for e in reversed(events) if e.checkpoint is not None)
                    break
                # The running checkpoint suspends before provider entry.
                if event.checkpoint is not None:
                    rows = event.checkpoint.state["model_ledger"]
                    if (isinstance(rows, tuple) and rows and isinstance(rows[-1], Mapping)
                        and rows[-1].get("required_tool") == "workspace_write_text"):
                        saved = event.checkpoint
                        break
    finally:
        await stream.aclose()
    assert saved is not None
    before = len(case.requests)
    restored = AutoGenDiscussionRuntime(case, case.runner._plan,
        capability_gateway=cast(CapabilityGateway, case), harness_tool_gateway=case,
        artifact_repository=case.runner._repository)
    await restored.restore_checkpoint(RuntimeCheckpoint.from_payload(saved.to_payload()))
    result = [e async for e in restored.run(case.context.model_copy(update={"checkpoint": saved}))]
    assert result[-1].kind is EventKind.RUNTIME_FAILED
    # Compacted transcript cannot match the pending request; fail closed before replay.
    assert result[-1].reason == "durable model request mismatch"
    assert len(case.requests) == before


@pytest.mark.parametrize("count", [1, 20])
def test_admitted_progress_renews_one_window_and_never_revives_absolute(count: int) -> None:
    budget = AdaptiveDeadline.create(now=0, initial_seconds=450, soft_seconds=300,
                                    absolute_seconds=3600, complexity_units=3)
    assert budget.renew_for_progress(progress_units=count, now=500, runway_seconds=300,
                                     admitted_completion=True)
    assert budget.deadline == 800
    assert not budget.renew_for_progress(progress_units=count, now=600, runway_seconds=300,
                                        admitted_completion=True)
    assert budget.deadline == 800
    assert not budget.renew_for_progress(progress_units=count + 1, now=3600, runway_seconds=300,
                                        admitted_completion=True)
    assert budget.deadline == 800 and budget.absolute_deadline == 3600


async def test_existing_project_action_without_generated_writer_still_honors_admission() -> None:
    case = Journey(slow_inspect=True)
    route = dict(case.context.routing_decision)
    route.pop("engineering_writer_continuation_version")
    case.context = case.context.model_copy(update={"routing_decision": route})
    events, saved = await run_to_write(case)
    assert saved is None
    assert [c.tool_name for c in case.calls] == ["project.inspect"]
    assert len(case.requests) == 2, "no new model after the admitted inspection crosses soft"
    failure = next(e for e in events if e.kind is EventKind.RUNTIME_FAILED)
    assert failure.payload["runtime_timeout_kind"] == "soft"


async def test_zero_soft_cold_restore_never_gets_a_new_one_millisecond_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = Journey(slow_selector=True)
    events, _ = await run_to_write(case)
    saved = next(e.checkpoint for e in reversed(events) if e.checkpoint is not None)
    assert saved.state["remaining_timeout_seconds"] == 0.0
    restored = AutoGenDiscussionRuntime(case, case.runner._plan,
        capability_gateway=cast(CapabilityGateway, case), harness_tool_gateway=case,
        artifact_repository=case.runner._repository)
    await restored.restore_checkpoint(saved)
    stream = cast(AsyncGenerator[RunEvent, None], restored.run(
        case.context.model_copy(update={"checkpoint": saved})))
    before = len(case.requests)
    try:
        with monkeypatch.context() as clock:
            clock.setattr(asyncio.get_running_loop(), "time", lambda: 0.0)
            event = await anext(stream)
            assert event.kind is EventKind.DISCUSSION_STARTED
            assert restored._wall_budget is not None
            assert restored._wall_budget.deadline <= 0.0
    finally:
        await stream.aclose()
    assert len(case.requests) == before
