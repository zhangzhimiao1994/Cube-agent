"""Original-only SDK preparation; supply receipts never authorize project execution.

T3 must inject a DB transaction/lease-bound CAS accessor and a separate exact
approval callback. This module neither registers a public tool nor runs Go.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Protocol, cast, get_args
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.go_project_recipe import (
    GoProjectDeclaration,
    parse_go_project_declaration,
)
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import ManagedProjectInput, ProjectCheckContract
from agent_hub.capabilities.project_stack_registry import detect_stacks
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainSupplyPlan
from agent_hub.skills.sandbox.toolchain_protocol import (
    ToolchainError,
    ToolchainInvocation,
    ToolchainReference,
    ToolchainRequest,
    ToolchainResponse,
    ToolchainSupplyReceipt,
    ToolchainSupplyStatus,
    require_plan_scope,
)

SUPPLY_KEY = 'toolchain_supply'
SUPPLY_PREFIX = 'workspace/current/project-inputs/toolchain/'
_SCOPE = 'toolchain_scope_changed'
_INVALID = 'toolchain_request_invalid'
_ROLES = {Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR}


class ToolchainSupplyClient(Protocol):
    async def plan(self, invocation: ToolchainInvocation) -> ToolchainResponse: ...

    async def supply(self, invocation: ToolchainInvocation, *,
                     plan: ToolchainSupplyPlan) -> ToolchainSupplyReceipt: ...

    async def inspect(self, invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan,
                      supply_execution_id: str) -> ToolchainSupplyStatus: ...

    async def profile(self, invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan,
                      expected_environment: ToolchainReference) -> ToolchainReference: ...


class ToolchainSupplyStateAccess(Protocol):
    """Read live DB state; replace atomically iff expected matches, else raise.

    Every call must recheck the active worker lease, user/role, original capture,
    scope and permissions. A write must commit before returning, preserve other
    routing keys, and return the exact persisted replacement. Files are not DB.
    """

    async def __call__(self, *, expected: Mapping[str, JsonValue] | None = None,
                       replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None: ...


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _reference(value: object, plan: ToolchainSupplyPlan) -> ProjectBaselineReference:
    ref = ProjectBaselineReference.from_payload(value)
    if (ref.input_source_sha256 != plan.scope.source_sha256
            or ref.contract_sha256 != plan.scope.contract_sha256):
        raise ValueError(_SCOPE)
    return ref


def toolchain_state_plan(value: object) -> ToolchainSupplyPlan:
    """Decode exact durable supply phases without accepting capability flags."""
    try:
        if not isinstance(value, Mapping) or len(_encoded(value)) > 512 * 1024:
            raise ValueError
        phase = value.get('phase')
        keys = {'schema_version', 'phase', 'plan', 'plan_sha256', 'original_reference'}
        if phase in ('executing', 'supplied'):
            keys.add('execution_id')
            execution = value.get('execution_id')
            if type(execution) is not str or re.fullmatch(r'toolchain_supply_[a-f0-9]{32}', execution) is None:
                raise ValueError
        if phase == 'supplied':
            keys.add('receipt')
        if (set(value) != keys or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or phase not in ('planned', 'executing', 'supplied')):
            raise ValueError
        plan = ToolchainSupplyPlan.from_json(_encoded(value['plan']))
        if plan.sha256 != value['plan_sha256']:
            raise ValueError
        _reference(value['original_reference'], plan)
        if phase == 'supplied':
            receipt = ToolchainSupplyReceipt.model_validate_json(_encoded(value['receipt']), strict=True)
            if receipt.plan.sha256 != plan.sha256 or receipt.supply_execution_id != value['execution_id']:
                raise ValueError
        return plan
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValueError(_INVALID) from None


def toolchain_supply_request(
    plan: ToolchainSupplyPlan, *, original_reference: ProjectBaselineReference,
    user_id: UUID, idempotency_key: str,
) -> CapabilityRequest:
    if (type(plan) is not ToolchainSupplyPlan or type(user_id) is not UUID
            or type(original_reference) is not ProjectBaselineReference):
        raise ValueError(_SCOPE)
    plan = ToolchainSupplyPlan.from_json(plan.to_json())
    ref = _reference(original_reference.to_payload(), plan)
    scope = plan.scope
    if (scope.user_id != user_id or scope.actor_id != 'main_agent'
            or scope.actor_role not in {role.value for role in _ROLES}):
        raise ValueError(_SCOPE)
    return CapabilityRequest(tenant_id=scope.tenant_id, run_id=scope.run_id, user_id=user_id,
        agent_id='main_agent', capability='command', operation='run',
        resource=SUPPLY_PREFIX + plan.sha256, idempotency_key=idempotency_key,
        arguments={'schema_version': 1, 'plan_sha256': plan.sha256,
            'original_reference': ref.to_payload(), 'project_id': scope.project_id,
            'workspace_session_id': scope.session_id, 'actor_id': scope.actor_id,
            'actor_role': scope.actor_role, 'source_sha256': scope.source_sha256,
            'contract_sha256': scope.contract_sha256, 'catalog_sha256': plan.catalog_sha256,
            'declaration_sha256': plan.declaration_sha256,
            'artifact_sha256': plan.artifact.artifact_sha256, 'archive_sha256': plan.artifact.sha256})


def toolchain_supply_binding(record: object, *, planned: bool = False) -> CapabilityRequest:
    """Bind persisted original capture and SDK plan, never Python/generated authority."""
    try:
        route = getattr(record, 'routing_decision', None)
        if not isinstance(route, Mapping) or {
            'generated_project_contract_version', 'generated_project_preparation', 'python_input_supply',
        } & route.keys():
            raise ValueError
        state = route.get(SUPPLY_KEY)
        plan = toolchain_state_plan(state)
        assert isinstance(state, Mapping)
        tenant, run, user = (getattr(record, name, None) for name in ('tenant_id', 'id', 'actor_id'))
        role = getattr(record, 'actor_role', None)
        if (not all(isinstance(item, UUID) for item in (tenant, run, user))
                or not isinstance(role, str) or Role(role) not in _ROLES
                or (plan.scope.tenant_id, plan.scope.run_id, plan.scope.user_id, plan.scope.actor_role)
                    != (tenant, run, user, role)
                or plan.scope.actor_id != 'main_agent'
                or planned and state['phase'] != 'planned'):
            raise ValueError
        project, session = authorized_workspace_scope(record, tenant_id=cast(UUID, tenant),
                                                       run_id=cast(UUID, run))
        preparation = route.get('project_input_preparation')
        permissions = route.get('requested_permissions')
        if (type(route.get('project_input_contract_version')) is not int
                or route['project_input_contract_version'] != 1
                or route.get('sandbox_profile') != 'workspace_write'
                or route.get('execution_backend', 'systemd') != 'systemd'
                or not isinstance(permissions, (tuple, list))
                or not {'workspace.read', 'workspace.write', 'command.run'}.issubset(permissions)
                or (project, session) != (plan.scope.project_id, plan.scope.session_id)
                or not isinstance(preparation, Mapping)
                or set(preparation) != {'schema_version', 'phase', 'reference'}
                or type(preparation.get('schema_version')) is not int
                or preparation['schema_version'] != 1 or preparation['phase'] != 'captured'
                or preparation['reference'] != state['original_reference']):
            raise ValueError
        ManagedProjectInput.from_payload(route.get('project_input'))
        ref = _reference(preparation['reference'], plan)
        return toolchain_supply_request(plan, original_reference=ref, user_id=UUID(str(user)),
                                        idempotency_key='toolchain-supply-binding')
    except (ValueError, TypeError, RuntimeError, LookupError):
        raise ValueError(_SCOPE) from None


def _captured_declaration(invocation: ToolchainInvocation, stored: StoredProjectBaseline) -> GoProjectDeclaration:
    try:
        if type(stored) is not StoredProjectBaseline or stored.generated_metadata is not None:
            raise ValueError
        replace(stored)
        contract = ProjectCheckContract.from_payload(stored.contract.to_payload())
        ref = ProjectBaselineReference.from_payload(invocation.original_reference.model_dump(mode='json'))
        scope = invocation.scope
        # The reference hashes baseline.json, not the inner protected contract.
        envelope_sha256 = hashlib.sha256(_encoded({'schema_version': 1,
            'contract': contract.to_payload(), 'source_files': dict(stored.source_files)})).hexdigest()
        if (stored.reference != ref or envelope_sha256 != ref.contract_sha256
                or (contract.tenant_id, contract.run_id, contract.project_id, contract.session_id,
                    contract.input_source_sha256) != (scope.tenant_id, scope.run_id,
                        scope.project_id, scope.session_id, scope.source_sha256)
                or scope.actor_id != 'main_agent' or Role(scope.actor_role) not in _ROLES):
            raise ValueError
        contract.require_current(stored.source_files)
        if detect_stacks(tuple(stored.source_files)) != ('go',):
            raise ValueError('toolchain_unsupported')
        controls: dict[str, bytes] = {}
        remaining = 65536
        for name in ('go.mod', 'go.sum'):
            if name not in stored.source_files:
                continue
            raw = _read_no_links(stored.snapshot, (name,), max_bytes=remaining)
            remaining -= len(raw)
            expected = stored.source_files[name]
            if (remaining < 0 or contract.protected_files.get(name) != expected
                    or expected not in {hashlib.sha256(tag + raw).hexdigest() for tag in (b'f\0', b'x\0')}):
                raise ValueError
            controls[name] = raw
        return parse_go_project_declaration(controls)
    except (ValueError, TypeError, RuntimeError, OSError) as error:
        raise ValueError('toolchain_unsupported' if str(error) == 'toolchain_unsupported' else _SCOPE) from None


def validate_toolchain_declaration(
    plan: ToolchainSupplyPlan, invocation: ToolchainInvocation, stored: StoredProjectBaseline,
) -> None:
    require_plan_scope(plan, invocation)
    declaration = _captured_declaration(invocation, stored)
    minimum = tuple(int(part) for part in declaration.go_version.split('.'))
    minimum += (0,) * (3 - len(minimum))
    if (plan.declaration_sha256 != declaration.sha256
            or tuple(int(part) for part in plan.artifact.version.split('.')) < minimum
            or declaration.toolchain not in {None, 'default', 'go' + plan.artifact.version}):
        raise ValueError('toolchain_plan_changed')


async def _root_io[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except TimeoutError:
        raise ValueError('toolchain_timeout') from None
    except (OSError, ValueError) as error:
        code = str(error)
        raise ValueError(code if code in get_args(ToolchainError) else 'toolchain_unavailable') from None


async def supply_project_toolchain(
    *, client: ToolchainSupplyClient, invocation: ToolchainInvocation, stored: StoredProjectBaseline,
    state_io: ToolchainSupplyStateAccess,
    authorize: Callable[[ToolchainSupplyPlan, Mapping[str, JsonValue]], Awaitable[None]],
    deadline: float,
) -> Mapping[str, JsonValue]:
    """CAS intent before supply; recover inspect-only; always profile after supplied.

    The caller owns live DB authorization. The returned state is supply evidence,
    not a recipe, execution approval, runtime lease, or supported-stack flag.
    """
    if type(invocation) is not ToolchainInvocation:
        raise ValueError(_INVALID)
    invocation = ToolchainInvocation.model_validate_json(invocation.model_dump_json(), strict=True)
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError('toolchain_timeout')
    _captured_declaration(invocation, stored)

    def fresh(prefix: str) -> ToolchainInvocation:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ValueError('toolchain_timeout')
        return ToolchainInvocation.model_validate({**invocation.model_dump(),
            'execution_id': prefix + uuid4().hex,
            'timeout_seconds': min(invocation.timeout_seconds, max(1, int(remaining)))}, strict=True)

    async def current(expected: Mapping[str, JsonValue] | None) -> None:
        if await state_io() != expected:
            raise ValueError(_SCOPE)

    async def persist(expected: Mapping[str, JsonValue] | None,
                      replacement: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        replacement = _freeze_object(replacement, name='toolchain supply state')
        toolchain_state_plan(replacement)
        result = await state_io(expected=expected, replace=replacement)
        if result != replacement:
            raise ValueError(_SCOPE)
        await current(replacement)
        return replacement

    async def measure() -> ToolchainSupplyPlan:
        await current(state)
        probe = fresh('toolchain_plan_')
        request = ToolchainRequest(action='plan_project_toolchain', invocation=probe)
        response = await _root_io(client.plan(probe))
        await current(state)
        if type(response) is not ToolchainResponse:
            raise ValueError(_INVALID)
        response = ToolchainResponse.model_validate_json(response.model_dump_json(), strict=True)
        response.require_for(request)
        if not response.ok or response.plan is None:
            raise ValueError(response.error or 'toolchain_unavailable')
        validate_toolchain_declaration(response.plan, probe, stored)
        return response.plan

    state = await state_io()
    if state is None:
        plan = await measure()
        state = await persist(None, {'schema_version': 1, 'phase': 'planned',
            'plan': cast(Mapping[str, JsonValue], plan.to_payload()), 'plan_sha256': plan.sha256,
            'original_reference': cast(Mapping[str, JsonValue], stored.reference.to_payload())})
    else:
        state = _freeze_object(state, name='toolchain supply state')
        plan = toolchain_state_plan(state)
        validate_toolchain_declaration(plan, invocation, stored)
        if state['original_reference'] != _freeze_object(stored.reference.to_payload(), name='original reference'):
            raise ValueError(_SCOPE)
        if state['phase'] == 'planned' and (await measure()).sha256 != plan.sha256:
            raise ValueError('toolchain_plan_changed')
    if state['phase'] == 'planned':
        await current(state)
        await authorize(plan, state)
        await current(state)
        execution = fresh('toolchain_supply_')
        state = await persist(state, {**state, 'phase': 'executing', 'execution_id': execution.execution_id})
        receipt = await _root_io(client.supply(execution, plan=plan))
        await current(state)
    elif state['phase'] == 'executing':
        await current(state)
        execution_id = cast(str, state['execution_id'])
        status = await _root_io(client.inspect(fresh('toolchain_inspect_'), plan=plan,
                                              supply_execution_id=execution_id))
        await current(state)
        if type(status) is not ToolchainSupplyStatus:
            raise ValueError(_INVALID)
        status = ToolchainSupplyStatus.model_validate_json(status.model_dump_json(), strict=True)
        if status.plan_sha256 != plan.sha256 or status.supply_execution_id != execution_id:
            raise ValueError(_SCOPE)
        if status.state != 'supplied' or status.receipt is None:
            raise ValueError('toolchain_unknown')
        receipt = status.receipt
    else:
        receipt = ToolchainSupplyReceipt.model_validate_json(_encoded(state['receipt']), strict=True)
    if type(receipt) is not ToolchainSupplyReceipt:
        raise ValueError(_INVALID)
    receipt = ToolchainSupplyReceipt.model_validate_json(receipt.model_dump_json(), strict=True)
    require_plan_scope(receipt.plan, invocation)
    if receipt.plan.sha256 != plan.sha256 or receipt.supply_execution_id != state['execution_id']:
        raise ValueError('toolchain_plan_changed')
    if state['phase'] != 'supplied':
        state = await persist(state, {**state, 'phase': 'supplied',
            'receipt': cast(Mapping[str, JsonValue], receipt.model_dump(mode='json'))})
    await current(state)
    environment = await _root_io(client.profile(fresh('toolchain_profile_'), plan=plan,
                                               expected_environment=receipt.environment))
    await current(state)
    if type(environment) is not ToolchainReference:
        raise ValueError(_INVALID)
    environment = ToolchainReference.model_validate_json(environment.model_dump_json(), strict=True)
    environment.require_plan(plan)
    if environment != receipt.environment:
        raise ValueError('toolchain_plan_changed')
    return state
