"""Pre-model original-project capture and once-only native baseline observation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agent_hub.auth.models import Role
from agent_hub.capabilities.project_baselines import (
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
)
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.project_verification import (
    ProjectVerificationService,
    validated_original_observation,
)
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_service import (
    PythonOriginalContext,
    load_python_original_inputs,
    python_original_invocation,
    python_recipe_marker,
)
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.domain.runs import RunStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.runs.project_source_context import original_source_context
from agent_hub.runs.repository import (
    RunConflict,
    RunRecord,
    RunRepository,
    _pre_model_original_capture,
)
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation

if TYPE_CHECKING:
    from agent_hub.capabilities.python_input_preparation import InputSupplyClient

_FAILURE = 'original project preparation is unavailable or changed'
_SCOPE_KEYS = ('project_id', 'workspace_session_id', 'sandbox_profile', 'requested_permissions',
               'execution_backend', 'project_input_contract_version', 'project_input')


class OriginalProjectApprovalPending(RuntimeError):
    """Captured inputs are durable; no model or project command has started."""

    def __init__(self, approval_id: str, preparation: Mapping[str, JsonValue], *,
                 supply_binding: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__('original project preparation authorization pending')
        if re.fullmatch(r'approval_[a-zA-Z0-9_-]{1,100}', approval_id) is None:
            raise ValueError(_FAILURE)
        self.approval_id = approval_id
        self.preparation = _freeze_object(preparation, name='original project approval capture')
        self.supply_binding = None if supply_binding is None else _freeze_object(
            supply_binding, name='original input supply approval')

    def matches(self, record: RunRecord, context: TaskContext) -> bool:
        route = record.routing_decision
        if (record.tenant_id != context.tenant_id or record.id != context.run_id
                or record.actor_id != context.actor_id or record.actor_role is not context.actor_role
                or record.mode is not context.mode or record.request != context.request
                or record.status is not RunStatus.WAITING_APPROVAL or not isinstance(route, Mapping)
                or not _pre_model_original_capture(route)
                or any(getattr(record, name) is not None for name in (
                    'worker_id', 'worker_lease_token', 'worker_lease_expires_at', 'worker_heartbeat_at'))
                or route.get('approval_kind') != 'capability_tool'
                or route.get('approval_id') != self.approval_id
                or route.get('approval_checkpoint_required') is not False
                or _freeze_object(route['project_input_preparation'], name='original project approval capture')
                    != self.preparation
                or _freeze_object({key: route.get(key) for key in _SCOPE_KEYS}, name='original scope')
                    != _freeze_object({key: context.routing_decision.get(key) for key in _SCOPE_KEYS},
                                      name='original scope')):
            return False
        resource = 'workspace/current/project-verification'
        if self.supply_binding is not None:
            try:
                from agent_hub.capabilities.python_input_preparation import input_supply_binding

                if (_freeze_object(route.get('python_input_supply'), name='input supply state')
                        != self.supply_binding):
                    return False
                resource = input_supply_binding(record, planned=True).resource
            except (ValueError, TypeError, RuntimeError):
                return False
        elif self.preparation['schema_version'] == 2:
            marker = cast(Mapping[str, JsonValue], self.preparation['python_recipe'])
            resource += '/python/' + cast(str, marker['recipe_sha256'])
        expected_scope = (
            f'capability-scope:v1:tenant={record.tenant_id}:user={record.actor_id}:run={record.id}:'
            f'capability=command:operation=run:resource={resource}'
        )
        return route.get('approval_scope') == expected_scope


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def python_original_source_index(
    stored: StoredProjectBaseline, *, source_limit_bytes: int,
) -> Mapping[str, JsonValue]:
    """Hash the captured baseline, independently of the bounded prompt projection."""
    paths = sorted(path for path in stored.source_files if path.endswith('.py'))
    if len(paths) > 256:
        raise ValueError('python_project_history_budget_exceeded')
    if type(source_limit_bytes) is not int or not 1 <= source_limit_bytes <= 500 * 1024**2:
        raise ValueError('python_project_history_budget_exceeded')
    rows: list[JsonValue] = []
    remaining = source_limit_bytes
    try:
        for path in paths:
            raw = _read_no_links(stored.snapshot, PurePosixPath(path).parts, max_bytes=remaining)
            if len(raw) > remaining:
                raise ValueError
            remaining -= len(raw)
            captured = stored.source_files[path]
            if captured not in {hashlib.sha256(tag + raw).hexdigest() for tag in (b'f\0', b'x\0')}:
                raise ValueError
            rows.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                         'size_bytes': len(raw), 'source_file_sha256': captured})
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError(_FAILURE) from None
    return _freeze_object({'schema_version': 1, 'source_sha256': stored.reference.input_source_sha256,
        'editable_files': tuple(scope for scope in stored.contract.editable_paths if scope in stored.source_files),
        'files': tuple(rows)}, name='Python baseline source index')


def python_original_context_metadata(context: PythonOriginalContext) -> Mapping[str, JsonValue]:
    """Project trusted service history, never derive expected hashes from tool output.

    The invocation is a historical validation expectation, not execution input
    or installation authority. No raw logs, source bytes or host paths are copied.
    """
    try:
        if (type(context) is not PythonOriginalContext
                or context.inputs.contract != context.stored.contract
                or context.inputs.original_reference != context.stored.reference):
            raise ValueError
        validated_original_observation(context.observation,
            expected_source=context.stored.reference.input_source_sha256,
            expected_contract=context.stored.contract.protected_sha256,
            expected_python_inputs=context.inputs, expected_python_invocation=context.invocation)
        reference = context.environment_reference
        reference_payload = None if reference is None else reference.model_dump(mode='json')
        if context.observation.get('environment_reference') != reference_payload:
            raise ValueError
        return _freeze_object({
            'schema_version': 1, 'kind': 'python_original_context',
            'inputs': context.inputs.to_payload(),
            'invocation': context.invocation.model_dump(mode='json'),
            'original_reference': context.stored.reference.to_payload(),
            'environment_reference': reference_payload,
            'expected_execution_id': context.invocation.execution_id,
            'expected_source_sha256': context.stored.reference.input_source_sha256,
            'expected_contract_sha256': context.stored.contract.protected_sha256,
            'expected_recipe_sha256': context.inputs.sha256,
            'expected_environment_input_sha256': context.inputs.environment_input_sha256,
            'expected_environment_sha256': None if reference is None else reference.environment_sha256,
        }, name='trusted Python original context')
    except Exception:  # noqa: BLE001 - invalid history cannot replace independent expectations.
        raise RuntimeError(_FAILURE) from None


class OriginalProjectPreparer:
    def __init__(self, *, root: Path, repository: RunRepository,
                 authorizer: HarnessToolGateway, verifier: ProjectVerificationService,
                 input_supply: InputSupplyClient | None = None,
                 artifact_repository: ArtifactRepository | None = None) -> None:
        self.root, self.repository = root, repository
        self.authorizer, self.verifier = authorizer, verifier
        self.input_supply = input_supply
        self.artifact_repository = artifact_repository

    def bind_artifact_repository(self, repository: ArtifactRepository) -> None:
        """Bootstrap-only private continuation reads; verifier/gateway remain unchanged."""
        if not callable(getattr(repository, 'get_many', None)):
            raise TypeError('private artifact repository is unavailable')
        if self.artifact_repository is not None and self.artifact_repository is not repository:
            raise RuntimeError('private artifact repository is already bound')
        self.artifact_repository = repository

    async def select_existing(self, context: TaskContext) -> TaskContext:
        from agent_hub.runs.automatic_project_inputs import select_existing

        async def generated_owned() -> bool:
            if 'generated_project_preparation' in context.routing_decision:
                current = await self.verifier.generated.load(
                    context.tenant_id, context.run_id, 'main_agent', readonly=True,
                )
                return current is not None
            if self.artifact_repository is None or context.checkpoint is None:
                return False
            from agent_hub.capabilities.action_preparation import capture_action_preparation
            from agent_hub.files.workspace import ProjectWorkspaceStore
            from agent_hub.runtime.action_continuation import writes_own_inventory

            project, session = authorized_workspace_scope(
                await self.repository.get(context.tenant_id, context.run_id),
                tenant_id=context.tenant_id, run_id=context.run_id,
            )
            files = await asyncio.to_thread(ProjectWorkspaceStore(self.root).list_files,
                context.tenant_id, project, session)
            preparation = await capture_action_preparation(self.verifier, context)
            return await writes_own_inventory(self.artifact_repository, context, files, preparation)

        return await select_existing(self.repository, self.root, context, generated_owned=generated_owned)

    async def _state(
        self, context: TaskContext, *, expected: Mapping[str, JsonValue] | None = None,
        replace: Mapping[str, JsonValue] | None = None,
        supply: bool = False, preparation: Mapping[str, JsonValue] | None = None,
        supply_binding: Mapping[str, JsonValue] | None = None,
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
            if (supply and route.get('project_input_preparation') != preparation
                    or supply_binding is not None and route.get('python_input_supply') != supply_binding):
                raise RunConflict(_FAILURE)
            key = 'python_input_supply' if supply else 'project_input_preparation'
            state = route.get(key)
            if state is not None and not isinstance(state, Mapping):
                raise RuntimeError(_FAILURE)
            if supply and state is not None:
                from agent_hub.capabilities.python_input_preparation import input_supply_binding

                input_supply_binding(record)
            if replace is not None:
                if state != expected:
                    raise RunConflict(_FAILURE)
                replacement = _freeze_object(replace, name='original project preparation')
                if supply:
                    from agent_hub.capabilities.python_input_preparation import supply_state_plan

                    supply_state_plan(replacement)
                row.routing_decision = {
                    **(record.routing_decision or {}),
                    key: json.loads(_encoded(replacement)),
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
        from agent_hub.runs.automatic_project_inputs import original_source

        selected_source = original_source(context)
        if state is None:
            if selected_source is not None:
                from agent_hub.skills.sandbox.project import project_source_fingerprint

                if await project_source_fingerprint(self.root, invocation) != selected_source:
                    raise RunConflict(_FAILURE)
            state = await self._state(context, replace={'schema_version': 1, 'phase': 'reserved'})
            reference = await store.capture_once(invocation, selection)
            if selected_source is not None and reference.input_source_sha256 != selected_source:
                raise RunConflict(_FAILURE)
            state = await self._state(context, expected=state, replace={
                'schema_version': 1, 'phase': 'captured',
                'reference': cast(Mapping[str, JsonValue], reference.to_payload()),
            })
        if (not isinstance(state, Mapping) or type(state.get('schema_version')) is not int
                or state.get('schema_version') not in {1, 2} or state.get('phase') not in {'captured', 'observed'}):
            raise RuntimeError(_FAILURE)
        reference = ProjectBaselineReference.from_payload(state.get('reference'))
        if selected_source is not None and reference.input_source_sha256 != selected_source:
            raise RunConflict(_FAILURE)
        stored = await asyncio.to_thread(store.load, invocation, reference)
        if stored.contract.editable_paths != selection.editable_paths:
            raise RuntimeError(_FAILURE)
        python_inputs: PythonProjectInputs | None = None
        supply_state = await self._state(context, supply=True, preparation=state)
        if state['schema_version'] == 1 and state['phase'] == 'captured' and 'pyproject.toml' in stored.source_files:
            # Local imports preserve cold entry ordering; these are trusted pre-model APIs.
            from agent_hub.capabilities.project_verification import ProjectVerificationError
            from agent_hub.capabilities.python_input_preparation import supply_python_inputs
            from agent_hub.skills.sandbox.python_project_protocol import (
                PythonProjectProbeInvocation,
            )

            probe_invocation = ProjectInvocation.model_validate({
                **invocation.model_dump(), 'execution_id': 'python_probe_' + uuid4().hex,
                'install_allowed': False,
            }, strict=True)
            missing = supply_state is not None
            if supply_state is None:
                try:
                    python_inputs = await self.verifier.prepare_python_original(
                        invocation=probe_invocation, reference=reference)
                except ProjectVerificationError as error:
                    if type(error) is not ProjectVerificationError or str(error) != 'python_project_wheel_missing':
                        raise
                    missing = True
            if python_inputs is None and missing:
                captured_state = state

                async def state_io(*, expected: Mapping[str, JsonValue] | None = None,
                                   replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None:
                    return await self._state(context, expected=expected, replace=replace,
                                             supply=True, preparation=captured_state)

                async def authorize_inputs(plan: object, binding: Mapping[str, JsonValue]) -> None:
                    from agent_hub.skills.sandbox.python_input_supply_protocol import (
                        PythonInputSupplyPlan,
                    )

                    assert type(plan) is PythonInputSupplyPlan
                    request = HarnessToolCallRequest(run_id=context.run_id, actor='main_agent',
                        tool_name='project.prepare_inputs', arguments={}, approval_required=True,
                        sandbox='workspace_write', idempotency_key='input-supply-' + plan.sha256)
                    assert context.actor_id is not None and context.actor_role is not None
                    allowed = await self.authorizer.authorize_project_inputs(context.tenant_id,
                        request, user_id=context.actor_id, role=context.actor_role, plan=plan)
                    if (allowed.status == 'failed' and not allowed.truncated
                            and allowed.call_id == request.call_id and allowed.tool_name == request.tool_name
                            and allowed.failure_reason == 'capability requires approval'
                            and set(allowed.payload) == {'approval_id'}
                            and isinstance(allowed.payload['approval_id'], str)):
                        raise OriginalProjectApprovalPending(allowed.payload['approval_id'], captured_state,
                                                             supply_binding=binding)
                    if (allowed.status != 'succeeded' or allowed.truncated
                            or allowed.call_id != request.call_id or allowed.tool_name != request.tool_name
                            or allowed.payload != {'authorized': True}):
                        raise RuntimeError('original project preparation authorization required or denied')

                if self.input_supply is None:
                    from agent_hub.skills.sandbox.python_input_supply_client import (
                        PythonInputSupplySandbox,
                    )

                    self.input_supply = PythonInputSupplySandbox()
                supply_state = await supply_python_inputs(client=self.input_supply,
                    invocation=PythonProjectProbeInvocation.model_validate({**probe_invocation.model_dump(),
                        'original_reference': reference.to_payload()}, strict=True), stored=stored,
                    state_io=state_io, authorize=authorize_inputs, deadline=started + context.timeout_seconds)
                probe_invocation = ProjectInvocation.model_validate({**probe_invocation.model_dump(),
                    'execution_id': 'python_probe_' + uuid4().hex}, strict=True)
                python_inputs = await self.verifier.prepare_python_original(
                    invocation=probe_invocation, reference=reference)
                if await state_io() != supply_state or python_inputs is None:
                    raise RuntimeError(_FAILURE)
            if python_inputs is not None:
                if supply_state is not None:
                    from agent_hub.capabilities.python_input_preparation import (
                        require_supplied_recipe,
                    )

                    require_supplied_recipe(supply_state, python_inputs)
                state = await self._state(context, expected=state, replace={
                    **state, 'schema_version': 2, 'python_recipe': python_recipe_marker(python_inputs),
                }, supply_binding=supply_state)
                assert state is not None
        if state['schema_version'] == 2:
            python_inputs = await asyncio.to_thread(load_python_original_inputs, self.root,
                invocation, reference, state.get('python_recipe'))
        if supply_state is not None and python_inputs is not None:
            from agent_hub.capabilities.python_input_preparation import require_supplied_recipe

            require_supplied_recipe(supply_state, python_inputs)

        def validate_observation(value: object) -> Mapping[str, JsonValue]:
            # This is a historical validation expectation, not installation authority.
            expected_invocation = None if python_inputs is None else python_original_invocation(
                invocation.model_copy(update={'install_allowed': True}), python_inputs)
            return validated_original_observation(value,
                expected_source=reference.input_source_sha256,
                expected_contract=stored.contract.protected_sha256,
                expected_python_inputs=python_inputs, expected_python_invocation=expected_invocation)

        if state['phase'] == 'captured':
            request = HarnessToolCallRequest(
                run_id=context.run_id, actor='main_agent', tool_name='project.verify', arguments={},
                approval_required=False, sandbox='workspace_write',
                idempotency_key='original-project-admission-' + context.run_id.hex,
            )
            allowed = await self.authorizer.authorize(
                context.tenant_id, request, user_id=context.actor_id, role=context.actor_role,
            )
            if (allowed.status == 'failed' and not allowed.truncated
                    and allowed.call_id == request.call_id and allowed.tool_name == request.tool_name
                    and allowed.failure_reason == 'capability requires approval'
                    and set(allowed.payload) == {'approval_id'}
                    and isinstance(allowed.payload['approval_id'], str)):
                raise OriginalProjectApprovalPending(allowed.payload['approval_id'], state)
            if (allowed.status != 'succeeded' or allowed.truncated
                    or allowed.call_id != request.call_id or allowed.tool_name != request.tool_name
                    or allowed.payload.get('authorized') is not True):
                raise RuntimeError('original project preparation authorization required or denied')
            state = await self._state(context, expected=state, replace={
                **state, 'phase': 'executing', 'execution_id': invocation.execution_id,
            }, supply_binding=supply_state)
            observation = await self.verifier.observe_original(
                tenant_id=context.tenant_id, run_id=context.run_id, actor='main_agent', reference=reference,
                execution_id=invocation.execution_id,
                timeout_seconds=context.timeout_seconds - (asyncio.get_running_loop().time() - started),
            )
            observation = validate_observation(observation)
            state = await self._state(context, expected=state, replace={
                **(state or {}), 'phase': 'observed', 'observation': observation,
                'observation_sha256': hashlib.sha256(_encoded(observation)).hexdigest(),
            }, supply_binding=supply_state)
        assert state is not None
        saved_observation = state.get('observation')
        if (not isinstance(saved_observation, Mapping)
                or state.get('observation_sha256') != hashlib.sha256(_encoded(saved_observation)).hexdigest()
                or saved_observation.get('status') not in {'passed', 'failed'}
                or not isinstance(saved_observation.get('verification'), Mapping)
                or cast(Mapping[str, JsonValue], saved_observation['verification']).get('source_sha256')
                    != reference.input_source_sha256):
            raise RuntimeError(_FAILURE)
        saved_observation = validate_observation(saved_observation)
        source_context = await asyncio.to_thread(
            original_source_context, stored, byte_budget=min(96_000, max(4096, context.token_budget)),
        )
        source_index: Mapping[str, JsonValue] = {}
        if python_inputs is not None:
            source_index = {'python_source_index': await asyncio.to_thread(
                python_original_source_index, stored, source_limit_bytes=invocation.source_limit_bytes)}
        artifact = Artifact(
            id=uuid5(NAMESPACE_URL, f'original-project:{context.tenant_id}:{context.run_id}:'
                     f'{state["observation_sha256"]}'),
            type='original_project_observation', producer='original_project_preparer',
            content={'schema_version': 1, 'original_source_sha256': reference.input_source_sha256,
                     'protected_sha256': stored.contract.protected_sha256,
                     'editable_paths': selection.editable_paths, 'observation': saved_observation,
                     'source_context': cast(Mapping[str, JsonValue], source_context),
                     **source_index,
                     'summary': 'Existing user project. Inspect its source with read_context; repair only '
                                'explicit editable paths. Original checks and configuration must remain unchanged. '
                                'Re-run project.verify after repair, then deliver with workspace.bundle.'})
        artifacts = (*context.artifacts, artifact)
        if supply_state is not None:
            from agent_hub.capabilities.python_input_preparation import supply_state_plan

            supply_state_plan(supply_state)
            if supply_state.get('phase') != 'supplied':
                raise RuntimeError(_FAILURE)
            artifacts = (*artifacts, Artifact(
                id=uuid5(NAMESPACE_URL, f'python-input-supply:{context.tenant_id}:{context.run_id}:'
                         f'{supply_state["plan_sha256"]}'), type='python_project_input_supply',
                producer='original_project_preparer', content={
                    'schema_version': 1, 'kind': 'published_inputs_not_installation',
                    'plan_sha256': supply_state['plan_sha256'], 'receipt': supply_state['receipt']}))
        history_route: Mapping[str, JsonValue] = {}
        if python_inputs is not None:
            from agent_hub.runtime.python_project_history import HISTORY_KEY, build_python_history

            trusted = await self.verifier.python_original_context(context.tenant_id, context.run_id)
            if (trusted is None or trusted.inputs != python_inputs or trusted.stored.reference != reference
                    or trusted.observation != saved_observation or await self._state(context) != state):
                raise RuntimeError(_FAILURE)
            metadata = Artifact(
                id=uuid5(NAMESPACE_URL, f'original-python-context:{context.tenant_id}:{context.run_id}:'
                         f'{python_inputs.sha256}:{state["observation_sha256"]}'),
                type='original_python_project_context', producer='original_project_preparer',
                content=python_original_context_metadata(trusted),
                source_ids=(str(artifact.id),),
            )
            artifacts = (*artifacts, metadata)
            history_route = {HISTORY_KEY: build_python_history(trusted, artifact)}
        return context.model_copy(update={
            'routing_decision': {**route, 'project_input_preparation': state, **history_route,
                                **({'python_input_supply': supply_state} if supply_state is not None else {})},
            'artifacts': artifacts,
        }).validated_internal_clone()
