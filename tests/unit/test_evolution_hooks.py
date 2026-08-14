from __future__ import annotations

from uuid import UUID

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.evolution_hooks import EvolutionExecutionIngestHook

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


class RecordingEvolutionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest_evolution_execution_run(
        self,
        run_id: str,
        execution_run_id: UUID,
        *,
        actor: str,
    ) -> object:
        self.calls.append(
            {
                "run_id": run_id,
                "execution_run_id": execution_run_id,
                "actor": actor,
            }
        )
        return object()


@pytest.mark.asyncio
async def test_evolution_execution_hook_ingests_completed_bound_execution_run() -> None:
    service = RecordingEvolutionService()
    hook = EvolutionExecutionIngestHook(service)

    await hook(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_id=RUN_ID,
        status=RunStatus.COMPLETED,
        mode=TaskMode.DISPATCH,
        routing_decision={
            "source": "evolution",
            "evolution_run_id": "evolution_abc",
            "evolution_round": 1,
        },
    )

    assert service.calls == [
        {
            "run_id": "evolution_abc",
            "execution_run_id": RUN_ID,
            "actor": str(ACTOR_ID),
        }
    ]


@pytest.mark.asyncio
async def test_evolution_execution_hook_ignores_non_evolution_or_unfinished_runs() -> None:
    service = RecordingEvolutionService()
    hook = EvolutionExecutionIngestHook(service)

    await hook(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_id=RUN_ID,
        status=RunStatus.FAILED,
        mode=TaskMode.DISPATCH,
        routing_decision={"source": "evolution", "evolution_run_id": "evolution_abc"},
    )
    await hook(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_id=RUN_ID,
        status=RunStatus.COMPLETED,
        mode=TaskMode.DISPATCH,
        routing_decision={"source": "chat"},
    )

    assert service.calls == []
