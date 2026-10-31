"""Trusted same-run input-supply metadata; publication is never installation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Protocol, cast, get_args
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepository
from agent_hub.runtime.contracts import Artifact, JsonValue, _freeze_object
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
    SupplyError,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, digest, requirement

SUPPLY_KEY = 'python_input_supply'
SUPPLY_PREFIX = 'workspace/current/project-inputs/python/'
_ERROR = 'input_supply_scope_changed'


class InputSupplyClient(Protocol):
    async def plan(self, invocation: PythonProjectProbeInvocation) -> PythonInputSupplyResponse: ...

    async def supply(self, invocation: PythonProjectProbeInvocation, *,
                     expected_plan_sha256: str) -> PythonInputSupplyReceipt: ...

    async def inspect(self, invocation: PythonProjectProbeInvocation, *, expected_plan_sha256: str,
                      supply_execution_id: str) -> PythonInputSupplyStatus: ...


class SupplyStateAccess(Protocol):
    async def __call__(self, *, expected: Mapping[str, JsonValue] | None = None,
                       replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None: ...


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def supply_state_plan(value: object) -> PythonInputSupplyPlan:
    """Decode exact durable shapes, including a separately bound Root receipt."""
    try:
        if not isinstance(value, Mapping) or len(_encoded(value)) > 1024**2:
            raise ValueError
        phase = value.get('phase')
        keys = {'schema_version', 'phase', 'plan', 'plan_sha256'}
        if phase in ('executing', 'supplied'):
            keys.add('execution_id')
            execution = value.get('execution_id')
            if type(execution) is not str or re.fullmatch(r'input_supply_[a-f0-9]{32}', execution) is None:
                raise ValueError
        if phase == 'supplied':
            keys.add('receipt')
        if (set(value) != keys or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or phase not in ('planned', 'executing', 'supplied')):
            raise ValueError
        plan = PythonInputSupplyPlan.model_validate_json(_encoded(value['plan']), strict=True)
        if plan.sha256 != value['plan_sha256']:
            raise ValueError
        if phase == 'supplied':
            receipt = PythonInputSupplyReceipt.model_validate_json(_encoded(value['receipt']), strict=True)
            if receipt.plan != plan or receipt.supply_execution_id != value['execution_id']:
                raise ValueError
        return plan
    except (ValueError, TypeError, KeyError):
        raise ValueError('input_supply_request_invalid') from None


def input_supply_request(
    plan: PythonInputSupplyPlan, *, user_id: UUID, idempotency_key: str,
) -> CapabilityRequest:
    if type(plan) is not PythonInputSupplyPlan or type(user_id) is not UUID:
        raise ValueError(_ERROR)
    plan = PythonInputSupplyPlan.model_validate_json(plan.model_dump_json(), strict=True)
    return CapabilityRequest(tenant_id=plan.scope.tenant_id, run_id=plan.scope.run_id,
        user_id=user_id, agent_id='main_agent', capability='command', operation='run',
        resource=SUPPLY_PREFIX + plan.sha256, idempotency_key=idempotency_key,
        arguments={'schema_version': 1, 'plan_sha256': plan.sha256,
            'original_reference': plan.scope.original_reference.model_dump(mode='json'),
            'project_id': plan.scope.project_id, 'workspace_session_id': plan.scope.session_id,
            'catalog_sha256': plan.catalog_sha256, 'declaration_sha256': plan.declaration_sha256,
            'manifest_sha256': plan.manifest_sha256, 'lock_sha256': plan.lock_sha256})


def input_supply_binding(record: object, *, planned: bool = False) -> CapabilityRequest:
    """Validate DB-owned metadata for policy/public history, not filesystem authority."""
    route = getattr(record, 'routing_decision', None)
    if not isinstance(route, Mapping):
        raise TypeError(_ERROR)
    state = route.get(SUPPLY_KEY)
    plan = supply_state_plan(state)
    assert isinstance(state, Mapping)
    tenant, run, actor = (getattr(record, name, None) for name in ('tenant_id', 'id', 'actor_id'))
    role = getattr(record, 'actor_role', None)
    if (not isinstance(role, str) or not all(isinstance(item, UUID) for item in (tenant, run, actor))
            or Role(role) not in {Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR}
            or plan.scope.tenant_id != tenant or plan.scope.run_id != run
            or planned and state['phase'] != 'planned'):
        raise ValueError(_ERROR)
    project, session = authorized_workspace_scope(record, tenant_id=tenant, run_id=run)
    preparation = route.get('project_input_preparation')
    permissions = route.get('requested_permissions')
    if (type(route.get('project_input_contract_version')) is not int
            or route['project_input_contract_version'] != 1
            or route.get('sandbox_profile') != 'workspace_write'
            or route.get('execution_backend', 'systemd') != 'systemd'
            or not isinstance(permissions, (tuple, list))
            or not {'workspace.read', 'workspace.write', 'command.run'}.issubset(permissions)
            or project != plan.scope.project_id or session != plan.scope.session_id
            or not isinstance(preparation, Mapping)
            or type(preparation.get('schema_version')) is not int
            or preparation['schema_version'] not in {1, 2}
            or preparation.get('phase') not in {'captured', 'executing', 'observed'}):
        raise ValueError(_ERROR)
    ManagedProjectInput.from_payload(route.get('project_input'))
    reference = ProjectBaselineReference.from_payload(preparation.get('reference'))
    keys = {'schema_version', 'phase', 'reference'}
    if preparation['schema_version'] == 1:
        if preparation['phase'] != 'captured':
            raise ValueError(_ERROR)
    else:
        keys.add('python_recipe')
        marker = preparation.get('python_recipe')
        if (not isinstance(marker, Mapping)
                or set(marker) != {'schema_version', 'recipe_sha256', 'environment_input_sha256'}
                or type(marker.get('schema_version')) is not int or marker['schema_version'] != 1
                or any(type(marker.get(key)) is not str or re.fullmatch(r'[a-f0-9]{64}', marker[key]) is None
                       for key in ('recipe_sha256', 'environment_input_sha256'))):
            raise ValueError(_ERROR)
        if preparation['phase'] in {'executing', 'observed'}:
            keys.add('execution_id')
            if preparation.get('execution_id') != 'original_' + cast(UUID, run).hex:
                raise ValueError(_ERROR)
        if preparation['phase'] == 'observed':
            keys.update({'observation', 'observation_sha256'})
            if (not isinstance(preparation.get('observation'), Mapping)
                    or type(preparation.get('observation_sha256')) is not str
                    or re.fullmatch(r'[a-f0-9]{64}', preparation['observation_sha256']) is None):
                raise ValueError(_ERROR)
    if (reference.to_payload() != plan.scope.original_reference.model_dump(mode='json')
            or set(preparation) != keys
            or planned and (preparation['schema_version'] != 1 or preparation['phase'] != 'captured')
            or preparation['schema_version'] == 2 and state['phase'] != 'supplied'):
        raise ValueError(_ERROR)
    # ORM rows can carry asyncpg's UUID subclass; strict capability IDs use UUID itself.
    return input_supply_request(plan, user_id=UUID(str(actor)), idempotency_key='input-supply-binding')


def validate_supply_declaration(
    plan: PythonInputSupplyPlan, invocation: PythonProjectProbeInvocation, stored: StoredProjectBaseline,
    *, expected_catalog_sha256: str | None = None,
) -> None:
    """Bind Root plan to immutable captured bytes, not model/prompt hashes."""
    plan.require_scope(invocation)
    if expected_catalog_sha256 is not None and plan.catalog_sha256 != expected_catalog_sha256:
        raise ValueError('input_supply_plan_changed')
    controls: list[bytes] = []
    for name in ('pyproject.toml', 'requirements.lock'):
        raw = _read_no_links(stored.snapshot, (name,), max_bytes=256 * 1024)
        if stored.source_files.get(name) not in {
                hashlib.sha256(tag + raw).hexdigest() for tag in (b'f\0', b'x\0')}:
            raise ValueError(_ERROR)
        controls.append(raw)
    declaration = parse_python_project_declaration(*controls, max_control_bytes=256 * 1024,
                                                  max_requirements=128)
    locked = {item.name: item for item in declaration.requirements}
    if (plan.declaration_sha256 != declaration.sha256
            or plan.manifest_sha256 != declaration.manifest_sha256
            or plan.lock_sha256 != declaration.lock_sha256
            or {item.name for item in plan.artifacts} != set(locked)
            or any(item.version != locked[item.name].version
                   or item.sha256 not in locked[item.name].hashes for item in plan.artifacts)):
        raise ValueError('input_supply_plan_changed')


def require_supplied_recipe(state: Mapping[str, JsonValue], inputs: PythonProjectInputs) -> None:
    plan = supply_state_plan(state)
    if (state['phase'] != 'supplied'
            or inputs.original_reference.to_payload() != plan.scope.original_reference.model_dump(mode='json')
            or inputs.declaration.sha256 != plan.declaration_sha256
            or inputs.interpreter.to_payload() != plan.interpreter.model_dump()
            or tuple(w.to_payload() for w in inputs.wheels) != tuple({
                'filename': w.filename, 'name': w.name, 'version': w.version, 'sha256': w.sha256,
            } for w in plan.artifacts)):
        raise ValueError('input_supply_plan_changed')


async def _root_io[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except TimeoutError:
        raise ValueError('input_supply_timeout') from None
    except (OSError, ValueError) as error:
        code = str(error)
        raise ValueError(code if code in get_args(SupplyError) else 'input_supply_unavailable') from None


async def supply_python_inputs(
    *, client: InputSupplyClient, invocation: PythonProjectProbeInvocation,
    stored: StoredProjectBaseline, state_io: SupplyStateAccess,
    authorize: Callable[[PythonInputSupplyPlan, Mapping[str, JsonValue]], Awaitable[None]],
    deadline: float, expected_catalog_sha256: str | None = None,
) -> Mapping[str, JsonValue]:
    """Persist intent before Root IO; executing is inspect-only, including after crashes."""
    def fresh(prefix: str) -> PythonProjectProbeInvocation:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ValueError('input_supply_timeout')
        return PythonProjectProbeInvocation.model_validate({**invocation.model_dump(),
            'execution_id': prefix + uuid4().hex, 'timeout_seconds': max(1, int(remaining)),
            'install_allowed': False}, strict=True)

    async def current(expected: Mapping[str, JsonValue] | None) -> None:
        if await state_io() != expected:
            raise ValueError(_ERROR)

    async def measure() -> PythonInputSupplyPlan:
        probe = fresh('input_plan_')
        request = PythonInputSupplyRequest(action='plan_python_project_inputs', invocation=probe)
        response = await _root_io(client.plan(probe))
        await current(state)
        if type(response) is not PythonInputSupplyResponse:
            raise ValueError('input_supply_request_invalid')
        response = PythonInputSupplyResponse.model_validate_json(response.model_dump_json(), strict=True)
        response.require_for(request)
        if not response.ok or response.plan is None:
            raise ValueError(response.error or 'input_supply_unavailable')
        validate_supply_declaration(response.plan, probe, stored, expected_catalog_sha256=expected_catalog_sha256)
        return response.plan

    state = await state_io()
    if state is None:
        plan = await measure()
        state = await state_io(replace=_freeze_object({'schema_version': 1, 'phase': 'planned',
            'plan': plan.model_dump(mode='json'), 'plan_sha256': plan.sha256}, name='input supply state'))
        assert state is not None
    else:
        plan = supply_state_plan(state)
        validate_supply_declaration(plan, invocation, stored, expected_catalog_sha256=expected_catalog_sha256)
        if state['phase'] == 'planned' and await measure() != plan:
            raise ValueError('input_supply_plan_changed')
    if state['phase'] == 'planned':
        await authorize(plan, state)
        await current(state)
        execution = fresh('input_supply_')
        state = await state_io(expected=state, replace={**state, 'phase': 'executing',
                                                       'execution_id': execution.execution_id})
        assert state is not None
        receipt = await _root_io(client.supply(execution, expected_plan_sha256=plan.sha256))
        await current(state)
    elif state['phase'] == 'executing':
        execution_id = cast(str, state['execution_id'])
        probe = fresh('input_inspect_')
        status = await _root_io(client.inspect(probe, expected_plan_sha256=plan.sha256,
                                              supply_execution_id=execution_id))
        await current(state)
        if type(status) is not PythonInputSupplyStatus:
            raise ValueError('input_supply_request_invalid')
        status = PythonInputSupplyStatus.model_validate_json(status.model_dump_json(), strict=True)
        if status.plan_sha256 != plan.sha256 or status.supply_execution_id != execution_id:
            raise ValueError(_ERROR)
        if status.state != 'supplied' or status.receipt is None:
            raise ValueError('input_supply_unknown')
        receipt = status.receipt
    else:
        await current(state)
        return state
    if type(receipt) is not PythonInputSupplyReceipt:
        raise ValueError('input_supply_request_invalid')
    receipt = PythonInputSupplyReceipt.model_validate_json(receipt.model_dump_json(), strict=True)
    receipt.plan.require_scope(invocation)
    if receipt.plan != plan or receipt.supply_execution_id != state['execution_id']:
        raise ValueError('input_supply_plan_changed')
    result = await state_io(expected=state, replace={**state, 'phase': 'supplied',
        'receipt': cast(Mapping[str, JsonValue], receipt.model_dump(mode='json'))})
    assert result is not None
    return result


def dynamic_python_needs(manifest: bytes) -> tuple[DependencyNeed, ...]:
    """Read declarative requirements before capture; not network/install authority.

    Evidence identifies each declaration rather than the whole mutable draft.
    Physical source/control hashes remain independently bound by Root planning.
    """
    if type(manifest) is not bytes or not 0 < len(manifest) <= 256 * 1024:
        raise ValueError('resolution_requirement_invalid')
    try:
        data = tomllib.loads(manifest.decode('utf-8-sig'))
        project, build = data['project'], data['build-system']
        if (type(project) is not dict or type(build) is not dict
                or build.get('build-backend') != 'setuptools.build_meta'
                or project.get('dynamic') or project.get('optional-dependencies')
                or 'dependency-groups' in data):
            raise ValueError('resolution_source_unsupported')
        declarations = []
        for owner, key in ((project, 'dependencies'), (build, 'requires')):
            entries = owner.get(key, [])
            if type(entries) is not list or len(entries) > 128:
                raise ValueError('resolution_requirement_invalid')
            for entry in entries:
                parsed = requirement(entry)
                text = str(parsed)
                declarations.append(DependencyNeed(text, digest({'requirement': text})))
        result = tuple(sorted(set(declarations), key=lambda entry: entry.requirement))
        if not 0 < len(result) <= 128:
            raise ValueError('resolution_requirement_invalid')
        return result
    except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError, RecursionError):
        raise ValueError('resolution_requirement_invalid') from None


def dynamic_python_pins(descriptor: object) -> str:
    """Machine lock suggestion from checked descriptor data, never a grant/receipt."""
    from agent_hub.skills.sandbox.python_resolution_protocol import decode_descriptor

    checked = decode_descriptor(descriptor)
    return ''.join(f'{wheel.candidate.download.name}=={wheel.candidate.download.version} '
                   f'--hash=sha256:{wheel.candidate.download.sha256}\n' for wheel in checked.wheels)


if TYPE_CHECKING:
    from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlanningRequest
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionPlan, ResolutionStatus


class DynamicResolutionClient(Protocol):
    async def inspect(self, *, needs: tuple[DependencyNeed, ...], environment: dict[str, str],
                      planning: PythonMetadataPlanningRequest,
                      plan: ResolutionPlan | None = None,
                      expected_plan_sha256: str | None = None) -> ResolutionStatus: ...
    async def resolve(self, *, needs: tuple[DependencyNeed, ...], environment: dict[str, str],
                      planning: PythonMetadataPlanningRequest) -> ResolutionStatus: ...
    async def freeze(self, *, needs: tuple[DependencyNeed, ...], environment: dict[str, str],
                     planning: PythonMetadataPlanningRequest) -> ResolutionStatus: ...
    async def publish(self, plan: ResolutionPlan, *, needs: tuple[DependencyNeed, ...],
                      environment: dict[str, str]) -> ResolutionStatus: ...


class DynamicResolutionJournal:
    """Append-only safe data checkpoint in the existing real artifact repository.

    The lookup is a scoped read; get_many still verifies every registered hash.
    No private plan/claim/token is accepted, and no run routing is mutated.
    """
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self.artifacts = artifacts
        self.find = artifacts.find_references

    async def read_artifacts(self, tenant: UUID, run: UUID, *, artifact_type: str, producer: str) -> tuple[Artifact, ...]:
        found: list[Artifact] = []
        after = None
        while True:
            try:
                references = await self.find(tenant, run, type=artifact_type,
                    producer=producer, limit=128, after=after)
            except (ValueError, RuntimeError, OSError):
                raise ValueError('resolution_checkpoint_unavailable') from None
            if (type(references) is not tuple or not all(type(ref) is ArtifactReference for ref in references)
                    or len(references) > 128):
                raise ValueError('resolution_checkpoint_invalid')
            if not references:
                break
            ids = tuple(ref.id for ref in references)
            if tuple(sorted(set(ids))) != ids or after is not None and ids[0] <= after:
                raise ValueError('resolution_checkpoint_invalid')
            found.extend(await self.artifacts.get_many(tenant, run, references))
            if len(references) < 128:
                break
            after = ids[-1]
        return tuple(found)

    async def load(self, scope: Mapping[str, object]) -> dict[str, object] | None:
        tenant, run = UUID(str(scope['tenant_id'])), UUID(str(scope['run_id']))
        found = await self.read_artifacts(tenant, run, artifact_type='python_resolution_checkpoint', producer='python_resolution')
        records: dict[int, dict[str, object]] = {}
        immutable = None
        for artifact in found:
            if artifact.type != 'python_resolution_checkpoint' or artifact.producer != 'python_resolution':
                raise ValueError('resolution_checkpoint_invalid')
            content = json.loads(json.dumps(dict(artifact.content), default=dict))
            if (set(content) != {'schema_version', 'scope', 'needs', 'environment', 'resolution_key',
                                'pending_plan_sha256'} or content['schema_version'] != 1
                    or content['scope'] != dict(scope)):
                raise ValueError('resolution_checkpoint_scope_changed')
            stable = (content['needs'], content['environment'], content['resolution_key'])
            if immutable is not None and stable != immutable:
                raise ValueError('resolution_checkpoint_conflict')
            immutable = stable
            if artifact.version in records and records[artifact.version] != content:
                raise ValueError('resolution_checkpoint_conflict')
            records[artifact.version] = content
        return records[max(records)] if records else None

    async def save(self, scope: Mapping[str, object], *, needs: tuple[DependencyNeed, ...],
                   environment: dict[str, str], resolution_key: str,
                   pending_plan_sha256: str | None) -> ArtifactReference:
        from agent_hub.skills.sandbox.python_registry_protocol import (
            require_sha,
            target_environment,
        )

        require_sha(resolution_key)
        if pending_plan_sha256 is not None:
            require_sha(pending_plan_sha256)
        payload = {'schema_version': 1, 'scope': dict(scope),
            'needs': [{'requirement': n.requirement, 'evidence_sha256': n.evidence_sha256} for n in needs],
            'environment': target_environment(environment), 'resolution_key': resolution_key,
            'pending_plan_sha256': pending_plan_sha256}
        previous = await self.load(scope)
        # Re-read the scoped immutable history to choose an append-only version.
        tenant, run = UUID(str(scope['tenant_id'])), UUID(str(scope['run_id']))
        versions = 0
        after = None
        while True:
            refs = await self.find(tenant, run, type='python_resolution_checkpoint',
                                   producer='python_resolution', limit=128, after=after)
            if refs:
                versions = max(versions, *(a.version for a in await self.artifacts.get_many(tenant, run, refs)))
            if len(refs) < 128:
                break
            after = refs[-1].id
        if previous is not None and any(previous[key] != payload[key]
                for key in ('scope', 'needs', 'environment', 'resolution_key')):
            raise ValueError('resolution_checkpoint_conflict')
        version = max(1, versions + int(previous != payload))
        artifact = Artifact(id=uuid5(NAMESPACE_URL, 'python-resolution-checkpoint:' +
            hashlib.sha256(_encoded(payload)).hexdigest() + ':' + str(version)), version=version,
            type='python_resolution_checkpoint', producer='python_resolution', content=_freeze_object(payload,
                name='safe Python resolution checkpoint'))
        reference = ArtifactReference(artifact.id, artifact.content_sha256)
        await self.artifacts.put(tenant, run, artifact)
        if await self.artifacts.get_many(tenant, run, (reference,)) != (artifact,):
            raise ValueError('resolution_checkpoint_unavailable')
        return reference

    async def store_public_data(self, tenant: UUID, run: UUID, data: Mapping[str, JsonValue]) -> ArtifactReference:
        artifact = Artifact(id=uuid5(NAMESPACE_URL, f'python-resolution:{tenant}:{run}:{data["descriptor_sha256"]}'),
            type='python_dependency_resolution', producer='project.inspect', content=data)
        reference = ArtifactReference(artifact.id, artifact.content_sha256)
        await self.artifacts.put(tenant, run, artifact)
        if await self.artifacts.get_many(tenant, run, (reference,)) != (artifact,):
            raise ValueError('resolution_checkpoint_unavailable')
        return reference

    async def public_data(self, scope: Mapping[str, object]) -> dict[str, JsonValue] | None:
        original = await self.load(scope)
        if original is None:
            return None
        tenant, run = UUID(str(scope['tenant_id'])), UUID(str(scope['run_id']))
        found = await self.read_artifacts(tenant, run, artifact_type='python_dependency_resolution', producer='project.inspect')
        data = None
        for artifact in found:
            content = artifact.content
            if (set(content) != {'kind', 'needs', 'pins', 'provenance', 'descriptor_sha256', 'data_ref'}
                    or content['kind'] != 'python_dependency_resolution_data'
                    or content['data_ref'] != 'python-resolution:' + cast(str, original['resolution_key'])
                    or _encoded(content['needs']) != _encoded(original['needs'])):
                raise ValueError('resolution_checkpoint_conflict')
            if data is not None and data != dict(content):
                raise ValueError('resolution_checkpoint_conflict')
            data = dict(content)
        return data


class DynamicPythonResolutionFlow:
    """Original data survives draft edits; fresh authority always comes from Root.

    A pending publication retains its exact plan and is inspect-only on reentry.
    This object grants nothing, installs nothing and creates no timeout/budget.
    """
    def __init__(self, client: DynamicResolutionClient, *,
                 checkpoint: Callable[[str, str | None], Awaitable[None]] | None = None) -> None:
        self.client = client
        self.checkpoint = checkpoint
        self.resolution_key: str | None = None
        self.pending_sha256: str | None = None
        self.recovered = False
        self.needs: tuple[DependencyNeed, ...] | None = None
        self.environment: dict[str, str] | None = None
        self.pending: ResolutionPlan | None = None
        self.descriptor: dict[str, object] | None = None
        self._data_status: ResolutionStatus | None = None

    def restore(self, checkpoint: Mapping[str, object]) -> None:
        from agent_hub.skills.sandbox.python_registry_protocol import (
            require_sha,
            target_environment,
        )

        rows = checkpoint['needs']
        if type(rows) is not list or not rows:
            raise ValueError('resolution_checkpoint_invalid')
        self.needs = tuple(DependencyNeed(row['requirement'], row['evidence_sha256']) for row in rows)
        self.environment = target_environment(cast(dict[str, str], checkpoint['environment']))
        require_sha(cast(str, checkpoint['resolution_key']))
        self.resolution_key = cast(str, checkpoint['resolution_key'])
        pending = checkpoint['pending_plan_sha256']
        if pending is not None:
            require_sha(cast(str, pending))
        self.pending_sha256 = cast(str | None, pending)
        self.recovered = True

    async def _save(self, key: str, pending: str | None) -> None:
        if self.resolution_key is not None and key != self.resolution_key:
            raise ValueError('resolution_data_changed')
        self.resolution_key = key
        if self.checkpoint is not None:
            await self.checkpoint(key, pending)

    def _data_inputs(self, needs: tuple[DependencyNeed, ...], environment: dict[str, str]) -> dict[str, str]:
        from packaging.utils import canonicalize_name
        from packaging.version import Version

        from agent_hub.skills.sandbox.python_registry_protocol import target_environment
        from agent_hub.skills.sandbox.python_resolution_protocol import decode_descriptor

        environment = target_environment(environment)
        if self.needs is None:
            self.needs, self.environment = needs, dict(environment)
        if environment != self.environment:
            raise ValueError('resolution_environment_changed')
        if needs != self.needs:
            if self.descriptor is None:
                raise ValueError('resolution_inspect_only')
            checked = decode_descriptor(self.descriptor)
            wheels = {w.candidate.download.name: w.candidate.download for w in checked.wheels}
            for entry in needs:
                parsed = requirement(entry.requirement)
                wheel = wheels.get(canonicalize_name(parsed.name))
                if wheel is None or Version(wheel.version) not in parsed.specifier:
                    raise ValueError('resolution_declaration_changed')
        return environment

    async def resolve_data(self, *, needs: tuple[DependencyNeed, ...], environment: dict[str, str],
                           planning: PythonMetadataPlanningRequest) -> ResolutionStatus:
        from agent_hub.skills.sandbox.python_resolution_protocol import decode_descriptor

        environment = self._data_inputs(self.needs if self.recovered and self.descriptor is None and self.needs else needs, environment)
        assert self.needs is not None
        if self.pending_sha256 is not None:
            inspected = await self.client.inspect(needs=self.needs, environment=environment, planning=planning,
                                                  expected_plan_sha256=self.pending_sha256)
            if self.resolution_key is not None and inspected.key != self.resolution_key:
                raise ValueError('resolution_data_changed')
            if inspected.state not in {'frozen', 'published'}:
                raise ValueError('resolution_inspect_only')
            self.pending_sha256 = None
        status = await self.client.inspect(needs=self.needs, environment=environment, planning=planning)
        if self.resolution_key is not None and status.key != self.resolution_key:
            raise ValueError('resolution_data_changed')
        if status.state == 'absent':
            # Root proves no resolution intent exists. A cold approval wait may
            # enter the original action only through its current exact grant.
            # UNKNOWN and any pending publication above never reach this branch.
            status = await self.client.resolve(needs=self.needs, environment=environment, planning=planning)
        if status.state != 'resolved' or status.descriptor is None:
            raise ValueError('resolution_inspect_only')
        descriptor = decode_descriptor(status.descriptor)
        if descriptor.needs != self.needs or dict(descriptor.environment) != environment:
            raise ValueError('resolution_data_changed')
        self.descriptor = descriptor.to_payload()
        self._data_status = status
        self._data_inputs(needs, environment)
        await self._save(status.key, None)
        return status

    def public_data(self) -> dict[str, JsonValue]:
        if self._data_status is None:
            raise ValueError('resolution_data_unavailable')
        return dynamic_python_public_data(self._data_status)

    async def prepare(self, *, needs: tuple[DependencyNeed, ...], environment: dict[str, str],
                      planning: PythonMetadataPlanningRequest) -> ResolutionStatus:
        if self.recovered and self.descriptor is None:
            await self.resolve_data(needs=needs, environment=environment, planning=planning)
        environment = self._data_inputs(needs, environment)
        assert self.needs is not None
        if self.pending is not None:
            status = await self.client.inspect(needs=self.needs, environment=environment,
                                               planning=planning, plan=self.pending)
            if status.state == 'frozen':
                # Root confirms no publication intent/effect exists. Rechecking the
                # same exact grant is safe; UNKNOWN below is never replayed.
                status = await self.client.publish(self.pending, needs=self.needs, environment=environment)
            if status.state != 'published' or status.plan != self.pending:
                raise ValueError('resolution_inspect_only')
            self.pending = None
            self.pending_sha256 = None
            await self._save(status.key, None)
            return status
        await self.resolve_data(needs=needs, environment=environment, planning=planning)
        status = await self.client.freeze(needs=self.needs, environment=environment, planning=planning)
        if status.state != 'frozen' or status.plan is None:
            raise ValueError('resolution_inspect_only')
        self.pending = status.plan
        self.pending_sha256 = self.pending.sha256
        await self._save(status.key, self.pending.sha256)
        observed = await self.client.inspect(needs=self.needs, environment=environment,
                                             planning=planning, plan=self.pending)
        if observed.state == 'published':
            self.pending = None
            self.pending_sha256 = None
            await self._save(observed.key, None)
            return observed
        if observed.state != 'frozen':
            raise ValueError('resolution_inspect_only')
        result = await self.client.publish(self.pending, needs=self.needs, environment=environment)
        if result.state != 'published' or result.plan != self.pending:
            raise ValueError('resolution_inspect_only')
        self.pending = None
        self.pending_sha256 = None
        await self._save(result.key, None)
        return result


def dynamic_python_public_data(status: ResolutionStatus) -> dict[str, JsonValue]:
    """Whitelist data only. The private claim/plan must never cross this boundary.

    data_ref is an opaque Root data reference, NOT a registered file artifact.
    The actual tool owner must register its own structured artifact before linking it.
    """
    from agent_hub.skills.sandbox.python_resolution_protocol import (
        ResolutionStatus,
        decode_descriptor,
    )

    if type(status) is not ResolutionStatus or status.descriptor is None:
        raise ValueError('resolution_data_unavailable')
    checked = decode_descriptor(status.descriptor)
    pins: list[JsonValue] = []
    provenance: list[JsonValue] = []
    for wheel in checked.wheels:
        entry = wheel.candidate.download
        pins.append({'name': entry.name, 'version': entry.version, 'filename': entry.filename,
                     'sha256': entry.sha256, 'size_bytes': entry.size_bytes})
        candidate = wheel.candidate
        provenance.append({'name': entry.name, 'metadata_url': candidate.provenance.url,
                           'metadata_sha256': candidate.provenance.sha256,
                           'index_url': candidate.index_provenance.url,
                           'index_sha256': candidate.index_provenance.sha256})
    return {'kind': 'python_dependency_resolution_data',
            'needs': tuple({'requirement': n.requirement, 'evidence_sha256': n.evidence_sha256} for n in checked.needs),
            'pins': tuple(pins), 'provenance': tuple(provenance), 'descriptor_sha256': checked.sha256,
            'data_ref': 'python-resolution:' + status.key}


def validate_dynamic_python_controls(manifest: bytes, lock: bytes, descriptor: object) -> None:
    """Current exact declaration and hashed closure, before any new effect freeze."""
    from agent_hub.capabilities.python_dependency_closure import check_python_dependency_closure
    from agent_hub.skills.sandbox.python_resolution_protocol import decode_descriptor

    try:
        current = parse_python_project_declaration(manifest, lock,
            max_control_bytes=256 * 1024, max_requirements=128)
        verified = decode_descriptor(descriptor)
        closure = check_python_dependency_closure(current.requirements,
            tuple(w.metadata for w in verified.wheels), dict(verified.environment))
        if closure.status != 'complete':
            raise ValueError('resolution_declaration_changed')
    except ValueError:
        raise ValueError('resolution_declaration_changed') from None
