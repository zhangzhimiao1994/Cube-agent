"""Real central tool dispatch: invalid readonly query is rejected data, not a run failure."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.project_inspection import (
    ProjectInspectionError,
    PythonCatalogQueryFeedback,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime, DiscussionParticipant
from agent_hub.runtime.autogen.engineering_delivery import (
    engineering_progress,
    engineering_work_progress,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.engineering_actions import EngineeringProgress, required_engineering_action
from tests.integration.runtime.test_autogen_adapter import ScriptedGateway, context, plan
from tests.unit.capabilities.test_project_inspection import backend
from tests.unit.capabilities.test_scoped_read import (
    OTHER,
    RUN,
    TENANT,
    project_path,
    stored_run,
    write_file,
)
from tests.unit.harness.test_tool_gateway import FakePolicyGateway


def request(arguments: Mapping[str, object], *, key: str='catalog-query') -> HarnessToolCallRequest:
    return HarnessToolCallRequest(run_id=RUN, actor='analyst', tool_name='project.inspect',
                                  arguments=cast(Mapping[str, JsonValue], arguments), sandbox='read_only', approval_required=False,
                                  idempotency_key=key)


async def test_actual_harness_returns_bounded_rejection_and_accepts_explicit_fresh_inspection(tmp_path: Path) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{"dependencies":{}}')
    runtime = backend(tmp_path)
    harness = HarnessToolGateway(runtime)
    rejected = await harness.invoke(TENANT, request({'python_catalog': {
        'catalog_sha256': 'synthetic-api_key=NEVER-ECHO', 'names': ['wheel', 'setuptools'],
    }}))
    assert rejected.status == 'succeeded'  # The rejection observation was delivered; no inspection succeeded.
    assert rejected.payload['kind'] == 'project_inspection_query_feedback'
    assert rejected.payload['status'] == 'rejected'
    assert rejected.payload['reason_code'] == 'python_catalog_query_invalid'
    assert rejected.payload['inspection_performed'] is False
    assert 'python_build_profile' not in rejected.payload
    assert 'NEVER-ECHO' not in repr(rejected.payload)
    assert not (tmp_path / 'skills').exists()

    corrected = await harness.invoke(TENANT, request({}, key='fresh-catalog'))
    assert corrected.status == 'succeeded'
    assert cast(Mapping[str, JsonValue], corrected.payload['scope'])['run_id'] == str(RUN)
    assert 'manifests' in corrected.payload
    assert 'kind' not in corrected.payload


@pytest.mark.parametrize('record', [
    stored_run(tenant=OTHER), replace(stored_run(), id=OTHER),
    stored_run(project_id='../escape'), stored_run(requested_permissions=[]),
])
async def test_invalid_query_never_masks_an_unauthorized_workspace(tmp_path: Path, record: RunRecord) -> None:
    class Repository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
            return record

    result = await HarnessToolGateway(backend(tmp_path, Repository())).invoke(
        TENANT, request({'python_catalog': {'catalog_sha256': 'invalid'}}))
    assert result.status == 'failed'
    assert 'inspection_performed' not in result.payload
    assert result.failure_reason == 'project inspection scope unavailable'


async def test_unrelated_inspection_error_with_identical_text_still_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = backend(tmp_path)

    async def broken_profile(*args: object, **kwargs: object) -> NoReturn:
        raise ProjectInspectionError('python_catalog_query_invalid')

    monkeypatch.setattr(runtime, '_python_build_profile', broken_profile)
    result = await HarnessToolGateway(runtime).invoke(TENANT, request({}))
    assert result.status == 'failed'
    assert result.failure_reason == 'python_catalog_query_invalid'
    assert not result.payload


@pytest.mark.parametrize('mode', tuple(TaskMode))
async def test_all_run_modes_receive_the_same_central_rejection_observation(tmp_path: Path, mode: TaskMode) -> None:
    row = stored_run()
    authoritative = SimpleNamespace(tenant_id=row.tenant_id, id=row.id,
        routing_decision=row.routing_decision, actor_id=row.actor_id, mode=mode)

    class Repository:
        async def get(self, tenant_id: UUID, run_id: UUID) -> RunRecord:
            return cast(RunRecord, authoritative)

    runtime = backend(tmp_path, Repository())
    result = await HarnessToolGateway(runtime).invoke(TENANT, request({
        'python_catalog': {'catalog_sha256': 'a' * 64, 'offset': True},
    }))
    assert result.status == 'succeeded'
    assert result.payload['status'] == 'rejected' and result.payload['inspection_performed'] is False
    assert not (tmp_path / 'projects').exists(), 'Invalid query must not inspect or create a workspace'


async def test_denied_permission_is_not_converted_to_query_feedback(tmp_path: Path) -> None:
    harness = HarnessToolGateway(backend(tmp_path),
        policy_gateway=FakePolicyGateway(CapabilityStatus.DENIED))
    result = await harness.invoke(TENANT, request({'python_catalog': {'catalog_sha256': 'invalid'}}),
                                  user_id=OTHER, role=Role.OPERATOR)
    assert result.status == 'failed'
    assert result.payload.get('kind') != 'project_inspection_query_feedback'
    assert not (tmp_path / 'projects').exists()


async def test_uncertain_profile_exception_cannot_become_correctable_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = backend(tmp_path)

    async def unknown_profile(*args: object, **kwargs: object) -> NoReturn:
        raise TimeoutError('synthetic SDK source token=DO-NOT-EXPORT')

    monkeypatch.setattr(runtime, '_python_build_profile', unknown_profile)
    result = await HarnessToolGateway(runtime).invoke(TENANT, request({}))
    assert result.status == 'failed'
    assert result.payload.get('kind') != 'project_inspection_query_feedback'
    assert 'DO-NOT-EXPORT' not in repr(result)


async def test_query_feedback_never_reads_workspace_or_catalog_and_cannot_override_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.capabilities import project_inspection

    runtime = backend(tmp_path)

    def forbidden_read(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail('Rejected query must not observe workspace or catalog')

    monkeypatch.setattr(project_inspection, '_read_no_links', forbidden_read)
    monkeypatch.setattr(runtime, '_python_build_profile', forbidden_read)
    harness = HarnessToolGateway(runtime)
    result = await harness.invoke(TENANT, request({'python_catalog': {'catalog_sha256': 'invalid'}}))
    assert result.payload['status'] == 'rejected'
    override = await harness.invoke(TENANT, request({
        'python_catalog': {'catalog_sha256': 'invalid'}, 'project_id': 'foreign',
    }, key='override'))
    assert override.status == 'failed' and not override.payload


@pytest.mark.parametrize('engineering', [False, True], ids=['complete-discussion', 'engineering-checkpoint'])
async def test_real_autogen_can_correct_rejected_query_without_new_run_or_automatic_tool_retry(
    tmp_path: Path, engineering: bool,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/package.json', '{"dependencies":{}}')
    runtime = backend(tmp_path)
    harness = HarnessToolGateway(runtime, policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED))

    class Gateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__([])
            self.actor_turn = 0
            self.correction_context = ''

        async def complete_with_context(self, model_request: ModelRequest) -> GatewayCompletion:
            self.requests.append(model_request)
            if model_request.logical_model == 'selector':
                response = ModelResponse(text='analyst', usage=TokenUsage(1, 1, 2))
            else:
                self.actor_turn += 1
                if self.actor_turn == 1:
                    response = ModelResponse(text=None, tool_calls=(ToolCall(
                        id='query-bad', name='project_inspect', arguments={
                            'python_catalog': {'catalog_sha256': 'a' * 64, 'offset': True}}),),
                        usage=TokenUsage(1, 1, 2))
                elif self.actor_turn == 2:
                    self.correction_context = '\n'.join(str(message.content) for message in model_request.messages)
                    response = ModelResponse(text=None, tool_calls=(ToolCall(
                        id='query-corrected', name='project_inspect', arguments={}),),
                        usage=TokenUsage(1, 1, 2))
                else:
                    response = ModelResponse(text='[COMPLETE] Inspection observed; no installation claimed.',
                                             usage=TokenUsage(1, 1, 2))
            return GatewayCompletion(response=response, deployment_id='shared',
                logical_model=model_request.logical_model, provider_id='mock',
                provider_model='mock/local', cost_usd=Decimal(0))

    gateway = Gateway()
    actor = DiscussionParticipant(id='analyst', role='Analyst', goal='Inspect the project',
        logical_model='shared', allowed_tools=(('project.inspect', 'workspace.write_text')
                                              if engineering else ('project.inspect',)))
    repository = InMemoryArtifactRepository()
    discussion = AutoGenDiscussionRuntime(gateway,
        plan(selector_model='selector', participants=(actor, plan().participants[1]),
             max_turns=4, wall_time_seconds=10), capability_gateway=runtime,
        harness_tool_gateway=harness, artifact_repository=repository)
    ctx = context(run_id=RUN, tenant_id=TENANT, actor_id=OTHER, actor_role=Role.OPERATOR,
        routing_decision=({**cast(Mapping[str, object], stored_run().routing_decision),
                          'engineering_delivery_contract_version': 1, 'engineering_action_version': 1,
                          'engineering_progress_budget_version': 1,
                          'engineering_checkpoint_ack_version': 1, 'engineering_soft_admission_version': 1}
                          if engineering else {}))
    events = []
    corrected_checkpoint = None
    observed_checkpoints = []
    async with asyncio.timeout(12):
        stream = discussion.run(ctx)
        try:
            async for event in stream:
                events.append(event)
                if event.kind is EventKind.TOOL_COMPLETED:
                    observed_checkpoints.append(await discussion.save_checkpoint())
                if event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == 'query-corrected':
                    corrected_checkpoint = await discussion.save_checkpoint()
                    if engineering:
                        break  # Inspect continuation only; no fake write/verify/bundle delivery.
        finally:
            await cast(AsyncGenerator[RunEvent, None], stream).aclose()
    if not engineering:
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(e.kind, e.reason) for e in events[-5:]]
    assert not any(e.kind is EventKind.RUNTIME_FAILED for e in events), [(e.kind, e.reason) for e in events[-5:]]
    outcomes = [e for e in events if e.kind is EventKind.TOOL_COMPLETED]
    assert len(outcomes) == 2
    assert [e.tool_call_id for e in outcomes] == ['query-bad', 'query-corrected']
    assert corrected_checkpoint is not None
    checkpoint = RuntimeCheckpoint.from_payload(json.loads(json.dumps(corrected_checkpoint.to_payload())))
    ledger = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state['tool_ledger'])
    assert len(ledger) == (2 if engineering else 1)
    assert all(row['status'] == 'succeeded' for row in ledger)
    assert len(observed_checkpoints) == 2
    assert all(e.run_id == RUN for e in events)
    assert gateway.actor_turn >= 2
    assert len([row for row in cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state['model_ledger'])
                if row.get('status') == 'succeeded']) <= ctx.token_budget // 2
    if engineering:
        artifacts = await repository.get_many(TENANT, RUN, tuple(
            ArtifactReference(UUID(cast(str, row['artifact_id'])), cast(str, row['artifact_sha256'])) for row in ledger))
        tools = {'analyst': actor.allowed_tools}
        rejected_progress = engineering_progress(ctx, tool_entries=ledger[:1], artifacts=artifacts,
                                                 allowed_tools=tools)
        assert rejected_progress == EngineeringProgress()
        assert required_engineering_action(rejected_progress) == 'project.inspect'
        assert not engineering_work_progress(ctx, tool_entries=ledger[:1], artifacts=artifacts,
            allowed_tools=tools, include_bootstrap_observations=True)
        corrected_progress = engineering_progress(ctx, tool_entries=ledger, artifacts=artifacts,
                                                  allowed_tools=tools)
        assert corrected_progress.inspected and not corrected_progress.uncertain
        assert not corrected_progress.verification_current and not corrected_progress.bundle_current
        assert [r.required_tool for r in gateway.requests if r.logical_model == 'shared'][:2] == [
            'project_inspect', 'project_inspect']
    assert 'project_inspection_query_feedback' in gateway.correction_context
    assert 'inspection_performed' in gateway.correction_context
    assert not any(e.kind in {'repair.classified', 'repair.skipped', 'approval.requested'} for e in events)
    assert all(0 < r.timeout_seconds <= ctx.timeout_seconds for r in gateway.requests)


@pytest.mark.parametrize('engine', ['autogen', 'crew'])
@pytest.mark.parametrize('defect', [None, 'extra', 'bool_version', 'performed', 'reason', 'hash', 'unknown'])
def test_trusted_rejection_is_neutral_engineering_evidence(engine: str, defect: str | None) -> None:
    from agent_hub.runtime.crew.engineering_delivery import engineering_action_progress
    from tests.unit.runtime.autogen.test_engineering_actions import action_context
    from tests.unit.runtime.autogen.test_engineering_delivery import append_result

    ctx = action_context()
    value = dict(PythonCatalogQueryFeedback().to_payload())
    if defect == 'extra':
        value['dependencies_truncated'] = False
    elif defect == 'bool_version':
        value['schema_version'] = True
    elif defect == 'performed':
        value['inspection_performed'] = True
    elif defect == 'reason':
        value['reason_code'] = 'other'
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    append_result(rows, artifacts, 'project.inspect', value)
    if defect == 'hash':
        rows[0]['artifact_sha256'] = '0' * 64
    elif defect == 'unknown':
        rows[0]['status'] = 'UNKNOWN'
    if engine == 'autogen':
        progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                        allowed_tools={'engineer': ('project.inspect',)})
        assert not engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
            allowed_tools={'engineer': ('project.inspect',)}, include_bootstrap_observations=True)
    else:
        from agent_hub.runtime.contracts import Artifact

        original = artifacts[0]
        artifact = Artifact(id=original.id, type=original.type, producer=original.producer,
            source_ids=(str(ctx.run_id),), content={**original.content, 'agent_id': 'engineer'})
        key = 'native-tool'
        state: dict[str, JsonValue] = {'step_id': 'step', 'attempt': 0, 'round': 0, 'tool_index': 0,
            'name': 'project.inspect', 'status': rows[0]['status'],
            'arguments_sha256': rows[0]['arguments_hash'], 'artifact_id': str(artifact.id),
            'sha256': '0' * 64 if defect == 'hash' else artifact.content_sha256}
        progress = engineering_action_progress(context=ctx, step_id='step', actor='engineer',
            states={key: state}, artifacts={key: artifact}, attempt=0, round_index=1)
    if defect is None:
        assert progress == EngineeringProgress()
        assert required_engineering_action(progress) == 'project.inspect'
    else:
        assert progress.uncertain
