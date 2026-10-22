"""Private DAO indexes stay readable without entering typed public projections.

Handlers and serializers run in process; no HTTP/provider/SQL network is used.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    PersistentAdminResourceService,
    RunDetailResponse,
)
from agent_hub.api.routers.runs import (
    CreateRunRequest,
    RunSummaryResponse,
    SubmittedRunResponse,
    _record_run_submit_audit,
    get_run,
    run_details,
)
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.harness.events import harness_started_event
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY, VerificationRecord
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, TaskQueue, _submitted
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.project_scale_artifact import project_scale_artifact_zip_arguments
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_project_verification_receipts import Database, passed


@dataclass
class IndexedRun:
    db: Database
    issued: VerificationRecord
    record: RunRecord

    def assert_public(self, serialized: str) -> None:
        assert RECEIPT_KEY not in serialized
        assert 'execution_token' not in serialized
        assert str(self.issued.authority.execution_token) not in serialized
        for reference in self.issued.artifact_references:
            assert str(reference.id) not in serialized
            assert reference.sha256 not in serialized

    def context(self) -> TaskContext:
        record = self.record
        assert record.mode is not None
        return TaskContext(run_id=record.id, tenant_id=record.tenant_id,
            actor_id=record.actor_id, actor_role=record.actor_role,
            execution_token=record.worker_lease_token, mode=record.mode,
            request=record.request, routing_decision=record.routing_decision or {})


@pytest.fixture
async def indexed_run() -> AsyncIterator[IndexedRun]:
    db = Database()
    db.row.routing_decision.update({
        'conversation_id': 'review-conversation',
        'harness_decision': {
            'selected_provider': 'fixture', 'selected_model': 'fixture-model',
            'selected_logical_model': 'main', 'mode': 'direct', 'requires_approval': False,
        },
    })
    issued = await passed(db)
    original = deepcopy(db.row.routing_decision)
    yield IndexedRun(db, issued, RunRepository._record(db.row))
    assert db.row.routing_decision == original
    assert RunRepository._record(db.row).routing_decision[RECEIPT_KEY] == issued.to_payload()
    assert await db.repository().load(db.authority()) == issued


def read_repository(indexed: IndexedRun, monkeypatch: pytest.MonkeyPatch) -> RunRepository:
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.scalar.return_value = indexed.db.row
    repository = RunRepository(cast(async_sessionmaker[AsyncSession],
        MagicMock(return_value=session)))
    for name, value in (
        ('completed_step_ids', ()), ('artifact_ids', ()), ('usage_cost', Decimal(0)),
        ('events', ()), ('artifacts', ()),
    ):
        monkeypatch.setattr(repository, name, AsyncMock(return_value=value))
    return repository


@pytest.mark.parametrize('handler', [get_run, run_details], ids=['get', 'details'])
async def test_owned_user_handler_serializes_no_private_index(
    indexed_run: IndexedRun, monkeypatch: pytest.MonkeyPatch, handler: Any,
) -> None:
    record = indexed_run.record
    repository = read_repository(indexed_run, monkeypatch)
    service = RunService(repository, runtime_registry=cast(RuntimeRegistry, object()),
        router=None, task_queue=cast(TaskQueue, object()))
    assert record.actor_id is not None
    principal = AuthenticatedPrincipal(record.actor_id, record.tenant_id, Role.OPERATOR)

    response = await handler(record.id, service=service, principal=principal)

    assert type(response) is RunSummaryResponse
    assert response.id == record.id and response.request == record.request
    indexed_run.assert_public(response.model_dump_json())


async def test_submitted_run_real_response_serializer_excludes_private_index(
    indexed_run: IndexedRun,
) -> None:
    response = SubmittedRunResponse.from_submitted(_submitted(indexed_run.record))

    assert response.id == indexed_run.record.id
    assert response.project_id == 'project-a'
    assert response.workspace_session_id == 'session-a'
    indexed_run.assert_public(response.model_dump_json())


async def test_persistent_admin_run_details_serializer_excludes_private_index(
    indexed_run: IndexedRun, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = indexed_run.record
    assert record.actor_id is not None
    admin = PersistentAdminResourceService(config_service=cast(Any, object()),
        secret_service=cast(Any, object()), tenant_id=record.tenant_id,
        actor_id=record.actor_id, model_transport=cast(Any, object()),
        run_repository=read_repository(indexed_run, monkeypatch))
    monkeypatch.setattr(admin, 'list_plugins', AsyncMock(return_value=()))
    monkeypatch.setattr(admin, 'list_audit_events', AsyncMock(return_value=()))

    response = await admin._run_detail(record)

    assert type(response) is RunDetailResponse
    assert response.explicit_details['project_id'] == 'project-a'
    indexed_run.assert_public(response.model_dump_json())


async def test_real_run_submit_audit_records_only_public_submission_fields(
    indexed_run: IndexedRun,
) -> None:
    record = indexed_run.record
    assert record.actor_id is not None and record.mode is not None
    admin = InMemoryAdminResourceService()
    request = Request({'type': 'http', 'app': SimpleNamespace(
        state=SimpleNamespace(admin_resource_service=admin))})
    principal = AuthenticatedPrincipal(record.actor_id, record.tenant_id, Role.OPERATOR)
    body = CreateRunRequest(message=record.request, mode=record.mode)

    await _record_run_submit_audit(request, principal, body, _submitted(record))

    events = [event for event in await admin.list_audit_events()
              if event.action == 'run.submit' and event.resource == str(record.id)]
    assert len(events) == 1
    assert events[0].details['project_id'] == 'project-a'
    indexed_run.assert_public(events[0].model_dump_json())


async def test_direct_actual_model_request_messages_exclude_private_route(
    indexed_run: IndexedRun,
) -> None:
    runtime = DirectRuntime(cast(Any, object()), logical_model='main')

    outcome = runtime._build_request(indexed_run.context())

    assert outcome.error_code is None and outcome.request is not None
    assert outcome.request.logical_model == 'main'
    messages = [{'role': message.role, 'content': message.content}
                for message in outcome.request.messages]
    assert indexed_run.record.request in json.dumps(messages)
    indexed_run.assert_public(json.dumps(messages))


async def test_harness_and_tool_routing_export_only_selected_public_fields(
    indexed_run: IndexedRun,
) -> None:
    context = indexed_run.context()
    event = harness_started_event(routing_decision=context.routing_decision,
        run_id=context.run_id, start_sequence=1)
    assert event is not None and event.payload['provider'] == 'fixture'
    indexed_run.assert_public(json.dumps(event.to_payload(), default=dict))
    arguments = project_scale_artifact_zip_arguments(context)
    assert arguments is not None and arguments['project_id'] == 'project-a'
    request = HarnessToolCallRequest(run_id=context.run_id, actor='implementer',
        tool_name='project.generate_zip', arguments=arguments, approval_required=False,
        sandbox='workspace_write', idempotency_key='projection-review')
    indexed_run.assert_public(json.dumps(dict(request.to_payload()), default=dict))
