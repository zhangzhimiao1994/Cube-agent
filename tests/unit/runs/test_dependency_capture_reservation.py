"""Actual reservation method with a mock session; not a live PostgreSQL proof."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_hub.capabilities.generated_project_service import generated_authority
from agent_hub.runs.repository import RunConflict
from tests.unit.capabilities.test_generated_project_service import USER
from tests.unit.capabilities.test_generated_python_dependency_closure import (
    draft as draft,  # noqa: PLC0414 - shared fixture with real archives, simulated Root.
)
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path
from tests.unit.runs.test_database_uuid_boundary import _repository_session


@pytest.fixture
async def reservation(draft: Any) -> Any:
    service, repository, corrected = draft
    row = repository.row
    row.routing_decision['python_dependency_preflight_version'] = 1
    (service.root / project_path() / 'requirements.lock').write_text(corrected, encoding='utf-8')
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    state = dict(context.state)
    state.update(phase='capturing')
    state.pop('reference')
    state.pop('acceptance_sha256')
    row.routing_decision.pop('generated_project_preparation')
    row.mode, row.version, row.created_at = 'direct', 1, datetime.now(UTC)
    dao, session = _repository_session()
    session.scalar.return_value = row
    authority = generated_authority(row, TENANT, RUN)
    kwargs = {'actor_id': USER, 'actor_role': authority.role, 'lease_token': authority.lease_token,
        'request_sha256': authority.request_sha256, 'project_id': authority.project_id,
        'session_id': authority.session_id, 'expected': None, 'replacement': state}
    return dao, session, row, kwargs


async def test_guard_runs_under_row_lock_before_any_reservation(reservation: Any) -> None:
    dao, session, row, kwargs = reservation
    entered = []

    async def check() -> None:
        assert session.scalar.await_count == 1 and session.flush.await_count == 0
        statement = str(session.scalar.call_args.args[0])
        assert 'FOR UPDATE' in statement
        assert 'generated_project_preparation' not in row.routing_decision
        entered.append(True)

    result = await dao.compare_and_set_generated_preparation(TENANT, RUN,
        **kwargs, before_reservation=check)
    assert entered == [True] and session.flush.await_count == 1
    assert result.routing_decision['generated_project_preparation']['phase'] == 'capturing'


@pytest.mark.parametrize('defect', ['missing_guard', 'missing_policy', 'bool_policy', 'failed_guard', 'expired_lease'])
async def test_reservation_refuses_unbound_or_stale_guard_without_writing(reservation: Any, defect: str) -> None:
    dao, session, row, kwargs = reservation

    async def check() -> None:
        if defect == 'failed_guard':
            raise ValueError('known draft changed')
        if defect == 'expired_lease':
            row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    if defect == 'missing_policy':
        row.routing_decision.pop('python_dependency_preflight_version')
    elif defect == 'bool_policy':
        row.routing_decision['python_dependency_preflight_version'] = True
    if defect != 'missing_guard':
        kwargs['before_reservation'] = check
    with pytest.raises((RunConflict, ValueError, RuntimeError)):
        await dao.compare_and_set_generated_preparation(TENANT, RUN, **kwargs)
    assert session.flush.await_count == 0 and row.version == 1
    assert 'generated_project_preparation' not in row.routing_decision
