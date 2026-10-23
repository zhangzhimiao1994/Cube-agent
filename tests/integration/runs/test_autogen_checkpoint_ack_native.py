"""Committed AutoGen model/tool intent through PG/RunService, not paid-user proof.

RunService submit/execute, AutoGen, worker ownership, transactions and private
artifact storage are real. The model and tools are fixed, offline doubles. No
project source, provider request, native command or verification proof is made.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import _builtin_input_schema
from agent_hub.db.models import RunCheckpointRow, RunEventRow
from agent_hub.db.session import Database, build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.registry import RuntimeRegistry
from tests.integration.runs.test_action_service_native_recovery import RecordingQueue

pytestmark = pytest.mark.integration
ACK = 'engineering_checkpoint_ack_version'
TOOLS = ('project.inspect', 'workspace.list', 'workspace.write_text', 'project.verify', 'workspace.bundle')


def plan() -> DiscussionPlan:
    return DiscussionPlan(participants=(
        DiscussionParticipant(id='writer', role='Writer', goal='Build the requested project.',
                              logical_model='offline-writer', allowed_tools=TOOLS),
        DiscussionParticipant(id='reviewer', role='Reviewer', goal='Review the evidence.',
                              logical_model='offline-reviewer'),
    ), selector_model='offline-selector', max_turns=8, token_budget=5000)


def is_running_intent(event: RunEvent, ledger_name: str) -> bool:
    checkpoint = event.checkpoint
    if event.kind is not EventKind.CHECKPOINT_SAVED or checkpoint is None:
        return False
    ledger = checkpoint.state.get(ledger_name)
    return (checkpoint.state.get('terminal') is False and isinstance(ledger, tuple | list)
            and any(isinstance(row, Mapping) and row.get('status') == 'running' for row in ledger))


class RejectIntentRepository(RunRepository):
    rejected: RunEvent | None = None
    ledger_name = 'model_ledger'

    async def persist_event(
        self, session: AsyncSession, *, tenant_id: UUID, run_id: UUID, event: RunEvent,
    ) -> None:
        await super().persist_event(session, tenant_id=tenant_id, run_id=run_id, event=event)
        if self.rejected is None and is_running_intent(event, self.ledger_name):
            assert event.checkpoint is not None
            # Confirm the real inserts happened inside this transaction before aborting it.
            assert await session.get(RunCheckpointRow, event.checkpoint.id) is not None
            self.rejected = event
            raise RuntimeError('test-only checkpoint transaction rejected')


class RejectToolIntentRepository(RejectIntentRepository):
    ledger_name = 'tool_ledger'


class ForbiddenTools:
    def __init__(self) -> None:
        self.calls: list[HarnessToolCallRequest] = []

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return name in TOOLS

    def is_replay_safe(self, name: str) -> bool:
        return name in TOOLS and name != 'project.verify'

    def manifest(self, tenant_id: UUID) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple({'name': name, 'input_schema': _builtin_input_schema(name)} for name in TOOLS)

    async def execute(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        raise AssertionError('tool effects must use the harness boundary')

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.calls.append(request)
        raise AssertionError('fixed model must never reach project tools')


class CheckingModel:
    def __init__(self, database: Database, tenant: UUID, actor: UUID, *, forbidden: bool = False) -> None:
        self.database = database
        self.tenant, self.actor = tenant, actor
        self.run_id: UUID | None = None
        self.project_id: str | None = None
        self.forbidden = forbidden
        self.requests: list[ModelRequest] = []
        self.errors: list[str] = []
        self.entered = asyncio.Event()
        self.checkpoint: RuntimeCheckpoint | None = None

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        try:
            assert not self.forbidden, 'model entry forbidden after rollback, close or UNKNOWN recovery'
            assert self.run_id is not None
            assert request.logical_model == plan().selector_model and not request.tools
            runs = RunRepository(self.database.session_factory)
            record = await runs.get(self.tenant, self.run_id, actor_id=self.actor)
            assert record.actor_id == self.actor and record.actor_role is Role.OPERATOR
            assert record.status is RunStatus.RUNNING and record.worker_lease_token is not None
            assert record.routing_decision is not None
            assert type(record.routing_decision.get(ACK)) is int and record.routing_decision[ACK] == 1
            # A fresh session, not the driver's transaction, must see both committed rows.
            async with self.database.session_factory() as session:
                checkpoint = await runs.latest_checkpoint(
                    session, tenant_id=self.tenant, run_id=self.run_id)
                assert checkpoint is not None, 'model entered before running checkpoint committed'
                assert checkpoint.run_id == self.run_id and checkpoint.tenant_id == self.tenant
                assert checkpoint.mode is TaskMode.DISCUSS and checkpoint.state['terminal'] is False
                assert checkpoint.state['plan_digest'] == plan().digest
                assert type(checkpoint.state.get(ACK)) is int and checkpoint.state[ACK] == 1
                ledger = checkpoint.state['model_ledger']
                assert isinstance(ledger, tuple | list) and len(ledger) == 1
                row = ledger[0]
                assert isinstance(row, Mapping) and row['status'] == 'running'
                assert row['request_hash'] == _DiscussionDurability.request_hash(request)
                stored = await session.get(RunCheckpointRow, checkpoint.id)
                assert stored is not None and stored.tenant_id == self.tenant
                event = await session.scalar(select(RunEventRow).where(
                    RunEventRow.tenant_id == self.tenant, RunEventRow.run_id == self.run_id,
                    RunEventRow.sequence == stored.sequence))
                assert event is not None and event.kind == EventKind.CHECKPOINT_SAVED.value
                payload = event.payload.get('checkpoint')
                assert isinstance(payload, Mapping) and payload['id'] == str(checkpoint.id)
                assert await runs.latest_checkpoint(session, tenant_id=uuid4(), run_id=self.run_id) is None
                self.checkpoint = checkpoint
        except AssertionError as error:
            self.errors.append(str(error))
            raise
        finally:
            self.entered.set()
        # No model reply or business source is produced. Cancellation ends the real driver.
        await asyncio.Event().wait()
        raise AssertionError('fixed model was unexpectedly released')


class InspectingModel(CheckingModel):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if len(self.requests) == 1:
            assert request.logical_model == plan().selector_model and not request.tools
            response = ModelResponse(text='writer', usage=TokenUsage(1, 1, 2))
        else:
            assert len(self.requests) == 2, 'no model followup allowed in this boundary fixture'
            assert request.logical_model == 'offline-writer'
            assert request.required_tool == 'project_inspect'
            response = ModelResponse(text=None, usage=TokenUsage(1, 1, 2), tool_calls=(
                ToolCall(id='offline-inspect', name=request.required_tool, arguments={}),))
        return GatewayCompletion(response=response, deployment_id='offline',
            logical_model=request.logical_model, provider_id='offline', provider_model='offline/fixed',
            cost_usd=Decimal(0))


class CheckingInspectTools(ForbiddenTools):
    def __init__(self, model: CheckingModel) -> None:
        super().__init__()
        self.model = model
        self.entered = asyncio.Event()
        self.errors: list[str] = []
        self.checkpoint: RuntimeCheckpoint | None = None

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, *,
        user_id: UUID | None = None, role: Role | None = None,
    ) -> HarnessToolCallResult:
        self.calls.append(request)
        model = self.model
        try:
            assert model.run_id is not None
            assert tenant_id == model.tenant and request.run_id == model.run_id
            assert user_id == model.actor and role is Role.OPERATOR
            assert request.actor == 'writer' and request.tool_name == 'project.inspect'
            assert dict(request.arguments) == {}
            arguments_hash = hashlib.sha256(b'{}').hexdigest()
            expected_key = hashlib.sha256(
                f'{model.run_id}:writer:0:project.inspect:{arguments_hash}'.encode()).hexdigest()
            assert request.idempotency_key == expected_key
            runs = RunRepository(model.database.session_factory)
            record = await runs.get(model.tenant, model.run_id, actor_id=model.actor)
            assert record.actor_id == model.actor and record.actor_role is Role.OPERATOR
            assert record.status is RunStatus.RUNNING and record.worker_lease_token is not None
            assert record.routing_decision is not None
            assert model.project_id is not None
            assert record.routing_decision['project_id'] == model.project_id
            assert record.routing_decision['workspace_session_id'] == 'main'
            # This independent transaction must see the acknowledged tool intent, not
            # merely the runtime's in-memory checkpoint or an uncommitted driver row.
            async with model.database.session_factory() as session:
                checkpoint = await runs.latest_checkpoint(
                    session, tenant_id=model.tenant, run_id=model.run_id)
                assert checkpoint is not None, 'tool entered before running checkpoint committed'
                assert checkpoint.tenant_id == model.tenant and checkpoint.run_id == model.run_id
                assert checkpoint.mode is TaskMode.DISCUSS and checkpoint.state['terminal'] is False
                assert checkpoint.state['plan_digest'] == plan().digest
                assert type(checkpoint.state.get(ACK)) is int and checkpoint.state[ACK] == 1
                ledger = checkpoint.state['tool_ledger']
                assert isinstance(ledger, tuple | list) and len(ledger) == 1
                row = ledger[0]
                assert isinstance(row, Mapping)
                assert row['status'] == 'running' and row['actor'] == 'writer'
                assert type(row['position']) is int and row['position'] == 0
                assert row['name'] == request.tool_name and row['arguments_hash'] == arguments_hash
                assert row['idempotency_key'] == expected_key
                models = checkpoint.state['model_ledger']
                assert isinstance(models, tuple | list) and len(models) == 2
                for entry, model_request in zip(models, model.requests, strict=True):
                    assert isinstance(entry, Mapping) and entry['status'] == 'succeeded'
                    tool_names = {'project.inspect': 'project.inspect',
                                  'project_inspect': 'project.inspect'} if model_request.tools else None
                    assert entry['request_hash'] == _DiscussionDurability.request_hash(
                        model_request, tool_names=tool_names)
                assert checkpoint.state['artifact_registry'], 'fixed replies must use private storage'
                stored = await session.get(RunCheckpointRow, checkpoint.id)
                assert stored is not None and stored.tenant_id == model.tenant
                assert stored.run_id == model.run_id
                event = await session.scalar(select(RunEventRow).where(
                    RunEventRow.tenant_id == model.tenant, RunEventRow.run_id == model.run_id,
                    RunEventRow.sequence == stored.sequence))
                assert event is not None and event.kind == EventKind.CHECKPOINT_SAVED.value
                payload = event.payload.get('checkpoint')
                assert isinstance(payload, Mapping) and payload == checkpoint.to_payload()
                assert await runs.latest_checkpoint(session, tenant_id=uuid4(), run_id=model.run_id) is None
                assert await runs.latest_checkpoint(session, tenant_id=model.tenant, run_id=uuid4()) is None
                self.checkpoint = checkpoint
        except AssertionError as error:
            self.errors.append(str(error))
            raise
        finally:
            self.entered.set()
        # Observe only the harness entrance; no Root inspection or business I/O.
        await asyncio.Event().wait()
        raise AssertionError('fixed inspection was unexpectedly released')


def stack(
    database: Database, model: CheckingModel, repository: RunRepository | None = None,
    tool_gateway: ForbiddenTools | None = None,
) -> tuple[RunService, RunRepository, AutoGenDiscussionRuntime, ForbiddenTools]:
    runs = repository if repository is not None else RunRepository(database.session_factory)
    tools = tool_gateway if tool_gateway is not None else ForbiddenTools()
    runtime = AutoGenDiscussionRuntime(model, plan(), capability_gateway=tools,
        harness_tool_gateway=tools, artifact_repository=PostgresArtifactRepository(database.session_factory))
    service = RunService(runs, runtime_registry=RuntimeRegistry((runtime,)), router=None,
        task_queue=RecordingQueue(), worker_id=f'ack-native-{uuid4().hex}',
        runtime_timeout_seconds=30, runtime_token_budget=5000)
    return service, runs, runtime, tools


async def submit(service: RunService, model: CheckingModel) -> UUID:
    model.project_id = f'ack-project-{uuid4().hex}'
    submitted = await service.submit(tenant_id=model.tenant, actor_id=model.actor,
        actor_role=Role.OPERATOR, mode=TaskMode.DISCUSS,
        message='Build a small Node.js software project that adds two numbers. '
                'Implement source and tests, then deliver a project ZIP.',
        agent_ids=('writer', 'reviewer'), allow_scale_mode_upgrade=False,
        project_id=model.project_id, workspace_session_id='main',
        sandbox_profile='workspace_write', execution_backend='systemd', permission_mode='default',
        requested_permissions=('workspace.read', 'workspace.write', 'command.run'))
    model.run_id = submitted.id
    return submitted.id


async def test_model_entry_requires_committed_running_checkpoint(database_url: str) -> None:
    database = build_database(database_url)
    model = CheckingModel(database, uuid4(), uuid4())
    service, runs, runtime, tools = stack(database, model)
    execution: asyncio.Task[object] | None = None
    waiting: asyncio.Task[bool] | None = None
    try:
        run_id = await submit(service, model)
        before = await runs.get(model.tenant, run_id)
        assert before.routing_decision is not None
        assert before.routing_decision['generated_project_contract_version'] == 1
        execution = asyncio.create_task(service.execute(run_id))
        waiting = asyncio.create_task(model.entered.wait())
        done, _ = await asyncio.wait((execution, waiting), timeout=10,
                                     return_when=asyncio.FIRST_COMPLETED)
        assert done and model.entered.is_set(), 'driver ended or stalled before fixed model entry'
        assert not model.errors, model.errors
        assert model.checkpoint is not None and len(model.requests) == 1
        await service.cancel(model.tenant, run_id)
        await asyncio.wait_for(execution, timeout=10)
        record = await runs.get(model.tenant, run_id)
        assert record.status is RunStatus.CANCELLED and record.worker_lease_token is None
        assert not tools.calls
    finally:
        await runtime.cancel()
        for task in (execution, waiting):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (execution, waiting) if task is not None),
                             return_exceptions=True)
        await database.dispose()


async def test_checkpoint_persist_failure_rolls_back_before_model_io(database_url: str) -> None:
    database = build_database(database_url)
    model = CheckingModel(database, uuid4(), uuid4(), forbidden=True)
    rejecting = RejectIntentRepository(database.session_factory)
    service, runs, runtime, tools = stack(database, model, rejecting)
    try:
        run_id = await submit(service, model)
        await asyncio.wait_for(service.execute(run_id), timeout=10)
        assert rejecting.rejected is not None and rejecting.rejected.checkpoint is not None
        assert not model.requests and not tools.calls
        async with database.session_factory() as session:
            assert await session.get(RunCheckpointRow, rejecting.rejected.checkpoint.id) is None
            assert await session.scalar(select(RunEventRow.id).where(
                RunEventRow.run_id == run_id,
                RunEventRow.sequence == rejecting.rejected.sequence,
                RunEventRow.kind == EventKind.CHECKPOINT_SAVED.value)) is None
        record = await runs.get(model.tenant, run_id)
        assert record.status is RunStatus.FAILED and record.worker_lease_token is None
    finally:
        await runtime.cancel()
        await database.dispose()


async def test_service_stream_close_does_not_start_model_io(database_url: str) -> None:
    database = build_database(database_url)
    model = CheckingModel(database, uuid4(), uuid4(), forbidden=True)
    service, runs, runtime, tools = stack(database, model)
    try:
        run_id = await submit(service, model)
        await asyncio.wait_for(service.execute(run_id, crash_after_event_kind=EventKind.CHECKPOINT_SAVED),
                               timeout=10)
        async with database.session_factory() as session:
            checkpoint = await runs.latest_checkpoint(session, tenant_id=model.tenant, run_id=run_id)
        assert checkpoint is not None and checkpoint.state['terminal'] is False
        ledger = checkpoint.state['model_ledger']
        assert isinstance(ledger, tuple | list) and len(ledger) == 1
        assert isinstance(ledger[0], Mapping) and ledger[0]['status'] == 'running'
        assert checkpoint.state[ACK] == 1
        assert not model.requests and not tools.calls
    finally:
        await runtime.cancel()
        await database.dispose()


async def test_fresh_pool_running_checkpoint_is_unknown_without_replay(database_url: str) -> None:
    original = build_database(database_url)
    model = CheckingModel(original, uuid4(), uuid4(), forbidden=True)
    service, runs, runtime, tools = stack(original, model)
    try:
        run_id = await submit(service, model)
        await asyncio.wait_for(service.execute(run_id, crash_after_event_kind=EventKind.CHECKPOINT_SAVED),
                               timeout=10)
        assert not model.requests and not tools.calls
        async with original.session_factory() as session, session.begin():
            row = await runs.get_for_update(session, run_id)
            assert row.tenant_id == model.tenant and row.status == RunStatus.RUNNING.value
            # Simulate loss of this test's first worker only, never a historical run.
            row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    finally:
        await runtime.cancel()
        await original.dispose()
    fresh = build_database(database_url)
    recovered_model = CheckingModel(fresh, model.tenant, model.actor, forbidden=True)
    recovered_model.run_id = run_id
    recovered, fresh_runs, fresh_runtime, fresh_tools = stack(fresh, recovered_model)
    try:
        await asyncio.wait_for(recovered.execute(run_id, allow_running_recovery=True), timeout=10)
        assert not recovered_model.requests and not fresh_tools.calls
        record = await fresh_runs.get(model.tenant, run_id, actor_id=model.actor)
        assert record.status is RunStatus.FAILED and record.worker_lease_token is None
        events = await fresh_runs.raw_events(model.tenant, run_id)
        assert any(event.kind is EventKind.RUNTIME_FAILED and event.reason == 'model_outcome_uncertain'
                   for event in events)
    finally:
        await fresh_runtime.cancel()
        await fresh.dispose()


async def test_tool_entry_requires_committed_running_checkpoint(database_url: str) -> None:
    database = build_database(database_url)
    model = InspectingModel(database, uuid4(), uuid4())
    inspection = CheckingInspectTools(model)
    service, runs, runtime, _ = stack(database, model, tool_gateway=inspection)
    execution: asyncio.Task[object] | None = None
    waiting: asyncio.Task[bool] | None = None
    try:
        run_id = await submit(service, model)
        execution = asyncio.create_task(service.execute(run_id))
        waiting = asyncio.create_task(inspection.entered.wait())
        done, _ = await asyncio.wait((execution, waiting), timeout=10,
                                     return_when=asyncio.FIRST_COMPLETED)
        assert done and inspection.entered.is_set(), 'driver ended or stalled before inspection entry'
        assert not inspection.errors, inspection.errors
        assert inspection.checkpoint is not None
        assert len(model.requests) == 2 and len(inspection.calls) == 1
        await service.cancel(model.tenant, run_id)
        await asyncio.wait_for(execution, timeout=10)
        record = await runs.get(model.tenant, run_id)
        assert record.status is RunStatus.CANCELLED and record.worker_lease_token is None
    finally:
        await runtime.cancel()
        for task in (execution, waiting):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (execution, waiting) if task is not None),
                             return_exceptions=True)
        await database.dispose()


async def test_tool_intent_persist_failure_rolls_back_before_tool_io(database_url: str) -> None:
    database = build_database(database_url)
    model = InspectingModel(database, uuid4(), uuid4())
    rejecting = RejectToolIntentRepository(database.session_factory)
    service, runs, runtime, tools = stack(database, model, rejecting)
    try:
        run_id = await submit(service, model)
        await asyncio.wait_for(service.execute(run_id), timeout=10)
        assert rejecting.rejected is not None and rejecting.rejected.checkpoint is not None
        assert len(model.requests) == 2 and not tools.calls
        assert is_running_intent(rejecting.rejected, 'tool_ledger')
        async with database.session_factory() as session:
            assert await session.get(RunCheckpointRow, rejecting.rejected.checkpoint.id) is None
            assert await session.scalar(select(RunEventRow.id).where(
                RunEventRow.tenant_id == model.tenant, RunEventRow.run_id == run_id,
                RunEventRow.sequence == rejecting.rejected.sequence,
                RunEventRow.kind == EventKind.CHECKPOINT_SAVED.value)) is None
        record = await runs.get(model.tenant, run_id)
        assert record.status is RunStatus.FAILED and record.worker_lease_token is None
    finally:
        await runtime.cancel()
        await database.dispose()
