from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runs.self_repair import SelfRepairPolicy
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import (
    TENANT_ID,
    ExecutableFakeRepository,
    RuntimeCompletes,
)

APPROVAL_ID = "approval-current"


def crew_checkpoint(run_id: UUID, approval_id: str = APPROVAL_ID) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        id=uuid4(), runtime_type="crew", runtime_version="11",
        run_id=run_id, tenant_id=TENANT_ID, mode=TaskMode.DISPATCH,
        state={"tools": {"tool-key": {
            "status": "waiting_approval", "approval_id": approval_id,
        }}},
    )


def hybrid_checkpoint(child: RuntimeCheckpoint, *, version: str = "2") -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        id=uuid4(), runtime_type="hybrid", runtime_version=version,
        run_id=child.run_id, tenant_id=child.tenant_id, mode=TaskMode.HYBRID,
        state={"terminal": False, "next_stage": 0,
               "child_checkpoint": cast(JsonValue, child.to_payload())},
    )


@pytest.mark.parametrize("version", [None, "2", "3"])
def test_matching_waiting_receipt(version: str | None) -> None:
    checkpoint = crew_checkpoint(uuid4())
    if version is not None:
        checkpoint = hybrid_checkpoint(checkpoint, version=version)
    assert checkpoint_waits_for_approval(checkpoint, APPROVAL_ID)
    assert not checkpoint_waits_for_approval(checkpoint, "approval-stale")


@pytest.mark.parametrize("version", ["2", "3"])
@pytest.mark.parametrize("mutation", [
    "run", "tenant", "child_hash", "missing_child_hash", "outer_hash", "runtime",
    "child_runtime", "recursive_hybrid", "child_mode", "outer_mode", "terminal",
    "stage", "version", "prepared", "nested_tools",
])
def test_hybrid_receipt_rejects_unrelated_or_invalid_state(mutation: str, version: str) -> None:
    payload = hybrid_checkpoint(crew_checkpoint(uuid4()), version=version).to_payload()
    state = cast(dict[str, Any], payload["state"])
    child = state["child_checkpoint"]
    if mutation in {"run", "tenant"}:
        child[f"{mutation}_id"] = str(uuid4())
    elif mutation == "child_hash":
        child["state_sha256"] = "0" * 64
    elif mutation == "missing_child_hash":
        child.pop("state_sha256")
    elif mutation == "outer_hash":
        checkpoint = hybrid_checkpoint(crew_checkpoint(uuid4()), version=version)
        checkpoint = checkpoint.model_copy(update={"state_sha256": "0" * 64})
        assert not checkpoint_waits_for_approval(checkpoint, APPROVAL_ID)
        return
    elif mutation == "runtime":
        payload["runtime_type"] = "unrecognized"
    elif mutation == "child_runtime":
        child["runtime_type"] = "direct"
    elif mutation == "recursive_hybrid":
        state["child_checkpoint"] = hybrid_checkpoint(crew_checkpoint(UUID(child["run_id"]))).to_payload()
    elif mutation == "child_mode":
        child["mode"] = "direct"
    elif mutation == "outer_mode":
        payload["mode"] = "dispatch"
    elif mutation == "terminal":
        state["terminal"] = True
    elif mutation == "stage":
        state["next_stage"] = 3
    elif mutation == "version":
        child["runtime_version"] = "10"
    else:
        receipt_state = child["state"]
        if mutation == "prepared":
            receipt_state["tools"]["tool-key"]["status"] = "prepared"
        else:
            child["state"] = {"unrelated": receipt_state}
        child["state_sha256"] = ""
        state["child_checkpoint"] = RuntimeCheckpoint.from_payload(child).to_payload()
    payload["state_sha256"] = ""
    checkpoint = RuntimeCheckpoint.from_payload(payload)
    assert not checkpoint_waits_for_approval(checkpoint, APPROVAL_ID)


@pytest.mark.parametrize("version", ["1", "4", "99"])
def test_hybrid_receipt_rejects_unknown_outer_version(version: str) -> None:
    checkpoint = hybrid_checkpoint(crew_checkpoint(uuid4()), version=version)
    assert not checkpoint_waits_for_approval(checkpoint, APPROVAL_ID)


class ApprovalRepository(ExecutableFakeRepository):
    def __init__(self) -> None:
        super().__init__(routing_decision={})
        self.checkpoint: RuntimeCheckpoint | None = None
        self.first_waiting_event = asyncio.Event()
        self.failed_renewal = asyncio.Event()
        self.approval_observations: list[bool] = []

    def begin_approval(self) -> None:
        assert self.row.worker_lease_token is not None
        owner_token = str(self.row.worker_lease_token)
        self.row.status = RunStatus.WAITING_APPROVAL.value
        RunRepository.clear_worker_lease(cast(Any, self.row))
        self.row.routing_decision = {
            **(self.row.routing_decision or {}),
            "approval_kind": "capability_tool", "approval_id": APPROVAL_ID,
            "approval_checkpoint_required": True,
            "approval_checkpoint_worker_token": owner_token,
        }
        # Both durable sequences are still equal, while the request is only queued.
        assert all(event.kind is not EventKind.TOOL_REQUESTED for event in self.event_log)

    async def persist_event(self, *args: Any, **kwargs: Any) -> None:
        await super().persist_event(*args, **kwargs)
        event = kwargs["event"]
        if event.checkpoint is not None:
            self.checkpoint = event.checkpoint
        if self.row.status == RunStatus.WAITING_APPROVAL.value:
            self.first_waiting_event.set()
            self.approval_observations.append(
                checkpoint_waits_for_approval(self.checkpoint, APPROVAL_ID)
            )

    async def renew_active_worker_lease(self, **kwargs: Any) -> bool:
        renewed = await super().renew_active_worker_lease(**kwargs)
        if not renewed:
            self.failed_renewal.set()
        return renewed

    async def release_worker_execution(self, *args: Any, **kwargs: Any) -> RunRecord:
        if self.row.worker_lease_token != kwargs["worker_lease_token"]:
            raise RunConflict("run worker execution ownership changed")
        return await super().release_worker_execution(*args, **kwargs)


class QueuedApprovalRuntime(RuntimeCompletes):
    def __init__(self, repository: ApprovalRepository, *, ending: str = "checkpoint",
                 hybrid: bool = False) -> None:
        self.repository = repository
        self.ending = ending
        self.mode = TaskMode.HYBRID if hybrid else TaskMode.DISPATCH
        self.cancel_calls = 0
        self.cancelled = asyncio.Event()
        self.emitted_after_boundary = False

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        checkpoint = crew_checkpoint(context.run_id)
        old_checkpoint = crew_checkpoint(context.run_id, "approval-stale")
        if self.mode is TaskMode.HYBRID:
            checkpoint = hybrid_checkpoint(checkpoint)
            old_checkpoint = hybrid_checkpoint(old_checkpoint)
        await queue.put(RunEvent(kind=EventKind.TOOL_REQUESTED, sequence=1,
                                 run_id=context.run_id, actor="implementer", tool_call_id="call-1",
                                 tool_name="workspace.write_text"))
        await queue.put(RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=2,
                                 run_id=context.run_id, checkpoint=old_checkpoint))
        self.repository.begin_approval()
        if self.ending in {"wrong_owner", "missing_owner", "wrong_owner_idle", "wrong_owner_error"}:
            assert self.repository.row.routing_decision is not None
            if self.ending != "missing_owner":
                self.repository.row.routing_decision["approval_checkpoint_worker_token"] = str(uuid4())
            else:
                self.repository.row.routing_decision.pop("approval_checkpoint_worker_token")
        if self.ending in {"idle", "wrong_owner_idle"}:
            await self.cancelled.wait()
            return
        if self.ending in {"error_before_first", "wrong_owner_error"}:
            raise RuntimeError("runtime checkpoint unavailable")
        if self.ending == "takeover_before_first":
            self.repository.row.status = RunStatus.QUEUED.value
        while not queue.empty():
            yield await queue.get()
        if self.ending == "heartbeat":
            await self.repository.failed_renewal.wait()
            await asyncio.sleep(0)
            assert self.cancel_calls == 0
        elif self.ending == "stall":
            await asyncio.Event().wait()
        elif self.ending == "error":
            raise RuntimeError("runtime checkpoint unavailable")
        elif self.ending == "eof":
            return
        elif self.ending == "cancel":
            self.repository.row.status = RunStatus.CANCELLED.value
        elif self.ending == "stale_id":
            assert self.repository.row.routing_decision is not None
            self.repository.row.routing_decision["approval_id"] = "approval-new"
        elif self.ending == "takeover":
            self.repository.row.status = RunStatus.QUEUED.value
        elif self.ending in {"runtime.failed", "runtime.cancelled", "runtime.completed"}:
            yield RunEvent(kind=EventKind(self.ending), sequence=3, run_id=context.run_id,
                           reason=None if self.ending == "runtime.cancelled" else "runtime terminated")
            return
        elif self.ending == "flood":
            while True:
                yield RunEvent(kind=EventKind.TOOL_REQUESTED, sequence=3,
                               run_id=context.run_id, actor="implementer", tool_call_id="call-1",
                               tool_name="workspace.write_text")
        yield RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=3,
                       run_id=context.run_id, checkpoint=checkpoint)
        self.emitted_after_boundary = True
        yield RunEvent(kind=EventKind.TOOL_FAILED, sequence=4,
                       run_id=context.run_id, actor="implementer", tool_call_id="call-1",
                       tool_name="workspace.write_text",
                       reason="waiting_approval")

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()


def make_service(repository: ApprovalRepository, runtime: QueuedApprovalRuntime) -> RunService:
    repository.row.mode = runtime.mode.value
    return RunService(cast(Any, repository), runtime_registry=RuntimeRegistry((runtime,)),
                      router=None, task_queue=cast(Any, object()))


@pytest.mark.asyncio
@pytest.mark.parametrize("hybrid", [False, True])
async def test_service_drains_unpersisted_request_and_stale_checkpoint(hybrid: bool) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, hybrid=hybrid)
    result = await make_service(repository, runtime).execute(repository.run_id)
    assert result.status is RunStatus.WAITING_APPROVAL
    assert [event.kind for event in repository.event_log] == [
        EventKind.TOOL_REQUESTED, EventKind.CHECKPOINT_SAVED, EventKind.CHECKPOINT_SAVED,
    ]
    assert repository.approval_observations == [False, False, True]
    assert not runtime.emitted_after_boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["runtime.failed", "runtime.cancelled", "runtime.completed"])
async def test_drain_does_not_disguise_terminal_events(ending: str) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, ending=ending)
    result = await make_service(repository, runtime).execute(repository.run_id)
    assert result.status.value == ending.split(".")[1]
    assert [event.kind for event in repository.event_log if event.kind in {
        EventKind.RUNTIME_FAILED, EventKind.RUNTIME_CANCELLED, EventKind.RUNTIME_COMPLETED,
    }] == [ending]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "stale_id", "takeover"])
async def test_drain_stops_on_cancellation_or_changed_approval(ending: str) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, ending=ending)
    result = await make_service(repository, runtime).execute(repository.run_id)
    assert len(repository.event_log) == 2
    assert not checkpoint_waits_for_approval(repository.checkpoint, APPROVAL_ID)
    assert result.status is (RunStatus.CANCELLED if ending == "cancel" else
                             RunStatus.QUEUED if ending == "takeover" else RunStatus.WAITING_APPROVAL)
    assert runtime.cancel_calls == 1


@pytest.mark.asyncio
async def test_heartbeat_allows_checkpoint_drain_after_lease_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, ending="heartbeat")
    service = make_service(repository, runtime)
    monkeypatch.setattr(service, "_worker_lease_heartbeat_interval_seconds", lambda: 0.001)
    result = await asyncio.wait_for(service.execute(repository.run_id), timeout=2)
    assert result.status is RunStatus.WAITING_APPROVAL
    assert checkpoint_waits_for_approval(repository.checkpoint, APPROVAL_ID)
    assert runtime.cancel_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["error", "eof", "stall", "flood", "idle", "error_before_first"])
async def test_missing_boundary_fails_closed_and_is_bounded(
    ending: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_hub.runs.service as service_module

    monkeypatch.setattr(service_module, "_APPROVAL_CHECKPOINT_DRAIN_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(service_module, "_APPROVAL_CHECKPOINT_DRAIN_EVENTS", 4, raising=False)
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, ending=ending)
    service = make_service(repository, runtime)
    monkeypatch.setattr(service, "_worker_lease_heartbeat_interval_seconds", lambda: 0.001)
    result = await asyncio.wait_for(service.execute(repository.run_id), 2)
    assert result.status is RunStatus.FAILED
    failures = [event for event in repository.event_log if event.kind is EventKind.RUNTIME_FAILED]
    assert len(failures) == 1
    assert runtime.cancel_calls == 1
    assert failures[0].sequence <= 5


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [
    "wrong_owner", "missing_owner", "wrong_owner_idle", "wrong_owner_error", "takeover_before_first",
])
async def test_other_worker_cannot_drain_a_new_approval(
    ending: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, ending=ending)
    service = make_service(repository, runtime)
    monkeypatch.setattr(service, "_worker_lease_heartbeat_interval_seconds", lambda: 0.001)
    result = await asyncio.wait_for(service.execute(repository.run_id), 2)
    assert result.status is (
        RunStatus.QUEUED if ending == "takeover_before_first" else RunStatus.WAITING_APPROVAL
    )
    assert repository.event_log == []
    assert runtime.cancel_calls == (0 if ending == "wrong_owner_error" else 1)


@pytest.mark.asyncio
async def test_registry_failure_does_not_enter_approval_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository)
    service = make_service(repository, runtime)

    def unavailable(self: RuntimeRegistry, mode: TaskMode) -> Any:
        raise RuntimeError("runtime unavailable")

    async def unexpected_drain(**kwargs: Any) -> None:
        raise AssertionError("runtime consumption never started")

    monkeypatch.setattr(RuntimeRegistry, "get", unavailable)
    monkeypatch.setattr(service, "_fail_approval_checkpoint_drain", unexpected_drain)
    result = await service.execute(repository.run_id)
    assert result.status is RunStatus.FAILED
    assert runtime.cancel_calls == 0
    assert any(event.reason == "runtime unavailable" for event in repository.event_log)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [
    "error", "eof", "runtime.failed", "runtime.cancelled", "runtime.completed",
])
async def test_approval_drain_terminal_closes_hooks_and_conversation_queue(
    ending: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ApprovalRepository()
    runtime = QueuedApprovalRuntime(repository, ending=ending)
    service = make_service(repository, runtime)
    service._self_repair_policy = SelfRepairPolicy(enabled=False)
    closed: list[str] = []

    async def hermes(**kwargs: Any) -> None:
        closed.append("hermes")

    async def hooks(**kwargs: Any) -> None:
        closed.append("hooks")

    async def successor(*args: Any) -> None:
        closed.append("successor")

    monkeypatch.setattr(service, "_safe_record_hermes_outcome", hermes)
    monkeypatch.setattr(service, "_safe_notify_terminal_hooks_once", hooks)
    monkeypatch.setattr(service, "_release_conversation_successor", successor)
    result = await service.execute(repository.run_id)

    expected = ending.split(".")[1] if ending.startswith("runtime.") else "failed"
    assert result.status.value == expected
    assert closed == ["hermes", "hooks", "successor"]
