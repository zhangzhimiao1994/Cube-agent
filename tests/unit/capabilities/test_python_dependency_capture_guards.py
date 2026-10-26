"""Capture guards using real fixture wheels and simulated in-process Root transport.

The default-inspector tests keep the actual profile/dependency clients and DTO
validation, replacing only broker exchange functions and the native-platform gate.
They do not authenticate a Root peer, open a socket, install packages, execute
project code, or prove native broker/Root integration or delivery readiness.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities import generated_project_service as generated
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectDraftRejected,
    GeneratedProjectError,
    GeneratedProjectService,
    generated_authority,
    validate_generated_preparation,
    validate_generated_transition,
)
from agent_hub.skills.sandbox import python_dependency_protocol as dependency
from agent_hub.skills.sandbox import python_project_profile as profile
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_generated_project_service import USER
from tests.unit.capabilities.test_generated_python_dependency_closure import (
    NormalizedRepository,
)
from tests.unit.capabilities.test_generated_python_dependency_closure import (
    draft as draft,  # noqa: PLC0414 - Explicitly register the shared pytest draft fixture.
)
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path

STATE = 'generated_project_preparation'
POLICY = 'python_dependency_preflight_version'
PROOF = 'python_dependency_preflight'


@pytest.fixture
def cas_calls(draft, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _, repo, _ = draft
    assert type(repo) is NormalizedRepository
    calls: list[dict[str, Any]] = []
    actual = repo.compare_and_set_generated_preparation

    async def counted(tenant, run, **kwargs):
        # Count entry, including calls rejected by the fixture repository's asserts.
        calls.append(kwargs)
        return await actual(tenant, run, **kwargs)

    monkeypatch.setattr(repo, 'compare_and_set_generated_preparation', counted)
    return calls


def _correct_lock(draft, *, selected: bool = True) -> None:
    service, repo, corrected = draft
    (service.root / project_path() / 'requirements.lock').write_text(corrected, encoding='utf-8')
    if selected:
        repo.row.routing_decision[POLICY] = 1


def _assert_uncaptured(draft, calls) -> None:
    service, repo, _ = draft
    assert calls == [], 'A rejected observation must not even attempt a capture CAS'
    assert repo.updates == 0
    assert STATE not in repo.row.routing_decision
    assert not (service.root / '.project-contracts' / str(TENANT) / str(RUN)).exists()


def _restricted_invocation(draft, seconds: int = 120) -> ProjectInvocation:
    _, repo, _ = draft
    invocation = generated_authority(repo.row, TENANT, RUN).invocation
    return ProjectInvocation.model_validate({**invocation.model_dump(),
        'sandbox_profile': 'restricted', 'install_allowed': False,
        'timeout_seconds': seconds}, strict=True)


async def test_missing_dependency_rejects_before_any_capture_attempt(draft, cas_calls) -> None:
    service, repo, _ = draft
    repo.row.routing_decision[POLICY] = 1
    with pytest.raises(GeneratedProjectDraftRejected,
                       match='^generated_acceptance_python_dependency_incomplete$') as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    payload = caught.value.rejection.to_payload()
    assert payload['schema_version'] == 2
    assert payload['phase'] == 'before_capture' and payload['execution_started'] is False
    observed = dependency.PythonDependencyClosureObservation.model_validate(
        payload['dependency_closure'], strict=True)
    assert observed.closure['status'] == 'incomplete'
    assert any(issue['code'] == 'missing_dependency' and issue['dependency'] == 'demo-helper'
               for issue in observed.closure['issues'])
    assert not {'python_recipe', 'python_input_supply', 'approval', 'execution_receipt'} & payload.keys()
    _assert_uncaptured(draft, cas_calls)


async def test_last_controls_check_drift_does_not_reserve_capture(draft, monkeypatch) -> None:
    service, repo, _ = draft
    _correct_lock(draft)
    original = service._controls_absent
    calls = 0

    def changed(invocation):
        nonlocal calls
        original(invocation)
        calls += 1
        if calls == 2:
            path = service.root / project_path() / 'requirements.lock'
            path.write_bytes(path.read_bytes() + b'\n')

    monkeypatch.setattr(service, '_controls_absent', changed)
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0
    assert STATE not in repo.row.routing_decision
    assert not (service.root / '.project-contracts' / str(TENANT) / str(RUN)).exists()


async def test_corrected_lock_captures_the_entire_complete_observation(draft, cas_calls) -> None:
    service, repo, _ = draft
    repo.row.routing_decision[POLICY] = 1
    with pytest.raises(GeneratedProjectDraftRejected):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    _assert_uncaptured(draft, cas_calls)
    _correct_lock(draft)
    actual = service._dependency_inspector
    observations = []

    async def recorded(invocation, source):
        observed = await actual(invocation, source)
        observations.append(observed.model_dump(mode='json'))
        return observed

    service._dependency_inspector = recorded
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured'
    assert len(observations) == 1 and len(cas_calls) == repo.updates == 2
    expected = observations[0]
    assert expected['closure'] == {'status': 'complete', 'issues': []}
    assert set(expected) == {'source_sha256', 'manifest_sha256', 'lock_sha256',
        'catalog_sha256', 'metadata_index_sha256', 'target_environment_sha256',
        'target_environment', 'closure'}
    assert expected['source_sha256'] == context.stored.reference.input_source_sha256
    for name, field in (('pyproject.toml', 'manifest_sha256'), ('requirements.lock', 'lock_sha256')):
        assert expected[field] == hashlib.sha256(
            (service.root / project_path() / name).read_bytes()).hexdigest()
    assert [call['replacement']['phase'] for call in cas_calls] == ['capturing', 'captured']
    assert all(call['replacement'][PROOF] == expected for call in cas_calls)
    reparsed = dependency.PythonDependencyClosureObservation.model_validate_json(
        json.dumps(dict(context.state[PROOF]), default=dict), strict=True)
    assert reparsed.model_dump(mode='json') == expected
    assert not {'python_recipe', 'python_input_supply', 'approval', 'execution_receipt'} & context.state.keys()
    with pytest.raises(TypeError):
        context.state[PROOF]['catalog_sha256'] = 'f' * 64
    current = await service.prepare(TENANT, RUN, 'tester', user_id=USER)
    assert current is not None and current.state == context.state
    assert len(cas_calls) == repo.updates == 2 and len(observations) == 1


@pytest.mark.parametrize('mutation', [
    'strip', 'manifest_sha256', 'lock_sha256', 'catalog_sha256',
    'metadata_index_sha256', 'target_environment', 'add',
])
async def test_capture_proof_cannot_be_stripped_changed_or_added(draft, cas_calls, mutation) -> None:
    service, repo, _ = draft
    _correct_lock(draft, selected=mutation != 'add')
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and len(cas_calls) == repo.updates == 2
    replacement = dict(context.state)
    if mutation == 'strip':
        replacement.pop(PROOF)
    elif mutation == 'add':
        assert PROOF not in context.state
        observed = await service._dependency_inspector(_restricted_invocation(draft),
            context.stored.reference.input_source_sha256)
        assert observed.closure['status'] == 'complete'
        replacement[PROOF] = observed.model_dump(mode='json')
    else:
        proof = dict(context.state[PROOF])
        if mutation == 'target_environment':
            environment = {**proof['target_environment'], 'platform_release': 'changed-release'}
            proof['target_environment'] = environment
            proof['target_environment_sha256'] = hashlib.sha256(json.dumps(environment,
                sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        else:
            proof[mutation] = 'f' * 64
        replacement[PROOF] = proof
    # Each replacement is a valid state on its own; immutability is a transition guard.
    validate_generated_preparation(replacement)
    with pytest.raises(GeneratedProjectError):
        validate_generated_transition(context.state, replacement)
    with pytest.raises(GeneratedProjectError):
        await service.update(context, replacement)
    assert len(cas_calls) == repo.updates == 2
    current = await service.load(TENANT, RUN, 'engineer')
    assert current is not None and current.state == context.state


@pytest.mark.parametrize('complete', [False, True], ids=['missing-lock', 'corrected-lock'])
@pytest.mark.parametrize('relative', [
    'src/billing/__init__.py', 'ACCEPTANCE.json', 'pyproject.toml', 'requirements.lock',
])
async def test_files_changed_during_inspection_fail_before_cas(
    draft, cas_calls, complete, relative,
) -> None:
    service, repo, _ = draft
    repo.row.routing_decision[POLICY] = 1
    if complete:
        _correct_lock(draft)
    actual = service._dependency_inspector
    inspected = []

    async def changed(invocation, source):
        observed = await actual(invocation, source)
        inspected.append(observed)
        path = service.root / project_path() / relative
        # Keep all declarations valid: even whitespace changes invalidate the observation.
        path.write_bytes(path.read_bytes() + b'\n')
        return observed

    service._dependency_inspector = changed
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert not isinstance(caught.value, GeneratedProjectDraftRejected)
    assert len(inspected) == 1
    assert inspected[0].closure['status'] == ('complete' if complete else 'incomplete')
    _assert_uncaptured(draft, cas_calls)


@pytest.mark.parametrize('complete', [False, True], ids=['missing-lock', 'corrected-lock'])
@pytest.mark.parametrize('change', [
    'lease_expired', 'lease_token', 'worker_id', 'request', 'actor', 'role',
    'permissions_removed', 'permissions_added', 'sandbox_profile', 'timeout',
    'selected_removed', 'selected_false', 'selected_bool', 'generated_selected_removed',
])
async def test_authority_changed_during_inspection_fail_before_cas(
    draft, cas_calls, complete, change,
) -> None:
    service, repo, _ = draft
    repo.row.routing_decision[POLICY] = 1
    if complete:
        _correct_lock(draft)
    actual = service._dependency_inspector
    inspected = []

    async def changed(invocation, source):
        observed = await actual(invocation, source)
        inspected.append(observed)
        row, route = repo.row, repo.row.routing_decision
        if change == 'lease_expired':
            row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == 'lease_token':
            row.worker_lease_token = uuid4()
        elif change == 'worker_id':
            row.worker_id = 'worker-reassigned'
        elif change == 'request':
            row.request += ' Changed while awaiting Root.'
        elif change == 'actor':
            row.actor_id = uuid4()
        elif change == 'role':
            row.actor_role = Role.ADMIN
        elif change == 'permissions_removed':
            route['requested_permissions'] = ['workspace.read', 'workspace.write']
        elif change == 'permissions_added':
            route['requested_permissions'] = [*route['requested_permissions'], 'network.http']
        elif change == 'sandbox_profile':
            route['sandbox_profile'] = 'restricted'
        elif change == 'timeout':
            route['runtime_timeout_seconds'] = 1
        elif change == 'selected_removed':
            route.pop(POLICY)
        elif change == 'selected_false':
            route[POLICY] = 0
        elif change == 'selected_bool':
            route[POLICY] = True
        elif change == 'generated_selected_removed':
            route.pop(generated.VERSION)
        else:
            raise AssertionError(change)
        return observed

    service._dependency_inspector = changed
    with pytest.raises(GeneratedProjectError) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert not isinstance(caught.value, GeneratedProjectDraftRejected)
    assert len(inspected) == 1
    _assert_uncaptured(draft, cas_calls)


@pytest.mark.parametrize('field,value', [
    ('source_sha256', False), ('manifest_sha256', 'F' * 64), ('lock_sha256', 'not-a-digest'),
    ('catalog_sha256', '../catalog'), ('metadata_index_sha256', 'e' * 63),
    ('target_environment_sha256', 'f' * 64), ('target_environment', {'python_version': '3.12'}),
    ('closure', {'status': 'installed', 'issues': []}),
    ('closure', {'status': 'complete', 'issues': [{'code': 'missing_dependency',
        'source': 'demo-dep', 'dependency': 'demo-helper'}]}),
])
async def test_model_copy_forged_root_observation_is_reparsed_before_cas(
    draft, cas_calls, field, value,
) -> None:
    service, _, _ = draft
    _correct_lock(draft)
    actual = service._dependency_inspector
    returned = []

    async def forged(invocation, source):
        observed = await actual(invocation, source)
        copied = observed.model_copy(update={field: value})
        assert type(copied) is dependency.PythonDependencyClosureObservation
        assert getattr(copied, field) == value
        returned.append(copied)
        return copied

    service._dependency_inspector = forged
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert len(returned) == 1
    _assert_uncaptured(draft, cas_calls)


@pytest.mark.parametrize('field', ['source_sha256', 'manifest_sha256', 'lock_sha256'])
async def test_well_formed_but_foreign_root_binding_fails_before_cas(draft, cas_calls, field) -> None:
    service, _, _ = draft
    _correct_lock(draft)
    actual = service._dependency_inspector
    inspected = []

    async def foreign(invocation, source):
        observed = await actual(invocation, source)
        copied = observed.model_copy(update={field: 'f' * 64})
        dependency.PythonDependencyClosureObservation.model_validate_json(
            copied.model_dump_json(), strict=True)
        inspected.append(copied)
        return copied

    service._dependency_inspector = foreign
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert len(inspected) == 1
    _assert_uncaptured(draft, cas_calls)


class FakeRootTransport:
    """Simulate typed exchanges only; clients still enforce scope and reparsing."""

    def __init__(self, draft, monkeypatch: pytest.MonkeyPatch, *, change: str = '') -> None:
        self.inspector = draft[0]._dependency_inspector
        self.calls: list[Any] = []
        self.change = change
        monkeypatch.setattr(profile, '_systemd_supported', lambda: True)
        monkeypatch.setattr(dependency, '_systemd_supported', lambda: True)
        monkeypatch.setattr(profile, 'request_python_project_profile_broker', self.profile)
        monkeypatch.setattr(dependency, 'request_python_dependency_closure_broker', self.dependencies)

    async def profile(self, request, **kwargs):
        self.calls.append(request)
        assert type(request) is profile.PythonProjectProfileRequest
        self._read_only(request.invocation)
        scope = profile.PythonProjectProfileScope.for_invocation(request.invocation)
        if self.change == 'profile_scope':
            scope = scope.model_copy(update={'execution_id': 'foreign_profile'})
        if self.change == 'profile_unavailable':
            return profile.PythonProjectProfileResponse(scope=scope, ok=False,
                error='input_supply_configuration_missing')
        configured = profile.PythonProjectProfile.model_validate_json(json.dumps({
            'interpreter': {'implementation': 'cpython', 'version': '3.12.14', 'abi': 'cp312',
                'platform': 'linux_x86_64', 'binary_sha256': '1' * 64,
                'base_runtime_sha256': '2' * 64},
            'catalog': [{'name': 'demo-dep', 'version': '1.0',
                'filename': 'demo_dep-1.0-py3-none-any.whl', 'sha256': '3' * 64, 'size_bytes': 1}],
            'catalog_sha256': 'd' * 64,
        }), strict=True)
        return profile.PythonProjectProfileResponse(scope=scope, ok=True, profile=configured)

    async def dependencies(self, request, **kwargs):
        self.calls.append(request)
        assert type(request) is dependency.PythonDependencyClosureRequest
        self._read_only(request.invocation)
        assert request.expected_catalog_sha256 == 'd' * 64
        observed = await self.inspector(request.invocation, request.expected_source_sha256)
        if self.change in {'source_sha256', 'catalog_sha256'}:
            observed = observed.model_copy(update={self.change: 'f' * 64})
        return dependency.PythonDependencyClosureResponse(
            scope=profile.PythonProjectProfileScope.for_invocation(request.invocation),
            ok=True, observation=observed)

    @staticmethod
    def _read_only(invocation) -> None:
        assert type(invocation) is ProjectInvocation
        assert invocation.sandbox_profile == 'restricted' and invocation.install_allowed is False
        assert 1 <= invocation.timeout_seconds <= 3600
        assert not {'approval', 'python_recipe', 'execution_receipt', 'environment_input_sha256'} & (
            invocation.model_dump().keys())


async def test_default_inspector_uses_real_profile_and_dependency_clients(
    draft, cas_calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_service, repo, _ = draft
    _correct_lock(draft)
    transport = FakeRootTransport(draft, monkeypatch)
    service = GeneratedProjectService(root=fixture_service.root, repository=repo)
    assert service._dependency_inspector is generated._inspect_dependencies
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured'
    assert len(transport.calls) == 2 and len(cas_calls) == repo.updates == 2
    discovery, closure = transport.calls
    assert discovery.action == 'inspect_python_project_profile'
    assert closure.action == 'inspect_python_dependency_closure'
    assert discovery.invocation.execution_id != closure.invocation.execution_id
    assert discovery.invocation.execution_id.startswith('dependency_profile_')
    assert closure.invocation.execution_id.startswith('dependency_preflight_')
    for key in ('tenant_id', 'run_id', 'project_id', 'session_id'):
        assert getattr(discovery.invocation, key) == getattr(closure.invocation, key)
    assert closure.expected_source_sha256 == context.stored.reference.input_source_sha256
    assert closure.expected_catalog_sha256 == context.state[PROOF]['catalog_sha256'] == 'd' * 64
    assert context.state[PROOF]['closure']['status'] == 'complete'
    assert 'python_recipe' not in context.state and 'python_input_supply' not in context.state


@pytest.mark.parametrize('change', [
    'profile_scope', 'profile_unavailable', 'source_sha256', 'catalog_sha256',
])
async def test_default_clients_reject_unavailable_or_foreign_root_before_cas(
    draft, cas_calls, monkeypatch: pytest.MonkeyPatch, change,
) -> None:
    fixture_service, repo, _ = draft
    _correct_lock(draft)
    transport = FakeRootTransport(draft, monkeypatch, change=change)
    service = GeneratedProjectService(root=fixture_service.root, repository=repo)
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert len(transport.calls) == (1 if change.startswith('profile_') else 2)
    _assert_uncaptured(draft, cas_calls)


@pytest.mark.parametrize('seconds,elapsed,remaining', [
    (1, 0.25, 1), (3, 1.25, 2), (120, 2.25, 118), (3600, 2.25, 3598),
])
async def test_default_inspector_passes_remaining_timeout_without_renewing_deadline(
    draft, monkeypatch: pytest.MonkeyPatch, seconds, elapsed, remaining,
) -> None:
    _correct_lock(draft)
    transport = FakeRootTransport(draft, monkeypatch)
    invocation = _restricted_invocation(draft, seconds)
    source = await project_source_fingerprint(draft[0].root, invocation)
    ticks = iter((100.0, 100.0 + elapsed))
    windows = []

    def timeout(delay):
        window = asyncio.timeout(delay)
        windows.append((delay, window, window.when()))
        return window

    # Override the service's clock only, preserving real asyncio client deadlines.
    monkeypatch.setattr(generated, 'asyncio', SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: next(ticks)), timeout=timeout))
    service = GeneratedProjectService(root=draft[0].root, repository=draft[1])
    result = await service._dependency_inspector(invocation, source)
    assert result.source_sha256 == source and result.closure['status'] == 'complete'
    discovery, closure = transport.calls
    assert discovery.invocation.timeout_seconds == min(10, seconds)
    assert closure.invocation.timeout_seconds == remaining
    assert len(windows) == 1 and windows[0][0] == seconds
    assert windows[0][1].when() == windows[0][2] and not windows[0][1].expired()
    assert invocation.timeout_seconds == seconds


@pytest.mark.parametrize('seconds,elapsed', [(1, 1.0), (3, 4.0)])
async def test_default_inspector_exhausted_budget_never_starts_dependency_exchange(
    draft, monkeypatch: pytest.MonkeyPatch, seconds, elapsed,
) -> None:
    _correct_lock(draft)
    transport = FakeRootTransport(draft, monkeypatch)
    invocation = _restricted_invocation(draft, seconds)
    source = await project_source_fingerprint(draft[0].root, invocation)
    ticks = iter((100.0, 100.0 + elapsed))
    monkeypatch.setattr(generated, 'asyncio', SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: next(ticks)), timeout=asyncio.timeout))
    with pytest.raises(TimeoutError):
        await generated._inspect_dependencies(invocation, source)
    assert len(transport.calls) == 1
    assert type(transport.calls[0]) is profile.PythonProjectProfileRequest


async def test_legacy_capture_never_accesses_profile_or_dependency_root(
    draft, cas_calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_service, repo, _ = draft
    assert POLICY not in repo.row.routing_decision
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail('Legacy capture unexpectedly accessed Root dependency discovery')

    monkeypatch.setattr(profile, 'request_python_project_profile_broker', forbidden)
    monkeypatch.setattr(dependency, 'request_python_dependency_closure_broker', forbidden)
    monkeypatch.setattr(profile, '_systemd_supported', lambda: True)
    monkeypatch.setattr(dependency, '_systemd_supported', lambda: True)
    service = GeneratedProjectService(root=fixture_service.root, repository=repo)
    assert service._dependency_inspector is generated._inspect_dependencies
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured'
    assert PROOF not in context.state and calls == []
    assert len(cas_calls) == repo.updates == 2
