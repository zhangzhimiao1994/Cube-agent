"""Database repository for durable run execution state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import (
    RunApprovalRow,
    RunArtifactRow,
    RunCheckpointRow,
    RunEventRow,
    RunOutboxRow,
    RunRow,
    RunStepRow,
    RunUsageRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    request: str
    mode: TaskMode | None
    status: RunStatus
    version: int
    routing_decision: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class PendingOutbox:
    id: UUID
    run_id: UUID
    idempotency_key: str


class RunNotFound(RuntimeError):
    """Stable missing-run error."""


class RunConflict(RuntimeError):
    """Stable run-state conflict error."""


class RunAlreadyActive(RuntimeError):
    """The run is already owned by another worker."""


_SENSITIVE_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "secret",
        "secret_ref",
        "credential_ref",
        "hidden_reasoning",
        "chain_of_thought",
    }
)


class RunRepository:
    """Persist runs and normalized runtime events behind tenant boundaries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_run(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        request: str,
        mode: TaskMode | None,
        status: RunStatus,
        idempotency_key: str | None,
        routing_decision: dict[str, object] | None = None,
        enqueue: bool,
    ) -> RunRecord:
        run_id = uuid4()
        outbox_id = uuid4()
        outbox_key = f"{tenant_id}:{idempotency_key or run_id}"
        async with self._session_factory() as session, session.begin():
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(RunRow).where(
                        RunRow.tenant_id == tenant_id,
                        RunRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return self._record(existing)
            row = RunRow(
                id=run_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                request=request,
                mode=None if mode is None else mode.value,
                status=status.value,
                idempotency_key=idempotency_key,
                routing_decision=routing_decision,
                version=1,
            )
            session.add(row)
            await session.flush()
            if enqueue:
                session.add(
                    RunOutboxRow(
                        id=outbox_id,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_name="agent_hub.runs.execute",
                        idempotency_key=outbox_key,
                        payload={"run_id": str(run_id)},
                    )
                )
            await session.flush()
            return self._record(row)

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        async with self._session_factory() as session:
            row = await session.scalar(self._run_select(tenant_id, run_id))
            if row is None:
                raise RunNotFound("run was not found")
            return self._record(row)

    async def list_recent(self, tenant_id: UUID, *, limit: int = 100) -> tuple[RunRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("run list limit must be between 1 and 500")
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunRow)
                    .where(RunRow.tenant_id == tenant_id)
                    .order_by(RunRow.created_at.desc(), RunRow.id.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(self._record(row) for row in rows)

    async def get_for_update(self, session: AsyncSession, run_id: UUID) -> RunRow:
        row = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
        if row is None:
            raise RunNotFound("run was not found")
        return row

    async def update_status(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        mode: TaskMode | None = None,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            row.status = status.value
            if mode is not None:
                row.mode = mode.value
            row.version += 1
            await session.flush()
            return self._record(row)

    async def choose_mode_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        mode: TaskMode,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_USER_MODE:
                raise RunConflict("run is not waiting for a mode choice")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("mode choice token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")

            row.mode = mode.value
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:choose-mode:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def approve_temporary_agent_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                raise RunConflict("run is not waiting for temporary agent approval")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if routing_decision.get("approval_kind") != "temporary_agent_creation":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("temporary agent approval token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")
            proposal = routing_decision.get("temporary_agent_proposal")
            if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str):
                raise RunConflict("temporary agent proposal is invalid")
            selected = routing_decision.get("selected_agent_ids")
            selected_agent_ids = [item for item in selected if isinstance(item, str)] if isinstance(selected, list) else []
            proposal_id = proposal["id"]
            if proposal_id not in selected_agent_ids:
                selected_agent_ids.append(proposal_id)
            row.routing_decision = {
                **routing_decision,
                "selected_agent_ids": selected_agent_ids,
                "temporary_agents": [proposal],
                "temporary_agent_approved": True,
            }
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:temporary-agent:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def revise_temporary_agent_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
        feedback: str,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                raise RunConflict("run is not waiting for temporary agent approval")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if routing_decision.get("approval_kind") != "temporary_agent_creation":
                raise RunConflict("run is waiting for a different approval")
            if routing_decision.get("decision_token") != decision_token:
                raise RunConflict("temporary agent revision token is invalid")
            if row.version != version:
                raise RunConflict("run version is stale")
            row.request = f"{row.request}\n\nUser feedback for temporary agent proposal: {feedback}"
            row.routing_decision = {
                **routing_decision,
                "temporary_agent_rejected": True,
                "temporary_agent_feedback": feedback,
                "temporary_agents": [],
                "workflow_adjustment_policy": "ask_before_apply",
            }
            row.status = RunStatus.QUEUED.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:temporary-agent-revision:{version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def enqueue_existing_run(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        from_status: RunStatus,
        to_status: RunStatus = RunStatus.QUEUED,
        idempotency_suffix: str,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not from_status:
                raise RunConflict("run state conflict")
            row.status = to_status.value
            row.version += 1
            await session.flush()
            session.add(
                RunOutboxRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_name="agent_hub.runs.execute",
                    idempotency_key=f"{tenant_id}:{run_id}:{idempotency_suffix}:{row.version}",
                    payload={"run_id": str(run_id)},
                )
            )
            await session.flush()
            return self._record(row)

    async def claim_for_execution(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
    ) -> tuple[RunRow, RuntimeCheckpoint | None] | RunRecord:
        row = await self.get_for_update(session, run_id)
        status = RunStatus(row.status)
        mode = TaskMode(row.mode) if row.mode is not None else None
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return self._record(row)
        if mode is None:
            return self._record(row)
        if status is RunStatus.RUNNING and not allow_running_recovery:
            raise RunAlreadyActive("run is already active")
        if status not in {RunStatus.QUEUED, RunStatus.RETRYING, RunStatus.RUNNING}:
            raise RunConflict("run cannot be executed from its current state")
        row.status = RunStatus.RUNNING.value
        row.version += 1
        checkpoint = await self.latest_checkpoint(session, tenant_id=row.tenant_id, run_id=row.id)
        if allow_running_recovery and status is RunStatus.RUNNING:
            checkpoint_sequence = (
                await session.scalar(
                    select(func.max(RunCheckpointRow.sequence)).where(
                        RunCheckpointRow.run_id == row.id
                    )
                )
                or 0
            )
            latest_event_sequence = await session.scalar(
                select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == row.id)
            )
            if latest_event_sequence is not None and latest_event_sequence > checkpoint_sequence:
                row.status = RunStatus.FAILED.value
                row.version += 1
                await session.flush()
                return self._record(row)
        return row, checkpoint

    async def update_control_status(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            current = RunStatus(row.status)
            if current in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                raise RunConflict("terminal run state is immutable")
            if status is RunStatus.PAUSED and current not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                raise RunConflict("run cannot be paused from its current state")
            if status is RunStatus.CANCELLED and current is RunStatus.CANCELLED:
                raise RunConflict("run state conflict")
            row.status = status.value
            row.version += 1
            await session.flush()
            return self._record(row)

    async def begin_capability_approval(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        approval_id: str,
        approval_fingerprint: str,
    ) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if (
                RunStatus(row.status) is RunStatus.WAITING_APPROVAL
                and routing_decision.get("approval_id") == approval_id
                and routing_decision.get("approval_fingerprint") == approval_fingerprint
            ):
                return self._record(row)
            if RunStatus(row.status) is not RunStatus.RUNNING:
                raise RunConflict("run is not running")
            row.status = RunStatus.WAITING_APPROVAL.value
            row.routing_decision = {
                **routing_decision,
                "approval_id": approval_id,
                "approval_fingerprint": approval_fingerprint,
            }
            row.version += 1
            await session.flush()
            return self._record(row)

    async def resolve_capability_approval(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        approval_id: str,
        approval_fingerprint: str,
    ) -> RunRecord:
        if status not in {RunStatus.RUNNING, RunStatus.CANCELLED}:
            raise RunConflict("approval resolution status is invalid")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(self._run_select(tenant_id, run_id).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            if RunStatus(row.status) is not RunStatus.WAITING_APPROVAL:
                raise RunConflict("run is not waiting for approval")
            routing_decision = {} if row.routing_decision is None else dict(row.routing_decision)
            if (
                routing_decision.get("approval_id") != approval_id
                or routing_decision.get("approval_fingerprint") != approval_fingerprint
            ):
                raise RunConflict("run is waiting for a different approval")
            row.status = status.value
            row.routing_decision = {
                key: value
                for key, value in routing_decision.items()
                if key not in {"approval_id", "approval_fingerprint"}
            }
            row.version += 1
            await session.flush()
            return self._record(row)

    async def fail_run(self, run_id: UUID, *, reason: str) -> RunRecord:
        async with self._session_factory() as session, session.begin():
            row = await self.get_for_update(session, run_id)
            current = RunStatus(row.status)
            if current in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                return self._record(row)
            sequence = (
                await session.scalar(
                    select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == run_id)
                )
                or 0
            ) + 1
            await self.persist_event(
                session,
                tenant_id=row.tenant_id,
                run_id=run_id,
                event=RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=sequence,
                    run_id=run_id,
                    reason=reason,
                ),
            )
            row.status = RunStatus.FAILED.value
            row.version += 1
            await session.flush()
            return self._record(row)

    async def pending_outbox(self, limit: int = 100) -> tuple[PendingOutbox, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunOutboxRow)
                    .where(RunOutboxRow.delivered.is_(False))
                    .order_by(RunOutboxRow.created_at, RunOutboxRow.id)
                    .limit(limit)
                )
            ).all()
            return tuple(
                PendingOutbox(
                    id=row.id,
                    run_id=row.run_id,
                    idempotency_key=row.idempotency_key,
                )
                for row in rows
            )

    async def deliver_outbox(
        self,
        outbox_id: UUID,
        deliver: Callable[[UUID, str], Awaitable[None]],
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(RunOutboxRow)
                .where(RunOutboxRow.id == outbox_id)
                .with_for_update(skip_locked=True)
            )
            if row is None or row.delivered:
                return False
            await deliver(row.run_id, row.idempotency_key)
            row.delivered = True
            row.delivered_at = func.now()
            await session.flush()
            return True

    async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        async with self._session_factory() as session:
            await self._assert_run(session, tenant_id, run_id)
            rows = (
                await session.scalars(
                    select(RunEventRow)
                    .where(RunEventRow.tenant_id == tenant_id, RunEventRow.run_id == run_id)
                    .order_by(RunEventRow.sequence)
                )
            ).all()
            return tuple(_public_event_payload(dict(row.payload)) for row in rows)

    async def completed_step_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[str, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunStepRow.step_id)
                    .where(
                        RunStepRow.tenant_id == tenant_id,
                        RunStepRow.run_id == run_id,
                        RunStepRow.status == "completed",
                    )
                    .order_by(RunStepRow.created_at, RunStepRow.step_id)
                )
            ).all()
            return tuple(rows)

    async def artifact_ids(self, tenant_id: UUID, run_id: UUID) -> tuple[UUID, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunArtifactRow.id)
                    .where(RunArtifactRow.tenant_id == tenant_id, RunArtifactRow.run_id == run_id)
                    .order_by(RunArtifactRow.created_at, RunArtifactRow.id)
                )
            ).all()
            return tuple(rows)

    async def artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunArtifactRow)
                    .where(RunArtifactRow.tenant_id == tenant_id, RunArtifactRow.run_id == run_id)
                    .order_by(RunArtifactRow.created_at, RunArtifactRow.id)
                )
            ).all()
            return tuple(_public_artifact_payload(dict(row.payload)) for row in rows)

    async def usage_cost(self, tenant_id: UUID, run_id: UUID) -> Decimal:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(RunUsageRow.cost_usd).where(
                        RunUsageRow.tenant_id == tenant_id,
                        RunUsageRow.run_id == run_id,
                    )
                )
            ).all()
            return sum((Decimal(value) for value in rows), Decimal("0.00"))

    async def latest_checkpoint(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
    ) -> RuntimeCheckpoint | None:
        row = await session.scalar(
            select(RunCheckpointRow)
            .where(RunCheckpointRow.tenant_id == tenant_id, RunCheckpointRow.run_id == run_id)
            .order_by(RunCheckpointRow.sequence.desc())
            .limit(1)
        )
        if row is None:
            return None
        return RuntimeCheckpoint.from_payload(dict(row.payload))

    async def persist_event(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        payload = event.to_payload()
        await session.execute(
            insert(RunEventRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=event.sequence,
                kind=payload["kind"],
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=[RunEventRow.run_id, RunEventRow.sequence])
        )
        if event.step_id is not None and event.kind is EventKind.STEP_COMPLETED:
            await self._persist_step(session, tenant_id, run_id, event)
        if event.artifact is not None:
            await self._persist_artifact(session, tenant_id, run_id, event.artifact)
        if event.checkpoint is not None:
            await self._persist_checkpoint(session, tenant_id, run_id, event)
        if event.kind in {EventKind.APPROVAL_REQUESTED, EventKind.APPROVAL_RESOLVED}:
            await self._persist_approval(session, tenant_id, run_id, event)
        if event.kind is EventKind.COST_RECORDED:
            await self._persist_usage(session, tenant_id, run_id, event)

    async def run_transaction(self) -> AsyncSession:
        return self._session_factory()

    @staticmethod
    def _record(row: RunRow) -> RunRecord:
        return RunRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            actor_id=row.actor_id,
            request=row.request,
            mode=None if row.mode is None else TaskMode(row.mode),
            status=RunStatus(row.status),
            version=row.version,
            routing_decision=None if row.routing_decision is None else dict(row.routing_decision),
        )

    @staticmethod
    def _run_select(tenant_id: UUID, run_id: UUID) -> Select[tuple[RunRow]]:
        return select(RunRow).where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)

    @staticmethod
    async def _assert_run(session: AsyncSession, tenant_id: UUID, run_id: UUID) -> None:
        exists = await session.scalar(
            select(RunRow.id).where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)
        )
        if exists is None:
            raise RunNotFound("run was not found")

    @staticmethod
    async def _persist_step(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.step_id is not None
        await session.execute(
            insert(RunStepRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                step_id=event.step_id,
                actor=event.actor or "runtime",
                status="completed",
                payload=event.to_payload(),
            )
            .on_conflict_do_nothing(index_elements=[RunStepRow.run_id, RunStepRow.step_id])
        )

    @staticmethod
    async def _persist_artifact(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        artifact: Artifact,
    ) -> None:
        await session.execute(
            insert(RunArtifactRow)
            .values(
                id=artifact.id,
                tenant_id=tenant_id,
                run_id=run_id,
                type=artifact.type,
                producer=artifact.producer,
                content_sha256=artifact.content_sha256,
                payload=artifact.to_payload(),
            )
            .on_conflict_do_nothing(index_elements=[RunArtifactRow.id])
        )

    @staticmethod
    async def _persist_checkpoint(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.checkpoint is not None
        checkpoint = event.checkpoint
        await session.execute(
            insert(RunCheckpointRow)
            .values(
                id=checkpoint.id,
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=event.sequence,
                runtime_type=checkpoint.runtime_type,
                runtime_version=checkpoint.runtime_version,
                mode=checkpoint.mode.value,
                state=checkpoint.to_payload()["state"],
                state_sha256=checkpoint.state_sha256,
                payload=checkpoint.to_payload(),
            )
            .on_conflict_do_nothing(
                index_elements=[RunCheckpointRow.run_id, RunCheckpointRow.sequence]
            )
        )

    @staticmethod
    async def _persist_approval(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.approval_id is not None
        payload = event.to_payload()
        status = "pending" if event.kind is EventKind.APPROVAL_REQUESTED else str(event.decision)
        action = event.action if event.action is not None else "resolved"
        await session.execute(
            insert(RunApprovalRow)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                approval_id=event.approval_id,
                action=action,
                status=status,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=[RunApprovalRow.run_id, RunApprovalRow.approval_id],
                set_={"status": status, "payload": payload},
            )
        )

    @staticmethod
    async def _persist_usage(
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        assert event.provider_id is not None
        assert event.cost_usd is not None
        await session.execute(
            insert(RunUsageRow).values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=event.sequence,
                provider_id=event.provider_id,
                cost_usd=str(event.cost_usd),
                payload=event.to_payload(),
            )
            .on_conflict_do_nothing(index_elements=[RunUsageRow.run_id, RunUsageRow.sequence])
        )


def _public_event_payload(payload: dict[str, object]) -> dict[str, object]:
    return {key: _sanitize_public_json(value) for key, value in payload.items() if _is_public_key(key)}


def _public_artifact_payload(payload: dict[str, object]) -> dict[str, object]:
    return {key: _sanitize_public_json(value) for key, value in payload.items() if _is_public_key(key)}


def _sanitize_public_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_public_json(item)
            for key, item in value.items()
            if _is_public_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_public_json(item) for item in value]
    return value


def _is_public_key(key: str) -> bool:
    lowered = key.lower()
    return not any(sensitive in lowered for sensitive in _SENSITIVE_PUBLIC_KEYS)
