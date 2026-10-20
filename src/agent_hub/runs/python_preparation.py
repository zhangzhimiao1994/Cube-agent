"""Strict Python-only extension of the normal pre-model service boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping

from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_service import (
    python_recipe_marker,
    validate_python_original_observation,
)
from agent_hub.runtime.contracts import TaskContext, _freeze_object
from agent_hub.runtime.python_project_history import _history
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation


def validate_python_preparation(context: TaskContext) -> None:
    """Validate derived history; the service separately checks the locked DB state."""
    try:
        history = _history(context)
        state = context.routing_decision['project_input_preparation']
        if not isinstance(state, Mapping) or set(state) != {
            'schema_version', 'phase', 'reference', 'python_recipe', 'execution_id',
            'observation', 'observation_sha256',
        }:
            raise ValueError
        originals = [item for item in context.artifacts if str(item.id) == history['artifact_id']]
        metadata = [item for item in context.artifacts if item.type == 'original_python_project_context']
        if len(originals) != 1 or len(metadata) != 1:
            raise ValueError
        original, artifact = originals[0], metadata[0]
        if (artifact.producer != 'original_project_preparer'
                or artifact.source_ids != (str(original.id),)
                or artifact.content_sha256 != artifact.recompute_content_sha256()):
            raise ValueError
        value = artifact.content
        inputs = PythonProjectInputs.from_payload(
            json.loads(json.dumps(value.get('inputs'), default=dict, allow_nan=False)),
        )
        invocation = PythonProjectInvocation.model_validate_json(
            json.dumps(value.get('invocation'), default=dict, allow_nan=False), strict=True,
        )
        if (invocation.tenant_id != context.tenant_id or invocation.run_id != context.run_id
                or invocation.project_id != context.routing_decision.get('project_id')
                or invocation.session_id != context.routing_decision.get('workspace_session_id')
                or state['reference'] != inputs.original_reference.to_payload()
                or state['python_recipe'] != python_recipe_marker(inputs)
                or state['execution_id'] != invocation.execution_id):
            raise ValueError
        observation = validate_python_original_observation(
            state['observation'], inputs=inputs, invocation=invocation,
            expected_source=inputs.original_reference.input_source_sha256,
            expected_contract=inputs.contract.protected_sha256,
        )
        reference = observation['environment_reference']
        environment = None
        if reference is not None:
            if not isinstance(reference, Mapping):
                raise ValueError
            environment = reference['environment_sha256']
        expected = _freeze_object({
            'schema_version': 1, 'kind': 'python_original_context',
            'inputs': inputs.to_payload(), 'invocation': invocation.model_dump(mode='json'),
            'original_reference': inputs.original_reference.to_payload(),
            'environment_reference': reference,
            'expected_execution_id': invocation.execution_id,
            'expected_source_sha256': inputs.original_reference.input_source_sha256,
            'expected_contract_sha256': inputs.contract.protected_sha256,
            'expected_recipe_sha256': inputs.sha256,
            'expected_environment_input_sha256': inputs.environment_input_sha256,
            'expected_environment_sha256': environment,
        }, name='Python preparation metadata')
        if value != expected or original.content.get('observation') != observation:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise RuntimeError('original project preparation is unavailable or changed') from None
