"""Catalog observations use real Harness/backend reads, not install authority."""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.tools.builtin_contracts import (
    builtin_tool_description,
    builtin_tool_input_schema,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.direct import (
    DirectRuntime,
    RuntimeExecutionError,
    _workspace_batch_from_model_text,
)
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_project_profile import (
    PythonProjectProfile,
    PythonProjectProfileInterpreter,
    PythonProjectProfileResponse,
    PythonProjectProfileSandbox,
    PythonProjectProfileScope,
    PythonProjectProfileWheel,
)
from tests.unit.capabilities.test_generated_project_delivery import configured
from tests.unit.runs.project_auto_repair_typed_fixture import (
    json_map,
    mapping,
    present,
    string,
)
from tests.unit.runs.reviewed_report_revision_typed_fixture import object_array, object_map
from tests.unit.runtime.direct_catalog_diagnostic_typed_fixture import BudgetWindow
from tests.unit.runtime.test_direct_project_verification import (
    FIXED,
    Model,
    Policy,
    batch,
    capture_delivery_deadline,
    diagnostic_context,
    runner,
)

DIGEST = 'a' * 64


def catalog_batch(**updates: object) -> str:
    return json.dumps({'workspace_batch': {
        'files': {}, 'complete': False, 'continuation': 'Select only needed exact dependency pins.',
        'python_catalog': {'catalog_sha256': DIGEST, 'names': ['pkg0500']}, **updates,
    }})


def profile() -> PythonProjectProfile:
    return PythonProjectProfile(interpreter=PythonProjectProfileInterpreter(
        implementation='cpython', version='3.12.321', abi='cp312', platform='linux_x86_64',
        binary_sha256='b' * 64, base_runtime_sha256='c' * 64), catalog_sha256=DIGEST,
        catalog=tuple(PythonProjectProfileWheel(name=f'pkg{i:04}', version='1.2.3',
            filename=f'pkg{i:04}-1.2.3-py3-none-any.whl',
            sha256=hashlib.sha256(str(i).encode()).hexdigest(), size_bytes=100)
            for i in range(512)))


def profile_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def inspect(_self: PythonProjectProfileSandbox, invocation: ProjectInvocation) -> PythonProjectProfileResponse:
        return PythonProjectProfileResponse(ok=True,
            scope=PythonProjectProfileScope.for_invocation(invocation), profile=profile())
    monkeypatch.setattr(PythonProjectProfileSandbox, 'inspect', inspect)


def test_catalog_query_has_its_own_exclusive_readonly_batch() -> None:
    parsed = _workspace_batch_from_model_text(catalog_batch(), allow_catalog=True)
    assert parsed is not None and parsed.files == {} and parsed.read_paths == ()
    assert not parsed.complete and present(parsed.python_catalog)['catalog_sha256'] == DIGEST


@pytest.mark.parametrize('updates', [
    {'files': {'main.py': FIXED}}, {'read_paths': ['main.py']}, {'complete': True},
    {'continuation': ''}, {'python_catalog': None},
    {'python_catalog': {'catalog_sha256': DIGEST, 'offset': True}},
    {'python_catalog': {'catalog_sha256': DIGEST, 'names': ['../private']}},
    {'python_catalog': {'catalog_sha256': DIGEST, 'extra': 'authority'}},
])
def test_catalog_query_cannot_be_mixed_with_writes_or_invalid_controls(updates: dict[str, object]) -> None:
    assert _workspace_batch_from_model_text(catalog_batch(**updates), allow_catalog=True) is None


def test_legacy_parser_rejects_catalog_even_when_mixed_with_valid_files() -> None:
    assert _workspace_batch_from_model_text(catalog_batch()) is None
    assert _workspace_batch_from_model_text(catalog_batch(files={'main.py': FIXED})) is None


def test_inspection_schema_advertises_readonly_catalog_but_verify_remains_no_arguments() -> None:
    schema = builtin_tool_input_schema('project.inspect')
    assert schema is not None and 'python_catalog' in mapping(schema['properties'])
    query = mapping(mapping(schema['properties'])['python_catalog'])
    assert query['required'] == ('catalog_sha256',) and query['additionalProperties'] is False
    assert mapping(mapping(query['properties'])['names'])['maxItems'] == 16
    assert present(builtin_tool_input_schema('project.verify'))['properties'] == {}
    assert 'catalog_complete=false' in present(builtin_tool_description('project.inspect'))


@pytest.mark.parametrize('version', [None, True, '1', 2, 1])
def test_direct_advertises_catalog_batch_only_for_exact_selected_protocol(
    tmp_path: Path, version: int | str | None,
) -> None:
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': version,
    }})
    runtime, _, _, _ = runner(tmp_path, ctx, Model())
    prompt = runtime._build_prompt(ctx)
    assert prompt.messages is not None
    content = '\n'.join(string(message.content) for message in prompt.messages)
    assert ('workspace_batch.python_catalog' in content) is (type(version) is int and version == 1)


async def test_direct_queries_real_pins_before_actual_local_child_and_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_transport(monkeypatch)
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': 1,
        'project_id': 'direct-verification', 'workspace_session_id': 'same-task',
    }})
    class PinModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 1:
                message = next(string(item.content) for item in request.messages
                               if 'UNTRUSTED_PYTHON_CATALOG_JSON' in string(item.content))
                raw = message.split('<UNTRUSTED_PYTHON_CATALOG_JSON>', 1)[1].split(
                    '</UNTRUSTED_PYTHON_CATALOG_JSON>', 1)[0]
                wheel_data, = object_array(object_map(object_map(json.loads(raw))['python_build_profile'])['catalog'])
                wheel = object_map(wheel_data)
                lock = f"{wheel['name']}=={wheel['version']} --hash=sha256:{wheel['sha256']}\n"
                self.texts[:] = [batch({'main.py': FIXED, 'requirements.lock': lock})]
            return await super().complete_with_context(request)

    model = PinModel(catalog_batch())
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls[0] == 'project.inspect' and backend.calls.count('project.inspect') == 1
    assert len(backend.process_results) == 1 and backend.process_results[0].exit_code == 0
    assert 'workspace.bundle' in backend.calls and len(list((tmp_path / 'generated').rglob('*.zip'))) == 1
    query = shared.requests[0]
    assert query.tool_name == 'project.inspect' and query.actor == 'main_agent'
    assert mapping(query.arguments['python_catalog'])['catalog_sha256'] == DIGEST
    assert query.run_id == ctx.run_id and query.sandbox == 'workspace_write'
    message = '\n'.join(string(item.content) for item in model.requests[1].messages)
    assert 'UNTRUSTED_PYTHON_CATALOG_JSON' in message
    assert profile().catalog[500].sha256 in message and 'not installation' in message.lower()
    assert not shared.results[0].truncated and shared.results[0].status == 'succeeded'
    lock = (backend.root / 'requirements.lock').read_text()
    assert lock == f'pkg0500==1.2.3 --hash=sha256:{profile().catalog[500].sha256}\n'
    with zipfile.ZipFile(next((tmp_path / 'generated').rglob('*.zip'))) as archive:
        assert archive.read('requirements.lock').decode() == lock


async def test_unrelated_large_python_catalog_does_not_block_generated_node_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_transport(monkeypatch)
    backend, repository = await configured(tmp_path)
    row = repository.row
    row.routing_decision = {**row.routing_decision,
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle'}
    ctx = TaskContext(tenant_id=row.tenant_id, run_id=row.id, actor_id=row.actor_id,
        actor_role=row.actor_role, mode=TaskMode.DIRECT, request=row.request,
        routing_decision=row.routing_decision, token_budget=50_000, timeout_seconds=60)
    model = Model(batch({'src/index.js': 'exports.add=(a,b)=>a+b;\n'}))
    shared = HarnessToolGateway(backend, policy_gateway=Policy(ctx, CapabilityStatus.ALLOWED),
                                require_actor_identity=True)
    runtime = DirectRuntime(model, logical_model='main', capability_gateway=backend,
                            harness_tool_gateway=shared)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 1
    inspection = next(event.artifact for event in events if event.artifact is not None
                      and event.artifact.content.get('tool_name') == 'project.inspect')
    assert mapping(mapping(inspection.content['result'])['python_build_profile'])['catalog_complete'] is False
    assert mapping(mapping(inspection.content['result'])['scope'])['run_id'] == str(ctx.run_id)
    first_prompt = '\n'.join(string(message.content) for message in model.requests[0].messages)
    assert 'UNTRUSTED_PROJECT_INSPECTION_JSON' in first_prompt and DIGEST in first_prompt
    assert 'pkg0511' not in first_prompt


async def test_catalog_read_cannot_extend_saved_time_token_or_completed_batch_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_transport(monkeypatch)
    deadlines = capture_delivery_deadline(monkeypatch)
    limits: list[tuple[int, int, int]] = []
    before: list[BudgetWindow] = []
    token_limit = direct._workspace_delivery_token_limit

    def limit(context: TaskContext, *, initial_soft_limit: int, completed_files: int,
              completed_batches: int, consumed_tokens: int, last_batch_tokens: int) -> int:
        result = token_limit(context, initial_soft_limit=initial_soft_limit, completed_files=completed_files,
                             completed_batches=completed_batches, consumed_tokens=consumed_tokens,
                             last_batch_tokens=last_batch_tokens)
        limits.append((completed_files, completed_batches, result))
        return result

    def window() -> BudgetWindow:
        value = deadlines[-1]
        from agent_hub.runtime.adaptive_budget import AdaptiveDeadline
        assert isinstance(value, AdaptiveDeadline)
        return value.deadline, value.absolute_deadline, value.progress_units, tuple(limits)

    monkeypatch.setattr(direct, '_workspace_delivery_token_limit', limit)
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': 1,
        'project_id': 'direct-verification', 'workspace_session_id': 'same-task',
    }})

    class ObservedModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 1:
                assert window() == before[0]
                assert limits == [(0, 0, ctx.token_budget)]
            return await super().complete_with_context(request)

    model = ObservedModel(catalog_batch(), batch({'main.py': FIXED}))
    runtime, _, shared, _ = runner(tmp_path, ctx, model)
    invoke = shared.invoke

    async def observe(tenant_id: UUID, request: HarnessToolCallRequest, *,
                      user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name == 'project.inspect':
            before.append(window())
        return await invoke(tenant_id, request, user_id=user_id, role=role)

    monkeypatch.setattr(shared, 'invoke', observe)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(before) == 1


@pytest.mark.parametrize('fault', [
    'scope', 'digest', 'gateway_truncated', 'names', 'next_offset', 'boolean_count',
    'installed_status', 'wrong_call', 'bad_hash',
])
async def test_catalog_receipt_mismatch_never_reaches_model_files_or_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    profile_transport(monkeypatch)
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': 1,
        'project_id': 'direct-verification', 'workspace_session_id': 'same-task',
    }})
    model = Model(catalog_batch(), batch({'main.py': FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    invoke = shared.invoke

    async def tamper(tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        result = await invoke(tenant_id, request, user_id=user_id, role=role)
        if request.tool_name != 'project.inspect':
            return result
        payload = object_map(json.loads(json.dumps(result.to_payload()['payload'])))
        value = object_map(payload['python_build_profile'])
        if fault == 'scope':
            object_map(payload['scope'])['run_id'] = str(ctx.actor_id)
        elif fault == 'digest':
            value['catalog_sha256'] = 'f' * 64
        elif fault == 'names':
            object_map(value['catalog_page'])['names'] = ['other-package']
        elif fault == 'next_offset':
            object_map(value['catalog_page'])['next_offset'] = 0
        elif fault == 'boolean_count':
            object_map(value['catalog_page'])['returned_count'] = True
        elif fault == 'installed_status':
            value['status'] = 'installed'
        elif fault == 'bad_hash':
            object_map(object_array(value['catalog'])[0])['sha256'] = 'pretend-wheel-hash'
        return replace(result, payload=json_map(payload), truncated=fault == 'gateway_truncated',
                       call_id='foreign-call' if fault == 'wrong_call' else result.call_id)

    monkeypatch.setattr(shared, 'invoke', tamper)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 1 and backend.calls == ['project.inspect']
    assert not (backend.root / 'main.py').exists() and not backend.process_results


async def test_duplicate_catalog_observation_cannot_buy_more_work_or_write_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_transport(monkeypatch)
    budgets = capture_delivery_deadline(monkeypatch)
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': 1,
        'project_id': 'direct-verification', 'workspace_session_id': 'same-task',
    }})
    model = Model(catalog_batch(), catalog_batch(), batch({'main.py': FIXED}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match='catalog'):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2 and backend.calls == ['project.inspect']
    assert budgets[-1].progress_units == 0
    assert not (backend.root / 'main.py').exists()
    assert not list((tmp_path / 'generated').rglob('*.zip'))


@pytest.mark.parametrize('version', [None, True, '1', 2])
async def test_unselected_catalog_request_cannot_fall_back_to_text_delivery(
    tmp_path: Path, version: int | str | None,
) -> None:
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': version,
    }})
    model = Model(catalog_batch())
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert backend.calls == [] and not (backend.root / 'main.py').exists()


@pytest.mark.parametrize('envelope', ['workspace_batch', 'workspace_bundle', 'root'])
async def test_invalid_catalog_controls_cannot_escape_through_legacy_file_fallback(
    tmp_path: Path, envelope: str,
) -> None:
    ctx = diagnostic_context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'engineering_catalog_reads_version': 1,
    }})
    value = {'files': {'unexpected.py': 'print(1)\n'}, 'complete': False,
             'continuation': 'Inspect the catalog before writing.',
             'python_catalog': {'catalog_sha256': DIGEST, 'offset': True}}
    text = json.dumps(value if envelope == 'root' else {envelope: value})
    model = Model(text)
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(ctx)]
    assert backend.calls == []
    assert not (backend.root / 'unexpected.py').exists()
