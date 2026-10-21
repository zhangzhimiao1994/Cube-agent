"""Historical generated evidence: real service validation, fake Root, no native claim."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.capabilities.test_generated_project_service import USER
from tests.unit.capabilities.test_python_generated_service import approve, configured, request


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=dict, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


async def evidence(tmp_path: Path) -> tuple[TaskContext, Mapping[str, JsonValue], dict[str, JsonValue]]:
    service, _, repo, _, _, generated = await configured(
        importlib.import_module('agent_hub.capabilities.python_generated_service'), tmp_path)
    exact = await service.authorization_request(request(), generated)
    approve(repo, exact)
    result = await service.verify(generated, 'engineer')
    context = TaskContext(tenant_id=repo.row.tenant_id, run_id=repo.row.id,
        actor_id=USER, actor_role=repo.row.actor_role, mode=TaskMode.DIRECT,
        request=repo.row.request, routing_decision={
            'generated_project_contract_version': 1,
            'project_id': generated.invocation.project_id,
            'workspace_session_id': generated.invocation.session_id,
            'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle'})
    receipt = cast(Mapping[str, JsonValue], result['verification'])
    bundle: dict[str, JsonValue] = {
        'verification_source_sha256': receipt['source_sha256'],
        'verification_receipt_sha256': digest(receipt),
        'generated_project_acceptance': result['generated_project_acceptance'],
        'presentation': 'final_attachment', 'file': {'filename': 'project.zip'},
    }
    return context, result, bundle


async def test_service_evidence_without_refreshed_route_state(tmp_path: Path) -> None:
    context, result, bundle = await evidence(tmp_path)
    api = importlib.import_module('agent_hub.runtime.generated_project_history')
    assert 'generated_project_preparation' not in context.routing_decision
    assert api.generated_success_source(context, result) == bundle['verification_source_sha256']
    assert api.generated_delivery_matches(context, result, bundle)
    from agent_hub.runtime.original_project_progress import original_failure_observed
    assert not original_failure_observed(context)


@pytest.mark.parametrize('defect', ['marker', 'request', 'user', 'scope', 'acceptance', 'names',
    'source', 'rootref', 'recipe', 'receipt', 'bundle-binding', 'bundle-receipt', 'mixed-original'])
async def test_invalid_or_cross_bound_evidence_cannot_complete(tmp_path: Path, defect: str) -> None:
    context, original, bundle = await evidence(tmp_path)
    result: Any = json.loads(json.dumps(original, default=dict))
    if defect == 'marker':
        context = context.model_copy(update={'routing_decision': {}})
    elif defect == 'request':
        context = context.model_copy(update={'request': 'a different user request'})
    elif defect == 'user':
        context = context.model_copy(update={'actor_id': None})
    elif defect == 'mixed-original':
        context = context.model_copy(update={'routing_decision': {
            **context.routing_decision, 'project_input_contract_version': 1}})
    elif defect == 'bundle-binding':
        bundle['generated_project_acceptance'] = {
            **cast(Mapping[str, JsonValue], bundle['generated_project_acceptance']),
            'acceptance_sha256': 'f' * 64}
    elif defect == 'bundle-receipt':
        bundle['verification_receipt_sha256'] = 'f' * 64
    elif defect == 'rootref':
        result.pop('python_environment_reference')
    elif defect == 'recipe':
        result['python_recipe']['recipe_sha256'] = 'f' * 64
    elif defect == 'receipt':
        result['verification']['environment_sha256'] = 'f' * 64
    else:
        binding = result['generated_project_acceptance']
        if defect == 'scope':
            binding['run_id'] = '00000000-0000-4000-8000-000000000000'
        elif defect == 'acceptance':
            binding['acceptance_sha256'] = 'f' * 64
        elif defect == 'source':
            binding['source_sha256'] = 'f' * 64
        else:
            binding['test_execution']['names'] = ['test_other.OtherTests.test_other']
    api = importlib.import_module('agent_hub.runtime.generated_project_history')
    assert not api.generated_delivery_matches(context, result, bundle)


def test_direct_marker_requires_workspace_delivery_not_ordinary_chat() -> None:
    from uuid import uuid4

    from agent_hub.runtime.direct import _is_workspace_project_delivery

    context = TaskContext(tenant_id=uuid4(), run_id=uuid4(), request='chat', mode=TaskMode.DIRECT,
        routing_decision={'generated_project_contract_version': 1})
    assert not _is_workspace_project_delivery(context)
    context = context.model_copy(update={'routing_decision': {**context.routing_decision,
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle'}})
    assert _is_workspace_project_delivery(context)


@pytest.mark.parametrize('mode', ['crew', 'autogen'])
@pytest.mark.parametrize('defect', [None, 'binding', 'hash', 'actor', 'order'])
async def test_mode_delivery_uses_trusted_causal_results_and_exact_binding(
    tmp_path: Path, mode: str, defect: str | None,
) -> None:
    context, result, bundle = await evidence(tmp_path)
    from tests.unit.runtime.autogen.test_engineering_delivery import (
        TOOLS,
        replace_result,
    )
    from tests.unit.runtime.autogen.test_engineering_delivery import fixture as auto_fixture
    from tests.unit.runtime.crew.test_engineering_delivery_gate import change_result
    from tests.unit.runtime.crew.test_engineering_delivery_gate import fixture as crew_fixture

    if defect == 'binding':
        bundle['generated_project_acceptance'] = {
            **cast(Mapping[str, JsonValue], bundle['generated_project_acceptance']),
            'acceptance_sha256': 'e' * 64}
    if mode == 'crew':
        data = crew_fixture()
        change_result(data, '1', result)
        original = cast(Mapping[str, JsonValue], data[1]['2'].content['result'])
        change_result(data, '2', {**original, **bundle})
        if defect == 'hash':
            data[0]['1'] = {**data[0]['1'], 'sha256': 'f' * 64}
        elif defect == 'actor':
            data[0]['1'] = {**data[0]['1'], 'arguments_sha256': 'f' * 64}
        elif defect == 'order':
            data[0]['1'] = {**data[0]['1'], 'round': 3}
        module = importlib.import_module('agent_hub.runtime.crew.engineering_delivery')
        ready = module.engineering_delivery_ready(step_id='build', actor='implementer',
            states=data[0], artifacts=data[1], context=context)
    else:
        rows, artifacts = auto_fixture()
        for position, row in enumerate(rows):
            row['idempotency_key'] = hashlib.sha256(
                f"{context.run_id}:engineer:{position}:{row['name']}:{row['arguments_hash']}".encode()).hexdigest()
        replace_result(rows, artifacts, 1, result)
        original = cast(Mapping[str, JsonValue], artifacts[2].content['result'])
        replace_result(rows, artifacts, 2, {**original, **bundle})
        if defect == 'hash':
            rows[1]['artifact_sha256'] = 'f' * 64
        elif defect == 'actor':
            rows[1]['actor'] = 'outsider'
        elif defect == 'order':
            rows[1]['position'] = 3
        module = importlib.import_module('agent_hub.runtime.autogen.engineering_delivery')
        ready = module.engineering_delivery_ready(run_id=context.run_id,
            tool_entries=rows, artifacts=artifacts, allowed_tools=TOOLS, context=context)
    assert ready is (defect is None)


@pytest.mark.parametrize('defect', [None, 'binding', 'call', 'truncated'])
async def test_direct_live_verify_then_bundle_and_terminal_proof(tmp_path: Path, defect: str | None) -> None:
    import asyncio
    from uuid import uuid4

    from agent_hub.harness.types import HarnessToolCallResult
    from agent_hub.runtime.contracts import Artifact
    from agent_hub.runtime.direct import (
        DirectRuntime,
        _original_project_terminal_artifact_matches,
        _WorkspaceVerificationError,
    )

    context, result, bundle = await evidence(tmp_path)

    class Gateway:
        async def invoke(self, tenant: Any, request: Any, **kw: Any) -> HarnessToolCallResult:
            payload = result if request.tool_name == 'project.verify' else bundle
            if request.tool_name == 'workspace.bundle' and defect == 'binding':
                payload = {**bundle, 'generated_project_acceptance': {
                    **cast(Mapping[str, JsonValue], bundle['generated_project_acceptance']),
                    'acceptance_sha256': 'e' * 64}}
            return HarnessToolCallResult(call_id='wrong' if defect == 'call' else request.call_id,
                tool_name=request.tool_name, status='succeeded', payload=payload,
                truncated=defect == 'truncated')

    runtime = DirectRuntime(cast(Any, object()), logical_model='fixture',
        harness_tool_gateway=cast(Any, Gateway()))

    async def execute(*args: Any, **kw: Any) -> Mapping[str, JsonValue]:
        if kw['name'] == 'workspace.bundle':
            payload = bundle
            if defect == 'binding':
                payload = {**bundle, 'generated_project_acceptance': {
                    **cast(Mapping[str, JsonValue], bundle['generated_project_acceptance']),
                    'acceptance_sha256': 'e' * 64}}
            return payload
        assert kw['name'] == 'workspace.write_text'  # Never automatic prune after capture.
        return {}

    runtime._execute_workspace_capability = execute  # type: ignore[method-assign]
    call = runtime._finish_workspace_delivery(context, known_files={},
        deadline=asyncio.get_running_loop().time() + 10)
    if defect is not None:
        with pytest.raises(_WorkspaceVerificationError):
            await call
    else:
        delivery = await call
        assert delivery['python_environment_reference'] == result['python_environment_reference']
        artifact = Artifact(id=uuid4(), type='tool_result', producer='main',
            content={'workspace_delivery': delivery})
        assert _original_project_terminal_artifact_matches(context, artifact)


@pytest.mark.parametrize('mode', ['direct', 'hybrid'])
async def test_generated_terminal_replay_cannot_bypass_missing_evidence(mode: str) -> None:
    from agent_hub.runtime.contracts import EventKind
    from tests.unit.runtime.test_original_project_terminal_replay import replay

    configured = await replay(mode, 'missing_proof')
    configured.context = configured.context.model_copy(update={'routing_decision': {
        'generated_project_contract_version': 1}})
    events = await configured.events()
    configured.assert_no_execution()
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


async def test_node_binding_selects_node_validator_without_python_receipt_dispatch(tmp_path: Path) -> None:
    from agent_hub.capabilities.generated_project_service import generated_verification_binding
    from tests.unit.capabilities.test_generated_project_service import setup
    from tests.unit.capabilities.test_scoped_read import RUN, TENANT
    from tests.unit.runtime.crew.test_engineering_delivery_gate import fixture

    service, repo = setup(tmp_path)
    generated = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert generated is not None
    context = TaskContext(run_id=RUN, tenant_id=TENANT, actor_id=USER,
        actor_role=repo.row.actor_role, request=repo.row.request, mode=TaskMode.DISPATCH,
        routing_decision={'generated_project_contract_version': 1, 'project_id': 'project-a',
            'workspace_session_id': 'session-a'})
    receipt = cast(Mapping[str, JsonValue], fixture()[1]['1'].content['result'])['verification']
    assert isinstance(receipt, Mapping)
    proof = {'schema_version': 1, 'manifest_sha256': generated.state['manifest_raw_sha256'],
        'count': 1, 'names': ['adds two numbers'],
        'test_evidence_sha256': digest({'schema_version': 1, 'passed_tests': ['adds two numbers']})}
    binding = generated_verification_binding(generated, cast(str, receipt['source_sha256']), proof)
    result = {'verification': {**receipt, 'generated_acceptance_sha256': generated.envelope.sha256},
        'generated_project_acceptance': binding}
    api = importlib.import_module('agent_hub.runtime.generated_project_history')
    assert api.generated_success_source(context, result) == receipt['source_sha256']
    bad = {**result, 'generated_project_acceptance': {**binding, 'stack': 'python'}}
    assert api.generated_success_source(context, bad) is None


def test_direct_shared_guidance_and_context_clone_keep_marker() -> None:
    from uuid import uuid4

    from agent_hub.runtime.direct import DirectRuntime
    from agent_hub.runtime.generated_project_guidance import generated_project_guidance

    context = TaskContext(tenant_id=uuid4(), run_id=uuid4(), request='Build a project', mode=TaskMode.DIRECT,
        routing_decision={'generated_project_contract_version': 1, 'project_delivery': 'workspace',
            'artifact_strategy': 'workspace_bundle'})
    clone = context.validated_internal_clone()
    assert clone.routing_decision['generated_project_contract_version'] == 1
    runtime = DirectRuntime(cast(Any, object()), logical_model='fixture')
    prompt = runtime._build_prompt(clone)
    assert prompt.error_code is None and prompt.messages is not None
    assert any(message.role == 'system' and generated_project_guidance(clone) in message.content
        for message in prompt.messages)


@pytest.mark.parametrize('mode', ['crew', 'autogen'])
async def test_generated_failure_tail_survives_adapter_feedback(tmp_path: Path, mode: str) -> None:
    from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
    from agent_hub.capabilities.project_verification import ProjectVerificationError
    from agent_hub.skills.sandbox.base import SkillResult

    service, _, repo, executor, _, generated = await configured(
        importlib.import_module('agent_hub.capabilities.python_generated_service'), tmp_path)
    approve(repo, await service.authorization_request(request(), generated))
    executor.failure_index = 2
    with pytest.raises(ProjectVerificationError) as caught:
        await service.verify(generated, 'engineer')
    failure = caught.value.python_project_failure
    assert failure is not None and failure.environment_reference is not None
    diagnostic = ExecutionDiagnostic(backend='systemd', result=SkillResult(exit_code=1, timed_out=False,
        stdout='progress\n' * 2000 + 'ActualFailure: totals are wrong\n', stderr=''), retain_tail=True)
    report: Any = json.loads(json.dumps(failure.to_payload(), default=dict))
    report['checks'][-1]['execution_diagnostic'] = {
        key: value for key, value in diagnostic.to_payload().items() if key not in {'stdout', 'stderr'}}
    payload = {'python_verification_failure': report, 'execution_diagnostic': diagnostic.to_payload(),
        'python_environment_reference': failure.environment_reference.model_dump(mode='json')}
    context = TaskContext(run_id=repo.row.id, tenant_id=repo.row.tenant_id,
        actor_id=USER, actor_role=repo.row.actor_role, request=repo.row.request, mode=TaskMode.DIRECT,
        routing_decision={'generated_project_contract_version': 1, 'project_id': 'project-a',
            'workspace_session_id': 'session-a'})
    feedback = importlib.import_module(f'agent_hub.runtime.{mode}.adapter')._execution_failure_result(
        payload, tool_name='project.verify')
    assert feedback is not None
    assert 'ActualFailure' in cast(Mapping[str, str], feedback['execution_diagnostic'])['stdout']
    api = importlib.import_module('agent_hub.runtime.generated_project_history')
    assert api.generated_failure_current(context, feedback)
