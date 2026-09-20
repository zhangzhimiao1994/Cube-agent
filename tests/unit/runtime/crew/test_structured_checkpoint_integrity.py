"""Checkpoint linkage regressions using an in-memory repository and capture gateway."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import DispatchPlan
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    _context,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import RepairCaptureGateway


def runtime_for(
    gateway: RepairCaptureGateway, plan: DispatchPlan, repository: InMemoryArtifactRepository,
) -> CrewDispatchRuntime:
    return CrewDispatchRuntime(
        gateway, plan, artifact_repository=repository, crew_factory=FastFactory(),
    )


def checkpoint_with_recomputed_hash(payload: dict[str, Any]) -> RuntimeCheckpoint:
    # Exercise semantic integrity, not the already-covered stale-digest rejection.
    payload["state_sha256"] = ""
    return RuntimeCheckpoint.from_payload(payload)


async def test_missing_worker_repair_link_cannot_reset_shared_correction_allowance() -> None:
    plan = _reviewed_step_plan()
    # Keep request caps stable so this probes linkage, not remaining-token replay.
    plan = plan.model_copy(update={
        "agents": tuple(agent.model_copy(update={"max_output_tokens": 128}) for agent in plan.agents),
    })
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        ("invalid worker", 10, True),
        ('{"summary":"corrected candidate"}', 10, False),
        ('{"verdict":"approve"}', 10, False),
        ("final", 10, False),
    )
    runtime = runtime_for(gateway, plan, repository)
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 4
    checkpoint = next(
        event.checkpoint
        for event in events
        if event.checkpoint is not None
        and isinstance(models := event.checkpoint.state["models"], Mapping)
        and len(models) == 2
        and sorted(cast(Mapping[str, Any], value)["status"] for value in models.values())
        == ["rejected", "succeeded"]
        and not event.checkpoint.state["completed"]
    )
    payload = cast(dict[str, Any], checkpoint.to_payload())
    assert payload["state"]["structured_repairs"]["draft"]["status"] == "succeeded"
    payload["state"]["structured_repairs"] = {}
    damaged = checkpoint_with_recomputed_hash(payload)

    replay_gateway = RepairCaptureGateway(
        ("invalid reviewer", 10, True),
        ('{"verdict":"approve"}', 10, False),
        ("final", 10, False),
    )
    replay = runtime_for(replay_gateway, plan, repository)
    replay_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await replay.restore_checkpoint(damaged)
        async for event in replay.run(_context(checkpoint=damaged)):
            replay_events.append(event)
    assert replay_gateway.requests == []
    assert not any(event.kind is EventKind.STEP_COMPLETED for event in replay_events)


async def test_review_repair_link_must_match_actual_review_artifact_candidate() -> None:
    plan = _reviewed_step_plan()
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        ('{"summary":"actual candidate"}', 10, False),
        ("invalid reviewer", 10, True),
        ('{"verdict":"approve"}', 10, False),
        ("final", 10, False),
    )
    runtime = runtime_for(gateway, plan, repository)
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    payload = cast(dict[str, Any], checkpoint.to_payload())
    state = payload["state"]
    link = state["structured_repairs"]["draft"]
    actual_candidate_id = link["candidate_artifact_id"]
    correction = state["models"][link["correction_key"]]
    review_artifact = next(
        event.artifact for event in events
        if event.artifact is not None and str(event.artifact.id) == correction["artifact_id"]
    )
    assert review_artifact.source_ids[0] == actual_candidate_id
    wrong_candidate_id = state["artifact_refs"]["final_response"]["id"]
    assert wrong_candidate_id != actual_candidate_id
    # Both private references agree, but neither may override the actual response lineage.
    link["candidate_artifact_id"] = wrong_candidate_id
    link["candidate_sha256"] = state["artifact_registry"][wrong_candidate_id]
    state["rejected_outputs"][link["source_key"]]["source_ids"][0] = wrong_candidate_id
    damaged = checkpoint_with_recomputed_hash(payload)

    replay_gateway = RepairCaptureGateway()
    replay = runtime_for(replay_gateway, plan, repository)
    with pytest.raises(RuntimeExecutionError, match="checkpoint"):
        await replay.restore_checkpoint(damaged)
        _ = [event async for event in replay.run(_context(checkpoint=damaged))]
    assert replay_gateway.requests == []


async def test_real_revise_then_approve_completed_checkpoint_replays_without_model_calls() -> None:
    plan = _reviewed_step_plan(reviewer_retries=1)
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        ('{"summary":"first candidate"}', 10, False),
        ('{"verdict":"revise","feedback":"improve"}', 10, False),
        ('{"summary":"revised candidate"}', 10, False),
        ('{"verdict":"approve"}', 10, False),
        ("final", 10, False),
    )
    runtime = runtime_for(gateway, plan, repository)
    events = [event async for event in runtime.run(_context())]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 5
    assert [event.payload["verdict"] for event in events
            if event.kind is EventKind.REVIEW_COMPLETED] == ["revise", "approve"]
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["retries"] == {"draft": 1, "final_response": 0}
    assert cast(Mapping[str, Any], checkpoint.state["usage"])["tokens"] == 50
    before = checkpoint.to_payload()

    replay_gateway = RepairCaptureGateway()
    replay = runtime_for(replay_gateway, plan, repository)
    await replay.restore_checkpoint(checkpoint)
    replay_events = [event async for event in replay.run(_context(checkpoint=checkpoint))]

    assert replay_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert replay_gateway.requests == []
    assert not any(event.kind is EventKind.COST_RECORDED for event in replay_events)
    assert checkpoint.to_payload() == before
