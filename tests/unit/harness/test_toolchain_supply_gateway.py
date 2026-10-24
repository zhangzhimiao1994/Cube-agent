"""Trusted SDK admission never executes a backend or accepts model authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, JsonValue
from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainSupplyPlan
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414


class NoRuntime:
    def is_available(self, tenant_id: UUID, name: str) -> bool:
        pytest.fail('SDK admission must not discover a runtime tool')

    async def execute(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        pytest.fail('SDK admission must not execute a runtime tool')


class Policy:
    def __init__(self, status: CapabilityStatus, *, reason: str | None = None,
                 approval_id: str | None = None, raises: bool = False) -> None:
        self.status, self.reason, self.approval_id = status, reason, approval_id
        self.raises = raises
        self.seen: list[tuple[CapabilityRequest, Role]] = []
        self.authorities: list[ToolchainSupplyAuthority] = []

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        pytest.fail('SDK authority must use the private policy entry')

    async def authorize_project_toolchain(
        self, request: CapabilityRequest, *, role: Role, authority: ToolchainSupplyAuthority,
    ) -> CapabilityResult:
        self.authorities.append(authority)
        self.seen.append((request, role))
        if self.raises:
            raise ValueError('PRIVATE-CATALOG-AND-PATH')
        return CapabilityResult(self.status, request.run_id, approval_id=self.approval_id,
                                reason=self.reason)


def envelope(*, approval_required: bool = False) -> HarnessToolCallRequest:
    return HarnessToolCallRequest(run_id=UUID(int=3), actor='main_agent',
        tool_name='project.prepare_toolchain', arguments={}, approval_required=approval_required,
        sandbox='workspace_write', idempotency_key='sdk-admission', call_id='sdk-call')


@pytest.fixture
def authority(case: Any) -> ToolchainSupplyAuthority:
    return ToolchainSupplyAuthority(UUID(int=1), UUID(int=3), UUID(int=2), Role.OPERATOR,
        'worker-A', uuid4(), 'repair Go original', TaskMode.DISPATCH, case.row.routing_decision)


@pytest.mark.parametrize('approval_required', [False, True])
@pytest.mark.parametrize('status,reason,approval_id', [
    (CapabilityStatus.ALLOWED, None, None),
    (CapabilityStatus.WAITING_APPROVAL, None, 'approval_sdk'),
    (CapabilityStatus.WAITING_APPROVAL, None, None),
    (CapabilityStatus.DENIED, None, 'must-not-leak'),
    (CapabilityStatus.DENIED, 'capability denied by reviewer', None),
])
async def test_trusted_entry_binds_exact_request_and_obeys_policy(
    case: Any, authority: ToolchainSupplyAuthority, approval_required: bool, status: CapabilityStatus,
    reason: str | None, approval_id: str | None,
) -> None:
    policy = Policy(status, reason=reason, approval_id=approval_id)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=policy)
    request = envelope(approval_required=approval_required)
    result = await gateway.authorize_project_toolchain(UUID(int=1), request,
        user_id=UUID(int=2), role=Role.OPERATOR, plan=case.plan, original_reference=case.ref, authority=authority)

    assert result.call_id == request.call_id and result.tool_name == request.tool_name
    assert len(policy.seen) == 1
    assert policy.authorities == [authority]
    admission, role = policy.seen[0]
    assert role is Role.OPERATOR
    assert (admission.tenant_id, admission.run_id, admission.user_id, admission.agent_id,
            admission.capability, admission.operation, admission.resource, admission.idempotency_key) == (
        UUID(int=1), UUID(int=3), UUID(int=2), 'main_agent', 'command', 'run',
        'workspace/current/project-inputs/toolchain/' + case.plan.sha256, 'sdk-admission')
    assert admission.arguments == {
        'schema_version': 1, 'plan_sha256': case.plan.sha256,
        'original_reference': case.ref.to_payload(), 'project_id': 'demo',
        'workspace_session_id': 'session', 'actor_id': 'main_agent', 'actor_role': 'operator',
        'source_sha256': case.ref.input_source_sha256, 'contract_sha256': case.ref.contract_sha256,
        'catalog_sha256': case.plan.catalog_sha256, 'declaration_sha256': case.plan.declaration_sha256,
        'artifact_sha256': case.plan.artifact.artifact_sha256, 'archive_sha256': case.plan.artifact.sha256,
    }
    if status is CapabilityStatus.ALLOWED:
        assert result.status == 'succeeded' and result.payload == {'authorized': True}
    else:
        assert result.status == 'failed'
        assert result.failure_reason == (reason or (
            'capability requires approval' if status is CapabilityStatus.WAITING_APPROVAL
            else 'capability denied'))
        assert result.payload == ({'approval_id': approval_id}
            if status is CapabilityStatus.WAITING_APPROVAL and approval_id is not None else {})


@pytest.mark.parametrize('patch', [
    {'actor': 'implementer'}, {'tool_name': 'project.prepare_inputs'},
    {'tool_name': 'project.verify'}, {'arguments': {'approved': True}},
    {'arguments': {'plan_sha256': 'a' * 64}}, {'sandbox': 'restricted'},
    {'sandbox': 'mcp_remote'}, {'sandbox': 'remote_connector'},
])
async def test_model_envelopes_cannot_reach_trusted_policy(
    case: Any, authority: ToolchainSupplyAuthority, patch: dict[str, Any],
) -> None:
    policy = Policy(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=policy)
    result = await gateway.authorize_project_toolchain(UUID(int=1), replace(envelope(), **patch),
        user_id=UUID(int=2), role=Role.OPERATOR, plan=case.plan, original_reference=case.ref, authority=authority)
    assert result.status == 'failed' and result.failure_reason == 'tool permission mapping unavailable'
    assert not result.payload and not policy.seen


@pytest.mark.parametrize('defect', [
    'tenant', 'run', 'user', 'role', 'role_string', 'no_user', 'no_policy', 'plan', 'reference',
])
async def test_identity_and_plan_mismatch_fail_before_policy(
    case: Any, authority: ToolchainSupplyAuthority, defect: str,
) -> None:
    policy = Policy(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=None if defect == 'no_policy' else policy)
    request = replace(envelope(), run_id=uuid4()) if defect == 'run' else envelope()
    tenant = uuid4() if defect == 'tenant' else UUID(int=1)
    user: Any = None if defect == 'no_user' else uuid4() if defect == 'user' else UUID(int=2)
    role: Any = 'operator' if defect == 'role_string' else Role.ADMIN if defect == 'role' else Role.OPERATOR
    result = await gateway.authorize_project_toolchain(tenant, request, user_id=user, role=role,
        plan=cast(ToolchainSupplyPlan, object()) if defect == 'plan' else case.plan,
        original_reference=cast(ProjectBaselineReference, object()) if defect == 'reference' else case.ref, authority=authority)
    assert result.status == 'failed' and not result.payload and not policy.seen


async def test_policy_exception_is_private_and_never_falls_back(
    case: Any, authority: ToolchainSupplyAuthority,
) -> None:
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=Policy(CapabilityStatus.ALLOWED, raises=True))
    result = await gateway.authorize_project_toolchain(UUID(int=1), envelope(),
        user_id=UUID(int=2), role=Role.OPERATOR, plan=case.plan, original_reference=case.ref, authority=authority)
    assert result.status == 'failed' and result.failure_reason == 'capability authorization unavailable'
    assert result.payload == {} and 'PRIVATE' not in str(result)


async def test_non_envelope_is_rejected(case: Any) -> None:
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=Policy(CapabilityStatus.ALLOWED))
    with pytest.raises(TypeError, match='request must be HarnessToolCallRequest'):
        await gateway.authorize_project_toolchain(UUID(int=1), None,  # type: ignore[arg-type]
            user_id=UUID(int=2), role=Role.OPERATOR, plan=case.plan, original_reference=case.ref)


@pytest.mark.parametrize('sandbox', ['workspace_write', 'restricted', 'mcp_remote', 'remote_connector'])
async def test_trusted_entry_is_not_a_public_tool(sandbox: str) -> None:
    policy = Policy(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=policy)
    request = replace(envelope(), sandbox=sandbox)
    for method in (gateway.invoke, gateway.authorize):
        result = await method(UUID(int=1), request, user_id=UUID(int=2), role=Role.OPERATOR)
        assert result.status == 'failed' and result.failure_reason == 'tool permission mapping unavailable'
    assert not policy.seen
