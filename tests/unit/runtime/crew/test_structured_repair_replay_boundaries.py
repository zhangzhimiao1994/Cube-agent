"""In-memory checkpoint boundaries, not DB-acknowledged or process-crash recovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    _context,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import (
    RepairCaptureGateway,
    assert_private_rejection,
)

INVALID_WORKER = "PRIVATE_BOUNDARY_WORKER_OUTPUT"
INVALID_REVIEW = "PRIVATE_BOUNDARY_REVIEW_OUTPUT"
CORRECTED = '{"summary":"actual corrected candidate"}'


def mapping(value: object) -> Mapping[str, Any]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, Any], value)


async def captured_correction_checkpoint(
    phase: str, native_rejection: bool,
) -> tuple[RuntimeCheckpoint, InMemoryArtifactRepository, RepairCaptureGateway]:
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        (INVALID_WORKER, 129, native_rejection),
        (CORRECTED, 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("actual final response", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    for event in events:
        checkpoint = event.checkpoint
        if checkpoint is None:
            continue
        repairs = mapping(checkpoint.state["structured_repairs"])
        if "draft" not in repairs:
            continue
        link = mapping(repairs["draft"])
        models = mapping(checkpoint.state["models"])
        correction = mapping(models[link["correction_key"]])
        if correction["status"] != phase:
            continue
        assert link["status"] == ("reserved" if phase == "prepared" else "running")
        assert correction["request_sha256"] == link["correction_request_sha256"]
        assert correction["call_index"] == 1 and correction["actor"] == "writer"
        assert correction["purpose"] == "step" and correction["artifact_id"] is None
        assert mapping(models[link["source_key"]])["status"] == "rejected"
        assert mapping(checkpoint.state["usage"])["tokens"] == 129
        assert checkpoint.state["completed"] == () and len(models) == 2
        return checkpoint, repository, gateway
    pytest.fail(f"successful capture did not emit correction {phase} checkpoint")


@pytest.mark.parametrize("native_rejection", [False, True])
async def test_prepared_correction_replay_submits_reserved_call_once_without_recharging_source(
    native_rejection: bool,
) -> None:
    checkpoint, repository, captured = await captured_correction_checkpoint(
        "prepared", native_rejection,
    )
    original_payload = checkpoint.to_payload()
    link = mapping(mapping(checkpoint.state["structured_repairs"])["draft"])
    gateway = RepairCaptureGateway(
        (CORRECTED, 17, False), ('{"verdict":"approve"}', 8, False), ("actual final response", 11, False),
    )
    replay = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    await replay.restore_checkpoint(checkpoint)
    events = [event async for event in replay.run(_context(checkpoint=checkpoint))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [request.logical_model for request in gateway.requests] == ["general", "review", "general"]
    correction = gateway.requests[0]
    assert correction.response_schema is not None
    assert correction.response_schema == captured.requests[1].response_schema
    assert correction.messages == captured.requests[1].messages
    assert correction.max_output_tokens == link["max_output_tokens"]
    assert correction.tools == () and correction.allow_fallback is False
    assert CrewDispatchRuntime._model_request_sha256(correction) == link["correction_request_sha256"]

    restored = await replay.save_checkpoint()
    restored_link = mapping(mapping(restored.state["structured_repairs"])["draft"])
    assert restored_link["correction_key"] == link["correction_key"]
    assert restored_link["source_key"] == link["source_key"]
    assert restored_link["status"] == "succeeded"
    models = mapping(restored.state["models"])
    assert len(models) == 4
    assert models[link["source_key"]] == mapping(checkpoint.state["models"])[link["source_key"]]
    assert restored.state["rejected_outputs"] == checkpoint.state["rejected_outputs"]
    assert mapping(restored.state["usage"])["tokens"] == 129 + 17 + 8 + 11
    assert mapping(mapping(restored.state["step_usage"])["draft"])["tokens"] == 129 + 17 + 8
    assert checkpoint.to_payload() == original_payload
    assert_private_rejection(events, INVALID_WORKER)


@pytest.mark.parametrize("native_rejection", [False, True])
async def test_running_correction_replay_is_uncertain_with_zero_calls(native_rejection: bool) -> None:
    checkpoint, repository, _ = await captured_correction_checkpoint("running", native_rejection)
    original_payload = checkpoint.to_payload()
    gateway = RepairCaptureGateway()
    replay = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="model outcome requires confirmation"):
        await replay.restore_checkpoint(checkpoint)
        async for event in replay.run(_context(checkpoint=checkpoint)):
            events.append(event)
    assert gateway.requests == []
    assert not any(event.kind in {
        EventKind.STEP_COMPLETED, EventKind.REVIEW_COMPLETED,
        EventKind.RUNTIME_COMPLETED, EventKind.COST_RECORDED,
    } for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    assert mapping((await replay.save_checkpoint()).state["usage"])["tokens"] == 129
    assert checkpoint.to_payload() == original_payload


def assert_review_correction(gateway: RepairCaptureGateway) -> None:
    review, correction = gateway.requests[1:3]
    assert review.logical_model == correction.logical_model == "review"
    assert review.response_schema == correction.response_schema == adapter._REVIEW_RESPONSE_SCHEMA
    assert correction.tools == () and correction.allow_fallback is False


def assert_no_completion_or_downstream(events: list[RunEvent]) -> None:
    assert not any(event.kind in {EventKind.STEP_COMPLETED, EventKind.RUNTIME_COMPLETED} for event in events)
    assert not any(event.step_id == "final_response" for event in events)
    assert not any(event.kind is EventKind.REVIEW_COMPLETED and event.payload["verdict"] == "approve"
                   for event in events)


@pytest.mark.parametrize("native_rejection", [False, True])
async def test_corrected_reviewer_reject_is_not_approval_and_blocks_downstream(
    native_rejection: bool,
) -> None:
    gateway = RepairCaptureGateway(
        ('{"summary":"actual candidate"}', 129, False),
        (INVALID_REVIEW, 8, native_rejection),
        ('{"verdict":"reject","feedback":"candidate is unacceptable"}', 17, False),
    )
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=InMemoryArtifactRepository(),
        crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="review rejected a step"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert len(gateway.requests) == 3
    assert_review_correction(gateway)
    assert_no_completion_or_downstream(events)
    reviews = [event for event in events if event.kind is EventKind.REVIEW_COMPLETED]
    assert [event.payload["verdict"] for event in reviews] == ["reject"]
    checkpoint = await runtime.save_checkpoint()
    repairs = mapping(checkpoint.state["structured_repairs"])
    assert set(repairs) == {"draft"}
    link = mapping(repairs["draft"])
    assert link["actor"] == "reviewer" and link["purpose"] == "review"
    assert link["status"] == "succeeded"
    assert link["candidate_artifact_id"] == reviews[0].payload["candidate_artifact_id"]
    assert mapping(checkpoint.state["usage"])["tokens"] == 129 + 8 + 17
    assert len(mapping(checkpoint.state["models"])) == 3
    assert len(mapping(checkpoint.state["rejected_outputs"])) == 1
    assert_private_rejection(events, INVALID_REVIEW)


@pytest.mark.parametrize("native_rejection", [False, True])
async def test_corrected_reviewer_revise_spends_shared_slot_before_invalid_worker_revision(
    native_rejection: bool,
) -> None:
    gateway = RepairCaptureGateway(
        ('{"summary":"actual candidate"}', 129, False),
        (INVALID_REVIEW, 8, native_rejection),
        ('{"verdict":"revise","feedback":"improve the candidate"}', 17, False),
        (INVALID_WORKER, 10, native_rejection),
    )
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(reviewer_retries=1),
        artifact_repository=InMemoryArtifactRepository(), crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured correction allowance exhausted"):
        async for event in runtime.run(_context()):
            events.append(event)
    assert [request.logical_model for request in gateway.requests] == ["general", "review", "review", "general"]
    assert_review_correction(gateway)
    assert gateway.requests[3].response_schema == gateway.requests[0].response_schema
    assert gateway.requests[3].response_schema is not None
    assert_no_completion_or_downstream(events)
    assert [event.payload["verdict"] for event in events if event.kind is EventKind.REVIEW_COMPLETED] == ["revise"]
    assert len([event for event in events if event.kind is EventKind.STEP_RETRYING]) == 1
    checkpoint = await runtime.save_checkpoint()
    repairs = mapping(checkpoint.state["structured_repairs"])
    assert set(repairs) == {"draft"}
    link = mapping(repairs["draft"])
    assert link["actor"] == "reviewer" and link["purpose"] == "review"
    assert link["status"] == "succeeded"
    models = mapping(checkpoint.state["models"])
    assert len(models) == 4
    assert mapping(models[link["correction_key"]])["actor"] == "reviewer"
    revised = [mapping(value) for value in models.values()
               if mapping(value)["actor"] == "writer" and mapping(value)["status"] == "rejected"]
    # Model attempts reserve a recovery slot between business revisions (0 -> 2).
    assert len(revised) == 1 and revised[0]["attempt"] == 2
    assert mapping(checkpoint.state["retries"])["draft"] == 1
    assert len(mapping(checkpoint.state["rejected_outputs"])) == 2
    assert mapping(checkpoint.state["usage"])["tokens"] == 129 + 8 + 17 + 10
    assert_private_rejection(events, INVALID_REVIEW)
    assert_private_rejection(events, INVALID_WORKER)


async def test_framework_reentry_spends_one_shared_correction_and_preserves_tool_ledger() -> None:
    from uuid import UUID

    from agent_hub.models.gateway import GatewayCompletion
    from agent_hub.models.types import ModelRequest
    from agent_hub.runtime.crew.adapter import (
        CrewAgentDefinition,
        CrewLLMBridge,
        CrewTaskDefinition,
    )
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        FakeCapabilities,
        FastGeneration,
        RecordingHarnessToolGateway,
        ToolGateway,
    )

    class ToolThenRepairGateway(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if not self.requests:
                self.requests.append(request)
                return await ToolGateway().complete_with_context(request)
            return await super().complete_with_context(request)

    class ReenteringGeneration(FastGeneration):
        execute_count = 0
        checkpoint_before_reentry: RuntimeCheckpoint | None = None

        async def execute(
            self,
            step_id: str,
            prompt: str,
            bridge: CrewLLMBridge,
            *,
            agent_id: str | None = None,
            storage_scope: tuple[UUID, UUID],
        ) -> str:
            self.execute_count += 1
            assert step_id == "draft" and agent_id == "writer"
            first = await super().execute(
                step_id, prompt, bridge, agent_id=agent_id, storage_scope=storage_scope,
            )
            assert first == CORRECTED
            self.checkpoint_before_reentry = await runtime.save_checkpoint()
            return await super().execute(
                step_id, prompt, bridge, agent_id=agent_id, storage_scope=storage_scope,
            )

    generation = ReenteringGeneration()

    class ReenteringFactory(FastFactory):
        def build(
            self,
            agents: tuple[CrewAgentDefinition, ...],
            tasks: tuple[CrewTaskDefinition, ...],
            *,
            share_crew: bool,
            telemetry_disabled: bool,
        ) -> FastGeneration:
            return generation

    gateway = ToolThenRepairGateway(
        ("unused: first response comes from ToolGateway", 2, False),
        (INVALID_WORKER, 129, True),
        (CORRECTED, 17, False),
        (INVALID_WORKER, 10, True),
    )
    plan = _reviewed_step_plan()
    plan = plan.model_copy(update={
        "agents": (plan.agents[0].model_copy(update={"allowed_tools": ("web.search",)}),
                   *plan.agents[1:]),
        "steps": (plan.steps[0].model_copy(update={"tools": ("web.search",)}), *plan.steps[1:]),
        "allowed_tools": ("web.search",),
    })
    harness = RecordingHarnessToolGateway()
    capabilities = FakeCapabilities()
    runtime = CrewDispatchRuntime(
        gateway, plan, artifact_repository=InMemoryArtifactRepository(),
        crew_factory=ReenteringFactory(), capability_gateway=capabilities,
        harness_tool_gateway=harness,
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="structured correction allowance exhausted"):
        async for event in runtime.run(_context()):
            events.append(event)

    assert generation.execute_count == 1
    before = generation.checkpoint_before_reentry
    assert before is not None
    assert mapping(before.state["usage"])["tokens"] == 2 + 129 + 17
    tools = mapping(before.state["tools"])
    assert len(tools) == 1
    tool = mapping(next(iter(tools.values())))
    assert tool["status"] == "succeeded" and tool["artifact_id"] is not None
    assert len(harness.calls) == 1 and capabilities.calls == []
    assert harness.calls[0][1].tool_name == "web.search"
    assert len([event for event in events if event.kind is EventKind.TOOL_COMPLETED]) == 1
    assert not any(event.kind is EventKind.TOOL_FAILED for event in events)

    assert [request.logical_model for request in gateway.requests] == ["general"] * 4
    original, correction, reentry = gateway.requests[1:]
    assert original.response_schema is not None
    assert original.response_schema == correction.response_schema == reentry.response_schema
    assert gateway.requests[0].tools and reentry.tools
    assert correction.tools == () and correction.allow_fallback is False
    assert_no_completion_or_downstream(events)
    assert not any(event.kind is EventKind.REVIEW_COMPLETED for event in events)
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["tools"] == before.state["tools"]
    assert checkpoint.state["structured_repairs"] == before.state["structured_repairs"]
    repairs = mapping(checkpoint.state["structured_repairs"])
    assert set(repairs) == {"draft"}
    link = mapping(repairs["draft"])
    assert link["status"] == "succeeded" and link["actor"] == "writer"
    models = mapping(checkpoint.state["models"])
    assert len(models) == 4
    assert sorted(mapping(value)["call_index"] for value in models.values()) == [0, 1, 2, 3]
    assert mapping(models[link["source_key"]])["call_index"] == 1
    assert mapping(models[link["correction_key"]])["call_index"] == 2
    assert len(mapping(checkpoint.state["rejected_outputs"])) == 2
    assert mapping(checkpoint.state["usage"])["tokens"] == 2 + 129 + 17 + 10
    assert mapping(mapping(checkpoint.state["step_usage"])["draft"])["tokens"] == 158
    assert_private_rejection(events, INVALID_WORKER)
