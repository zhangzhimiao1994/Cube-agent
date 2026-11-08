"""Pure V3 role hints from admitted engineering history, never tool authority.

The context is supplied by the worker after preparation. This module performs no
I/O, captures no baseline and cannot replace runtime/lease/approval validation.
Unbound explicit inputs conservatively retain ordinary text-based planning.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from agent_hub.auth.models import Role
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.role_intent import role_planning_intent_view

if TYPE_CHECKING:
    from agent_hub.runtime.role_planner import TaskProfile

_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RuntimeError,
           RecursionError, OverflowError)
_ORIGINAL = {'project_input', 'project_input_contract_version',
             'project_input_preparation', 'python_project_history'}
_GENERATED = {'generated_project_contract_version', 'generated_project_preparation'}


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _need(value: object) -> None:
    if not value:
        raise ValueError('engineering role context unavailable')


def _writing(request: str) -> bool:
    from agent_hub.runs.automatic_project_inputs import maintenance_intent

    text = role_planning_intent_view(request, version=3).strip()
    if maintenance_intent(text):
        return True
    # The verb supplies write intent, not a software domain or permission grant.
    first = re.split(r'[。；;\n!?！？]', text, maxsplit=1)[0]
    return (re.search(r'[？?]|如果|假如|仅分析|只分析|仅讨论|只讨论|只给方案|仅给方案', first) is None
        and re.match(r'^(?:请你?\s*)?(?:给|为)[^。；;\n!?！？？?]{1,120}?'
                     r'(?:增加|新增|添加|修改|修复|实现|支持)', first) is not None)


def _original(context: TaskContext) -> bool:
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.project_contracts import ManagedProjectInput
    from agent_hub.capabilities.project_verification import validated_original_observation
    from agent_hub.runs.automatic_project_inputs import saved_selection

    route = context.routing_decision
    _need(not _GENERATED.intersection(route)
        and type(route.get('project_input_contract_version')) is int
        and route['project_input_contract_version'] == 1
        and type(route.get('automatic_project_input_version')) is int
        and route['automatic_project_input_version'] == 1)
    selected = saved_selection(context, route.get('automatic_project_input_selection'))
    selection = ManagedProjectInput.from_payload(route.get('project_input'))
    state = route.get('project_input_preparation')
    _need(isinstance(state, Mapping))
    assert isinstance(state, Mapping)
    reference = ProjectBaselineReference.from_payload(state.get('reference'))
    _need(selected['kind'] == 'original' and selected['source_sha256'] == reference.input_source_sha256
        and selected['editable_paths'] == selection.editable_paths and state.get('phase') == 'observed'
        and state.get('observation_sha256') == hashlib.sha256(_encoded(state.get('observation'))).hexdigest())
    artifacts = [a for a in context.artifacts if a.type == 'original_project_observation']
    _need(len(artifacts) == 1)
    artifact = artifacts[0]
    content = artifact.content
    protected = content.get('protected_sha256')
    _need(artifact.producer == 'original_project_preparer'
        and artifact.content_sha256 == artifact.recompute_content_sha256()
        and content.get('observation') == state.get('observation')
        and content.get('original_source_sha256') == reference.input_source_sha256
        and content.get('editable_paths') == selection.editable_paths
        and type(protected) is str and re.fullmatch('[a-f0-9]{64}', protected) is not None)
    if type(state.get('schema_version')) is int and state['schema_version'] == 2:
        from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
        from agent_hub.runtime.python_project_history import _history
        from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation

        history = _history(context)
        metadata = [a for a in context.artifacts if a.type == 'original_python_project_context']
        _need(len(metadata) == 1)
        typed = metadata[0]
        _need(typed.producer == 'original_project_preparer'
            and typed.source_ids == (str(artifact.id),)
            and typed.content_sha256 == typed.recompute_content_sha256())
        data = typed.content
        inputs = PythonProjectInputs.from_payload(json.loads(_encoded(data.get('inputs'))))
        invocation = PythonProjectInvocation.model_validate_json(_encoded(data.get('invocation')), strict=True)
        _need((inputs.contract.tenant_id, inputs.contract.run_id, inputs.contract.project_id,
               inputs.contract.session_id) == (context.tenant_id, context.run_id,
               route.get('project_id'), route.get('workspace_session_id'))
            and inputs.original_reference == reference
            and inputs.contract.editable_paths == selection.editable_paths
            and inputs.contract.protected_sha256 == protected
            and history['runtime'] == inputs.interpreter.to_payload())
        observation = validated_original_observation(state.get('observation'),
            expected_source=reference.input_source_sha256, expected_contract=cast(str, protected),
            expected_python_inputs=inputs, expected_python_invocation=invocation)
        environment = observation.get('environment_reference')
        _need(_encoded(data) == _encoded({'schema_version': 1, 'kind': 'python_original_context',
            'inputs': inputs.to_payload(), 'invocation': invocation.model_dump(mode='json'),
            'original_reference': reference.to_payload(), 'environment_reference': environment,
            'expected_execution_id': invocation.execution_id,
            'expected_source_sha256': reference.input_source_sha256,
            'expected_contract_sha256': protected, 'expected_recipe_sha256': inputs.sha256,
            'expected_environment_input_sha256': inputs.environment_input_sha256,
            'expected_environment_sha256': environment.get('environment_sha256')
                if isinstance(environment, Mapping) else None}))
    else:
        _need(type(state.get('schema_version')) is int and state['schema_version'] == 1
            and 'python_project_history' not in route and 'python_recipe' not in state)
        from uuid import NAMESPACE_URL, uuid5

        # Only the current preparer attests the full baseline, not its source hash alone.
        _need(_encoded(content.get('baseline_reference')) == _encoded(reference.to_payload())
            and artifact.id == uuid5(NAMESPACE_URL,
                f'original-project:{context.tenant_id}:{context.run_id}:{state["observation_sha256"]}'))
        observation = validated_original_observation(state.get('observation'),
            expected_source=reference.input_source_sha256, expected_contract=cast(str, protected))
    if state['schema_version'] == 2:
        _need(observation.get('execution_id') == state.get('execution_id'))
    return True


def _generated(context: TaskContext) -> bool:
    from agent_hub.capabilities.generated_project_service import validate_generated_preparation
    from agent_hub.runtime.generated_project_history import generated_success_source

    route = context.routing_decision
    _need(not _ORIGINAL.intersection(route)
        and type(route.get('generated_project_contract_version')) is int
        and route['generated_project_contract_version'] == 1)
    state = validate_generated_preparation(route.get('generated_project_preparation'))
    # Node verify history does not attest the complete captured baseline reference.
    # Retain text-based planning until that native proof exists; never infer it.
    if state['stack'] != 'python':
        return False
    _need(state['phase'] in ('captured', 'python_ready')
        and state['user_id'] == str(context.actor_id)
        and state['request_sha256'] == hashlib.sha256(context.request.encode('utf-8')).hexdigest())
    for artifact in context.artifacts:
        content = artifact.content
        result = content.get('result')
        if (artifact.type != 'tool_result' or content.get('tool_name') != 'project.verify'
                or content.get('agent_id') != artifact.producer or not isinstance(result, Mapping)
                or artifact.content_sha256 != artifact.recompute_content_sha256()):
            continue
        source = generated_success_source(context, result)
        binding = result.get('generated_project_acceptance')
        if source is None or not isinstance(binding, Mapping):
            continue
        if all(state[key] == binding[key] for key in ('user_id', 'request_sha256', 'stack',
                'acceptance_sha256', 'manifest_sha256', 'manifest_raw_sha256')):
            if state['stack'] == 'python':
                from agent_hub.skills.sandbox.python_project_protocol import (
                    PythonEnvironmentReference,
                    PythonEphemeralEnvironmentEvidence,
                )

                reference: PythonEnvironmentReference | PythonEphemeralEnvironmentEvidence
                if 'python_ephemeral_environment' in result:
                    from agent_hub.runtime.generated_project_history import _root_ephemeral

                    reference = _root_ephemeral(context, result['python_ephemeral_environment'])
                else:
                    reference = PythonEnvironmentReference.model_validate_json(
                        _encoded(result.get('python_environment_reference')), strict=True)
                if (state.get('python_recipe') != result.get('python_recipe')
                        or reference.scope.original_reference.model_dump() != state['reference']):
                    continue
            return True
    return False


def engineering_software_context(context: TaskContext) -> bool:
    """Return a domain hint only for V3 with intact, scoped write preparation."""
    from agent_hub.runs.automatic_project_inputs import eligible

    try:
        route = context.routing_decision
        if not (type(route.get('role_planning_intent_version')) is int
                and route['role_planning_intent_version'] == 3
                and context.actor_id is not None and isinstance(context.actor_role, Role)
                and context.actor_role is not Role.VIEWER and eligible(route)
                and route.get('project_delivery') == 'workspace'
                and route.get('artifact_strategy') == 'workspace_bundle'
                and _writing(context.request)):
            return False
        if _ORIGINAL.intersection(route):
            return _original(context)
        return _generated(context)
    except _ERRORS:
        return False


def engineering_role_context(context: TaskContext) -> TaskProfile | None:
    from agent_hub.runtime.role_planner import TaskProfile

    return TaskProfile.SOFTWARE if engineering_software_context(context) else None


def engineering_role_profiles(
    context: TaskContext, profiles: tuple[TaskProfile, ...],
) -> tuple[TaskProfile, ...]:
    from agent_hub.runtime.role_planner import TaskProfile

    if not engineering_software_context(context):
        return profiles
    return (TaskProfile.SOFTWARE, *(p for p in dict.fromkeys(profiles)
            if p not in (TaskProfile.SOFTWARE, TaskProfile.GENERAL)), TaskProfile.GENERAL)
