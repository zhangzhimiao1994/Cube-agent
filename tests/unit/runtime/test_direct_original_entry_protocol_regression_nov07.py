"""Original-entry protocol regression: scripted wire responses, real runtime/tools.

No provider calls, delivery overrides, fabricated bundle, or product monkeypatches.
The reused native executor is a report fixture (not a host/native execution test).
The entry regression fails before that executor or any workspace tool is called.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime import direct
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext, _freeze_object
from agent_hub.runtime.generated_project_history import generated_history_selected
from tests.unit.runtime.test_direct_original_project import batch, fixture, prompt
from tests.unit.runtime.test_direct_project_verification import Model, Policy

PROSE = 'I will inspect the existing source, repair the defect, and run the original checks.'
PATCH = 'module.exports = () => 12;\n'


class PricedModel(Model):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        return replace(await super().complete_with_context(request), cost_usd=Decimal('0.001'))


def assert_received_responses_accounted(
    events: Sequence[RunEvent], model: Model, context: TaskContext,
) -> None:
    calls = len(model.requests)
    assert calls == 2, 'One correction of a known response; repeated unchanged prose must stop'
    assert not model.texts
    assert sum(e.kind is EventKind.MODEL_STARTED for e in events) == calls
    assert sum(e.kind is EventKind.MODEL_COMPLETED for e in events) == calls
    costs = [e for e in events if e.kind is EventKind.COST_RECORDED]
    assert len(costs) == calls
    assert all(e.cost_usd == Decimal('0.001') and e.payload['total_tokens'] == 180 for e in costs)
    token_counts = [e.payload['total_tokens'] for e in costs]
    assert all(isinstance(value, int) for value in token_counts)
    assert sum(value for value in token_counts if isinstance(value, int)) <= context.token_budget
    assert sum(e.cost_usd for e in costs if e.cost_usd is not None) == Decimal('0.002')
    assert all(0 < r.timeout_seconds <= context.timeout_seconds for r in model.requests)
    assert all(0 < r.max_output_tokens <= context.token_budget for r in model.requests)
    assert all(r.logical_model == model.requests[0].logical_model for r in model.requests)
    assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
    assert EventKind.COST_RECORDED.value == 'cost.recorded'


def entry_context() -> TaskContext:
    """Routing-only fixture; no baseline or delivery receipt is fabricated."""
    return TaskContext(run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        mode=TaskMode.DIRECT, request='Repair the existing project.', token_budget=50_000,
        timeout_seconds=60, routing_decision=_freeze_object({
            'project_id': 'existing', 'workspace_session_id': 'same-session',
            'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
            'requested_permissions': ['workspace.read', 'workspace.write', 'command.run', 'network.read'],
            'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
            'project_scale': 'medium', 'project_input_contract_version': 1,
            'project_input': ManagedProjectInput(('src',)).to_payload(),
            'engineering_diagnostic_reads_version': 1, 'engineering_catalog_reads_version': 1,
        }, name='entry routing'))


class EntryBackend:
    """Discovery only: any accidental execution fails instead of granting a tool."""
    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return True

    def is_replay_safe(self, name: str) -> bool:
        raise AssertionError('entry-only regression cannot replay capabilities')

    async def execute(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
        arguments: Mapping[str, JsonValue], idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        raise AssertionError('entry-only regression cannot execute capabilities')


async def test_portable_original_received_prose_gets_a_bounded_protocol_correction() -> None:
    context = entry_context()
    model = Model(PROSE, PROSE)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 2, (
        'RED: original entry consumed one real GatewayCompletion, then rejected missing bundle '
        'without allowing a same-budget protocol correction')
    assert len(model.texts) == 0
    assert all(request.timeout_seconds <= context.timeout_seconds for request in model.requests)


async def test_portable_repeated_prose_rejected_with_each_received_response_accounted() -> None:
    context = entry_context()
    model = PricedModel(PROSE, PROSE)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError):
        async for event in runtime.run(context):
            events.append(event)
    assert_received_responses_accounted(events, model, context)


async def test_portable_received_priced_response_emits_usage_before_delivery_failure() -> None:
    model = PricedModel(PROSE, PROSE)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    events = []
    with pytest.raises(direct.RuntimeExecutionError):
        async for event in runtime.run(entry_context()):
            events.append(event)
    assert sum(e.kind is EventKind.MODEL_COMPLETED for e in events) == len(model.requests), (
        'Every received response needs its completion/usage event even if delivery is rejected')
    costs = [e for e in events if e.kind is EventKind.COST_RECORDED]
    assert len(costs) == len(model.requests)
    assert all(e.cost_usd == Decimal('0.001') and e.payload['total_tokens'] == 180 for e in costs)


async def test_portable_exhausted_usage_does_not_buy_protocol_continuation() -> None:
    model = Model(PROSE, PROSE, usage=50_001)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    with pytest.raises(direct.RuntimeExecutionError):
        _ = [event async for event in runtime.run(entry_context())]
    assert len(model.requests) == 1


async def test_portable_expired_deadline_does_not_restart_for_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpiringModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            response = await super().complete_with_context(request)
            loop = asyncio.get_running_loop()
            expired = loop.time() + 61
            monkeypatch.setattr(loop, 'time', lambda: expired)
            return response

    model = ExpiringModel(PROSE, PROSE)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    with pytest.raises(direct.RuntimeExecutionError):
        _ = [event async for event in runtime.run(entry_context())]
    assert len(model.requests) == 1


async def test_portable_unknown_outcome_is_not_a_protocol_rejection() -> None:
    class UnknownModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise RuntimeError('unknown_outcome_no_replay')

    model = UnknownModel()
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    with pytest.raises(direct.RuntimeExecutionError):
        _ = [event async for event in runtime.run(entry_context())]
    assert len(model.requests) == 1


async def test_portable_two_rejection_shapes_charge_budget_before_first_scoped_read() -> None:
    context = entry_context().model_copy(update={'token_budget': 25_000})
    model = PricedModel(PROSE, json.dumps({'summary': 'A malformed file envelope.'}),
        batch(reads=['src/detail.js']), usage=8_000)
    policy = Policy(context, CapabilityStatus.DENIED)
    shared = HarnessToolGateway(EntryBackend(), policy_gateway=policy, require_actor_identity=True)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend(),
        harness_tool_gateway=shared)
    events = []
    with pytest.raises(direct.RuntimeExecutionError, match='workspace read'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(model.requests) == 3 and not model.texts
    assert len(policy.calls) == 1 and policy.calls[0].operation == 'read'
    assert policy.calls[0].resource == 'workspace/src/detail.js'
    for index, request in enumerate(model.requests):
        assert request.max_output_tokens + index * 8_000 <= context.token_budget
        assert request.timeout_seconds <= context.timeout_seconds
    costs = [event for event in events if event.kind is EventKind.COST_RECORDED]
    assert len(costs) == 3 and sum(event.payload['total_tokens'] == 8_000 for event in costs) * 8_000 == 24_000
    assert sum(event.kind is EventKind.MODEL_COMPLETED for event in events) == 3
    assert not any(event.tool_name == 'project.inspect' for event in events)
    assert runtime._generated_model_events is None and runtime._generated_model_run_id is None
    assert runtime._active_task is None and runtime._active_capability_task is None


async def test_portable_queue_releases_after_failure_and_same_runtime_can_run_again() -> None:
    model = PricedModel(PROSE, PROSE, PROSE, PROSE)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=EntryBackend())
    run_call_ids = []
    for _ in range(2):
        events = []
        with pytest.raises(direct.RuntimeExecutionError):
            async for event in runtime.run(entry_context()):
                events.append(event)
        assert sum(e.kind is EventKind.MODEL_COMPLETED for e in events) == 2
        assert sum(e.kind is EventKind.COST_RECORDED for e in events) == 2
        starts = {e.payload['call_id'] for e in events if e.kind is EventKind.MODEL_STARTED}
        completed = {e.payload['call_id'] for e in events if e.kind is EventKind.MODEL_COMPLETED}
        assert len(starts) == 2 and starts == completed
        run_call_ids.append(starts)
        assert runtime._active_token is None and runtime._generated_model_events is None
    assert run_call_ids[0].isdisjoint(run_call_ids[1])
    assert len(model.requests) == 4 and not model.texts


@pytest.mark.parametrize('first', [
    PROSE,
    '{"summary":"I will inspect the existing source before implementing the repair."}',
])
async def test_original_first_nonbatch_response_continues_to_real_read_verify_bundle(
    tmp_path: Path, first: str,
) -> None:
    configured = await fixture(tmp_path)
    assert direct._original_project_policy(configured.context)
    assert direct._is_workspace_project_delivery(configured.context)
    assert not generated_history_selected(configured.context)
    protected = {p: (configured.source / p).read_bytes()
                 for p in ('tests/original.js', 'package.json')}
    model = Model(first, batch(reads=['src/detail.js']),
                  batch(files={'src/index.js': PATCH}, complete=True))
    runtime, _, _ = configured.runtime(model)
    events = []
    try:
        async for event in runtime.run(configured.context):
            events.append(event)
    except direct.RuntimeExecutionError as error:
        # Show the precise missing bootstrap, rather than a fixture/import failure.
        assert str(error) == 'project workspace bundle is missing'
        assert len(model.requests) == 1
        assert configured.backend.calls == []
        assert len(configured.executor.calls) == 1  # Original failure observation only.
        pytest.fail('original entry rejected a received nonbatch response before protocol '
                    'correction; expected same-budget read -> patch -> verify -> bundle')
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 3
    names = [name for name, _ in configured.backend.calls]
    assert names[0] == 'read_context'
    assert names[-2:] == ['project.verify', 'workspace.bundle']
    assert (configured.source / 'src/index.js').read_text() == PATCH
    assert all((configured.source / p).read_bytes() == data for p, data in protected.items())
    assert 'UNTRUSTED_WORKSPACE_READ_JSON' in prompt(model.requests[-1])
    assert 'expected total 12, got 10' in prompt(model.requests[-1])
    assert all(not request.tools for request in model.requests)
    assert all(0 < request.timeout_seconds <= configured.context.timeout_seconds
               for request in model.requests)
    assert all(0 < request.max_output_tokens <= configured.context.token_budget
               for request in model.requests)


async def test_valid_first_batch_control_uses_real_capability_chain(tmp_path: Path) -> None:
    configured = await fixture(tmp_path)
    model = Model(batch(reads=['src/detail.js']), batch(files={'src/index.js': PATCH}, complete=True))
    runtime, _, _ = configured.runtime(model)
    events = [event async for event in runtime.run(configured.context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert [name for name, _ in configured.backend.calls] == [
        'read_context', 'workspace.write_text', 'workspace.write_text', 'project.verify', 'workspace.bundle']
    assert (configured.source / 'src/index.js').read_text() == PATCH


async def test_repeated_prose_refuses_delivery_but_records_every_response(tmp_path: Path) -> None:
    configured = await fixture(tmp_path)
    model = PricedModel(PROSE, PROSE)
    runtime, _, _ = configured.runtime(model)
    events = []
    with pytest.raises(direct.RuntimeExecutionError):
        async for event in runtime.run(configured.context):
            events.append(event)
    assert_received_responses_accounted(events, model, configured.context)
    assert configured.backend.calls == []
    assert len(configured.executor.calls) == 1  # Original failure, never a candidate success.
    assert (configured.source / 'src/index.js').read_text() != PATCH


async def test_original_read_denied_cannot_write_verify_or_bundle(tmp_path: Path) -> None:
    configured = await fixture(tmp_path)
    model = Model(batch(reads=['src/detail.js']), batch(files={'src/index.js': PATCH}, complete=True))
    policy = Policy(configured.context, CapabilityStatus.DENIED)
    shared = HarnessToolGateway(configured.backend, policy_gateway=policy, require_actor_identity=True)
    runtime = direct.DirectRuntime(model, logical_model='main', capability_gateway=configured.backend,
                                   harness_tool_gateway=shared)
    with pytest.raises(direct.RuntimeExecutionError):
        _ = [event async for event in runtime.run(configured.context)]
    assert len(model.requests) == 1
    assert configured.backend.calls == []
    assert (configured.source / 'src/index.js').read_text() != PATCH


async def test_unknown_gateway_outcome_never_becomes_protocol_response(tmp_path: Path) -> None:
    configured = await fixture(tmp_path)

    class UnknownModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise RuntimeError('unknown_outcome_no_replay')

    model = UnknownModel()
    runtime, _, _ = configured.runtime(model)
    with pytest.raises(direct.RuntimeExecutionError):
        _ = [event async for event in runtime.run(configured.context)]
    assert len(model.requests) == 1
    assert configured.backend.calls == []


async def test_plain_chat_has_no_workspace_bootstrap_authority(tmp_path: Path) -> None:
    configured = await fixture(tmp_path)
    context = configured.context.model_copy(update={'routing_decision': {}, 'artifacts': ()})
    model = Model('A plain explanation.')
    runtime, _, _ = configured.runtime(model)
    events = [event async for event in runtime.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 1 and configured.backend.calls == []
