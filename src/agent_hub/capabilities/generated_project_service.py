"""Once-only post-model acceptance capture, separate from user original inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_draft import GeneratedDraftRejection
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
)
from agent_hub.capabilities.project_contracts import _path
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.domain.runs import RunStatus
from agent_hub.plugins.handover_snapshots import _Directory
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_manifest

VERSION = 'generated_project_contract_version'
STATE = 'generated_project_preparation'
_MANIFEST = 'ACCEPTANCE.json'
_ENVELOPE = 'generated-acceptance.json'
_SHA = re.compile(r'[a-f0-9]{64}')
_BASE = {'schema_version', 'phase', 'user_id', 'request_sha256', 'proposer_actor', 'stack',
         'manifest_sha256', 'manifest_raw_sha256'}
_CAPTURED = {'reference', 'acceptance_sha256'}


class GeneratedProjectError(RuntimeError):
    """Finite generated preparation failure; never raw project or host text."""


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
    def __init__(self, *, root: Path, repository: object) -> None:
        self.root, self.repository = root, repository

    async def _record(self, tenant: UUID, run: UUID) -> Any:
        getter = getattr(self.repository, 'get', None)
        _need(callable(getter))
        assert callable(getter)
        return await getter(tenant, run)

    async def _cas(self, authority: GeneratedAuthority, expected: Mapping[str, JsonValue] | None,
                   replacement: Mapping[str, JsonValue]) -> object:
        value = validate_generated_transition(expected, replacement)
        _need(authority.lease_token is not None)
        setter = getattr(self.repository, 'compare_and_set_generated_preparation', None)
        _need(callable(setter), 'generated_acceptance_persistence_unavailable')
        assert callable(setter)
        return await setter(authority.tenant_id, authority.run_id, actor_id=authority.user_id,
            actor_role=authority.role, lease_token=authority.lease_token,
            request_sha256=authority.request_sha256, project_id=authority.project_id,
            session_id=authority.session_id, expected=expected, replacement=json.loads(_encoded(value)))

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
        reason: str, issues: tuple[str, ...],
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
        raise GeneratedProjectDraftRejected(GeneratedDraftRejection(
            tenant_id=str(authority.tenant_id), user_id=str(authority.user_id), run_id=str(authority.run_id),
            project_id=authority.project_id, session_id=authority.session_id,
            request_sha256=authority.request_sha256, proposer_actor=actor,
            observation_sha256=observation_sha, reason=reason, issues=issues))

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
            observation = await self._draft_observation(invocation)
            raw, files = observation
            if raw is None:
                await self._reject_draft(authority, actor, observation,
                    'generated_acceptance_manifest_required', ('manifest_missing',))
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
            if manifest.stack == 'node':
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
                from agent_hub.capabilities.python_project_recipe import (
                    parse_python_project_declaration,
                )
                controls: list[bytes] = []
                for name in ('pyproject.toml', 'requirements.lock'):
                    data = await asyncio.to_thread(_read_no_links, self.root, (*parts, name), max_bytes=256 * 1024)
                    _need(files[name] in {hashlib.sha256(tag + data).hexdigest() for tag in (b'f\0', b'x\0')})
                    controls.append(data)
                try:
                    parse_python_project_declaration(*controls, max_control_bytes=256 * 1024, max_requirements=128)
                except (ValueError, TypeError):
                    await self._reject_draft(authority, actor, observation,
                        'generated_acceptance_python_declaration_invalid', ('python_declaration_invalid',))
            state = validate_generated_preparation({'schema_version': 1, 'phase': 'capturing',
                'user_id': str(authority.user_id), 'request_sha256': authority.request_sha256,
                'proposer_actor': actor, 'stack': manifest.stack, 'manifest_sha256': manifest.sha256,
                'manifest_raw_sha256': hashlib.sha256(raw).hexdigest()})
            await self._cas(authority, None, state)
            metadata: dict[str, JsonValue] = {'schema_version': 1, 'purpose': 'generated_acceptance',
                'capture_phase': 'post_model', 'user_id': str(authority.user_id),
                'request_sha256': authority.request_sha256, 'manifest_sha256': hashlib.sha256(raw).hexdigest()}
            store = ProjectBaselineStore(self.root)
            reference = await store.capture_generated_once(invocation, editable_paths=manifest.editable_paths, metadata=metadata)
            stored = await asyncio.to_thread(store.load, invocation, reference)
            captured_raw = await asyncio.to_thread(self._manifest, invocation, stored=stored)
            _need(raw == captured_raw)
            envelope = GeneratedAcceptanceEnvelope.bind(tenant_id=tenant, user_id=authority.user_id, run_id=run,
                project_id=authority.project_id, session_id=authority.session_id, request_sha256=authority.request_sha256,
                proposer_actor=actor, manifest_raw=raw, reference=reference, source_files=stored.source_files,
                contract=stored.contract)
            await asyncio.to_thread(self._write_envelope, invocation, envelope)
            await self._cas(authority, state, {**state, 'phase': 'captured', 'reference': cast(JsonValue, reference.to_payload()),
                                             'acceptance_sha256': envelope.sha256})
            return await self.load(tenant, run, actor)
        except GeneratedProjectError:
            raise
        except Exception:  # noqa: BLE001 - uncertain captures remain consumed; never expose source/path data.
            raise GeneratedProjectError('generated_acceptance_unavailable_or_changed') from None

    async def update(self, context: GeneratedProjectContext, replacement: Mapping[str, JsonValue]) -> GeneratedProjectContext:
        value = validate_generated_preparation(replacement)
        _need(all(value.get(key) == context.state.get(key) for key in (_BASE | _CAPTURED) - {'phase'}))
        await self._cas(context.authority, context.state, value)
        current = await self.load(context.authority.tenant_id, context.authority.run_id, context.envelope.proposer_actor)
        _need(current is not None)
        assert current is not None
        return current
