from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CrewAIObjectFactory,
    CrewDispatchRuntime,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _ModelLedger,
    _RunState,
    _RunToken,
)
from agent_hub.runtime.instruction_context import InstructionContextLoader, model_request_sha256
from agent_hub.runtime.project_scale_artifact import ProjectScaleArtifactPreseedRuntime
from tests.integration.runtime.test_crew_adapter import (
    FakeCapabilities,
    FakeGateway,
    FastFactory,
    FastGeneration,
    ToolGateway,
    one_step_plan,
)
from tests.unit.capabilities.test_scoped_read import (
    FakeRunRepository,
    project_path,
    stored_run,
    write_file,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import _reviewed_step_plan
from tests.unit.runtime.test_hybrid import RecordingHarnessToolGateway, UnusedRuntime


async def task(root: Path, *, marker: str = 'PRIVATE-GUIDANCE',
               tenant: UUID | None = None, run: UUID | None = None) -> TaskContext:
    tenant, run = tenant or uuid4(), run or uuid4()
    write_file(root, f'{project_path(tenant)}/AGENTS.md', marker)
    repository = FakeRunRepository(stored_run(tenant=tenant, run=run))
    bundle = await InstructionContextLoader(repository=repository, project_root=root).load(
        tenant_id=tenant, run_id=run,
    )
    return TaskContext(run_id=run, tenant_id=tenant, mode=TaskMode.DISPATCH,
                       actor_id=uuid4(), actor_role=Role.OPERATOR,
                       request='Write the requested project', instruction_context=bundle,
                       token_budget=20_000)


def injections(events: list[RunEvent]) -> list[RunEvent]:
    return [event for event in events if event.kind == 'context.injected']


@pytest.mark.parametrize('real_crew', [False, True])
async def test_worker_and_reviewer_requests_have_private_guidance_and_ledger_evidence(
    tmp_path: Path, real_crew: bool,
) -> None:
    context = await task(tmp_path)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=(
        CrewAIObjectFactory(storage_dir=tmp_path / 'crew') if real_crew else FastFactory()
    ))
    events = [event async for event in runtime.run(context)]
    actual = injections(events)
    assert len(actual) == len(gateway.requests) == 3
    assert {event.payload['stage'] for event in actual} == {'dispatch_step', 'dispatch_review'}
    checkpoint = await runtime.save_checkpoint()
    ledger = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state['models'])
    assert context.instruction_context is not None
    for request, event in zip(gateway.requests, actual, strict=True):
        text = '\n'.join(str(message.content) for message in request.messages)
        assert text.count('<PROJECT_GUIDANCE_JSON>') == 1
        assert 'PRIVATE-GUIDANCE' in text
        assert event.payload['load_id'] == str(context.instruction_context.load_id)
        key = cast(str, event.payload['ledger_key'])
        assert event.payload['ledger_request_sha256'] == ledger[key]['request_sha256']
        assert event.payload['ledger_request_sha256'] == runtime._model_request_sha256(request)
        assert event.payload['actor'] == ledger[key]['actor']
        assert event.payload['step_id'] == ledger[key]['step_id']
        assert event.actor is None and event.step_id is None
        assert event.payload['attempt'] == ledger[key]['attempt']
        assert event.payload['call_index'] == ledger[key]['call_index']
        assert event.payload['request_sha256'] != event.payload['ledger_request_sha256']
        assert event.payload['request_sha256'] == model_request_sha256(request)
        assert 'PRIVATE-GUIDANCE' not in json.dumps(event.to_payload())
        preceding = [item for item in events if item.sequence < event.sequence and item.checkpoint]
        assert any(cast(Mapping[str, Mapping[str, JsonValue]], item.checkpoint.state['models']).get(
            key, {}).get('status') == 'running' for item in preceding if item.checkpoint)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    review = next(request for request in gateway.requests if request.logical_model == 'review')
    assert review.response_schema is not None
    assert review.tools == ()


async def test_tool_continuation_has_guidance_once_without_changing_authority(tmp_path: Path) -> None:
    context = await task(tmp_path)
    gateway, capabilities = ToolGateway(), FakeCapabilities()
    plan = one_step_plan(tools=('web.search',))
    before = plan.model_dump_json()
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory(), capability_gateway=capabilities)
    events = [event async for event in runtime.run(context)]
    assert len(injections(events)) == len(gateway.requests) == 2
    for request in gateway.requests:
        assert sum(str(message.content).count('<PROJECT_GUIDANCE_JSON>')
                   for message in request.messages) == 1
    assert capabilities.calls == [('writer', 'web.search')]
    assert [event.payload['call_index'] for event in injections(events)] == [0, 1]
    assert plan.model_dump_json() == before


@pytest.mark.parametrize('changed', [False, True])
async def test_partial_replay_uses_content_digest_not_random_load_id(tmp_path: Path, changed: bool) -> None:
    context = await task(tmp_path)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(context)]
    checkpoint = await runtime.save_checkpoint()
    payload = checkpoint.to_payload()
    state = cast(dict[str, object], payload['state'])
    state.update(completed=[], artifact_refs={}, frontier=['final'], phase='running', terminal=False)
    payload['state_sha256'] = ''
    resumable = RuntimeCheckpoint.from_payload(payload)
    fresh = await task(tmp_path, tenant=context.tenant_id, run=context.run_id,
                       marker='CHANGED-GUIDANCE' if changed else 'PRIVATE-GUIDANCE')
    assert fresh.instruction_context is not None and context.instruction_context is not None
    assert fresh.instruction_context.load_id != context.instruction_context.load_id
    fresh = fresh.model_copy(update={
        'checkpoint': resumable,
        'artifacts': tuple(event.artifact for event in events if event.artifact is not None),
    })
    second_gateway = FakeGateway()
    replay = CrewDispatchRuntime(second_gateway, one_step_plan(), crew_factory=FastFactory())
    await replay.restore_checkpoint(resumable)
    replay_events: list[RunEvent] = []

    async def consume() -> None:
        async for event in replay.run(fresh):
            replay_events.append(event)

    if changed:
        with pytest.raises(RuntimeExecutionError, match='model request changed'):
            await consume()
    else:
        await consume()
    assert second_gateway.requests == []
    assert injections(replay_events) == []


async def test_concurrent_runs_share_no_context_or_environment(tmp_path: Path) -> None:
    first = await task(tmp_path, marker='FIRST-PRIVATE')
    second = await task(tmp_path, marker='SECOND-PRIVATE')
    gateway = FakeGateway(barrier=2)
    plan = one_step_plan()
    before = plan.model_dump_json(), dict(os.environ)

    async def execute(context: TaskContext) -> list[RunEvent]:
        runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
        return [event async for event in runtime.run(context)]

    results = await asyncio.wait_for(asyncio.gather(execute(first), execute(second)), timeout=5)
    assert gateway.maximum_observed_concurrency == 2
    for context, events in zip((first, second), results, strict=True):
        assert context.instruction_context is not None
        assert len(injections(events)) == 1
        assert injections(events)[0].payload['load_id'] == str(context.instruction_context.load_id)
    for request in gateway.requests:
        text = '\n'.join(str(message.content) for message in request.messages)
        assert ('FIRST-PRIVATE' in text) != ('SECOND-PRIVATE' in text)
    assert (plan.model_dump_json(), dict(os.environ)) == before


async def test_guidance_contributes_to_final_request_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.runtime.crew import adapter

    context = await task(tmp_path, marker='x' * 8192)
    monkeypatch.setattr(adapter, '_MAX_PROMPT_BYTES', 4096)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match='limit'):
        async for event in runtime.run(context):
            events.append(event)
    assert gateway.requests == []
    assert injections(events) == []


async def test_cancel_before_gateway_submission_never_emits_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create_task = asyncio.create_task
    submissions: list[asyncio.Task[Any]] = []

    def create_task(coro: Coroutine[Any, Any, Any], **kwargs: Any) -> asyncio.Task[Any]:
        pending = original_create_task(coro, **kwargs)
        if getattr(coro, '__name__', '') == 'submit_guided_request':
            submissions.append(pending)
            pending.cancel()
        return pending

    context = await task(tmp_path)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    monkeypatch.setattr(asyncio, 'create_task', create_task)
    async with asyncio.timeout(3):
        with pytest.raises(asyncio.CancelledError):
            async for event in runtime.run(context):
                events.append(event)
    assert submissions and all(pending.done() for pending in submissions)
    assert gateway.requests == []
    assert injections(events) == []


async def test_real_runtime_cancel_wins_before_submission_task_enters_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await task(tmp_path)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    original_create_task = asyncio.create_task
    owned: list[asyncio.Task[Any]] = []

    def create_task(coro: Coroutine[Any, Any, Any], **kwargs: Any) -> asyncio.Task[Any]:
        if getattr(coro, '__name__', '') == 'submit_guided_request':
            # Schedule the real cancellation API first, without directly cancelling submission.
            owned.append(original_create_task(runtime.cancel()))
            pending = original_create_task(coro, **kwargs)
            owned.append(pending)
            return pending
        return original_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, 'create_task', create_task)
    events: list[RunEvent] = []
    async with asyncio.timeout(3):
        with pytest.raises(asyncio.CancelledError):
            async for event in runtime.run(context):
                events.append(event)
        await asyncio.gather(*owned, return_exceptions=True)
    assert len(owned) == 2 and all(pending.done() for pending in owned)
    assert gateway.requests == []
    assert injections(events) == []
    assert gateway.active == 0


@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
async def test_submitted_calls_are_collected_on_cancel_and_timeout(tmp_path: Path, stop: str) -> None:
    context = await task(tmp_path)
    if stop == 'timeout':
        context = context.model_copy(update={'timeout_seconds': 0.2})
    gateway = FakeGateway(barrier=99)
    runtime = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    injected = asyncio.Event()

    async def consume() -> None:
        async for event in runtime.run(context):
            events.append(event)
            if event.kind == 'context.injected':
                injected.set()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(injected.wait(), timeout=3)
        if stop == 'cancel':
            await asyncio.wait_for(runtime.cancel(), timeout=3)
            with pytest.raises(asyncio.CancelledError):
                await consumer
        else:
            with pytest.raises(RuntimeExecutionError):
                await asyncio.wait_for(consumer, timeout=3)
        assert len(injections(events)) == len(gateway.requests) == 1
        assert gateway.active == 0
    finally:
        gateway.release.set()
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def test_budget_refusal_and_fixture_shortcut_do_not_claim_injection(tmp_path: Path) -> None:
    context = await task(tmp_path)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match='budget'):
        async for event in runtime.run(context.model_copy(update={'token_budget': 1})):
            events.append(event)
    assert gateway.requests == []
    assert injections(events) == []
    fixture = ProjectScaleArtifactPreseedRuntime(
        UnusedRuntime(TaskMode.DISPATCH, 'fixture must short-circuit'),
        harness_tool_gateway=RecordingHarnessToolGateway(),
    )
    events = [event async for event in fixture.run(context.model_copy(update={
        'request': 'Project-scale acceptance fixture: build a small project for scale=small and flow=dispatch.',
        'routing_decision': {'project_id': 'project-a', 'workspace_session_id': 'session-a',
                             'sandbox_profile': 'workspace_write'},
    }))]
    assert injections(events) == []
    assert events[-1].kind == 'runtime.completed'


async def test_running_worker_ledger_refuses_resubmission_and_injection(tmp_path: Path) -> None:
    context = await task(tmp_path)
    runtime = CrewDispatchRuntime(FakeGateway(), one_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(context)]
    checkpoint = next(event.checkpoint for event in events if event.checkpoint is not None and any(
        entry.get('status') == 'running'
        for entry in cast(Mapping[str, Mapping[str, JsonValue]], event.checkpoint.state['models']).values()
    ))
    assert checkpoint is not None
    gateway = FakeGateway()
    resumed = CrewDispatchRuntime(gateway, one_step_plan(), crew_factory=FastFactory())
    await resumed.restore_checkpoint(checkpoint)
    replay_events: list[RunEvent] = []
    with pytest.raises(ModelOutcomeUncertain, match='model outcome requires confirmation'):
        async for event in resumed.run(context.model_copy(update={'checkpoint': checkpoint})):
            replay_events.append(event)
    assert gateway.requests == []
    assert injections(replay_events) == []


@pytest.mark.parametrize('status,changed', [('succeeded', False), ('succeeded', True), ('running', False)])
async def test_review_bridge_replay_uses_persisted_candidate_and_guidance_digest(
    tmp_path: Path, status: str, changed: bool,
) -> None:
    context = await task(tmp_path)
    plan = _reviewed_step_plan()
    runtime = CrewDispatchRuntime(FakeGateway(), plan, crew_factory=FastFactory())
    events = [event async for event in runtime.run(context)]
    checkpoint = await runtime.save_checkpoint()
    key, state = next((key, state) for key, state in cast(
        Mapping[str, Mapping[str, JsonValue]], checkpoint.state['models'],
    ).items() if state['purpose'] == 'review')
    candidate = next(event.artifact for event in events if event.artifact is not None
                     and event.artifact.type == 'text' and event.artifact.producer == 'writer')
    response = next(event.artifact for event in events if event.artifact is not None
                    and str(event.artifact.id) == state['artifact_id'])
    assert candidate is not None and response is not None
    ledger = _ModelLedger(states={key: {**state, 'status': status}}, artifacts={key: response})
    fresh = await task(tmp_path, tenant=context.tenant_id, run=context.run_id,
                       marker='CHANGED-GUIDANCE' if changed else 'PRIVATE-GUIDANCE')
    gateway = FakeGateway()
    replay = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    deadline = asyncio.get_running_loop().time() + 10
    run_state = _RunState(_RunToken(1), deadline=deadline, crew_generation=FastGeneration())
    observed: list[dict[str, object]] = []

    async def emit(**values: object) -> None:
        observed.append(values)

    async def no_checkpoint(step_id: str, retries: int, review_artifact: Artifact | None = None) -> None:
        raise AssertionError('replay must not publish a new checkpoint')

    async def no_state(
        key: str, model_state: Mapping[str, JsonValue], *,
        repair: Mapping[str, JsonValue] | None = None,
    ) -> None:
        raise AssertionError('replay must not change model state')

    async def no_usage(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('replay must not charge another model call')

    async def review() -> tuple[str, str | None, tuple[Artifact, ...]]:
        return await replay._review(
            fresh, plan.steps[0], plan.agents[1], candidate, emit, no_checkpoint, no_state,
            no_usage, ledger, 0, 0, run_state, deadline,
        )

    if changed:
        with pytest.raises(RuntimeExecutionError, match='model request changed'):
            await review()
    elif status == 'running':
        with pytest.raises(ModelOutcomeUncertain, match='model outcome requires confirmation'):
            await review()
    else:
        assert (await review())[0] == 'approve'
    assert gateway.requests == []
    assert not any(event['kind'] == 'context.injected' for event in observed)
