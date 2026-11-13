from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import NoReturn, Protocol, cast
from uuid import UUID

import pytest
from autogen_core import CancellationToken
from autogen_core.models import UserMessage

from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    CapabilityGateway,
    DiscussionParticipant,
    GatewayCapabilityTool,
    GatewayChatCompletionClient,
    RuntimeExecutionError,
    ToolOutcomeUncertain,
)
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from tests.integration.runtime.test_autogen_adapter import context, plan
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability, _Gateway


class _MutableReplayPolicy(Protocol):
    is_replay_safe: Callable[[str], bool | int]


class PreparationCase:
    def __init__(self, *, policy: bool | int=True, repeat: bool=False, repair: bool=False) -> None:
        self.policy = policy
        self.repeat = repeat
        self.requests: list[ModelRequest] = []
        self.proposals = 0
        self.calls: list[HarnessToolCallRequest] = []
        self.ctx = context(routing_decision={"engineering_auto_repair_version": 1} if repair else {})
        actor = DiscussionParticipant(id="analyst", role="Analyst", goal="Search",
                                     logical_model="shared", allowed_tools=("web.search",))
        self.runtime = AutoGenDiscussionRuntime(
            self, plan(selector_model="selector", participants=(actor, plan().participants[1]),
                       max_turns=8, token_budget=1000),
            capability_gateway=cast(CapabilityGateway, self), harness_tool_gateway=self,
        )

    def is_replay_safe(self, name: str) -> bool | int:
        return self.policy

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if request.logical_model == "selector":
            response = ModelResponse(text="analyst", usage=TokenUsage(1, 1, 2))
        elif self.calls:
            response = ModelResponse(text="[COMPLETE] safe result", usage=TokenUsage(1, 1, 2))
        else:
            self.proposals += 1
            # Current code must fail quickly on the unexpected next request,
            # rather than burn a long runtime budget during the RED run.
            if self.proposals > 2:
                raise AssertionError("unexpected repeated model after rejected preparation")
            query = "x" * 66_000 if self.policy is True and (self.proposals == 1 or self.repeat) else "small"
            response = ModelResponse(text=None, tool_calls=(
                ToolCall(id=f"Call_{self.proposals}", name="web_search", arguments={"query": query}),
            ), usage=TokenUsage(1, 1, 2))
        return GatewayCompletion(response=response, deployment_id="shared",
                                 logical_model=request.logical_model, provider_id="mock",
                                 provider_model="mock/local", cost_usd=Decimal(0))

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object) -> HarnessToolCallResult:
        self.calls.append(request)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status="succeeded", payload={"safe": True})

    async def collect(self) -> list[RunEvent]:
        async with asyncio.timeout(10):
            return [event async for event in self.runtime.run(self.ctx)]


@pytest.mark.parametrize("policy,kind", [(1, "invalid_replay_policy"), (True, "payload_limit")])
async def test_real_framework_preparation_rejection_stops_before_another_model(policy: bool | int, kind: str) -> None:
    case = PreparationCase(policy=policy)
    events = await case.collect()
    failure = events[-1]
    assert failure.kind is EventKind.RUNTIME_FAILED
    assert failure.payload["discussion_failure_phase"] == "tool_preparation"
    assert failure.payload["discussion_failure_kind"] == kind
    assert len(case.requests) == 2 and case.calls == []
    assert not any(e.kind in {EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED} for e in events)
    assert not any(e.kind is EventKind.MESSAGE_CREATED for e in events)
    assert RunEvent.model_validate_json(failure.model_dump_json()).payload == failure.payload
    assert _public_event_payload(failure.to_payload())["payload"] == failure.to_payload()["payload"]


async def test_real_framework_replans_oversized_unsubmitted_proposal_once_without_approval() -> None:
    case = PreparationCase(repair=True)
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(e.kind,e.reason,dict(e.payload)) for e in events]
    assert len(case.calls) == 1 and case.calls[0].arguments == {"query": "small"}
    rejected = [e for e in events if e.kind == "tool.preparation_rejected"]
    assert len(rejected) == 1 and rejected[0].payload["tool_submitted"] is False
    assert rejected[0].payload["replanning_available"] is True
    assert [r.logical_model for r in case.requests[:3]] == ["selector", "shared", "shared"]
    assert case.proposals == 2
    assert sum(e.kind is EventKind.TOOL_STARTED for e in events) == 1
    assert not any("approval" in str(e.kind) or "repair.proposed" in str(e.kind) for e in events)
    assert "x" * 100 not in json.dumps([e.to_payload() for e in events if e.kind == "tool.preparation_rejected"])


async def test_real_framework_second_rejected_proposal_cannot_buy_another_replan() -> None:
    case = PreparationCase(repair=True, repeat=True)
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload.get("discussion_failure_phase") == "tool_preparation", [(e.kind,e.reason,dict(e.payload)) for e in events]
    assert case.proposals == 2 and case.calls == []
    assert len(case.requests) == 3


async def test_preparation_failure_does_not_release_another_model_lock_or_replace_prior_diagnostic() -> None:
    durable = _durability()
    await durable._model_lock.acquire()
    durable.failure_reason = "model_outcome_uncertain"
    durable.failure_payload = {"known": "original"}
    try:
        with pytest.raises(RuntimeExecutionError):
            durable.reject_preparation(actor="analyst", name="web.search", call_id="call_1",
                                       kind="invalid_replay_policy")
        assert durable._model_lock.locked()
        assert durable.failure_reason == "model_outcome_uncertain"
        assert durable.failure_payload == {"known": "original"}
        assert durable.model_entries == [] and durable.tool_entries == []
    finally:
        durable._model_lock.release()


async def test_checkpoint_failure_after_before_tool_remains_uncertain_not_preparation_rejection() -> None:
    case = PreparationCase(policy=False, repair=True)
    durable = _durability()

    async def storage_failure(*args: object) -> NoReturn:
        raise RuntimeError("PRIVATE_CHECKPOINT_BODY")

    durable._publish = storage_failure
    tool = GatewayCapabilityTool(cast(CapabilityGateway, case), tenant_id=case.ctx.tenant_id, run_id=case.ctx.run_id,
        actor="analyst", name="web.search", records=[], durability=durable,
        harness_tool_gateway=case, server_context=case.ctx)
    with pytest.raises(RuntimeError, match="PRIVATE_CHECKPOINT_BODY"):
        await tool.run_json({"query": "small"}, CancellationToken(), call_id="call_1")
    assert durable.preparation_rejection is None and case.calls == []
    assert durable.tool_entries[0]["status"] == "running"
    with pytest.raises(ToolOutcomeUncertain):
        await tool.run_json({"query": "small"}, CancellationToken(), call_id="call_1")
    assert durable.preparation_rejection is None and case.calls == []


async def test_cold_rejection_checkpoint_replays_known_models_before_one_corrective_request() -> None:
    case = PreparationCase(repair=True)
    stream = case.runtime.run(case.ctx)
    rejected = False
    saved = None
    try:
        async with asyncio.timeout(10):
            async for event in stream:
                rejected |= event.kind == "tool.preparation_rejected"
                if rejected and event.checkpoint is not None:
                    saved = event.checkpoint
                    break
    finally:
        await cast(AsyncGenerator[RunEvent, None], stream).aclose()
    assert saved is not None and len(case.requests) == 2 and case.calls == []
    # Known completed model responses and their exact usage survive the rejected
    # proposal. The marker is not a fabricated tool ledger entry.
    assert cast(Mapping[str, JsonValue], saved.state["usage"])["tokens"] == 4
    assert not saved.state["tool_ledger"]
    assert all(row["status"] == "succeeded" for row in cast(tuple[Mapping[str, JsonValue], ...], saved.state["model_ledger"]))
    saved = RuntimeCheckpoint.from_payload(json.loads(json.dumps(saved.to_payload())))
    resumed = AutoGenDiscussionRuntime(case, case.runtime._plan, capability_gateway=cast(CapabilityGateway, case),
        harness_tool_gateway=case, artifact_repository=case.runtime._repository)
    await resumed.restore_checkpoint(saved)
    async with asyncio.timeout(10):
        events = [event async for event in resumed.run(case.ctx.model_copy(update={"checkpoint": saved}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(e.kind,e.reason,dict(e.payload)) for e in events]
    assert len(case.calls) == 1
    # No provider selector/initial proposal is replayed; first new request is
    # the corrective actor request, and all original model artifacts stay raw.
    assert case.requests[2].logical_model == "shared"
    assert len(case.requests) == 5


async def test_rejection_during_model_publication_fences_actual_provider_send() -> None:
    durable = _durability()
    gateway = _Gateway(ModelResponse(text="unused", usage=TokenUsage(1, 1, 2)))

    async def reject_while_publishing(*args: object) -> None:
        with pytest.raises(RuntimeExecutionError):
            durable.reject_preparation(actor="analyst", name="web.search", call_id="call_1",
                                       kind="invalid_replay_policy")

    durable._publish = reject_while_publishing
    client = GatewayChatCompletionClient(gateway, "shared", DiscussionUsage(),
                                        durability=durable, actor="analyst")
    with pytest.raises(RuntimeExecutionError):
        await client.create([UserMessage(content="safe", source="user")])
    assert gateway.requests == [] and not durable._model_lock.locked()
    assert durable.failure_payload["discussion_failure_kind"] == "invalid_replay_policy"


async def test_policy_exception_text_is_not_published_as_summary_or_failure() -> None:
    case = PreparationCase()
    case.proposals = 1  # The tool proposal is valid and reaches only the policy lookup.

    def broken_policy(name: str) -> NoReturn:
        raise RuntimeError("PRIVATE_POLICY_BODY Bearer sk-test-private")

    cast(_MutableReplayPolicy, case).is_replay_safe = broken_policy
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["discussion_failure_kind"] == "invalid_replay_policy"
    assert not case.calls and len(case.requests) == 2
    assert "PRIVATE" not in json.dumps([e.to_payload() for e in events])


async def test_waiting_after_corrective_proposal_resumes_original_approval_without_model_replay() -> None:
    class WaitingCase(PreparationCase):
        approved = False
        requests_at_approved_invoke: int | None = None

        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object) -> HarnessToolCallResult:
            if not self.approved:
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status="failed", payload={"approval_id": "approval_preparation"},
                    failure_reason="capability requires approval")
            self.requests_at_approved_invoke = len(self.requests)
            return await super().invoke(tenant_id, request, **kwargs)

    case = WaitingCase(repair=True)
    # Approval resumption is an existing engineering-only contract. Retain that
    # gate rather than widening it to a plain chat merely for this regression.
    case.ctx = case.ctx.model_copy(update={"routing_decision": {
        **case.ctx.routing_decision, "engineering_delivery_contract_version": 1,
    }})
    actor = replace(case.runtime._plan.participants[0], allowed_tools=("web.search", "workspace.write_text"))
    case.runtime = AutoGenDiscussionRuntime(case,
        replace(case.runtime._plan, participants=(actor, case.runtime._plan.participants[1])),
        capability_gateway=cast(CapabilityGateway, case), harness_tool_gateway=case)
    events = await case.collect()
    saved = await case.runtime.save_checkpoint()
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert checkpoint_waits_for_approval(saved, "approval_preparation")
    assert not case.calls and len(case.requests) == 3
    case.approved = True
    resumed = AutoGenDiscussionRuntime(case, case.runtime._plan, capability_gateway=cast(CapabilityGateway, case),
        harness_tool_gateway=case, artifact_repository=case.runtime._repository)
    await resumed.restore_checkpoint(saved)
    stream = resumed.run(case.ctx.model_copy(update={"checkpoint": saved}))
    restored_events = []
    try:
        async with asyncio.timeout(10):
            async for event in stream:
                restored_events.append((event.kind,event.reason,dict(event.payload)))
                if event.checkpoint is not None and cast(tuple[Mapping[str, JsonValue], ...], event.checkpoint.state["tool_ledger"])[-1]["status"] == "succeeded":
                    break
    finally:
        await cast(AsyncGenerator[RunEvent, None], stream).aclose()
    assert len(case.calls) == 1, restored_events
    assert case.requests_at_approved_invoke == 3
    assert case.calls[0].arguments == {"query": "small"}


async def test_invoked_uncertain_failure_is_not_reclassified_or_replanned_as_unsubmitted() -> None:
    class UncertainCase(PreparationCase):
        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: object) -> HarnessToolCallResult:
            self.calls.append(request)
            raise TimeoutError("PRIVATE_INVOKED_UNCERTAINTY")

    case = UncertainCase(repair=True)
    case.proposals = 1
    events = await case.collect()
    assert len(case.calls) == 1 and len(case.requests) == 2
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload.get("discussion_failure_phase") != "tool_preparation"
    assert not any(e.kind == "tool.preparation_rejected" for e in events)


async def test_budget_stop_with_unresolved_rejection_cannot_report_runtime_completed() -> None:
    case = PreparationCase(repair=True)
    case.runtime._plan = replace(case.runtime._plan, token_budget=4)
    case.ctx = case.ctx.model_copy(update={"token_budget": 4})
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["discussion_failure_phase"] == "tool_preparation"
    assert len(case.requests) == 2 and not case.calls


async def test_corrective_prose_cannot_replace_an_unsubmitted_tool_or_buy_another_model() -> None:
    class TextCorrection(PreparationCase):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if request.logical_model == "shared" and self.proposals == 1:
                self.requests.append(request)
                self.proposals += 1
                return GatewayCompletion(response=ModelResponse(text="[COMPLETE] pretend result",
                    usage=TokenUsage(1, 1, 2)), deployment_id="shared", logical_model="shared",
                    provider_id="mock", provider_model="mock/local", cost_usd=Decimal(0))
            return await super().complete_with_context(request)

    case = TextCorrection(repair=True)
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["discussion_failure_phase"] == "tool_preparation"
    assert len(case.requests) == 3 and not case.calls
    assert not any(e.kind is EventKind.MESSAGE_CREATED for e in events)


async def test_corrective_alternate_authorized_tool_is_rejected_by_current_same_tool_boundary() -> None:
    class AlternateCorrection(PreparationCase):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            result = await super().complete_with_context(request)
            if self.proposals == 2 and result.response.tool_calls:
                return replace(result, response=replace(result.response, tool_calls=(
                    ToolCall(id="Call_alternate", name="workspace_read", arguments={"path": "safe.txt"}),
                )))
            return result

    case = AlternateCorrection(repair=True)
    actor = replace(case.runtime._plan.participants[0], allowed_tools=("web.search", "workspace.read"))
    case.runtime = AutoGenDiscussionRuntime(case,
        replace(case.runtime._plan, participants=(actor, case.runtime._plan.participants[1])),
        capability_gateway=cast(CapabilityGateway, case), harness_tool_gateway=case)
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["discussion_failure_phase"] == "tool_preparation"
    assert len(case.requests) == 3 and not case.calls
    assert not any(e.kind is EventKind.TOOL_STARTED for e in events)


async def test_multiple_unsubmitted_calls_do_not_open_a_corrective_batch_or_fake_tool_outcome() -> None:
    class BatchCase(PreparationCase):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            result = await super().complete_with_context(request)
            if result.response.tool_calls:
                call = result.response.tool_calls[0]
                return replace(result, response=replace(result.response, tool_calls=(
                    call, replace(call, id="Call_second"),
                )))
            return result

    case = BatchCase(repair=True)
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["discussion_failure_phase"] == "tool_preparation"
    assert len(case.requests) == 2 and not case.calls
    assert not any(e.kind in {EventKind.TOOL_STARTED, EventKind.TOOL_COMPLETED} for e in events)
    assert not any(e.kind == "tool.preparation_rejected" for e in events)


@pytest.mark.parametrize("second_tool", ["web_search", "workspace_read"])
async def test_corrective_response_cannot_dispatch_extra_calls_after_first_valid_tool(second_tool: str) -> None:
    class CorrectiveBatch(PreparationCase):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            result = await super().complete_with_context(request)
            if self.proposals == 2 and result.response.tool_calls:
                first = result.response.tool_calls[0]
                return replace(result, response=replace(result.response, tool_calls=(
                    first, ToolCall(id="Call_extra", name=second_tool,
                                    arguments={"query": "another", "path": "safe.txt"}),
                )))
            return result

    case = CorrectiveBatch(repair=True)
    actor = replace(case.runtime._plan.participants[0], allowed_tools=("web.search", "workspace.read"))
    case.runtime = AutoGenDiscussionRuntime(case,
        replace(case.runtime._plan, participants=(actor, case.runtime._plan.participants[1])),
        capability_gateway=cast(CapabilityGateway, case), harness_tool_gateway=case)
    events = await case.collect()
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["discussion_failure_phase"] == "tool_preparation"
    assert len(case.requests) == 3 and not case.calls
    assert not any(e.kind is EventKind.TOOL_STARTED for e in events)
