"""A valid UNKNOWN inspection retains finite diagnosis and never grants replay."""
import asyncio
from pathlib import Path
from typing import Unpack

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectError,
    GeneratedProjectService,
)
from agent_hub.capabilities.python_input_preparation import (
    DynamicPythonResolutionFlow,
    InspectOnlyError,
)
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic
from agent_hub.skills.sandbox import python_resolution_worker as wire
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlanningRequest
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionRequest,
    ResolutionStatus,
)
from tests.unit.capabilities.dynamic_resolution_typed_fixture import (
    FlowRepository,
    ResolveArgs,
    present,
)
from tests.unit.capabilities.test_dynamic_python_resolution_flow import generated_dynamic_case
from tests.unit.harness.test_tool_gateway import FakePolicyGateway
from tests.unit.runtime.test_direct_project_verification import Model


@pytest.fixture
def dynamic_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[GeneratedProjectService, FlowRepository, TaskContext, str]:
    return generated_dynamic_case(tmp_path, monkeypatch)


async def test_normal_unknown_inspection_retains_finite_diagnosis_without_replay(
    monkeypatch: pytest.MonkeyPatch,
    dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str],
) -> None:
    service, repo, context, _ = dynamic_case
    assert type(repo.row) is RunRecord and type(repo.row.actor_role) is Role
    claims: list[ResolutionClaim] = []
    calls: list[str] = []
    original_claim = PythonResolutionPreparation._claim

    async def claim(self: PythonResolutionPreparation,
                    planning: PythonMetadataPlanningRequest) -> ResolutionClaim:
        result = await original_claim(self, planning)
        claims.append(result)
        return result

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request.action)
        assert request.action == 'inspect_python_resolution'
        assert request.claim.worker_id == repo.row.worker_id
        assert request.claim.lease_token == context.execution_token
        return ResolutionStatus(state='unknown', key=request.key)

    monkeypatch.setattr(PythonResolutionPreparation, '_claim', claim)
    monkeypatch.setattr(wire, 'request_resolution', transport)
    for _ in range(2):
        with (
            bind_execution_authority(context, worker_id=present(repo.row.worker_id), actor='engineer'),
            pytest.raises(GeneratedProjectError) as caught,
        ):
            await service.prepare(context.tenant_id, context.run_id, 'engineer',
                                  user_id=present(context.actor_id))
        assert str(caught.value) == 'generated_acceptance_resolution_inspect_only'
    assert calls == ['inspect_python_resolution', 'inspect_python_resolution']
    assert len(claims) >= 6
    assert claims[-1].original_deadline <= claims[0].original_deadline
    assert repo.updates == 0
    assert 'generated_project_preparation' not in present(repo.row.routing_decision)
    assert 'python_metadata_preparation' not in present(repo.row.routing_decision)
    assert not (service.root / '.project-contracts').exists()
    journal = present(service._resolution_journal)
    assert not await journal.read_artifacts(context.tenant_id, context.run_id,
        artifact_type='python_resolution_checkpoint', producer='python_resolution')


@pytest.mark.parametrize('kind', ['plain', 'subclass', 'mutated', 'invalid_status'])
async def test_unowned_or_invalid_failures_stay_generic(
    monkeypatch: pytest.MonkeyPatch,
    dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str],
    kind: str,
) -> None:
    service, repo, context, _ = dynamic_case
    calls: list[str] = []

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request.action)
        assert request.action == 'inspect_python_resolution'
        return ResolutionStatus(state='unknown', key=request.key).model_copy(update={'state': 'in_progress'})

    if kind == 'invalid_status':
        monkeypatch.setattr(wire, 'request_resolution', transport)
    else:
        failure: ValueError
        if kind == 'plain':
            failure = ValueError('resolution_inspect_only')
        elif kind == 'subclass':
            class OtherInspectOnlyError(InspectOnlyError):
                pass
            failure = OtherInspectOnlyError()
        else:
            failure = InspectOnlyError()
            failure.args = ('private_invalid_error_not_exported',)

        async def fail(self: DynamicPythonResolutionFlow, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
            raise failure

        monkeypatch.setattr(DynamicPythonResolutionFlow, 'resolve_data', fail)
    with (
        bind_execution_authority(context, worker_id=present(repo.row.worker_id), actor='engineer'),
        pytest.raises(GeneratedProjectError) as caught,
    ):
        await service.prepare(context.tenant_id, context.run_id, 'engineer',
                              user_id=present(context.actor_id))
    assert str(caught.value) == 'generated_acceptance_unavailable_or_changed'
    assert calls == (['inspect_python_resolution'] if kind == 'invalid_status' else [])
    assert repo.updates == 0 and not (service.root / '.project-contracts').exists()


async def test_service_error_reaches_harness_and_runtime_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dynamic_case: tuple[GeneratedProjectService, FlowRepository, TaskContext, str],
) -> None:
    service, repo, context, _ = dynamic_case
    calls: list[str] = []

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request.action)
        assert request.action == 'inspect_python_resolution'
        return ResolutionStatus(state='unknown', key=request.key)

    monkeypatch.setattr(wire, 'request_resolution', transport)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=service.root, run_repository=repo)
    assert backend._project_verification is not None
    backend._project_verification.generated = service
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    harness = HarnessToolGateway(backend, policy_gateway=policy)
    model = Model()
    runtime = DirectRuntime(model, logical_model='fixture', harness_tool_gateway=harness)
    with (
        bind_execution_authority(context, worker_id=present(repo.row.worker_id), actor='main_agent'),
        pytest.raises(RuntimeExecutionError) as caught,
    ):
        await runtime._verify_workspace(context, known_files={}, attempt=1,
            deadline=asyncio.get_running_loop().time() + 30)
    assert getattr(caught.value, 'failure_code', None) == 'resolution_inspect_only'
    diagnostic = safe_runtime_failure_diagnostic(caught.value)
    assert diagnostic['error_code'] == 'project_verification.generated_acceptance_resolution_inspect_only'
    assert diagnostic['retryable'] is False
    action = diagnostic['suggested_action']
    assert isinstance(action, str)
    assert '依赖准备' in action and '避免重复操作' in action
    assert 'outcome_unconfirmed' not in str(diagnostic)
    assert calls == ['inspect_python_resolution']
    assert not model.requests and not policy.requests
    assert repo.updates == 0 and not (service.root / '.project-contracts').exists()
