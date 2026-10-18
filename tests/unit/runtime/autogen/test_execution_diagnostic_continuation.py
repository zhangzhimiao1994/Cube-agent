from pathlib import Path

import pytest

from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.domain.runs import TaskMode
from agent_hub.harness import HarnessToolGateway
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.contracts import EventKind
from tests.unit.runtime.crew.test_execution_diagnostic_continuation import (
    DEFECTS,
    TOOLS,
    Policy,
    ProcessBackend,
    ReceiptHarness,
    RepairModel,
    context,
    malformed_receipt,
)


def runtime(model, backend, policy):
    return AutoGenDiscussionRuntime(
        model,
        DiscussionPlan(
            participants=(DiscussionParticipant(
                id="implementer", role="Implementer", goal="Repair", logical_model="general",
                allowed_tools=TOOLS,
            ), DiscussionParticipant(
                id="reviewer", role="Reviewer", goal="Review", logical_model="general",
            )), selector_model="general", max_turns=8,
        ),
        capability_gateway=backend,
        harness_tool_gateway=HarnessToolGateway(backend, policy_gateway=policy),
    )


@pytest.mark.parametrize("failure", ("exit", "timeout"))
async def test_process_failure_reaches_same_model_read_repair_and_reverify(tmp_path: Path, failure: str):
    ctx = context(TaskMode.DISCUSS)
    backend, model = ProcessBackend(tmp_path, failure), RepairModel(autogen=True)
    policy = Policy(ctx)
    runner = runtime(model, backend, policy)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == ["project.verify", "workspace.read", "workspace.write_text", "project.verify"]
    assert model.stage == 5
    assert len(policy.calls) == 4 and backend.results[-1].exit_code == 0
    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failed) == 1 and failed[0].payload.get("artifact_id") is not None
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == failed[0].tool_call_id for event in events)


@pytest.mark.parametrize("failure", ("backend", "unknown", "denied", "waiting"))
async def test_untyped_or_policy_failure_stays_terminal(tmp_path: Path, failure: str):
    ctx = context(TaskMode.DISCUSS)
    backend, model = ProcessBackend(tmp_path, failure), RepairModel(autogen=True)
    policy = Policy(ctx, {
        "denied": CapabilityStatus.DENIED, "waiting": CapabilityStatus.WAITING_APPROVAL,
    }.get(failure, CapabilityStatus.ALLOWED))
    events = [event async for event in runtime(model, backend, policy).run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert "workspace.write_text" not in backend.calls


@pytest.mark.parametrize("defect", DEFECTS)
async def test_invalid_failure_evidence_cannot_open_continuation(tmp_path, defect):
    ctx = context(TaskMode.DISCUSS)
    backend, model, policy = ProcessBackend(tmp_path), RepairModel(autogen=True), Policy(ctx)
    runner = runtime(model, backend, policy)
    runner._tool_gateway = ReceiptHarness(backend, policy, lambda result: malformed_receipt(result, defect))
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert backend.calls == ["project.verify"]


async def test_failed_observation_checkpoint_resumes_without_replaying_process(tmp_path, monkeypatch):
    import asyncio

    from agent_hub.runtime.autogen.adapter import _DiscussionDurability

    ctx = context(TaskMode.DISCUSS)
    backend, model, policy = ProcessBackend(tmp_path), RepairModel(autogen=True), Policy(ctx)
    runner = runtime(model, backend, policy)
    saved = None
    ready = asyncio.Event()
    original = _DiscussionDurability.fail_tool

    async def pause_after_checkpoint(durability, artifact):
        nonlocal saved
        await original(durability, artifact)
        saved = runner._last_checkpoint
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_DiscussionDurability, "fail_tool", pause_after_checkpoint)

    async def consume():
        return [event async for event in runner.run(ctx)]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
    finally:
        await runner.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    monkeypatch.setattr(_DiscussionDurability, "fail_tool", original)
    assert saved is not None
    resumed_model = RepairModel(autogen=True)
    resumed_model.stage = 1
    resumed = runtime(resumed_model, backend, policy)
    resumed._repository = runner._repository
    await resumed.restore_checkpoint(saved)
    events = [event async for event in resumed.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == ["project.verify", "workspace.read", "workspace.write_text", "project.verify"]
