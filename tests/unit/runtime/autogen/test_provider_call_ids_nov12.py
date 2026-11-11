import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    RuntimeExecutionError,
    _pending_approval_call,
    _provider_call_id,
    _provider_call_ids,
)
from agent_hub.runtime.contracts import EventKind, RuntimeCheckpoint
from tests.integration.runtime.test_autogen_adapter import ScriptedGateway, context, plan
from tests.unit.runtime.autogen.test_capability_approval_resume import (
    APPROVAL,
    RealDiscussionApproval,
)
from tests.unit.runtime.autogen.test_model_tool_bridge import _durability


def internal_id(identifier: str) -> str:
    if identifier == 'call_lower' or identifier == 'call_case':
        return identifier
    return 'call-v1-' + hashlib.sha256(
        b'agent-hub/autogen/provider-call-id/v1\0' + identifier.encode()).hexdigest()


@pytest.mark.parametrize('identifiers', [
    ('call_lower',), ('call_UpperAa09',), ('A' * 128,), ('call_Case', 'call_case'),
    ('Call_X', internal_id('Call_X')), ('call_lower', 'call_lower'),
])
async def test_real_autogen_maps_provider_ids_through_harness(identifiers) -> None:
    class Gateway(ScriptedGateway):
        async def complete_with_context(self, request):
            self.requests.append(request)
            count = len(self.requests)
            response = ModelResponse(text='analyst' if request.logical_model == 'selector' else '[COMPLETE] verified',
                                     usage=TokenUsage(1, 1, 2))
            if count == 2:
                response = ModelResponse(text=None, tool_calls=tuple(ToolCall(
                    id=identifier, name='web_search', arguments={'query': 'safe'})
                    for identifier in identifiers), usage=TokenUsage(1, 1, 2))
            return GatewayCompletion(response=response, deployment_id='shared',
                logical_model=request.logical_model, provider_id='mock', provider_model='mock/local',
                cost_usd=Decimal(0))

    class Harness:
        def __init__(self):
            self.calls = []

        def is_replay_safe(self, name):
            return True

        async def invoke(self, tenant_id, request, **kwargs):
            self.calls.append(request)
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                         status='succeeded', payload={'safe': True})

    gateway, harness = Gateway([]), Harness()
    participant = DiscussionParticipant(id='analyst', role='Analyst', goal='Search',
                                       logical_model='shared', allowed_tools=('web.search',))
    runtime = AutoGenDiscussionRuntime(gateway, plan(selector_model='selector', participants=(participant, plan().participants[1])),
                                      capability_gateway=harness, harness_tool_gateway=harness)
    async with asyncio.timeout(10):
        events = [event async for event in runtime.run(context())]
    if len(identifiers) == 2 and (identifiers[0] == identifiers[1]
                                or internal_id(identifiers[0]) == identifiers[1]):
        assert harness.calls == []
        assert events[-1].kind is EventKind.RUNTIME_FAILED
        assert events[-1].reason == 'discussion_failed'
        assert len(gateway.requests) == 2
        return
    expected = [internal_id(identifier) for identifier in identifiers]
    assert [call.call_id for call in harness.calls] == expected
    completed = [event for event in events if event.kind is EventKind.TOOL_COMPLETED]
    assert [event.tool_call_id for event in completed] == expected
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) <= 4


async def test_real_approval_cold_resume_preserves_uppercase_call_id_without_new_model() -> None:
    class UppercaseApproval(RealDiscussionApproval):
        requests_at_approved_verify = None

        async def complete_with_context(self, request):
            completion = await super().complete_with_context(request)
            if completion.response.tool_calls:
                return replace(completion, response=replace(completion.response, tool_calls=tuple(
                    replace(call, id='Call_' + call.id) for call in completion.response.tool_calls)))
            return completion

        async def invoke(self, tenant_id, request, **kwargs):
            if self.approved and request.tool_name == 'project.verify':
                self.requests_at_approved_verify = len(self.requests)
            return await super().invoke(tenant_id, request, **kwargs)

    case = UppercaseApproval()
    async with asyncio.timeout(10):
        first = [event async for event in case.runner.run(case.context)]
    saved = await case.runner.save_checkpoint()
    assert checkpoint_waits_for_approval(saved, APPROVAL), [(e.kind, e.reason) for e in first[-3:]]
    artifacts = await case.runner._hydrate_checkpoint(saved, case.context)
    raw_ids = [call['id'] for artifact in artifacts if artifact.type == 'model_result'
               for call in artifact.content.get('tool_calls', ())]
    assert 'Call_actual-verify' in raw_ids
    assert internal_id('Call_actual-verify') not in raw_ids
    saved = RuntimeCheckpoint.from_payload(json.loads(json.dumps(saved.to_payload())))
    count = len(case.requests)
    case.approved = True
    restored = AutoGenDiscussionRuntime(case, case.runner._plan, capability_gateway=case,
        harness_tool_gateway=case, artifact_repository=case.runner._repository)
    await restored.restore_checkpoint(saved)
    stream = restored.run(case.context.model_copy(update={'checkpoint': saved}))
    succeeded = False
    try:
        async for event in stream:
            if event.checkpoint is not None:
                rows = event.checkpoint.state['tool_ledger']
                if (isinstance(rows, tuple) and rows and isinstance(rows[-1], Mapping)
                        and rows[-1].get('name') == 'project.verify'
                        and rows[-1].get('status') == 'succeeded'):
                    succeeded = True
                    break
    finally:
        await stream.aclose()
    assert succeeded and case.executions == 1
    assert case.requests_at_approved_verify == count
    assert case.calls[-1].call_id == internal_id('Call_actual-verify')
    assert len(case.requests) == count


@pytest.mark.parametrize('identifier', [None, 1, '', ' padded', 'padded ', 'bad\nID', 'bad\x00ID', 'bad\x7fID', '\ud800', 'a' * 513])
def test_invalid_provider_identifier_is_rejected(identifier) -> None:
    with pytest.raises(RuntimeExecutionError, match='provider tool call id is invalid'):
        _provider_call_id(identifier)


def test_mixed_passthrough_hash_collision_and_duplicate_ids_reject() -> None:
    mapped = internal_id('call_UpperAa09')
    with pytest.raises(RuntimeExecutionError, match='ambiguous'):
        _provider_call_ids(('call_UpperAa09', mapped))
    with pytest.raises(RuntimeExecutionError, match='ambiguous'):
        _provider_call_ids(('call_lower', 'call_lower'))


@pytest.mark.parametrize('identifiers', [('Call_X', internal_id('Call_X')), ('call_lower', 'call_lower')])
def test_pending_approval_rejects_complete_batch_canonical_collision(identifiers) -> None:
    durable = _durability()
    completion = GatewayCompletion(response=ModelResponse(text=None, tool_calls=tuple(
        ToolCall(id=identifier, name='web_search', arguments={}) for identifier in identifiers),
        usage=TokenUsage(1, 1, 2)), deployment_id='shared', logical_model='shared',
        provider_id='mock', provider_model='mock/local', cost_usd=Decimal(0))
    artifact = durable._completion_artifact(completion)
    durable._artifacts[str(artifact.id)] = artifact
    durable.model_entries = [{'status': 'succeeded', 'artifact_id': str(artifact.id),
                              'artifact_sha256': artifact.content_sha256}]
    calls = [{'id': identifier, 'name': 'web_search', 'arguments': {}} for identifier in identifiers]
    durable.approval_batch = {'model_artifact_id': str(artifact.id),
        'model_artifact_sha256': artifact.content_sha256, 'actor': 'analyst', 'start_position': 0,
        'next_index': 0, 'calls_sha256': hashlib.sha256(json.dumps(calls, ensure_ascii=False,
            allow_nan=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    with pytest.raises(RuntimeExecutionError, match='ambiguous'):
        _pending_approval_call(durable, {'analyst': 'shared'})


def test_legacy_and_opaque_ids_keep_case_distinct_without_relaxing_roles() -> None:
    assert _provider_call_id('call_legacy-1.2') == 'call_legacy-1.2'
    opaque = 'opaque:/ID.' + 'A' * (512 - len('opaque:/ID.'))
    assert _provider_call_id(opaque) == internal_id(opaque)
    assert _provider_call_id('call_UpperAa09') == (
        'call-v1-f3b7dbda39fb57282267838f4654810a82336e902cb89d9a115a1f516e959918')
    assert _provider_call_id('call_A') != _provider_call_id('call_a')
    with pytest.raises(ValueError, match='participant id'):
        DiscussionParticipant(id='UpperRole', role='Role', goal='Goal', logical_model='shared')
