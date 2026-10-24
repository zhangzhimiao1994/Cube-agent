from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
from agent_hub.runtime.hybrid import HybridPlan, HybridRuntime, HybridUpgrade
from agent_hub.runtime.streams import closing_runtime_events
from tests.unit.runtime.test_hybrid import (
    FailingRuntime,
    MultiArtifactRuntime,
    UnusedRuntime,
    artifact,
    final_zip_artifact,
)


class DiagnosticFailure(FailingRuntime):
    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=context.run_id,
            reason="model gateway failed: model transport failed (status=503)",
            payload={**runtime_failure_diagnostic_from_reason(
                "model gateway failed: model transport failed (status=503)",
            ), "error_code": "model.transport_failed", "error_stage": "discussion_selector"},
        )


def task(*inputs: Artifact) -> TaskContext:
    return TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
                       request="Resolve the request.", artifacts=inputs)


@pytest.mark.parametrize("producer", ("conversation_history", "conversation_history_compacted",
                                      "retrieval_context", "main"))
async def test_fresh_input_ids_do_not_skip_dispatch_or_become_delivery(producer: str) -> None:
    context_input = artifact(producer, "Private input reference.")
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    runtime = HybridRuntime(dispatch, DiagnosticFailure(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(task(context_input))]
    assert len(dispatch.contexts) == 1
    assert dispatch.contexts[0].artifacts == (context_input,)
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == (
        "hybrid discuss failed: model gateway failed: model transport failed (status=503)"
    )
    assert events[-1].payload["error_code"] == "model.transport_failed"
    assert events[-1].payload["error_stage"] == "discussion_selector"
    assert events[-1].payload["status_code"] == 503
    assert "private_message" not in events[-1].payload
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.artifact is not None for event in events)


@pytest.mark.parametrize("context_input", (
    final_zip_artifact(),
    artifact("worker", "Prior model output.", artifact_type="model_response"),
    artifact("loader", "Retrieved context.", artifact_type="runtime_context"),
    Artifact(id=uuid4(), type="tool_result", producer="reader",
             content={"text": "Prior tool observation."}),
))
async def test_different_input_types_cannot_inherit_delivery(context_input: Artifact) -> None:
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    runtime = HybridRuntime(dispatch, DiagnosticFailure(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(task(context_input))]
    assert len(dispatch.contexts) == 1
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.artifact is not None for event in events)


async def test_status_followup_can_deliver_new_natural_language_without_rebuilding_files() -> None:
    context_input = final_zip_artifact()
    reply = artifact("main", "The current evidence does not establish a completed delivery.")
    runtime = HybridRuntime(MultiArtifactRuntime(TaskMode.DISPATCH, ()),
                            MultiArtifactRuntime(TaskMode.DISCUSS, ()),
                            MultiArtifactRuntime(TaskMode.DIRECT, (reply,)))
    events = [event async for event in runtime.run(task(context_input))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [event.artifact for event in events if event.artifact is not None] == [reply]
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["output_artifact_registry"] == {str(reply.id): reply.content_sha256}


@pytest.mark.parametrize("echo", (False, True))
async def test_success_without_displayable_output_cannot_publish_inputs_or_complete(echo: bool) -> None:
    context_input = artifact("reference_loader", "Private input reference.")
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (context_input,) if echo else ()),
        MultiArtifactRuntime(TaskMode.DISCUSS, ()), MultiArtifactRuntime(TaskMode.DIRECT, ()),
    )
    events = [event async for event in runtime.run(task(context_input))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert not any(event.artifact is not None for event in events)


@pytest.mark.parametrize("output", (
    artifact("worker", "   ", artifact_type="model_response"),
    artifact("worker", "", artifact_type="model_response"),
    Artifact(id=uuid4(), type="tool_result", producer="worker", content={"status": "ok"}),
))
async def test_non_displayable_execution_receipts_do_not_enable_partial_success(output: Artifact) -> None:
    runtime = HybridRuntime(MultiArtifactRuntime(TaskMode.DISPATCH, (output,)),
                            DiagnosticFailure(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(task())]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].payload["error_stage"] == "discussion_selector"


@pytest.mark.parametrize("output", (artifact("worker", "Executed result."), final_zip_artifact()))
async def test_real_execution_output_keeps_partial_compatibility_and_diagnostics(output: Artifact) -> None:
    context_input = artifact("reference_loader", "Private input reference.")
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, (output,))
    runtime = HybridRuntime(dispatch, DiagnosticFailure(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(task(context_input))]
    assert len(dispatch.contexts) == 1
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason in {
        "partial_hybrid_after_discussion_failure", "partial_hybrid_after_final_attachment",
    }
    assert events[-1].payload["error_code"] == "model.transport_failed"
    assert events[-1].payload["error_stage"] == "discussion_selector"
    assert "private_message" not in events[-1].payload
    assert [event.artifact for event in events if event.artifact is not None] == [output]


async def test_explicit_dispatch_upgrade_preserves_transferred_execution_output() -> None:
    output = artifact("worker", "Executed dispatch result.")
    context = task(output)
    dispatch_events = tuple([event async for event in MultiArtifactRuntime(
        TaskMode.DISPATCH, (output,),
    ).run(context.model_copy(update={"mode": TaskMode.DISPATCH}))])
    runtime = HybridRuntime(
        UnusedRuntime(TaskMode.DISPATCH, "must not repeat explicit upgrade"),
        DiagnosticFailure(TaskMode.DISCUSS, "unused"), UnusedRuntime(TaskMode.DIRECT, "unused"),
        plan=HybridPlan(upgrade=HybridUpgrade.DISPATCH_TO_HYBRID),
        dispatch_events=dispatch_events,
    )
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert events[-1].reason == "partial_hybrid_after_discussion_failure"


async def test_explicit_plan_without_same_run_execution_evidence_does_not_skip_dispatch() -> None:
    prior = artifact("worker", "An unrelated run's output is only context.")
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    runtime = HybridRuntime(
        dispatch, DiagnosticFailure(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"), plan=HybridPlan(),
    )
    events = [event async for event in runtime.run(task(prior))]
    assert len(dispatch.contexts) == 1
    assert events[-1].kind is EventKind.RUNTIME_FAILED


async def test_marked_context_cannot_be_upgraded_to_execution_output() -> None:
    context_input = Artifact(id=uuid4(), type="text", producer="any_loader",
                             content={"text": "Private history.",
                                      "trust": "internal_conversation_summary"})
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    runtime = HybridRuntime(
        dispatch, DiagnosticFailure(TaskMode.DISCUSS, "unused"),
        UnusedRuntime(TaskMode.DIRECT, "unused"),
        plan=HybridPlan(upgrade=HybridUpgrade.DISPATCH_TO_HYBRID),
    )
    events = [event async for event in runtime.run(task(context_input))]
    assert len(dispatch.contexts) == 1
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.artifact is not None for event in events)


@pytest.mark.parametrize("mutation", ("foreign_run", "unfinished", "changed_content"))
async def test_dispatch_upgrade_requires_matching_same_run_completed_events(mutation: str) -> None:
    output = artifact("worker", "Executed dispatch result.")
    context = task(output)
    prior_context = context.model_copy(update={
        "mode": TaskMode.DISPATCH,
        "run_id": uuid4() if mutation == "foreign_run" else context.run_id,
    })
    recorded = [event async for event in MultiArtifactRuntime(
        TaskMode.DISPATCH, (output,),
    ).run(prior_context)]
    if mutation == "unfinished":
        recorded.pop()
    if mutation == "changed_content":
        context = task(artifact("worker", "Only a different output is available."))
        context = context.model_copy(update={"run_id": prior_context.run_id})
    dispatch = MultiArtifactRuntime(TaskMode.DISPATCH, ())
    runtime = HybridRuntime(dispatch, DiagnosticFailure(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"), plan=HybridPlan(),
                            dispatch_events=tuple(recorded))
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(dispatch.contexts) == (1 if mutation == "changed_content" else 0)


@pytest.mark.parametrize("produce_output", (False, True))
@pytest.mark.parametrize("legacy_registry", (False, True))
async def test_resume_retains_input_output_boundary_without_replaying_dispatch(
    produce_output: bool, legacy_registry: bool,
) -> None:
    context_input = artifact("reference_loader", "Private input reference.")
    output = artifact("worker", "Executed dispatch result.")
    context = task(context_input)
    repository = InMemoryArtifactRepository()
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (output,) if produce_output else ()),
        UnusedRuntime(TaskMode.DISCUSS, "paused"), UnusedRuntime(TaskMode.DIRECT, "paused"),
        artifact_repository=repository,
    )
    checkpoint = None
    async with closing_runtime_events(runtime.run(context)) as stream:
        async for event in stream:
            if event.checkpoint is not None and event.checkpoint.state["next_stage"] == 1:
                checkpoint = event.checkpoint
                break
    assert checkpoint is not None
    assert checkpoint.state["output_artifact_registry"] == (
        {str(output.id): output.content_sha256} if produce_output else {}
    )
    if legacy_registry:
        state = dict(checkpoint.state)
        state.pop("output_artifact_registry")
        checkpoint = RuntimeCheckpoint(
            id=uuid4(), runtime_type=checkpoint.runtime_type,
            runtime_version=checkpoint.runtime_version, run_id=context.run_id,
            tenant_id=context.tenant_id, mode=context.mode, state=state,
        )
    original = checkpoint.to_payload()
    resumed = HybridRuntime(
        UnusedRuntime(TaskMode.DISPATCH, "already executed"),
        DiagnosticFailure(TaskMode.DISCUSS, "unused"), UnusedRuntime(TaskMode.DIRECT, "unused"),
        artifact_repository=repository,
    )
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(context.model_copy(update={
        "artifacts": (), "checkpoint": checkpoint,
    }))]
    assert events[-1].kind is (
        EventKind.RUNTIME_COMPLETED if produce_output and not legacy_registry
        else EventKind.RUNTIME_FAILED
    )
    assert checkpoint.to_payload() == original
    assert not any(event.artifact == context_input for event in events)
