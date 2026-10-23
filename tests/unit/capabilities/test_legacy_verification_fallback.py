"""Legacy workers must not bypass an action worker's durable execution index."""

from pathlib import Path

import pytest

from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.capabilities.test_verification_cold_recovery import Fixture, verify


def legacy(fixture: Fixture) -> ProjectVerificationService:
    return ProjectVerificationService(root=fixture.root, repository=fixture.runs, executor=fixture.executor)


@pytest.mark.parametrize('state', ['running', 'failed', 'passed', 'malformed'])
async def test_legacy_verifier_cannot_repeat_durable_or_unknown_candidate(tmp_path: Path, state: str) -> None:
    fixture = Fixture(tmp_path)
    with fixture.bind():
        await verify(fixture.verifier())
    if state != 'passed':
        fixture.db.row.routing_decision[RECEIPT_KEY] = None if state == 'malformed' else {'status':state}
    with pytest.raises(ProjectVerificationError, match='durable verification recovery required'):
        await legacy(fixture).verify(tenant_id=TENANT, run_id=RUN, actor='main_agent', action_key='legacy-new-key')
    assert len(fixture.executor.calls) == 1


async def test_warm_legacy_delivery_is_revoked_when_durable_execution_appears(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    service = legacy(fixture)
    await service.verify(tenant_id=TENANT, run_id=RUN, actor='main_agent')
    assert await service.require_current_receipt(TENANT, RUN)
    fixture.db.row.routing_decision[RECEIPT_KEY] = {'status':'running'}
    with pytest.raises(ProjectVerificationError, match='durable verification recovery required'):
        await service.require_current_receipt(TENANT, RUN)
    assert not service._receipts
    assert len(fixture.executor.calls) == 1
