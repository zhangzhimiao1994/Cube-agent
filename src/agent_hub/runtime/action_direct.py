"""Direct action continuation using private data and concrete policy gates.

Configured Direct factories select this runtime for supported workspace runs and
v2 recovery when the action stack is injected. Replay observations grant no tool
execution or successful verification. Stable guidance and Root-validated current
preparation snapshots are bound on resume; existing services retain ownership of
preparation, supply and verification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine, Mapping
from contextlib import aclosing
from typing import Any, cast
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.action_preparation import CONTRACT_ROUTE_KEYS as _CONTRACT_ROUTE_KEYS
from agent_hub.capabilities.generated_project_service import validate_generated_preparation
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.action_approval_capture import capture_action_approval
from agent_hub.runtime.action_checkpoint import (
    action_checkpoint_valid as action_checkpoint_valid,  # noqa: PLC0414
)
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.continuation_artifacts import _encode, load_continuation, save_continuation
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
    _freeze_object,
)
from agent_hub.runtime.delivery_followup import (
    DELIVERY_FOLLOWUP_POLICY,
    DELIVERY_FOLLOWUP_SUPPORTED_VERSIONS,
)
from agent_hub.runtime.direct import (
    CapabilityGateway,
    DirectRuntime,
    Gateway,
    RuntimeExecutionError,
)
from agent_hub.runtime.direct_completion_journal import DirectCompletionJournal
from agent_hub.runtime.engineering_admission import _fingerprint
from agent_hub.runtime.project_scale_artifact import HarnessToolInvoker
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_FIXED_ROUTE_KEYS = (
    'project_delivery', 'artifact_strategy', 'project_scale', 'replace_workspace_files',
    'website_preview_required', 'runtime_timeout_source', 'critical_path_complexity_units',
    'direct_model', 'main_agent_model', 'allowed_models', 'harness_policy', 'permission_mode',
    'engineering_diagnostic_reads_version',
    'engineering_prerequisite_repair_version',
)
_GUIDANCE_ROUTE_KEYS = (
    'project_preflight_approved', 'project_preflight_proposal',
    'self_repair_context', 'self_repair_accepted', 'source',
    DELIVERY_FOLLOWUP_POLICY,
)
_APPROVAL_ROUTE_KEYS = (
    'approval_kind', 'approval_id', 'approval_fingerprint', 'approval_scope',
    'approval_checkpoint_required', 'approval_checkpoint_worker_token',
)
_TOKEN_BUDGET_KEYS = (
    'runtime_token_absolute_tokens', 'runtime_token_soft_base_tokens',
    'main_agent_context_window_tokens', 'context_window_tokens',
)
_TIME_BUDGET_KEYS = (
    'runtime_timeout_seconds', 'runtime_timeout_absolute_seconds', 'runtime_timeout_soft_seconds',
)


def _json(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def _contract_identity(route: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    # Keep presence: an absent selector and an explicit null select different paths.
    identity = {key: route[key] for key in _CONTRACT_ROUTE_KEYS if key in route}
    for key in ('project_input_contract_version', 'generated_project_contract_version'):
        if key in identity and (type(identity[key]) is not int or identity[key] != 1):
            raise RuntimeExecutionError('action contract is unavailable')
    for key, value in identity.items():
        if key.endswith('_version'):
            continue
        if not isinstance(value, Mapping):
            raise RuntimeExecutionError('action contract is unavailable')
    if 'generated_project_preparation' in identity:
        failed = False
        try:
            state = validate_generated_preparation(identity['generated_project_preparation'])
        except (ValueError, TypeError, RuntimeError):
            failed = True
        if failed:
            raise RuntimeExecutionError('action contract is unavailable') from None
        # Frozen manifest/capture/recipe identity, not an authorization receipt.
        identity['generated_project_preparation'] = {
            key: value for key, value in state.items() if key != 'phase'
        }
    return identity


def _binding(context: TaskContext, logical_model: str | None = None) -> str:
    route = context.routing_decision
    decision = route.get('harness_decision')
    selected = ({'selected_logical_model': decision['selected_logical_model']}
                if isinstance(decision, Mapping) and 'selected_logical_model' in decision else {})
    return hashlib.sha256(_json({'scope': _fingerprint(context),
        'route': {key: route[key] for key in _FIXED_ROUTE_KEYS if key in route},
        'contracts': _contract_identity(route),
        'guidance': _guidance_identity(route),
        'resolved_model': logical_model, 'selected_model': selected,
        'instructions': None if context.instruction_context is None else context.instruction_context.render(),
        'sources': [item.to_payload() for item in context.artifacts],
    })).hexdigest()


def _guidance_identity(route: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    # Keep presence and every field, including values the prompt renderer ignores.
    return _freeze_object({key: route[key] for key in _GUIDANCE_ROUTE_KEYS if key in route},
                          name='action guidance')


def _prepared_context(context: TaskContext, frame: Mapping[str, JsonValue]) -> TaskContext:
    if not frame:
        if any(key in context.routing_decision for key in _CONTRACT_ROUTE_KEYS):
            raise RuntimeExecutionError('action preparation scope is unavailable')
        return context
    contracts = frame.get('contracts')
    if (set(frame) != {'schema_version', 'scope', 'contracts', 'workspace_sha256'}
            or type(frame['schema_version']) is not int or frame['schema_version'] != 1
            or not isinstance(frame['scope'], Mapping) or not isinstance(contracts, Mapping)
            or type(frame['workspace_sha256']) is not str
            or re.fullmatch(r'[a-f0-9]{64}', frame['workspace_sha256']) is None
            or set(contracts) - set(_CONTRACT_ROUTE_KEYS)):
        raise RuntimeExecutionError('action preparation scope is unavailable')
    for key in ('project_input_contract_version', 'project_input', 'generated_project_contract_version'):
        if ((key in context.routing_decision) != (key in contracts)
                or context.routing_decision.get(key) != contracts.get(key)):
            raise RuntimeExecutionError('action preparation selector changed')
    return context.model_copy(update={'routing_decision': _freeze_object({
        **{key: value for key, value in context.routing_decision.items() if key not in _CONTRACT_ROUTE_KEYS},
        **contracts,
    }, name='action prepared routing')})


def _check_guidance(context: TaskContext) -> None:
    route = context.routing_decision
    try:
        # Reuse the storage boundary's checks before any model or tool side effect.
        # This grants no permission and never substitutes the bounded prompt text.
        _encode(_guidance_identity(route))
        if DELIVERY_FOLLOWUP_POLICY in route and (
            type(route[DELIVERY_FOLLOWUP_POLICY]) is not int
            or route[DELIVERY_FOLLOWUP_POLICY] not in DELIVERY_FOLLOWUP_SUPPORTED_VERSIONS
        ):
            raise ValueError
        for flag in ('project_preflight_approved', 'self_repair_accepted'):
            if flag in route and type(route[flag]) is not bool:
                raise ValueError
        proposal = route.get('project_preflight_proposal')
        repair = route.get('self_repair_context')
        for key, value, marker, expected in (
            ('project_preflight_proposal', proposal, 'kind', 'project_architecture_preflight'),
            ('self_repair_context', repair, 'source', 'self_repair'),
        ):
            if key not in route:
                continue
            if not isinstance(value, Mapping) or value.get(marker) != expected:
                raise ValueError
            if 'schema_version' in value and (
                type(value['schema_version']) is not int or value['schema_version'] != 1
            ):
                raise ValueError
        if route.get('project_preflight_approved') is True and proposal is None:
            raise ValueError
        if (route.get('source') == 'self_repair' or route.get('self_repair_accepted') is True) and repair is None:
            raise ValueError
        return
    except (TypeError, ValueError, RecursionError):
        pass
    raise RuntimeExecutionError('action recovery guidance is unsupported or unsafe to persist') from None


def _tighten_budgets(snapshot: TaskContext, current: TaskContext) -> TaskContext:
    route = {key: value for key, value in snapshot.routing_decision.items()
             if key not in _APPROVAL_ROUTE_KEYS}
    for key in (*_TOKEN_BUDGET_KEYS, *_TIME_BUDGET_KEYS):
        values: list[int | float] = []
        for source in (snapshot.routing_decision, current.routing_decision):
            if key not in source:
                continue
            value = source[key]
            if (type(value) not in (int, float) or not math.isfinite(cast(float, value))
                    or cast(float, value) <= 0
                    or key in _TOKEN_BUDGET_KEYS and type(value) is not int):
                raise RuntimeExecutionError('action continuation budget is unavailable')
            values.append(cast(int | float, value))
        if values:
            route[key] = min(values)
    tokens = min(snapshot.token_budget, current.token_budget,
                 cast(int, route.get('runtime_token_absolute_tokens', snapshot.token_budget)))
    seconds = min(snapshot.timeout_seconds, current.timeout_seconds,
                  cast(float, route.get('runtime_timeout_absolute_seconds', snapshot.timeout_seconds)),
                  cast(float, route.get('runtime_timeout_seconds', snapshot.timeout_seconds)))
    route['runtime_token_absolute_tokens'] = tokens
    route['runtime_timeout_absolute_seconds'] = seconds
    windows = [cast(int, route[key]) for key in (
        'main_agent_context_window_tokens', 'context_window_tokens') if key in route]
    if windows:
        route['main_agent_context_window_tokens'] = route['context_window_tokens'] = min(windows)
    return snapshot.model_copy(update={
        'routing_decision': route, 'token_budget': tokens, 'timeout_seconds': seconds,
    })


class _PendingAction(BaseException):
    def __init__(self, request: HarnessToolCallRequest, approval: str) -> None:
        self.request, self.approval = request, approval


class _ModelGateway:
    def __init__(self, owner: ActionApprovedDirectRuntime, gateway: Gateway) -> None:
        self.owner, self.gateway = owner, gateway

    async def complete_with_context(self, request: Any) -> Any:
        assert self.owner._journal is not None
        return await self.owner._journal.complete(request, self.gateway.complete_with_context)


class _WorkspaceGateway:
    def __init__(self, owner: ActionApprovedDirectRuntime, backend: CapabilityGateway) -> None:
        self.owner, self.backend = owner, backend

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        method = getattr(self.backend, 'is_available', None)
        return callable(method) and method(tenant_id, name) is True

    def is_replay_safe(self, name: str) -> bool:
        return self.backend.is_replay_safe(name)

    async def execute(self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
                      arguments: Mapping[str, JsonValue], idempotency_key: str) -> Mapping[str, JsonValue]:
        owner = self.owner
        context = owner._action_context
        if context is None or (tenant_id, run_id, actor) != (context.tenant_id, context.run_id, 'main_agent'):
            raise RuntimeExecutionError('action scope is unavailable')
        raw_sha = hashlib.sha256(_json({'name': name, 'arguments': arguments, 'key': idempotency_key})).hexdigest()
        pending = owner._pending
        if pending is not None and owner._action_cursor == len(owner._actions):
            if pending['raw_sha256'] != raw_sha:
                raise RuntimeExecutionError('pending action does not match continuation')
            request = owner._request(pending['request'])
        elif owner._action_cursor < len(owner._actions):
            saved = owner._actions[owner._action_cursor]
            if saved['raw_sha256'] != raw_sha:
                raise RuntimeExecutionError('action does not match continuation')
            request = owner._request(saved['request'])
        else:
            values = dict(arguments)
            if name == 'workspace.write_text' and len(_json(values)) > 48_000:
                content = values.pop('content', None)
                if not isinstance(content, str):
                    raise RuntimeExecutionError('workspace content is unavailable')
                values['content_reference'] = await save_continuation(owner._repository,
                    tenant_id=tenant_id, run_id=run_id, value=content)
            request = HarnessToolCallRequest(run_id=run_id, actor=actor, tool_name=name, arguments=values,
                idempotency_key=idempotency_key, approval_required=False,
                sandbox=cast(str, context.routing_decision.get('sandbox_profile', 'none')))
        result = await owner._invoke(request, raw_sha=raw_sha)
        if result.status != 'succeeded' or result.truncated:
            raise RuntimeExecutionError(result.failure_reason or 'workspace action failed')
        return result.payload


class _ToolGateway:
    def __init__(self, owner: ActionApprovedDirectRuntime) -> None:
        self.owner = owner

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        context = self.owner._action_context
        if context is None or (tenant_id, user_id, role) != (context.tenant_id, context.actor_id, context.actor_role):
            raise RuntimeExecutionError('action identity is unavailable')
        return await self.owner._invoke(request, raw_sha=hashlib.sha256(_json(request.to_payload())).hexdigest())


class ActionApprovedDirectRuntime(DirectRuntime):
    def __init__(self, gateway: Gateway, *, artifact_repository: ArtifactRepository,
                 capability_gateway: CapabilityGateway, harness_tool_gateway: HarnessToolInvoker,
                 **kwargs: Any) -> None:
        self._repository = artifact_repository
        self._action_gateway, self._action_backend = harness_tool_gateway, capability_gateway
        self._action_context: TaskContext | None = None
        self._journal: DirectCompletionJournal | None = None
        self._action_restored: RuntimeCheckpoint | None = None
        self._actions: list[dict[str, Any]] = []
        self._action_cursor = 0
        self._pending: dict[str, Any] | None = None
        self._pending_preparation: Mapping[str, JsonValue] | None = None
        self._action_deadline = 0.0
        self._inspection: Artifact | None = None
        super().__init__(_ModelGateway(self, gateway), capability_gateway=_WorkspaceGateway(self, capability_gateway),
                         harness_tool_gateway=_ToolGateway(self), **kwargs)

    @staticmethod
    def _request(value: object) -> HarnessToolCallRequest:
        if not isinstance(value, Mapping):
            raise RuntimeExecutionError('action request is unavailable')
        return HarnessToolCallRequest(**{**dict(value), 'run_id': UUID(value['run_id'])})

    async def _invoke(self, request: HarnessToolCallRequest, *, raw_sha: str) -> HarnessToolCallResult:
        context = self._action_context
        if context is None or request.run_id != context.run_id or request.actor != 'main_agent':
            raise RuntimeExecutionError('action scope is unavailable')
        if self._action_cursor < len(self._actions):
            saved = self._actions[self._action_cursor]
            if saved['raw_sha256'] != raw_sha or _json(saved['request']) != _json(request.to_payload()):
                raise RuntimeExecutionError('action does not match continuation')
            result = HarnessToolCallResult(**saved['result'])
            if request.tool_name == 'project.verify' and result.status == 'succeeded':
                # Later attempts supersede authority even if failed or awaiting approval.
                later_verify = any(action['request']['tool_name'] == 'project.verify'
                    for action in self._actions[self._action_cursor + 1:]) or (
                    self._pending is not None
                    and self._pending['request']['tool_name'] == 'project.verify')
                if not later_verify:
                    restore = getattr(self._action_backend, 'restore_project_verification', None)
                    if not callable(restore):
                        raise RuntimeExecutionError('project verification recovery is unavailable')
                    restored = await restore(tenant_id=context.tenant_id, run_id=context.run_id,
                        actor=request.actor, action_key=request.idempotency_key)
                    if restored != result.payload:
                        raise RuntimeExecutionError('project verification recovery does not match')
            self._action_cursor += 1
            return result
        if self._pending is not None and (self._pending['raw_sha256'] != raw_sha
                or _json(self._pending['request']) != _json(request.to_payload())):
            raise RuntimeExecutionError('pending action does not match continuation')
        self._pending_preparation = None
        async def read_preparation() -> Mapping[str, JsonValue]:
            return await self._preparation_snapshot(context, deadline=self._action_deadline)
        with capture_action_approval(context, request, read_preparation) as capture:
            result = await self._action_gateway.invoke(context.tenant_id, request,
                user_id=context.actor_id, role=context.actor_role)
            if isinstance(result, HarnessToolCallResult) and result.status == 'failed' and result.failure_reason == 'capability requires approval':
                approval_id = result.payload.get('approval_id')
                if isinstance(approval_id, str):
                    self._pending_preparation = capture.frame_for(approval_id)
        if not isinstance(result, HarnessToolCallResult) or (result.call_id, result.tool_name) != (request.call_id, request.tool_name):
            raise RuntimeExecutionError('action result is unavailable')
        if result.status == 'failed' and result.failure_reason == 'capability requires approval':
            approval = result.payload.get('approval_id')
            if result.truncated or not isinstance(approval, str) or not re.fullmatch(r'approval_[A-Za-z0-9_-]{1,100}', approval):
                raise RuntimeExecutionError('approval identity is unavailable')
            self._pending = {'raw_sha256': raw_sha, 'request': dict(request.to_payload())}
            raise _PendingAction(request, approval)
        self._pending = None
        self._actions.append({'raw_sha256': raw_sha, 'request': dict(request.to_payload()),
                              'result': dict(result.to_payload())})
        self._action_cursor += 1
        return result

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        if checkpoint.runtime_type == 'direct_action':
            checkpoint = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
            if not action_checkpoint_valid(checkpoint) or self._active_token is not None:
                raise RuntimeExecutionError('action checkpoint is incompatible')
            self._action_restored = self._last_checkpoint = checkpoint
        else:
            await super().restore_checkpoint(checkpoint)

    async def _run(self, context: TaskContext, token: object, done: asyncio.Event) -> AsyncIterator[RunEvent]:
        try:
            async with aclosing(self._run_actions(context, token, done)) as events:
                async for event in events:
                    yield event
        finally:
            self._action_context = None
            self._pending_preparation = None
            self._action_deadline = 0.0
            super()._release_run(token, done)

    def _release_run(self, token: object, done: asyncio.Event) -> None:
        # The outer continuation owns the stream until its private IO has drained.
        pass

    def _inspection_artifact(
        self, request: HarnessToolCallRequest, inspection: Mapping[str, JsonValue],
    ) -> Artifact:
        candidate = super()._inspection_artifact(request, inspection)
        if self._inspection is None:
            self._inspection = candidate
        elif self._inspection.content != candidate.content:
            raise RuntimeExecutionError('project inspection changed during recovery')
        return self._inspection

    async def _data_io(
        self, operation: Coroutine[Any, Any, dict[str, JsonValue]], *, deadline: float,
    ) -> dict[str, JsonValue]:
        task = asyncio.create_task(operation)
        previous = self._active_capability_task
        self._active_capability_task = task
        try:
            async with asyncio.timeout_at(deadline):
                result = await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self._active_capability_task is task:
                # Nested approval reads must return cancellation ownership to the issuer.
                self._active_capability_task = previous
        # A finished storage task cannot be cancelled; publication still must be fenced.
        if self._cancel_requested:
            raise asyncio.CancelledError
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        return result

    async def _preparation_snapshot(self, context: TaskContext, *, deadline: float) -> Mapping[str, JsonValue]:
        contracts = any(key in context.routing_decision for key in _CONTRACT_ROUTE_KEYS)
        permissions = context.routing_decision.get('requested_permissions', ())
        if not contracts and (not isinstance(permissions, (tuple, list)) or 'command.run' not in permissions):
            return {}
        getter = getattr(self._action_backend, 'action_preparation_snapshot', None)
        if not callable(getter):
            raise RuntimeExecutionError('action preparation recovery is unavailable')
        async def read() -> dict[str, JsonValue]:
            try:
                frame = await getter(context)
            except (asyncio.CancelledError, ProcessCleanupIncomplete):
                raise
            except Exception:  # noqa: BLE001 - preparation errors expose no private repository details.
                raise RuntimeExecutionError('action preparation context changed') from None
            if not isinstance(frame, Mapping):
                raise RuntimeExecutionError('action preparation recovery is unavailable')
            return dict(_freeze_object(frame, name='action preparation snapshot'))
        return await self._data_io(read(), deadline=deadline)

    async def _run_actions(self, context: TaskContext, token: object, done: asyncio.Event) -> AsyncGenerator[RunEvent, None]:
        _check_guidance(context)
        original = _tighten_budgets(context, context)
        checkpoint = self._action_restored
        self._action_restored = None
        self._actions, self._pending, self._inspection = [], None, None
        self._pending_preparation = None
        started = asyncio.get_running_loop().time()
        deadline = started + original.timeout_seconds
        elapsed = 0.0
        skip = emitted = 0
        refs: tuple[JsonValue, ...] = ()
        if checkpoint is not None:
            if ((checkpoint.tenant_id, checkpoint.run_id) != (context.tenant_id, context.run_id)
                    or context.checkpoint is None or context.checkpoint.id != checkpoint.id):
                raise RuntimeExecutionError('action checkpoint context changed')
            elapsed = cast(float, checkpoint.state['elapsed_seconds'])
            deadline = started + min(original.timeout_seconds,
                cast(float, checkpoint.state['timeout_seconds'])) - elapsed
            if deadline <= started:
                raise RuntimeExecutionError('action continuation execution budget is exhausted')

            async def load() -> dict[str, JsonValue]:
                value = await load_continuation(self._repository, tenant_id=context.tenant_id, run_id=context.run_id,
                    reference=cast(Mapping[str, JsonValue], checkpoint.state['reference']))
                if not isinstance(value, Mapping):
                    raise RuntimeExecutionError('action continuation is unavailable')
                return dict(value)

            value = await self._data_io(load(), deadline=deadline)
            if set(value) != {'context', 'models', 'actions', 'pending', 'emitted', 'elapsed', 'inspection', 'preparation'}:
                raise RuntimeExecutionError('action continuation is unavailable')
            snapshot = TaskContext.from_payload(json.loads(_json(value['context'])))
            snapshot = snapshot.model_copy(update={'instruction_context': context.instruction_context})
            sources = {item.id: item for item in context.artifacts}
            if any(item.id not in sources or _json(sources[item.id].to_payload()) != _json(item.to_payload())
                   for item in snapshot.artifacts):
                raise RuntimeExecutionError('action continuation sources changed')
            current = context.model_copy(update={'artifacts': snapshot.artifacts})
            _check_guidance(snapshot)
            preparation = await self._preparation_snapshot(current, deadline=deadline)
            if value['preparation'] != preparation:
                raise RuntimeExecutionError('action preparation context changed')
            bound_snapshot, bound_current = _prepared_context(snapshot, preparation), _prepared_context(current, preparation)
            if (_binding(bound_snapshot, self._logical_model) != _binding(bound_current, self._logical_model)
                    or _binding(bound_snapshot, self._logical_model) != checkpoint.state['context_sha256']):
                raise RuntimeExecutionError('action continuation context changed')
            if value['inspection'] is not None:
                self._inspection = Artifact.from_payload(json.loads(_json(value['inspection'])))
                published = sources.get(self._inspection.id)
                if published is None or _json(published.to_payload()) != _json(self._inspection.to_payload()):
                    raise RuntimeExecutionError('project inspection recovery is unavailable')
            refs = tuple(cast(tuple[JsonValue, ...], value['models']))
            self._actions = list(cast(list[dict[str, Any]], value['actions']))
            self._pending = cast(dict[str, Any], value['pending'])
            skip = cast(int, value['emitted'])
            if type(skip) is not int or skip < 0:
                raise RuntimeExecutionError('action continuation position is unavailable')
            recorded = value['elapsed']
            if (type(recorded) not in (int, float) or not math.isfinite(cast(float, recorded))
                    or not 0 <= cast(float, recorded) <= elapsed):
                raise RuntimeExecutionError('action continuation budget is unavailable')
            original = _tighten_budgets(snapshot, context)
            if original.timeout_seconds > cast(float, checkpoint.state['timeout_seconds']):
                raise RuntimeExecutionError('action continuation budget is unavailable')
            remaining = (original.timeout_seconds - elapsed
                         - (asyncio.get_running_loop().time() - started))
            if remaining <= 0:
                raise RuntimeExecutionError('action continuation execution budget is exhausted')
            context = original.model_copy(update={'execution_token': context.execution_token,
                'checkpoint': None, 'timeout_seconds': remaining,
                'routing_decision': {**original.routing_decision,
                                     'runtime_timeout_absolute_seconds': remaining}})
            deadline = asyncio.get_running_loop().time() + remaining
        else:
            context = original
            deadline = started + context.timeout_seconds
        self._action_context = context
        self._action_deadline = deadline
        self._journal = DirectCompletionJournal(self._repository, tenant_id=context.tenant_id,
                                               run_id=context.run_id, references=refs)
        self._action_cursor = 0
        try:
            async for event in super()._run(context, token, done):
                emitted += 1
                if emitted > skip:
                    yield event
        except _PendingAction as pending:
            captured = self._pending_preparation
            preparation = (await self._preparation_snapshot(context, deadline=deadline)
                           if captured is None else captured)
            binding = _binding(_prepared_context(original, preparation), self._logical_model)
            reference = await self._data_io(save_continuation(self._repository, tenant_id=context.tenant_id, run_id=context.run_id,
                value=cast(JsonValue, {'context': original.model_copy(update={'checkpoint': None, 'execution_token': None}).to_payload(),
                    'models': self._journal.references, 'actions': self._actions,
                    'pending': self._pending, 'emitted': emitted,
                    'preparation': preparation,
                    'inspection': None if self._inspection is None else self._inspection.to_payload(),
                    'elapsed': elapsed + asyncio.get_running_loop().time() - started})), deadline=deadline)
            self._last_checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='direct_action', runtime_version='2',
                run_id=context.run_id, tenant_id=context.tenant_id, mode=self.mode,
                state={'phase': 'concrete_action', 'completed': False, 'approval_id': pending.approval,
                    'tool_name': pending.request.tool_name, 'context_sha256': binding, 'reference': reference,
                    'elapsed_seconds': elapsed + asyncio.get_running_loop().time() - started,
                    'timeout_seconds': original.timeout_seconds})
            yield RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=emitted+1,
                           run_id=context.run_id, checkpoint=self._last_checkpoint)
