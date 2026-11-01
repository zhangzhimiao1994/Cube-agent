"""Metadata-only domain selection, using real original/generated service fixtures."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import engineering_role_context as engineering_api
from agent_hub.runtime.contracts import Artifact, TaskContext, _freeze_object
from agent_hub.runtime.role_planner import TaskProfile
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map, string_value


def api() -> ModuleType:
    assert importlib.util.find_spec('agent_hub.runtime.engineering_role_context') is not None, 'engineering context helper missing'
    return importlib.import_module('agent_hub.runtime.engineering_role_context')


def clone(context: TaskContext, **values: object) -> TaskContext:
    return context.model_copy(update=values).validated_internal_clone()


async def original(tmp_path: Path) -> TaskContext:
    from agent_hub.runs.automatic_project_inputs import _binding
    from agent_hub.runs.project_inputs import python_original_context_metadata
    from tests.unit.runtime.test_python_project_history import history_case
    _,repo,_,trusted,context=await history_case(tmp_path)
    metadata=Artifact(id=uuid4(),type='original_python_project_context',producer='original_project_preparer',
        content=python_original_context_metadata(trusted),source_ids=(str(context.artifacts[0].id),))
    route={**context.routing_decision,'role_planning_intent_version':3,
        'project_delivery':'workspace','artifact_strategy':'workspace_bundle',
        'sandbox_profile':'workspace_write','execution_backend':'systemd','permission_mode':'default',
        'requested_permissions':['workspace.read','workspace.write','command.run'],
        'automatic_project_input_version':1,'conversation_id':'existing-conversation'}
    context=clone(context,request='给现有CLI增加功能。',mode=TaskMode.HYBRID,
        actor_id=repo.record.actor_id,actor_role=repo.record.actor_role,
        routing_decision=route,artifacts=(*context.artifacts,metadata))
    route['automatic_project_input_selection']={'schema_version':1,'phase':'selected','kind':'original',
        'binding_sha256':_binding(context),'source_sha256':trusted.stored.reference.input_source_sha256,
        'editable_paths':trusted.inputs.contract.editable_paths}
    return clone(context,routing_decision=route)


async def test_domain_without_software_keywords_is_selected_and_does_not_mutate(tmp_path: Path) -> None:
    module=engineering_api;assert api() is module;context=await original(tmp_path)
    before=context.model_dump_json()
    assert module.engineering_software_context(context)
    assert module.engineering_role_profiles(context,(TaskProfile.GENERAL,))==(TaskProfile.SOFTWARE,TaskProfile.GENERAL)
    assert context.model_dump_json()==before


@pytest.mark.parametrize('mode',[TaskMode.DIRECT,TaskMode.DISPATCH,TaskMode.DISCUSS,TaskMode.HYBRID])
async def test_same_bound_context_is_a_hint_in_each_mode(tmp_path: Path,mode: TaskMode) -> None:
    module=engineering_api;assert api() is module;context=await original(tmp_path)
    from agent_hub.runs.automatic_project_inputs import _binding
    changed=clone(context,mode=mode)
    route=json.loads(changed.model_dump_json())['routing_decision']
    route['automatic_project_input_selection']['binding_sha256']=_binding(changed)
    assert module.engineering_software_context(clone(changed,routing_decision=route))


@pytest.mark.parametrize('fault',['tenant','run','user','project','session','source','observation','artifact','marker_only',
    'readonly','permission','query','old0','old1','old2','conflicting_generated'])
async def test_invalid_or_nonwriting_context_never_promotes_domain(tmp_path: Path,fault: str) -> None:
    module=engineering_api;assert api() is module;context=await original(tmp_path)
    route=json.loads(context.model_dump_json())['routing_decision']
    values: dict[str, object]={}
    if fault in {'tenant','run','user'}:values[{'tenant':'tenant_id','run':'run_id','user':'actor_id'}[fault]]=uuid4()
    elif fault=='project':route['project_id']='foreign-project'
    elif fault=='session':route['workspace_session_id']='foreign-session'
    elif fault=='source':route['project_input_preparation']['reference']['input_source_sha256']='0'*64
    elif fault=='observation':route['project_input_preparation'].pop('observation')
    elif fault=='artifact':values['artifacts']=context.artifacts[1:]
    elif fault=='marker_only':route={k:v for k,v in route.items() if k not in {'project_input_preparation','python_project_history','automatic_project_input_selection'}};values['artifacts']=()
    elif fault=='readonly':route['sandbox_profile']='read_only';route['permission_mode']='read_only'
    elif fault=='permission':route['requested_permissions']=['workspace.read']
    elif fault=='query':values['request']='请解释这个项目当前如何使用？'
    elif fault.startswith('old'):route['role_planning_intent_version']=int(fault[-1])
    else:route['generated_project_contract_version']=1
    broken=clone(context,routing_decision=route,**values)
    assert not module.engineering_software_context(broken)
    assert module.engineering_role_profiles(broken,(TaskProfile.GENERAL,))==(TaskProfile.GENERAL,)


async def test_positive_auxiliary_profiles_stay_non_authorizing(tmp_path: Path) -> None:
    module=engineering_api;assert api() is module;context=await original(tmp_path)
    assert module.engineering_role_profiles(context,(TaskProfile.OPERATIONS,TaskProfile.RESEARCH,TaskProfile.GENERAL))==(
        TaskProfile.SOFTWARE,TaskProfile.OPERATIONS,TaskProfile.RESEARCH,TaskProfile.GENERAL)


async def test_generated_metadata_requires_captured_preparation_and_native_bound_result(tmp_path: Path) -> None:
    module=engineering_api;assert api() is module
    from agent_hub.capabilities import python_generated_service
    from tests.unit.capabilities.test_python_generated_service import approve, configured, request
    service,_,repo,_,_,generated=await configured(python_generated_service,tmp_path)
    exact=await service.authorization_request(request(),generated);approve(repo,exact)
    result=await service.verify(generated,'engineer')
    route={**repo.row.routing_decision,'role_planning_intent_version':3,
        'sandbox_profile':'workspace_write','permission_mode':'default','execution_backend':'systemd',
        'requested_permissions':['workspace.read','workspace.write','command.run'],
        'project_id':generated.invocation.project_id,'workspace_session_id':generated.invocation.session_id,
        'project_delivery':'workspace','artifact_strategy':'workspace_bundle'}
    artifact=Artifact(id=uuid4(),type='tool_result',producer='engineer',content={
        'tool_name':'project.verify','agent_id':'engineer','result':result})
    context=TaskContext(run_id=repo.row.id,tenant_id=repo.row.tenant_id,actor_id=repo.row.actor_id,
        actor_role=repo.row.actor_role,mode=TaskMode.HYBRID,request=repo.row.request,
        routing_decision=route,artifacts=(artifact,))
    assert module.engineering_software_context(context)
    forged = copy.deepcopy(json.loads(context.model_dump_json())['routing_decision'])
    forged['generated_project_preparation']['reference']['input_source_sha256'] = '0' * 64
    assert not module.engineering_software_context(clone(context, routing_decision=forged))
    assert not module.engineering_software_context(clone(context,artifacts=()))
    assert not module.engineering_software_context(clone(context,actor_id=uuid4()))
    bad=copy.deepcopy(json.loads(context.model_dump_json())['routing_decision'])
    bad['generated_project_preparation']['acceptance_sha256']='0'*64
    assert not module.engineering_software_context(clone(context,routing_decision=bad))


async def node_original(tmp_path: Path) -> TaskContext:
    from agent_hub.runs.automatic_project_inputs import _binding
    from tests.unit.runs.test_original_project_preparation import setup

    preparer, repository, context, _, _, _, _ = await setup(tmp_path)
    route = {**context.routing_decision, 'role_planning_intent_version': 3}
    repository.row.routing_decision = route
    context = clone(context, routing_decision=route)
    prepared = await preparer(context)
    route = {**prepared.routing_decision, 'role_planning_intent_version': 3,
        'automatic_project_input_version': 1, 'conversation_id': 'existing-node',
        'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
        'permission_mode': 'default'}
    prepared = clone(prepared, request='给现有工具增加功能。', routing_decision=route)
    state = read_map(route['project_input_preparation'])
    route['automatic_project_input_selection'] = {'schema_version': 1, 'phase': 'selected',
        'kind': 'original', 'binding_sha256': _binding(prepared),
        'source_sha256': read_map(state['reference'])['input_source_sha256'],
        'editable_paths': read_map(route['project_input'])['editable_paths']}
    prepared = clone(prepared, routing_decision=route)
    return prepared


@pytest.mark.parametrize('version', [None, 0, 1, 2])
async def test_node_legacy_snapshot_resumes_with_identical_preparation(
    tmp_path: Path, version: int | None,
) -> None:
    import hashlib

    from agent_hub.harness.types import HarnessToolCallRequest
    from agent_hub.runtime.action_continuation import read_action_continuation
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.continuation_artifacts import _encode, save_continuation
    from agent_hub.runtime.contracts import RuntimeCheckpoint
    from tests.unit.runs.test_original_project_preparation import setup

    preparer, repository, context, observer, _, _, _ = await setup(tmp_path)
    route = dict(context.routing_decision)
    if version is not None:
        route['role_planning_intent_version'] = version
    repository.row.routing_decision = route
    repository.row.mode = 'direct'
    context = clone(context, mode=TaskMode.DIRECT, routing_decision=route)
    prepared = await preparer(context)
    # This is the deployed legacy artifact schema, before the new Node hint proof.
    current = prepared.artifacts[-1]
    content = json.loads(current.model_dump_json())['content']
    content.pop('baseline_reference', None)
    historical = Artifact(id=current.id, type=current.type, producer=current.producer, content=content)
    snapshot = clone(prepared, artifacts=(*prepared.artifacts[:-1], historical), execution_token=None)
    pending = HarnessToolCallRequest(run_id=context.run_id, actor='main_agent',
        tool_name='project.inspect', arguments={}, sandbox='workspace_write', approval_required=True,
        idempotency_key='pending_node_compat')
    store = InMemoryArtifactRepository()
    value = {'context': snapshot.to_payload(), 'models': [], 'actions': [],
        'pending': {'raw_sha256': hashlib.sha256(_encode(pending.to_payload())).hexdigest(),
                    'request': pending.to_payload()},
        'emitted': 0, 'elapsed': 0.0, 'inspection': None, 'preparation': {}}
    reference = await save_continuation(store, tenant_id=context.tenant_id,
        run_id=context.run_id, value=_freeze_object(value, name='continuation fixture'))
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='direct_action', runtime_version='2',
        tenant_id=context.tenant_id, run_id=context.run_id, mode=TaskMode.DIRECT,
        state={'phase': 'concrete_action', 'completed': False, 'approval_id': 'approval_node_compat',
            'tool_name': pending.tool_name, 'context_sha256': 'b' * 64, 'reference': reference,
            'elapsed_seconds': 1.0, 'timeout_seconds': context.timeout_seconds})
    restored = await preparer(clone(context, routing_decision=prepared.routing_decision))
    assert observer.count == 1
    frame = await read_action_continuation(store, clone(restored, checkpoint=checkpoint))
    assert frame.snapshot.artifacts[-1].to_payload() == historical.to_payload()


async def test_node_original_observation_uses_its_own_v1_wire_format(tmp_path: Path) -> None:
    from uuid import NAMESPACE_URL, uuid5

    prepared = await node_original(tmp_path)
    state = read_map(prepared.routing_decision['project_input_preparation'])
    artifact = prepared.artifacts[-1]
    assert dict(read_map(artifact.content['baseline_reference'])) == dict(read_map(state['reference']))
    assert artifact.content_sha256 == artifact.recompute_content_sha256()
    assert artifact.id == uuid5(NAMESPACE_URL,
        f'original-project:{prepared.tenant_id}:{prepared.run_id}:{state["observation_sha256"]}')
    assert engineering_api.engineering_software_context(prepared)
    assert not engineering_api.engineering_software_context(clone(prepared, actor_id=uuid4()))


@pytest.mark.parametrize('fault', ['contract_sha256', 'file_count', 'missing_proof',
    'missing_proof_field', 'extra_proof_field', 'bool_proof_count', 'artifact_id', 'rebound_run',
    'rebound_tenant', 'duplicate_artifact'])
async def test_node_original_requires_full_current_preparer_proof(tmp_path: Path, fault: str) -> None:
    from agent_hub.runs.automatic_project_inputs import _binding

    context = await node_original(tmp_path)
    assert engineering_api.engineering_software_context(context)
    route = json.loads(context.model_dump_json())['routing_decision']
    values: dict[str, object] = {}
    artifact = context.artifacts[-1]
    if fault in {'contract_sha256', 'file_count'}:
        reference = route['project_input_preparation']['reference']
        reference[fault] = '0' * 64 if fault == 'contract_sha256' else reference[fault] + 1
    elif fault in {'missing_proof', 'missing_proof_field', 'extra_proof_field', 'bool_proof_count'}:
        content = json.loads(artifact.model_dump_json())['content']
        if fault == 'missing_proof':
            content.pop('baseline_reference', None)
        else:
            proof = content.setdefault('baseline_reference', dict(route['project_input_preparation']['reference']))
            if fault == 'missing_proof_field':
                proof.pop('contract_sha256')
            elif fault == 'extra_proof_field':
                proof['untrusted'] = True
            else:
                proof['file_count'] = True
        values['artifacts'] = (*context.artifacts[:-1], Artifact(id=artifact.id,
            type=artifact.type, producer=artifact.producer, content=content))
    elif fault == 'artifact_id':
        values['artifacts'] = (*context.artifacts[:-1], artifact.model_copy(update={'id': uuid4()}))
    elif fault == 'duplicate_artifact':
        values['artifacts'] = (*context.artifacts, artifact.model_copy(update={'id': uuid4()}))
    else:
        changed = clone(context, **{'run_id' if fault == 'rebound_run' else 'tenant_id': uuid4()})
        route['automatic_project_input_selection']['binding_sha256'] = _binding(changed)
        context = changed
    broken = clone(context, routing_decision=route, **values)
    assert not engineering_api.engineering_software_context(broken)


@pytest.mark.parametrize('field', [None, 'contract_sha256', 'input_source_sha256', 'file_count'])
async def test_node_generated_success_without_full_reference_proof_never_promotes(
    tmp_path: Path, field: str | None,
) -> None:
    import hashlib

    from agent_hub.capabilities.generated_project_service import generated_verification_binding
    from agent_hub.runtime.generated_project_history import generated_success_source
    from tests.unit.capabilities.test_generated_project_service import USER, setup
    from tests.unit.capabilities.test_scoped_read import RUN, TENANT
    from tests.unit.runtime.crew.test_engineering_delivery_gate import fixture

    service, repo = setup(tmp_path)
    repo.row.request = '给现有工具增加功能。'
    generated = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert generated is not None
    receipt = read_map(read_map(fixture()[1]['1'].content['result'])['verification'])
    proof = {'schema_version': 1, 'manifest_sha256': generated.state['manifest_raw_sha256'],
        'count': 1, 'names': ['adds two numbers'], 'test_evidence_sha256': hashlib.sha256(
            json.dumps({'schema_version': 1, 'passed_tests': ['adds two numbers']},
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    binding = generated_verification_binding(generated, string_value(receipt['source_sha256']), proof)
    result = {'verification': {**receipt, 'generated_acceptance_sha256': generated.envelope.sha256},
        'generated_project_acceptance': binding}
    route = {**repo.row.routing_decision, 'role_planning_intent_version': 3,
        'permission_mode': 'default', 'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle'}
    artifact = Artifact(id=uuid4(), type='tool_result', producer='engineer', content={
        'tool_name': 'project.verify', 'agent_id': 'engineer',
        'result': _freeze_object(result, name='verification result fixture')})
    context = TaskContext(run_id=RUN, tenant_id=TENANT, actor_id=USER, actor_role=repo.row.actor_role,
        mode=TaskMode.HYBRID, request=repo.row.request, routing_decision=route, artifacts=(artifact,))
    assert generated_success_source(context, _freeze_object(read_map(artifact.content['result']), name='result fixture')) == receipt['source_sha256']
    changed = json.loads(context.model_dump_json())['routing_decision']
    if field is not None:
        reference = changed['generated_project_preparation']['reference']
        reference[field] = reference[field] + 1 if field == 'file_count' else '0' * 64
    assert not engineering_api.engineering_software_context(clone(context, routing_decision=changed))
