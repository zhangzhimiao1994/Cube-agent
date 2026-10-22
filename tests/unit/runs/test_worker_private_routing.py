"""Private verifier indexing stays in persistence, never in a runtime/model snapshot."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import get_verification_authority
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import RunEvent, TaskContext
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.unit.capabilities.test_verification_cold_recovery import Fixture
from tests.unit.runs.test_terminal_hooks import RuntimeCompletes


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('bound', [False, True])
async def test_private_route_is_removed_without_mutating_record_or_adopting_lease(tmp_path: Path, mode: TaskMode, bound: bool) -> None:
    fixture = Fixture(tmp_path)
    fixture.db.row.mode = mode.value
    index = {'authority': {'execution_token': str(uuid4())}, 'artifact_references': [{'id': str(uuid4()), 'sha256': 'a'*64}]}
    token = str(uuid4())
    fixture.db.row.routing_decision.update({'project_verification_receipt': index,
                                          'approval_checkpoint_worker_token': token,
                                          'approval_id': 'approval_keep', 'unrelated': {'keep': [1, False]}})
    original = fixture.context().model_copy(update={'mode': mode})

    class Runtime(RuntimeCompletes):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            assert 'project_verification_receipt' not in context.routing_decision
            assert 'approval_checkpoint_worker_token' not in context.routing_decision
            assert context.routing_decision['approval_id'] == 'approval_keep'
            assert context.routing_decision['unrelated'] == {'keep': (1, False)}
            assert context.execution_token == original.execution_token
            assert context.instruction_context is original.instruction_context
            assert 'execution_token' not in str(context.to_payload())
            if bound:
                assert context.actor_id is not None
                authority = get_verification_authority(tenant_id=context.tenant_id, run_id=context.run_id,
                    user_id=context.actor_id, role=Role.OPERATOR,
                    current_record=await fixture.runs.get(context.tenant_id, context.run_id))
                assert authority.execution_token == original.execution_token
            yield RunEvent(kind='runtime.completed', sequence=1, run_id=context.run_id)

    async with worker_runtime_events(Runtime(), original, worker_id=fixture.db.row.worker_id if bound else None) as stream:
        assert len([event async for event in stream]) == 1
    assert fixture.db.row.routing_decision['project_verification_receipt'] == index
    assert fixture.db.row.routing_decision['approval_checkpoint_worker_token'] == token
    assert 'project_verification_receipt' in original.routing_decision
