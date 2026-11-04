"""Changed declarations retain draft feedback without replaying Root resolution."""
from dataclasses import replace
from pathlib import Path
from typing import Unpack

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectDraftRejected,
    GeneratedProjectError,
    GeneratedProjectService,
)
from agent_hub.capabilities.python_input_preparation import (
    DynamicPythonResolutionFlow,
    ResolutionDeclarationChangedError,
)
from agent_hub.skills.sandbox import python_resolution_worker as wire
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionRequest, ResolutionStatus
from tests.unit.capabilities.dynamic_resolution_typed_fixture import ResolveArgs, RootState, present
from tests.unit.capabilities.test_dynamic_python_resolution_flow import (
    descriptor_with_build_inputs,
    generated_dynamic_case,
)
from tests.unit.capabilities.test_python_resolution_preparation import (
    offline_network as offline_network,  # noqa: PLC0414
)
from tests.unit.capabilities.test_scoped_read import project_path


@pytest.mark.parametrize('cold', [False, True], ids=['warm', 'fresh-service'])
async def test_changed_requirement_gets_bound_draft_feedback_without_second_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cold: bool,
) -> None:
    service, repo, context, _ = generated_dynamic_case(tmp_path, monkeypatch)
    descriptor = await descriptor_with_build_inputs()
    state = RootState()
    calls: list[str] = []

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request.action)
        if request.action == 'inspect_python_resolution':
            return ResolutionStatus(state='resolved' if state.descriptor else 'absent', key=request.key,
                descriptor=state.descriptor.to_payload() if state.descriptor else None)
        assert request.action == 'resolve_python_inputs' and state.descriptor is None
        state.origin = request.claim
        state.descriptor = replace(descriptor, scope=request.claim.resolution_scope(), needs=request.needs)
        return ResolutionStatus(state='resolved', key=request.key, descriptor=state.descriptor.to_payload())

    async def approved(*args: object) -> bool:
        return True

    repo.is_capability_approval_scope_approved = approved
    monkeypatch.setattr(wire, 'request_resolution', transport)
    worker = present(repo.row.worker_id)
    with (bind_execution_authority(context, worker_id=worker, actor='engineer'),
          pytest.raises(GeneratedProjectDraftRejected) as original):
        await service.prepare(context.tenant_id, context.run_id, 'engineer', user_id=present(context.actor_id))
    assert original.value.rejection.reason == 'generated_acceptance_python_declaration_invalid'
    repaired_data = original.value.rejection.to_payload()
    assert repaired_data['schema_version'] == 3
    pins = repaired_data['dependency_pins']
    assert isinstance(pins, list) and len(pins) == 4
    assert {pin['name'] for pin in pins} == {'demo-dep', 'demo-helper', 'setuptools', 'wheel'}
    assert all(set(pin) == {'name', 'version', 'filename', 'sha256', 'size_bytes'} for pin in pins)
    assert calls.count('resolve_python_inputs') == 1 and state.descriptor is not None
    journal = present(service._resolution_journal)
    checkpoints = await journal.read_artifacts(context.tenant_id, context.run_id,
        artifact_type='python_resolution_checkpoint', producer='python_resolution')
    assert checkpoints
    manifest = service.root / project_path() / 'pyproject.toml'
    raw = manifest.read_bytes()
    assert b'demo-dep>=1' in raw
    manifest.write_bytes(raw.replace(b'demo-dep>=1', b'demo-dep>=2'))
    if cold:
        service = GeneratedProjectService(root=service.root, repository=repo,
            dependency_inspector=service._dependency_inspector, metadata_client=service._metadata_client)
        service.configure_resolution_artifacts(journal.artifacts)
    with (bind_execution_authority(context, worker_id=worker, actor='engineer'),
          pytest.raises(GeneratedProjectDraftRejected) as rejected):
        await service.prepare(context.tenant_id, context.run_id, 'engineer', user_id=present(context.actor_id))
    rejection = rejected.value.rejection
    assert rejection.reason == 'generated_acceptance_python_declaration_invalid'
    assert rejection.issues == ('python_declaration_invalid',)
    assert (rejection.tenant_id, rejection.run_id, rejection.user_id, rejection.proposer_actor) == (
        str(context.tenant_id), str(context.run_id), str(context.actor_id), 'engineer')
    assert rejection.observation_sha256 != original.value.rejection.observation_sha256
    assert calls.count('resolve_python_inputs') == 1
    assert 'freeze_python_resolution' not in calls and 'publish_python_resolution' not in calls
    assert await journal.read_artifacts(context.tenant_id, context.run_id,
        artifact_type='python_resolution_checkpoint', producer='python_resolution') == checkpoints
    assert repo.updates == 0
    assert 'generated_project_preparation' not in present(repo.row.routing_decision)
    assert not (service.root / '.project-contracts').exists()
    # Correcting the draft can re-enter the existing declaration gate. It must
    # not grant a second networking attempt or capture the still-unpinned lock.
    manifest.write_bytes(raw)
    with (bind_execution_authority(context, worker_id=worker, actor='engineer'),
          pytest.raises(GeneratedProjectDraftRejected) as corrected):
        await service.prepare(context.tenant_id, context.run_id, 'engineer', user_id=present(context.actor_id))
    assert corrected.value.rejection.reason == 'generated_acceptance_python_declaration_invalid'
    assert calls.count('resolve_python_inputs') == 1
    assert 'freeze_python_resolution' not in calls and 'publish_python_resolution' not in calls
    assert repo.updates == 0 and not (service.root / '.project-contracts').exists()


@pytest.mark.parametrize('kind', ['plain', 'subclass', 'mutated'])
async def test_unowned_declaration_failures_remain_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    service, repo, context, _ = generated_dynamic_case(tmp_path, monkeypatch)
    error: ValueError
    if kind == 'plain':
        error = ValueError('resolution_declaration_changed')
    elif kind == 'subclass':
        class OtherDeclarationError(ResolutionDeclarationChangedError):
            pass
        error = OtherDeclarationError()
    else:
        error = ResolutionDeclarationChangedError()
        error.args = ('untrusted_private_error',)

    async def fail(self: DynamicPythonResolutionFlow, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
        raise error

    calls: list[str] = []

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request.action)
        raise AssertionError('not dispatched')

    monkeypatch.setattr(DynamicPythonResolutionFlow, 'resolve_data', fail)
    monkeypatch.setattr(wire, 'request_resolution', transport)
    with (bind_execution_authority(context, worker_id=present(repo.row.worker_id), actor='engineer'),
          pytest.raises(GeneratedProjectError) as caught):
        await service.prepare(context.tenant_id, context.run_id, 'engineer', user_id=present(context.actor_id))
    assert type(caught.value) is GeneratedProjectError
    assert str(caught.value) == 'generated_acceptance_unavailable_or_changed'
    assert calls == [] and repo.updates == 0
    assert 'generated_project_preparation' not in present(repo.row.routing_decision)
    assert not (service.root / '.project-contracts').exists()
