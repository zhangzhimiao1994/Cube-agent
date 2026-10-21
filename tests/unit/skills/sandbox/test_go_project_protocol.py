"""v8 identifies trusted Go inputs; it never grants install or command authority."""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.project import ContractProjectBrokerRequest, ProjectBrokerRequest
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerRequest
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainReference, ToolchainRequest


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_protocol'
    assert importlib.util.find_spec(name) is not None, 'Go project v8 protocol missing'
    return importlib.import_module(name)


def invocation(api: Any, **changes: Any) -> Any:
    scope = ToolchainPlanScope(
        tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3),
        project_id='demo', session_id='session', actor_id='main_agent', actor_role='operator',
        source_sha256='a' * 64, contract_sha256='b' * 64,
    )
    toolchain = ToolchainReference(
        artifact_sha256='c' * 64, archive_sha256='d' * 64, tree_sha256='e' * 64,
        binary_sha256='f' * 64, gofmt_sha256='1' * 64, total_bytes=1000,
        file_count=5, version='1.26.1', platform='linux_amd64',
    )
    return api.GoProjectInvocation.model_validate({
        'execution_id': 'go_1', 'tenant_id': scope.tenant_id, 'run_id': scope.run_id,
        'project_id': scope.project_id, 'session_id': scope.session_id,
        'sandbox_profile': 'workspace_write', 'timeout_seconds': 180,
        'scope': scope, 'original_reference': {
            'input_source_sha256': 'a' * 64, 'contract_sha256': 'b' * 64, 'file_count': 3,
        },
        'source_kind': 'original', 'recipe_sha256': '2' * 64,
        'environment_input_sha256': '3' * 64, 'toolchain': toolchain, **changes,
    }, strict=True)


def request(api: Any, action: str = 'run_go_project') -> Any:
    return api.GoProjectRequest(action=action, invocation=invocation(api))


def response(api: Any, call: Any, *, ok: bool = True, stdout: str = '{}') -> Any:
    inv = call.invocation
    outcome = ({'result': {'exit_code': 0, 'stdout': stdout, 'stderr': '', 'timed_out': False}}
               if call.action == 'run_go_project' else {'environment': inv.toolchain})
    return api.GoProjectResponse(
        action=call.action, execution_id=inv.execution_id, scope=inv.scope,
        recipe_sha256=inv.recipe_sha256, environment_input_sha256=inv.environment_input_sha256,
        toolchain=inv.toolchain, ok=ok,
        **(outcome if ok else {'error': 'go_project_execution_unavailable'}),
    )


@pytest.mark.parametrize('kind', ['original', 'candidate'])
@pytest.mark.parametrize('role', ['operator', 'admin', 'super_admin'])
def test_v8_roundtrip_and_base_invocation_preserve_contract_source_kind(
    api: Any, kind: str, role: str,
) -> None:
    original = invocation(api)
    scope = original.scope.model_copy(update={'actor_role': role})
    inv = invocation(api, source_kind=kind, scope=scope)
    call = api.GoProjectRequest(action='run_go_project', invocation=inv)
    assert call.version == 8 and inv.install_allowed is False
    assert api.parse_go_project_request(call.model_dump_json().encode()) == call
    assert api.base_go_invocation(inv).model_dump() == {
        key: value for key, value in inv.model_dump().items()
        if key not in {'scope', 'recipe_sha256', 'environment_input_sha256', 'toolchain',
                       'original_reference', 'source_kind'}
    }
    for legacy in (ProjectBrokerRequest, ContractProjectBrokerRequest,
                   PythonProjectBrokerRequest, ToolchainRequest):
        with pytest.raises(ValueError):
            legacy.model_validate_json(call.model_dump_json(), strict=True)


@pytest.mark.parametrize('field,value', [
    ('tenant_id', UUID(int=9)), ('run_id', UUID(int=9)),
    ('project_id', 'other'), ('session_id', 'other'), ('sandbox_profile', 'restricted'),
    ('source_kind', 'working-tree'), ('recipe_sha256', 'invalid'),
    ('environment_input_sha256', 'A' * 64),
    ('command', 'go test ./...'), ('flags', ['-race']), ('cwd', '/host'),
    ('toolchain_root', '/root/sdk'), ('download_url', 'https://example.invalid/go'),
    ('inputs', {'source_files': {}}),
])
def test_invocation_rejects_rebinding_and_model_selected_inputs(
    api: Any, field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        invocation(api, **{field: value})


@pytest.mark.parametrize('value', [True, 0, 1, 0.0, 'false', None])
def test_install_is_literal_false_without_numeric_coercion(api: Any, value: object) -> None:
    with pytest.raises(ValueError):
        invocation(api, install_allowed=value)


@pytest.mark.parametrize('output', [2 * 1024**2, 16 * 1024**2, 32 * 1024**2])
def test_go_output_budget_can_grow_without_changing_legacy_protocol(api: Any, output: int) -> None:
    from agent_hub.skills.sandbox.project import ProjectInvocation

    inv = invocation(api, output_limit_bytes=output)
    assert inv.output_limit_bytes == output
    assert api.parse_go_project_request(
        api.GoProjectRequest(action='run_go_project', invocation=inv).model_dump_json().encode()
    ).invocation.output_limit_bytes == output
    legacy = inv.model_dump()
    legacy = {field: legacy[field] for field in ProjectInvocation.model_fields}
    with pytest.raises(ValueError):
        ProjectInvocation.model_validate(legacy, strict=True)
    source_only = api.base_go_invocation(inv)
    assert source_only.output_limit_bytes == 1024**2
    assert source_only.model_dump(exclude={'output_limit_bytes'}) == {
        field: value for field, value in legacy.items() if field != 'output_limit_bytes'
    }
    assert inv.output_limit_bytes == output


@pytest.mark.parametrize('output', [0, True, 32 * 1024**2 + 1])
def test_go_output_budget_still_has_explicit_finite_resource_ceiling(api: Any, output: object) -> None:
    with pytest.raises(ValueError):
        invocation(api, output_limit_bytes=output)


@pytest.mark.parametrize('field,value', [
    ('tenant_id', UUID(int=9)), ('run_id', UUID(int=9)), ('project_id', 'other'),
    ('session_id', 'other'), ('source_sha256', '9' * 64), ('contract_sha256', '9' * 64),
    ('actor_id', 'subagent'), ('actor_role', 'viewer'),
])
def test_scope_must_bind_baseline_and_main_agent_role(api: Any, field: str, value: object) -> None:
    scope = invocation(api).scope.model_dump() | {field: value}
    with pytest.raises(ValueError):
        invocation(api, scope=scope)


@pytest.mark.parametrize('version', [True, 8.0, '8', 7, 4])
def test_protocol_version_is_exact_integer_eight(api: Any, version: object) -> None:
    with pytest.raises(ValueError):
        api.GoProjectRequest(version=version, action='run_go_project', invocation=invocation(api))


@pytest.mark.parametrize('action', [
    'run_project', 'run_python_project', 'supply_project_toolchain', 'probe_go_project',
])
def test_only_two_go_actions_exist(api: Any, action: str) -> None:
    with pytest.raises(ValueError):
        request(api, action)


@pytest.mark.parametrize('action', ['run_go_project', 'inspect_go_project_environment'])
def test_success_and_failure_have_exclusive_outcomes(api: Any, action: str) -> None:
    call = request(api, action)
    good = response(api, call)
    good.require_for(call)
    assert api.parse_go_project_response(good.model_dump_json().encode()) == good
    failed = response(api, call, ok=False)
    failed.require_for(call)
    assert failed.result is None and failed.environment is None
    assert (good.result is not None) == (action == 'run_go_project')
    assert (good.environment is not None) == (action == 'inspect_go_project_environment')
    for patch in (
        {'ok': False}, {'error': 'go_project_timeout'}, {'ok': 1},
        {'result': None, 'environment': None},
        {'environment': call.invocation.toolchain} if good.result else
        {'result': {'exit_code': 0, 'stdout': '{}', 'stderr': '', 'timed_out': False}},
    ):
        with pytest.raises(ValueError):
            api.GoProjectResponse.model_validate(good.model_dump() | patch, strict=True)
    for patch in ({'error': None}, {'error': '/root/private/diagnostic'}, {'result': good.result}
                  if good.result else {'environment': good.environment}):
        with pytest.raises(ValueError):
            api.GoProjectResponse.model_validate(failed.model_dump() | patch, strict=True)


@pytest.mark.parametrize('ok', [True, False])
@pytest.mark.parametrize('field,value', [
    ('action', 'inspect_go_project_environment'), ('execution_id', 'other'),
    ('recipe_sha256', '9' * 64), ('environment_input_sha256', '9' * 64),
    ('tenant_id', UUID(int=9)), ('user_id', UUID(int=9)), ('run_id', UUID(int=9)),
    ('project_id', 'other'), ('session_id', 'other'), ('actor_id', 'other'),
    ('actor_role', 'admin'), ('source_sha256', '9' * 64), ('contract_sha256', '9' * 64),
    ('tree_sha256', '9' * 64), ('binary_sha256', '9' * 64), ('archive_sha256', '9' * 64),
    ('artifact_sha256', '9' * 64), ('gofmt_sha256', '9' * 64), ('total_bytes', 999),
    ('file_count', 9), ('version', '1.27.1'),
])
def test_require_for_binds_all_identity_even_on_failure(
    api: Any, ok: bool, field: str, value: object,
) -> None:
    call = request(api)
    reply = response(api, call, ok=ok)
    patch: dict[str, Any] = {field: value}
    if field in ToolchainPlanScope.model_fields:
        patch = {'scope': reply.scope.model_copy(update=patch)}
    elif field in ToolchainReference.model_fields:
        patch = {'toolchain': reply.toolchain.model_copy(update=patch)}
    with pytest.raises(ValueError):
        reply.model_copy(update=patch).require_for(call)


@pytest.mark.parametrize('field,value', [('binary_sha256', '9' * 64), ('total_bytes', 999)])
def test_inspect_requires_measured_reference_to_match_selected_identity(
    api: Any, field: str, value: object,
) -> None:
    call = request(api, 'inspect_go_project_environment')
    reply = response(api, call)
    with pytest.raises(ValueError):
        reply.model_copy(update={'environment': reply.environment.model_copy(update={field: value})}).require_for(call)


@pytest.mark.parametrize('target', ['request', 'response'])
@pytest.mark.parametrize('damage', ['duplicate', 'nested-duplicate', 'nan', 'infinity', 'exponent', 'array', 'encoding'])
def test_parsers_reject_ambiguous_or_nonfinite_json(api: Any, target: str, damage: str) -> None:
    call = request(api)
    item = call if target == 'request' else response(api, call)
    raw = item.model_dump_json().encode()
    if damage == 'duplicate':
        raw = raw.replace(b'"version":8', b'"version":8,"version":8', 1)
    elif damage == 'nested-duplicate':
        raw = raw.replace(b'"file_count":5', b'"file_count":5,"file_count":5')
    elif damage == 'array':
        raw = b'[]'
    elif damage == 'encoding':
        raw = b'\xff'
    else:
        value = {'nan': b'NaN', 'infinity': b'Infinity', 'exponent': b'1e9999'}[damage]
        raw = raw.replace(b'"total_bytes":1000', b'"total_bytes":' + value)
    with pytest.raises(ValueError, match='go_project_request_invalid'):
        getattr(api, 'parse_go_project_' + target)(raw)


def test_request_has_exact_512kib_byte_boundary(api: Any) -> None:
    call = request(api)
    raw = call.model_dump_json().encode()
    padded = raw + b' ' * (512 * 1024 - len(raw))
    assert api.parse_go_project_request(padded) == call
    with pytest.raises(ValueError):
        api.parse_go_project_request(padded + b' ')


def test_response_accepts_large_report_and_uses_dynamic_encoded_byte_limits(api: Any) -> None:
    call = request(api)
    reply = response(api, call, stdout='x' * (2 * 1024**2))
    raw = reply.model_dump_json().encode()
    assert len(raw) > 1024**2
    assert api.parse_go_project_response(raw) == reply
    assert api.go_project_response_limit(diagnostic_limit_bytes=64) == 512 * 1024 + 6 * 64
    with pytest.raises(ValueError):
        api.parse_go_project_response(raw, diagnostic_limit_bytes=64)
    small = response(api, call, stdout='\x01' * 64)
    assert api.parse_go_project_response(small.model_dump_json().encode(), diagnostic_limit_bytes=64) == small
    with pytest.raises(ValueError):
        api.parse_go_project_response(small.model_dump_json().encode(), diagnostic_limit_bytes=63)
    maximum = api.go_project_response_limit(diagnostic_limit_bytes=1)
    with pytest.raises(ValueError):
        api.parse_go_project_response(b' ' * (maximum + 1), diagnostic_limit_bytes=1)


def test_diagnostic_limit_counts_utf8_stdout_and_stderr_together(api: Any) -> None:
    call = request(api)
    reply = response(api, call, stdout='\u00e9' * 2)
    reply = reply.model_copy(update={'result': reply.result.model_copy(update={'stderr': 'xx'})})
    raw = reply.model_dump_json().encode()
    assert api.parse_go_project_response(raw, diagnostic_limit_bytes=6) == reply
    with pytest.raises(ValueError):
        api.parse_go_project_response(raw, diagnostic_limit_bytes=5)


@pytest.mark.parametrize('limit', [True, 0, -1, '100', 32 * 1024**2 + 1])
def test_response_limit_cannot_disable_the_32mib_hard_ceiling(api: Any, limit: object) -> None:
    with pytest.raises(ValueError):
        api.go_project_response_limit(diagnostic_limit_bytes=limit)


def test_model_validation_also_rejects_raw_diagnostics_over_32mib(api: Any) -> None:
    with pytest.raises(ValueError):
        response(api, request(api), stdout='x' * (32 * 1024**2 + 1))


def test_unknown_request_fields_and_constructed_install_authority_are_revalidated(api: Any) -> None:
    call = request(api)
    for field in ('inputs', 'plan', 'expected_environment', 'command'):
        with pytest.raises(ValueError):
            api.parse_go_project_request(call.model_dump_json()[:-1].encode() +
                                         b',"' + field.encode() + b'":{}}')
    bad = call.model_copy(update={'invocation': call.invocation.model_copy(update={'install_allowed': True})})
    with pytest.raises(ValueError):
        response(api, call).require_for(bad)
    with pytest.raises(ValueError):
        api.base_go_invocation(bad.invocation)
