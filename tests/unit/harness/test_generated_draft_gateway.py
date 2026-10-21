from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.generated_project_draft import GeneratedDraftRejection
from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.generated_draft_feedback import generated_draft_feedback
from tests.unit.harness.test_generated_project_admission import GeneratedBackend
from tests.unit.harness.test_tool_gateway import FakePolicyGateway
from tests.unit.runtime.test_generated_draft_feedback import case


@pytest.mark.parametrize('phase', ['authorization', 'execute'])
@pytest.mark.parametrize('defect', [None, 'tenant', 'actor', 'mutated', 'ordinary-error'])
async def test_gateway_returns_only_typed_scope_bound_preparation_rejections(
    phase: str, defect: str | None,
) -> None:
    context, request, example = case()
    data = dict(cast(Mapping[str, Any], example.payload['generated_draft_rejection']))
    data['issues'] = tuple(data['issues'])
    if defect == 'tenant':
        data['tenant_id'] = str(uuid4())
    elif defect == 'actor':
        data['proposer_actor'] = 'other_agent'
    observation = GeneratedDraftRejection(**data)
    if defect == 'mutated':
        object.__setattr__(observation, 'execution_started', True)

    def reject() -> None:
        if defect == 'ordinary-error':
            raise RuntimeError('generated_acceptance_manifest_invalid')
        raise GeneratedProjectDraftRejected(observation)

    class Backend(GeneratedBackend):
        async def project_verification_authorization(self, request: CapabilityRequest) -> None:
            if phase == 'authorization':
                reject()

        async def execute(self, **kw: Any) -> Mapping[str, JsonValue]:
            self.calls.append(('execute', {}, ''))
            reject()
            raise AssertionError('unreachable')

    backend, policy = Backend(), FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    result = await gateway.invoke(context.tenant_id, request, user_id=context.actor_id, role=Role.OPERATOR)
    assert result.status == 'failed' and not result.truncated
    assert len(policy.requests) == (0 if phase == 'authorization' else 1)
    feedback = generated_draft_feedback(result, context=context, request=request)
    assert (feedback is not None) is (defect is None)
    assert 'approval_id' not in result.payload and 'verification' not in result.payload


async def test_actual_service_runtime_authorization_preserves_draft_then_full_capture(tmp_path: Path) -> None:
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from agent_hub.harness.types import HarnessToolCallRequest
    from tests.unit.capabilities.test_generated_project_service import USER, manifest, setup
    from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file

    service, repo = setup(tmp_path)
    body = json.loads(manifest())
    body['editable_paths'] = ['src', 'package.json']
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', json.dumps(body))

    class NoExecution:
        async def run(self, invocation: Any) -> Any:
            pytest.fail('draft rejection must not execute commands')

    runtime = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=service.root, generated_artifact_dir=tmp_path / 'artifacts',
        run_repository=repo, project_executor=NoExecution())
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)
    call = HarnessToolCallRequest(run_id=RUN, actor='engineer', tool_name='project.verify', arguments={},
        sandbox='workspace_write', approval_required=True, idempotency_key='actual-draft')
    result = await gateway.invoke(TENANT, call, user_id=USER, role=Role.OPERATOR)
    assert result.status == 'failed' and result.failure_reason == 'generated project draft requires correction'
    assert cast(Mapping[str, JsonValue], result.payload['generated_draft_rejection'])['issues'] == (
        'protected_editable_paths',)
    assert not policy.requests and repo.updates == 0
    assert 'generated_project_preparation' not in repo.row.routing_decision
    write_file(service.root, f'{project_path()}/ACCEPTANCE.json', manifest().decode())
    selected = await runtime.project_verification_authorization(CapabilityRequest(tenant_id=TENANT,
        user_id=USER, run_id=RUN, agent_id='engineer', capability='command', operation='run',
        resource='workspace/current/project-verification', arguments={}, idempotency_key='corrected-draft'))
    assert selected is None and repo.updates == 2
    assert repo.row.routing_decision['generated_project_preparation']['phase'] == 'captured'
