"""Owned recovery accounting must not turn past consumption into fresh credit."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.streams import closing_runtime_events
from tests.unit.runtime.test_adaptive_token_budget_propagation import offline_only
from tests.unit.runtime.test_hybrid import UsageRecordingRuntime, artifact

__all__ = ["offline_only"]


def stages() -> tuple[UsageRecordingRuntime, UsageRecordingRuntime, UsageRecordingRuntime]:
    return (
        UsageRecordingRuntime(TaskMode.DISPATCH, artifact("dispatch", "owned dispatch"),
                              tokens_used=400),
        UsageRecordingRuntime(TaskMode.DISCUSS, artifact("discussion", "owned discussion"),
                              tokens_used=300),
        UsageRecordingRuntime(TaskMode.DIRECT, artifact("synthesis", "owned synthesis"),
                              tokens_used=200),
    )


async def paused() -> tuple[TaskContext, RuntimeCheckpoint, InMemoryArtifactRepository]:
    task = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
                       request="Owned recovery fixture.", token_budget=1000)
    repository = InMemoryArtifactRepository()
    runtime = HybridRuntime(*stages(), artifact_repository=repository)
    async with closing_runtime_events(runtime.run(task)) as stream:
        async for event in stream:
            checkpoint = event.checkpoint
            if (checkpoint is not None and checkpoint.state["terminal"] is False
                    and checkpoint.state["next_stage"] == 1
                    and checkpoint.state.get("child_checkpoint") is None):
                return task, checkpoint, repository
    raise AssertionError("own pause unavailable")


@pytest.mark.parametrize("limit,completed", ((600, False), (800, False), (1000, True), (2000, True)))
async def test_resume_keeps_historical_spend_and_does_not_regrant(
    limit: int, completed: bool,
) -> None:
    task, checkpoint, repository = await paused()
    original = checkpoint.to_payload()
    dispatch, discussion, synthesis = stages()
    runtime = HybridRuntime(dispatch, discussion, synthesis, artifact_repository=repository)
    await runtime.restore_checkpoint(checkpoint)
    resumed = task.model_copy(update={"token_budget": limit, "checkpoint": checkpoint})
    events = [event async for event in runtime.run(resumed)]
    assert events[-1].kind is (
        EventKind.RUNTIME_COMPLETED if completed else EventKind.RUNTIME_FAILED
    )
    assert dispatch.contexts == []
    assert discussion.contexts[0].token_budget == min(limit, 1000) - 400
    if completed:
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        saved = await runtime.save_checkpoint()
        assert saved.state["remaining_token_budget"] == 100
        assert saved.state["token_budget_grant"] == 1000
        assert saved.state["token_budget_spent"] == 900
    assert checkpoint.to_payload() == original


@pytest.mark.parametrize("limit", (300, 400))
async def test_resume_without_new_allowance_never_drives_children(limit: int) -> None:
    task, checkpoint, repository = await paused()
    dispatch, discussion, synthesis = stages()
    runtime = HybridRuntime(dispatch, discussion, synthesis, artifact_repository=repository)
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(task.model_copy(update={
        "token_budget": limit, "checkpoint": checkpoint,
    }))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert "token budget exhausted" in (events[-1].reason or "")
    assert dispatch.contexts == discussion.contexts == synthesis.contexts == []


async def test_version_three_history_is_exact_and_immutable() -> None:
    _, checkpoint, _ = await paused()
    assert checkpoint.runtime_version == "3"
    assert checkpoint.state["token_budget_grant"] == 1000
    assert checkpoint.state["token_budget_spent"] == 400
    assert checkpoint.state["remaining_token_budget"] == 600
    restored = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
    assert restored.state_sha256 == checkpoint.state_sha256


@pytest.mark.parametrize("field,value", (
    ("token_budget_grant", True), ("token_budget_grant", -1),
    ("token_budget_grant", 10_000_001), ("token_budget_spent", True),
    ("token_budget_spent", -1), ("token_budget_spent", 10_000_001),
    ("token_budget_spent", 0), ("remaining_token_budget", 700),
))
async def test_forged_history_denies_before_any_child(field: str, value: JsonValue) -> None:
    task, checkpoint, repository = await paused()
    payload = checkpoint.to_payload()
    state = dict(checkpoint.state)
    state[field] = value
    forged = checkpoint.model_copy(update={"state": state})
    # Recomputed hash does not make internally inconsistent accounting trustworthy.
    forged = forged.model_copy(update={"state_sha256": forged.recompute_state_sha256()})
    dispatch, discussion, synthesis = stages()
    runtime = HybridRuntime(dispatch, discussion, synthesis, artifact_repository=repository)
    await runtime.restore_checkpoint(forged)
    events = [event async for event in runtime.run(task.model_copy(update={"checkpoint": forged}))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert "checkpoint is incompatible" in (events[-1].reason or "")
    assert dispatch.contexts == discussion.contexts == synthesis.contexts == []
    assert checkpoint.to_payload() == payload


@pytest.mark.parametrize("version", ("1", "2"))
async def test_legacy_completed_stage_without_history_never_guesses_credit(version: str) -> None:
    task, checkpoint, repository = await paused()
    state = dict(checkpoint.state)
    state.pop("token_budget_grant")
    state.pop("token_budget_spent")
    if version == "1":
        for key in ("remaining_absolute_timeout_seconds", "timeout_progress_units",
                    "last_child_progress_fingerprint", "child_checkpoint"):
            state.pop(key)
    legacy = checkpoint.model_copy(update={"runtime_version": version, "state": state})
    legacy = legacy.model_copy(update={"state_sha256": legacy.recompute_state_sha256()})
    dispatch, discussion, synthesis = stages()
    runtime = HybridRuntime(dispatch, discussion, synthesis, artifact_repository=repository)
    await runtime.restore_checkpoint(legacy)
    events = [event async for event in runtime.run(task.model_copy(update={"checkpoint": legacy}))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert "token history is unavailable" in (events[-1].reason or "")
    assert dispatch.contexts == discussion.contexts == synthesis.contexts == []
