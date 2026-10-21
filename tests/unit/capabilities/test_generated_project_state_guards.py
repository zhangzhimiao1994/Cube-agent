from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectError,
    generated_authority,
    validate_generated_preparation,
    validate_generated_transition,
)
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
)
from tests.unit.capabilities.test_generated_project_service import USER, Repository, setup
from tests.unit.capabilities.test_scoped_read import RUN, TENANT


class DriverUUID(UUID):
    """Model asyncpg's UUID subtype without requiring a database connection."""


def supply(phase: str) -> dict[str, object]:
    plan = PythonInputSupplyPlan.model_validate({
        'scope': {'tenant_id': TENANT, 'run_id': RUN, 'project_id': 'project-a', 'session_id': 'session-a',
            'original_reference': {'schema_version': 1, 'file_count': 6,
                'input_source_sha256': '1' * 64, 'contract_sha256': '2' * 64}},
        'declaration_sha256': '3' * 64, 'manifest_sha256': '4' * 64,
        'lock_sha256': '5' * 64, 'catalog_sha256': '6' * 64,
        'interpreter': {'implementation': 'cpython', 'version': '3.12.14',
            'abi': 'cp312', 'platform': 'linux_x86_64', 'binary_sha256': '7' * 64,
            'base_runtime_sha256': '8' * 64},
        'artifacts': ({'filename': 'packaging-26.0-py3-none-any.whl', 'name': 'packaging',
            'version': '26.0', 'sha256': '9' * 64, 'size_bytes': 100},)}, strict=True)
    result: dict[str, object] = {'schema_version': 1, 'phase': phase,
        'plan': plan.model_dump(mode='json'), 'plan_sha256': plan.sha256}
    if phase != 'planned':
        result['execution_id'] = 'input_supply_' + 'a' * 32
    if phase == 'supplied':
        result['receipt'] = PythonInputSupplyReceipt(plan=plan,
            supply_execution_id='input_supply_' + 'a' * 32,
            published=('packaging-26.0-py3-none-any.whl',), reused=()).model_dump(mode='json')
    return result


def state(supply_phase: str | None, *, ready: bool = False) -> dict[str, object]:
    result: dict[str, object] = {'schema_version': 1, 'phase': 'python_ready' if ready else 'captured',
        'user_id': str(USER), 'request_sha256': 'a' * 64, 'proposer_actor': 'engineer',
        'stack': 'python', 'manifest_sha256': 'b' * 64, 'manifest_raw_sha256': 'c' * 64,
        'acceptance_sha256': 'd' * 64, 'reference': {'schema_version': 1, 'file_count': 6,
            'input_source_sha256': '1' * 64, 'contract_sha256': '2' * 64}}
    if supply_phase is not None:
        result['python_input_supply'] = supply(supply_phase)
    if ready:
        result['python_recipe'] = {'schema_version': 1, 'recipe_sha256': 'e' * 64,
            'environment_input_sha256': 'f' * 64}
    return result


@pytest.mark.parametrize(('before', 'after'), [(None, 'executing'), (None, 'supplied'),
    ('planned', 'supplied'), ('executing', 'planned'), ('supplied', 'planned'), ('supplied', 'executing')])
def test_supply_cannot_skip_or_rewind_intent(before: str | None, after: str) -> None:
    with pytest.raises(GeneratedProjectError):
        validate_generated_transition(state(before), state(after))


@pytest.mark.parametrize(('before', 'after'), [(None, 'planned'), ('planned', 'planned'),
    ('planned', 'executing'), ('executing', 'executing'), ('executing', 'supplied'), ('supplied', 'supplied')])
def test_supply_monotonic_transitions_preserve_retry_intent(before: str | None, after: str) -> None:
    assert validate_generated_transition(state(before), state(after))['phase'] == 'captured'


@pytest.mark.parametrize('phase', ['planned', 'executing'])
def test_python_ready_never_contains_incomplete_supply(phase: str) -> None:
    with pytest.raises(GeneratedProjectError):
        validate_generated_preparation(state(phase, ready=True))


def test_supplied_recipe_can_become_ready_without_changing_supply() -> None:
    assert validate_generated_transition(state('supplied'), state('supplied', ready=True))['phase'] == 'python_ready'


@pytest.mark.parametrize('phase', ['planned', 'executing', 'supplied'])
def test_capture_completion_cannot_create_supply(phase: str) -> None:
    before = state(None)
    before['phase'] = 'capturing'
    del before['reference'], before['acceptance_sha256']
    with pytest.raises(GeneratedProjectError):
        validate_generated_transition(before, state(phase))


@pytest.mark.parametrize('phase', ['executing', 'supplied'])
def test_supply_execution_identity_is_never_replaced(phase: str) -> None:
    after = state(phase)
    altered = cast(dict[str, object], after['python_input_supply'])
    altered['execution_id'] = 'input_supply_' + 'b' * 32
    if phase == 'supplied':
        cast(dict[str, object], altered['receipt'])['supply_execution_id'] = altered['execution_id']
    with pytest.raises(GeneratedProjectError):
        validate_generated_transition(state('executing'), after)


def test_generated_authority_normalizes_driver_uuid_scope() -> None:
    authority = generated_authority(Repository().row, DriverUUID(str(TENANT)), DriverUUID(str(RUN)))
    assert type(authority.tenant_id) is UUID and type(authority.run_id) is UUID


async def test_generated_capture_accepts_driver_uuid_scope_without_consuming_broken_capture(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    context = await service.prepare(DriverUUID(str(TENANT)), DriverUUID(str(RUN)), 'engineer', user_id=USER)
    assert context is not None and repo.updates == 2
    loaded = await service.load(DriverUUID(str(TENANT)), DriverUUID(str(RUN)), 'tester', readonly=True)
    assert loaded is not None and loaded.envelope.sha256 == context.envelope.sha256


async def test_service_cas_rejects_invalid_transition_before_calling_repository(tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo = setup(tmp_path)
    setter = AsyncMock()
    monkeypatch.setattr(repo, 'compare_and_set_generated_preparation', setter)
    with pytest.raises(GeneratedProjectError):
        await service._cas(generated_authority(repo.row, TENANT, RUN),
            validate_generated_preparation(state(None)), validate_generated_preparation(state('supplied')))
    setter.assert_not_awaited()


@pytest.mark.parametrize('defect', ['none', 'stale-state', 'lease', 'expiry', 'user', 'request', 'workspace', 'skip-intent'])
async def test_real_dao_cas_guards_locked_state_and_authority(defect: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the real DAO decision path, not a replacement implementation of CAS.
    # The session is in-memory; this does not prove PostgreSQL lock scheduling.
    row = Repository().row
    row.version = 1
    row.routing_decision['generated_project_preparation'] = state('planned')
    authority = generated_authority(row, TENANT, RUN)
    expected = validate_generated_preparation(state('planned'))
    replacement = validate_generated_preparation(state('executing'))
    if defect == 'stale-state':
        expected = validate_generated_preparation(state(None))
    elif defect == 'lease':
        row.worker_lease_token = UUID(int=99)
    elif defect == 'expiry':
        row.worker_lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
    elif defect == 'user':
        row.actor_id = UUID(int=99)
    elif defect == 'request':
        row.request = 'changed'
    elif defect == 'workspace':
        row.routing_decision['project_id'] = 'another'
    elif defect == 'skip-intent':
        replacement = validate_generated_preparation(state('supplied'))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.scalar = AsyncMock(return_value=row)
    session.flush = AsyncMock()
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock(return_value=session)))
    monkeypatch.setattr(repository, '_record', lambda raw: cast(RunRecord, raw))

    async def commit() -> RunRecord:
        assert authority.lease_token is not None
        return await repository.compare_and_set_generated_preparation(TENANT, RUN,
            actor_id=authority.user_id, actor_role=authority.role, lease_token=authority.lease_token,
            request_sha256=authority.request_sha256, project_id=authority.project_id,
            session_id=authority.session_id, expected=expected, replacement=replacement)

    if defect == 'none':
        await commit()
        assert row.version == 2
        current = cast(Mapping[str, object], row.routing_decision['generated_project_preparation'])
        assert cast(Mapping[str, object], current['python_input_supply'])['phase'] == 'executing'
        session.flush.assert_awaited_once()
    else:
        with pytest.raises((RunConflict, GeneratedProjectError)):
            await commit()
        assert row.version == 1
        session.flush.assert_not_awaited()
    statement = session.scalar.call_args.args[0]
    assert statement._for_update_arg is not None
