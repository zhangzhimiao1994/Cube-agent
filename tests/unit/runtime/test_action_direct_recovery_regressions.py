"""Approval recovery must preserve current inputs and own all pending storage I/O."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime, _binding
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.continuation_artifacts import load_continuation
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.instruction_context import InstructionContextLoader
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run
from tests.unit.runtime.test_action_direct import Backend, Policy
from tests.unit.runtime.test_direct_project_verification import Model, batch, context


class BlockingRepository(InMemoryArtifactRepository):
    def __init__(self, *, block_put: int | None = None) -> None:
        super().__init__()
        self.block_put = block_put
        self.put_count = 0
        self.block_reads = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def put(self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *,
                  write_id: UUID | None = None) -> None:
        self.put_count += 1
        if self.put_count == self.block_put:
            self.entered.set()
            await self.release.wait()
        await super().put(tenant_id, run_id, artifact, write_id=write_id)

    async def get_many(self, tenant_id: UUID, run_id: UUID,
                       references: tuple[ArtifactReference, ...]) -> tuple[Artifact, ...]:
        if self.block_reads:
            self.entered.set()
            await self.release.wait()
        return await super().get_many(tenant_id, run_id, references)


def runtime(backend: Backend, policy: Policy, repo: InMemoryArtifactRepository,
            model: Model, *, logical_model: str = 'main') -> ActionApprovedDirectRuntime:
    return ActionApprovedDirectRuntime(
        model, logical_model=logical_model, capability_gateway=backend, artifact_repository=repo,
        harness_tool_gateway=HarnessToolGateway(
            backend, policy_gateway=policy, require_actor_identity=True,
        ),
    )


async def pause_at_write(subject: ActionApprovedDirectRuntime, ctx: TaskContext) -> RuntimeCheckpoint:
    events = [event async for event in subject.run(ctx)]
    checkpoint = events[-1].checkpoint
    assert checkpoint is not None and checkpoint.state['tool_name'] == 'workspace.write_text'
    return checkpoint


async def collect(subject: ActionApprovedDirectRuntime, ctx: TaskContext,
                  events: list[RunEvent]) -> None:
    async for event in subject.run(ctx):
        events.append(event)


async def test_unchanged_loaded_instructions_survive_approval_recovery(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    backend.root.mkdir(parents=True)
    (backend.root/'AGENTS.md').write_text('Use project conventions.', encoding='utf-8')
    loader = InstructionContextLoader(
        repository=FakeRunRepository(stored_run(
            tenant=ctx.tenant_id, run=ctx.run_id,
            project_id='direct-verification', session='same-task',
        )), project_root=tmp_path/'workspaces',
    )
    instructions = await loader.load(tenant_id=ctx.tenant_id, run_id=ctx.run_id)
    assert instructions.sources[0].status == 'loaded'
    ctx = ctx.model_copy(update={'instruction_context': instructions})
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    policy.allowed.update({'write', 'create'})
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)

    events = [event async for event in resumed.run(ctx.model_copy(update={'checkpoint': checkpoint}))]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert (backend.root/'README.md').read_text() == 'Actual content'
    assert len(model.requests) == 1


async def test_changed_source_artifact_is_rejected_before_pending_write(tmp_path: Path) -> None:
    source = Artifact(id=uuid4(), type='text', producer='user', content={'text': 'SOURCE_V1'})
    ctx, policy, repo = context(artifacts=(source,)), Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Built from SOURCE_V1'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    changed = Artifact(id=source.id, type='text', producer='user', content={'text': 'SOURCE_V2'})
    assert changed.content_sha256 != source.content_sha256
    policy.allowed.update({'write', 'create'})
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)

    with pytest.raises(RuntimeExecutionError, match='(?:context|sources) changed'):
        _ = [event async for event in resumed.run(ctx.model_copy(update={
            'checkpoint': checkpoint, 'artifacts': (changed,),
        }))]

    assert not backend.root.exists()
    assert backend.calls == []
    assert len(model.requests) == 1


async def test_cancel_during_approval_save_cannot_publish_checkpoint(tmp_path: Path) -> None:
    ctx, policy = context(), Policy()
    # The small model observation is one put; the approval continuation is the next.
    repo = BlockingRepository(block_put=2)
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    subject = runtime(backend, policy, repo, model)
    events: list[RunEvent] = []
    consumer = asyncio.create_task(collect(subject, ctx, events))
    try:
        await asyncio.wait_for(repo.entered.wait(), timeout=2)
        await asyncio.wait_for(subject.cancel(), timeout=0.5)
        repo.release.set()
        await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=1)

        assert not any(event.kind is EventKind.CHECKPOINT_SAVED for event in events), (
            'cancel() returned before an approval checkpoint was published'
        )
        assert consumer.cancelled() or (
            consumer.exception() is None
            and any(event.kind is EventKind.RUNTIME_CANCELLED for event in events)
        )
        assert subject._active_token is None
        assert not backend.root.exists()
    finally:
        consumer.cancel()
        repo.release.set()
        await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=1)


async def test_cancel_interrupts_private_continuation_reference_load(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), BlockingRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    repo.block_reads = True
    events: list[RunEvent] = []
    consumer = asyncio.create_task(collect(
        resumed, ctx.model_copy(update={'checkpoint': checkpoint}), events,
    ))
    try:
        await asyncio.wait_for(repo.entered.wait(), timeout=2)
        await asyncio.wait_for(resumed.cancel(), timeout=0.5)

        assert consumer.done(), 'cancel() must join the blocked private artifact read'
        assert consumer.cancelled() or (
            consumer.exception() is None
            and any(event.kind is EventKind.RUNTIME_CANCELLED for event in events)
        )
        assert resumed._active_token is None
        assert resumed._active_stream is None
        assert backend.calls == []
        assert len(model.requests) == 1
    finally:
        consumer.cancel()
        repo.release.set()
        await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=1)


async def test_private_continuation_read_obeys_current_execution_timeout(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), BlockingRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    repo.block_reads = True
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            _ = [event async for event in resumed.run(ctx.model_copy(update={
                'checkpoint': checkpoint, 'timeout_seconds': 0.05,
            }))]
    assert asyncio.get_running_loop().time() - started < 0.5
    assert resumed._active_token is None
    assert resumed._active_stream is None
    assert resumed._active_capability_task is None
    assert not backend.root.exists()
    assert len(model.requests) == 1


@pytest.mark.parametrize(('key', 'value'), [
    ('project_delivery', 'text'), ('artifact_strategy', 'inline'), ('project_scale', 'none'),
    ('replace_workspace_files', True), ('website_preview_required', True),
    ('project_input_contract_version', None), ('project_input', None),
    ('generated_project_contract_version', None), ('generated_project_preparation', None),
    ('project_input_preparation', None), ('python_project_history', None),
    ('harness_decision', {'selected_logical_model': 'other'}),
])
async def test_changed_execution_contract_is_rejected_before_write(
    tmp_path: Path, key: str, value: JsonValue,
) -> None:
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    policy.allowed.update({'write', 'create'})
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match='context changed|contract.*unavailable'):
        _ = [event async for event in resumed.run(ctx.model_copy(update={
            'checkpoint': checkpoint, 'routing_decision': {**ctx.routing_decision, key: value},
        }))]
    assert backend.calls == []
    assert len(model.requests) == 1


async def test_changed_resolved_model_is_rejected_before_replay(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    resumed = runtime(backend, policy, repo, model, logical_model='other')
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError, match='context changed'):
        _ = [event async for event in resumed.run(ctx.model_copy(update={'checkpoint': checkpoint}))]
    assert backend.calls == []
    assert len(model.requests) == 1


def test_generated_contract_fingerprint_includes_frozen_manifest() -> None:
    from agent_hub.capabilities.generated_project_service import validate_generated_preparation

    ctx = context()
    preparation = validate_generated_preparation({
        'schema_version': 1, 'phase': 'capturing', 'user_id': str(ctx.actor_id),
        'request_sha256': hashlib.sha256(ctx.request.encode()).hexdigest(),
        'proposer_actor': 'main_agent', 'stack': 'node',
        'manifest_sha256': 'a' * 64, 'manifest_raw_sha256': 'b' * 64,
    })
    first = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'generated_project_contract_version': 1,
        'generated_project_preparation': preparation,
    }})
    changed = first.model_copy(update={'routing_decision': {
        **first.routing_decision,
        'generated_project_preparation': {**preparation, 'manifest_sha256': 'c' * 64},
    }})
    assert _binding(first) != _binding(changed)


async def test_approval_metadata_is_not_a_fixed_execution_contract(tmp_path: Path) -> None:
    ctx = context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'approval_kind': 'capability_tool', 'approval_id': 'approval_old',
        'approval_fingerprint': 'a' * 64, 'approval_scope': 'write',
        'approval_checkpoint_required': True, 'approval_checkpoint_worker_token': str(uuid4()),
        'harness_decision': {'selected_logical_model': 'main', 'requires_approval': True},
    }})
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    policy.allowed.update({'write', 'create'})
    route = {key: value for key, value in ctx.routing_decision.items()
             if not key.startswith('approval_')}
    route['harness_decision'] = {'selected_logical_model': 'main', 'requires_approval': False}
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(ctx.model_copy(update={
        'checkpoint': checkpoint, 'routing_decision': route,
    }))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 1


@pytest.mark.parametrize('key', [
    'runtime_token_absolute_tokens', 'main_agent_context_window_tokens', 'context_window_tokens',
])
async def test_current_hard_budget_cannot_be_replaced_by_snapshot(tmp_path: Path, key: str) -> None:
    ctx = context()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'runtime_token_soft_base_tokens': 50_000,
        'runtime_token_absolute_tokens': 50_000,
    }})
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'First'}, complete=False), batch({'README.md': 'Second'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    policy.allowed.update({'write', 'create'})
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in resumed.run(ctx.model_copy(update={
            'checkpoint': checkpoint, 'routing_decision': {**ctx.routing_decision, key: 1},
        }))]
    assert backend.calls == []
    assert len(model.requests) == 1


async def test_tightened_budgets_survive_another_approval(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), InMemoryArtifactRepository()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'context_window_tokens': 16_000,
    }})
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    policy.allowed.add('write')
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(ctx.model_copy(update={
        'checkpoint': checkpoint, 'token_budget': 40_000, 'timeout_seconds': 20,
        'routing_decision': {**ctx.routing_decision, 'runtime_token_absolute_tokens': 40_000,
                             'runtime_timeout_absolute_seconds': 20},
    }))]
    next_checkpoint = events[-1].checkpoint
    assert next_checkpoint is not None and next_checkpoint.state['tool_name'] == 'workspace.bundle'
    checkpoint = next_checkpoint
    reference = checkpoint.state['reference']
    assert isinstance(reference, Mapping)
    saved = await load_continuation(repo, tenant_id=ctx.tenant_id, run_id=ctx.run_id,
                                    reference=reference)
    assert isinstance(saved, Mapping) and isinstance(saved['context'], Mapping)
    durable = TaskContext.from_payload(json.loads(json.dumps(saved['context'], default=dict)))
    assert durable.token_budget == 40_000
    assert durable.timeout_seconds == 20
    assert durable.routing_decision['runtime_token_absolute_tokens'] == 40_000
    assert durable.routing_decision['runtime_timeout_absolute_seconds'] == 20


@pytest.mark.parametrize(('key', 'value'), [
    ('project_preflight_approved', True), ('project_preflight_proposal', {}),
    ('source', 'self_repair'), ('self_repair_context', {}),
])
async def test_unimplemented_guidance_is_rejected_before_model(
    tmp_path: Path, key: str, value: JsonValue,
) -> None:
    ctx = context()
    ctx = ctx.model_copy(update={'routing_decision': {**ctx.routing_decision, key: value}})
    policy, repo = Policy(), InMemoryArtifactRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    with pytest.raises(RuntimeExecutionError, match='guidance.*unsupported'):
        _ = [event async for event in runtime(backend, policy, repo, model).run(ctx)]
    assert model.requests == []
    assert backend.calls == []


async def test_cancel_at_private_commit_cannot_publish_checkpoint(tmp_path: Path) -> None:
    class CommitRepository(BlockingRepository):
        subject: ActionApprovedDirectRuntime | None = None
        cancellation: asyncio.Task[None] | None = None

        async def put(self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *,
                      write_id: UUID | None = None) -> None:
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            if self.put_count == 2:
                assert self.subject is not None
                self.cancellation = asyncio.create_task(self.subject.cancel())

    ctx, policy, repo = context(), Policy(), CommitRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    repo.subject = runtime(backend, policy, repo, model)
    events: list[RunEvent] = []
    consumer = asyncio.create_task(collect(repo.subject, ctx, events))
    try:
        await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=1)
        assert repo.cancellation is not None
        await asyncio.wait_for(repo.cancellation, timeout=0.5)
        assert not any(event.kind is EventKind.CHECKPOINT_SAVED for event in events)
    finally:
        consumer.cancel()
        if repo.cancellation is not None:
            repo.cancellation.cancel()
            await asyncio.gather(repo.cancellation, return_exceptions=True)
        await asyncio.gather(consumer, return_exceptions=True)


async def test_completed_private_save_time_counts_toward_elapsed_budget(tmp_path: Path) -> None:
    class SlowRepository(BlockingRepository):
        async def put(self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *,
                      write_id: UUID | None = None) -> None:
            if self.put_count == 1:
                await asyncio.sleep(0.06)
            await super().put(tenant_id, run_id, artifact, write_id=write_id)

    ctx, policy, repo = context(), Policy(), SlowRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    reference = checkpoint.state['reference']
    assert isinstance(reference, Mapping)
    value = await load_continuation(repo, tenant_id=ctx.tenant_id, run_id=ctx.run_id,
                                    reference=reference)
    assert isinstance(value, Mapping)
    elapsed = checkpoint.state.get('elapsed_seconds', value['elapsed'])
    assert isinstance(elapsed, int | float) and elapsed >= 0.05


async def test_saved_timeout_caps_the_next_private_read(tmp_path: Path) -> None:
    ctx, policy, repo = context(), Policy(), BlockingRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    policy.allowed.add('write')
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    events = [event async for event in resumed.run(ctx.model_copy(update={
        'checkpoint': checkpoint, 'timeout_seconds': 0.15,
    }))]
    next_checkpoint = events[-1].checkpoint
    assert next_checkpoint is not None and next_checkpoint.state['tool_name'] == 'workspace.bundle'
    checkpoint = next_checkpoint
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    repo.block_reads = True
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            _ = [event async for event in resumed.run(ctx.model_copy(update={'checkpoint': checkpoint}))]
    assert asyncio.get_running_loop().time() - started < 0.5
    assert resumed._active_token is None
    assert backend.calls.count('workspace.bundle') == 0


@pytest.mark.parametrize('key', ['runtime_timeout_absolute_seconds', 'runtime_timeout_seconds'])
async def test_current_routed_timeout_caps_private_read(tmp_path: Path, key: str) -> None:
    ctx, policy, repo = context(), Policy(), BlockingRepository()
    backend = Backend(tmp_path, ctx, repo)
    model = Model(batch({'README.md': 'Actual content'}))
    checkpoint = await pause_at_write(runtime(backend, policy, repo, model), ctx)
    resumed = runtime(backend, policy, repo, model)
    await resumed.restore_checkpoint(checkpoint)
    repo.block_reads = True
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            _ = [event async for event in resumed.run(ctx.model_copy(update={
                'checkpoint': checkpoint, 'routing_decision': {**ctx.routing_decision, key: 0.05},
            }))]
    assert asyncio.get_running_loop().time() - started < 0.5
    assert resumed._active_token is None
    assert backend.calls == []
