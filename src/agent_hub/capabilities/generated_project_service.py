"""Once-only post-model acceptance capture, separate from user original inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_draft import (
    GeneratedDraftRejection,
    dependency_observation_payload,
    dependency_pins_payload,
)
from agent_hub.capabilities.project_acceptance import (
    GeneratedAcceptanceEnvelope,
    _nonfinite,
    _protected_control,
    _unique,
    parse_acceptance_manifest,
)
from agent_hub.capabilities.project_baselines import (
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
    _fingerprint,
)
from agent_hub.capabilities.project_contracts import _path
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.domain.runs import RunStatus
from agent_hub.plugins.handover_snapshots import _Directory
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_manifest
from agent_hub.skills.sandbox.python_metadata_preparation_client import (
    PythonMetadataPreparationFailure,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.python_input_preparation import (
        DynamicPythonResolutionFlow,
        DynamicResolutionJournal,
    )
    from agent_hub.capabilities.python_metadata_preparation_flow import MetadataPreparationClient
    from agent_hub.capabilities.types import CapabilityRequest
    from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepository
    from agent_hub.skills.sandbox.python_dependency_protocol import (
        PythonDependencyClosureObservation,
    )

VERSION = 'generated_project_contract_version'
STATE = 'generated_project_preparation'
_DEPENDENCY = 'python_dependency_preflight'
_DEPENDENCY_POLICY = 'python_dependency_preflight_version'
_MANIFEST = 'ACCEPTANCE.json'
_ENVELOPE = 'generated-acceptance.json'
_SHA = re.compile(r'[a-f0-9]{64}')
_BASE = {'schema_version', 'phase', 'user_id', 'request_sha256', 'proposer_actor', 'stack',
         'manifest_sha256', 'manifest_raw_sha256'}
_CAPTURED = {'reference', 'acceptance_sha256'}
_LOGGER = logging.getLogger(__name__)
_PREPARATION_ERROR_KINDS = frozenset({
    'ValueError', 'TypeError', 'RuntimeError', 'OSError', 'TimeoutError', 'AttributeError',
    'RecursionError', 'NameError', 'ImportError', 'FileNotFoundError', 'PermissionError',
    'ValidationError', 'InvalidSnapshot', 'ExecutionAuthorityError',
    'InvalidResolutionAuthorization', '_PhysicalAdmissionStopped', 'AdmissionConflict',
    'PythonProjectRecipeError',
})


class GeneratedProjectError(RuntimeError):
    """Finite generated preparation failure; never raw project or host text."""


class PythonMetadataPreparationApprovalRequired(GeneratedProjectError):
    """An exact metadata plan needs admission, not a project installation grant."""

    def __init__(self, request: CapabilityRequest) -> None:
        super().__init__('metadata_prepare_approval_required')
        self.approval_request = request


class GeneratedProjectDraftRejected(GeneratedProjectError):
    """A certain, uncaptured draft can be corrected without claiming execution."""

    def __init__(self, rejection: GeneratedDraftRejection) -> None:
        if type(rejection) is not GeneratedDraftRejection:
            raise ValueError('invalid generated draft rejection')
        rejection.to_payload()
        self.rejection = rejection
        super().__init__(rejection.reason)


def _need(value: object, code: str = 'generated_acceptance_scope_changed') -> None:
    if not value:
        raise GeneratedProjectError(code)


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _uuid(value: object) -> UUID:
    _need(isinstance(value, UUID))
    return UUID(str(value))


def _dependency_selected(route: Mapping[str, object]) -> bool:
    if _DEPENDENCY_POLICY not in route:
        return False
    _need(type(route[_DEPENDENCY_POLICY]) is int and route[_DEPENDENCY_POLICY] == 1)
    return True


async def _inspect_dependencies(
    invocation: ProjectInvocation, source: str,
) -> PythonDependencyClosureObservation:
    from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureSandbox
    from agent_hub.skills.sandbox.python_project_profile import (
        PythonProjectProfileRequest,
        PythonProjectProfileSandbox,
    )

    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(invocation.timeout_seconds):
        profile_invocation = ProjectInvocation.model_validate({**invocation.model_dump(),
            'execution_id': 'dependency_profile_' + uuid4().hex,
            'timeout_seconds': min(10, invocation.timeout_seconds)}, strict=True)
        profile = await PythonProjectProfileSandbox().inspect(profile_invocation, expected_source_sha256=source)
        profile.require_for(PythonProjectProfileRequest(invocation=profile_invocation, expected_source_sha256=source))
        _need(profile.ok and profile.profile is not None, 'python_dependency_metadata_unavailable')
        assert profile.profile is not None
        remaining = invocation.timeout_seconds - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            raise TimeoutError
        actual = ProjectInvocation.model_validate({**invocation.model_dump(),
            'timeout_seconds': max(1, math.ceil(remaining))}, strict=True)
        return await PythonDependencyClosureSandbox().inspect(actual,
            expected_source_sha256=source, expected_catalog_sha256=profile.profile.catalog_sha256)


def _manifest_issues(raw: bytes) -> tuple[str, ...]:
    """Classify only pure manifest failures; never return model text or paths."""
    try:
        item = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_nonfinite)
        if type(item) is not dict:
            return ('manifest_invalid',)
        issues = []
        for field, issue, limit in (('editable_paths', 'protected_editable_paths', 128),
                                     ('test_files', 'protected_test_files', 256)):
            paths = item.get(field)
            if (type(paths) is list and 0 < len(paths) <= limit
                    and any(_protected_control(_path(path)) for path in paths)):
                issues.append(issue)
        return tuple(issues) or ('manifest_invalid',)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return ('manifest_invalid',)


def _draft_route(record: object) -> None:
    route = getattr(record, 'routing_decision', None)
    _need(isinstance(route, Mapping) and STATE not in route)
    pending = [(route, 0)]
    remaining = 4096
    while pending:
        item, depth = pending.pop()
        remaining -= 1
        _need(remaining >= 0 and depth <= 32)
        if isinstance(item, str):
            _need(item.lower() not in {'unknown', 'uncertain', 'partial', 'outcome_uncertain',
                'tool_outcome_uncertain', 'model_outcome_uncertain'})
        elif isinstance(item, Mapping):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((value, depth + 1) for value in item)


def generated_selected(record: object) -> bool:
    route = getattr(record, 'routing_decision', None)
    _need(isinstance(route, Mapping))
    assert isinstance(route, Mapping)
    if VERSION not in route:
        _need(STATE not in route)
        return False
    _need(type(route[VERSION]) is int and route[VERSION] == 1
          and not {'project_input', 'project_input_contract_version', 'project_input_preparation'} & route.keys())
    return True


@dataclass(frozen=True)
class GeneratedAuthority:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    project_id: str
    session_id: str
    role: Role
    request_sha256: str
    lease_token: UUID | None
    worker_id: str | None
    timeout_seconds: int

    @property
    def invocation(self) -> ProjectInvocation:
        return ProjectInvocation(execution_id='generated_' + uuid4().hex, tenant_id=self.tenant_id,
            run_id=self.run_id, project_id=self.project_id, session_id=self.session_id,
            sandbox_profile='workspace_write', install_allowed=False, timeout_seconds=self.timeout_seconds)


def generated_authority(record: object, tenant_id: UUID, run_id: UUID, *, readonly: bool = False) -> GeneratedAuthority:
    tenant_id, run_id = _uuid(tenant_id), _uuid(run_id)
    _need(generated_selected(record))
    actual_tenant, actual_run = _uuid(getattr(record, 'tenant_id', None)), _uuid(getattr(record, 'id', None))
    user = _uuid(getattr(record, 'actor_id', None))
    _need((actual_tenant, actual_run) == (tenant_id, run_id))
    try:
        raw_role, raw_status = getattr(record, 'actor_role', None), getattr(record, 'status', None)
        _need(isinstance(raw_role, str) and isinstance(raw_status, str))
        role = Role(cast(str, raw_role))
        status = RunStatus(cast(str, raw_status))
    except (ValueError, TypeError):
        raise GeneratedProjectError('generated_acceptance_scope_changed') from None
    _need(role in {Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR})
    project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
    route = cast(Mapping[str, object], getattr(record, 'routing_decision', None))
    permissions = route.get('requested_permissions')
    _need(route.get('sandbox_profile') == 'workspace_write' and route.get('execution_backend', 'systemd') == 'systemd'
          and isinstance(permissions, (tuple, list))
          and {'workspace.read', 'workspace.write', 'command.run'} <= set(permissions))
    request = getattr(record, 'request', None)
    _need(type(request) is str)
    token = getattr(record, 'worker_lease_token', None)
    worker = getattr(record, 'worker_id', None)
    expiry = getattr(record, 'worker_lease_expires_at', None)
    if not readonly:
        _need(status is RunStatus.RUNNING and isinstance(token, UUID)
              and type(worker) is str and bool(worker) and isinstance(expiry, datetime)
              and expiry.tzinfo is not None and expiry > datetime.now(UTC))
    timeout = route.get('runtime_timeout_seconds', 300)
    _need(type(timeout) in (int, float) and math.isfinite(cast(float, timeout)) and cast(float, timeout) > 0)
    return GeneratedAuthority(tenant_id, user, run_id, project, session, role,
        hashlib.sha256(cast(str, request).encode('utf-8')).hexdigest(),
        UUID(str(token)) if isinstance(token, UUID) else None, worker if isinstance(worker, str) else None,
        min(3600, max(1, math.ceil(cast(float, timeout)))))


def validate_generated_preparation(value: object) -> Mapping[str, JsonValue]:
    try:
        _need(isinstance(value, Mapping), 'generated_acceptance_state_invalid')
        state = _freeze_object(cast(Mapping[str, JsonValue], value), name='generated preparation')
        phase = state.get('phase')
        _need(type(state.get('schema_version')) is int and state['schema_version'] == 1
              and phase in {'capturing', 'captured', 'python_ready'} and state.get('stack') in {'node', 'python'},
              'generated_acceptance_state_invalid')
        keys = _BASE | (_CAPTURED if phase != 'capturing' else set())
        if phase == 'python_ready':
            keys.add('python_recipe')
            marker = state['python_recipe']
            _need(state['stack'] == 'python' and isinstance(marker, Mapping)
                  and set(marker) == {'schema_version', 'recipe_sha256', 'environment_input_sha256'}
                  and type(marker.get('schema_version')) is int and marker['schema_version'] == 1)
            assert isinstance(marker, Mapping)
            for name in ('recipe_sha256', 'environment_input_sha256'):
                _need(type(marker[name]) is str and _SHA.fullmatch(cast(str, marker[name])) is not None)
        if 'python_input_supply' in state:
            from agent_hub.capabilities.python_input_preparation import supply_state_plan
            _need(phase != 'capturing' and state['stack'] == 'python')
            supply = state['python_input_supply']
            supply_state_plan(supply)
            _need(isinstance(supply, Mapping)
                  and (phase != 'python_ready' or supply['phase'] == 'supplied'))
            keys.add('python_input_supply')
        if _DEPENDENCY in state:
            _need(state['stack'] == 'python')
            proof = dependency_observation_payload(state[_DEPENDENCY], complete=True)
            if phase != 'capturing':
                reference = ProjectBaselineReference.from_payload(state['reference'])
                _need(proof['source_sha256'] == reference.input_source_sha256)
            keys.add(_DEPENDENCY)
        _need(set(state) == keys, 'generated_acceptance_state_invalid')
        user = state['user_id']
        _need(type(user) is str and str(UUID(user)) == user)
        actor = state['proposer_actor']
        _need(type(actor) is str and re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,127}', actor) is not None)
        for name in ('request_sha256', 'manifest_sha256', 'manifest_raw_sha256',
                     *(('acceptance_sha256',) if phase != 'capturing' else ())):
            _need(type(state[name]) is str and _SHA.fullmatch(cast(str, state[name])) is not None)
        if phase != 'capturing':
            ProjectBaselineReference.from_payload(state['reference'])
        return state
    except (ValueError, TypeError, KeyError, RecursionError):
        raise GeneratedProjectError('generated_acceptance_state_invalid') from None


def validate_generated_transition(previous: object, replacement: object) -> Mapping[str, JsonValue]:
    after = validate_generated_preparation(replacement)
    if previous is None:
        _need(after['phase'] == 'capturing')
        return after
    before = validate_generated_preparation(previous)
    common = _BASE - {'phase'}
    _need(all(before[key] == after[key] for key in common))
    allowed = {'capturing': {'captured'}, 'captured': {'captured', 'python_ready'}, 'python_ready': {'python_ready'}}
    _need(after['phase'] in allowed[cast(str, before['phase'])])
    _need((_DEPENDENCY in before) == (_DEPENDENCY in after)
          and before.get(_DEPENDENCY) == after.get(_DEPENDENCY))
    if before['phase'] != 'capturing':
        _need(all(before[key] == after[key] for key in _CAPTURED))
    if 'python_recipe' in before:
        _need(before['python_recipe'] == after.get('python_recipe'))
    old_supply = before.get('python_input_supply')
    new_supply = after.get('python_input_supply')
    if isinstance(old_supply, Mapping):
        _need(isinstance(new_supply, Mapping))
        assert isinstance(new_supply, Mapping)
        _need(new_supply.get('plan') == old_supply.get('plan')
              and new_supply.get('plan_sha256') == old_supply.get('plan_sha256'))
        if old_supply['phase'] == 'planned':
            _need(new_supply['phase'] in {'planned', 'executing'})
        elif old_supply['phase'] == 'executing':
            _need(new_supply['phase'] in {'executing', 'supplied'}
                  and new_supply.get('execution_id') == old_supply.get('execution_id'))
        elif old_supply['phase'] == 'supplied':
            _need(new_supply == old_supply)
    elif isinstance(new_supply, Mapping):
        _need(before['phase'] == after['phase'] == 'captured' and new_supply['phase'] == 'planned')
    return after


@dataclass(frozen=True)
class GeneratedProjectContext:
    record: object
    invocation: ProjectInvocation
    stored: StoredProjectBaseline
    envelope: GeneratedAcceptanceEnvelope
    state: Mapping[str, JsonValue]
    authority: GeneratedAuthority


def generated_verification_binding(
    context: GeneratedProjectContext, source_sha256: str, evidence: Mapping[str, object],
) -> Mapping[str, JsonValue]:
    _need(type(context) is GeneratedProjectContext and type(source_sha256) is str
          and _SHA.fullmatch(source_sha256) is not None)
    _need(set(evidence) == {'schema_version', 'manifest_sha256', 'count', 'names', 'test_evidence_sha256'})
    expected = context.envelope.manifest.test_ids
    _need(type(evidence['schema_version']) is int and evidence['schema_version'] == 1
          and type(evidence['count']) is int and evidence['count'] == len(expected)
          and isinstance(evidence['names'], (tuple, list)) and tuple(evidence['names']) == expected
          and evidence['manifest_sha256'] == context.state['manifest_raw_sha256'])
    if context.envelope.manifest.stack == 'python':
        from agent_hub.skills.sandbox.python_project_phase import (
            validate_generated_python_test_execution,
        )
        validate_generated_python_test_execution(evidence)
    else:
        _need(evidence['test_evidence_sha256'] == hashlib.sha256(_encoded({
            'schema_version': 1, 'passed_tests': expected})).hexdigest())
    return _freeze_object({
        'schema_version': 1, 'kind': 'generated_project_acceptance', 'capture_phase': 'post_model',
        'tenant_id': str(context.authority.tenant_id), 'user_id': str(context.authority.user_id),
        'run_id': str(context.authority.run_id), 'project_id': context.authority.project_id,
        'session_id': context.authority.session_id, 'request_sha256': context.authority.request_sha256,
        'acceptance_sha256': context.envelope.sha256, 'manifest_sha256': context.envelope.manifest.sha256,
        'manifest_raw_sha256': context.state['manifest_raw_sha256'], 'source_sha256': source_sha256,
        'stack': context.envelope.manifest.stack, 'test_execution': cast(JsonValue, evidence),
    }, name='generated verified binding')


class GeneratedProjectService:
    def __init__(self, *, root: Path, repository: object,
                 dependency_inspector: Callable[[ProjectInvocation, str],
                    Awaitable[PythonDependencyClosureObservation]] | None = None,
                 metadata_client: MetadataPreparationClient | None = None) -> None:
        self.root, self.repository = root, repository
        self._dependency_inspector = _inspect_dependencies if dependency_inspector is None else dependency_inspector
        self._metadata_client = metadata_client
        self._resolution_flows: dict[tuple[UUID, UUID, str], DynamicPythonResolutionFlow] = {}
        self._resolution_journal: DynamicResolutionJournal | None = None
        self._resolution_refs: dict[tuple[UUID, UUID, str], ArtifactReference] = {}

    async def _record(self, tenant: UUID, run: UUID) -> Any:
        getter = getattr(self.repository, 'get', None)
        _need(callable(getter))
        assert callable(getter)
        return await getter(tenant, run)

    async def _cas(self, authority: GeneratedAuthority, expected: Mapping[str, JsonValue] | None,
                   replacement: Mapping[str, JsonValue], *,
                   before_reservation: Callable[[], Awaitable[None]] | None = None) -> object:
        value = validate_generated_transition(expected, replacement)
        _need(authority.lease_token is not None)
        setter = getattr(self.repository, 'compare_and_set_generated_preparation', None)
        _need(callable(setter), 'generated_acceptance_persistence_unavailable')
        assert callable(setter)
        extra = {} if before_reservation is None else {'before_reservation': before_reservation}
        return await setter(authority.tenant_id, authority.run_id, actor_id=authority.user_id,
            actor_role=authority.role, lease_token=authority.lease_token,
            request_sha256=authority.request_sha256, project_id=authority.project_id,
            session_id=authority.session_id, expected=expected, replacement=json.loads(_encoded(value)), **extra)

    def _manifest(self, invocation: ProjectInvocation, *, stored: StoredProjectBaseline | None = None) -> bytes:
        if stored is not None:
            return _read_no_links(stored.snapshot, (_MANIFEST,), max_bytes=128 * 1024)
        parts = (str(invocation.tenant_id), 'projects', invocation.project_id, 'sessions', invocation.session_id, _MANIFEST)
        return _read_no_links(self.root, parts, max_bytes=128 * 1024)

    def _read_envelope(self, invocation: ProjectInvocation) -> bytes:
        with ExitStack() as stack:
            controls = ProjectBaselineStore(self.root)._controls(invocation, stack, create=False)
            return controls.read(_ENVELOPE, 32 * 1024**2)

    def _write_envelope(self, invocation: ProjectInvocation, envelope: GeneratedAcceptanceEnvelope) -> None:
        store = ProjectBaselineStore(self.root)
        with ExitStack() as stack:
            controls = store._controls(invocation, stack, create=False)
            store._write_once(controls, _ENVELOPE, _encoded(envelope.to_payload()))

    def _controls_absent(self, invocation: ProjectInvocation) -> None:
        # Require the entire run namespace absent, including empty/partial controls.
        # Descriptor-backed info is no-follow; errors other than ENOENT remain fatal.
        with ExitStack() as stack:
            parent = _Directory(self.root, private=False)
            stack.callback(parent.close)
            for name in ('.project-contracts', str(invocation.tenant_id), str(invocation.run_id)):
                try:
                    parent.info(name)
                except FileNotFoundError:
                    parent.check()
                    return
                child = _Directory(parent.path / name, private=True)
                stack.callback(child.close)
                parent.check()
                parent = child
            _need(False, 'generated_acceptance_unknown_no_recapture')

    async def _draft_observation(self, invocation: ProjectInvocation) -> tuple[bytes | None, dict[str, str]]:
        try:
            raw: bytes | None = await asyncio.to_thread(self._manifest, invocation)
        except FileNotFoundError:
            raw = None
        files = dict(await project_source_manifest(self.root, invocation))
        if raw is None:
            _need(_MANIFEST not in files)
        else:
            _need(files.get(_MANIFEST) in {hashlib.sha256(tag + raw).hexdigest() for tag in (b'f\0', b'x\0')})
        return raw, files

    async def _reject_draft(
        self, authority: GeneratedAuthority, actor: str, observation: tuple[bytes | None, dict[str, str]],
        reason: str, issues: tuple[str, ...], *, dependency: Mapping[str, JsonValue] | None = None,
    ) -> NoReturn:
        invocation = authority.invocation
        await asyncio.to_thread(self._controls_absent, invocation)
        _need(await self._draft_observation(invocation) == observation)
        latest = await self._record(authority.tenant_id, authority.run_id)
        _need(generated_authority(latest, authority.tenant_id, authority.run_id) == authority)
        _draft_route(latest)
        await asyncio.to_thread(self._controls_absent, invocation)
        raw, files = observation
        observation_sha = hashlib.sha256(_encoded({'schema_version': 1,
            'manifest': {'missing': True} if raw is None else {'sha256': hashlib.sha256(raw).hexdigest()},
            'source_files': files})).hexdigest()
        pins = None
        if reason == 'generated_acceptance_python_declaration_invalid':
            flow = self._resolution_flows.get((authority.tenant_id, authority.run_id, actor))
            if flow is not None and flow.descriptor is not None:
                pins = tuple(dependency_pins_payload(flow.public_data()['pins']))
        raise GeneratedProjectDraftRejected(GeneratedDraftRejection(
            tenant_id=str(authority.tenant_id), user_id=str(authority.user_id), run_id=str(authority.run_id),
            project_id=authority.project_id, session_id=authority.session_id,
            request_sha256=authority.request_sha256, proposer_actor=actor,
            observation_sha256=observation_sha, reason=reason, issues=issues,
            schema_version=3 if pins is not None else 2 if dependency is not None else 1,
            dependency_closure=dependency, dependency_pins=pins))

    def configure_resolution_artifacts(self, artifacts: ArtifactRepository) -> None:
        from agent_hub.capabilities.python_input_preparation import DynamicResolutionJournal

        self._resolution_journal = DynamicResolutionJournal(artifacts)

    async def recover_resolution_data(self, record: Any) -> Mapping[str, JsonValue] | None:
        """Warm and cold projections share the same current-scope registered-data gate.

        The stored actor selects data only. It never supplies a worker, grant or plan.
        """
        from datetime import timedelta

        from agent_hub.capabilities.python_metadata_preparation import metadata_scope
        from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileSandbox

        journal = self._resolution_journal
        route = getattr(record, 'routing_decision', {})
        if journal is None or any(type(route.get(name)) is not int or route[name] != 1 for name in (
                'generated_project_contract_version', 'python_dependency_preflight_version',
                'python_dependency_preflight_submission_version')):
            return None
        authority = generated_authority(record, record.tenant_id, record.id, readonly=True)
        checkpoints = await journal.read_artifacts(authority.tenant_id, authority.run_id,
            artifact_type='python_resolution_checkpoint', producer='python_resolution')
        if not checkpoints:
            return None
        stored = checkpoints[0].content['scope']
        _need(isinstance(stored, Mapping), 'resolution_checkpoint_invalid')
        assert isinstance(stored, Mapping)
        actor = stored.get('actor')
        _need(type(actor) is str, 'resolution_checkpoint_invalid')
        scope = metadata_scope(record, actor=cast(str, actor)).model_dump(mode='json')
        invocation = authority.invocation.model_copy(update={'sandbox_profile': 'restricted', 'install_allowed': False})
        profile = await PythonProjectProfileSandbox().inspect(invocation)
        _need(profile.ok and profile.profile is not None, 'resolution_data_unavailable')
        assert profile.profile is not None
        deadline = datetime.fromisoformat(cast(str, stored.get('original_deadline')))
        _need(deadline.tzinfo is not None and record.created_at < deadline
              <= record.created_at + timedelta(seconds=authority.timeout_seconds), 'resolution_checkpoint_scope_changed')
        scope.update(root_sha256=profile.profile.root_config_sha256, catalog_sha256=profile.profile.catalog_sha256,
                     original_deadline=deadline.isoformat())
        result = await journal.public_data(scope)
        latest = await self._record(authority.tenant_id, authority.run_id)
        _need(generated_authority(latest, authority.tenant_id, authority.run_id, readonly=True) == authority
              and metadata_scope(latest, actor=cast(str, actor)).model_dump(mode='json')
              == metadata_scope(record, actor=cast(str, actor)).model_dump(mode='json'), 'resolution_checkpoint_scope_changed')
        return result

    async def _resolve_dependency_draft(
        self, authority: GeneratedAuthority, actor: str,
        observation: tuple[bytes | None, dict[str, str]], controls: tuple[bytes, bytes],
        *, publish: bool = False, force: bool = False,
    ) -> bool:
        from datetime import timedelta

        from packaging.utils import canonicalize_name
        from packaging.version import Version

        from agent_hub.capabilities.approvals import capability_approval_scope
        from agent_hub.capabilities.execution_authority import _trusted_review_context
        from agent_hub.capabilities.python_input_preparation import (
            DynamicPythonResolutionFlow,
            ResolutionDeclarationChangedError,
            dynamic_python_needs,
            validate_dynamic_python_controls,
        )
        from agent_hub.capabilities.python_metadata_preparation import metadata_scope
        from agent_hub.capabilities.python_resolution_preparation import (
            PythonResolutionPreparation,
            ResolutionApprovalRequired,
        )
        from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
            PythonMetadataDraft,
            PythonMetadataPlanningRequest,
        )
        from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileSandbox
        from agent_hub.skills.sandbox.python_registry_protocol import requirement

        latest = await self._record(authority.tenant_id, authority.run_id)
        route = latest.routing_decision
        if (type(route.get('python_dependency_preflight_submission_version')) is not int
                or route['python_dependency_preflight_submission_version'] != 1):
            return False
        try:
            needs = dynamic_python_needs(controls[0])
        except ValueError:
            return False  # Original exact recipe/declaration gate still rejects unsupported sources.
        key = (authority.tenant_id, authority.run_id, actor)
        flow = self._resolution_flows.get(key)
        invocation = ProjectInvocation.model_validate({**authority.invocation.model_dump(),
            'sandbox_profile': 'restricted', 'install_allowed': False}, strict=True)

        async def guard() -> None:
            await asyncio.to_thread(self._controls_absent, invocation)
            _need(await self._draft_observation(invocation) == observation)
            current = await self._record(authority.tenant_id, authority.run_id)
            _need(generated_authority(current, authority.tenant_id, authority.run_id) == authority)
            _draft_route(current)

        await guard()
        profiles = PythonProjectProfileSandbox()
        try:
            base = await profiles.inspect(invocation)
        except (ValueError, OSError, TimeoutError):
            _need(flow is None and self._resolution_journal is None, 'resolution_data_unavailable')
            return False  # Compatibility: the existing Root preflight still must succeed.
        await guard()
        if not base.ok or base.profile is None:
            _need(flow is None, 'resolution_data_unavailable')
            return False
        catalog = base.profile.catalog
        root_config_sha256 = base.profile.root_config_sha256
        _need(root_config_sha256 is not None, 'resolution_data_unavailable')
        assert root_config_sha256 is not None
        source = _fingerprint(observation[1])
        scope = metadata_scope(latest, actor=actor, live=True)
        _trusted_review_context(tenant_id=authority.tenant_id, run_id=authority.run_id,
            user_id=authority.user_id, role=authority.role, current_record=latest)
        # The live context carries remaining time. Charge elapsed time once;
        # the resolution client separately enforces that remaining-time cap.
        original_deadline = latest.created_at + timedelta(seconds=authority.timeout_seconds)
        journal_scope = {**scope.model_dump(mode='json'), 'root_sha256': root_config_sha256,
            'catalog_sha256': base.profile.catalog_sha256}
        journal = self._resolution_journal
        if journal is not None:
            checkpoints = await journal.read_artifacts(authority.tenant_id, authority.run_id,
                artifact_type='python_resolution_checkpoint', producer='python_resolution')
            if checkpoints:
                stored = checkpoints[0].content.get('scope')
                _need(isinstance(stored, Mapping), 'resolution_checkpoint_scope_changed')
                assert isinstance(stored, Mapping)
                _need({name: value for name, value in stored.items() if name != 'original_deadline'}
                      == journal_scope, 'resolution_checkpoint_scope_changed')
                raw_deadline = stored.get('original_deadline')
                _need(isinstance(raw_deadline, str), 'resolution_checkpoint_scope_changed')
                assert isinstance(raw_deadline, str)
                original_deadline = datetime.fromisoformat(raw_deadline)
                _need(original_deadline.tzinfo is not None and original_deadline.isoformat() == raw_deadline
                      and latest.created_at < original_deadline
                      <= latest.created_at + timedelta(seconds=authority.timeout_seconds),
                      'resolution_checkpoint_scope_changed')
        # A checkpoint is data, not a grant. Keep its first exact deadline, then
        # validate the COMPLETE immutable history, including every scope field.
        journal_scope['original_deadline'] = original_deadline.isoformat()
        recovered = await journal.load(journal_scope) if journal is not None else None
        from agent_hub.capabilities.python_resolution_authorization import (
            bind_resolution_original_deadline,
        )

        bind_resolution_original_deadline(
            {name: value for name, value in journal_scope.items() if name != 'original_deadline'}, original_deadline)
        if flow is None and recovered is None and not force and all(any(
            wheel.name == canonicalize_name(requirement(n.requirement).name)
            and Version(wheel.version) in requirement(n.requirement).specifier
            for wheel in catalog) for n in needs):
            return False
        _need(journal is not None, 'resolution_artifact_unavailable')
        assert journal is not None
        async def current() -> Any:
            await guard()
            return await self._record(authority.tenant_id, authority.run_id)

        async def persist(key_sha256: str, pending_sha256: str | None) -> None:
            assert flow is not None
            await guard()
            _need(flow.needs is not None and flow.environment is not None, 'resolution_data_unavailable')
            assert flow.needs is not None and flow.environment is not None
            reference = await journal.save(journal_scope, needs=flow.needs, environment=flow.environment,
                resolution_key=key_sha256, pending_plan_sha256=pending_sha256)
            self._resolution_refs[key] = reference
            await guard()

        async def approved(request: CapabilityRequest) -> bool:
            await guard()
            # Durable data is registered before an action can wait or enter Root IO.
            await persist(cast(str, request.arguments['key']),
                cast(str | None, request.arguments.get('plan_sha256')))
            from agent_hub.capabilities.python_resolution_authorization import (
                resolution_policy_allowed,
            )

            current_policy = await resolution_policy_allowed(request)
            if current_policy is not None:
                return current_policy
            checker = getattr(self.repository, 'is_capability_approval_scope_approved', None)
            _need(callable(checker), 'resolution_authority_unavailable')
            assert callable(checker)
            return await checker(authority.tenant_id, authority.run_id, capability_approval_scope(request)) is True

        # Retain immutable data, but never retain a previous call's source/actor fences.
        client = PythonResolutionPreparation(current=current, approved=approved,
            original_deadline=original_deadline)
        if flow is None:
            flow = DynamicPythonResolutionFlow(client, checkpoint=persist)
            if recovered is not None:
                flow.restore(recovered)
            self._resolution_flows[key] = flow
        else:
            flow.client, flow.checkpoint = client, persist
        if flow.environment is not None:
            environment = dict(flow.environment)
        else:
            configured = await profiles.inspect(invocation, expected_source_sha256=source)
            await guard()
            _need(configured.ok and configured.profile is not None
                  and configured.profile.target_environment is not None, 'resolution_data_unavailable')
            assert configured.profile is not None and configured.profile.target_environment is not None
            environment = dict(configured.profile.target_environment)
        planning = PythonMetadataPlanningRequest(invocation=invocation, scope=scope,
            draft=PythonMetadataDraft(source_sha256=source,
                acceptance_sha256=hashlib.sha256(cast(bytes, observation[0])).hexdigest(),
                manifest_sha256=hashlib.sha256(controls[0]).hexdigest(),
                lock_sha256=hashlib.sha256(controls[1]).hexdigest()),
            expected_root_config_sha256=root_config_sha256,
            expected_catalog_sha256=base.profile.catalog_sha256)
        try:
            if publish:
                # Pins/closure are actual current controls; hash equality alone is insufficient.
                await flow.resolve_data(needs=needs, environment=environment, planning=planning)
                validate_dynamic_python_controls(controls[0], controls[1], flow.descriptor)
                await flow.prepare(needs=needs, environment=environment, planning=planning)
            else:
                await flow.resolve_data(needs=needs, environment=environment, planning=planning)
        except ResolutionDeclarationChangedError as error:
            if (type(error) is not ResolutionDeclarationChangedError or len(error.args) != 1
                    or type(error.args[0]) is not str or error.args[0] != 'resolution_declaration_changed'):
                raise
            await self._reject_draft(authority, actor, observation,
                'generated_acceptance_python_declaration_invalid', ('python_declaration_invalid',))
        except ResolutionApprovalRequired as error:
            # Reuse the existing concrete project.verify approval projection.
            # The request retains the distinct resolution/publication namespace.
            raise PythonMetadataPreparationApprovalRequired(error.request) from None
        await guard()
        await journal.store_public_data(authority.tenant_id, authority.run_id, flow.public_data())
        await guard()
        return True

    async def _prepare_dependency_metadata(
        self, authority: GeneratedAuthority, actor: str,
        observation: tuple[bytes | None, dict[str, str]], controls: tuple[bytes, bytes],
        *, deadline: float,
    ) -> None:
        from agent_hub.capabilities.approvals import capability_approval_scope
        from agent_hub.capabilities.python_metadata_preparation import (
            METADATA_KEY,
            metadata_preparation_request,
            metadata_scope,
        )
        from agent_hub.capabilities.python_metadata_preparation_flow import prepare_python_metadata
        from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
            PythonMetadataDraft,
            PythonMetadataPlan,
        )

        invocation = authority.invocation
        _need(observation[0] is not None)
        latest = await self._record(authority.tenant_id, authority.run_id)
        scope = metadata_scope(latest, actor=actor, live=True)
        draft = PythonMetadataDraft(source_sha256=_fingerprint(observation[1]),
            acceptance_sha256=hashlib.sha256(cast(bytes, observation[0])).hexdigest(),
            manifest_sha256=hashlib.sha256(controls[0]).hexdigest(),
            lock_sha256=hashlib.sha256(controls[1]).hexdigest())

        async def guard() -> None:
            await asyncio.to_thread(self._controls_absent, invocation)
            _need(await self._draft_observation(invocation) == observation)

        async def state_io(*, expected: Mapping[str, JsonValue] | None = None,
                           replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None:
            await guard()
            current = await self._record(authority.tenant_id, authority.run_id)
            _need(generated_authority(current, authority.tenant_id, authority.run_id) == authority
                  and metadata_scope(current, actor=actor, live=True) == scope)
            route = getattr(current, 'routing_decision', {})
            _draft_route(current)
            state = cast(Mapping[str, JsonValue] | None, route.get(METADATA_KEY))
            if replace is not None:
                _need(state == expected and authority.lease_token is not None)
                setter = getattr(self.repository, 'compare_and_set_python_metadata_preparation', None)
                _need(callable(setter), 'metadata_prepare_persistence_unavailable')
                assert callable(setter)
                current = await setter(authority.tenant_id, authority.run_id, actor_id=authority.user_id,
                    actor_role=authority.role, lease_token=authority.lease_token,
                    request_sha256=authority.request_sha256, project_id=authority.project_id,
                    session_id=authority.session_id, expected=expected, replacement=replace,
                    before_reservation=guard)
                return cast(Mapping[str, JsonValue], current.routing_decision[METADATA_KEY])
            return state

        async def authorize(plan: PythonMetadataPlan) -> None:
            request = metadata_preparation_request(plan, idempotency_key='metadata-prepare-' + plan.sha256)
            checker = getattr(self.repository, 'is_capability_approval_scope_approved', None)
            _need(callable(checker), 'metadata_prepare_persistence_unavailable')
            assert callable(checker)
            approved = await checker(authority.tenant_id, authority.run_id, capability_approval_scope(request))
            if approved is not True:
                raise PythonMetadataPreparationApprovalRequired(request)

        client = self._metadata_client
        if client is None:
            from agent_hub.skills.sandbox.python_metadata_preparation_client import (
                PythonMetadataPreparationSandbox,
            )

            client = PythonMetadataPreparationSandbox()
        await prepare_python_metadata(client=client, invocation=invocation, scope=scope, draft=draft,
            state_io=state_io, authorize=authorize, deadline=deadline)

    async def _dependency_draft_preflight(
        self, authority: GeneratedAuthority, actor: str,
        observation: tuple[bytes | None, dict[str, str]], controls: tuple[bytes, bytes],
    ) -> Mapping[str, JsonValue]:
        from agent_hub.skills.sandbox.python_dependency_protocol import (
            PythonDependencyClosureFailure,
            PythonDependencyClosureObservation,
        )

        invocation = authority.invocation
        deadline = asyncio.get_running_loop().time() + invocation.timeout_seconds
        latest = await self._record(authority.tenant_id, authority.run_id)
        _draft_route(latest)
        await asyncio.to_thread(self._controls_absent, invocation)
        source = _fingerprint(observation[1])
        async def inspect() -> PythonDependencyClosureObservation:
            remaining = deadline - asyncio.get_running_loop().time()
            _need(remaining >= 1, 'python_dependency_unavailable')
            readonly = ProjectInvocation.model_validate({**invocation.model_dump(),
                'execution_id': 'dependency_preflight_' + uuid4().hex, 'timeout_seconds': int(remaining),
                'sandbox_profile': 'restricted', 'install_allowed': False}, strict=True)
            async with asyncio.timeout_at(deadline):
                return await self._dependency_inspector(readonly, source)

        route = latest.routing_decision
        metadata_enabled = (type(route.get('python_dependency_preflight_submission_version')) is int
                            and route['python_dependency_preflight_submission_version'] == 1)
        prepared = 'python_metadata_preparation' in route
        if prepared:
            from agent_hub.capabilities.python_metadata_preparation import (
                metadata_scope,
                metadata_state_plan,
            )

            metadata = route['python_metadata_preparation']
            prior_plan = metadata_state_plan(metadata)
            _need(prior_plan.scope == metadata_scope(latest, actor=actor, live=True))
            # A known receipt is history, not authority over a later corrected draft.
            # Only unresolved executions must inspect their original bound draft.
            if metadata['phase'] != 'prepared':
                await self._prepare_dependency_metadata(authority, actor, observation, controls, deadline=deadline)
        try:
            observed = await inspect()
        except PythonDependencyClosureFailure as error:
            if metadata_enabled and not prepared and error.code == 'python_dependency_metadata_unavailable':
                await self._prepare_dependency_metadata(authority, actor, observation, controls, deadline=deadline)
                observed = await inspect()
            else:
                raise GeneratedProjectError(error.code) from None
        _need(type(observed) is PythonDependencyClosureObservation, 'python_dependency_unavailable')
        observed = PythonDependencyClosureObservation.model_validate_json(
            observed.model_dump_json(warnings='error'), strict=True)
        _need(observed.source_sha256 == source
              and observed.manifest_sha256 == hashlib.sha256(controls[0]).hexdigest()
              and observed.lock_sha256 == hashlib.sha256(controls[1]).hexdigest())
        _need(await self._draft_observation(invocation) == observation)
        latest = await self._record(authority.tenant_id, authority.run_id)
        _need(generated_authority(latest, authority.tenant_id, authority.run_id) == authority
              and _dependency_selected(latest.routing_decision))
        _draft_route(latest)
        if observed.closure['status'] != 'complete' and await self._resolve_dependency_draft(
            authority, actor, observation, controls, publish=True, force=True
        ):
            observed = await inspect()
            _need(observed.source_sha256 == source
                  and observed.manifest_sha256 == hashlib.sha256(controls[0]).hexdigest()
                  and observed.lock_sha256 == hashlib.sha256(controls[1]).hexdigest())
        payload = cast(Mapping[str, JsonValue], observed.model_dump(mode='json'))
        if observed.closure['status'] != 'complete':
            await self._reject_draft(authority, actor, observation,
                'generated_acceptance_python_dependency_incomplete', ('python_dependency_incomplete',),
                dependency=payload)
        await asyncio.to_thread(self._controls_absent, invocation)
        return _freeze_object(payload, name='dependency preflight')

    async def load(self, tenant: UUID, run: UUID, actor: str, *, readonly: bool = False) -> GeneratedProjectContext | None:
        try:
            tenant, run = _uuid(tenant), _uuid(run)
            record = await self._record(tenant, run)
            if not generated_selected(record):
                return None
            authority = generated_authority(record, tenant, run, readonly=readonly)
            route = cast(Mapping[str, object], record.routing_decision)
            raw_state = route.get(STATE)
            if raw_state is None:
                return None
            state = validate_generated_preparation(raw_state)
            if state['stack'] == 'python':
                _need((_DEPENDENCY in state) == _dependency_selected(route))
            _need(state['phase'] != 'capturing', 'generated_acceptance_unknown_no_recapture')
            invocation = authority.invocation
            reference = ProjectBaselineReference.from_payload(state['reference'])
            stored = await asyncio.to_thread(ProjectBaselineStore(self.root).load, invocation, reference)
            raw = await asyncio.to_thread(self._manifest, invocation, stored=stored)
            body = await asyncio.to_thread(self._read_envelope, invocation)
            envelope = GeneratedAcceptanceEnvelope.from_payload(json.loads(body), manifest_raw=raw)
            _need(body == _encoded(envelope.to_payload()) and stored.generated_metadata == {
                'schema_version': 1, 'purpose': 'generated_acceptance', 'capture_phase': 'post_model',
                'user_id': str(authority.user_id), 'request_sha256': authority.request_sha256,
                'manifest_sha256': hashlib.sha256(raw).hexdigest()})
            envelope.require_scope(tenant_id=tenant, user_id=authority.user_id, run_id=run,
                project_id=authority.project_id, session_id=authority.session_id,
                request_sha256=authority.request_sha256, proposer_actor=cast(str, state['proposer_actor']))
            _need(envelope.reference == reference and envelope.contract == stored.contract
                  and dict(envelope.source_files) == dict(stored.source_files)
                  and state['acceptance_sha256'] == envelope.sha256
                  and state['user_id'] == str(authority.user_id) and state['stack'] == envelope.manifest.stack
                  and state['request_sha256'] == authority.request_sha256
                  and state['manifest_sha256'] == envelope.manifest.sha256
                  and state['manifest_raw_sha256'] == hashlib.sha256(raw).hexdigest())
            envelope.require_current(await project_source_manifest(self.root, invocation))
            latest = await self._record(tenant, run)
            _need(generated_authority(latest, tenant, run, readonly=readonly) == authority
                  and validate_generated_preparation(latest.routing_decision[STATE]) == state)
            return GeneratedProjectContext(record, invocation, stored, envelope, state, authority)
        except GeneratedProjectError:
            raise
        except Exception:  # noqa: BLE001 - filesystem and store failures expose only finite codes.
            raise GeneratedProjectError('generated_acceptance_unavailable_or_changed') from None

    async def prepare(self, tenant: UUID, run: UUID, actor: str, *, user_id: UUID) -> GeneratedProjectContext | None:
        phase = 'authority'
        try:
            tenant, run = _uuid(tenant), _uuid(run)
            record = await self._record(tenant, run)
            if not generated_selected(record):
                return None
            authority = generated_authority(record, tenant, run)
            _need(user_id == authority.user_id)
            route = cast(Mapping[str, object], record.routing_decision)
            if route.get(STATE) is not None:
                return await self.load(tenant, run, actor)
            _need(type(actor) is str and re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,127}', actor) is not None)
            invocation = authority.invocation
            phase = 'draft_observation'
            observation = await self._draft_observation(invocation)
            raw, files = observation
            if raw is None:
                await self._reject_draft(authority, actor, observation,
                    'generated_acceptance_manifest_required', ('manifest_missing',))
            phase = 'acceptance_manifest'
            try:
                manifest = parse_acceptance_manifest(raw)
            except ValueError:
                await self._reject_draft(authority, actor, observation,
                    'generated_acceptance_manifest_invalid', _manifest_issues(raw))
            required = {'package.json'} if manifest.stack == 'node' else {'pyproject.toml', 'requirements.lock'}
            if not {*manifest.test_files, *required, _MANIFEST} <= files.keys():
                await self._reject_draft(authority, actor, observation,
                    'generated_acceptance_scaffold_incomplete', ('scaffold_incomplete',))
            parts = (str(tenant), 'projects', authority.project_id, 'sessions', authority.session_id)
            dependency: Mapping[str, JsonValue] | None = None
            if manifest.stack == 'node':
                phase = 'node_declaration'
                from agent_hub.capabilities.project_inspection import _parse_node
                data = await asyncio.to_thread(_read_no_links, self.root, (*parts, 'package.json'), max_bytes=1024**2)
                _need(files['package.json'] in {hashlib.sha256(tag + data).hexdigest() for tag in (b'f\0', b'x\0')})
                try:
                    _, _, operations, issues = _parse_node(data.decode('utf-8-sig'))
                    package = json.loads(data)
                    if (issues or operations != {'build': 'declared', 'test': 'declared'}
                            or {'workspaces', 'overrides', 'resolutions', 'pnpm', 'devEngines'} & package.keys()
                            or {'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'yarn.lock'} & files.keys()):
                        raise ValueError
                except (ValueError, TypeError, KeyError, RecursionError):
                    await self._reject_draft(authority, actor, observation,
                        'generated_acceptance_node_declaration_invalid', ('node_declaration_invalid',))
            if manifest.stack == 'python':
                phase = 'python_controls'
                from agent_hub.capabilities.python_project_recipe import (
                    parse_python_project_declaration,
                    python_declaration_issue,
                )
                controls: list[bytes] = []
                for name in ('pyproject.toml', 'requirements.lock'):
                    data = await asyncio.to_thread(_read_no_links, self.root, (*parts, name), max_bytes=256 * 1024)
                    _need(files[name] in {hashlib.sha256(tag + data).hexdigest() for tag in (b'f\0', b'x\0')})
                    controls.append(data)
                if _dependency_selected(route):
                    phase = 'dependency_resolution_read'
                    await self._resolve_dependency_draft(authority, actor, observation, (controls[0], controls[1]))
                phase = 'python_declaration'
                try:
                    parse_python_project_declaration(*controls, max_control_bytes=256 * 1024, max_requirements=128)
                except (ValueError, TypeError) as error:
                    await self._reject_draft(authority, actor, observation,
                        'generated_acceptance_python_declaration_invalid',
                        (python_declaration_issue(controls[1], error),))
                if _dependency_selected(route):
                    if (authority.tenant_id, authority.run_id, actor) in self._resolution_flows:
                        phase = 'dependency_resolution_publish'
                        await self._resolve_dependency_draft(authority, actor, observation,
                            (controls[0], controls[1]), publish=True)
                    phase = 'dependency_preflight'
                    dependency = await self._dependency_draft_preflight(authority, actor, observation,
                        (controls[0], controls[1]))
            state_body: dict[str, JsonValue] = {'schema_version': 1, 'phase': 'capturing',
                'user_id': str(authority.user_id), 'request_sha256': authority.request_sha256,
                'proposer_actor': actor, 'stack': manifest.stack, 'manifest_sha256': manifest.sha256,
                'manifest_raw_sha256': hashlib.sha256(raw).hexdigest()}
            if dependency is not None:
                state_body[_DEPENDENCY] = dependency
            state = validate_generated_preparation(state_body)

            async def before_reservation() -> None:
                # The repository holds the run row lock; do not reserve a known-stale draft.
                await asyncio.to_thread(self._controls_absent, invocation)
                _need(await self._draft_observation(invocation) == observation)

            phase = 'baseline_reservation'
            await self._cas(authority, None, state,
                before_reservation=before_reservation if dependency is not None else None)
            metadata: dict[str, JsonValue] = {'schema_version': 1, 'purpose': 'generated_acceptance',
                'capture_phase': 'post_model', 'user_id': str(authority.user_id),
                'request_sha256': authority.request_sha256, 'manifest_sha256': hashlib.sha256(raw).hexdigest()}
            store = ProjectBaselineStore(self.root)
            phase = 'baseline_capture'
            reference = await store.capture_generated_once(invocation, editable_paths=manifest.editable_paths, metadata=metadata)
            if dependency is not None:
                _need(reference.input_source_sha256 == dependency['source_sha256'])
            stored = await asyncio.to_thread(store.load, invocation, reference)
            captured_raw = await asyncio.to_thread(self._manifest, invocation, stored=stored)
            _need(raw == captured_raw)
            envelope = GeneratedAcceptanceEnvelope.bind(tenant_id=tenant, user_id=authority.user_id, run_id=run,
                project_id=authority.project_id, session_id=authority.session_id, request_sha256=authority.request_sha256,
                proposer_actor=actor, manifest_raw=raw, reference=reference, source_files=stored.source_files,
                contract=stored.contract)
            phase = 'envelope_persistence'
            await asyncio.to_thread(self._write_envelope, invocation, envelope)
            await self._cas(authority, state, {**state, 'phase': 'captured', 'reference': cast(JsonValue, reference.to_payload()),
                                             'acceptance_sha256': envelope.sha256})
            phase = 'captured_load'
            return await self.load(tenant, run, actor)
        except GeneratedProjectError:
            raise
        except Exception as error:  # noqa: BLE001 - uncertain captures remain consumed; never expose source/path data.
            from agent_hub.capabilities.python_input_preparation import InspectOnlyError

            if (type(error) is InspectOnlyError and len(error.args) == 1
                    and type(error.args[0]) is str and error.args[0] == 'resolution_inspect_only'):
                raise GeneratedProjectError('generated_acceptance_resolution_inspect_only') from None
            if type(error) is PythonMetadataPreparationFailure:
                try:
                    public_code = PythonMetadataPreparationFailure(phase=error.phase, code=error.code).public_code
                except (ValueError, TypeError, AttributeError):
                    pass
                else:
                    raise GeneratedProjectError(public_code) from None
            kind = type(error).__name__
            _LOGGER.warning(
                'generated_project_prepare_failed stage=%s error_kind=%s run_id=%s',
                phase, kind if kind in _PREPARATION_ERROR_KINDS else 'Exception',
                run if type(run) is UUID else None,
            )
            raise GeneratedProjectError('generated_acceptance_unavailable_or_changed') from None

    async def update(self, context: GeneratedProjectContext, replacement: Mapping[str, JsonValue]) -> GeneratedProjectContext:
        value = validate_generated_preparation(replacement)
        _need(all(value.get(key) == context.state.get(key) for key in (_BASE | _CAPTURED) - {'phase'}))
        await self._cas(context.authority, context.state, value)
        current = await self.load(context.authority.tenant_id, context.authority.run_id, context.envelope.proposer_actor)
        _need(current is not None)
        assert current is not None
        return current
