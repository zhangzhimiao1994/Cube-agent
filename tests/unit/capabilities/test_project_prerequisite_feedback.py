from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_project_verification import Executor, gateway, verify
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.harness.test_tool_gateway import (
    USER_ID,
    ErrorRuntimeCapabilityGateway,
    FakePolicyGateway,
)


class BlockedExecutor(Executor):
    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        self.calls.append(invocation)
        source = await project_source_fingerprint(self.root, invocation)
        return SkillResult(exit_code=0, timed_out=False, stderr='', stdout=json.dumps({
            'schema_version': 1, 'status': 'blocked', 'source_sha256': source,
            'reason': 'node_runtime_missing', 'runtime': {'node': '', 'npm': ''},
            'checks': [{'phase': phase, 'status': 'not_run', 'exit_code': None,
                        'timed_out': False, 'stdout': '', 'stderr': ''}
                       for phase in ('install', 'build', 'test')],
        }))


async def test_actual_gateway_preserves_missing_runtime_without_fake_execution(tmp_path: Path) -> None:
    executor = BlockedExecutor()
    backend = gateway(tmp_path, executor)
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(backend)
    observation = getattr(caught.value, 'project_prerequisite', None)
    assert observation is not None
    payload = observation.to_payload()
    assert payload['status'] == 'blocked' and payload['reason'] == 'node_runtime_missing'
    assert payload['source_sha256'] == await project_source_fingerprint(executor.root, executor.calls[0])
    assert payload['runtime'] == {}
    assert caught.value.execution_diagnostic is None and caught.value.project_failure is None
    assert all(row['status'] == 'not_run' for row in payload['checks'])
    with pytest.raises(RuntimeCapabilityError, match='verification required before delivery'):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer', name='workspace.bundle',
                              arguments={}, idempotency_key='blocked_bundle')


@pytest.mark.parametrize('tool', ['project.verify', 'docx'])
async def test_harness_feedback_keeps_prerequisite_separate_from_process_failure(
    tmp_path: Path, tool: str,
) -> None:
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(gateway(tmp_path, BlockedExecutor()))
    result = await HarnessToolGateway(ErrorRuntimeCapabilityGateway(caught.value),
        policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED)).invoke(
            TENANT, HarnessToolCallRequest(run_id=RUN, actor='engineer', tool_name=tool,
                arguments={}, approval_required=False, sandbox='workspace_write', idempotency_key='blocked'),
            user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == 'failed'
    assert ('verification_blocked' in result.payload) is (tool == 'project.verify')
    assert 'verification_failure' not in result.payload and 'execution_diagnostic' not in result.payload
