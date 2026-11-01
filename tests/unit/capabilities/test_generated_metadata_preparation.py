"""Actual generated owner capture boundary with simulated metadata and Root clients."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectService,
    PythonMetadataPreparationApprovalRequired,
)
from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureFailure
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlan
from tests.unit.capabilities.test_generated_project_service import USER
from tests.unit.capabilities.test_generated_python_dependency_closure import (
    draft as draft,  # noqa: PLC0414
)
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path
from tests.unit.skills.test_python_metadata_plan_protocol import plan_fields


def metadata_case(draft: Any) -> Any:
    service, repo, corrected = draft
    (service.root / project_path() / 'requirements.lock').write_text(corrected, encoding='utf-8')
    repo.row.routing_decision['python_dependency_preflight_submission_version'] = 1
    repo.row.routing_decision['python_dependency_preflight_version'] = 1
    inspect = service._dependency_inspector
    events = []
    completed = False

    class Client:
        async def plan(self, invocation: Any, scope: Any, observed: Any) -> Any:
            assert invocation.install_allowed is False
            events.append('plan')
            return PythonMetadataPlan.model_validate(plan_fields() | {'scope': scope.model_dump(),
                'draft': observed.model_dump()})

        async def prepare(self, invocation: Any, plan: Any) -> Any:
            from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
                PythonMetadataPreparationReceipt,
            )

            nonlocal completed
            assert repo.row.routing_decision['python_metadata_preparation']['phase'] == 'executing'
            assert 'generated_project_preparation' not in repo.row.routing_decision
            completed = True
            events.append('prepare')
            return PythonMetadataPreparationReceipt(plan=plan, preparation_execution_id=invocation.execution_id,
                index_sha256='f' * 64, index_record_count=plan.catalog_count)

        async def inspect(self, *args: Any) -> Any:
            raise AssertionError('new execution should not inspect')

    async def closure(invocation: Any, source: str) -> Any:
        events.append('closure')
        if not completed:
            raise PythonDependencyClosureFailure('python_dependency_metadata_unavailable')
        return await inspect(invocation, source)

    async def cas(tenant: Any, run: Any, **kwargs: Any) -> Any:
        from agent_hub.capabilities.python_metadata_preparation import (
            METADATA_KEY,
            validate_metadata_transition,
        )

        assert (tenant, run) == (TENANT, RUN)
        assert kwargs['expected'] == repo.row.routing_decision.get(METADATA_KEY)
        await kwargs['before_reservation']()
        after = validate_metadata_transition(kwargs['expected'], kwargs['replacement'])
        repo.row.routing_decision[METADATA_KEY] = json.loads(json.dumps(after, default=dict))
        return repo.row

    repo.compare_and_set_python_metadata_preparation = cas
    async def approved_scope(*args: Any) -> bool:
        events.append('grant')
        return False

    repo.is_capability_approval_scope_approved = approved_scope
    return SimpleNamespace(service=service, repo=repo, client=Client(), closure=closure, events=events,
        )


async def test_missing_metadata_requests_concrete_plan_before_capture(draft: Any) -> None:
    c = metadata_case(draft)
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=c.client)
    with pytest.raises(Exception) as error:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert type(error.value).__name__ == 'PythonMetadataPreparationApprovalRequired', repr(error.value.__context__)
    assert isinstance(error.value, PythonMetadataPreparationApprovalRequired)
    assert error.value.approval_request.resource.startswith('workspace/current/project-metadata/python/')
    assert c.repo.row.routing_decision['python_metadata_preparation']['phase'] == 'planned'
    assert 'generated_project_preparation' not in c.repo.row.routing_decision
    assert c.events == ['closure', 'plan', 'grant']


async def test_exact_metadata_grant_rechecks_closure_before_capture(draft: Any) -> None:
    c = metadata_case(draft)

    async def approved(*args: Any) -> bool:
        c.events.append('grant')
        return True

    c.repo.is_capability_approval_scope_approved = approved
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=c.client)
    result = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert result is not None and result.state['phase'] == 'captured'
    assert c.repo.row.routing_decision['python_metadata_preparation']['phase'] == 'prepared'
    assert c.events == ['closure', 'plan', 'grant', 'prepare', 'closure']
    metadata = c.repo.row.routing_decision['python_metadata_preparation']['plan']['draft']
    assert metadata['acceptance_sha256'] == hashlib.sha256(
        (c.service.root / project_path() / 'ACCEPTANCE.json').read_bytes()).hexdigest()


async def test_real_gateway_routes_concrete_metadata_approval_not_generic_tool(draft: Any, tmp_path: Any) -> None:
    from agent_hub.auth.models import Role
    from agent_hub.capabilities.gateway import CapabilityStatus
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest
    from tests.unit.harness.test_tool_gateway import FakePolicyGateway

    c = metadata_case(draft)
    runtime = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=c.service.root, run_repository=c.repo)
    assert runtime._project_verification is not None
    runtime._project_verification.generated._metadata_client = c.client
    runtime._project_verification.generated._dependency_inspector = c.closure
    policy = FakePolicyGateway(CapabilityStatus.WAITING_APPROVAL)
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)
    call = HarnessToolCallRequest(run_id=RUN, actor='engineer', tool_name='project.verify', arguments={},
        sandbox='workspace_write', approval_required=True, idempotency_key='metadata-authorize')
    result = await gateway.invoke(TENANT, call, user_id=USER, role=Role.OPERATOR)
    assert result.status == 'failed' and len(policy.requests) == 1
    selected, _ = policy.requests[0]
    assert selected.resource.startswith('workspace/current/project-metadata/python/')
    assert selected.idempotency_key == call.idempotency_key
    assert selected.arguments['purpose'] == 'python_dependency_metadata'
    assert 'generated_project_preparation' not in c.repo.row.routing_decision


async def test_known_metadata_receipt_does_not_block_uncaptured_lock_repair(draft: Any) -> None:
    from agent_hub.capabilities.generated_project_service import GeneratedProjectDraftRejected

    c = metadata_case(draft)
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=c.client)

    async def approved(*args: Any) -> bool:
        return True

    c.repo.is_capability_approval_scope_approved = approved
    # First build the metadata receipt but let the real closure reject the old incomplete lock.
    corrected = (service.root / project_path() / 'requirements.lock').read_text(encoding='utf-8')
    incomplete = '\n'.join(line for line in corrected.splitlines() if not line.startswith('demo-helper==')) + '\n'
    (service.root / project_path() / 'requirements.lock').write_text(incomplete, encoding='utf-8')
    with pytest.raises(GeneratedProjectDraftRejected):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert c.repo.row.routing_decision['python_metadata_preparation']['phase'] == 'prepared'
    assert 'generated_project_preparation' not in c.repo.row.routing_decision
    (service.root / project_path() / 'requirements.lock').write_text(corrected, encoding='utf-8')
    c.events.clear()
    result = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert result is not None and result.state['phase'] == 'captured'
    assert c.events == ['closure'], 'Known metadata must not redownload or reapprove after draft correction'
