"""A failed pre-model baseline is diagnosis, never successful delivery credit."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.generated_project_history import (
    generated_binding_matches,
    generated_history_selected,
)
from agent_hub.runtime.python_project_history import (
    python_binding_matches,
    python_history_selected,
    python_original_failed,
)


def python_project_inspection_declared(result: Mapping[str, JsonValue]) -> bool:
    """Advisory prompt state only, never validator selection or delivery credit."""
    advisory = result.get('python_recipe')
    if (not isinstance(advisory, Mapping)
            or advisory != {'kind': 'python_offline_wheel_v1', 'status': 'declared'}):
        return False
    manifests = result.get('manifests')
    if not isinstance(manifests, tuple | list):
        return False
    observed: set[str] = set()
    for item in manifests:
        if not isinstance(item, Mapping):
            continue
        path = item.get('path')
        if not isinstance(path, str) or path not in {'pyproject.toml', 'requirements.lock'}:
            continue
        digest, size = item.get('sha256'), item.get('observed_bytes')
        if (path in observed or item.get('status') != 'observed'
                or type(digest) is not str or re.fullmatch('[a-f0-9]{64}', digest) is None
                or type(size) is not int or not 0 < size < 2**63):
            return False
        observed.add(path)
    return observed == {'pyproject.toml', 'requirements.lock'}


def original_failure_observed(context: TaskContext) -> bool:
    if generated_history_selected(context):
        return False
    if python_history_selected(context):
        return python_original_failed(context)
    route = context.routing_decision
    if (type(route.get('project_input_contract_version')) is not int
            or route.get('project_input_contract_version') != 1):
        return False
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.project_contracts import ManagedProjectInput
    from agent_hub.capabilities.project_verification import validated_original_observation

    try:
        ManagedProjectInput.from_payload(route.get('project_input'))
        state = route.get('project_input_preparation')
        if (not isinstance(state, Mapping) or type(state.get('schema_version')) is not int
                or state['schema_version'] != 1 or state.get('phase') != 'observed'):
            return False
        reference = ProjectBaselineReference.from_payload(state.get('reference'))
        observation = state.get('observation')
        if not isinstance(observation, Mapping) or observation.get('status') != 'failed':
            return False
        raw = json.dumps(observation, default=dict, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False).encode('ascii')
        if hashlib.sha256(raw).hexdigest() != state.get('observation_sha256'):
            return False
        binding = observation.get('original_check_contract')
        if not isinstance(binding, Mapping):
            return False
        digest = binding.get('contract_sha256')
        if type(digest) is not str or re.fullmatch('[a-f0-9]{64}', digest) is None:
            return False
        validated_original_observation(observation, expected_source=reference.input_source_sha256,
                                       expected_contract=digest)
        return True
    except (TypeError, ValueError, RuntimeError, RecursionError):
        return False


def original_contract_binding_matches(
    context: TaskContext | None, result: Mapping[str, JsonValue], source: str,
) -> bool:
    if generated_history_selected(context):
        assert context is not None
        return generated_binding_matches(context, result, source)
    if 'generated_project_acceptance' in result:
        return False
    if context is None:
        return True
    if python_history_selected(context):
        return python_binding_matches(context, result, source)
    route = context.routing_decision
    if 'project_input_contract_version' not in route and 'project_input' not in route:
        return True
    if (type(route.get('project_input_contract_version')) is not int
            or route.get('project_input_contract_version') != 1
            or type(source) is not str or re.fullmatch('[a-f0-9]{64}', source) is None):
        return False
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.project_contracts import ManagedProjectInput
    from agent_hub.capabilities.project_verification import validated_original_observation

    try:
        ManagedProjectInput.from_payload(route.get('project_input'))
        state = route.get('project_input_preparation')
        if (not isinstance(state, Mapping) or type(state.get('schema_version')) is not int
                or state.get('schema_version') != 1 or state.get('phase') != 'observed'):
            return False
        reference = ProjectBaselineReference.from_payload(state.get('reference'))
        observation = state.get('observation')
        if not isinstance(observation, Mapping):
            return False
        baseline = observation.get('original_check_contract')
        if not isinstance(baseline, Mapping):
            return False
        protected = baseline.get('contract_sha256')
        if type(protected) is not str or re.fullmatch('[a-f0-9]{64}', protected) is None:
            return False
        observation = validated_original_observation(observation,
            expected_source=reference.input_source_sha256, expected_contract=protected)
        digest = hashlib.sha256(json.dumps(observation, default=dict, ensure_ascii=True,
            sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
        if state.get('observation_sha256') != digest:
            return False
        binding = result.get('original_check_contract')
        return bool(isinstance(binding, Mapping) and type(binding.get('schema_version')) is int
            and binding == {'schema_version': 1, 'contract_sha256': protected, 'protected_sha256': protected,
                'baseline_envelope_sha256': reference.contract_sha256,
                'baseline_source_sha256': reference.input_source_sha256,
                'baseline_observation_sha256': digest, 'candidate_source_sha256': source})
    except (ValueError, TypeError, RuntimeError, RecursionError):
        return False
