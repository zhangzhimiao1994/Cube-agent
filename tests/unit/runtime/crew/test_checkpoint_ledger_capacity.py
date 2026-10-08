from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from tests.integration.runtime.test_crew_adapter import FakeGateway, FastFactory

READ_COUNT = 100
RUN_ID = UUID("00000000-0000-4000-8000-000000000081")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000082")


class LedgerReadGateway(FakeGateway):
    def __init__(self, batch_size: int) -> None:
        super().__init__()
        self.batch_size = batch_size
        self.next_read = 0

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        count = min(self.batch_size, READ_COUNT - self.next_read)
        calls = tuple(
            ToolCall(
                id=f"fixture-read-{index}",
                name=request.tools[0].name,
                arguments={"path": f"fixture/read-{index:03d}.txt"},
            )
            for index in range(self.next_read, self.next_read + count)
        )
        self.next_read += count
        return GatewayCompletion(
            response=ModelResponse(
                text=None if calls else "All synthetic reads completed.",
                tool_calls=calls,
                usage=TokenUsage(1, 1, 2),
            ),
            deployment_id="fixture",
            logical_model=request.logical_model,
            provider_id="fixture",
            provider_model="fixture/read-ledger",
            cost_usd=Decimal("0.001"),
        )


class FixtureReads:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID and run_id == RUN_ID
        assert actor == "implementer" and name == "workspace.read"
        assert idempotency_key
        path = arguments["path"]
        assert isinstance(path, str)
        self.paths.append(path)
        # Synthetic results only: this double never reads a workspace file or database.
        return {"path": path, "content": "short fixture text", "encoding": "utf-8"}

    def is_replay_safe(self, name: str) -> bool:
        return name == "workspace.read"


def ledger_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(
                id="implementer",
                role="implementer",
                goal="Read synthetic ledger fixtures",
                logical_model="general",
                allowed_tools=("workspace.read",),
            ),
        ),
        steps=(
            DispatchStep(
                id="implementer_step",
                agent="implementer",
                task="Read synthetic ledger fixtures and return their completion summary.",
                tools=("workspace.read",),
                final_synthesizer=True,
                token_budget=100_000,
                timeout_seconds=600,
                cost_budget_usd=Decimal(1),
            ),
        ),
        allowed_tools=("workspace.read",),
        total_token_budget=100_000,
        total_timeout_seconds=600,
        total_cost_usd=Decimal(1),
    )


def ledger_context(checkpoint: RuntimeCheckpoint | None = None) -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        actor_id=UUID("00000000-0000-4000-8000-000000000083"),
        actor_role=Role.OPERATOR,
        mode=TaskMode.DISPATCH,
        request="Read synthetic ledger fixtures.",
        checkpoint=checkpoint,
        token_budget=100_000,
        timeout_seconds=600,
    )


@pytest.mark.parametrize("batch_size", (1, 2))
async def test_100_reads_checkpoint_roundtrip_and_restore_without_rebilling(
    batch_size: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryArtifactRepository()
    gateway = LedgerReadGateway(batch_size)
    reads = FixtureReads()
    plan = ledger_plan()
    runtime = CrewDispatchRuntime(
        gateway,
        plan,
        capability_gateway=reads,
        crew_factory=FastFactory(),
        artifact_repository=repository,
    )
    first_failure: dict[str, object] = {}
    make_checkpoint = runtime._make_checkpoint

    def record_checkpoint_boundary(*args: Any, **kwargs: Any) -> RuntimeCheckpoint:
        try:
            return make_checkpoint(*args, **kwargs)
        except Exception as error:
            if not first_failure:
                registry = kwargs.get("artifact_registry", runtime._current_artifact_registry)
                first_failure.update(
                    phase=kwargs["phase"],
                    completed_steps=len(args[3]),
                    tools=len(args[5].states),
                    models=len(args[6].states),
                    artifact_refs=len(registry),
                    completed_reads=len(reads.paths),
                    error_type=type(error).__name__,
                    error_summary=str(error),
                )
            raise

    # Observe the real snapshot constructor without changing its validation or limits.
    monkeypatch.setattr(runtime, "_make_checkpoint", record_checkpoint_boundary)
    events: list[RunEvent] = []
    runtime_failure: str | None = None
    try:
        async for event in runtime.run(ledger_context()):
            events.append(event)
    except RuntimeExecutionError as error:
        runtime_failure = str(error)

    tool_failures = [
        event.reason for event in events if event.kind is EventKind.TOOL_FAILED
    ]
    assert runtime_failure is None, (
        f"runtime_failure={runtime_failure}; first_checkpoint_failure={first_failure}; "
        f"completed_reads={len(reads.paths)}; "
        f"tool_failures={tool_failures}"
    )
    assert not first_failure
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert sum(event.kind is EventKind.TOOL_COMPLETED for event in events) == READ_COUNT
    assert not any(event.kind is EventKind.TOOL_FAILED for event in events)
    assert reads.paths == [f"fixture/read-{index:03d}.txt" for index in range(READ_COUNT)]

    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["phase"] == "completed"
    tools = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["tools"])
    models = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state["models"])
    expected_models = (READ_COUNT + batch_size - 1) // batch_size + 1
    assert len(tools) == READ_COUNT
    assert all(state["status"] == "succeeded" for state in tools.values())
    assert len(models) == expected_models
    assert all(state["status"] == "succeeded" for state in models.values())
    roundtripped = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
    assert roundtripped == checkpoint
    usage_before = cast(Mapping[str, int | str], checkpoint.state["usage"])
    step_usage_before = cast(
        Mapping[str, Mapping[str, int | str]], checkpoint.state["step_usage"],
    )
    assert len(gateway.requests) == expected_models
    assert usage_before["tokens"] == expected_models * 2
    assert Decimal(usage_before["cost_usd"]) == Decimal("0.001") * expected_models

    replay_gateway = LedgerReadGateway(batch_size)
    replay_reads = FixtureReads()
    replay = CrewDispatchRuntime(
        replay_gateway,
        plan,
        capability_gateway=replay_reads,
        crew_factory=FastFactory(),
        artifact_repository=repository,
    )
    hydrated_usage: list[Any] = []
    hydrate_checkpoint = replay._hydrate_checkpoint

    async def record_hydrated_usage(*args: Any, **kwargs: Any) -> Any:
        result = await hydrate_checkpoint(*args, **kwargs)
        hydrated_usage.append(result[4])
        return result

    # Completed restores do not publish a new snapshot; inspect their actual ledger.
    monkeypatch.setattr(replay, "_hydrate_checkpoint", record_hydrated_usage)
    await replay.restore_checkpoint(roundtripped)
    resumed = [event async for event in replay.run(ledger_context(roundtripped))]
    assert [event.kind for event in resumed] == [EventKind.RUNTIME_COMPLETED]
    assert replay_gateway.requests == []
    assert replay_reads.paths == []
    assert len(hydrated_usage) == 1
    restored_usage = hydrated_usage[0]
    assert restored_usage.tokens == usage_before["tokens"]
    assert restored_usage.cost_usd == Decimal(usage_before["cost_usd"])
    assert {
        step_id: {
            "tokens": restored_usage.step_tokens[step_id],
            "cost_usd": str(restored_usage.step_costs_usd[step_id]),
        }
        for step_id in restored_usage.step_tokens
    } == step_usage_before
