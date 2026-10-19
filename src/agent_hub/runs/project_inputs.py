"""Pre-model original-project capture and once-only native baseline observation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.project_verification import (
    ProjectVerificationService,
    validated_original_observation,
)
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.domain.runs import RunStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runs.project_source_context import original_source_context
from agent_hub.runs.repository import RunConflict, RunRepository
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation

_FAILURE = 'original project preparation is unavailable or changed'
_SCOPE_KEYS = ('project_id', 'workspace_session_id', 'sandbox_profile', 'requested_permissions',
               'execution_backend', 'project_input_contract_version', 'project_input')


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


class OriginalProjectPreparer:
    def __init__(self, *, root: Path, repository: RunRepository,
                 authorizer: HarnessToolGateway, verifier: ProjectVerificationService) -> None:
        self.root, self.repository = root, repository
        self.authorizer, self.verifier = authorizer, verifier

    async def _state(
        self, context: TaskContext, *, expected: Mapping[str, JsonValue] | None = None,
        replace: Mapping[str, JsonValue] | None = None,
    ) -> Mapping[str, JsonValue] | None:
        async with await self.repository.run_transaction() as session, session.begin():
            row = await self.repository.get_for_update(session, context.run_id)
            record = RunRepository._record(row)
            if (
                record.tenant_id != context.tenant_id or record.actor_id != context.actor_id
                or record.actor_role is not context.actor_role or not record.worker_id
                or record.request != context.request or record.mode is not context.mode
                or record.status is not RunStatus.RUNNING
                or record.worker_lease_token != context.execution_token
                or record.worker_lease_expires_at is None or record.worker_lease_expires_at <= datetime.now(UTC)
            ):
                raise RunConflict(_FAILURE)
            project, session_id = authorized_workspace_scope(
                record, tenant_id=context.tenant_id, run_id=context.run_id,
            )
            route = _freeze_object(record.routing_decision or {}, name='original project routing')
            if any(route.get(key) != context.routing_decision.get(key) for key in _SCOPE_KEYS):
                raise RunConflict(_FAILURE)
            if (route.get('sandbox_profile') != 'workspace_write'
                    or route.get('execution_backend', 'systemd') != 'systemd'
                    or not isinstance(route.get('requested_permissions'), tuple)
                    or not {'workspace.read', 'workspace.write', 'command.run'}.issubset(
                        cast(tuple[str, ...], route['requested_permissions']))
                    or not project or not session_id):
                raise RuntimeError(_FAILURE)
            state = route.get('project_input_preparation')
            if state is not None and not isinstance(state, Mapping):
                raise RuntimeError(_FAILURE)
            if replace is not None:
                if state != expected:
                    raise RunConflict(_FAILURE)
                replacement = _freeze_object(replace, name='original project preparation')
                row.routing_decision = {
                    **(record.routing_decision or {}),
                    'project_input_preparation': json.loads(_encoded(replacement)),
                }
                row.version += 1
                return replacement
            return state

    async def __call__(self, context: TaskContext) -> TaskContext:
        context = context.validated_internal_clone()
        route = context.routing_decision
        if (type(route.get('project_input_contract_version')) is not int
                or route.get('project_input_contract_version') != 1
                or type(context.execution_token) is not UUID
                or context.actor_id is None or not isinstance(context.actor_role, Role)):
            raise RuntimeError(_FAILURE)
        selection = ManagedProjectInput.from_payload(route.get('project_input'))
        started = asyncio.get_running_loop().time()
        invocation = ProjectInvocation(
            execution_id='original_' + context.run_id.hex, tenant_id=context.tenant_id, run_id=context.run_id,
            project_id=cast(str, route['project_id']), session_id=cast(str, route['workspace_session_id']),
            sandbox_profile='workspace_write', timeout_seconds=max(1, int(context.timeout_seconds)),
        )
        state = await self._state(context)
        store = ProjectBaselineStore(self.root)
        if state is None:
            state = await self._state(context, replace={'schema_version': 1, 'phase': 'reserved'})
            reference = await store.capture_once(invocation, selection)
            state = await self._state(context, expected=state, replace={
                'schema_version': 1, 'phase': 'captured',
                'reference': cast(Mapping[str, JsonValue], reference.to_payload()),
            })
        if (not isinstance(state, Mapping) or type(state.get('schema_version')) is not int
                or state.get('schema_version') != 1 or state.get('phase') not in {'captured', 'observed'}):
            raise RuntimeError(_FAILURE)
        reference = ProjectBaselineReference.from_payload(state.get('reference'))
        stored = await asyncio.to_thread(store.load, invocation, reference)
        if stored.contract.editable_paths != selection.editable_paths:
            raise RuntimeError(_FAILURE)
        if state['phase'] == 'captured':
            request = HarnessToolCallRequest(
                run_id=context.run_id, actor='main_agent', tool_name='project.verify', arguments={},
                approval_required=False, sandbox='workspace_write',
                idempotency_key='original-project-admission-' + context.run_id.hex,
            )
            allowed = await self.authorizer.authorize(
                context.tenant_id, request, user_id=context.actor_id, role=context.actor_role,
            )
            if (allowed.status != 'succeeded' or allowed.truncated
                    or allowed.call_id != request.call_id or allowed.tool_name != request.tool_name
                    or allowed.payload.get('authorized') is not True):
                raise RuntimeError('original project preparation authorization required or denied')
            state = await self._state(context, expected=state, replace={
                **state, 'phase': 'executing', 'execution_id': invocation.execution_id,
            })
            observation = await self.verifier.observe_original(
                tenant_id=context.tenant_id, run_id=context.run_id, actor='main_agent', reference=reference,
                execution_id=invocation.execution_id,
                timeout_seconds=context.timeout_seconds - (asyncio.get_running_loop().time() - started),
            )
            observation = validated_original_observation(
                observation, expected_source=reference.input_source_sha256,
                expected_contract=stored.contract.protected_sha256,
            )
            state = await self._state(context, expected=state, replace={
                **(state or {}), 'phase': 'observed', 'observation': observation,
                'observation_sha256': hashlib.sha256(_encoded(observation)).hexdigest(),
            })
        assert state is not None
        saved_observation = state.get('observation')
        if (not isinstance(saved_observation, Mapping)
                or state.get('observation_sha256') != hashlib.sha256(_encoded(saved_observation)).hexdigest()
                or saved_observation.get('status') not in {'passed', 'failed'}
                or not isinstance(saved_observation.get('verification'), Mapping)
                or cast(Mapping[str, JsonValue], saved_observation['verification']).get('source_sha256')
                    != reference.input_source_sha256):
            raise RuntimeError(_FAILURE)
        saved_observation = validated_original_observation(
            saved_observation, expected_source=reference.input_source_sha256,
            expected_contract=stored.contract.protected_sha256,
        )
        source_context = await asyncio.to_thread(
            original_source_context, stored, byte_budget=min(96_000, max(4096, context.token_budget)),
        )
        artifact = Artifact(
            id=uuid5(NAMESPACE_URL, f'original-project:{context.tenant_id}:{context.run_id}:'
                     f'{state["observation_sha256"]}'),
            type='original_project_observation', producer='original_project_preparer',
            content={'schema_version': 1, 'original_source_sha256': reference.input_source_sha256,
                     'protected_sha256': stored.contract.protected_sha256,
                     'editable_paths': selection.editable_paths, 'observation': saved_observation,
                     'source_context': cast(Mapping[str, JsonValue], source_context),
                     'summary': 'Existing user project. Inspect its source with read_context; repair only '
                                'explicit editable paths. Original checks and configuration must remain unchanged. '
                                'Re-run project.verify after repair, then deliver with workspace.bundle.'})
        return context.model_copy(update={
            'routing_decision': {**route, 'project_input_preparation': state},
            'artifacts': (*context.artifacts, artifact),
        }).validated_internal_clone()
