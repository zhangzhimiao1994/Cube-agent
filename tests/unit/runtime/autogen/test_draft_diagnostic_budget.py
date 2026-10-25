"""A new causal draft diagnosis gets repair runway, never execution credit."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.engineering_delivery import (
    engineering_delivery_ready,
    engineering_work_progress,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent
from tests.unit.runtime.autogen.test_draft_action_progress import (
    ALLOWED,
    append,
    draft_case,
    result,
)
from tests.unit.runtime.autogen.test_soft_model_admission import Journey, run_to_write
from tests.unit.runtime.test_generated_draft_autogen import draft_artifact


def test_new_draft_diagnosis_earns_one_replay_stable_hint_not_delivery() -> None:
    ctx, rows, artifacts = draft_case()

    def hints() -> frozenset[str]:
        return engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
            allowed_tools=ALLOWED, include_draft_diagnoses=True)

    assert len(hints()) == 2  # one productive write and one causal diagnosis
    assert any(key.startswith('draft.diagnosis:') for key in hints())
    assert not engineering_delivery_ready(run_id=ctx.run_id, tool_entries=rows,
        artifacts=artifacts, allowed_tools=ALLOWED, context=ctx)
    expected = hints()
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert engineering_work_progress(ctx, tool_entries=[dict(row) for row in rows],
        artifacts=replay, allowed_tools=ALLOWED, include_draft_diagnoses=True) == expected
    assert len(engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=ALLOWED)) == 1, 'legacy callers do not opt into diagnostic runway'
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='c' * 64), status='failed')
    assert hints() == expected, 'changed failure text without new work cannot renew'
    append(ctx, rows, artifacts, result('workspace.write_text', {
        'path': 'ACCEPTANCE.json', 'sha256': 'b' * 64, 'size_bytes': 10}))
    append(ctx, rows, artifacts, draft_artifact(ctx), status='failed')
    assert hints() == expected, 'no-op writes cannot buy another diagnosis'
    append(ctx, rows, artifacts, result('workspace.write_text', {
        'path': 'package.json', 'sha256': 'c' * 64, 'size_bytes': 10}))
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='d' * 64), status='failed')
    assert len(hints()) == len(expected) + 2, 'new productive work can earn its own diagnosis'
    append(ctx, rows, artifacts, result('workspace.write_text', {
        'path': 'package.json', 'sha256': 'd' * 64, 'size_bytes': 11}))
    before = hints()
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='d' * 64), status='failed')
    assert hints() == before, 'a repeated observation cannot buy another diagnosis'
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='e' * 64), status='failed')
    assert hints() == before, 'a repeated diagnosis consumes the intervening write'


def test_diagnosis_without_prior_productive_write_has_no_hint() -> None:
    ctx, _, _ = draft_case()
    rows, artifacts = [], []
    append(ctx, rows, artifacts, draft_artifact(ctx), status='failed')
    assert not engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=ALLOWED, include_draft_diagnoses=True)


class DiagnosticJourney(Journey):
    def __init__(self) -> None:
        super().__init__()
        self.correction_seen = False

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        if request.tool_name != 'project.verify':
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        assert tenant_id == self.context.tenant_id and user_id == self.context.actor_id
        assert role == self.context.actor_role
        self.calls.append(request)
        artifact = draft_artifact(self.context, call=request.call_id)
        value = artifact.content['result']
        assert isinstance(value, Mapping)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='failed', failure_reason='generated project draft requires correction',
            payload={'generated_draft_rejection': value['generated_draft_rejection'],
                     'call_id': request.call_id, 'arguments_sha256': artifact.content['arguments_sha256']})

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.required_tool == 'project_verify' or (
            any(call.tool_name == 'workspace.write_text' for call in self.calls)
            and not any(call.tool_name == 'project.verify' for call in self.calls)
        ):
            self.requests.append(request)
            await self.cross_soft()
            call = ToolCall(id='late-diagnosis', name='project_verify', arguments={})
        elif any(call.tool_name == 'project.verify' for call in self.calls):
            self.requests.append(request)
            text = '\n'.join(message.content for message in request.messages)
            assert 'generated_draft_rejection' in text
            self.correction_seen = True
            call = ToolCall(id='correct-draft', name='workspace_write_text',
                arguments={'path': 'package.json', 'content': 'corrected declaration'})
        else:
            return await super().complete_with_context(request)
        return GatewayCompletion(response=ModelResponse(text=None, tool_calls=(call,),
            usage=TokenUsage(1, 1, 2)), deployment_id='test', logical_model=request.logical_model,
            provider_id='test', provider_model='test/model', cost_usd=Decimal(0))


@pytest.mark.asyncio
async def test_admitted_late_draft_rejection_can_reach_next_repair_model() -> None:
    case = DiagnosticJourney()
    events = []
    stream = cast(AsyncGenerator[RunEvent, None], case.runner.run(case.context))
    saved = None
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                events.append(event)
                if event.checkpoint is not None and event.checkpoint.state.get('engineering_tool_prefix') == 5:
                    saved = event.checkpoint
                    break
    finally:
        await stream.aclose()
    assert case.correction_seen, [(event.kind, event.reason) for event in events[-3:]]
    assert saved is not None
    assert [call.tool_name for call in case.calls] == [
        'project.inspect', 'workspace.list', 'workspace.write_text', 'project.verify', 'workspace.write_text']
    assert 0 < cast(float, saved.state['remaining_timeout_seconds']) <= 0.3
    assert 0 < cast(float, saved.state['remaining_absolute_timeout_seconds']) < 2.0
    case.runner._validate_checkpoint(saved, case.context)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


class BootstrapJourney(Journey):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.required_tool == 'workspace_list':
            await self.cross_soft()
        return await super().complete_with_context(request)


@pytest.mark.asyncio
async def test_completed_initial_inventory_crossing_soft_can_reach_first_write() -> None:
    case = BootstrapJourney()
    events, saved = await run_to_write(case)
    assert saved is not None, [(event.kind, event.reason) for event in events[-3:]]
    assert [call.tool_name for call in case.calls] == [
        'project.inspect', 'workspace.list', 'workspace.write_text']
    assert 0 < cast(float, saved.state['remaining_timeout_seconds']) <= 0.3
    assert 0 < cast(float, saved.state['remaining_absolute_timeout_seconds']) < 2.0
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


def test_initial_observations_are_two_once_only_hints_and_replay_stable() -> None:
    ctx, _, original = draft_case()
    rows, artifacts = [], []
    inspected = {**cast(Mapping[str, JsonValue], original[0].content['result']),
                 'dependencies_truncated': False}
    append(ctx, rows, artifacts, result('project.inspect', inspected))
    append(ctx, rows, artifacts, result('workspace.list', {'workspace_files': (), 'truncated': False}))
    tools = {actor: (*names, 'workspace.list') for actor, names in ALLOWED.items()}

    def hints() -> frozenset[str]:
        return engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
            allowed_tools=tools, include_bootstrap_observations=True)

    expected = frozenset({'bootstrap.project.inspect', 'bootstrap.workspace.list'})
    assert hints() == expected
    for _ in range(3):
        append(ctx, rows, artifacts, result('project.inspect', inspected))
        append(ctx, rows, artifacts, result('workspace.list', {'workspace_files': (), 'truncated': False}))
        assert hints() == expected
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert engineering_work_progress(ctx, tool_entries=[dict(row) for row in rows],
        artifacts=replay, allowed_tools=tools, include_bootstrap_observations=True) == expected
    assert not engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts, allowed_tools=tools)


@pytest.mark.parametrize('inventory', [
    {'workspace_files': ()},
    {'workspace_files': (), 'truncated': True},
    {'workspace_files': (), 'truncated': 0},
    {'summary': 'empty workspace'},
    {'workspace_files': ({'path': '../other.py', 'sha256': 'b' * 64, 'size_bytes': 1},), 'truncated': False},
    {'workspace_files': ({'path': 'x.py', 'sha256': 'b' * 64, 'size_bytes': 1},) * 2, 'truncated': False},
])
def test_incomplete_or_invalid_initial_inventory_has_no_hint(inventory: dict[str, JsonValue]) -> None:
    ctx, _, original = draft_case()
    rows, artifacts = [], []
    inspected = {**cast(Mapping[str, JsonValue], original[0].content['result']),
                 'dependencies_truncated': False}
    append(ctx, rows, artifacts, result('project.inspect', inspected))
    append(ctx, rows, artifacts, result('workspace.list', inventory))
    tools = {actor: (*names, 'workspace.list') for actor, names in ALLOWED.items()}
    assert engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=tools, include_bootstrap_observations=True) == {'bootstrap.project.inspect'}


def test_initial_observation_credit_cannot_start_after_a_write() -> None:
    ctx, _, original = draft_case()
    rows, artifacts = [], []
    append(ctx, rows, artifacts, original[1])
    append(ctx, rows, artifacts, original[0])
    append(ctx, rows, artifacts, result('workspace.list', {'workspace_files': ()}))
    tools = {actor: (*names, 'workspace.list') for actor, names in ALLOWED.items()}
    hints = engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=tools, include_bootstrap_observations=True)
    assert len(hints) == 1 and all(key.startswith('workspace.write_text:') for key in hints)


def test_prepared_context_rebuilds_causal_history_but_has_no_fresh_bootstrap() -> None:
    ctx, _, original = draft_case()
    rows, artifacts = [], []
    inspected = {**cast(Mapping[str, JsonValue], original[0].content['result']),
                 'dependencies_truncated': False}
    append(ctx, rows, artifacts, result('project.inspect', inspected))
    append(ctx, rows, artifacts, result('workspace.list', {'workspace_files': (), 'truncated': False}))
    tools = {actor: (*names, 'workspace.list') for actor, names in ALLOWED.items()}
    prepared = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'generated_project_preparation': {'phase': 'captured'}}})
    for _ in range(3):
        assert not engineering_work_progress(prepared, tool_entries=rows, artifacts=artifacts,
            allowed_tools=tools, include_bootstrap_observations=True)
    append(ctx, rows, artifacts, original[1])
    prior = engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=tools, include_bootstrap_observations=True)
    assert len(prior) == 3
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert engineering_work_progress(prepared, tool_entries=[dict(row) for row in rows], artifacts=replay,
        allowed_tools=tools, include_bootstrap_observations=True) == prior


def test_prepared_context_cannot_reconstruct_bootstrap_from_noop_write() -> None:
    ctx, _, original = draft_case()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'generated_project_preparation': {'phase': 'captured'}}})
    rows, artifacts = [], []
    metadata: dict[str, JsonValue] = {'path': 'package.json', 'sha256': 'b' * 64, 'size_bytes': 10}
    inspected = {**cast(Mapping[str, JsonValue], original[0].content['result']),
        'manifests': ({'path': 'package.json', 'status': 'observed', 'sha256': 'b' * 64,
                       'observed_bytes': 10},), 'dependencies_truncated': False}
    append(ctx, rows, artifacts, result('project.inspect', inspected))
    append(ctx, rows, artifacts, result('workspace.list', {'workspace_files': (metadata,), 'truncated': False}))
    append(ctx, rows, artifacts, result('workspace.write_text', metadata))
    tools = {actor: (*names, 'workspace.list') for actor, names in ALLOWED.items()}
    assert not engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=tools, include_bootstrap_observations=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('policy,source', [
    (None, 'project_scale_soft_budget'), (True, 'project_scale_soft_budget'),
    (2, 'project_scale_soft_budget'), (1, 'explicit'),
])
async def test_initial_observations_do_not_enable_legacy_or_explicit_renewal(
    policy: JsonValue, source: str,
) -> None:
    case = BootstrapJourney(policy=policy, source=source)
    events, saved = await run_to_write(case)
    assert saved is None and not any(call.tool_name == 'workspace.write_text' for call in case.calls)
    assert any(event.kind is EventKind.RUNTIME_FAILED for event in events)
