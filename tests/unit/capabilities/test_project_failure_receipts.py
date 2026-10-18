from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationFailure,
    _validate_report,
)
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_project_verification import Executor, gateway, verify
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.harness.test_tool_gateway import (
    USER_ID,
    ErrorRuntimeCapabilityGateway,
    FakePolicyGateway,
)


async def test_failed_execution_preserves_actual_source_bound_report(tmp_path: Path) -> None:
    executor = Executor(failed=True)
    backend = gateway(tmp_path, executor)
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(backend)
    failure = caught.value.project_failure
    assert failure is not None
    payload = failure.to_payload()
    assert payload['schema_version'] == 1 and payload['status'] == 'failed'
    assert payload['source_sha256'] == await project_source_fingerprint(executor.root, executor.calls[0])
    assert payload['runtime'] == {'node': 'v22.12.0', 'npm': '10.9.0'}
    checks = payload['checks']
    assert isinstance(checks, tuple)
    statuses = []
    for item in checks:
        assert isinstance(item, Mapping)
        statuses.append(item['status'])
        diagnostic = item['execution_diagnostic']
        assert isinstance(diagnostic, Mapping)
        assert 'stdout' not in diagnostic and 'stderr' not in diagnostic
        if item['status'] == 'failed':
            assert diagnostic['exit_code'] == 1
    assert statuses == ['passed', 'passed', 'failed']
    assert 'SECRET' not in json.dumps(payload, default=dict)
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], payload)['source_sha256'] = 'b' * 64
    assert ProjectVerificationFailure(payload).to_payload() == payload


def test_non_execution_failures_cannot_become_source_bound_failures() -> None:
    with pytest.raises(ProjectVerificationError) as caught:
        _validate_report(json.dumps({'schema_version':1,'source_sha256':'a'*64,
            'status':'blocked','runtime':{},'checks':[]}), expected_source='a'*64)
    assert caught.value.project_failure is None


def test_fake_project_failure_payload_cannot_cross_typed_capability_boundary() -> None:
    with pytest.raises(TypeError):
        RuntimeCapabilityError('project test failed', project_failure={'source_sha256':'a'*64})  # type: ignore[arg-type]


async def test_source_drift_after_failed_execution_is_not_repair_evidence(tmp_path: Path) -> None:
    from tests.unit.capabilities.test_scoped_read import project_path
    executor = Executor(failed=True)
    backend = gateway(tmp_path, executor)
    executor.mutate = tmp_path / 'projects' / project_path() / 'package.json'
    with pytest.raises(RuntimeCapabilityError, match='project source changed during verification') as caught:
        await verify(backend)
    assert caught.value.project_failure is None


@pytest.mark.parametrize('tool', ['project.verify', 'docx'])
async def test_gateway_publishes_source_report_only_for_project_verification(
    tmp_path: Path, tool: str,
) -> None:
    backend = gateway(tmp_path, Executor(failed=True))
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(backend)
    result = await HarnessToolGateway(ErrorRuntimeCapabilityGateway(caught.value),
        policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED)).invoke(
            TENANT, HarnessToolCallRequest(run_id=RUN, actor='engineer', tool_name=tool,
                arguments={}, approval_required=False, sandbox='workspace_write', idempotency_key='failure_report'),
            user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == 'failed'
    assert ('verification_failure' in result.payload) is (tool == 'project.verify')
    assert 'SECRET' not in repr(result)


async def test_gateway_ignores_untyped_report_attribute() -> None:
    from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
    from agent_hub.skills.sandbox.base import SkillResult
    error = RuntimeCapabilityError('project test failed', execution_diagnostic=ExecutionDiagnostic(
        backend='systemd', result=SkillResult(exit_code=1, timed_out=False, stdout='', stderr='failed')))
    error._project_failure = {'source_sha256':'a'*64}  # type: ignore[assignment]
    result = await HarnessToolGateway(ErrorRuntimeCapabilityGateway(error)).invoke(
        TENANT, HarnessToolCallRequest(run_id=RUN, actor='engineer', tool_name='project.verify',
            arguments={}, approval_required=False, sandbox='workspace_write', idempotency_key='untyped_report'))
    assert result.status == 'failed'
    assert 'verification_failure' not in result.payload


@pytest.mark.parametrize('statuses', [
    ('not_run', 'passed', 'failed'), ('failed', 'passed', 'not_run'),
    ('failed', 'failed', 'not_run'),
])
def test_inconsistent_phase_history_cannot_become_trusted_failure(statuses: tuple[str, ...]) -> None:
    checks = [{'phase':phase,'status':status,'exit_code':None if status == 'not_run' else 1 if status == 'failed' else 0,
               'timed_out':False,'stdout':'','stderr':''}
              for phase,status in zip(('install','build','test'),statuses,strict=True)]
    with pytest.raises(ProjectVerificationError, match='execution report is invalid') as caught:
        _validate_report(json.dumps({'schema_version':1,'status':'failed','source_sha256':'a'*64,
                                     'runtime':{},'checks':checks}), expected_source='a'*64)
    assert caught.value.project_failure is None and caught.value.diagnostic is None


@pytest.mark.parametrize('checks', [(), ({'phase':'test','status':'failed'},),
    ({'phase':'install','status':'not_run'}, {'phase':'build','status':'not_run'}, {'phase':'test','status':'not_run'})])
def test_serialized_failure_without_actual_ordered_checks_is_rejected(checks: tuple[object, ...]) -> None:
    with pytest.raises((ValueError, TypeError)):
        ProjectVerificationFailure(cast(Mapping[str, JsonValue], {
            'schema_version':1,'status':'failed','source_sha256':'a'*64,'runtime':{},'checks':checks}))


async def test_executor_exception_cannot_publish_a_claimed_source_report(tmp_path: Path) -> None:
    original = gateway(tmp_path / 'original', Executor(failed=True))
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(original)
    failure, diagnostic = caught.value.project_failure, caught.value.execution_diagnostic
    assert failure is not None and diagnostic is not None
    forged = ProjectVerificationFailure({**failure.to_payload(), 'source_sha256':'b'*64})

    class RaisingExecutor(Executor):
        async def run(self, invocation: ProjectInvocation) -> SkillResult:
            self.calls.append(invocation)
            raise ProjectVerificationError('project test failed', diagnostic, project_failure=forged)

    executor = RaisingExecutor()
    backend = gateway(tmp_path / 'target', executor)
    with pytest.raises(RuntimeCapabilityError, match='backend is unavailable') as raised:
        await verify(backend)
    assert raised.value.project_failure is None and raised.value.execution_diagnostic is None
    assert len(executor.calls) == 1


@pytest.mark.parametrize('field', ['runtime', 'check', 'diagnostic', 'metadata'])
async def test_serialized_failure_rejects_nested_raw_output(tmp_path: Path, field: str) -> None:
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(gateway(tmp_path, Executor(failed=True)))
    failure = caught.value.project_failure
    assert failure is not None
    raw = json.loads(json.dumps(failure.to_payload(), default=dict))
    target = {'runtime':raw['runtime'],'check':raw['checks'][0],
              'diagnostic':raw['checks'][0]['execution_diagnostic'],
              'metadata':raw['checks'][0]['execution_diagnostic']['stdout_metadata']}[field]
    target['stdout'] = 'PRIVATE_SOURCE'
    with pytest.raises(ValueError):
        ProjectVerificationFailure(raw)


@pytest.mark.parametrize('mode', ['crew', 'autogen'])
async def test_mode_failure_observation_and_replay_keep_verified_source_metadata(
    tmp_path: Path, mode: str,
) -> None:
    import importlib
    adapter = importlib.import_module(f'agent_hub.runtime.{mode}.adapter')
    with pytest.raises(RuntimeCapabilityError) as caught:
        await verify(gateway(tmp_path, Executor(failed=True)))
    failure, diagnostic = caught.value.project_failure, caught.value.execution_diagnostic
    assert failure is not None and diagnostic is not None
    payload = {'execution_diagnostic':diagnostic.to_payload(), 'verification_failure':failure.to_payload()}
    observed = adapter._execution_failure_result(payload)
    assert observed is not None and observed['verification_failure'] == failure.to_payload()
    assert adapter._execution_failure_result(observed) == observed
    altered: dict[str, object] = {**payload, 'verification_failure':{**failure.to_payload(), 'checks':[]}}
    assert adapter._execution_failure_result(altered) is None
    outer = {**diagnostic.to_payload(), 'exit_code':7}
    assert adapter._execution_failure_result({**payload,'execution_diagnostic':outer}) is None
