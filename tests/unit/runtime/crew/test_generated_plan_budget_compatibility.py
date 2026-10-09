"""Owned paused checkpoints retain only the exact known legacy step envelope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchPlan
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.streams import closing_runtime_events
from tests.unit.runtime.crew.test_adapter_failure_reason import FastFactory
from tests.unit.runtime.crew.test_default_step_token_envelope import UsageGateway, generated_plan
from tests.unit.runtime.test_adaptive_token_budget_propagation import adaptive_context, offline_only
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact

__all__ = ["offline_only"]


async def paused_legacy(
    mode: TaskMode,
) -> tuple[TaskContext, DispatchPlan, RuntimeCheckpoint, InMemoryArtifactRepository]:
    task = adaptive_context(3_000_000, mode)
    payload = generated_plan(task).to_payload()
    for step in cast(list[dict[str, object]], payload["steps"]):
        step["token_budget"] = 1_000_000
    legacy = DispatchPlan.from_payload(payload)
    repository = InMemoryArtifactRepository()
    crew = CrewDispatchRuntime(
        UsageGateway((2, 2, 2, 2)), legacy,
        artifact_repository=repository, crew_factory=FastFactory(),
    )
    runtime = crew if mode is TaskMode.DISPATCH else HybridRuntime(
        crew,
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("discussion", "owned discussion"),)),
        MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "owned synthesis"),)),
        artifact_repository=repository,
    )
    async with closing_runtime_events(runtime.run(task)) as events:
        async for event in events:
            checkpoint = event.checkpoint
            if checkpoint is None or checkpoint.state["terminal"] is True:
                continue
            if mode is TaskMode.HYBRID:
                child = checkpoint.state.get("child_checkpoint")
                if not isinstance(child, Mapping):
                    continue
                state = child.get("state")
                if not isinstance(state, Mapping) or state.get("usage") != {
                    "tokens": 2, "cost_usd": "0",
                }:
                    continue
            elif checkpoint.state.get("usage") != {"tokens": 2, "cost_usd": "0"}:
                continue
            return task, legacy, checkpoint, repository
    raise AssertionError("owned paused checkpoint unavailable")


@pytest.mark.parametrize("mode", (TaskMode.DISPATCH, TaskMode.HYBRID))
async def test_default_builder_retains_exact_legacy_digest_for_paused_context(mode: TaskMode) -> None:
    task, legacy, checkpoint, repository = await paused_legacy(mode)
    original = checkpoint.to_payload()
    resumed = task.model_copy(update={"checkpoint": checkpoint})
    selected = generated_plan(resumed)
    assert selected.digest == legacy.digest
    assert selected.to_payload() == legacy.to_payload()
    assert checkpoint.to_payload() == original
    assert generated_plan(task).digest != legacy.digest
    gateway = UsageGateway((2, 2, 2))
    crew = CrewDispatchRuntime(
        gateway, selected, artifact_repository=repository, crew_factory=FastFactory(),
    )
    runtime = crew if mode is TaskMode.DISPATCH else HybridRuntime(
        crew,
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("discussion", "owned discussion"),)),
        MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "owned synthesis"),)),
        artifact_repository=repository,
    )
    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(resumed)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3
    assert (await crew.save_checkpoint()).state["usage"] == {"tokens": 8, "cost_usd": "0"}
    assert checkpoint.to_payload() == original


@pytest.mark.parametrize("change", ("digest", "request", "terminal", "hash"))
async def test_unknown_or_terminal_legacy_checkpoint_never_selects_or_calls(change: str) -> None:
    task, legacy, checkpoint, repository = await paused_legacy(TaskMode.DISPATCH)
    original = checkpoint.to_payload()
    state = dict(checkpoint.state)
    if change == "digest":
        state["plan_digest"] = "0" * 64
    if change == "terminal":
        state.update(terminal=True, phase="budget_exhausted")
    altered = RuntimeCheckpoint(
        id=checkpoint.id, runtime_type=checkpoint.runtime_type,
        runtime_version=checkpoint.runtime_version,
        run_id=checkpoint.run_id, tenant_id=checkpoint.tenant_id,
        mode=checkpoint.mode, state=state,
    )
    if change == "hash":
        altered = altered.model_copy(update={"state_sha256": "0" * 64})
    resumed = task.model_copy(update={
        "checkpoint": altered,
        "request": "Different owned request." if change == "request" else task.request,
    })
    gateway = UsageGateway(())
    try:
        selected = generated_plan(resumed)
        assert selected.digest != legacy.digest
        runtime = CrewDispatchRuntime(
            gateway, selected, artifact_repository=repository, crew_factory=FastFactory(),
        )
        with pytest.raises(RuntimeExecutionError):
            await runtime.restore_checkpoint(altered)
    except (ValueError, TypeError):
        assert change == "hash"
    assert gateway.requests == []
    assert checkpoint.to_payload() == original
