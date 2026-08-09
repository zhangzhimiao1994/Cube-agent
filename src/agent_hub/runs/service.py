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
    conversation_id: str | None = None
    reference_conversation_id: str | None = None


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


@dataclass(frozen=True, slots=True)
class HermesRunAdvice:
    recommended_mode: TaskMode
    confidence: float
    reasons: tuple[str, ...]
    recommended_skills: tuple[str, ...] = ()
    requires_approval: bool = True


@dataclass(frozen=True, slots=True)
class HermesRunOutcome:
    tenant_id: UUID
    actor_id: UUID | None
    run_id: UUID
    status: RunStatus
    mode: TaskMode | None
    workflow_id: str | None
    conversation_id: str | None
    agent_ids: tuple[str, ...]


class HermesAdvisorProtocol(Protocol):
    async def advise(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
    ) -> HermesRunAdvice | None: ...

    async def record_outcome(self, outcome: HermesRunOutcome) -> None: ...


class RunService:
    """Coordinate run state transitions around durable runtime checkpoints."""

    def __init__(
        self,
        repository: RunRepository,
        *,
        runtime_registry: RuntimeRegistry,
        router: ModeRouterProtocol | None,
        task_queue: TaskQueue,
        hermes_advisor: HermesAdvisorProtocol | None = None,
    ) -> None:
        self._repository = repository
        self._runtime_registry = runtime_registry
        self._router = router
        self._queue = task_queue
        self._hermes_advisor = hermes_advisor

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...] = (),
        workflow_id: str | None = None,
        conversation_id: str | None = None,
        reference_conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SubmittedRun:
        effective_conversation_id = conversation_id or f"conv-{uuid4().hex}"
        operator_selection: dict[str, object] = {
            "selected_agent_ids": list(agent_ids),
            "workflow_id": workflow_id,
            "conversation_id": effective_conversation_id,
            "reference_conversation_id": reference_conversation_id,
        }
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
                    routing_decision={
                        **decision.model_dump(mode="json"),
                        **operator_selection,
                    },
                    enqueue=True,
                )
                return _submitted(record)

            hermes_advice = await self._safe_hermes_advice(
                tenant_id=tenant_id,
                actor_id=actor_id,
                message=message,
                mode=mode,
                agent_ids=agent_ids,
                workflow_id=workflow_id,
            )
            if _usable_hermes_advice(hermes_advice):
                assert hermes_advice is not None
                record = await self._repository.create_run(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    request=message,
                    mode=hermes_advice.recommended_mode,
                    status=RunStatus.QUEUED,
                    idempotency_key=idempotency_key,
                    routing_decision={
                        "reason": "hermes_recommendation",
                        "hermes": _hermes_advice_payload(hermes_advice),
                        **operator_selection,
                    },
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
                    **(
                        {"hermes": _hermes_advice_payload(hermes_advice)}
                        if hermes_advice is not None
                        else {}
                    ),
                    **operator_selection,
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
            routing_decision=operator_selection,
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
            actor_id = row.actor_id
            assert row.mode is not None
            mode = TaskMode(row.mode)
            request = row.request
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)

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
            failed = await self._repository.fail_run(run_id, reason="runtime_failed")
            await self._safe_record_hermes_outcome(
                tenant_id=failed.tenant_id,
                actor_id=failed.actor_id,
                run_id=run_id,
                status=failed.status,
                mode=failed.mode,
                routing_decision=failed.routing_decision,
            )
            return _submitted(failed)
        if terminal is RunStatus.RUNNING:
            terminal = RunStatus.COMPLETED
            await self._repository.update_status(tenant_id, run_id, terminal)
        if terminal in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            await self._safe_record_hermes_outcome(
                tenant_id=tenant_id,
                actor_id=actor_id,
                run_id=run_id,
                status=terminal,
                mode=mode,
                routing_decision=routing_decision,
            )
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

    async def _safe_hermes_advice(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
    ) -> HermesRunAdvice | None:
        if self._hermes_advisor is None:
            return None
        try:
            return await self._hermes_advisor.advise(
                tenant_id=tenant_id,
                actor_id=actor_id,
                message=message,
                mode=mode,
                agent_ids=agent_ids,
                workflow_id=workflow_id,
            )
        except Exception:
            _LOGGER.exception("hermes_advice_failed tenant_id=%s", tenant_id)
            return None

    async def _safe_record_hermes_outcome(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None,
        run_id: UUID,
        status: RunStatus,
        mode: TaskMode | None,
        routing_decision: dict[str, object] | None,
    ) -> None:
        if self._hermes_advisor is None:
            return
        decision = routing_decision or {}
        try:
            await self._hermes_advisor.record_outcome(
                HermesRunOutcome(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    run_id=run_id,
                    status=status,
                    mode=mode,
                    workflow_id=_string_or_none(decision.get("workflow_id")),
                    conversation_id=_string_or_none(decision.get("conversation_id")),
                    agent_ids=_string_tuple(decision.get("selected_agent_ids")),
                )
            )
        except Exception:
            _LOGGER.exception("hermes_outcome_record_failed run_id=%s", run_id)


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
        conversation_id=_string_or_none(decision.get("conversation_id")) or f"conv-{record.id}",
        reference_conversation_id=_string_or_none(decision.get("reference_conversation_id")),
    )


def _decision_token() -> str:
    return f"decision-{uuid4().hex}{uuid4().hex}"


def _usable_hermes_advice(advice: HermesRunAdvice | None) -> bool:
    return (
        advice is not None
        and not advice.requires_approval
        and advice.confidence >= 0.75
        and advice.recommended_mode is not TaskMode.AUTO
    )


def _hermes_advice_payload(advice: HermesRunAdvice) -> dict[str, object]:
    return {
        "recommended_mode": advice.recommended_mode.value,
        "confidence": advice.confidence,
        "reasons": list(advice.reasons),
        "recommended_skills": list(advice.recommended_skills),
        "requires_approval": advice.requires_approval,
    }


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


__all__ = [
    "HermesAdvisorProtocol",
    "HermesRunAdvice",
    "HermesRunOutcome",
    "RunService",
    "RunSummary",
    "SubmittedRun",
    "TaskQueue",
]
