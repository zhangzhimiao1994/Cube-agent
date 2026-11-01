"""Actual owner/gateway propagation; metadata transports remain simulated."""
from __future__ import annotations

from typing import Any

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectError,
    GeneratedProjectService,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.skills.sandbox import python_metadata_preparation_client as metadata_api
from agent_hub.skills.sandbox.python_metadata_preparation_client import (
    PythonMetadataPreparationSandbox,
)
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileSandbox
from tests.unit.capabilities.capability_lifecycle_typed_fixture import set_failure_fields
from tests.unit.capabilities.test_generated_metadata_preparation import metadata_case
from tests.unit.capabilities.test_generated_project_service import USER
from tests.unit.capabilities.test_generated_python_dependency_closure import (
    draft as draft,  # noqa: PLC0414
)
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.harness.test_tool_gateway import FakePolicyGateway


async def unavailable_profile(self: Any, invocation: Any) -> Any:
    assert invocation.install_allowed is False
    raise OSError('/private/never-echo-this')


async def test_sdk_profile_failure_retains_finite_stage_before_owner_capture(
    draft: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = metadata_case(draft)
    monkeypatch.setattr(PythonProjectProfileSandbox, 'inspect', unavailable_profile)
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=PythonMetadataPreparationSandbox())
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert str(caught.value) == 'python_metadata.profile.metadata_prepare_unavailable'
    assert not {'generated_project_preparation', 'python_metadata_preparation'} & c.repo.row.routing_decision.keys()
    assert c.events == ['closure']


async def test_finite_metadata_stage_survives_actual_authorization_gateway(
    draft: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = metadata_case(draft)
    monkeypatch.setattr(PythonProjectProfileSandbox, 'inspect', unavailable_profile)
    runtime = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=c.service.root, run_repository=c.repo)
    assert runtime._project_verification is not None
    runtime._project_verification.generated._metadata_client = PythonMetadataPreparationSandbox()
    runtime._project_verification.generated._dependency_inspector = c.closure
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    request = HarnessToolCallRequest(run_id=RUN, actor='engineer', tool_name='project.verify',
        arguments={}, sandbox='workspace_write', approval_required=True, idempotency_key='metadata-failure-stage')
    result = await HarnessToolGateway(runtime, policy_gateway=policy).invoke(
        TENANT, request, user_id=USER, role=Role.OPERATOR)
    assert result.status == 'failed'
    assert result.failure_reason == 'python_metadata.profile.metadata_prepare_unavailable'
    assert not result.payload and not policy.requests
    assert not {'generated_project_preparation', 'python_metadata_preparation'} & c.repo.row.routing_decision.keys()


@pytest.mark.parametrize('kind', [ValueError, OSError, TimeoutError])
async def test_plain_error_cannot_impersonate_typed_metadata_failure(
    draft: Any, kind: type[Exception],
) -> None:
    c = metadata_case(draft)
    async def spoof(*args: Any) -> Any:
        raise kind('python_metadata.plan.metadata_plan_timeout')
    c.client.plan = spoof
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=c.client)
    with pytest.raises(GeneratedProjectError, match='^generated_acceptance_unavailable_or_changed$'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert 'python_metadata_preparation' not in c.repo.row.routing_decision


async def test_mutating_failure_keeps_consumed_intent_and_next_call_only_inspects(draft: Any) -> None:
    api = metadata_api
    c = metadata_case(draft)
    async def approved(*args: Any) -> bool:
        return True
    async def prepare(*args: Any) -> Any:
        c.events.append('prepare')
        raise api.PythonMetadataPreparationFailure(phase='prepare', code='metadata_prepare_unavailable')
    async def inspect(*args: Any) -> Any:
        c.events.append('inspect')
        raise api.PythonMetadataPreparationFailure(phase='inspect', code='metadata_prepare_unavailable')
    c.repo.is_capability_approval_scope_approved = approved
    c.client.prepare, c.client.inspect = prepare, inspect
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=c.client)
    with pytest.raises(GeneratedProjectError, match='^python_metadata.prepare.metadata_prepare_unavailable$'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    state = c.repo.row.routing_decision['python_metadata_preparation'].copy()
    assert state['phase'] == 'executing' and c.events.count('prepare') == 1
    with pytest.raises(GeneratedProjectError, match='^python_metadata.inspect.metadata_prepare_unavailable$'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert c.repo.row.routing_decision['python_metadata_preparation'] == state
    assert c.events.count('prepare') == 1 and c.events.count('inspect') == 1
    assert 'generated_project_preparation' not in c.repo.row.routing_decision


@pytest.mark.parametrize('spoof', ['attributes', 'subclass', 'mutated'])
async def test_owner_revalidates_exact_typed_metadata_failure(draft: Any, spoof: str) -> None:
    api = metadata_api
    c = metadata_case(draft)
    failure: Exception
    if spoof == 'attributes':
        failure = ValueError('/private/never-echo')
        set_failure_fields(failure)
    elif spoof == 'subclass':
        class Other(metadata_api.PythonMetadataPreparationFailure):
            pass
        failure = RuntimeError.__new__(Other)
        RuntimeError.__init__(failure, '/private/never-echo')
        object.__setattr__(failure, 'phase', 'profile')
        object.__setattr__(failure, 'code', 'metadata_prepare_unavailable')
    else:
        failure = api.PythonMetadataPreparationFailure(phase='profile', code='metadata_prepare_unavailable')
        object.__setattr__(failure, 'code', '/private/never-echo')
    async def plan(*args: Any) -> Any:
        raise failure
    c.client.plan = plan
    service = GeneratedProjectService(root=c.service.root, repository=c.repo,
        dependency_inspector=c.closure, metadata_client=c.client)
    with pytest.raises(GeneratedProjectError, match='^generated_acceptance_unavailable_or_changed$'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert 'python_metadata_preparation' not in c.repo.row.routing_decision
