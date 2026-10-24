"""Real team final delivery is a separate, billed, no-tool model response."""

from collections.abc import AsyncGenerator, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, TaskContext
from tests.unit.runtime.autogen.test_engineering_completion import (
    Capabilities,
    Gateway,
    context,
    plan,
)

ACTOR = "delivery_main_agent"
REPORT = "Delivered the project archive. Build and tests passed; deployment was not tested."


class ReportGateway(Gateway):
    def __init__(self, *, report: str = REPORT) -> None:
        super().__init__(["workspace.write_text", "written", "project.verify", "checked",
                          "workspace.bundle", "[COMPLETE] writer finished"])
        self.report = report
        self.final_requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if any("Main Agent final delivery" in str(message.content) for message in request.messages
               if message.role == "system"):
            self.requests.append(request)
            self.final_requests.append(request)
            return GatewayCompletion(response=ModelResponse(text=self.report,
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)),
                deployment_id="shared", logical_model=request.logical_model,
                provider_id="test", provider_model="test/model", cost_usd=Decimal(0))
        return await super().complete_with_context(request)


def report_context() -> TaskContext:
    ctx = context()
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "engineering_final_report_version": 1,
    }})


async def test_current_bundle_is_followed_by_durable_actual_main_agent_report() -> None:
    gateway, backend = ReportGateway(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    ctx = report_context()
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert len(gateway.final_requests) == 1
    assert gateway.final_requests[0].tools == ()
    assert gateway.final_requests[0].required_tool is None
    assert gateway.final_requests[0].max_output_tokens < ctx.token_budget
    assert backend.calls == ["workspace.write_text", "project.verify", "workspace.bundle"]
    final = [event.artifact for event in events if event.artifact is not None
             and event.artifact.content.get("presentation") == "final_answer"]
    assert len(final) == 1 and final[0].producer == ACTOR
    assert final[0].content["text"] == REPORT
    completed = next(event for event in events if event.kind == "discussion.completed")
    assert completed.payload["summary"] == REPORT
    assert completed.payload["final_answer_artifact_id"] == str(final[0].id)
    checkpoint = await runner.save_checkpoint()
    assert checkpoint.state["engineering_final_report_version"] == 1
    assert str(final[0].id) in cast(Mapping[str, JsonValue], checkpoint.state["artifact_registry"])
    assert checkpoint.state["model_ledger"]
    unused = ReportGateway()
    restored = AutoGenDiscussionRuntime(unused, replace(plan(), max_turns=16),
        capability_gateway=backend, artifact_repository=runner._repository)
    await restored.restore_checkpoint(checkpoint)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert unused.requests == [] and len(backend.calls) == 3


@pytest.mark.parametrize("text", ["", " \n "])
async def test_blank_main_report_never_promotes_tool_delivery_to_completed(text: str) -> None:
    gateway, backend = ReportGateway(report=text), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert len(gateway.final_requests) == 1


async def test_no_main_report_for_legacy_context() -> None:
    gateway = ReportGateway()
    runner = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=Capabilities())
    events = [event async for event in runner.run(context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert gateway.final_requests == []


async def test_no_main_report_call_when_delivery_evidence_is_missing() -> None:
    gateway = ReportGateway()
    gateway.actions = ["[COMPLETE] planning is not delivery"]
    runner = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=Capabilities())
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert gateway.final_requests == []


async def test_cold_restore_before_public_report_event_does_not_repeat_model_or_tools() -> None:
    gateway, backend = ReportGateway(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    ctx = report_context()
    saved = None
    stream = cast(AsyncGenerator[RunEvent, None], runner.run(ctx))
    try:
        async for event in stream:
            if event.kind is EventKind.MESSAGE_CREATED and event.actor == ACTOR:
                saved = await runner.save_checkpoint()
                assert saved.state["terminal"] is False
                break
    finally:
        await stream.aclose()
    assert saved is not None and len(gateway.final_requests) == 1
    unused = ReportGateway()
    restored = AutoGenDiscussionRuntime(unused, replace(plan(), max_turns=16),
        capability_gateway=backend, artifact_repository=runner._repository)
    await restored.restore_checkpoint(saved)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED, resumed[-1].reason
    assert any(event.artifact is not None and event.artifact.producer == ACTOR for event in resumed)
    assert unused.requests == [] and len(backend.calls) == 3


async def test_failed_final_artifact_storage_cannot_turn_report_observation_into_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AutoGenDiscussionRuntime._store_artifact

    async def fail_report(runner: AutoGenDiscussionRuntime, ctx: TaskContext,
                          artifact: Artifact) -> UUID:
        if artifact.producer == ACTOR:
            raise RuntimeError("test-only final report storage failure")
        return await original(runner, ctx, artifact)

    monkeypatch.setattr(AutoGenDiscussionRuntime, "_store_artifact", fail_report)
    gateway, backend = ReportGateway(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.final_requests) == 1
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_finalizer_uses_selected_main_model_without_relabeling_writer_output() -> None:
    gateway, backend = ReportGateway(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    ctx = report_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision,
                                                      "main_agent_model": "main-final-model"}})
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.final_requests) == 1
    assert gateway.final_requests[0].logical_model == "main-final-model"
    artifact = next(event.artifact for event in events if event.artifact is not None
                    and event.artifact.producer == ACTOR)
    assert cast(Mapping[str, JsonValue], artifact.content["engineering_report"])[
        "logical_model"] == "main-final-model"


async def test_main_model_failure_does_not_promote_writer_or_tool_summary() -> None:
    class FailedReport(ReportGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if any("Main Agent final delivery" in str(message.content) for message in request.messages
                   if message.role == "system"):
                self.final_requests.append(request)
                raise ModelTransportError("test-only unavailable", status_code=503)
            return await super().complete_with_context(request)

    gateway, backend = FailedReport(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.final_requests) == 1 and len(backend.calls) == 3


async def test_no_tool_can_be_requested_by_the_no_tool_finalizer() -> None:
    class HostileReport(ReportGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            result = await super().complete_with_context(request)
            if request in self.final_requests:
                return replace(result, response=replace(result.response, text=None,
                    tool_calls=(ToolCall(id="not-permitted", name="workspace_write_text", arguments={}),)))
            return result

    gateway, backend = HostileReport(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.final_requests) == 1 and len(backend.calls) == 3


async def test_final_report_round_persists_with_checkpoint_ack_enabled() -> None:
    gateway, backend = ReportGateway(), Capabilities()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=backend)
    ctx = report_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision,
                                                      "engineering_checkpoint_ack_version": 1}})
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert len(gateway.final_requests) == 1
    assert any(event.checkpoint is not None and event.checkpoint.state["model_ledger"]
               for event in events if event.kind is EventKind.CHECKPOINT_SAVED)


@pytest.mark.parametrize("cut", ["bundle_outcome", "report_model_outcome"])
async def test_cold_restore_at_native_phase_boundaries_does_not_replay_billed_work(cut: str) -> None:
    gateway, backend = ReportGateway(), Capabilities()
    runtime_plan = replace(plan(), max_turns=16)
    runner = AutoGenDiscussionRuntime(gateway, runtime_plan, capability_gateway=backend)
    ctx = report_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision,
                                                      "engineering_checkpoint_ack_version": 1}})
    saved = None
    stream = cast(AsyncGenerator[RunEvent, None], runner.run(ctx))
    try:
        async for event in stream:
            if saved is not None and (
                cut == "bundle_outcome" and event.kind is EventKind.TOOL_COMPLETED
                and event.tool_name == "workspace.bundle"
                or cut == "report_model_outcome" and event.kind is EventKind.MESSAGE_CREATED
                and event.actor == ACTOR
            ):
                # Resuming after the checkpoint models its committed ACK;
                # closing at the yield itself would instead abort publication.
                break
            if event.kind is not EventKind.CHECKPOINT_SAVED or event.checkpoint is None:
                continue
            state = event.checkpoint.state
            tools = cast(tuple[Mapping[str, JsonValue], ...], state["tool_ledger"])
            models = cast(tuple[Mapping[str, JsonValue], ...], state["model_ledger"])
            if (tools and tools[-1]["name"] == "workspace.bundle" and tools[-1]["status"] == "succeeded"
                    and models and all(row["status"] == "succeeded" for row in models)
                    and bool(gateway.final_requests) == (cut == "report_model_outcome")):
                saved = event.checkpoint
    finally:
        await stream.aclose()
    assert saved is not None and len(backend.calls) == 3
    unused = ReportGateway()
    restored = AutoGenDiscussionRuntime(unused, runtime_plan, capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(saved)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED, resumed[-1].reason
    assert len(backend.calls) == 3
    assert len(unused.requests) == (0 if cut == "report_model_outcome" else 1)
    if cut == "bundle_outcome":
        assert any("project.zip" in str(message.content) and "verification_checks" in str(message.content)
                   for message in unused.final_requests[0].messages)
    assert any(event.artifact is not None and event.artifact.producer == ACTOR for event in resumed)


async def test_valid_gateway_fallback_is_bound_to_requested_and_actual_models() -> None:
    class FallbackReport(ReportGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            result = await super().complete_with_context(request)
            if request in self.final_requests:
                return replace(result, logical_model="backup-final-model", fallback_used=True,
                    fallback_from_logical_model=request.logical_model, fallback_reason="capacity_unavailable")
            return result

    gateway = FallbackReport()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16), capability_gateway=Capabilities())
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    report = next(event.artifact for event in events if event.artifact is not None
                  and event.artifact.producer == ACTOR)
    metadata = cast(Mapping[str, JsonValue], report.content["engineering_report"])
    assert metadata["requested_logical_model"] == "shared"
    assert metadata["logical_model"] == "backup-final-model"
    assert len(gateway.final_requests) == 1


async def test_recovery_republishes_same_report_id_after_original_publication() -> None:
    gateway, backend = ReportGateway(), Capabilities()
    runtime_plan, ctx = replace(plan(), max_turns=16), report_context()
    runner = AutoGenDiscussionRuntime(gateway, runtime_plan, capability_gateway=backend)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    original = next(event.artifact for event in events if event.artifact is not None
                    and event.artifact.producer == ACTOR)
    saved = next(event.checkpoint for event in events if event.checkpoint is not None
                 and event.checkpoint.state["model_ledger"]
                 and cast(tuple[Mapping[str, JsonValue], ...], event.checkpoint.state["model_ledger"])[-1].get(
                     "report_binding") is not None
                 and cast(tuple[Mapping[str, JsonValue], ...], event.checkpoint.state["model_ledger"])[-1][
                     "status"] == "succeeded"
                 and str(original.id) not in cast(Mapping[str, JsonValue],
                                                  event.checkpoint.state["artifact_registry"]))
    assert saved is not None
    unused = ReportGateway()
    restored = AutoGenDiscussionRuntime(unused, runtime_plan, capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await restored.restore_checkpoint(saved)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED, resumed[-1].reason
    repeated = next(event.artifact for event in resumed if event.artifact is not None
                    and event.artifact.producer == ACTOR)
    assert repeated.id == original.id
    assert repeated.content_sha256 == original.content_sha256
    assert unused.requests == [] and len(backend.calls) == 3


async def test_native_offline_fixture_authors_one_write_and_leaves_required_checks_to_driver() -> None:
    from tests.integration.runs.test_discussion_final_report_native import (
        TOOL_CALLS,
        OfflineReportGateway,
        native_plan,
    )

    ctx = report_context()
    ctx = ctx.model_copy(update={"routing_decision": {**ctx.routing_decision,
        "engineering_action_version": 1, "project_id": "fixture", "workspace_session_id": "main"}})
    gateway, backend = OfflineReportGateway(), Capabilities()
    backend.results["project.inspect"] = {
        "schema_version": 1, "scope": {"tenant_id": str(ctx.tenant_id), "run_id": str(ctx.run_id),
            "project_id": "fixture", "workspace_session_id": "main"},
        "manifests": (), "operations": {}, "issues": (),
    }
    runner = AutoGenDiscussionRuntime(gateway, native_plan(), capability_gateway=backend)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert backend.calls == TOOL_CALLS and len(gateway.final_requests) == 1


async def test_service_batches_public_report_with_its_durable_checkpoint() -> None:
    from agent_hub.runs.service import _delivery_publication_batches

    gateway, ctx = ReportGateway(), report_context()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16),
                                     capability_gateway=Capabilities())
    batches = [batch async for batch in _delivery_publication_batches(runner.run(ctx), ctx)]
    flat = [event for batch in batches for event in batch]
    assert flat[-1].kind is EventKind.RUNTIME_COMPLETED
    report_batch = next(batch for batch in batches if any(
        event.kind is EventKind.MESSAGE_CREATED and event.actor == ACTOR for event in batch))
    assert [event.kind for event in report_batch] == [
        EventKind.MESSAGE_CREATED, EventKind.ARTIFACT_CREATED, EventKind.CHECKPOINT_SAVED]
    final_batch = next(batch for batch in batches if any(
        event.kind == "discussion.completed" for event in batch))
    assert [event.kind for event in final_batch] == ["discussion.completed", EventKind.CHECKPOINT_SAVED]


async def test_planning_discussion_without_report_keeps_single_event_transactions() -> None:
    from agent_hub.runs.service import _delivery_publication_batches

    ctx = report_context()
    event = RunEvent(kind="discussion.completed", sequence=1, run_id=ctx.run_id,
                     payload={"summary": "Planning only"})

    async def planning() -> AsyncGenerator[RunEvent, None]:
        yield event

    assert [batch async for batch in _delivery_publication_batches(planning(), ctx)] == [(event,)]


async def test_planning_participant_named_like_final_actor_is_not_a_report() -> None:
    from agent_hub.runs.service import _delivery_publication_batches

    ctx = report_context()
    artifact = Artifact(id=UUID(int=2), type="text", producer=ACTOR,
                        content={"text": "Ordinary planning"})
    events = [RunEvent(kind=EventKind.MESSAGE_CREATED, sequence=1, run_id=ctx.run_id,
                       actor=ACTOR, message="Ordinary planning"),
              RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=2, run_id=ctx.run_id,
                       artifact=artifact)]

    async def planning() -> AsyncGenerator[RunEvent, None]:
        for event in events:
            yield event

    assert [batch async for batch in _delivery_publication_batches(planning(), ctx)] == [
        (event,) for event in events]


async def test_eight_discussion_roles_still_receive_an_independent_final_report() -> None:
    original = plan()
    participants = (*original.participants, *(replace(original.participants[1], id=f"reviewer{index}")
                                             for index in range(2, 8)))
    runtime_plan = replace(original, participants=participants, max_turns=24)
    gateway = ReportGateway()
    runner = AutoGenDiscussionRuntime(gateway, runtime_plan, capability_gateway=Capabilities())
    events = [event async for event in runner.run(report_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    started = next(event for event in events if event.kind is EventKind.DISCUSSION_STARTED)
    assert started.participants == tuple(participant.id for participant in participants)
    assert len(gateway.final_requests) == 1
    assert any(event.artifact is not None and event.artifact.producer == ACTOR for event in events)


@pytest.mark.parametrize("fault", ["eof", "exception", "tool", "foreign", "missing_report"])
async def test_incomplete_report_publication_never_leaks_a_partial_batch(fault: str) -> None:
    from agent_hub.runs.service import _delivery_publication_batches

    ctx, gateway = report_context(), ReportGateway()
    runner = AutoGenDiscussionRuntime(gateway, replace(plan(), max_turns=16),
                                     capability_gateway=Capabilities())
    events = [event async for event in runner.run(ctx)]
    start = next(index for index, event in enumerate(events)
                 if event.kind is EventKind.MESSAGE_CREATED and event.actor == ACTOR)
    saved = next(event.checkpoint for event in reversed(events[:start])
                 if event.checkpoint is not None)
    ctx = ctx.model_copy(update={"checkpoint": saved})
    report = events[start:start + 3]
    assert report[-1].checkpoint is not None
    if fault == "tool":
        report[1] = next(event for event in events if event.kind is EventKind.TOOL_COMPLETED)
    elif fault == "foreign":
        report[-1] = report[-1].model_copy(update={"run_id": UUID(int=1)})
    elif fault == "missing_report":
        report[-1] = next(event for event in events
                          if event.kind is EventKind.CHECKPOINT_SAVED)

    async def broken() -> AsyncGenerator[RunEvent, None]:
        yield report[0]
        if fault == "eof":
            return
        if fault == "exception":
            raise RuntimeError("simulated publication loss")
        yield report[1]
        yield report[2]

    published: list[tuple[RunEvent, ...]] = []
    with pytest.raises(RuntimeError):
        async for batch in _delivery_publication_batches(broken(), ctx):
            published.append(batch)
    assert published == []
