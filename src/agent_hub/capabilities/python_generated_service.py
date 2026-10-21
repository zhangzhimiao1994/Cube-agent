"""Generated Python preparation and receipts, never original-input observations.

The generated owner authenticates the live lease and immutable post-model capture.
Root probes, exact independent grants and live cache inspections remain mandatory.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, NoReturn, cast, get_args
from uuid import UUID, uuid4

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_verification import ProjectVerificationError, _strict_object
from agent_hub.capabilities.python_input_preparation import (
    InputSupplyClient,
    input_supply_request,
    require_supplied_recipe,
    supply_python_inputs,
    supply_state_plan,
    validate_supply_declaration,
)
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_reports import (
    PythonProjectFailure,
    decode_python_project_report,
)
from agent_hub.capabilities.python_project_service import (
    PythonProjectExecutor,
    PythonProjectService,
    load_python_original_inputs,
    python_original_invocation,
    python_recipe_marker,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.project import project_source_fingerprint, project_source_manifest
from agent_hub.skills.sandbox.python_input_supply_client import PythonInputSupplySandbox
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyPlan,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    SupplyError,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
    PythonProjectProbeInvocation,
    _reference_matches,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.capabilities.generated_project_service import (
        GeneratedProjectContext,
        GeneratedProjectService,
    )
    from agent_hub.capabilities.project_verification import ProjectVerificationService

_STATE = 'generated_project_preparation'
_SUPPLY = 'python_input_supply'
_PREFIX = 'generated-environment-'
_ERROR = 'generated_python_project_unavailable'
_CHANGED = 'generated_acceptance_scope_changed'
_RECIPE_REQUIRED = 'python_project_install_approval_required'


def _need(value: object, code: str = _CHANGED) -> None:
    if not value:
        raise ProjectVerificationError(code)


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _probe(context: GeneratedProjectContext, prefix: str) -> PythonProjectProbeInvocation:
    return PythonProjectProbeInvocation.model_validate({**context.invocation.model_dump(),
        'execution_id': prefix + uuid4().hex, 'install_allowed': False,
        'original_reference': context.stored.reference.to_payload()}, strict=True)


def generated_python_approval_request(
    record: object, *, agent_id: str, idempotency_key: str,
) -> CapabilityRequest | None:
    """Project DB-owned safe bindings for policy; no file reads or execution grant."""
    from agent_hub.capabilities.generated_project_service import (
        generated_authority,
        generated_selected,
        validate_generated_preparation,
    )

    if not generated_selected(record):
        return None
    tenant, run = getattr(record, 'tenant_id', None), getattr(record, 'id', None)
    _need(isinstance(tenant, UUID) and isinstance(run, UUID))
    authority = generated_authority(record, UUID(str(tenant)), UUID(str(run)), readonly=True)
    route = cast(Mapping[str, object], getattr(record, 'routing_decision', None))
    if route.get(_STATE) is None:
        return None
    state = validate_generated_preparation(route[_STATE])
    _need(state['user_id'] == str(authority.user_id)
          and state['request_sha256'] == authority.request_sha256)
    if state['stack'] != 'python' or state['phase'] == 'capturing':
        return None
    if _SUPPLY in state:
        supply = cast(Mapping[str, JsonValue], state[_SUPPLY])
        plan = supply_state_plan(supply)
        probe = PythonProjectProbeInvocation.model_validate({**authority.invocation.model_dump(),
            'original_reference': dict(cast(Mapping[str, JsonValue], state['reference']))}, strict=True)
        plan.require_scope(probe)
        if supply['phase'] != 'supplied':
            _need(agent_id == state['proposer_actor'], 'input_supply_scope_changed')
            return input_supply_request(plan, user_id=authority.user_id,
                idempotency_key=idempotency_key).model_copy(update={'agent_id': agent_id})
    if state['phase'] != 'python_ready':
        return None
    marker = cast(Mapping[str, JsonValue], state['python_recipe'])
    return CapabilityRequest(tenant_id=authority.tenant_id, run_id=authority.run_id,
        user_id=authority.user_id, agent_id=agent_id, capability='command', operation='run',
        resource='workspace/current/project-verification/python/' + cast(str, marker['recipe_sha256']),
        idempotency_key=idempotency_key, arguments={
            'schema_version': 1, 'recipe_sha256': marker['recipe_sha256'],
            'environment_input_sha256': marker['environment_input_sha256'],
            'project_id': authority.project_id, 'workspace_session_id': authority.session_id})


class PythonGeneratedRecipeApprovalRequired(ProjectVerificationError):
    """Supply completed, but its grant cannot authorize project installation."""

    def __init__(self, request: CapabilityRequest) -> None:
        super().__init__(_RECIPE_REQUIRED)
        self.approval_request = request


@dataclass(frozen=True)
class _Receipt:
    context: GeneratedProjectContext
    invocation: PythonProjectInvocation
    inputs: PythonProjectInputs
    source: str
    reference: PythonEnvironmentReference
    actor: str
    binding: Mapping[str, JsonValue]


@dataclass(frozen=True)
class PythonGeneratedEnvironmentContext:
    """Readonly generated issuance; neither original observation nor live grant."""

    invocation: PythonProjectInvocation
    environment_references: tuple[PythonEnvironmentReference, ...]


class PythonGeneratedProjectService:
    def __init__(self, owner: ProjectVerificationService, generated: GeneratedProjectService,
                 executor: PythonProjectExecutor | None = None) -> None:
        self.owner, self.generated = owner, generated
        self.executor = executor or owner._python.executor
        self.supply_client: InputSupplyClient = PythonInputSupplySandbox()
        self.receipts: dict[tuple[UUID, UUID], _Receipt] = {}

    async def _current(self, context: GeneratedProjectContext, actor: str,
                       *, exact_state: bool = False) -> GeneratedProjectContext:
        base = context.invocation
        current = await self.generated.load(base.tenant_id, base.run_id, actor)
        _need(current is not None)
        assert current is not None
        _need(current.envelope.sha256 == context.envelope.sha256
              and current.stored.reference == context.stored.reference
              and current.authority == context.authority
              and (not exact_state or current.state == context.state)
              and current.envelope.manifest.stack == 'python')
        return current

    async def _inputs(self, context: GeneratedProjectContext) -> PythonProjectInputs:
        inputs = await asyncio.to_thread(load_python_original_inputs, self.owner.root,
            context.invocation, context.stored.reference, context.state.get('python_recipe'))
        _need(inputs.contract == context.stored.contract
              and inputs.original_reference == context.stored.reference)
        if _SUPPLY in context.state:
            require_supplied_recipe(cast(Mapping[str, JsonValue], context.state[_SUPPLY]), inputs)
        return inputs

    async def _prepare(self, context: GeneratedProjectContext, actor: str,
                       *, fresh: bool = False) -> tuple[GeneratedProjectContext, PythonProjectInputs]:
        if context.state['phase'] == 'python_ready' and not fresh:
            return context, await self._inputs(context)
        invocation = context.invocation.model_copy(update={
            'execution_id': 'generated_probe_' + uuid4().hex, 'install_allowed': False})
        inputs = await self.owner._python.prepare(invocation, context.stored.reference)
        _need(type(inputs) is PythonProjectInputs, _ERROR)
        assert inputs is not None
        _need(inputs.contract == context.stored.contract
              and inputs.original_reference == context.stored.reference)
        await self._current(context, actor, exact_state=True)
        if _SUPPLY in context.state:
            require_supplied_recipe(cast(Mapping[str, JsonValue], context.state[_SUPPLY]), inputs)
        marker = python_recipe_marker(inputs)
        if context.state['phase'] == 'python_ready':
            _need(context.state['python_recipe'] == marker, 'python_project_recipe_invalid')
        else:
            context = await self.generated.update(context, {**context.state,
                'phase': 'python_ready', 'python_recipe': marker})
        return context, inputs

    async def _plan(self, context: GeneratedProjectContext, actor: str) -> GeneratedProjectContext:
        _need(actor == context.envelope.proposer_actor, 'input_supply_scope_changed')
        probe = _probe(context, 'input_plan_')
        response = await self.supply_client.plan(probe)
        _need(type(response) is PythonInputSupplyResponse, 'input_supply_request_invalid')
        response = PythonInputSupplyResponse.model_validate_json(response.model_dump_json(), strict=True)
        response.require_for(PythonInputSupplyRequest(action='plan_python_project_inputs', invocation=probe))
        _need(response.ok and response.plan is not None, response.error or 'input_supply_unavailable')
        assert response.plan is not None
        validate_supply_declaration(response.plan, probe, context.stored)
        await self._current(context, actor, exact_state=True)
        if _SUPPLY in context.state:
            _need(supply_state_plan(context.state[_SUPPLY]) == response.plan, 'input_supply_plan_changed')
            return context
        state: Mapping[str, JsonValue] = _freeze_object({'schema_version': 1, 'phase': 'planned',
            'plan': response.plan.model_dump(mode='json'), 'plan_sha256': response.plan.sha256},
            name='generated Python supply')
        return await self.generated.update(context, {**context.state, _SUPPLY: state})

    async def authorization_request(self, request: CapabilityRequest,
                                    context: GeneratedProjectContext) -> CapabilityRequest:
        try:
            _need(type(request) is CapabilityRequest)
            request = CapabilityRequest.model_validate(request.model_dump(warnings='error'), strict=True)
            _need((request.tenant_id, request.run_id, request.user_id)
                  == (context.invocation.tenant_id, context.invocation.run_id, context.envelope.user_id))
            generic = (request.capability == 'command' and request.operation == 'run'
                and request.resource == 'workspace/current/project-verification' and request.arguments == {})
            projected = generated_python_approval_request(context.record,
                agent_id=request.agent_id, idempotency_key=request.idempotency_key)
            _need(generic or request == projected)
            context = await self._current(context, request.agent_id)
            if _SUPPLY in context.state and cast(Mapping[str, JsonValue], context.state[_SUPPLY])['phase'] != 'supplied':
                _need(request.agent_id == context.envelope.proposer_actor, 'input_supply_scope_changed')
                if cast(Mapping[str, JsonValue], context.state[_SUPPLY])['phase'] == 'planned':
                    context = await self._plan(context, request.agent_id)
            else:
                try:
                    context, _ = await self._prepare(context, request.agent_id)
                except ProjectVerificationError as error:
                    if str(error) != 'python_project_wheel_missing':
                        raise
                    context = await self._plan(context, request.agent_id)
            result = generated_python_approval_request(context.record,
                agent_id=request.agent_id, idempotency_key=request.idempotency_key)
            _need(result is not None)
            assert result is not None
            return result
        except ProjectVerificationError:
            raise
        except ProcessCleanupIncomplete:
            raise
        except Exception as error:  # noqa: BLE001 - backend failures expose only finite codes.
            self._raise(error)

    async def _approved(self, request: CapabilityRequest, code: str) -> None:
        checker = getattr(self.owner.repository, 'is_capability_approval_scope_approved', None)
        scope = capability_approval_scope(request)
        _need(callable(checker) and type(scope) is str, code)
        assert callable(checker)
        value = checker(request.tenant_id, request.run_id, scope)
        approved = await cast(Awaitable[object], value) if isawaitable(value) else value
        _need(approved is True, code)

    async def _supply(self, context: GeneratedProjectContext, actor: str) -> GeneratedProjectContext:
        _need(actor == context.envelope.proposer_actor, 'input_supply_scope_changed')
        current = context

        async def state_io(*, expected: Mapping[str, JsonValue] | None = None,
                           replace: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue] | None:
            nonlocal current
            current = await self._current(current, actor, exact_state=True)
            state = cast(Mapping[str, JsonValue] | None, current.state.get(_SUPPLY))
            if replace is not None:
                _need(state == expected, 'input_supply_scope_changed')
                current = await self.generated.update(current, {**current.state, _SUPPLY: replace})
                return cast(Mapping[str, JsonValue], current.state[_SUPPLY])
            return state

        async def authorize(plan: PythonInputSupplyPlan, state: Mapping[str, JsonValue]) -> None:
            await self._current(current, actor, exact_state=True)
            _need(supply_state_plan(state) == plan, 'input_supply_scope_changed')
            await self._approved(input_supply_request(plan, user_id=current.envelope.user_id,
                idempotency_key='generated-supply-check').model_copy(update={'agent_id': actor}),
                'input_supply_approval_required')

        state = cast(Mapping[str, JsonValue], context.state[_SUPPLY])
        await authorize(supply_state_plan(state), state)
        await supply_python_inputs(client=self.supply_client, invocation=_probe(context, 'input_base_'),
            stored=context.stored, state_io=state_io, authorize=authorize,
            deadline=asyncio.get_running_loop().time() + context.invocation.timeout_seconds)
        return current

    async def verify(self, context: GeneratedProjectContext, actor: str) -> Mapping[str, JsonValue]:
        key = (context.invocation.tenant_id, context.invocation.run_id)
        self.receipts.pop(key, None)
        try:
            context = await self._current(context, actor)
            if _SUPPLY in context.state and cast(Mapping[str, JsonValue], context.state[_SUPPLY])['phase'] != 'supplied':
                context = await self._supply(context, actor)
            context, inputs = await self._prepare(context, actor, fresh=True)
            approval = inputs.approval_request(user_id=context.envelope.user_id, agent_id=actor,
                idempotency_key='generated-python-recipe-check')
            try:
                await self._approved(approval, _RECIPE_REQUIRED)
            except ProjectVerificationError:
                raise PythonGeneratedRecipeApprovalRequired(approval) from None
            base = context.invocation.model_copy(update={
                'execution_id': 'generated_python_' + uuid4().hex, 'install_allowed': False})
            source = await project_source_fingerprint(self.owner.root, base)
            context.envelope.require_current(await project_source_manifest(self.owner.root, base))
            invocation = python_original_invocation(base.model_copy(update={'install_allowed': True}), inputs)
            invocation = invocation.model_copy(update={'source_kind': 'candidate'})
            await self._after(context, actor, approval, source)
            response = await self.executor.run(invocation)
            _need(type(response) is PythonProjectBrokerResponse)
            response = PythonProjectBrokerResponse.model_validate(response.model_dump(), strict=True)
            response.require_for(PythonProjectBrokerRequest(invocation=invocation))
            _need(response.ok and response.result is not None, response.error or _ERROR)
            assert response.result is not None
            report = decode_python_project_report(invocation=invocation, inputs=inputs,
                expected_source_sha256=source, environment_reference=response.environment_reference,
                result=response.result)
            await self._after(context, actor, approval, source)
            if report.environment_reference is not None:
                await asyncio.to_thread(self._persist_reference, context, inputs, report.environment_reference)
            if isinstance(report, PythonProjectFailure):
                raise ProjectVerificationError('python_project_checks_failed', report.diagnostic,
                    python_project_failure=report)
            proof = self._named_proof(context, response.result.stdout, report.report['test_count'])
            from agent_hub.capabilities.generated_project_service import (
                generated_verification_binding,
            )

            binding = generated_verification_binding(context, source, proof)
            await self._after(context, actor, approval, source)
            self.receipts[key] = _Receipt(context, invocation, inputs, source,
                report.environment_reference, actor, binding)
            return {'verification': {**report.to_payload(),
                    'generated_acceptance_sha256': context.envelope.sha256},
                'generated_project_acceptance': binding,
                'python_recipe': python_recipe_marker(inputs),
                'python_environment_reference': report.environment_reference.model_dump(mode='json'),
                'summary': 'Generated acceptance preserved; Python build and named tests verified in isolation.'}
        except (ProjectVerificationError, ProcessCleanupIncomplete):
            raise
        except Exception as error:  # noqa: BLE001 - no arbitrary native text becomes public evidence.
            self._raise(error)

    async def _after(self, context: GeneratedProjectContext, actor: str,
                     approval: CapabilityRequest, source: str) -> None:
        current = await self._current(context, actor, exact_state=True)
        _need(await self._inputs(current) == await self._inputs(context))
        await self._approved(approval, _RECIPE_REQUIRED)
        context.envelope.require_current(await project_source_manifest(self.owner.root, context.invocation))
        _need(await project_source_fingerprint(self.owner.root, context.invocation) == source,
              'python_project_source_changed')
        await self._current(context, actor, exact_state=True)

    @staticmethod
    def _named_proof(context: GeneratedProjectContext, stdout: str,
                     count: JsonValue) -> Mapping[str, object]:
        from agent_hub.skills.sandbox.python_project_phase import (
            validate_generated_python_test_execution,
        )

        # Only called after the real decoder has bound the full raw response.
        try:
            raw = json.loads(stdout, object_pairs_hook=_strict_object)
            proof = validate_generated_python_test_execution(raw.get('generated_test_execution'))
        except (ValueError, TypeError, AttributeError):
            raise ProjectVerificationError('generated_acceptance_test_execution_invalid') from None
        _need(proof.get('manifest_sha256') == context.state['manifest_raw_sha256']
              and proof.get('names') == context.envelope.manifest.test_ids
              and proof.get('count') == count
              and type(count) is int and count == len(context.envelope.manifest.test_ids),
              'generated_acceptance_test_execution_invalid')
        return proof

    def _persist_reference(self, context: GeneratedProjectContext, inputs: PythonProjectInputs,
                           reference: PythonEnvironmentReference) -> None:
        _need(_reference_matches(python_original_invocation(context.invocation, inputs), reference))
        store = ProjectBaselineStore(self.owner.root)
        name = _PREFIX + reference.origin_execution_id + '.json'
        data = _encoded(reference.model_dump(mode='json'))
        with ExitStack() as stack:
            controls = store._controls(context.invocation, stack, create=False)
            references = self._read_references(context, inputs)
            _need(reference in references or len(references) < 128)
            try:
                store._write_once(controls, name, data)
            except FileExistsError:
                _need(controls.read(name, 256 * 1024) == data)
            controls.check()

    def _read_references(self, context: GeneratedProjectContext,
                         inputs: PythonProjectInputs) -> tuple[PythonEnvironmentReference, ...]:
        result: list[PythonEnvironmentReference] = []
        with ExitStack() as stack:
            controls = ProjectBaselineStore(self.owner.root)._controls(context.invocation, stack, create=False)
            with os.scandir(controls.path) as members:
                names = sorted(member.name for member in members if member.name.startswith(_PREFIX))
            _need(len(names) <= 128)
            for name in names:
                raw = controls.read(name, 256 * 1024)
                payload = json.loads(raw, object_pairs_hook=_strict_object)
                reference = PythonEnvironmentReference.model_validate_json(_encoded(payload), strict=True)
                _need(name == _PREFIX + reference.origin_execution_id + '.json'
                      and _encoded(reference.model_dump(mode='json')) == raw
                      and _reference_matches(python_original_invocation(context.invocation, inputs), reference))
                result.append(reference)
            controls.check()
        return tuple(result)

    async def environment_context(self, tenant: UUID, run: UUID) -> PythonGeneratedEnvironmentContext | None:
        """Recover generated issuance from trusted storage, without probes or grants."""
        context = await self.generated.load(tenant, run, 'main_agent', readonly=True)
        if context is None or context.envelope.manifest.stack != 'python' or context.state['phase'] != 'python_ready':
            return None
        inputs = await self._inputs(context)
        references = await asyncio.to_thread(self._read_references, context, inputs)
        invocation = python_original_invocation(context.invocation, inputs).model_copy(
            update={'source_kind': 'candidate', 'install_allowed': False})
        return PythonGeneratedEnvironmentContext(invocation, references)

    async def environment_references(self, tenant: UUID, run: UUID) -> tuple[PythonEnvironmentReference, ...]:
        """Readonly durable issuance for terminal recovery; no execution or fresh approval."""
        context = await self.environment_context(tenant, run)
        return () if context is None else context.environment_references

    async def require_current(self, tenant: UUID, run: UUID) -> str:
        key = (tenant, run)
        try:
            receipt = self.receipts[key]
            context = await self._current(receipt.context, receipt.actor, exact_state=True)
            inputs = await self._inputs(context)
            _need(inputs == receipt.inputs)
            approval = inputs.approval_request(user_id=context.envelope.user_id, agent_id=receipt.actor,
                idempotency_key='generated-python-delivery-check')
            await self._after(context, receipt.actor, approval, receipt.source)
            invocation = PythonProjectInvocation.model_validate({**receipt.invocation.model_dump(),
                'execution_id': 'python_inspect_' + uuid4().hex, 'install_allowed': False}, strict=True)
            actual = await self.executor.inspect_environment(invocation, expected_environment=receipt.reference)
            _need(type(actual) is PythonEnvironmentReference and actual == receipt.reference)
            _need(receipt.reference in await asyncio.to_thread(self._read_references, context, inputs))
            await self._after(context, receipt.actor, approval, receipt.source)
            return receipt.source
        except (asyncio.CancelledError, ProcessCleanupIncomplete):
            self.receipts.pop(key, None)
            raise
        except Exception:  # noqa: BLE001 - stale or revoked receipts cannot authorize delivery.
            self.receipts.pop(key, None)
            raise ProjectVerificationError('project verification required before delivery') from None

    @staticmethod
    def _raise(error: Exception) -> NoReturn:
        if str(error) in get_args(SupplyError) or str(error) == 'input_supply_approval_required':
            raise ProjectVerificationError(str(error)) from None
        PythonProjectService._raise_backend(error)
