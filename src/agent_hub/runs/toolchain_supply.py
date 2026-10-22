"""Original-only SDK supply authority and a file-free production CAS adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.capabilities.toolchain_preparation import (
    SUPPLY_KEY,
    toolchain_state_plan,
    toolchain_supply_binding,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object

if TYPE_CHECKING:
    from agent_hub.runs.repository import RunRecord, RunRepository

_SCOPE = 'toolchain_scope_changed'
_STATE = 'toolchain_supply_state_changed'
_BOUND_KEYS = ('project_id', 'workspace_session_id', 'sandbox_profile', 'requested_permissions',
               'execution_backend', 'project_input_contract_version', 'project_input',
               'project_input_preparation')
_MIXED_KEYS = {'python_input_supply', 'generated_project_contract_version', 'generated_project_preparation'}


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _original_binding(route: object, tenant_id: UUID, run_id: UUID) -> Mapping[str, JsonValue]:
    if not isinstance(route, Mapping) or _MIXED_KEYS & route.keys():
        raise ValueError(_SCOPE)
    authorized_workspace_scope(SimpleNamespace(tenant_id=tenant_id, id=run_id, routing_decision=route),
                               tenant_id=tenant_id, run_id=run_id)
    capture = route.get('project_input_preparation')
    permissions = route.get('requested_permissions')
    selection = route.get('project_input')
    if (type(route.get('project_input_contract_version')) is not int
            or route['project_input_contract_version'] != 1
            or route.get('sandbox_profile') != 'workspace_write'
            or route.get('execution_backend', 'systemd') != 'systemd'
            or not isinstance(permissions, (tuple, list))
            or not {'workspace.read', 'workspace.write', 'command.run'}.issubset(permissions)
            or not isinstance(capture, Mapping)
            or set(capture) != {'schema_version', 'phase', 'reference'}
            or type(capture.get('schema_version')) is not int or capture['schema_version'] != 1
            or capture.get('phase') != 'captured'
            or not isinstance(selection, Mapping)
            or set(selection) != {'schema_version', 'kind', 'editable_paths'}):
        raise ValueError(_SCOPE)
    ProjectBaselineReference.from_payload(capture['reference'])
    ManagedProjectInput.from_payload(selection)
    return _freeze_object({key: route[key] for key in _BOUND_KEYS if key in route},
                          name='toolchain original scope')


@dataclass(frozen=True, slots=True)
class ToolchainSupplyAuthority:
    tenant_id: UUID
    run_id: UUID
    actor_id: UUID
    actor_role: Role
    worker_id: str
    lease_token: UUID = field(repr=False)
    request: str
    mode: TaskMode
    binding: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        try:
            if (not all(type(value) is UUID for value in (
                    self.tenant_id, self.run_id, self.actor_id, self.lease_token))
                    or not isinstance(self.actor_role, Role)
                    or self.actor_role not in {Role.OPERATOR, Role.ADMIN, Role.SUPER_ADMIN}
                    or type(self.worker_id) is not str or not self.worker_id.strip()
                    or type(self.request) is not str or not self.request.strip()
                    or not isinstance(self.mode, TaskMode)):
                raise ValueError(_SCOPE)
            binding = _original_binding(self.binding, self.tenant_id, self.run_id)
            if _encoded(binding) != _encoded(self.binding):
                raise ValueError(_SCOPE)
            object.__setattr__(self, 'binding', binding)
        except (ValueError, TypeError, RuntimeError, KeyError):
            raise RunConflict(_SCOPE) from None

    @classmethod
    def from_context(cls, context: TaskContext, *, worker_id: str) -> ToolchainSupplyAuthority:
        try:
            context = context.validated_internal_clone()
            if context.actor_id is None or context.actor_role is None or context.execution_token is None:
                raise ValueError(_SCOPE)
            return cls(context.tenant_id, context.run_id, context.actor_id, context.actor_role,
                       worker_id, context.execution_token, context.request, context.mode,
                       _original_binding(context.routing_decision, context.tenant_id, context.run_id))
        except (ValueError, TypeError, RuntimeError, KeyError):
            raise RunConflict(_SCOPE) from None

    def require_current(self, record: RunRecord, tenant_id: UUID, run_id: UUID) -> None:
        try:
            if ((tenant_id, run_id) != (self.tenant_id, self.run_id)
                    or (record.tenant_id, record.id, record.actor_id, record.actor_role,
                        record.worker_id, record.worker_lease_token, record.request, record.mode)
                    != (self.tenant_id, self.run_id, self.actor_id, self.actor_role,
                        self.worker_id, self.lease_token, self.request, self.mode)
                    or record.status is not RunStatus.RUNNING
                    or record.worker_lease_expires_at is None
                    or record.worker_lease_expires_at <= datetime.now(UTC)
                    or _encoded(_original_binding(record.routing_decision, tenant_id, run_id))
                        != _encoded(self.binding)):
                raise ValueError(_SCOPE)
        except (ValueError, TypeError, RuntimeError, KeyError):
            raise RunConflict(_SCOPE) from None


def supply_state(record: RunRecord) -> Mapping[str, JsonValue] | None:
    route = record.routing_decision
    if route is None:
        raise RunConflict(_SCOPE)
    if SUPPLY_KEY not in route:
        return None
    try:
        state = _freeze_object(route[SUPPLY_KEY], name='toolchain supply state')
        toolchain_state_plan(state)
        toolchain_supply_binding(record)
        return state
    except (ValueError, TypeError, RuntimeError, KeyError):
        raise RunConflict(_STATE) from None


def supply_transition(
    record: RunRecord, expected: Mapping[str, JsonValue] | None,
    replacement: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    try:
        previous = supply_state(record)
        normalized_expected = None if expected is None else _freeze_object(expected, name='expected supply')
        if _encoded(previous) != _encoded(normalized_expected):
            raise ValueError(_STATE)
        after = _freeze_object(replacement, name='replacement supply')
        toolchain_state_plan(after)
        before_phase = None if previous is None else previous['phase']
        if (before_phase, after['phase']) not in {
                (None, 'planned'), ('planned', 'executing'), ('executing', 'supplied')}:
            raise ValueError(_STATE)
        if previous is not None:
            fixed = ('schema_version', 'plan', 'plan_sha256', 'original_reference')
            if (_encoded({key: previous[key] for key in fixed})
                    != _encoded({key: after[key] for key in fixed})
                    or before_phase == 'executing' and after['execution_id'] != previous['execution_id']):
                raise ValueError(_STATE)
        candidate = replace(record, routing_decision={**(record.routing_decision or {}),
            SUPPLY_KEY: json.loads(_encoded(after))})
        toolchain_supply_binding(candidate)
        return after
    except (ValueError, TypeError, RuntimeError, KeyError):
        raise RunConflict(_STATE) from None


class ToolchainSupplyStateAdapter:
    def __init__(self, repository: RunRepository, *, authority: ToolchainSupplyAuthority) -> None:
        self.repository, self.authority = repository, authority

    async def __call__(
        self, *, expected: Mapping[str, JsonValue] | None = None,
        replace: Mapping[str, JsonValue] | None = None,
    ) -> Mapping[str, JsonValue] | None:
        authority = self.authority
        if replace is None:
            if expected is not None:
                raise RunConflict(_STATE)
            return await self.repository.get_toolchain_supply_state(
                authority.tenant_id, authority.run_id, authority=authority)
        record = await self.repository.compare_and_set_toolchain_supply(
            authority.tenant_id, authority.run_id, authority=authority,
            expected=expected, replacement=replace)
        return _freeze_object(cast(Mapping[str, object], record.routing_decision)[SUPPLY_KEY],
                              name='committed toolchain supply')
