from __future__ import annotations

from typing import Any, cast

import pytest

from agent_hub.domain.runs import RunStatus
from tests.unit.runs.test_recovery_guard import (
    _CapabilityApprovalSession,
    _CapabilityApprovalSessionFactory,
    _RecoveryBlockingRepository,
)


@pytest.mark.parametrize('phase,version,expected', [
    ('captured', 1, False), ('observed', 1, True), ('executing', 1, True),
    ('captured', True, True), ('unknown', 1, True),
])
async def test_only_pre_model_captured_input_can_wait_without_runtime_checkpoint(
    phase: str, version: object, expected: bool,
) -> None:
    repository = _RecoveryBlockingRepository(status=RunStatus.RUNNING, routing_decision={
        'project_input_contract_version': version,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']},
        'project_input_preparation': {'schema_version': 1, 'phase': phase,
            'reference': {'schema_version': 1, 'contract_sha256': 'a' * 64,
                          'input_source_sha256': 'b' * 64, 'file_count': 3}},
    }, blocked_after_sequence=0)
    row = repository.row
    row.worker_id, row.worker_lease_token = 'worker-current', repository.lease_token
    session = _CapabilityApprovalSession(row, approved=False)
    repository._session_factory = cast(Any, _CapabilityApprovalSessionFactory(session))
    record = await repository.begin_capability_approval(row.tenant_id, row.id,
        approval_id='approval-1', approval_fingerprint='fingerprint-1')
    assert record.status is RunStatus.WAITING_APPROVAL
    assert record.routing_decision is not None
    assert record.routing_decision['approval_checkpoint_required'] is expected
