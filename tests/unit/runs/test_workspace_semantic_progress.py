"""Adaptive budgets must reflect information, not fresh observation UUIDs."""

from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import RunService, _adaptive_runtime_events
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import (
    ExecutableFakeRepository,
    SelfRepairCheckpointRepository,
)
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", [
    "workspace.read", "workspace_read", "workspace.list", "read_context",
])
async def test_unchanged_workspace_reads_do_not_replenish_token_budget(tool_name: str) -> None:
    budgets: list[int] = []

    class Runtime:
        mode = TaskMode.DISPATCH

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budgets.append(context.token_budget)
            for index, text in enumerate(("old", "old", "changed", "changed"), 1):
                artifact = Artifact(
                    id=uuid4(), type="tool_result", producer="implementer",
                    content={
                        "tool_name": tool_name, "arguments_sha256": "a" * 64,
                        "result": (
                            {"workspace_files": ({"path": "src/main.ts",
                                                  "sha256": sha256(text.encode()).hexdigest()},)}
                            if tool_name == "workspace.list" else
                            {"path": "src/main.ts", "text": text, "truncated": False}
                        ),
                    },
                )
                yield RunEvent(
                    kind=EventKind.TOOL_COMPLETED, sequence=index * 2 - 1,
                    run_id=context.run_id, actor="implementer", tool_name=tool_name,
                    tool_call_id=f"read-{index}", artifact=artifact,
                )
                yield RunEvent(
                    kind=EventKind.ARTIFACT_CREATED, sequence=index * 2,
                    run_id=context.run_id, artifact=artifact,
                )
                budgets.append(context.token_budget)

        async def cancel(self) -> None:
            pass

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            raise AssertionError("unused")

        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            raise AssertionError("unused")

    decision: dict[str, JsonValue] = {
        "project_scale": "medium", "critical_path_complexity_units": 4,
    }
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH,
        request="Implement a project", token_budget=1_500_000,
        routing_decision=decision,
    )
    events = [event async for event in _adaptive_runtime_events(
        Runtime(), context, configured_tokens=1_000_000,
        routing_decision=decision, initial_progress_units=0,
    )]

    assert len(events) == 8
    assert budgets == [1_500_000, 1_750_000, 1_750_000, 2_000_000, 2_000_000]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_source", ["checkpoint", "routing", "legacy"])
async def test_restore_seeds_only_checkpoint_bound_observations_without_credit_replay(
    policy_source: str,
) -> None:
    repository = SelfRepairCheckpointRepository(routing_decision={
        "source": "self_repair", "self_repair_accepted": True,
        "project_scale": "medium", "critical_path_complexity_units": 8,
    })
    if policy_source == "routing":
        assert repository.row.routing_decision is not None
        repository.row.routing_decision["runtime_progress_policy"] = "semantic_workspace_v1"
    observations = tuple(Artifact(
        id=uuid4(), type="tool_result", producer="implementer",
        content={"tool_name": "workspace.read", "arguments_sha256": "a" * 64,
                 "result": {"path": "src/main.ts", "text": text, "truncated": False}},
    ) for text in ("old", "old", "changed"))
    # The third event lies after the checkpoint and must not suppress a fresh changed read.
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), runtime_type="test_dispatch", runtime_version="1.0",
        run_id=repository.run_id, tenant_id=repository.checkpoint.tenant_id,
        mode=TaskMode.DISPATCH,
        state={**({"workspace_meaningful_progress": True} if policy_source == "checkpoint" else {}),
               "completed": (), "artifact_registry": {
            str(artifact.id): artifact.content_sha256 for artifact in observations[:2]
        }},
    )
    repository.checkpoint = checkpoint
    repository.event_log.extend(RunEvent(
        kind=EventKind.ARTIFACT_CREATED, sequence=index, run_id=repository.run_id,
        artifact=artifact,
    ) for index, artifact in enumerate(observations, 1))
    budgets: list[int] = []

    class Runtime:
        mode = TaskMode.DISPATCH

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            budgets.append(context.token_budget)
            for index, old in enumerate(observations, 4):
                artifact = old.model_copy(update={"id": uuid4()})
                yield RunEvent(
                    kind=EventKind.TOOL_COMPLETED, sequence=index, run_id=context.run_id,
                    actor="implementer", tool_name="workspace.read", tool_call_id=f"read-{index}",
                    artifact=artifact,
                )
                budgets.append(context.token_budget)
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=7, run_id=context.run_id)

        async def cancel(self) -> None:
            pass

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            return checkpoint

        async def restore_checkpoint(self, restored: RuntimeCheckpoint) -> None:
            assert restored == checkpoint

    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((Runtime(),)), router=None,
        task_queue=object(),  # type: ignore[arg-type]
        runtime_token_budget=1_000_000,
    )
    result = await service.execute(repository.run_id)

    assert result.status is RunStatus.COMPLETED
    assert budgets == (
        [1_750_000, 1_875_000, 2_000_000, 2_125_000] if policy_source == "legacy" else
        [1_625_000, 1_625_000, 1_625_000, 1_750_000]
    )


@pytest.mark.asyncio
async def test_observation_events_without_result_evidence_cannot_claim_progress() -> None:
    class Runtime:
        mode = TaskMode.DISPATCH

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            for index in range(1, 5):
                yield RunEvent(
                    kind=EventKind.TOOL_COMPLETED, sequence=index, run_id=context.run_id,
                    actor="implementer", tool_name="workspace.read", tool_call_id=f"read-{index}",
                    payload={"result_bytes": 20},
                )

        async def cancel(self) -> None:
            pass

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            raise AssertionError("unused")

        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            raise AssertionError("unused")

    decision: dict[str, JsonValue] = {
        "project_scale": "medium", "critical_path_complexity_units": 4,
    }
    context = TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH,
        request="Inspect a project", token_budget=1_500_000, routing_decision=decision,
    )
    events = [event async for event in _adaptive_runtime_events(
        Runtime(), context, configured_tokens=1_000_000,
        routing_decision=decision, initial_progress_units=0,
    )]

    assert len(events) == 4
    assert context.token_budget == 1_500_000


@pytest.mark.asyncio
async def test_new_runs_persist_semantic_budget_policy_before_starting_runtime() -> None:
    repository = ExecutableFakeRepository(routing_decision={"project_scale": "medium"})
    runtime = MultiArtifactRuntime(TaskMode.DISPATCH, (
        Artifact(id=uuid4(), type="text", producer="worker", content={"text": "Delivered"}),
    ))
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((runtime,)), router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    result = await service.execute(repository.run_id)

    assert result.status is RunStatus.COMPLETED
    assert repository.row.routing_decision is not None
    assert repository.row.routing_decision["runtime_progress_policy"] == "semantic_workspace_v1"
    assert runtime.contexts[0].routing_decision["runtime_progress_policy"] == "semantic_workspace_v1"
