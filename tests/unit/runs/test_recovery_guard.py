from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import (
    RunAlreadyActive,
    RunRecord,
    RunRepository,
    _is_recovery_replayable_event_kind,
    _is_self_repair_recovery_baseline_event_kind,
)
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint


@dataclass(slots=True)
class _FakeRunRow:
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    actor_role: str | None
    request: str
    mode: str | None
    status: str
    version: int
    created_at: datetime
    routing_decision: dict[str, object] | None
    worker_id: str | None = None
    worker_lease_token: UUID | None = None
    worker_lease_expires_at: datetime | None = None
    worker_heartbeat_at: datetime | None = None


class _FakeTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def begin(self) -> Self:
        return self

    async def flush(self) -> None:
        pass


class _AcceptSelfRepairSession(_FakeTransaction):
    def __init__(self, row: _FakeRunRow) -> None:
        self._row = row
        self.added: list[object] = []

    async def scalar(self, statement: object) -> _FakeRunRow:
        del statement
        return self._row

    def add(self, item: object) -> None:
        self.added.append(item)


class _AcceptSelfRepairSessionFactory:
    def __init__(self, row: _FakeRunRow) -> None:
        self._row = row

    def __call__(self) -> _AcceptSelfRepairSession:
        return _AcceptSelfRepairSession(self._row)


class _CapabilityApprovalSession(_FakeTransaction):
    def __init__(self, row: _FakeRunRow, *, approved: bool, rejected: bool = False) -> None:
        self._row = row
        self._approved = approved
        self._rejected = rejected
        self.scalar_calls = 0
        self.added: list[object] = []
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object:
        self.scalar_calls += 1
        self.statements.append(statement)
        if self.scalar_calls == 1:
            return self._row
        return uuid4() if self._approved or self._rejected else None

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)

    def add(self, item: object) -> None:
        self.added.append(item)


class _CapabilityApprovalSessionFactory:
    def __init__(self, session: _CapabilityApprovalSession) -> None:
        self._session = session

    def __call__(self) -> _CapabilityApprovalSession:
        return self._session


class _ScalarRecordingSession:
    def __init__(self, responses: tuple[object, ...]) -> None:
        self._responses = list(responses)
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self._responses.pop(0)


class _AcceptSelfRepairRepository(RunRepository):
    def __init__(self, *, latest_event_sequence: int) -> None:
        self.run_id = uuid4()
        self.tenant_id = uuid4()
        self.latest_event_sequence = latest_event_sequence
        self.row = _FakeRunRow(
            id=self.run_id,
            tenant_id=self.tenant_id,
            actor_id=uuid4(),
            actor_role=None,
            request="repair failed run",
            mode=TaskMode.DISPATCH.value,
            status=RunStatus.FAILED.value,
            version=7,
            created_at=datetime.now(UTC),
            routing_decision={
                "approval_kind": "self_repair",
                "decision_token": "repair-token",
                "repair_proposal": {
                    "kind": "self_repair",
                    "attempt": 1,
                    "max_attempts": 1,
                },
            },
        )
        self._session_factory = cast(Any, _AcceptSelfRepairSessionFactory(self.row))

    async def next_event_sequence(
        self,
        session: Any,
        run_id: UUID,
    ) -> int:
        del session
        assert run_id == self.run_id
        return self.latest_event_sequence + 1


class _RecoveryBlockingRepository(RunRepository):
    def __init__(
        self,
        *,
        status: RunStatus,
        routing_decision: dict[str, object] | None,
        blocked_after_sequence: int,
        blocked_after_kind: str = EventKind.RUNTIME_FAILED.value,
    ) -> None:
        self.run_id = uuid4()
        self.tenant_id = uuid4()
        self.lease_token = uuid4()
        self.lease_expires_at = datetime(2026, 9, 12, 0, 1, tzinfo=UTC)
        self.row = _FakeRunRow(
            id=self.run_id,
            tenant_id=self.tenant_id,
            actor_id=uuid4(),
            actor_role=None,
            request="repair blocked contract",
            mode=TaskMode.DISPATCH.value,
            status=status.value,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision=routing_decision,
        )
        self.checkpoint = RuntimeCheckpoint(
            id=uuid4(),
            runtime_type="test_runtime",
            runtime_version="1.0",
            run_id=self.run_id,
            tenant_id=self.tenant_id,
            mode=TaskMode.DISPATCH,
            state={"step": "draft"},
        )
        self.blocked_after_sequence = blocked_after_sequence
        self.blocked_after_kind = blocked_after_kind
        self.persisted_events: list[RunEvent] = []

    async def get_for_update(self, session: Any, run_id: UUID) -> Any:
        del session
        assert run_id == self.run_id
        return self.row

    async def latest_checkpoint(
        self,
        session: Any,
        *,
        tenant_id: UUID,
        run_id: UUID,
    ) -> RuntimeCheckpoint | None:
        del session
        assert tenant_id == self.tenant_id
        assert run_id == self.run_id
        return self.checkpoint

    async def _recovery_blocked_after_checkpoint(
        self,
        session: Any,
        *,
        run_id: UUID,
        minimum_event_sequence: int = 0,
    ) -> bool:
        del session
        assert run_id == self.run_id
        return self.blocked_after_sequence > 1 and not (
            self.blocked_after_sequence <= minimum_event_sequence
            and _is_self_repair_recovery_baseline_event_kind(self.blocked_after_kind)
        )

    async def next_event_sequence(self, session: Any, run_id: UUID) -> int:
        del session
        assert run_id == self.run_id
        return len(self.persisted_events) + 1

    async def persist_event(
        self,
        session: Any,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        del session
        assert tenant_id == self.tenant_id
        assert run_id == self.run_id
        self.persisted_events.append(event)


def _accepted_self_repair_routing(*, baseline_sequence: int) -> dict[str, object]:
    return {
        "source": "self_repair",
        "self_repair_accepted": True,
        "self_repair_recovery_baseline_sequence": baseline_sequence,
    }


def test_harness_started_is_recovery_replayable_observability() -> None:
    assert _is_recovery_replayable_event_kind("harness.started")


def test_runtime_recovered_is_recovery_replayable_observability() -> None:
    assert _is_recovery_replayable_event_kind("runtime.recovered")


def test_repair_started_is_recovery_replayable_observability() -> None:
    assert _is_recovery_replayable_event_kind("repair.started")


def test_approval_events_are_recovery_replayable_observability() -> None:
    assert _is_recovery_replayable_event_kind(EventKind.APPROVAL_REQUESTED.value)
    assert _is_recovery_replayable_event_kind(EventKind.APPROVAL_RESOLVED.value)


def test_runtime_and_tool_events_remain_recovery_blocking() -> None:
    assert not _is_recovery_replayable_event_kind("runtime.completed")
    assert not _is_recovery_replayable_event_kind("tool.completed")


def test_self_repair_baseline_only_ignores_diagnostic_events() -> None:
    assert _is_self_repair_recovery_baseline_event_kind("runtime.failed")
    assert _is_self_repair_recovery_baseline_event_kind("step.failed")
    assert _is_self_repair_recovery_baseline_event_kind("repair.classified")
    assert _is_self_repair_recovery_baseline_event_kind("observer.notice")
    assert not _is_self_repair_recovery_baseline_event_kind("tool.completed")
    assert not _is_self_repair_recovery_baseline_event_kind("artifact.created")


@pytest.mark.asyncio
async def test_recovery_block_query_ignores_only_baseline_diagnostic_events() -> None:
    repository = RunRepository(cast(Any, None))
    session = _ScalarRecordingSession((1, 4))

    blocked = await repository._recovery_blocked_after_checkpoint(
        cast(Any, session),
        run_id=uuid4(),
        minimum_event_sequence=3,
    )

    assert blocked is True
    assert len(session.statements) == 2
    compiled = str(session.statements[1].compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined, no-untyped-call]
    assert "agent_hub_run_events.sequence <= " in compiled
    assert "agent_hub_run_events.kind IN" in compiled
    assert "agent_hub_run_events.kind NOT IN" in compiled
    assert "NOT (" in compiled
    assert session.statements[1].compile(dialect=postgresql.dialect()).params["sequence_1"] == 3  # type: ignore[attr-defined, no-untyped-call]


@pytest.mark.asyncio
async def test_accept_self_repair_records_current_event_sequence_as_recovery_baseline() -> None:
    repository = _AcceptSelfRepairRepository(latest_event_sequence=3)

    record = await repository.accept_self_repair_and_enqueue(
        tenant_id=repository.tenant_id,
        run_id=repository.run_id,
        decision_token="repair-token",
        version=7,
    )

    assert record.status is RunStatus.QUEUED
    assert record.routing_decision is not None
    assert record.routing_decision["self_repair_accepted"] is True
    assert record.routing_decision["self_repair_recovery_baseline_sequence"] == 3
    assert record.routing_decision["self_repair_decision_token_hash"] == hashlib.sha256(
        b"repair-token"
    ).hexdigest()


@pytest.mark.asyncio
async def test_repeated_self_repair_accept_returns_record_without_duplicate_outbox() -> None:
    token_hash = hashlib.sha256(b"repair-token").hexdigest()
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="repair failed run",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.QUEUED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={
            "source": "self_repair",
            "repair_proposal": {
                "kind": "self_repair",
                "attempt": 1,
                "max_attempts": 1,
                "fingerprint": "repair-fp",
            },
            "self_repair_accepted": True,
            "self_repair_attempt": 1,
            "self_repair_max_attempts": 1,
            "self_repair_source_run_id": "source-run",
            "self_repair_recovery_baseline_sequence": 3,
            "self_repair_fingerprint": "repair-fp",
            "self_repair_decision_token_hash": token_hash,
        },
    )
    session = _AcceptSelfRepairSession(row)
    repository = RunRepository(cast(Any, None))
    repository._session_factory = cast(Any, lambda: session)

    record = await repository.accept_self_repair_and_enqueue(
        tenant_id=row.tenant_id,
        run_id=row.id,
        decision_token="repair-token",
        version=7,
    )

    assert record.status is RunStatus.QUEUED
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_mode_choice_returns_record_without_duplicate_outbox() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="choose a mode",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.QUEUED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={
            "decision_token": "mode-token",
            "selected_mode": TaskMode.DISPATCH.value,
            "operator_note": "dispatch it",
        },
    )
    session = _CapabilityApprovalSession(row, approved=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.choose_mode_and_enqueue(
        tenant_id=row.tenant_id,
        run_id=row.id,
        mode=TaskMode.DISPATCH,
        decision_token="mode-token",
        version=7,
        operator_note="dispatch it",
    )

    assert record.status is RunStatus.QUEUED
    assert record.version == 8
    assert record.mode is TaskMode.DISPATCH
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_capability_approval_returns_record_without_duplicate_outbox() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="needs approved tool",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.QUEUED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={"source": "manual"},
    )
    session = _CapabilityApprovalSession(row, approved=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.approve_capability_and_enqueue(
        tenant_id=row.tenant_id,
        run_id=row.id,
        approval_id="approval-1",
        version=7,
    )

    assert record.status is RunStatus.QUEUED
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_capability_rejection_returns_cancelled_record() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="needs rejected tool",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.CANCELLED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={"source": "manual"},
    )
    session = _CapabilityApprovalSession(row, approved=False, rejected=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.reject_capability_approval(
        tenant_id=row.tenant_id,
        run_id=row.id,
        approval_id="approval-1",
        version=7,
    )

    assert record.status is RunStatus.CANCELLED
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_capability_approval_resolution_returns_record() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="runtime resumed after approved tool boundary",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.RUNNING.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={"source": "manual"},
    )
    session = _CapabilityApprovalSession(row, approved=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.resolve_capability_approval(
        row.tenant_id,
        row.id,
        RunStatus.RUNNING,
        approval_id="approval-1",
        approval_fingerprint="fingerprint-1",
    )

    assert record.status is RunStatus.RUNNING
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_capability_rejection_resolution_returns_record() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="runtime cancelled after rejected tool boundary",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.CANCELLED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={"source": "manual"},
    )
    session = _CapabilityApprovalSession(row, approved=False, rejected=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.resolve_capability_approval(
        row.tenant_id,
        row.id,
        RunStatus.CANCELLED,
        approval_id="approval-1",
        approval_fingerprint="fingerprint-1",
    )

    assert record.status is RunStatus.CANCELLED
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_resume_returns_queued_record_without_duplicate_outbox() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="resume after network retry",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.QUEUED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={"source": "manual"},
    )
    session = _CapabilityApprovalSession(row, approved=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.enqueue_existing_run(
        tenant_id=row.tenant_id,
        run_id=row.id,
        from_status=RunStatus.PAUSED,
        to_status=RunStatus.QUEUED,
        idempotency_suffix="resume",
    )

    assert record.status is RunStatus.QUEUED
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_temporary_agent_approval_returns_record_without_duplicate_outbox() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request="needs temporary specialist",
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.QUEUED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={
            "approval_kind": "temporary_agent_creation",
            "decision_token": "temp-token",
            "temporary_agent_approved": True,
            "temporary_agent_proposal": {"id": "agent-reviewer", "model": "safe-model"},
            "selected_agent_ids": ["agent-reviewer"],
            "temporary_agents": [{"id": "agent-reviewer", "model": "safe-model"}],
        },
    )
    session = _CapabilityApprovalSession(row, approved=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.approve_temporary_agent_and_enqueue(
        tenant_id=row.tenant_id,
        run_id=row.id,
        decision_token="temp-token",
        version=7,
    )

    assert record.status is RunStatus.QUEUED
    assert record.version == 8
    assert session.added == []


@pytest.mark.asyncio
async def test_repeated_temporary_agent_revision_returns_record_without_duplicate_outbox_or_feedback() -> None:
    repository = RunRepository(cast(Any, None))
    row = _FakeRunRow(
        id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        actor_role=None,
        request=(
            "needs temporary specialist"
            "\n\nUser feedback for temporary agent proposal: use a reviewer instead"
        ),
        mode=TaskMode.DISPATCH.value,
        status=RunStatus.QUEUED.value,
        version=8,
        created_at=datetime.now(UTC),
        routing_decision={
            "approval_kind": "temporary_agent_creation",
            "decision_token": "temp-token",
            "temporary_agent_rejected": True,
            "temporary_agent_feedback": "use a reviewer instead",
            "temporary_agents": [],
            "workflow_adjustment_policy": "ask_before_apply",
        },
    )
    session = _CapabilityApprovalSession(row, approved=True)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))

    record = await repository.revise_temporary_agent_and_enqueue(
        tenant_id=row.tenant_id,
        run_id=row.id,
        decision_token="temp-token",
        version=7,
        feedback="use a reviewer instead",
    )

    assert record.status is RunStatus.QUEUED
    assert record.version == 8
    assert row.request.count("User feedback for temporary agent proposal") == 1
    assert session.added == []


@pytest.mark.asyncio
async def test_accepted_self_repair_ignores_events_recorded_before_requeue() -> None:
    repository = _RecoveryBlockingRepository(
        status=RunStatus.QUEUED,
        routing_decision=_accepted_self_repair_routing(baseline_sequence=3),
        blocked_after_sequence=3,
    )

    claimed = await repository.claim_for_execution(
        cast(Any, _FakeTransaction()),
        repository.run_id,
        allow_running_recovery=False,
    )

    assert not isinstance(claimed, RunRecord)
    row, checkpoint = claimed
    assert row.status == RunStatus.RUNNING.value
    assert checkpoint == repository.checkpoint
    assert repository.persisted_events == []


@pytest.mark.asyncio
async def test_claim_for_execution_records_worker_lease() -> None:
    repository = _RecoveryBlockingRepository(
        status=RunStatus.QUEUED,
        routing_decision=None,
        blocked_after_sequence=0,
    )

    claimed = await repository.claim_for_execution(
        cast(Any, _FakeTransaction()),
        repository.run_id,
        allow_running_recovery=False,
        worker_id="worker-alpha",
        worker_lease_token=repository.lease_token,
        worker_lease_expires_at=repository.lease_expires_at,
    )

    assert not isinstance(claimed, RunRecord)
    assert repository.row.worker_id == "worker-alpha"
    assert repository.row.worker_lease_token == repository.lease_token
    assert repository.row.worker_lease_expires_at == repository.lease_expires_at
    assert repository.row.worker_heartbeat_at is not None


@pytest.mark.asyncio
async def test_running_recovery_refuses_unexpired_worker_lease() -> None:
    repository = _RecoveryBlockingRepository(
        status=RunStatus.RUNNING,
        routing_decision=None,
        blocked_after_sequence=0,
    )
    repository.row.worker_id = "worker-active"
    repository.row.worker_lease_token = uuid4()
    repository.row.worker_lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)

    with pytest.raises(RunAlreadyActive):
        await repository.claim_for_execution(
            cast(Any, _FakeTransaction()),
            repository.run_id,
            allow_running_recovery=True,
            worker_id="worker-recovery",
            worker_lease_token=repository.lease_token,
            worker_lease_expires_at=repository.lease_expires_at,
        )


def test_running_for_recovery_requires_expired_worker_lease() -> None:
    repository = RunRepository(cast(Any, None))
    now = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)

    statement = repository._running_for_recovery_select(limit=5, now=now)

    compiled = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "agent_hub_runs.worker_lease_expires_at IS NOT NULL" in compiled
    assert "agent_hub_runs.worker_lease_expires_at <=" in compiled
    assert "agent_hub_runs.worker_lease_expires_at IS NULL" not in compiled
    assert statement.compile(dialect=postgresql.dialect()).params["worker_lease_expires_at_1"] == now  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_accepted_self_repair_blocks_events_recorded_after_requeue() -> None:
    repository = _RecoveryBlockingRepository(
        status=RunStatus.RUNNING,
        routing_decision=_accepted_self_repair_routing(baseline_sequence=3),
        blocked_after_sequence=4,
    )

    claimed = await repository.claim_for_execution(
        cast(Any, _FakeTransaction()),
        repository.run_id,
        allow_running_recovery=True,
    )

    assert isinstance(claimed, RunRecord)
    assert claimed.status is RunStatus.FAILED
    assert [event.kind for event in repository.persisted_events] == [EventKind.RUNTIME_FAILED]


@pytest.mark.asyncio
async def test_accepted_self_repair_still_blocks_prior_tool_side_effect() -> None:
    repository = _RecoveryBlockingRepository(
        status=RunStatus.QUEUED,
        routing_decision=_accepted_self_repair_routing(baseline_sequence=3),
        blocked_after_sequence=2,
        blocked_after_kind=EventKind.TOOL_COMPLETED.value,
    )

    claimed = await repository.claim_for_execution(
        cast(Any, _FakeTransaction()),
        repository.run_id,
        allow_running_recovery=False,
    )

    assert isinstance(claimed, RunRecord)
    assert claimed.status is RunStatus.FAILED
