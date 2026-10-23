"""Private original-only Go admission data; no dispatch or native execution authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from agent_hub.capabilities.go_project_inputs import GoProjectInputs
from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.toolchain_preparation import (
    toolchain_state_plan,
    toolchain_supply_binding,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.go_project_protocol import GoProjectInvocation
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainReference, ToolchainSupplyReceipt

RECIPE_KEY = 'go_project_recipe'
EXECUTION_KEY = 'go_project_execution'
RECIPE_PREFIX = 'workspace/current/project-verification/go/'
INVALID = 'go_project_recipe_changed'
APPROVAL_REQUIRED = 'go_project_recipe_approval_required'


def encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def is_go_recipe_scope(value: object) -> bool:
    # Include malformed suffixes in the private namespace so generic paths fail closed.
    return isinstance(value, str) and ':resource=workspace/current/project-verification/go' in value


def go_recipe_marker(inputs: GoProjectInputs, *, supply_plan_sha256: str) -> dict[str, object]:
    if type(inputs) is not GoProjectInputs:
        raise ValueError(INVALID)
    inputs = GoProjectInputs.from_payload(inputs.to_payload())
    return {'schema_version': 1, 'binding': inputs.approval_request(user_id=inputs.scope.user_id,
        agent_id='main_agent', idempotency_key='go-recipe-binding').arguments,
        'toolchain': inputs.toolchain.model_dump(mode='json'), 'supply_plan_sha256': supply_plan_sha256,
        'source_limit_bytes': inputs.source_limit_bytes, 'file_limit': inputs.file_limit}


def go_recipe_binding(record: object, *, idempotency_key: str = 'go-recipe-binding') -> CapabilityRequest:
    """Revalidate the compact persisted binding against the preserved capture and supplied SDK."""
    try:
        route = getattr(record, 'routing_decision', None)
        if not isinstance(route, Mapping):
            raise TypeError(INVALID)
        supply_request = toolchain_supply_binding(record)
        supply = route['toolchain_supply']
        plan = toolchain_state_plan(supply)
        if supply['phase'] != 'supplied':
            raise ValueError(INVALID)
        receipt = ToolchainSupplyReceipt.model_validate_json(encoded(supply['receipt']), strict=True)
        marker = route[RECIPE_KEY]
        if (not isinstance(marker, Mapping) or set(marker) != {'schema_version', 'binding', 'toolchain',
                'supply_plan_sha256', 'source_limit_bytes', 'file_limit'}
                or type(marker['schema_version']) is not int or marker['schema_version'] != 1
                or marker['supply_plan_sha256'] != plan.sha256
                or type(marker['source_limit_bytes']) is not int
                or not 1 <= marker['source_limit_bytes'] <= 500 * 1024**2
                or type(marker['file_limit']) is not int or not 1 <= marker['file_limit'] <= 100000):
            raise ValueError(INVALID)
        sdk = ToolchainReference.model_validate_json(encoded(marker['toolchain']), strict=True)
        if encoded(sdk.model_dump(mode='json')) != encoded(receipt.environment.model_dump(mode='json')):
            raise ValueError(INVALID)
        binding = marker['binding']
        if (not isinstance(binding, Mapping) or set(binding) != {'schema_version', 'kind', 'recipe_sha256',
                'environment_input_sha256', 'scope', 'original_reference', 'original_check_contract',
                'toolchain_sha256'} or type(binding['schema_version']) is not int
                or (binding['schema_version'], binding['kind']) not in {
                    (1, 'go_stdlib_cli_v1'), (2, 'go_stdlib_cli_business_v1')}):
            raise ValueError(INVALID)
        for key in ('recipe_sha256', 'environment_input_sha256', 'original_check_contract', 'toolchain_sha256'):
            if type(binding[key]) is not str or re.fullmatch(r'[a-f0-9]{64}', binding[key]) is None:
                raise ValueError(INVALID)
        scope = ToolchainPlanScope.from_payload(binding['scope'])
        ref = ProjectBaselineReference.from_payload(binding['original_reference'])
        environment = {'schema_version': 1, 'kind': 'go_stdlib_cli_v1', 'toolchain': marker['toolchain'],
                       'dependency_policy': 'stdlib_only', 'external_dependencies': [],
                       'build_target': '.', 'test_target': './...'}
        if (encoded(scope.to_payload()) != encoded(plan.scope.to_payload())
                or encoded(ref.to_payload()) != encoded(supply['original_reference'])
                or encoded(binding['original_reference']) != encoded(supply['original_reference'])
                or binding['toolchain_sha256'] != _digest(marker['toolchain'])
                or binding['environment_input_sha256'] != _digest(environment)):
            raise ValueError(INVALID)
        return CapabilityRequest(tenant_id=supply_request.tenant_id, user_id=supply_request.user_id,
            run_id=supply_request.run_id, agent_id='main_agent', capability='command', operation='run',
            resource=RECIPE_PREFIX + binding['recipe_sha256'], idempotency_key=idempotency_key,
            arguments=dict(binding))
    except (ValueError, TypeError, RuntimeError, LookupError, OverflowError, RecursionError):
        raise ValueError(INVALID) from None


def require_recipe_request(record: object, request: CapabilityRequest) -> CapabilityRequest:
    expected = go_recipe_binding(record, idempotency_key=request.idempotency_key)
    if encoded(request.model_dump(mode='json')) != encoded(expected.model_dump(mode='json')):
        raise ValueError(INVALID)
    return expected


def require_recipe_marker(record: object, expected: Mapping[str, object] | None) -> None:
    route = getattr(record, 'routing_decision', None)
    if expected is not None and (not isinstance(route, Mapping)
                                or encoded(route[RECIPE_KEY]) != encoded(expected)):
        raise ValueError(INVALID)


def go_reservation_state(record: object, invocation: GoProjectInvocation) -> dict[str, object]:
    if type(invocation) is not GoProjectInvocation:
        raise ValueError(INVALID)
    invocation = GoProjectInvocation.model_validate_json(invocation.model_dump_json(), strict=True)
    request = go_recipe_binding(record)
    route = getattr(record, 'routing_decision', None)
    assert isinstance(route, Mapping)
    marker = route[RECIPE_KEY]
    scope = request.arguments['scope']
    assert isinstance(scope, Mapping)
    if (invocation.source_kind != 'original' or invocation.execution_id != 'go_original_' + request.run_id.hex
            or invocation.tenant_id != request.tenant_id or invocation.run_id != request.run_id
            or invocation.project_id != scope['project_id'] or invocation.session_id != scope['session_id']
            or encoded(invocation.scope.to_payload()) != encoded(scope)
            or encoded(invocation.original_reference.model_dump(mode='json'))
                != encoded(request.arguments['original_reference'])
            or invocation.recipe_sha256 != request.arguments['recipe_sha256']
            or invocation.environment_input_sha256 != request.arguments['environment_input_sha256']
            or encoded(invocation.toolchain.model_dump(mode='json')) != encoded(marker['toolchain'])
            or invocation.source_limit_bytes != marker['source_limit_bytes']
            or invocation.file_limit != marker['file_limit']):
        raise ValueError(INVALID)
    worker = getattr(record, 'worker_id', None)
    token = getattr(record, 'worker_lease_token', None)
    run_request = getattr(record, 'request', None)
    mode = getattr(record, 'mode', None)
    if (type(worker) is not str or not worker.strip() or not isinstance(token, UUID)
            or type(run_request) is not str or not run_request.strip() or not isinstance(mode, str)):
        raise ValueError(INVALID)
    # Bind the intent's creator, not the heartbeat schedule. Only the digest
    # crosses this boundary; a later worker cannot adopt this intent as its own.
    lease_binding = {'schema_version': 1, 'kind': 'go_project_execution_lease_v1',
        'tenant_id': str(request.tenant_id), 'run_id': str(request.run_id), 'user_id': str(request.user_id),
        'actor_role': scope['actor_role'], 'worker_id': worker, 'lease_token': str(token),
        'mode': TaskMode(mode).value,
        'request_sha256': _digest(run_request), 'capture_sha256': _digest(route['project_input_preparation'])}
    return {'schema_version': 1, 'phase': 'reserved', 'source_kind': 'original',
            'execution_id': invocation.execution_id, 'recipe_sha256': invocation.recipe_sha256,
            'environment_input_sha256': invocation.environment_input_sha256,
            'source_sha256': scope['source_sha256'], 'invocation_sha256': _digest(invocation.model_dump(mode='json')),
            'lease_binding_sha256': _digest(lease_binding)}


@dataclass(frozen=True, slots=True)
class GoExecutionReservation:
    """Committed intent metadata only, including on first creation; never a dispatch permit."""

    state: Mapping[str, JsonValue]
    disposition: Literal['created', 'existing']

    def __post_init__(self) -> None:
        object.__setattr__(self, 'state', _freeze_object(cast(Mapping[str, object], self.state),
                                                       name='Go execution reservation'))
