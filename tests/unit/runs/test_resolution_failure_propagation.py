"""Offline native-boundary fakes; real broker/client/original-project failure flow."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Never, cast

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs import original_python_resolution
from agent_hub.runs.project_inputs import OriginalProjectPreparer
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.self_repair import SelfRepairDecision
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext, _freeze_object
from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic
from agent_hub.skills.sandbox import python_metadata_preparation_broker as broker_api
from agent_hub.skills.sandbox import python_resolution_protocol as protocol
from agent_hub.skills.sandbox import python_resolution_worker
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed
from agent_hub.skills.sandbox.python_registry_read import RegistryReader
from tests.unit.capabilities.test_protected_original_resolution_authorization import (
    original as running_record,
)
from tests.unit.capabilities.test_standard_python_private_recipe import ENVIRONMENT
from tests.unit.skills.sandbox import test_resolution_broker_diagnostics as native
from tests.unit.skills.sandbox.test_protected_original_resolution import _original
from tests.unit.skills.sandbox.test_resolution_lease_witness import request as resolution_request


def diagnostic(**changes: object) -> protocol.ResolutionFailureDiagnostic:
    return protocol.ResolutionFailureDiagnostic.model_validate({
        'phase': 'registry_metadata', 'code': 'registry_unavailable',
        'exit_code': 2, 'timed_out': False, **changes}, strict=True)


@pytest.mark.parametrize('changes', [
    {'exit_code': True}, {'timed_out': 0}, {'phase': 'PRIVATE'},
    {'code': 'PRIVATE'}, {'secret': 'PRIVATE'}, {'exit_code': 2**64},
])
def test_closed_diagnostic(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        diagnostic(**changes)


def test_unknown_only_and_exception_privacy() -> None:
    value = diagnostic()
    assert protocol.ResolutionStatus(state='unknown', key='a'*64, diagnostic=value).diagnostic == value
    assert protocol.ResolutionStatus(state='unknown', key='a'*64).diagnostic is None
    with pytest.raises(ValueError):
        protocol.ResolutionStatus(state='resolved', key='a'*64, diagnostic=value)
    result = protocol.resolution_failure_diagnostic(ValueError('token=PRIVATE'), phase='resolution')
    assert result.code == 'internal_failure' and 'PRIVATE' not in result.model_dump_json()
    with pytest.raises(ValueError):
        protocol.ResolutionFailureDiagnostic.model_validate(value.model_copy(update={'secret': 'PRIVATE'}))


@pytest.mark.parametrize('boundary', ['registry', 'wheel'])
async def test_real_resolver_preserves_closed_leaf_diagnostic(boundary: str) -> None:
    from agent_hub.capabilities.python_dependency_resolution import resolve_dependencies
    from tests.unit.skills.test_python_registry_read import (
        Wire,
        budget,
        environment,
        reader,
        scope,
    )

    wire = Wire()
    wire.release('demo')
    actual_reader = reader(wire)
    leaf = diagnostic(code='registry_gzip_invalid')
    failure = protocol.ResolutionFailureError(leaf)

    class FailingRegistry:
        async def candidates(self, *args: object, **kwargs: object) -> Never:
            raise failure

    async def fail_wheel(*args: object) -> Never:
        raise failure

    allowance = budget()
    deadline = allowance.deadline
    with pytest.raises(protocol.ResolutionFailureError) as caught:
        await resolve_dependencies(
            (DependencyNeed('demo', 'a'*64),), scope=scope(),
            environment=environment(),
            reader=cast(RegistryReader, FailingRegistry()) if boundary == 'registry' else actual_reader,
            wheel_reader=fail_wheel, budget=allowance,
        )
    assert caught.value is failure
    assert protocol.resolution_failure_diagnostic(
        caught.value, phase='resolution', exit_code=2,
    ) == leaf
    assert allowance.deadline == deadline
    assert len(wire.requests) == (0 if boundary == 'registry' else 2)


async def test_actual_broker_client_original_project_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline, _invocation_request = _original(tmp_path)
    captured = {'schema_version': 1, 'phase': 'captured', 'reference': baseline.reference.to_payload()}
    record = running_record()
    route = dict(record.routing_decision or {})
    route.update(project_id='project', workspace_session_id='session', project_input_preparation=captured,
                 project_input_contract_version=1,
                 project_input={'schema_version': 1, 'kind': 'managed_workspace',
                                'editable_paths': ['src/expense_cli/cli.py']},
                 runtime_timeout_source='project_scale_soft_budget', runtime_timeout_seconds=30,
                 runtime_timeout_absolute_seconds=180)
    record = replace(record, created_at=datetime.now(UTC), routing_decision=route)

    async def current(*args: object) -> RunRecord: return record
    async def approved(*args: object, **kwargs: object) -> bool: return True
    repository = SimpleNamespace(get=current, is_capability_approval_approved=approved)

    class Profiles:
        async def inspect(self, invocation: object, *, expected_source_sha256: str | None=None) -> SimpleNamespace:
            return SimpleNamespace(ok=True, profile=SimpleNamespace(root_config_sha256='4'*64,
                catalog_sha256='5'*64, target_environment=ENVIRONMENT if expected_source_sha256 else None))

    class Verifier:
        async def prepare_python_original(self, **kwargs: object) -> Never:
            raise ProjectVerificationError('python_original_catalog_incomplete')

    class Preparer(OriginalProjectPreparer):
        async def _state(self, context: TaskContext, **kwargs: object) -> Mapping[str, JsonValue] | None:
            return None if kwargs.get('supply') else cast(Mapping[str, JsonValue], captured)

    monkeypatch.setattr(ProjectBaselineStore, 'load', lambda *args: baseline)
    monkeypatch.setattr(original_python_resolution, 'PythonProjectProfileSandbox', Profiles)
    active = resolution_request()
    monkeypatch.setattr(native, 'request', lambda: active)
    (tmp_path / 'broker').mkdir()
    case = native.portable_broker(tmp_path / 'broker', monkeypatch, 'execution', 2, False)
    actions: list[str] = []

    async def child(*args: object, **kwargs: object) -> SkillResult:
        state: Literal['absent', 'unknown'] = 'absent' if active.action == 'inspect_python_resolution' else 'unknown'
        response = protocol.ResolutionStatus(state=state, key=active.key,
            diagnostic=diagnostic() if state == 'unknown' else None)
        return SkillResult(exit_code=2 if state == 'unknown' else 0, timed_out=False,
                           stdout=response.model_dump_json(), stderr='token=PRIVATE')

    async def transport(call: protocol.ResolutionRequest) -> protocol.ResolutionStatus:
        nonlocal active
        active = call
        actions.append(call.action)
        return await broker_api.handle_resolution_preparation(case.broker, call,
            peer_uid=case.broker.policy.allowed_uid)

    monkeypatch.setattr(broker_api, 'run_subprocess_with_limits', child)
    monkeypatch.setattr(python_resolution_worker, 'request_resolution', transport)
    context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, request=record.request,
        mode=TaskMode.DISCUSS, actor_id=record.actor_id, actor_role=record.actor_role,
        execution_token=record.worker_lease_token, routing_decision=_freeze_object(route, name='test routing'), timeout_seconds=25.)
    preparer = Preparer(root=tmp_path, repository=cast(RunRepository, repository), authorizer=cast(HarnessToolGateway, SimpleNamespace()),
        verifier=cast(ProjectVerificationService, Verifier()), artifact_repository=InMemoryArtifactRepository())
    with (bind_execution_authority(context, worker_id=cast(str, record.worker_id)),
          pytest.raises(ProjectVerificationError) as caught):
        await preparer(context)
    error = caught.value
    assert error.resolution_failure == diagnostic()
    payload = safe_runtime_failure_diagnostic(error)
    assert payload['error_code'] == 'project_verification.resolution.registry_unavailable'
    assert payload['retryable'] is False
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=record.id,
                    reason=str(error), payload=payload)
    from agent_hub.runs.self_repair import _failure_diagnostic_metadata
    assert _failure_diagnostic_metadata(event)[0] == payload['error_code']
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
    decision = classify_terminal_run(status=RunStatus.FAILED, mode=TaskMode.DISCUSS,
        routing_decision=route, events=(event,), policy=SelfRepairPolicy(enabled=True))
    decision = cast(SelfRepairDecision, decision)
    assert decision.skip_reason == 'project_verification_no_safe_recovery'
    assert decision.error_code == payload['error_code']
    assert decision.automatic_execution is False and decision.recovery_strategy is None
    assert 'PRIVATE' not in repr(event)
    assert actions == ['inspect_python_resolution', 'resolve_python_inputs']
    before = len(actions)
    again = await broker_api.handle_resolution_preparation(case.broker, active,
        peer_uid=case.broker.policy.allowed_uid)
    assert again.state == 'unknown' and cast(protocol.ResolutionFailureDiagnostic, again.diagnostic).code == 'metadata_prepare_consumed'
    assert len(actions) == before


@pytest.mark.parametrize('bad', ['wrong_key', 'success', 'private', 'extra', 'bool', 'oversize'])
async def test_broker_rejects_untrusted_failure_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    case = native.portable_broker(tmp_path, monkeypatch, 'execution', 2, False)
    request = resolution_request()
    payload = {'state': 'unknown', 'key': request.key, 'diagnostic': diagnostic().model_dump()}
    if bad == 'wrong_key': payload['key'] = 'a'*64
    elif bad == 'success': payload['state'] = 'published'
    elif bad == 'extra': cast(dict[str, object], payload['diagnostic'])['secret'] = 'PRIVATE'
    elif bad == 'bool': cast(dict[str, object], payload['diagnostic'])['exit_code'] = True
    import json
    stdout = 'PRIVATE' if bad == 'private' else json.dumps(payload)
    if bad == 'oversize': stdout += ' ' * 4096
    async def child(*args: object, **kwargs: object) -> SkillResult:
        return SkillResult(exit_code=2, timed_out=False, stdout=stdout, stderr='PRIVATE')
    monkeypatch.setattr(broker_api, 'run_subprocess_with_limits', child)
    result = await broker_api.handle_resolution_preparation(case.broker, request,
        peer_uid=case.broker.policy.allowed_uid)
    assert result.state == 'unknown' and cast(protocol.ResolutionFailureDiagnostic, result.diagnostic).code == 'metadata_prepare_unavailable'
    assert 'PRIVATE' not in result.model_dump_json()


@pytest.mark.parametrize('defect', ['extra', 'bool', 'code', 'retryable'])
def test_event_rejects_corrupted_resolution_diagnostic(defect: str) -> None:
    from agent_hub.runtime.failure_reason import resolution_failure_payload
    payload = resolution_failure_payload(diagnostic())
    if defect == 'extra': cast(dict[str, object], payload['resolution_failure'])['secret'] = 'PRIVATE'
    elif defect == 'bool': cast(dict[str, object], payload['resolution_failure'])['exit_code'] = True
    elif defect == 'code': cast(dict[str, object], payload['resolution_failure'])['code'] = 'PRIVATE'
    else: payload['retryable'] = True
    with pytest.raises(ValueError):
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=running_record().id,
                 reason='project verification failed: resolution inspect only', payload=payload)


@pytest.mark.parametrize('known', [True, False])
def test_registry_worker_main_only_emits_closed_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], known: bool) -> None:
    import io
    import time

    from agent_hub.skills.sandbox import python_registry_fetch as worker
    from agent_hub.skills.sandbox.python_registry_protocol import ResolutionError

    query = worker.MetadataReadRequest(name='setuptools', maximum=1024, deadline=time.monotonic()+20)
    flags = {name: getattr(sys.flags, name) for name in dir(sys.flags) if not name.startswith('_')}
    flags.update(isolated=True, optimize=0)
    monkeypatch.setattr(sys, 'flags', SimpleNamespace(**flags))
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os, 'geteuid', lambda: 10001, raising=False)
    monkeypatch.setattr(sys, 'argv', ['worker', '--metadata-worker'])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(query.model_dump_json().encode())))
    async def fail(request: object) -> Never:
        if known: raise ResolutionError('registry_unavailable')
        raise ValueError('token=PRIVATE /private/source')
    monkeypatch.setattr(worker, 'read_metadata_snapshot', fail)
    assert worker.main() == 2
    streams = capsys.readouterr()
    result = protocol.ResolutionFailureDiagnostic.model_validate_json(streams.out, strict=True)
    assert result.phase == 'registry_metadata' and result.exit_code == 2
    assert result.code == ('registry_unavailable' if known else 'internal_failure')
    assert streams.err == 'registry_worker_unavailable\n' and 'PRIVATE' not in streams.out


@pytest.mark.parametrize('child_output,timed_out,want', [
    ('valid', False, 'registry_unavailable'), ('invalid', False, 'registry_worker_unavailable'),
    ('valid', True, 'resolution_deadline'),
])
async def test_fetch_unit_retains_leaf_only_after_confirmed_cleanup(monkeypatch: pytest.MonkeyPatch, child_output: str, timed_out: bool, want: str) -> None:
    import time

    from agent_hub.skills.sandbox import python_input_supply_worker as worker
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from tests.unit.skills.sandbox.python_input_supply_worker_typed_fixture import (
        configuration,
        supply_configuration,
    )

    request = resolution_request()
    calls: list[Sequence[str]] = []
    async def run(command: Sequence[str], *args: object, **kwargs: object) -> SkillResult:
        calls.append(command)
        if command[0].endswith('systemd-run'):
            return SkillResult(exit_code=None if timed_out else 2, timed_out=timed_out,
                stdout=diagnostic().model_dump_json() if child_output == 'valid' else 'PRIVATE', stderr='PRIVATE')
        return SkillResult(exit_code=0, timed_out=False, stdout=(
            'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nControlGroup=\nTasksCurrent=0\n'), stderr='')
    monkeypatch.setattr(worker, 'run_subprocess_with_limits', run)
    with pytest.raises(protocol.ResolutionFailureError) as caught:
        await worker._run_fetch_unit(request.invocation, BrokerPolicy(), configuration(), supply_configuration(),
            time.monotonic()+20, payload_raw=b'{}', maximum=4096, registry=True)
    assert caught.value.diagnostic.code == want
    assert caught.value.diagnostic.timed_out is timed_out
    assert [row[1] for row in calls if not row[0].endswith('systemd-run')] == ['show', 'kill', 'show']
    assert 'PRIVATE' not in caught.value.diagnostic.model_dump_json()


def test_resolution_worker_main_retains_leaf_without_root_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_hub.skills.sandbox import broker, project_contract_broker
    from agent_hub.skills.sandbox import python_metadata_preparation_worker as native_worker
    worker = python_resolution_worker
    request = resolution_request()
    stage = tmp_path / 'stage'
    monkeypatch.setattr(native_worker, '_native', lambda: None)
    monkeypatch.setattr(native_worker, '_stage_identity', lambda *args: None)
    monkeypatch.setattr(worker, '_root', lambda *args, **kwargs: None)
    monkeypatch.setattr(project_contract_broker, '_read_control', lambda *args: request.model_dump_json().encode())
    monkeypatch.setattr(worker, 'read_root_configuration', lambda: SimpleNamespace(stage_root=tmp_path))
    monkeypatch.setattr(worker, 'read_supply_configuration', lambda: SimpleNamespace())
    monkeypatch.setattr(broker, 'load_broker_policy', lambda: broker.BrokerPolicy())
    import sys
    flags = {name: getattr(sys.flags, name) for name in dir(sys.flags) if not name.startswith('_')}
    flags.update(isolated=True, optimize=0)
    monkeypatch.setattr(sys, 'flags', SimpleNamespace(**flags))
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    monkeypatch.setattr(sys, 'argv', ['worker', '--stage', str(stage)])
    async def fail(*args: object, **kwargs: object) -> Never: raise protocol.ResolutionFailureError(diagnostic())
    monkeypatch.setattr(worker, 'execute_resolution', fail)
    assert worker.main() == 2
    streams = capsys.readouterr()
    result = protocol.ResolutionStatus.model_validate_json(streams.out, strict=True)
    assert result.state == 'unknown' and result.key == request.key and result.diagnostic == diagnostic()
    assert streams.err == 'resolution_unavailable\n'


async def test_real_registry_reader_and_resolver_preserve_finite_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from agent_hub.capabilities.python_dependency_resolution import resolve_dependencies
    from agent_hub.skills.sandbox import python_input_supply_worker as worker
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_registry_fetch import BrokerRegistryReader
    from agent_hub.skills.sandbox.python_registry_protocol import ResolutionBudget, ResolutionLimits
    from tests.unit.skills.sandbox.python_input_supply_worker_typed_fixture import (
        configuration,
        supply_configuration,
    )

    request = resolution_request()
    leaf = diagnostic(code='registry_gzip_invalid')
    calls: list[Sequence[str]] = []
    async def run(command: Sequence[str], *args: object, **kwargs: object) -> SkillResult:
        calls.append(command)
        if command[0].endswith('systemd-run'):
            return SkillResult(exit_code=2, timed_out=False, stdout=leaf.model_dump_json(), stderr='PRIVATE')
        return SkillResult(exit_code=0, timed_out=False, stdout=(
            'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nControlGroup=\nTasksCurrent=0\n'), stderr='')
    async def no_wheel(*args: object) -> Never: pytest.fail('failed metadata must not fetch wheels')
    monkeypatch.setattr(worker, 'run_subprocess_with_limits', run)
    reader = BrokerRegistryReader(request, BrokerPolicy(), configuration(), supply_configuration())
    with pytest.raises(protocol.ResolutionFailureError) as caught:
        await resolve_dependencies(request.needs, scope=request.claim.resolution_scope(),
            environment=request.environment, reader=reader, wheel_reader=no_wheel,
            budget=ResolutionBudget(ResolutionLimits(), time.monotonic()+20))
    assert caught.value.diagnostic == leaf
    assert protocol.resolution_failure_diagnostic(caught.value, phase='resolution') == leaf
    assert sum(row[0].endswith('systemd-run') for row in calls) == 1
    assert calls[-1][1] == 'show'


async def test_fail_run_real_sql_roundtrip_and_worker_ownership() -> None:
    from uuid import uuid4

    from sqlalchemy import select, update

    from agent_hub.db.models import RunEventRow, RunRow
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.repository import RunConflict
    from tests.unit.runs.test_crew_report_publication_red_oct29 import sql_repository

    record = running_record()
    context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, request=record.request,
        mode=cast(TaskMode, record.mode), actor_id=record.actor_id, actor_role=record.actor_role, timeout_seconds=25.)
    async with sql_repository(context) as (repository, factory, _engine):
        async with factory.begin() as session:
            await session.execute(update(RunRow).where(RunRow.id == record.id).values(
                worker_id=record.worker_id, worker_lease_token=record.worker_lease_token))
        with pytest.raises(RunConflict, match='worker execution ownership changed'):
            await repository.fail_run(record.id, reason='project verification failed: resolution inspect only',
                worker_id='wrong-worker', worker_lease_token=uuid4(), resolution_failure=diagnostic())
        assert await repository.raw_events(record.tenant_id, record.id) == ()
        failed = await repository.fail_run(record.id,
            reason='project verification failed: resolution inspect only', worker_id=record.worker_id,
            worker_lease_token=record.worker_lease_token, resolution_failure=diagnostic())
        assert failed.status is RunStatus.FAILED and failed.worker_id == record.worker_id
        assert failed.worker_lease_token == record.worker_lease_token
        events = await repository.raw_events(record.tenant_id, record.id)
        assert len(events) == 1
        event = RunEvent.from_payload(events[0].to_payload())
        assert event.payload['resolution_failure'] == diagnostic().model_dump()
        assert event.payload['error_code'] == 'project_verification.resolution.registry_unavailable'
        assert event.payload['retryable'] is False
        async with factory() as session:
            row = await session.scalar(select(RunEventRow).where(RunEventRow.run_id == record.id))
            assert row is not None
            assert RunEvent.from_payload(dict(row.payload)).payload == event.payload
        await repository.fail_run(record.id, reason='must not replace terminal event',
            worker_id=record.worker_id, worker_lease_token=record.worker_lease_token,
            resolution_failure=diagnostic(code='internal_failure'))
        assert await repository.raw_events(record.tenant_id, record.id) == events
