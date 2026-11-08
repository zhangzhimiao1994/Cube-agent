"""Pure post-model history, never approval, original-input or live ZIP authority.

Callers must first verify tool ownership, call/argument/content hashes and causal
order. The capability-issued binding is an attestation, not an independent source
measurement. No mutable preparation state or filesystem is consulted here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import cast

from agent_hub.capabilities.project_acceptance import _text
from agent_hub.capabilities.project_verification import (
    _validate_original_diagnostic,
    validated_project_failure_payload,
)
from agent_hub.capabilities.python_project_receipts import validate_python_passed_receipt
from agent_hub.capabilities.python_project_reports import (
    PythonEphemeralProjectReport,
    PythonProjectFailure,
)
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.python_project_history import python_failure_feedback
from agent_hub.skills.sandbox.python_project_phase import validate_generated_python_test_execution
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonEphemeralEnvironmentEvidence,
)

_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError)
_FIELDS = {'schema_version', 'kind', 'capture_phase', 'tenant_id', 'user_id', 'run_id',
    'project_id', 'session_id', 'request_sha256', 'acceptance_sha256', 'manifest_sha256',
    'manifest_raw_sha256', 'source_sha256', 'stack', 'test_execution'}


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')


def _need(value: object) -> None:
    if not value:
        raise ValueError('generated_project_history_invalid')


def _hash(value: object) -> str:
    _need(type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None)
    return cast(str, value)


def generated_history_selected(context: TaskContext | None) -> bool:
    """Presence selects fail-closed validation; malformed versions cannot fall back."""
    return context is not None and any(key in context.routing_decision for key in
        ('generated_project_contract_version', 'generated_project_preparation'))


def _context(context: TaskContext) -> None:
    route = context.routing_decision
    version = route.get('generated_project_contract_version')
    _need(type(version) is int and version == 1 and context.actor_id is not None
        and context.actor_role is not None
        and not {'project_input', 'project_input_contract_version', 'project_input_preparation',
                 'python_project_history'} & route.keys())


def _binding(context: TaskContext, result: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    _context(context)
    binding = result.get('generated_project_acceptance')
    _need(isinstance(binding, Mapping) and set(binding) == _FIELDS)
    assert isinstance(binding, Mapping)
    _need(len(_encoded(binding)) <= 256 * 1024
        and type(binding['schema_version']) is int and binding['schema_version'] == 1
        and binding['kind'] == 'generated_project_acceptance' and binding['capture_phase'] == 'post_model'
        and binding['stack'] in ('node', 'python'))
    expected = {'tenant_id': str(context.tenant_id), 'run_id': str(context.run_id),
        'user_id': str(context.actor_id), 'project_id': context.routing_decision.get('project_id'),
        'session_id': context.routing_decision.get('workspace_session_id'),
        'request_sha256': hashlib.sha256(context.request.encode('utf-8')).hexdigest()}
    _need(all(isinstance(value, str) and value and binding[key] == value
        for key, value in expected.items()))
    for name in ('request_sha256', 'acceptance_sha256', 'manifest_sha256',
                 'manifest_raw_sha256', 'source_sha256'):
        _hash(binding[name])
    proof = binding['test_execution']
    _need(isinstance(proof, Mapping) and set(proof) == {
        'schema_version', 'manifest_sha256', 'count', 'names', 'test_evidence_sha256'})
    assert isinstance(proof, Mapping)
    _need(type(proof['schema_version']) is int and proof['schema_version'] == 1
        and proof['manifest_sha256'] == binding['manifest_raw_sha256'])
    names = proof['names']
    _need(isinstance(names, tuple | list) and 1 <= len(names) <= 1000)
    assert isinstance(names, tuple | list)
    validated = tuple(_text(name, maximum=512, test_id=True) for name in names)
    _need(validated == tuple(sorted(validated)) and len({name.casefold() for name in validated}) == len(names)
        and type(proof['count']) is int and proof['count'] == len(names))
    if binding['stack'] == 'python':
        validate_generated_python_test_execution(proof)
    else:
        _need(_hash(proof['test_evidence_sha256']) == hashlib.sha256(_encoded({
            'schema_version': 1, 'passed_tests': validated})).hexdigest())
    return binding


def generated_binding_matches(
    context: TaskContext, result: Mapping[str, JsonValue], source: str,
) -> bool:
    try:
        return _binding(context, result)['source_sha256'] == _hash(source)
    except _ERRORS:
        return False


def _root_reference(context: TaskContext, value: object) -> PythonEnvironmentReference:
    ref = PythonEnvironmentReference.model_validate_json(_encoded(value), strict=True)
    _need(ref.scope.tenant_id == context.tenant_id and ref.scope.run_id == context.run_id
        and ref.scope.project_id == context.routing_decision.get('project_id')
        and ref.scope.session_id == context.routing_decision.get('workspace_session_id'))
    return ref


def _root_ephemeral(context: TaskContext, value: object) -> PythonEphemeralEnvironmentEvidence:
    evidence = PythonEphemeralEnvironmentEvidence.model_validate_json(_encoded(value), strict=True)
    _need(evidence.scope.tenant_id == context.tenant_id and evidence.scope.run_id == context.run_id
        and evidence.scope.project_id == context.routing_decision.get('project_id')
        and evidence.scope.session_id == context.routing_decision.get('workspace_session_id'))
    return evidence


def generated_success_source(context: TaskContext, result: Mapping[str, JsonValue]) -> str | None:
    try:
        binding = _binding(context, result)
        receipt = result.get('verification')
        _need(isinstance(receipt, Mapping))
        assert isinstance(receipt, Mapping)
        source = _hash(binding['source_sha256'])
        _need(receipt.get('source_sha256') == source
            and receipt.get('generated_acceptance_sha256') == binding['acceptance_sha256'])
        core = {key: value for key, value in receipt.items() if key != 'generated_acceptance_sha256'}
        if binding['stack'] == 'python':
            if 'python_ephemeral_environment' in result:
                _need('python_environment_reference' not in result)
                temporary = _root_ephemeral(context, result['python_ephemeral_environment'])
                report = PythonEphemeralProjectReport(core, temporary, source).to_payload()
                recipe_sha, input_sha = temporary.recipe_sha256, temporary.environment_input_sha256
            else:
                ref = _root_reference(context, result.get('python_environment_reference'))
                recipe_sha, input_sha = ref.recipe_sha256, ref.environment_input_sha256
                report = validate_python_passed_receipt(core, expected_source=source,
                    expected_recipe=recipe_sha, expected_environment_inputs=input_sha,
                    expected_environment=ref.environment_sha256)
                install = cast(Mapping[str, JsonValue], report['dependency_install'])
                _need(install['origin_execution_id'] == ref.origin_execution_id)
            _need(result.get('python_recipe') == {'schema_version': 1,
                'recipe_sha256': recipe_sha, 'environment_input_sha256': input_sha})
            proof = cast(Mapping[str, JsonValue], binding['test_execution'])
            _need(report['test_count'] == proof['count'])
        else:
            _need(set(core) == {'schema_version', 'status', 'source_sha256', 'checks', 'runtime'}
                and type(core['schema_version']) is int and core['schema_version'] == 1
                and core['status'] == 'passed')
            checks = core['checks']
            _need(isinstance(checks, tuple | list) and len(checks) == 3)
            assert isinstance(checks, tuple | list)
            for phase, check in zip(('install', 'build', 'test'), checks, strict=True):
                _need(isinstance(check, Mapping) and set(check) == {'phase', 'status', 'execution_diagnostic'})
                assert isinstance(check, Mapping)
                _need(check['phase'] == phase and check['status'] == 'passed')
                diagnostic = check['execution_diagnostic']
                _validate_original_diagnostic(diagnostic)
                assert isinstance(diagnostic, Mapping)
                _need(diagnostic['exit_code'] == 0 and diagnostic['timed_out'] is False)
            runtime = core['runtime']
            _need(isinstance(runtime, Mapping) and set(runtime) == {'node', 'npm'}
                and all(isinstance(value, str) and 0 < len(value) <= 128 for value in runtime.values()))
        return source
    except _ERRORS:
        return None


def generated_delivery_matches(
    context: TaskContext, verified: Mapping[str, JsonValue], bundle: Mapping[str, JsonValue],
) -> bool:
    try:
        source = generated_success_source(context, verified)
        return (source is not None and generated_binding_matches(context, bundle, source)
            and _encoded(verified['generated_project_acceptance']) == _encoded(bundle['generated_project_acceptance'])
            and bundle.get('verification_source_sha256') == source
            and bundle.get('verification_receipt_sha256') == hashlib.sha256(_encoded(verified['verification'])).hexdigest())
    except _ERRORS:
        return False


def generated_failure_current(context: TaskContext, result: Mapping[str, JsonValue]) -> bool:
    """A real tool failure is diagnosis only, never pre-model baseline credit."""
    try:
        _context(context)
        if 'python_verification_failure' in result:
            feedback = python_failure_feedback(result, tool_name='project.verify')
            _need(feedback is not None)
            assert feedback is not None
            report = cast(Mapping[str, JsonValue], feedback['python_verification_failure'])
            sibling = feedback.get('python_environment_reference')
            if 'python_ephemeral_environment' in feedback:
                _need(sibling is None)
                temporary = _root_ephemeral(context, feedback['python_ephemeral_environment'])
                PythonProjectFailure(report, None, ephemeral_environment=temporary)
            elif sibling is not None:
                ref = _root_reference(context, sibling)
                _need(report['recipe_sha256'] == ref.recipe_sha256
                    and report['environment_input_sha256'] == ref.environment_input_sha256
                    and report['environment_sha256'] == ref.environment_sha256)
            else:
                checks = cast(tuple[Mapping[str, JsonValue], ...], report['checks'])
                _need(len(checks) == 1 and checks[0]['phase'] == 'install'
                    and report['environment_sha256'] is None)
            return True
        diagnostic = result.get('execution_diagnostic')
        _validate_original_diagnostic(diagnostic)
        assert isinstance(diagnostic, Mapping)
        _need(diagnostic['timed_out'] is True or diagnostic['exit_code'] != 0)
        return validated_project_failure_payload(result.get('verification_failure'), diagnostic=diagnostic) is not None
    except _ERRORS:
        return False
