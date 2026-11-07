"""Original delivery entry vs continuation, through real plan/gateway/SDK decoding.

HTTP is MockTransport; inspection is a scoped metadata fixture. A later read is
deliberately denied. No verification success, bundle, provider, or Root is faked.
"""
from __future__ import annotations

import asyncio
import json
import selectors
import time
from decimal import Decimal

import httpx
import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayRejectedOutput, ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelCapability
from agent_hub.runtime import defaults
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.crew.adapter import (
    CrewDispatchRuntime,
    RuntimeExecutionError,
    _is_incremental_workspace_contract_step,
)
from agent_hub.runtime.original_project_progress import original_failure_observed
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease
from tests.unit.runtime.crew.required_correction_typed_fixture import LocalSDKTransport
from tests.unit.runtime.crew.test_adapter_failure_reason import FastFactory
from tests.unit.runtime.test_configured_runtime import TENANT_ID, FakeCapabilityAvailability
from tests.unit.runtime.test_original_project_progress import selected

TOOLS = ('workspace.read', 'workspace.list', 'workspace.write_text',
         'workspace.bundle', 'project.inspect', 'project.verify')
PROSE = {'role': 'assistant', 'content': 'I will inspect and repair the existing source.'}


def tool(name: str, call_id: str, **arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function', 'function': {
            'name': name.replace('.', '_'), 'arguments': json.dumps(arguments)}}]}


INSPECT = tool('project.inspect', 'inspect-original')
READ = tool('workspace.read', 'read-original', path='src/index.js')


def task(action: int = 0):
    context = selected(project_delivery='workspace', artifact_strategy='workspace_bundle',
        project_scale='medium', project_id='existing', workspace_session_id='same-session',
        sandbox_profile='workspace_write', execution_backend='systemd',
        engineering_delivery_contract_version=1, engineering_action_version=action,
        requested_permissions=['workspace.read', 'workspace.write', 'command.run', 'network.read'])
    return context.model_copy(update={'tenant_id': TENANT_ID, 'mode': TaskMode.DISPATCH,
        'request': 'Repair the existing project source; preserve its original tests and configuration.'})


def plan(context, *, discussion=False):
    role = RoleAssignment(id='implementer' if not discussion else 'reviewer',
        role='Implementer' if not discussion else 'Reviewer',
        purpose=RolePurpose.EXECUTE if not discussion else RolePurpose.RISK_REVIEW,
        mission='Repair only permitted original source.' if not discussion else 'Review the proposed repair.',
        must_answer=('What changed?',), allowed_tools=TOOLS if not discussion else (),
        forbidden_actions=('Do not alter protected checks or grant permissions.',),
        skills=(), output_schema={'summary': 'string'}, model='main')
    return defaults._dispatch_plan((role,), context,
        capability_gateway=FakeCapabilityAvailability(set(TOOLS)))


class ScopeHarness:
    def __init__(self, context):
        self.context, self.calls = context, []

    async def invoke(self, tenant_id, request, *, user_id=None, role=None):
        assert tenant_id == self.context.tenant_id and request.run_id == self.context.run_id
        assert user_id == self.context.actor_id and role == self.context.actor_role
        self.calls.append(request)
        if request.tool_name == 'project.inspect':
            payload = {'scope': {'run_id': str(request.run_id), 'tenant_id': str(tenant_id)},
                'manifests': [{'path': 'package.json', 'status': 'observed'}],
                'operations': {'build': 'declared', 'test': 'declared'}, 'issues': []}
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='succeeded', payload=payload)
        assert request.tool_name == 'workspace.read', 'No write/verify/bundle is allowed in this entry test'
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='failed', payload={'reason': 'capability denied'})


async def drive(messages, *, action=0, discussion=False):
    context = task(action)
    current = plan(context, discussion=discussion)
    harness, wire, requests, rejected = ScopeHarness(context), [], [], []

    def handle(request: httpx.Request):
        packet = json.loads(request.content)
        wire.append(packet)
        assert len(wire) <= len(messages), 'Unexpected additional model request'
        response = messages[len(wire) - 1]
        return httpx.Response(200, json={'id': f'local-{len(wire)}',
            'object': 'chat.completion', 'created': 1, 'model': 'mock-model',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls' if response.get('tool_calls') else 'stop',
                         'message': response}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 16, 'total_tokens': 17}})

    transport = LocalSDKTransport(handle)

    class RecordingGateway(ModelGateway):
        async def complete_with_context(self, request):
            requests.append(request)
            try:
                return await super().complete_with_context(request)
            except GatewayRejectedOutput as error:
                rejected.append(error)
                raise

    capacity = CapacityStub([lease('primary') for _ in messages])
    registry = ModelRegistry([deployment('primary', logical_model='main',
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING,
                                ModelCapability.STRUCTURED_OUTPUT}),
        request_model='mock-model', api_base='https://offline.example/v1',
        input_per_million_usd=Decimal(1), output_per_million_usd=Decimal(2))])
    gateway = RecordingGateway(registry, capacity, SecretStub([]), transport)
    runtime = CrewDispatchRuntime(gateway, current,
        capability_gateway=FakeCapabilityAvailability(set(TOOLS)), harness_tool_gateway=harness,
        crew_factory=FastFactory(), artifact_repository=InMemoryArtifactRepository())
    events, reason = [], None
    try:
        try:
            async for event in runtime.run(context):
                events.append(event)
        except RuntimeExecutionError as error:
            reason = str(error)
        checkpoint = await runtime.save_checkpoint()
        return context, current, harness, wire, requests, rejected, events, reason, checkpoint
    finally:
        for client in transport.clients:
            await client.close()


class MemorySelector(selectors.SelectSelector):
    def select(self, timeout=None):
        assert not self.get_map(), 'Entry tests cannot open sockets or process descriptors'
        if timeout:
            time.sleep(min(timeout, .005))
        return []


class MemoryLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(selector=MemorySelector())
    def _make_self_pipe(self): pass
    def _close_self_pipe(self): pass
    def _write_to_self(self): pass


def run(messages, **options):
    # Test-only loop factory avoids Windows sandbox socketpair startup. Product
    # routing/decision/SDK code is neither monkeypatched nor replaced.
    with asyncio.Runner(loop_factory=MemoryLoop) as runner:
        return runner.run(drive(messages, **options))


@pytest.mark.parametrize('action', [0, 1])
def test_actual_original_plan_first_prose_reaches_scoped_tool_with_original_budget(action):
    context, current, harness, wire, requests, rejected, events, reason, checkpoint = run(
        (PROSE, INSPECT, READ), action=action)
    assert original_failure_observed(context)
    assert _is_incremental_workspace_contract_step(current.steps[0])
    assert [call.tool_name for call in harness.calls] == ['project.inspect', 'workspace.read']
    assert len(requests) == len(wire) == 3
    assert reason is not None and not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
    assert all(0 < request.timeout_seconds <= context.timeout_seconds for request in requests)
    assert checkpoint.state['usage']['tokens'] == 51
    if action:
        assert requests[0].required_tool == requests[1].required_tool == 'project_inspect'
        assert requests[0].tools == requests[1].tools
        assert requests[1].allow_fallback is False
        assert len(rejected) == 1 and rejected[0].evidence.reason == 'invalid_tool'
    else:
        assert not rejected
        assert any('ENGINEERING_DELIVERY_CONTINUATION:' in str(m.content) for m in requests[1].messages)


@pytest.mark.parametrize('action', [0, 1])
def test_existing_inspection_then_prose_uses_delivery_continuation_not_new_milestone(action):
    context, _current, harness, _wire, requests, rejected, events, reason, checkpoint = run(
        (INSPECT, PROSE, READ), action=action)
    assert original_failure_observed(context)
    assert [call.tool_name for call in harness.calls] == ['project.inspect', 'workspace.read']
    assert len(requests) == 3 and not rejected
    assert requests[1].required_tool is None
    assert any('ENGINEERING_DELIVERY_CONTINUATION:' in str(m.content) for m in requests[2].messages)
    assert checkpoint.state['usage']['tokens'] == 51
    assert reason is not None and not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)


def test_repeated_prose_spends_one_progress_correction_then_refuses_without_tools():
    _context, _current, harness, _wire, requests, rejected, events, reason, checkpoint = run((PROSE, PROSE))
    assert len(requests) == 2 and not harness.calls and not rejected
    assert reason is not None and not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
    assert checkpoint.state['usage']['tokens'] == 34
    assert Decimal(checkpoint.state['usage']['cost_usd']) > 0


def test_review_role_keeps_text_contract_and_no_delivery_tool_grant():
    context = task()
    current = plan(context, discussion=True)
    assert all(not step.tools and not _is_incremental_workspace_contract_step(step) for step in current.steps)
    assert all(step.engineering_delivery_contract_version == 0 for step in current.steps)
