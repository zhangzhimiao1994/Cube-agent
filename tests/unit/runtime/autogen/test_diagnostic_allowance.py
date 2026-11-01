"""Diagnostic evidence consumes one bounded opportunity, not delivery credit."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.autogen.project_repair_phase import (
    DIAGNOSTIC_POLICY,
    DIAGNOSTIC_WATERMARK,
    diagnostic_progress,
    validate_diagnostic_transition,
)
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint, TaskContext
from tests.unit.runs.test_project_auto_repair_runtime import _read_repair_fixture
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository
from tests.unit.runtime.diagnostic_action_artifact_typed_fixture import (
    _CheckpointRepository,
    _execute_policy,
    json_map,
    ledger,
    mapping,
    mutable,
    number,
    present,
    sequence,
    string,
)


async def diagnostic_case() -> tuple[
    TaskContext, RuntimeCheckpoint, RuntimeCheckpoint, tuple[Artifact, ...]
]:
    journey, repo, runtime, service, _, source = await _read_repair_fixture()
    runtime.second_fault = "ack_read"
    await service.execute(repo.run_id)
    saved = repo.checkpoint
    refs = tuple(
        ArtifactReference(id=UUID(key), sha256=string(value))
        for key, value in mapping(saved.state["artifact_registry"]).items()
    )
    artifacts = await journey.artifacts.get_many(saved.tenant_id, saved.run_id, refs)
    context = journey.context.model_copy(update={"routing_decision": repo.row.routing_decision})
    return context, source, saved, artifacts


def changed(checkpoint: RuntimeCheckpoint, **updates: object) -> RuntimeCheckpoint:
    state = mutable(checkpoint.to_payload()["state"])
    state.update(updates)
    return RuntimeCheckpoint(
        id=uuid4(),
        runtime_type=checkpoint.runtime_type,
        runtime_version=checkpoint.runtime_version,
        run_id=checkpoint.run_id,
        tenant_id=checkpoint.tenant_id,
        mode=checkpoint.mode,
        state=json_map(state),
    )


@pytest.mark.asyncio
async def test_diagnostic_transition_accepts_first_consumption_and_only_decaying_saved_window() -> (
    None
):
    context, source, saved, artifacts = await diagnostic_case()
    validate_diagnostic_transition(context, source, saved, artifacts)
    assert saved.state[DIAGNOSTIC_WATERMARK] == len(sequence(saved.state["tool_ledger"]))
    next_checkpoint = changed(
        saved,
        remaining_timeout_seconds=max(0, number(saved.state["remaining_timeout_seconds"]) - 0.01),
        remaining_absolute_timeout_seconds=number(saved.state["remaining_absolute_timeout_seconds"])
        - 0.01,
    )
    validate_diagnostic_transition(context, saved, next_checkpoint, artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", ["regrant", "no_decay", "watermark", "flag_drop", "bool_flag", "scope", "usage"]
)
async def test_latest_predecessor_rejects_regrant_and_forged_diagnostic_state(defect: str) -> None:
    context, _, saved, artifacts = await diagnostic_case()
    updates: dict[str, object] = {
        "remaining_timeout_seconds": max(
            0, number(saved.state["remaining_timeout_seconds"]) - 0.01
        ),
        "remaining_absolute_timeout_seconds": number(
            saved.state["remaining_absolute_timeout_seconds"]
        )
        - 0.01,
    }
    if defect == "regrant":
        saved = changed(saved, remaining_timeout_seconds=0.0)
        updates["remaining_timeout_seconds"] = 1.0
    elif defect == "no_decay":
        updates["remaining_timeout_seconds"] = saved.state["remaining_timeout_seconds"]
    elif defect == "watermark":
        updates[DIAGNOSTIC_WATERMARK] = 0
    elif defect == "bool_flag":
        updates[DIAGNOSTIC_POLICY] = True
    elif defect == "scope":
        phase = mutable(mutable(saved.to_payload()["state"])["project_repair_phase"])
        phase["source_scope_sha256"] = "f" * 64
        updates["project_repair_phase"] = phase
    elif defect == "usage":
        updates["usage"] = {"tokens": 0, "cost_usd": "0"}
    candidate = changed(saved, **updates)
    if defect == "flag_drop":
        state = mutable(candidate.to_payload()["state"])
        state.pop(DIAGNOSTIC_POLICY)
        candidate = RuntimeCheckpoint(
            id=candidate.id,
            runtime_type=candidate.runtime_type,
            runtime_version=candidate.runtime_version,
            run_id=candidate.run_id,
            tenant_id=candidate.tenant_id,
            mode=candidate.mode,
            state=json_map(state),
        )
    with pytest.raises(ValueError):
        validate_diagnostic_transition(context, saved, candidate, artifacts)


@pytest.mark.asyncio
async def test_partial_or_unknown_diagnostic_batch_cannot_consume_new_opportunity() -> None:
    context, _, saved, artifacts = await diagnostic_case()
    models, tools = ledger(saved.state["model_ledger"]), ledger(saved.state["tool_ledger"])
    phase = json_map(saved.state["project_repair_phase"])
    assert (
        len(
            diagnostic_progress(
                context, phase=phase, models=models, tools=tools, artifacts=artifacts
            )
        )
        == 1
    )
    unknown = (*tools[:-1], {**tools[-1], "status": "running"})
    assert (
        diagnostic_progress(context, phase=phase, models=models, tools=unknown, artifacts=artifacts)
        == {}
    )
    missing = tuple(item for item in artifacts if str(item.id) != tools[-1]["artifact_id"])
    with pytest.raises(ValueError):
        diagnostic_progress(context, phase=phase, models=models, tools=tools, artifacts=missing)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, True, "1", 2, 1])
@pytest.mark.parametrize("checkpoint", [False, True])
async def test_only_trusted_fresh_submission_enables_diagnostic_allowance(
    marker: object, checkpoint: bool
) -> None:
    route: dict[str, object] = {
        "source": "manual",
        "project_scale": "small",
        "project_delivery": "workspace",
        "runtime_timeout_source": "project_scale_soft_budget",
        "runtime_timeout_seconds": 300.0,
        "runtime_timeout_absolute_seconds": 3600.0,
    }
    if marker is not None:
        route["engineering_diagnostic_submission_version"] = marker
    repository = (_CheckpointRepository if checkpoint else ExecutableFakeRepository)(
        routing_decision=route
    )
    repository.row.mode = TaskMode.DISCUSS.value
    context = await _execute_policy(repository)
    enabled = type(marker) is int and marker == 1 and not checkpoint
    for decision in (context.routing_decision, repository.row.routing_decision):
        assert (DIAGNOSTIC_POLICY in present(decision)) is enabled
