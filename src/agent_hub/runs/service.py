"""Application service for durable run submission and recovery."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.routing.types import RouteDecision
from agent_hub.runs.repository import RunAlreadyActive, RunRecord, RunRepository
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmittedRun:
    id: UUID
    tenant_id: UUID
    status: RunStatus
    mode: TaskMode | None
    decision_token: str | None
    version: int
    clarification_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    id: UUID
    tenant_id: UUID
    status: RunStatus
    mode: TaskMode | None
    request: str
    completed_step_ids: tuple[str, ...]
    artifact_ids: tuple[UUID, ...]
    usage_cost_usd: Decimal


class TaskQueue(Protocol):
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None: ...


class ModeRouterProtocol(Protocol):
    async def route(self, task_text: object) -> RouteDecision: ...


class RunService:
    """Coordinate run state transitions around durable runtime checkpoints."""

    def __init__(
        self,
        repository: RunRepository,
        *,
        runtime_registry: RuntimeRegistry,
        router: ModeRouterProtocol | None,
        task_queue: TaskQueue,
    ) -> None:
        self._repository = repository
        self._runtime_registry = runtime_registry
        self._router = router
        self._queue = task_queue

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        idempotency_key: str | None = None,
    ) -> SubmittedRun:
        if mode is TaskMode.AUTO:
            decision: RouteDecision | None = None
            if self._router is not None:
                decision = await self._router.route(message)
            if decision is not None and decision.status == "ready":
                assert decision.mode is not None
                record = await self._repository.create_run(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    request=message,
                    mode=decision.mode,
                    status=RunStatus.QUEUED,
                    idempotency_key=idempotency_key,
                    routing_decision=decision.model_dump(mode="json"),
                    enqueue=True,
                )
                return _submitted(record)

            token = (
                _decision_token()
                if decision is None or decision.decision_token is None
                else decision.decision_token
            )
            clarification_reason = (
                "routing_requires_user_choice"
                if decision is None or decision.clarification_reason is None
                else decision.clarification_reason
            )
            record = await self._repository.create_run(
                tenant_id=tenant_id,
                actor_id=actor_id,
                request=message,
                mode=None,
                status=RunStatus.WAITING_USER_MODE,
                idempotency_key=idempotency_key,
                routing_decision={
                    "reason": clarification_reason,
                    "decision_token": token,
                    "decision": None if decision is None else decision.model_dump(mode="json"),
                },
                enqueue=False,
            )
            stored_decision = record.routing_decision or {}
            return SubmittedRun(
                id=record.id,
                tenant_id=record.tenant_id,
                status=record.status,
                mode=record.mode,
                decision_token=str(stored_decision.get("decision_token", token)),
                version=record.version,
                clarification_reason=clarification_reason,
            )

        record = await self._repository.create_run(
            tenant_id=tenant_id,
            actor_id=actor_id,
            request=message,
            mode=mode,
            status=RunStatus.QUEUED,
            idempotency_key=idempotency_key,
            enqueue=True,
        )
        return _submitted(record)

    async def choose_mode(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        mode: TaskMode,
        decision_token: str,
        version: int,
    ) -> SubmittedRun:
        del actor_id
        record = await self._repository.choose_mode_and_enqueue(
            tenant_id=tenant_id,
            run_id=run_id,
            mode=mode,
            decision_token=decision_token,
            version=version,
        )
        return _submitted(record)

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        record = await self._repository.get(tenant_id, run_id)
        return await self._summary(record)

    async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        return await self._repository.events(tenant_id, run_id)

    async def pause(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        record = await self._repository.update_control_status(
            tenant_id, run_id, RunStatus.PAUSED
        )
        return await self._summary(record)

    async def resume(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        record = await self._repository.enqueue_existing_run(
            tenant_id=tenant_id,
            run_id=run_id,
            from_status=RunStatus.PAUSED,
            idempotency_suffix="resume",
        )
        return await self._summary(record)

    async def cancel(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        record = await self._repository.update_control_status(
            tenant_id, run_id, RunStatus.CANCELLED
        )
        return await self._summary(record)

    async def publish_pending(self, limit: int = 100) -> int:
        delivered = 0
        for item in await self._repository.pending_outbox(limit):
            if await self._repository.deliver_outbox(item.id, self._enqueue_outbox_run):
                delivered += 1
        return delivered

    async def _enqueue_outbox_run(self, run_id: UUID, idempotency_key: str) -> None:
        await self._queue.enqueue_run(run_id, idempotency_key=idempotency_key)

    async def execute(
        self,
        run_id: UUID,
        *,
        crash_after_event_kind: EventKind | None = None,
        allow_running_recovery: bool = False,
    ) -> SubmittedRun:
        async with await self._repository.run_transaction() as session, session.begin():
            try:
                claimed = await self._repository.claim_for_execution(
                    session,
                    run_id,
                    allow_running_recovery=allow_running_recovery,
                )
            except RunAlreadyActive:
                active = await self._repository.get_for_update(session, run_id)
                return _submitted(RunRepository._record(active))
            if isinstance(claimed, RunRecord):
                return _submitted(claimed)
            row, checkpoint = claimed
            tenant_id = row.tenant_id
            assert row.mode is not None
            mode = TaskMode(row.mode)
            request = row.request

        terminal = RunStatus.RUNNING
        try:
            runtime = self._runtime_registry.get(mode)
            if checkpoint is not None:
                await runtime.restore_checkpoint(checkpoint)
            context = TaskContext(
                run_id=run_id,
                tenant_id=tenant_id,
                mode=mode,
                request=request,
                checkpoint=checkpoint,
            )
            async for event in runtime.run(context):
                async with await self._repository.run_transaction() as session, session.begin():
                    locked = await self._repository.get_for_update(session, run_id)
                    current_status = RunStatus(locked.status)
                    if current_status is RunStatus.CANCELLED:
                        await runtime.cancel()
                        terminal = RunStatus.CANCELLED
                        break
                    if current_status is RunStatus.PAUSED:
                        terminal = RunStatus.PAUSED
                        break
                    await self._repository.persist_event(
                        session,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        event=event,
                    )
                    if event.kind is EventKind.RUNTIME_COMPLETED:
                        terminal = RunStatus.COMPLETED
                    elif event.kind is EventKind.RUNTIME_CANCELLED:
                        terminal = RunStatus.CANCELLED
                    elif event.kind is EventKind.RUNTIME_FAILED:
                        terminal = RunStatus.FAILED
                    if terminal is not RunStatus.RUNNING:
                        locked.status = terminal.value
                        locked.version += 1
                if crash_after_event_kind is not None and event.kind is crash_after_event_kind:
                    return await self._submitted_by_run_id(tenant_id, run_id)
        except Exception as error:
            _LOGGER.exception(
                "run_execute_failed run_id=%s error_type=%s",
                run_id,
                type(error).__name__,
            )
            return _submitted(await self._repository.fail_run(run_id, reason="runtime_failed"))
        if terminal is RunStatus.RUNNING:
            terminal = RunStatus.COMPLETED
            await self._repository.update_status(tenant_id, run_id, terminal)
        return await self._submitted_by_run_id(tenant_id, run_id)

    async def recover(self, run_id: UUID) -> SubmittedRun:
        return await self.execute(run_id, allow_running_recovery=True)

    async def _submitted_by_run_id(self, tenant_id: UUID, run_id: UUID) -> SubmittedRun:
        return _submitted(await self._repository.get(tenant_id, run_id))

    async def _summary(self, record: RunRecord) -> RunSummary:
        return RunSummary(
            id=record.id,
            tenant_id=record.tenant_id,
            status=record.status,
            mode=record.mode,
            request=record.request,
            completed_step_ids=await self._repository.completed_step_ids(
                record.tenant_id, record.id
            ),
            artifact_ids=await self._repository.artifact_ids(record.tenant_id, record.id),
            usage_cost_usd=await self._repository.usage_cost(record.tenant_id, record.id),
        )


def _submitted(record: RunRecord) -> SubmittedRun:
    decision = record.routing_decision or {}
    reason = str(decision.get("reason", "routing_requires_user_choice"))
    return SubmittedRun(
        id=record.id,
        tenant_id=record.tenant_id,
        status=record.status,
        mode=record.mode,
        decision_token=None
        if record.status is not RunStatus.WAITING_USER_MODE
        else str(decision.get("decision_token", "")),
        version=record.version,
        clarification_reason=None
        if record.status is not RunStatus.WAITING_USER_MODE
        else reason,
    )


def _decision_token() -> str:
    return f"decision-{uuid4().hex}{uuid4().hex}"


__all__ = ["RunService", "RunSummary", "SubmittedRun", "TaskQueue"]
