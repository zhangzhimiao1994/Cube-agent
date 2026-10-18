from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from asyncpg.pgproto.pgproto import UUID as DriverUUID

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository


def test_postgres_driver_uuid_identity_is_canonical_at_repository_boundary():
    identifiers={key:DriverUUID(str(uuid4())) for key in (
        'id','tenant_id','actor_id','blocked_by_run_id','worker_lease_token')}
    assert all(isinstance(value,UUID) and type(value) is not UUID for value in identifiers.values())
    row=SimpleNamespace(**identifiers,actor_role='operator',request='Owned diagnostic',mode='direct',
        status='running',version=1,created_at=datetime.now(UTC),routing_decision={},worker_id='worker',
        worker_lease_expires_at=None,worker_heartbeat_at=None)
    record=RunRepository._record(row)
    for key,value in identifiers.items():
        assert type(getattr(record,key)) is UUID
        assert getattr(record,key)==value
    assert record.status is RunStatus.RUNNING and record.mode is TaskMode.DIRECT


def test_nullable_database_uuid_fields_remain_none():
    row=SimpleNamespace(id=uuid4(),tenant_id=uuid4(),actor_id=None,actor_role=None,request='Read',
        mode=None,status='queued',version=1,created_at=datetime.now(UTC),routing_decision=None,
        blocked_by_run_id=None,worker_lease_token=None)
    record=RunRepository._record(row)
    assert record.actor_id is None and record.blocked_by_run_id is None and record.worker_lease_token is None
