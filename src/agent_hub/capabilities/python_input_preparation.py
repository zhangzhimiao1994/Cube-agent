"""Trusted same-run input-supply metadata; publication is never installation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast, get_args
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
    SupplyError,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation

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
) -> None:
    """Bind Root plan to immutable captured bytes, not model/prompt hashes."""
    plan.require_scope(invocation)
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
    deadline: float,
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
        validate_supply_declaration(response.plan, probe, stored)
        return response.plan

    state = await state_io()
    if state is None:
        plan = await measure()
        state = await state_io(replace=_freeze_object({'schema_version': 1, 'phase': 'planned',
            'plan': plan.model_dump(mode='json'), 'plan_sha256': plan.sha256}, name='input supply state'))
        assert state is not None
    else:
        plan = supply_state_plan(state)
        validate_supply_declaration(plan, invocation, stored)
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
