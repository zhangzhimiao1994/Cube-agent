"""An expired durable pool must not keep an unchanged historical run busy."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agent_hub.api.routers.admin import PersistentAdminResourceService
from agent_hub.api.routers.runs import RunSummaryResponse
from agent_hub.db.models import RunAdmissionPoolRow, RunRow
from agent_hub.runs.conversation_queue import (
    ConversationQueueConflict,
    ConversationQueueRepository,
    EnqueueConversationMessage,
)
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService


def fixture(*, deadline_seconds=-60, worker=False, pool_owner=True):
    now = datetime.now(UTC)
    row = RunRow(id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role='operator',
        request='Retained approval', mode='discuss', status='waiting_approval', version=15,
        created_at=now-timedelta(days=1), routing_decision={
            'conversation_id':'existing', 'runtime_timeout_absolute_seconds':3600,
            'approval_review_profile':{'timeout_seconds':3600}},
        worker_id='held' if worker else None, worker_lease_token=uuid4() if worker else None,
        worker_lease_expires_at=now-timedelta(seconds=1) if worker else None)
    pool = None if deadline_seconds is None else RunAdmissionPoolRow(
        run_id=row.id, tenant_id=row.tenant_id, actor_id=row.actor_id if pool_owner else uuid4(),
        actor_role=row.actor_role, original_deadline=now+timedelta(seconds=deadline_seconds))
    session = MagicMock()
    session.__aenter__.return_value = session
    session.begin.return_value.__aenter__.return_value = session
    session.scalar = AsyncMock(return_value=row)
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [] if pool is None else [pool]))
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return row, pool, session, RunRepository(lambda: session)


@pytest.mark.asyncio
@pytest.mark.parametrize(('seconds','worker','owner','active'), [
    (-60,False,True,False), (60,False,True,True), (None,False,True,True),
    (-60,True,True,True), (-60,False,False,True),
])
async def test_get_projects_only_owned_pool_deadline_without_history_writes(seconds,worker,owner,active):
    row,pool,session,repo=fixture(deadline_seconds=seconds,worker=worker,pool_owner=owner)
    before=(row.status,row.version,row.routing_decision.copy(),None if pool is None else pool.original_deadline)
    record=await repo.get(row.tenant_id,row.id)
    activity=getattr(record,'execution_activity',None)
    assert activity is not None, 'read API needs server-derived activity, not status-only busy'
    assert activity.is_active is active
    if seconds is None or not owner: assert activity.deadline_at is None
    else: assert activity.deadline_at==pool.original_deadline
    assert before==(row.status,row.version,row.routing_decision,None if pool is None else pool.original_deadline)
    session.add.assert_not_called();session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_and_conversation_reads_carry_the_same_projection():
    row,pool,session,repo=fixture()
    session.scalars.side_effect=[SimpleNamespace(all=lambda:[row]),SimpleNamespace(all=lambda:[pool])]
    recent=await repo.list_recent(row.tenant_id)
    assert getattr(recent[0],'execution_activity',None) is not None
    assert recent[0].execution_activity.is_active is False
    session.scalars.side_effect=[SimpleNamespace(all=lambda:[row]),SimpleNamespace(all=lambda:[pool])]
    history=await repo.list_conversation(row.tenant_id,'existing')
    assert history[0].execution_activity.is_active is False


@pytest.mark.asyncio
async def test_admin_and_public_reads_preserve_server_activity():
    row,_,_,repo=fixture()
    record=await repo.get(row.tenant_id,row.id)
    repo.usage_cost=AsyncMock(return_value=Decimal(0))
    repo.events=AsyncMock(return_value=())
    repo.artifacts=AsyncMock(return_value=())
    repo.completed_step_ids=AsyncMock(return_value=())
    repo.artifact_ids=AsyncMock(return_value=())
    admin=object.__new__(PersistentAdminResourceService)
    admin._run_repository=repo;admin._tenant_id=row.tenant_id
    for response in [await admin._run_list_item(record),await admin._run_detail(record)]:
        assert response.model_dump().get('execution_activity',{}).get('is_active') is False
        assert response.status=='waiting_approval' and response.version==15
    service=RunService(repo,runtime_registry=MagicMock(),router=None,task_queue=MagicMock())
    response=RunSummaryResponse.from_summary(await service._summary(record))
    assert response.model_dump().get('execution_activity',{}).get('is_active') is False


@pytest.mark.asyncio
async def test_service_does_not_create_a_successor_behind_expired_approval():
    row,_,_,repo=fixture()
    record=await repo.get(row.tenant_id,row.id)
    repo.list_conversation=AsyncMock(return_value=(record,))
    queue=MagicMock();queue.enqueue=AsyncMock()
    service=RunService(repo,runtime_registry=MagicMock(),router=None,task_queue=MagicMock(),
        conversation_queue_repository=queue)
    service.submit=AsyncMock(side_effect=AssertionError('expired predecessor must not create blocked work'))
    with pytest.raises(ConversationQueueConflict,match='no active run'):
        await service.queue_message(tenant_id=row.tenant_id,actor_id=row.actor_id,actor_role=None,
            conversation_id='existing',message='New work',mode=record.mode,idempotency_key='one')
    service.submit.assert_not_awaited();queue.enqueue.assert_not_awaited()
    assert row.status=='waiting_approval' and row.version==15


@pytest.mark.asyncio
async def test_durable_queue_rechecks_expiry_inside_its_transaction():
    row,_pool,session,_=fixture()
    successor=RunRow(id=uuid4(),tenant_id=row.tenant_id,actor_id=row.actor_id,actor_role='operator',
        request='new',status='queued',mode='discuss',version=1,
        blocked_by_run_id=row.id,routing_decision={'conversation_id':'existing'})
    session.scalar.side_effect=[None,row,successor,None,0]
    queue=ConversationQueueRepository(lambda:session)
    with pytest.raises(ConversationQueueConflict,match='no active run'):
        await queue.enqueue(EnqueueConversationMessage(tenant_id=row.tenant_id,
            conversation_id='existing',predecessor_run_id=row.id,successor_run_id=successor.id,
            message='new',idempotency_key='one'))
    session.add.assert_not_called()
    assert row.status=='waiting_approval' and row.version==15
