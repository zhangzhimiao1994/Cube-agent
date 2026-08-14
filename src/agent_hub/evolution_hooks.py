"""Runtime hooks for Evolution execution runs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agent_hub.domain.runs import RunStatus, TaskMode


class EvolutionExecutionIngestService(Protocol):
    async def ingest_evolution_execution_run(
        self,
        run_id: str,
        execution_run_id: UUID,
        *,
        actor: str,
    ) -> object: ...


class EvolutionExecutionIngestHook:
    """Best-effort bridge from completed execution runs to Evolution rounds."""

    def __init__(self, service: EvolutionExecutionIngestService, *, fallback_actor: str = "system:evolution-worker") -> None:
        self._service = service
        self._fallback_actor = fallback_actor

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None,
        run_id: UUID,
        status: RunStatus,
        mode: TaskMode | None,
        routing_decision: dict[str, object],
    ) -> None:
        del tenant_id, mode
        if status is not RunStatus.COMPLETED:
            return
        if str(routing_decision.get("source") or "") != "evolution":
            return
        evolution_run_id = str(routing_decision.get("evolution_run_id") or "").strip()
        if not evolution_run_id:
            return
        await self._service.ingest_evolution_execution_run(
            evolution_run_id,
            run_id,
            actor=str(actor_id) if actor_id is not None else self._fallback_actor,
        )


__all__ = ["EvolutionExecutionIngestHook", "EvolutionExecutionIngestService"]
