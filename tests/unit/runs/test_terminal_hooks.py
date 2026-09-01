from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.self_repair import SelfRepairPolicy, repair_context_from_proposal
from agent_hub.runs.service import HermesRunOutcome, RunService
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(slots=True)
class FakeRunRow:
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


class FakeTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


    def begin(self) -> FakeTransaction:
        return self


class ExecutableFakeRepository:
    def __init__(self, *, routing_decision: dict[str, object]) -> None:
        self.run_id = uuid4()
        self.row = FakeRunRow(
            id=self.run_id,
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            actor_role=None,
            request="run evolution round",
            mode=TaskMode.DISPATCH.value,
            status=RunStatus.QUEUED.value,
            version=1,
            created_at=datetime.now(UTC),
            routing_decision=routing_decision,
        )
        self.event_log: list[RunEvent] = []
        self.artifacts: list[Artifact] = []
        self.conversation_context_calls: list[dict[str, object]] = []

    async def run_transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def claim_for_execution(
        self,
        session: FakeTransaction,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        del session, allow_running_recovery
        assert run_id == self.run_id
        if self.row.status in {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            return self._record()
        self.row.status = RunStatus.RUNNING.value
        self.row.version += 1
        return self.row, None

    async def get_for_update(self, session: FakeTransaction, run_id: UUID) -> FakeRunRow:
        del session
        assert run_id == self.run_id
        return self.row

    async def persist_event(
        self,
        session: FakeTransaction,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
    ) -> None:
        del session
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        self.event_log.append(event)
        if event.artifact is not None:
            self.artifacts.append(event.artifact)

    async def next_event_sequence(self, session: FakeTransaction, run_id: UUID) -> int:
        del session
        assert run_id == self.run_id
        return max((event.sequence for event in self.event_log), default=0) + 1

    async def update_status(
        self,
        tenant_id: UUID,
        run_id: UUID,
        status: RunStatus,
        *,
        mode: TaskMode | None = None,
    ) -> RunRecord:
        del mode
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        self.row.status = status.value
        self.row.version += 1
        return self._record()

    async def fail_run(self, run_id: UUID, *, reason: str) -> RunRecord:
        assert run_id == self.run_id
        if self.row.status == RunStatus.WAITING_APPROVAL.value:
            return self._record()
        sequence = max((event.sequence for event in self.event_log), default=0) + 1
        self.event_log.append(
            RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=run_id,
                reason=reason,
            )
        )
        self.row.status = RunStatus.FAILED.value
        self.row.version += 1
        return self._record()

    async def record_self_repair_decision(
        self,
        session: FakeTransaction,
        *,
        tenant_id: UUID,
        run_id: UUID,
        event: RunEvent,
        proposal: dict[str, object] | None,
        decision_token: str | None,
    ) -> None:
        await self.persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)
        if proposal is not None:
            routing_decision = (
                {} if self.row.routing_decision is None else dict(self.row.routing_decision)
            )
            self.row.routing_decision = {
                **routing_decision,
                "approval_kind": "self_repair",
                "decision_token": decision_token,
                "repair_proposal": proposal,
            }
            self.row.version += 1

    async def accept_self_repair_and_enqueue(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> RunRecord:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        routing_decision = {} if self.row.routing_decision is None else dict(self.row.routing_decision)
        if self.row.status != RunStatus.FAILED.value:
            raise AssertionError("run must be failed")
        if routing_decision.get("approval_kind") != "self_repair":
            raise AssertionError("run must be waiting for self repair")
        if routing_decision.get("decision_token") != decision_token:
            raise AssertionError("token mismatch")
        if self.row.version != version:
            raise AssertionError("version mismatch")
        proposal = routing_decision.get("repair_proposal")
        assert isinstance(proposal, dict)
        attempt = proposal.get("attempt")
        max_attempts = proposal.get("max_attempts")
        assert type(attempt) is int
        assert type(max_attempts) is int
        self.row.routing_decision = {
            **{
                key: value
                for key, value in routing_decision.items()
                if key not in {"approval_kind", "decision_token"}
            },
            "source": "self_repair",
            "self_repair_accepted": True,
            "self_repair_attempt": attempt,
            "self_repair_max_attempts": max_attempts,
            "self_repair_context": repair_context_from_proposal(proposal),
        }
        self.row.status = RunStatus.QUEUED.value
        self.row.version += 1
        return self._record()

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return self._record()

    async def conversation_context(
        self,
        tenant_id: UUID,
        conversation_id: str,
        *,
        before_run_id: UUID,
    ) -> list[object]:
        assert tenant_id == TENANT_ID
        assert before_run_id == self.run_id
        assert conversation_id
        self.conversation_context_calls.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "before_run_id": before_run_id,
            }
        )
        return []

    async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        assert tenant_id == TENANT_ID
        assert run_id == self.run_id
        return tuple(
            {**event.to_payload(), "created_at": datetime.now(UTC).isoformat()}
            for event in self.event_log
        )

    def _record(self) -> RunRecord:
        return RunRecord(
            id=self.row.id,
            tenant_id=self.row.tenant_id,
            actor_id=self.row.actor_id,
            actor_role=None if self.row.actor_role is None else Role(self.row.actor_role),
            request=self.row.request,
            mode=None if self.row.mode is None else TaskMode(self.row.mode),
            status=RunStatus(self.row.status),
            version=self.row.version,
            created_at=self.row.created_at,
            routing_decision=None
            if self.row.routing_decision is None
            else dict(self.row.routing_decision),
        )


class RecoveredFailedRepository(ExecutableFakeRepository):
    async def claim_for_execution(
        self,
        session: FakeTransaction,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        del session
        assert allow_running_recovery is True
        assert run_id == self.run_id
        self.row.status = RunStatus.FAILED.value
        self.row.version += 1
        self.event_log.append(
            RunEvent(
                kind=EventKind.STEP_FAILED,
                sequence=1,
                run_id=run_id,
                actor="engineer",
                step_id="previous_step",
                reason="non replayable event after checkpoint",
            )
        )
        return self._record()


class RecoveredEmptyResponseRepository(ExecutableFakeRepository):
    async def claim_for_execution(
        self,
        session: FakeTransaction,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        del session, allow_running_recovery
        assert run_id == self.run_id
        self.row.status = RunStatus.FAILED.value
        self.row.version += 1
        if not self.event_log:
            self.event_log.append(
                RunEvent(
                    kind=EventKind.RUNTIME_FAILED,
                    sequence=1,
                    run_id=run_id,
                    reason=(
                        "hybrid dispatch failed: model gateway failed: "
                        "model response text is empty"
                    ),
                )
            )
        return self._record()


class ApprovalWaitingRepository(ExecutableFakeRepository):
    async def get_for_update(self, session: FakeTransaction, run_id: UUID) -> FakeRunRow:
        row = await super().get_for_update(session, run_id)
        if row.status == RunStatus.RUNNING.value:
            row.status = RunStatus.WAITING_APPROVAL.value
            row.version += 1
        return row


class RuntimeCompletes:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeRecordsRepairContextCompletes:
    mode = TaskMode.DISPATCH

    def __init__(self) -> None:
        self.contexts: list[TaskContext] = []

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeReportsCapacityPressure:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.STEP_FAILED,
            sequence=1,
            run_id=context.run_id,
            actor="planner",
            step_id="planner_step",
            reason="model gateway failed: model capacity unavailable",
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=2,
            run_id=context.run_id,
            reason="model gateway failed: model capacity unavailable",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeReportsEmptyModelResponse:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=1,
            run_id=context.run_id,
            reason="hybrid dispatch failed: model gateway failed: model response text is empty",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeReportsToolFailure:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.TOOL_FAILED,
            sequence=1,
            run_id=context.run_id,
            actor="planner",
            tool_call_id="call_1",
            tool_name="workspace_read",
            reason="awaiting approval",
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=2,
            run_id=context.run_id,
            reason="capability execution failed",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeReportsUncertainToolThenRuntimeFailure:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.TOOL_FAILED,
            sequence=1,
            run_id=context.run_id,
            actor="planner",
            tool_call_id="call_1",
            tool_name="workspace_read",
            reason="capability execution failed",
            payload={"failure_kind": "uncertain"},
        )
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=2,
            run_id=context.run_id,
            reason="capability execution failed",
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeReportsUncertainToolThenRaises:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(
            kind=EventKind.TOOL_FAILED,
            sequence=1,
            run_id=context.run_id,
            actor="planner",
            tool_call_id="call_1",
            tool_name="workspace_read",
            reason="capability execution failed",
            payload={"failure_kind": "uncertain"},
        )
        raise RuntimeError("runtime interrupted before terminal event")
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=2, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeCancels:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        yield RunEvent(kind=EventKind.RUNTIME_CANCELLED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeWaitsForApprovalThenRaises:
    mode = TaskMode.DISPATCH

    def __init__(self, repository: ExecutableFakeRepository) -> None:
        self._repository = repository

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        assert context.run_id == self._repository.run_id
        self._repository.row.status = RunStatus.WAITING_APPROVAL.value
        self._repository.row.version += 1
        raise RuntimeError("capability approval boundary interrupted")
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=context.run_id)

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class RuntimeRaises:
    mode = TaskMode.DISPATCH

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        del context
        raise RuntimeError("planner crashed")
        yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=uuid4())

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("not used")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        del checkpoint

    async def cancel(self) -> None:
        raise AssertionError("not used")


class ContextRecordingRuntime(RuntimeCompletes):
    def __init__(self) -> None:
        self.contexts: list[TaskContext] = []

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.contexts.append(context)
        async for event in super().run(context):
            yield event


class RecordingHermesAdvisor:
    def __init__(self) -> None:
        self.outcomes: list[HermesRunOutcome] = []

    async def advise(self, **kwargs: object) -> None:
        del kwargs

    async def record_outcome(self, outcome: HermesRunOutcome) -> None:
        self.outcomes.append(outcome)

class RecordingHook:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "run_id": run_id,
                "status": status,
                "mode": mode,
                "routing_decision": routing_decision,
            }
        )
        if self.fail:
            raise RuntimeError("hook failed")


@pytest.mark.asyncio
async def test_execute_notifies_terminal_hooks_after_completed_run() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={"source": "evolution", "evolution_run_id": "evolution_1"}
    )
    hook = RecordingHook()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        terminal_run_hooks=(hook,),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert hook.calls == [
        {
            "tenant_id": TENANT_ID,
            "actor_id": ACTOR_ID,
            "run_id": repository.run_id,
            "status": RunStatus.COMPLETED,
            "mode": TaskMode.DISPATCH,
            "routing_decision": {"source": "evolution", "evolution_run_id": "evolution_1"},
        }
    ]


@pytest.mark.asyncio
async def test_execute_forwards_persisted_actor_identity_to_runtime_context() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    repository.row.actor_role = Role.OPERATOR.value
    runtime = ContextRecordingRuntime()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    assert runtime.contexts[0].actor_id == ACTOR_ID
    assert runtime.contexts[0].actor_role is Role.OPERATOR


@pytest.mark.asyncio
async def test_execute_persists_harness_started_event_before_vibe_runtime_events() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={
            "source": "manual",
            "vibe_coding": True,
            "harness_decision": {
                "mode": "dispatch",
                "selected_provider": "openai",
                "selected_model": "gpt-5.6-sol",
                "selected_logical_model": "vibe_engineer",
                "requires_approval": False,
                "capability_reasons": [
                    "supports_reasoning_delta",
                    "supports_parallel_tool_calls",
                ],
                "policy_reasons": ["policy_allows_restricted_sandbox"],
                "context_reasons": ["context_window_fits"],
                "fallbacks_considered": ["deepseek"],
            },
        }
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert [event.kind for event in repository.event_log] == [
        "harness.started",
        EventKind.RUNTIME_COMPLETED,
    ]
    assert [event.sequence for event in repository.event_log] == [1, 2]
    assert repository.event_log[0].payload == {
        "schema_version": 1,
        "phase": "started",
        "mode": "dispatch",
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "logical_model": "vibe_engineer",
        "requires_approval": False,
        "capabilities": (
            "supports_reasoning_delta",
            "supports_parallel_tool_calls",
        ),
        "policy": ("policy_allows_restricted_sandbox",),
        "context": ("context_window_fits",),
        "fallbacks": ("deepseek",),
    }


@pytest.mark.asyncio
async def test_execute_without_harness_decision_keeps_runtime_event_sequence() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert [event.kind for event in repository.event_log] == [EventKind.RUNTIME_COMPLETED]
    assert [event.sequence for event in repository.event_log] == [1]


@pytest.mark.asyncio
async def test_execute_does_not_emit_harness_started_event_with_sensitive_profile() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={
            "source": "manual",
            "vibe_coding": True,
            "harness_decision": {
                "mode": "dispatch",
                "selected_provider": "openai",
                "selected_model": "sk-secret-model-token",
                "selected_logical_model": "vibe_engineer",
                "requires_approval": False,
                "capability_reasons": ["supports_parallel_tool_calls"],
                "policy_reasons": ["authorization bypass"],
            },
        }
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert [event.kind for event in repository.event_log] == [EventKind.RUNTIME_COMPLETED]
    assert "sk-secret-model-token" not in repr(repository.event_log)
    assert "authorization bypass" not in repr(repository.event_log)


@pytest.mark.asyncio
async def test_execute_preserves_waiting_approval_status_set_by_capability_policy() -> None:
    repository = ApprovalWaitingRepository(routing_decision={})
    hook = RecordingHook()
    hermes = RecordingHermesAdvisor()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsToolFailure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        hermes_advisor=hermes,
        terminal_run_hooks=(hook,),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert repository.event_log == []
    assert hermes.outcomes == []
    assert hook.calls == []


@pytest.mark.asyncio
async def test_execute_exception_does_not_overwrite_waiting_approval_status() -> None:
    repository = ExecutableFakeRepository(routing_decision={})
    runtime = RuntimeWaitsForApprovalThenRaises(repository)
    hook = RecordingHook()
    hermes = RecordingHermesAdvisor()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        hermes_advisor=hermes,
        terminal_run_hooks=(hook,),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.WAITING_APPROVAL
    assert repository.event_log == []
    assert hermes.outcomes == []
    assert hook.calls == []


@pytest.mark.asyncio
async def test_terminal_hook_failure_does_not_fail_completed_run() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={"source": "evolution", "evolution_run_id": "evolution_1"}
    )
    hook = RecordingHook(fail=True)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        terminal_run_hooks=(hook,),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert hook.calls


@pytest.mark.asyncio
async def test_execute_persists_observer_notice_for_capacity_pressure() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    notices = [event for event in repository.event_log if event.kind == "observer.notice"]
    assert len(notices) == 1
    notice = notices[0]
    assert [event.kind for event in repository.event_log] == [
        EventKind.STEP_FAILED,
        EventKind.RUNTIME_FAILED,
        "observer.notice",
        "repair.classified",
    ]
    assert notice.sequence == 3
    assert notice.payload["trigger"] == "model_capacity_pressure"
    assert notice.payload["action"] == "reschedule_or_reassign_model"
    assert notice.payload["source_sequence"] == 1
    assert "message" not in notice.payload
    assert "prompt" not in notice.payload


@pytest.mark.asyncio
async def test_execute_persists_repair_classification_for_failed_run() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    repair_events = [event for event in repository.event_log if event.kind == "repair.classified"]
    assert len(repair_events) == 1
    repair = repair_events[0]
    assert [event.kind for event in repository.event_log] == [
        EventKind.STEP_FAILED,
        EventKind.RUNTIME_FAILED,
        "observer.notice",
        "repair.classified",
    ]
    assert repair.sequence == 4
    assert repair.payload["trigger"] == "runtime_failure"
    assert repair.payload["action"] == "draft_repair_proposal"
    assert repair.payload["status"] == RunStatus.FAILED.value
    assert repair.payload["mode"] == TaskMode.DISPATCH.value
    assert repair.payload["source_kind"] == "runtime.failed"
    assert repair.payload["source_sequence"] == 2
    assert repair.payload["failure_category"] == "capacity_pressure"
    assert repair.payload["requires_approval"] is True
    assert repair.payload["automatic_execution"] is False
    assert len(str(repair.payload["fingerprint"])) == 64
    assert "message" not in repair.payload
    assert "prompt" not in repair.payload
    assert "secret" not in repr(repair.payload)
    assert submitted.decision_token is not None
    assert submitted.repair_proposal == {
        "kind": "self_repair",
        "title": "受控自修复建议",
        "summary": "运行失败已分类，可在审批后创建一次受控修复重试。",
        "repair_action": "draft_repair_proposal",
        "failure_kind": "capacity_pressure",
        "source_run_id": str(repository.run_id),
        "source_event_sequence": 2,
        "attempt": 1,
        "max_attempts": 1,
        "instruction": "用更小的输入和更低负载重试，必要时标记模型 fallback，但不要绕过审批或隐藏失败。",
        "requires_approval": True,
        "replay_safe": False,
        "automatic_execution": False,
        "fingerprint": repair.payload["fingerprint"],
    }
    assert "secret" not in repr(submitted.repair_proposal)


@pytest.mark.asyncio
async def test_execute_classifies_empty_model_response_for_controlled_retry() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsEmptyModelResponse(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    notice = next(event for event in repository.event_log if event.kind == "observer.notice")
    assert notice.payload["trigger"] == "empty_model_response"
    assert notice.payload["action"] == "retry_fallback_or_reassign_model"
    repair = next(event for event in repository.event_log if event.kind == "repair.classified")
    assert repair.payload["failure_category"] == "empty_model_response"
    assert repair.payload["automatic_execution"] is False
    assert submitted.repair_proposal is not None
    assert submitted.repair_proposal["failure_kind"] == "empty_model_response"
    assert "压缩输入" in str(submitted.repair_proposal["instruction"])
    assert "fallback" in str(submitted.repair_proposal["instruction"])
    assert [event.kind for event in repository.event_log] == [
        EventKind.RUNTIME_FAILED,
        "observer.notice",
        "repair.classified",
        EventKind.ARTIFACT_CREATED,
    ]
    closure_events = [
        event
        for event in repository.event_log
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None
        and event.artifact.producer == "run_service"
    ]
    assert len(closure_events) == 1
    closure_text = closure_events[0].artifact.content["text"]
    assert isinstance(closure_text, str)
    assert "模型返回了空内容" in closure_text
    assert "压缩输入" in closure_text
    assert "审批后" in closure_text
    assert "hybrid dispatch failed" not in closure_text
    assert "secret" not in closure_text
    assert repository.artifacts == [closure_events[0].artifact]


@pytest.mark.asyncio
async def test_execute_records_empty_response_closure_when_runtime_raises() -> None:
    class RuntimeRaisesEmptyResponse:
        mode = TaskMode.DISPATCH

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            del context
            raise RuntimeError(
                "hybrid dispatch failed: model gateway failed: model response text is empty"
            )
            yield RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=uuid4())

        async def save_checkpoint(self) -> RuntimeCheckpoint:
            raise AssertionError("not used")

        async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
            del checkpoint

        async def cancel(self) -> None:
            raise AssertionError("not used")

    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeRaisesEmptyResponse(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    assert submitted.repair_proposal is not None
    assert submitted.repair_proposal["failure_kind"] == "empty_model_response"
    closure_events = [
        event
        for event in repository.event_log
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None
        and event.artifact.producer == "run_service"
    ]
    assert len(closure_events) == 1
    closure_text = str(closure_events[0].artifact.content["text"])
    assert "模型返回了空内容" in closure_text
    assert "审批后" in closure_text


@pytest.mark.asyncio
async def test_execute_skips_self_repair_source_without_recursive_repair() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "self_repair"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    skipped = [event for event in repository.event_log if event.kind == "repair.skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["skip_reason"] == "recursive_self_repair"
    assert [event for event in repository.event_log if event.kind == "repair.classified"] == []


@pytest.mark.asyncio
async def test_execute_does_not_emit_repair_event_for_completed_run() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert [
        event for event in repository.event_log if str(event.kind).startswith("repair.")
    ] == []


@pytest.mark.asyncio
async def test_execute_does_not_emit_repair_event_for_cancelled_run() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCancels(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.CANCELLED
    assert [
        event for event in repository.event_log if str(event.kind).startswith("repair.")
    ] == []


@pytest.mark.asyncio
async def test_execute_respects_self_repair_policy_kill_switch() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        self_repair_policy=SelfRepairPolicy(enabled=False),
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    assert [
        event for event in repository.event_log if str(event.kind).startswith("repair.")
    ] == []


def test_self_repair_policy_has_no_automatic_execution_knob_in_foundation_slice() -> None:
    with pytest.raises(TypeError):
        SelfRepairPolicy(automatic_execution=True)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_execute_skips_any_uncertain_failure_without_auto_repair() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsUncertainToolThenRuntimeFailure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    skipped = [event for event in repository.event_log if event.kind == "repair.skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["skip_reason"] == "side_effect_outcome_uncertain"
    assert skipped[0].payload["source_kind"] == "tool.failed"
    assert skipped[0].payload["automatic_execution"] is False
    assert "secret" not in repr(skipped[0].payload)
    assert submitted.decision_token is None
    assert submitted.repair_proposal is None


@pytest.mark.asyncio
async def test_execute_skips_uncertain_tool_failure_when_runtime_raises_later() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsUncertainToolThenRaises(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    skipped = [event for event in repository.event_log if event.kind == "repair.skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["skip_reason"] == "side_effect_outcome_uncertain"
    assert skipped[0].payload["source_kind"] == "tool.failed"
    assert submitted.repair_proposal is None


@pytest.mark.asyncio
async def test_execute_classifies_runtime_exception_failure() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeRaises(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    assert [event.kind for event in repository.event_log] == [
        EventKind.RUNTIME_FAILED,
        "repair.classified",
    ]
    repair = repository.event_log[-1]
    assert repair.sequence == 2
    assert repair.payload["source_kind"] == "runtime.failed"
    assert repair.payload["source_sequence"] == 1
    assert repair.payload["failure_category"] == "runtime_failure"
    assert submitted.repair_proposal is not None
    assert submitted.repair_proposal["source_event_sequence"] == 1


@pytest.mark.asyncio
async def test_accept_self_repair_requeues_failed_run_with_recursion_guard() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )
    failed = await service.execute(repository.run_id)
    assert failed.decision_token is not None

    accepted = await service.accept_self_repair(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_id=repository.run_id,
        decision_token=failed.decision_token,
        version=failed.version,
    )

    assert accepted.status is RunStatus.QUEUED
    assert accepted.decision_token is None
    assert accepted.repair_proposal is not None
    assert repository.row.routing_decision is not None
    assert repository.row.routing_decision["source"] == "self_repair"
    assert repository.row.routing_decision["self_repair_accepted"] is True
    assert repository.row.routing_decision["self_repair_attempt"] == 1
    assert repository.row.routing_decision["self_repair_max_attempts"] == 1
    assert isinstance(repository.row.routing_decision["self_repair_context"], dict)
    assert repository.row.routing_decision["self_repair_context"]["source"] == "self_repair"


@pytest.mark.asyncio
async def test_accepted_self_repair_run_records_bounded_execution_audit() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    failure_service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )
    failed = await failure_service.execute(repository.run_id)
    assert failed.decision_token is not None
    await failure_service.accept_self_repair(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_id=repository.run_id,
        decision_token=failed.decision_token,
        version=failed.version,
    )
    repair_runtime = RuntimeRecordsRepairContextCompletes()
    repair_service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((repair_runtime,)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    repaired = await repair_service.execute(repository.run_id)

    assert repaired.status is RunStatus.COMPLETED
    repair_events = [
        event for event in repository.event_log if str(event.kind).startswith("repair.")
    ]
    assert [event.kind for event in repair_events] == [
        "repair.classified",
        "repair.started",
        "repair.completed",
    ]
    assert repair_events[-2].payload["attempt"] == 1
    assert repair_events[-2].payload["max_attempts"] == 1
    assert repair_events[-1].payload["status"] == RunStatus.COMPLETED.value
    assert repair_runtime.contexts
    repair_context = repair_runtime.contexts[0].routing_decision["self_repair_context"]
    assert isinstance(repair_context, dict)
    assert repair_context["source"] == "self_repair"
    assert repair_context["automatic_execution"] is False


@pytest.mark.asyncio
async def test_recover_persists_repair_classification_for_failed_running_recovery() -> None:
    repository = RecoveredFailedRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    submitted = await service.recover(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    repair_events = [event for event in repository.event_log if event.kind == "repair.classified"]
    assert len(repair_events) == 1
    assert repair_events[0].payload["source_kind"] == "step.failed"
    assert submitted.repair_proposal is not None


@pytest.mark.asyncio
async def test_recover_backfills_empty_response_closure_once_for_failed_run() -> None:
    repository = RecoveredEmptyResponseRepository(routing_decision={"source": "manual"})
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
    )

    first = await service.recover(repository.run_id)
    second = await service.recover(repository.run_id)

    assert first.status is RunStatus.FAILED
    assert second.status is RunStatus.FAILED
    closure_events = [
        event
        for event in repository.event_log
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None
        and event.artifact.producer == "run_service"
    ]
    assert len(closure_events) == 1
    assert "模型返回了空内容" in str(closure_events[0].artifact.content["text"])


@pytest.mark.asyncio
async def test_execute_records_observer_notices_in_hermes_scheduler_outcome() -> None:
    repository = ExecutableFakeRepository(routing_decision={"source": "manual"})
    hermes = RecordingHermesAdvisor()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeReportsCapacityPressure(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        hermes_advisor=hermes
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.FAILED
    assert len(hermes.outcomes) == 1
    outcome = hermes.outcomes[0]
    assert outcome.status is RunStatus.FAILED
    assert outcome.scheduler_notices == (
        {
            "schema_version": 1,
            "trigger": "model_capacity_pressure",
            "action": "reschedule_or_reassign_model",
            "severity": "warning",
            "source_kind": "step.failed",
            "source_sequence": 1,
            "event_count": 1,
            "failure_events": 1,
            "retry_events": 0,
            "message_events": 0,
            "artifact_events": 0,
            "actor": "planner",
        },
    )
    assert "正文" not in repr(outcome.scheduler_notices)
    assert "prompt" not in repr(outcome.scheduler_notices)


@pytest.mark.asyncio
async def test_execute_backfills_hermes_outcome_for_terminal_run_after_crash() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={
            "source": "manual",
            "conversation_id": "conv-crash-after-terminal",
            "workflow_id": "workflow-alpha",
            "selected_agent_ids": ["writer"],
        }
    )
    hermes = RecordingHermesAdvisor()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        hermes_advisor=hermes,
    )

    interrupted = await service.execute(
        repository.run_id,
        crash_after_event_kind=EventKind.RUNTIME_COMPLETED,
    )
    recovered = await service.execute(repository.run_id)

    assert interrupted.status is RunStatus.COMPLETED
    assert recovered.status is RunStatus.COMPLETED
    assert len(hermes.outcomes) == 1
    outcome = hermes.outcomes[0]
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.conversation_id == "conv-crash-after-terminal"
    assert outcome.workflow_id == "workflow-alpha"
    assert outcome.agent_ids == ("writer",)


@pytest.mark.asyncio
async def test_execute_records_hermes_outcome_when_cleared_conversation_has_no_context() -> None:
    repository = ExecutableFakeRepository(
        routing_decision={
            "source": "manual",
            "conversation_id": "conv-cleared-then-continued",
            "workflow_id": None,
            "selected_agent_ids": ["writer"],
        }
    )
    hermes = RecordingHermesAdvisor()
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None,
        task_queue=object(),  # type: ignore[arg-type]
        hermes_advisor=hermes,
    )

    submitted = await service.execute(repository.run_id)

    assert submitted.status is RunStatus.COMPLETED
    assert repository.conversation_context_calls == [
        {
            "tenant_id": TENANT_ID,
            "conversation_id": "conv-cleared-then-continued",
            "before_run_id": repository.run_id,
        }
    ]
    assert len(hermes.outcomes) == 1
    outcome = hermes.outcomes[0]
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.conversation_id == "conv-cleared-then-continued"
    assert outcome.agent_ids == ("writer",)
