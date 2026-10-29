"""Native codecs/typed receipts in memory; no runtime restore, provider or Root."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.events import gateway_completion_events
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.action_direct import _binding, _prepared_context
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.continuation_artifacts import _encode, save_continuation
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct_completion_journal import _response_payload
from agent_hub.runtime.instruction_context import model_request_sha256
from agent_hub.runtime.model_scope import ModelScopeTracker
from tests.unit.capabilities.test_python_generated_lifecycle import generated_session

MODULE = 'agent_hub.capabilities.python_direct_expiry'


async def native_case(count: int = 1, *, content_reference: bool = False,
                      guidance: bool = False) -> dict[str, Any]:
    row = generated_session().row
    row.status = 'failed'
    row.actor_id, row.actor_role, row.request, row.mode = UUID(int=3), Role.OPERATOR, 'synthetic task', 'direct'
    row.routing_decision['requested_permissions'] = ['workspace.read','workspace.write','command.run']
    row.routing_decision['generated_project_preparation']['request_sha256'] = hashlib.sha256(row.request.encode()).hexdigest()
    context = TaskContext(tenant_id=row.tenant_id,run_id=row.id,mode=TaskMode.DIRECT,
        request=row.request,actor_id=row.actor_id,actor_role=row.actor_role,routing_decision=row.routing_decision)
    if guidance:
        from agent_hub.runtime.instruction_context import InstructionContext, InstructionSource

        text = 'Synthetic guidance, retained only in the execution context.'
        digest = hashlib.sha256(text.encode()).hexdigest()
        source = InstructionSource(path='AGENTS.md',status='loaded',project_id='demo',session_id='one',
            text=text,read_bytes=len(text.encode()),content_bytes=len(text.encode()),
            read_sha256=digest,file_sha256=digest,content_sha256=digest)
        context = context.model_copy(update={'instruction_context':InstructionContext(load_id=uuid4(),
            tenant_id=row.tenant_id,run_id=row.id,sources=(source,))})
        assert 'instruction_context' not in context.to_payload()
    repository = InMemoryArtifactRepository()
    model = ModelRequest(logical_model='primary',messages=(ModelMessage(role='user',content='synthetic'),),timeout_seconds=30)
    completion = GatewayCompletion(response=ModelResponse(text='synthetic',usage=TokenUsage(1,2,3)),
        logical_model='primary',deployment_id='primary',provider_id='provider',provider_model='provider/primary',
        cost_usd=Decimal('.01'),attempted_logical_models=('primary',))
    journal = await save_continuation(repository,tenant_id=row.tenant_id,run_id=row.id,
        value={'position':0,'request_sha256':model_request_sha256(replace(model,timeout_seconds=1)),
               'response':_response_payload(completion)})
    actions=[]
    for index in range(11):
        arguments: dict[str, Any] = {} if index == 0 else {'path':f'src/f{index}.py','content':'# synthetic only\n'}
        if content_reference and index == 1:
            arguments['content'] = '# synthetic only\n' * 5000
        expanded = dict(arguments)
        if content_reference and index == 1:
            arguments['content_reference'] = await save_continuation(repository,
                tenant_id=row.tenant_id,run_id=row.id,value=arguments.pop('content'))
        request = HarnessToolCallRequest(run_id=row.id,actor='main_agent',
            tool_name='project.inspect' if index==0 else 'workspace.write_text',
            arguments=arguments,
            approval_required=False,sandbox='workspace_write',idempotency_key=f'action{index}')
        result = HarnessToolCallResult(call_id=request.call_id,tool_name=request.tool_name,status='succeeded',payload={})
        raw = request.to_payload() if index == 0 else {'name':request.tool_name,
            'arguments':expanded,'key':request.idempotency_key}
        actions.append({'raw_sha256':hashlib.sha256(_encode(raw)).hexdigest(),
                        'request':request.to_payload(),'result':result.to_payload()})
    pending = HarnessToolCallRequest(run_id=row.id,actor='main_agent',tool_name='project.verify',arguments={},
        approval_required=True,sandbox='workspace_write',idempotency_key='pending_verify')
    contracts = {k:v for k,v in row.routing_decision.items() if k.startswith('generated_project_')}
    preparation = {'schema_version':1,'scope':{},'contracts':contracts,'workspace_sha256':'a'*64}
    inspection = Artifact(id=uuid4(),type='tool_result',producer='main_agent',content={
        'agent_id':'main_agent','tool_name':'project.inspect','call_id':actions[0]['request']['call_id'],
        'arguments_sha256':hashlib.sha256(b'{}').hexdigest(),'result':{}})
    frame = {'context':context.to_payload(),'models':[journal],'actions':actions,
             'pending':{'raw_sha256':hashlib.sha256(_encode(pending.to_payload())).hexdigest(),'request':pending.to_payload()},
             'emitted':8,'elapsed':1.0,'inspection':inspection.to_payload(),'preparation':preparation}
    reference = await save_continuation(repository,tenant_id=row.tenant_id,run_id=row.id,value=cast(JsonValue,frame))
    checkpoint = RuntimeCheckpoint(id=uuid4(),tenant_id=row.tenant_id,run_id=row.id,mode=TaskMode.DIRECT,
        runtime_type='direct_action',runtime_version='2',state={'phase':'concrete_action','completed':False,
            'approval_id':'approval_fixture','tool_name':'project.verify','reference':reference,
            'context_sha256':_binding(_prepared_context(context,preparation),'primary'),
            'elapsed_seconds':2.0,'timeout_seconds':context.timeout_seconds})
    events=[RunEvent(kind='tool.started',run_id=row.id,sequence=3,actor='main_agent',
        tool_call_id=actions[0]['request']['call_id'],tool_name='project.inspect'),RunEvent(kind='tool.completed',run_id=row.id,sequence=4,actor='main_agent',
        tool_call_id=actions[0]['request']['call_id'],tool_name='project.inspect',payload={'status':'succeeded'}),
        RunEvent(kind='artifact.created',run_id=row.id,sequence=5,actor='main_agent',artifact=inspection)]
    usages=[]
    tracker=ModelScopeTracker(run_id=row.id,tenant_id=row.tenant_id,logical_model='primary')
    for index in range(count):
        offset=6 if index==0 else 12
        identity={'call_id':str(uuid4()),'request_sha256':model_request_sha256(model)}
        events.append(RunEvent(kind='model.started',run_id=row.id,sequence=offset,actor='main_agent',payload={**identity,'logical_model':'primary'}))
        completed=next(iter(gateway_completion_events(completion,run_id=row.id,start_sequence=offset+1)))
        events.append(completed.model_copy(update={'payload':{**completed.payload,**identity}}))
        cost=RunEvent(kind='cost.recorded',run_id=row.id,sequence=offset+2,actor='main_agent',provider_id='provider',
            cost_usd=Decimal('.01'),currency='USD',payload={**identity,'usage_known':True,'input_tokens':1,'output_tokens':2,'total_tokens':3})
        events.append(cost)
        usages.append(SimpleNamespace(tenant_id=row.tenant_id,run_id=row.id,sequence=cost.sequence,
            provider_id=cost.provider_id,cost_usd=str(cost.cost_usd),payload=cost.to_payload()))
        position=tracker.begin()
        tracker.received(position,model,completion)
    seq=12 if count==1 else 15
    parts=tracker.artifacts(include_received_only=True)
    assert len(parts)==1
    artifact=parts[0]
    events.extend((RunEvent(kind='artifact.created',run_id=row.id,sequence=seq,actor='main_agent',artifact=artifact),
        RunEvent(kind='model.failure_receipt',run_id=row.id,sequence=seq+1,payload={'artifact_id':str(artifact.id),'actor':'main_agent'}),
        RunEvent(kind='runtime.failed',run_id=row.id,sequence=seq+2,reason='synthetic_known_failure')))
    return {'record':row,'checkpoint':checkpoint,'checkpoint_sequence':9,'repository':repository,
            'events':tuple(sorted(events,key=lambda e:e.sequence)),'usage':tuple(usages)}


@pytest.mark.parametrize('count',[1,2])
async def test_native_unstarted_pending_has_known_closed_models_without_uuid_or_sha_bridge(count: int) -> None:
    case=await native_case(count)
    spec=importlib.util.find_spec(MODULE)
    result=False if spec is None else await importlib.import_module(MODULE).direct_action_expiry_liveness(**case)
    assert result is True
    assert case['checkpoint'].state['completed'] is False


@pytest.mark.parametrize('defect', [
    'foreign-run', 'foreign-actor', 'request', 'checkpoint-hash', 'model-open',
    'model-duplicate', 'model-request', 'model-provenance', 'unknown-usage',
    'usage-row', 'usage-cost', 'scope-missing', 'receipt-missing', 'scope-hash',
    'pending-started', 'pending-completed', 'prior-tool-open', 'prior-tool-failed',
    'completed-run', 'no-failure', 'scope-incomplete', 'event-order',
    'inspection-missing', 'inspection-conflict', 'inspection-duplicate',
])
async def test_unproven_liveness_is_retained(defect: str) -> None:
    from agent_hub.capabilities.python_direct_expiry import direct_action_expiry_liveness

    case = await native_case(2)
    events = list(case['events'])
    if defect == 'foreign-run':
        case['record'].id = uuid4()
    elif defect == 'foreign-actor':
        case['record'].actor_id = uuid4()
    elif defect == 'request':
        case['record'].request = 'changed'
    elif defect == 'checkpoint-hash':
        case['checkpoint'] = case['checkpoint'].model_copy(update={'state_sha256':'0'*64})
    elif defect == 'model-open':
        events = [e for e in events if e.sequence != 13]
    elif defect == 'model-duplicate':
        event = next(e for e in events if str(e.kind) == 'model.completed')
        events.append(event.model_copy(update={'sequence':20}))
    elif defect in ('model-request', 'model-provenance', 'unknown-usage'):
        index = next(i for i,e in enumerate(events) if str(e.kind) == 'model.completed')
        key,value = {'model-request':('request_sha256','0'*64),
            'model-provenance':('provider_model','wrong'), 'unknown-usage':('total_tokens',None)}[defect]
        events[index] = events[index].model_copy(update={'payload':{**events[index].payload,key:value}})
    elif defect == 'usage-row':
        case['usage'][0].payload = {}
    elif defect == 'usage-cost':
        case['usage'][0].cost_usd = '0'
    elif defect in ('scope-missing', 'receipt-missing', 'no-failure'):
        kind = {'scope-missing':'artifact.created', 'receipt-missing':'model.failure_receipt',
                'no-failure':'runtime.failed'}[defect]
        events = [e for e in events if str(e.kind) != kind]
    elif defect == 'scope-hash':
        index = next(i for i,e in enumerate(events) if e.artifact is not None and e.artifact.type=='model_attempt')
        events[index] = events[index].model_copy(update={'artifact':events[index].artifact.model_copy(
            update={'content_sha256':'0'*64})})
    elif defect.startswith('inspection-'):
        index = next(i for i,e in enumerate(events) if e.sequence == 5)
        if defect == 'inspection-missing':
            del events[index]
        elif defect == 'inspection-duplicate':
            events.append(events[index].model_copy(update={'sequence':20}))
        else:
            events[index] = events[index].model_copy(update={'artifact':events[index].artifact.model_copy(
                update={'content_sha256':'0'*64})})
    elif defect in ('pending-started','pending-completed'):
        events.append(RunEvent(kind='tool.started' if defect.endswith('started') else 'tool.completed',
            run_id=case['record'].id,sequence=20,actor='main_agent',tool_call_id='pending_verify',
            tool_name='project.verify',payload={} if defect.endswith('started') else {'status':'succeeded'}))
    elif defect == 'prior-tool-open':
        events = [e for e in events if str(e.kind) != 'tool.completed']
    elif defect == 'prior-tool-failed':
        events = [e.model_copy(update={'kind':'tool.failed'}) if str(e.kind)=='tool.completed' else e for e in events]
    elif defect == 'completed-run':
        case['record'].status = 'completed'
    elif defect == 'scope-incomplete':
        events.append(RunEvent(kind='model.scope_incomplete',run_id=case['record'].id,sequence=20,
            payload={'reason':'synthetic'}))
    else:
        events.reverse()
    case['events'] = tuple(sorted(events,key=lambda e:e.sequence)) if defect != 'event-order' else tuple(events)
    assert await direct_action_expiry_liveness(**case) is False


async def test_real_workspace_hash_with_segmented_content_is_decoded_without_tool_replay() -> None:
    from agent_hub.capabilities.python_direct_expiry import direct_action_expiry_liveness

    assert await direct_action_expiry_liveness(**await native_case(content_reference=True)) is True


async def test_asyncpg_uuid_is_canonicalized_only_at_readonly_db_boundary() -> None:
    from asyncpg.pgproto.pgproto import UUID as PgUUID

    from agent_hub.capabilities.python_direct_expiry import direct_action_expiry_liveness

    case = await native_case()
    row = case['record']
    row.tenant_id, row.id = PgUUID(str(row.tenant_id)), PgUUID(str(row.id))
    assert type(row.tenant_id) is not UUID and isinstance(row.tenant_id, UUID)
    assert await direct_action_expiry_liveness(**case) is True
    assert type(row.tenant_id) is PgUUID and type(row.id) is PgUUID


async def test_expiry_is_not_resume_and_does_not_reconstruct_excluded_instruction_binding() -> None:
    import json

    from agent_hub.capabilities.python_direct_expiry import direct_action_expiry_liveness
    from agent_hub.runtime.continuation_artifacts import load_continuation

    case = await native_case(guidance=True)
    frame = await load_continuation(case['repository'],tenant_id=case['record'].tenant_id,
        run_id=case['record'].id,reference=case['checkpoint'].state['reference'])
    snapshot = TaskContext.from_payload(json.loads(_encode(frame['context'])))
    assert snapshot.instruction_context is None
    assert _binding(_prepared_context(snapshot,frame['preparation']),'primary') != case['checkpoint'].state['context_sha256']
    assert await direct_action_expiry_liveness(**case) is True
    assert case['checkpoint'].state['completed'] is False


@pytest.mark.parametrize('defect',['raw-hash','result-call','truncated','result-failed',
    'pending-run','journal-position','journal-unknown-cost','journal-unknown-usage'])
async def test_private_native_frame_mutations_do_not_prove_liveness(defect: str) -> None:
    import json

    from agent_hub.capabilities.python_direct_expiry import direct_action_expiry_liveness
    from agent_hub.runtime.continuation_artifacts import load_continuation

    case = await native_case()
    row = case['record']
    value = await load_continuation(case['repository'],tenant_id=row.tenant_id,run_id=row.id,
        reference=case['checkpoint'].state['reference'])
    frame = json.loads(_encode(value))
    if defect == 'raw-hash':
        frame['actions'][0]['raw_sha256'] = '0'*64
    elif defect == 'result-call':
        frame['actions'][0]['result']['call_id'] = 'different'
    elif defect == 'truncated':
        frame['actions'][0]['result']['truncated'] = True
    elif defect == 'result-failed':
        frame['actions'][0]['result'].update(status='failed',failure_reason='synthetic')
    elif defect == 'pending-run':
        frame['pending']['request']['run_id'] = str(uuid4())
        frame['pending']['raw_sha256'] = hashlib.sha256(_encode(frame['pending']['request'])).hexdigest()
    else:
        journal = await load_continuation(case['repository'],tenant_id=row.tenant_id,run_id=row.id,
            reference=frame['models'][0])
        journal = json.loads(_encode(journal))
        if defect == 'journal-position':
            journal['position'] = 1
        elif defect == 'journal-unknown-cost':
            journal['response']['cost'] = None
        else:
            journal['response']['usage'] = None
        frame['models'][0] = await save_continuation(case['repository'],tenant_id=row.tenant_id,
            run_id=row.id,value=journal)
    reference = await save_continuation(case['repository'],tenant_id=row.tenant_id,run_id=row.id,value=frame)
    payload = case['checkpoint'].to_payload()
    payload['state']['reference'] = reference
    payload.pop('state_sha256')
    case['checkpoint'] = RuntimeCheckpoint.from_payload(payload)
    assert await direct_action_expiry_liveness(**case) is False


@pytest.mark.parametrize('defect', [None, 'foreign-artifact', 'artifact-sha', 'artifact-bytes',
    'checkpoint-state', 'checkpoint-scope', 'checkpoint-missing-hash', 'duplicate-checkpoint',
    'foreign-event', 'wrong-event-kind', 'foreign-usage', 'too-many-events'])
async def test_scoped_readonly_dao_uses_native_hydration_and_fails_closed(defect: str | None) -> None:
    from agent_hub.capabilities.python_direct_expiry import verified_direct_expiry
    from agent_hub.runs.artifacts import PostgresArtifactRepository

    case = await native_case(2)
    row, cp = case['record'], case['checkpoint']
    checkpoints = [SimpleNamespace(tenant_id=row.tenant_id,run_id=row.id,id=cp.id,
        mode=cp.mode.value,runtime_type=cp.runtime_type,runtime_version=cp.runtime_version,
        state=cp.to_payload()['state'],state_sha256=cp.state_sha256,payload=cp.to_payload(),sequence=9)]
    events = [SimpleNamespace(tenant_id=row.tenant_id,run_id=row.id,sequence=e.sequence,
        kind=str(e.kind),payload=e.to_payload()) for e in case['events']]
    artifacts = {}
    for (tenant,run,identity), artifact in case['repository']._artifacts.items():
        payload = PostgresArtifactRepository._encode_payload(artifact.to_payload())
        artifacts[identity] = SimpleNamespace(tenant_id=tenant,run_id=run,id=identity,
            content_sha256=artifact.content_sha256,payload=payload,byte_size=len(payload.encode()))
    if defect == 'foreign-artifact':
        next(iter(artifacts.values())).tenant_id = uuid4()
    elif defect == 'artifact-sha':
        next(iter(artifacts.values())).content_sha256 = '0'*64
    elif defect == 'artifact-bytes':
        next(iter(artifacts.values())).byte_size += 1
    elif defect == 'checkpoint-state':
        checkpoints[0].state = {}
    elif defect == 'checkpoint-scope':
        checkpoints[0].tenant_id = uuid4()
    elif defect == 'checkpoint-missing-hash':
        del checkpoints[0].payload['state_sha256']
    elif defect == 'duplicate-checkpoint':
        checkpoints.append(checkpoints[0])
    elif defect == 'foreign-event':
        events[0].tenant_id = uuid4()
    elif defect == 'wrong-event-kind':
        events[0].kind = 'tool.completed'
    elif defect == 'foreign-usage':
        case['usage'][0].tenant_id = uuid4()
    elif defect == 'too-many-events':
        events *= 4097

    class Session:
        open = False

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        @asynccontextmanager
        async def begin(self) -> Any:
            self.open = True
            try:
                yield
            finally:
                self.open = False

        async def scalar(self, query: Any) -> Any:
            if query.column_descriptions[0]['entity'].__name__ == 'RunRow':
                assert query._for_update_arg is not None
                return row
            assert self.open
            return None

        async def scalars(self, query: Any) -> Any:
            assert query._for_update_arg is None
            entity = query.column_descriptions[0]['entity'].__name__
            sql = str(query)
            if entity == 'RunRow':
                return SimpleNamespace(all=lambda:[row])
            assert 'tenant_id =' in sql and 'run_id =' in sql and query._limit_clause is not None
            if entity == 'RunCheckpointRow':
                result = checkpoints
            elif entity == 'RunEventRow':
                result = events
            elif entity == 'RunUsageRow':
                result = case['usage']
            else:
                identities = next(v for v in query.compile().params.values() if isinstance(v, list))
                result = [artifacts[i] for i in identities if i in artifacts]
            return SimpleNamespace(all=lambda:result)

    assert await verified_direct_expiry(Session(), row) is (defect is None)
    if defect is None:
        from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
        from tests.unit.skills.sandbox.test_python_project_client import invocation

        row.updated_at = datetime.now(UTC) - timedelta(days=2)
        session = Session()
        calls = []

        class Repository:
            async def run_transaction(self) -> Any:
                return session

        class Reader:
            async def python_expired_environment_invocation(self, *args: Any) -> Any:
                assert session.open
                return invocation()

        class Root:
            async def release_expired(self, item: Any) -> bool:
                assert session.open and item.install_allowed is False and item.source_kind == 'candidate'
                calls.append(item.run_id)
                return True

        assert await PythonProjectTerminalHook(Repository(), Reader(), Root()).maintain_capacity() == 1
        assert calls == [row.id]
