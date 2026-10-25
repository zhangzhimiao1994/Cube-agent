"""Fault injection at a stopped real AutoGen boundary, not a fake repair receipt."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.db.models import RunOutboxRow, RunRow, RuntimeArtifactRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, FakeTransaction
from tests.unit.runtime.autogen.test_soft_model_admission import Journey
from tests.unit.runtime.test_generated_draft_autogen import EMPTY_HASH, rejection


class ProjectJourney(Journey):
    def __init__(self) -> None:
        super().__init__()
        self.context = self.context.model_copy(update={
            'timeout_seconds': 10.0,
            'routing_decision': {
                **self.context.routing_decision,
                **workspace_selection(project_id='draft-project', session_id='draft-session',
                                      sandbox_profile='workspace_write').routing_payload(),
                'runtime_timeout_soft_seconds': 10.0, 'runtime_timeout_seconds': 10.0,
                'runtime_timeout_absolute_seconds': 20.0, 'runtime_plan_timeout_seconds': 20.0,
                'runtime_plan_token_budget': 500, 'runtime_token_absolute_tokens': 500,
            },
        })
        fresh_route = dict(self.context.routing_decision)
        fresh_route.pop('engineering_action_version', None)
        self.context = self.context.model_copy(update={'routing_decision': fresh_route})
        self.files: dict[str, str] = {}
        self.write_count = 0
        self.verify_count = 0
        self.stage = 0
        self.artifacts = InMemoryArtifactRepository()
        self.plans: list[str] = []
        self.bundle_bytes = b''
        self.filename = 'project-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '.zip'
        self.verified = None
        self.repair_content = 'repaired draft'
        self.strict_soft = False
        self.report = 'Delivered the repaired fixture archive. The harness recovery path was checked; native business acceptance was not performed.'

    def runtime(self) -> AutoGenDiscussionRuntime:
        plan = DiscussionPlan(participants=(
            DiscussionParticipant(id='implementer', role='Engineer', goal='Build', logical_model='writer',
                                  allowed_tools=('project.inspect', 'workspace.list', 'workspace.write_text',
                                                 'project.verify', 'workspace.bundle')),
            DiscussionParticipant(id='reviewer', role='Reviewer', goal='Review', logical_model='reviewer'),
        ), selector_model='selector', max_turns=8, token_budget=500, wall_time_seconds=20.0)
        self.plans.append(plan.digest)
        self.runner = AutoGenDiscussionRuntime(self, plan, capability_gateway=self,
                                       harness_tool_gateway=self, artifact_repository=self.artifacts)
        return self.runner

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if any(m.role == 'system' and 'Main Agent final delivery' in str(m.content) for m in request.messages):
            response = ModelResponse(text=self.report, usage=TokenUsage(1, 1, 2))
        elif request.logical_model == 'selector':
            response = ModelResponse(text='implementer', usage=TokenUsage(1, 1, 2))
        else:
            stage, self.stage = self.stage, self.stage + 1
            if stage == 4:
                messages = '\n'.join(str(m.content) for m in request.messages)
                assert 'UNTRUSTED_PROJECT_REPAIR_FEEDBACK_JSON=' in messages
                assert 'generated_acceptance_manifest_invalid' in messages
                assert 'protected_editable_paths' in messages
                assert 'a' * 64 in messages
            if stage == 4 and getattr(self, 'model_freeze', None) is not None:
                self.model_freeze.set()
                await asyncio.Event().wait()
            actions = ['project_inspect', 'workspace_list', 'workspace_write_text',
                       'project_verify', 'workspace_write_text', 'project_verify', 'workspace_bundle']
            assert stage < len(actions), 'bounded proof must not start an unplanned call'
            action = actions[stage]
            assert request.required_tool in {None, action}
            args = {'path': 'ACCEPTANCE.json', 'content': 'bad draft' if stage == 2 else self.repair_content} \
                if action == 'workspace_write_text' else {}
            response = ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
                                     tool_calls=(ToolCall(id=f'project-{stage}', name=action, arguments=args),))
        return GatewayCompletion(response=response, deployment_id='fixture', logical_model=request.logical_model,
                                 provider_id='fixture', provider_model='fixture/model', cost_usd=Decimal('0.001'))

    async def invoke(self, tenant_id, request, *, user_id=None, role=None):
        if request.tool_name not in {'workspace.write_text', 'project.verify', 'workspace.bundle'}:
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        self.calls.append(request)
        if request.tool_name == 'workspace.write_text':
            self.write_count += 1
            path, content = request.arguments['path'], request.arguments['content']
            self.files[path] = content
            if self.write_count == 2 and getattr(self, 'tool_freeze', None) is not None:
                self.tool_freeze.set()
                await asyncio.Event().wait()
            result = {'workspace_files': ({'path': path, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                                            'size_bytes': len(content.encode())},)}
        elif request.tool_name == 'project.verify':
            self.verify_count += 1
            if self.verify_count == 1:
                return HarnessToolCallResult(
                    call_id=request.call_id, tool_name=request.tool_name, status='failed',
                    failure_reason='generated project draft requires correction', payload={
                        'generated_draft_rejection': rejection(self.context, request.actor),
                        'call_id': request.call_id, 'arguments_sha256': EMPTY_HASH,
                    },
                )
            assert self.files['ACCEPTANCE.json'] == 'repaired draft'
            result = self.passed_fixture_receipt()
            self.verified = result
        else:
            assert self.verified is not None and self.files['ACCEPTANCE.json'] == 'repaired draft'
            content = io.BytesIO()
            with zipfile.ZipFile(content, 'w') as archive:
                for path, text in sorted(self.files.items()):
                    archive.writestr(path, text)
            self.bundle_bytes = content.getvalue()
            receipt = self.verified['verification']
            result = {
                'artifact_id': str(uuid4()), 'presentation': 'final_attachment',
                'file': {'filename': self.filename, 'sha256': hashlib.sha256(self.bundle_bytes).hexdigest(),
                         'size_bytes': len(self.bundle_bytes)},
                'workspace_files': self.manifest(),
                'verification_source_sha256': receipt['source_sha256'],
                'verification_receipt_sha256': self.digest(receipt),
                'generated_project_acceptance': self.verified['generated_project_acceptance'],
            }
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status='succeeded', payload=result)

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=True).encode()).hexdigest()

    def manifest(self, tenant_id=None):
        if tenant_id is not None:
            return super().manifest(tenant_id)
        return [{'path': path, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                 'size_bytes': len(text.encode())} for path, text in sorted(self.files.items())]

    def passed_fixture_receipt(self):
        # Protocol fixtures, not native systemd execution or business acceptance evidence.
        source = self.digest(self.manifest())
        acceptance = hashlib.sha256(self.files['ACCEPTANCE.json'].encode()).hexdigest()
        empty = {'sanitized_bytes': 0, 'returned_bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest(),
                 'truncated': False, 'redacted': False, 'redacted_lines': 0}
        diagnostic = {'schema_version': 1, 'backend': 'systemd', 'exit_code': 0, 'timed_out': False,
                      'stdout': '', 'stderr': '', 'stdout_metadata': empty, 'stderr_metadata': empty}
        names = ['fixture_repaired_draft']
        binding = {
            'schema_version': 1, 'kind': 'generated_project_acceptance', 'capture_phase': 'post_model',
            'tenant_id': str(self.context.tenant_id), 'user_id': str(self.context.actor_id),
            'run_id': str(self.context.run_id), 'project_id': 'draft-project', 'session_id': 'draft-session',
            'request_sha256': hashlib.sha256(self.context.request.encode()).hexdigest(),
            'acceptance_sha256': acceptance, 'manifest_sha256': source, 'manifest_raw_sha256': source,
            'source_sha256': source, 'stack': 'node', 'test_execution': {
                'schema_version': 1, 'manifest_sha256': source, 'count': len(names), 'names': names,
                'test_evidence_sha256': self.digest({'schema_version': 1, 'passed_tests': names}),
            },
        }
        return {'verification': {
            'schema_version': 1, 'status': 'passed', 'source_sha256': source,
            'generated_acceptance_sha256': acceptance, 'runtime': {'node': 'fixture', 'npm': 'fixture'},
            'checks': [{'phase': phase, 'status': 'passed', 'execution_diagnostic': diagnostic}
                       for phase in ('install', 'build', 'test')],
        }, 'generated_project_acceptance': binding}


class ContinuationRepository(ExecutableFakeRepository):
    def __init__(self, journey: ProjectJourney) -> None:
        super().__init__(routing_decision=dict(journey.context.routing_decision))
        self.row = RunRow(**asdict(self.row), blocked_by_run_id=None)
        self.row.mode = TaskMode.DISCUSS.value
        self.row.actor_role = Role.OPERATOR.value
        self.row.request = journey.context.request
        journey.context = journey.context.model_copy(update={'run_id': self.row.id,
            'tenant_id': self.row.tenant_id, 'actor_id': self.row.actor_id, 'actor_role': Role.OPERATOR})
        self.checkpoint = None
        self.accept_count = 0
        self.outbox: list[Any] = []
        self.journey = journey
        self.continuation_errors = []

    async def persist_event(self, *args, **kwargs):
        await super().persist_event(*args, **kwargs)
        event = kwargs['event']
        if event.checkpoint is not None:
            self.checkpoint = event.checkpoint

    async def claim_for_execution(self, *args, **kwargs):
        result = await super().claim_for_execution(*args, **kwargs)
        return result if isinstance(result, RunRecord) else (result[0], self.checkpoint)

    async def latest_checkpoint(self, *args, **kwargs):
        return self.checkpoint

    _run_select = staticmethod(RunRepository._run_select)
    has_worker_execution = staticmethod(RunRepository.has_worker_execution)

    def _record(self, row=None):
        return RunRepository._record(self.row if row is None else row)

    async def _recovery_blocked_after_checkpoint(self, *args, **kwargs):
        # Real service persists the full ledger-bearing CHECKPOINT_SAVED event.
        last = max(e.sequence for e in self.event_log if e.checkpoint is not None)
        return any(e.sequence > last and e.kind not in {
            EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED, 'repair.classified', 'observer.notice',
        } for e in self.event_log)

    async def accept_self_repair_and_enqueue(self, **kwargs):
        repository = self

        class Transaction(FakeTransaction):
            async def scalar(self, statement):
                return repository.row

            async def flush(self):
                return None

            def add(self, value):
                repository.outbox.append(value)

        self._session_factory = Transaction
        result = await RunRepository.accept_self_repair_and_enqueue(self, **kwargs)
        self.accept_count += 1
        return result

    async def continue_project_repair_and_enqueue(self, **kwargs):
        from agent_hub.runs.artifacts import PostgresArtifactRepository
        repository = self

        class Transaction(FakeTransaction):
            async def scalar(self, statement):
                return repository.row

            async def scalars(self, statement):
                refs = tuple(ArtifactReference(id=UUID(k), sha256=v)
                    for k, v in repository.checkpoint.state['artifact_registry'].items())
                artifacts = await repository.journey.artifacts.get_many(
                    repository.row.tenant_id, repository.run_id, refs)
                rows = []
                for a in artifacts:
                    if a.type != 'tool_result':
                        continue
                    payload = PostgresArtifactRepository._encode_payload(a.to_payload())
                    rows.append(RuntimeArtifactRow(id=a.id, run_id=repository.run_id,
                        tenant_id=repository.row.tenant_id, content_sha256=a.content_sha256,
                        payload=payload, byte_size=len(payload.encode()), permanent=True))
                return SimpleNamespace(all=lambda: rows)

            async def flush(self):
                pass

            def add(self, value):
                repository.outbox.append(value)

        self._session_factory = Transaction
        try:
            return await RunRepository.continue_project_repair_and_enqueue(self, **kwargs)
        except Exception as error:
            self.continuation_errors.append(repr(error))
            raise


class BoundaryFailure:
    mode = TaskMode.DISCUSS

    def __init__(self, journey: ProjectJourney) -> None:
        self.journey = journey
        self.runner = journey.runtime()
        self.initial = True

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.runner = self.journey.runtime()
        self.initial = False
        await self.runner.restore_checkpoint(checkpoint)

    async def save_checkpoint(self):
        return await self.runner.save_checkpoint()

    async def cancel(self):
        await self.runner.cancel()

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        self.journey.context = context
        original_fail, original_success = _DiscussionDurability.fail_tool, _DiscussionDurability.succeed_tool
        boundary = asyncio.Event()
        if not self.initial and getattr(self, 'second_fault', None) == 'unknown_model':
            self.journey.model_freeze = boundary
        if not self.initial and getattr(self, 'second_fault', None) == 'unknown_write':
            self.journey.tool_freeze = boundary

        async def freeze():
            boundary.set()
            await asyncio.Event().wait()

        async def fail(durable, artifact):
            await original_fail(durable, artifact)
            if self.initial:
                if self.journey.strict_soft:
                    # Simulate elapsed soft runway, retaining the real absolute clock and ACK publication.
                    self.runner._wall_budget.deadline = asyncio.get_running_loop().time() + 0.25
                    await durable._checkpoint()
                # The driver ACK and artifact finalization must finish before fault injection.
                await freeze()

        async def succeed(durable, artifact):
            await original_success(durable, artifact)
            if (not self.initial and getattr(self, 'second_fault', None) == 'after_write'
                    and artifact.content.get('tool_name') == 'workspace.write_text'):
                await freeze()

        with patch.object(_DiscussionDurability, 'fail_tool', fail), \
                patch.object(_DiscussionDurability, 'succeed_tool', succeed):
            async with aclosing(self.runner.run(context)) as stream:
                stop = asyncio.create_task(boundary.wait())
                item = None
                try:
                    while True:
                        item = asyncio.create_task(anext(stream))
                        await asyncio.wait((item, stop), return_when=asyncio.FIRST_COMPLETED)
                        if stop.done():
                            await self.runner.cancel()
                            item.cancel()
                            await asyncio.gather(item, return_exceptions=True)
                            break
                        try:
                            event = item.result()
                        except StopAsyncIteration:
                            return
                        yield event
                        late_tool = {'after_verify': 'project.verify', 'after_bundle': 'workspace.bundle',
                                     'after_bundle_list': 'workspace.list'}.get(
                            getattr(self, 'second_fault', None))
                        if not self.initial and late_tool is not None and event.checkpoint is not None:
                            state = event.checkpoint.state
                            tools, models = state['tool_ledger'], state['model_ledger']
                            phase = state.get('project_repair_phase', {})
                            if (tools and tools[-1]['name'] == late_tool and tools[-1]['status'] == 'succeeded'
                                    and state['engineering_tool_prefix'] == len(tools)
                                    and phase.get('model_floor') == len(models)):
                                # The service has persisted and ACKed a complete round, before the report starts.
                                await self.runner.cancel()
                                break
                finally:
                    stop.cancel()
                    if item is not None and not item.done():
                        item.cancel()
                        await asyncio.gather(item, return_exceptions=True)
                    await asyncio.gather(stop, return_exceptions=True)
        yield RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=context.run_id,
                       reason='project repair boundary failure')


@pytest.mark.asyncio
async def test_real_runtime_service_repository_continue_without_replaying_old_effects() -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    first = await service.execute(repo.run_id)
    assert repo.row.routing_decision.get('engineering_auto_repair_version') == 1
    assert first.status is RunStatus.QUEUED, (first.status, first.repair_proposal,
        [(e.kind, e.reason) for e in repo.event_log[-5:]],
        [c.tool_name for c in journey.calls],
        [(e.kind, dict(e.payload)) for e in repo.event_log if e.kind in {EventKind.TOOL_FAILED, 'repair.skipped'}])
    saved = repo.checkpoint
    assert saved is not None and repo.accept_count == 1
    before = saved.to_payload()
    calls_before = list(journey.calls)
    requests_before = list(journey.requests)
    assert journey.write_count == 1 and journey.verify_count == 1
    assert first.decision_token is None

    second = await service.execute(repo.run_id)
    assert second.status is RunStatus.COMPLETED, [(e.kind, e.reason) for e in repo.event_log[-5:]]
    assert journey.write_count == 2 and journey.verify_count == 2, (
        second.status, [(e.kind, e.reason) for e in repo.event_log[-5:]],
        [c.tool_name for c in journey.calls],
    )
    assert journey.calls[:len(calls_before)] == calls_before
    assert [c.tool_name for c in journey.calls[len(calls_before):]] == ['workspace.write_text', 'project.verify', 'workspace.bundle']
    assert all('SELF_REPAIR_CONTEXT' in str(m.content)
        for request in journey.requests[len(requests_before):] if request.logical_model == 'writer'
        for m in request.messages if m.role == 'user' and str(m.content).startswith(journey.context.request))
    assert repo.accept_count == 1 and len(repo.outbox) == 1
    assert len(set(journey.plans)) == 1
    assert saved.to_payload() == before
    assert repo.checkpoint.state['usage']['tokens'] >= saved.state['usage']['tokens']
    assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(saved.state['usage']['cost_usd']) > 0
    assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= saved.state['remaining_absolute_timeout_seconds']
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    assert repo.checkpoint.state['project_repair_phase']['source_checkpoint_sha256'] == saved.state_sha256
    assert repo.checkpoint.state['tool_ledger'][:len(saved.state['tool_ledger'])] == saved.state['tool_ledger']
    assert repo.checkpoint.state['model_ledger'][:len(saved.state['model_ledger'])] == saved.state['model_ledger']
    final = [e.artifact for e in repo.event_log if e.artifact is not None
             and e.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    assert final[0].producer == 'delivery_main_agent'
    with zipfile.ZipFile(io.BytesIO(journey.bundle_bytes)) as archive:
        assert archive.read('ACCEPTANCE.json') == b'repaired draft'
    before_requests, before_calls = len(journey.requests), len(journey.calls)
    cold = journey.runtime()
    await cold.restore_checkpoint(repo.checkpoint)
    events = [e async for e in cold.run(journey.context.model_copy(update={'checkpoint': repo.checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(journey.requests) == before_requests and len(journey.calls) == before_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['after_write', 'unknown_model', 'unknown_write'])
async def test_second_fault_preserves_phase_and_cold_restore_never_replays_unknown_or_write(fault: str) -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    first = await service.execute(repo.run_id)
    assert first.status is RunStatus.QUEUED
    source = repo.checkpoint
    runtime.second_fault = fault
    second = await service.execute(repo.run_id)
    assert second.status is (RunStatus.QUEUED if fault == 'after_write' else RunStatus.FAILED)
    failed = repo.checkpoint
    phase = failed.state['project_repair_phase']
    assert phase['source_checkpoint_sha256'] == source.state_sha256
    assert failed.state['model_ledger'][:len(source.state['model_ledger'])] == source.state['model_ledger']
    assert failed.state['tool_ledger'][:len(source.state['tool_ledger'])] == source.state['tool_ledger']
    assert failed.state['usage']['tokens'] >= source.state['usage']['tokens'] > 0
    assert Decimal(failed.state['usage']['cost_usd']) >= Decimal(source.state['usage']['cost_usd']) > 0
    before = failed.to_payload()
    old_count = len(journey.calls)
    old_model_count = len(journey.requests)
    if fault == 'after_write':
        runtime.second_fault = None
        third = await service.execute(repo.run_id)
        assert third.status is RunStatus.COMPLETED
        assert repo.accept_count == 1 and len(repo.outbox) == 2
        assert repo.row.routing_decision['self_repair_attempt'] == 1
        assert journey.write_count == 2
        assert [c.tool_name for c in journey.calls[old_count:]] == ['project.verify', 'workspace.bundle']
        assert journey.verify_count == 2 and journey.bundle_bytes
        final = [e.artifact for e in repo.event_log if e.artifact is not None
                 and e.artifact.content.get('presentation') == 'final_answer']
        assert len(final) == 1 and final[0].content['text'] == journey.report
        assert len(set(journey.plans)) == 1
        assert failed.to_payload() == before
        assert repo.checkpoint.state['usage']['tokens'] >= failed.state['usage']['tokens']
        assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(failed.state['usage']['cost_usd'])
        assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= failed.state['remaining_absolute_timeout_seconds']
        with zipfile.ZipFile(io.BytesIO(journey.bundle_bytes)) as archive:
            assert archive.read('ACCEPTANCE.json') == b'repaired draft'
        return
    # A terminal fetch/recovery cannot silently start another repair episode.
    assert (await service.execute(repo.run_id)).status is RunStatus.FAILED
    assert repo.accept_count == 1 and len(repo.outbox) == 1
    cold = journey.runtime()
    await cold.restore_checkpoint(failed)
    events = [e async for e in cold.run(journey.context.model_copy(update={'checkpoint': failed}))]
    assert failed.to_payload() == before
    if fault in {'unknown_model', 'unknown_write'}:
        key = 'model_ledger' if fault == 'unknown_model' else 'tool_ledger'
        assert failed.state[key][-1]['status'] == 'running'
        assert events[-1].kind is EventKind.RUNTIME_FAILED
        assert events[-1].reason == ('model_outcome_uncertain' if fault == 'unknown_model' else 'tool_outcome_uncertain')
        assert len(journey.requests) == old_model_count
        assert len(journey.calls) == old_count
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0


@pytest.mark.asyncio
@pytest.mark.parametrize('faults,pending_tools,pending_models', [
    (['after_verify'], ['workspace.bundle'], ['selector', 'writer', 'main_report']),
    (['after_bundle'], [], ['main_report']),
    (['after_verify', 'after_bundle'], [], ['main_report']),
])
async def test_late_delivery_progress_continues_after_write_credit_is_consumed(
    faults: list[str], pending_tools: list[str], pending_models: list[str],
) -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    for index, fault in enumerate(faults):
        consumed = dict(repo.row.routing_decision['self_repair_episode_progress'])
        runtime.second_fault = fault
        continued = await service.execute(repo.run_id)
        stopped = repo.checkpoint
        before = stopped.to_payload()
        assert journey.write_count == 2 and journey.verify_count == 2
        assert all(row['status'] == 'succeeded' for row in stopped.state['model_ledger'])
        assert all(row['status'] in {'succeeded', 'failed'} for row in stopped.state['tool_ledger'])
        assert stopped.state['project_repair_phase']['model_floor'] == len(stopped.state['model_ledger'])
        assert stopped.state['engineering_tool_prefix'] == len(stopped.state['tool_ledger'])
        expected = ['project.verify'] if fault == 'after_verify' else (
            ['workspace.bundle'] if index else ['project.verify', 'workspace.bundle'])
        assert [row['name'] for row in stopped.state['tool_ledger'][consumed['tool_count']:]] == expected
        assert not any('report_binding' in row for row in stopped.state['model_ledger'])
        assert continued.status is RunStatus.QUEUED, repo.continuation_errors
    assert repo.accept_count == 1 and len(repo.outbox) == 2 + len(faults)
    calls, models = len(journey.calls), len(journey.requests)
    runtime.second_fault = None
    assert (await service.execute(repo.run_id)).status is RunStatus.COMPLETED
    assert [call.tool_name for call in journey.calls[calls:]] == pending_tools
    assert [('main_report' if any(m.role == 'system' and 'Main Agent final delivery' in str(m.content)
                                 for m in request.messages) else request.logical_model)
            for request in journey.requests[models:]] == pending_models
    assert journey.write_count == 2 and journey.verify_count == 2
    assert sum(call.tool_name == 'workspace.bundle' for call in journey.calls) == 1
    assert sum(any(m.role == 'system' and 'Main Agent final delivery' in str(m.content)
                   for m in request.messages) for request in journey.requests) == 1
    final = [event.artifact for event in repo.event_log if event.artifact is not None
             and event.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    assert stopped.to_payload() == before
    assert repo.checkpoint.state['model_ledger'][:len(stopped.state['model_ledger'])] == stopped.state['model_ledger']
    assert repo.checkpoint.state['tool_ledger'][:len(stopped.state['tool_ledger'])] == stopped.state['tool_ledger']
    assert repo.row.routing_decision['self_repair_attempt'] == 1
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    assert repo.checkpoint.state['usage']['tokens'] >= stopped.state['usage']['tokens']
    assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(stopped.state['usage']['cost_usd'])
    assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= stopped.state['remaining_absolute_timeout_seconds']
    with zipfile.ZipFile(io.BytesIO(journey.bundle_bytes)) as archive:
        assert archive.read('ACCEPTANCE.json') == b'repaired draft'


@pytest.mark.asyncio
async def test_repair_progress_outbox_key_fits_orm_and_binds_stable_full_scope() -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    stopped = repo.checkpoint
    original = repo.outbox[-1]
    assert isinstance(original, RunOutboxRow)
    assert len(original.idempotency_key.encode('ascii')) <= RunOutboxRow.__table__.c.idempotency_key.type.length
    route = {k: v for k, v in repo.row.routing_decision.items() if k != 'self_repair_episode_progress'}
    state = stopped.to_payload()['state']
    state['remaining_timeout_seconds'] -= 0.01
    later = RuntimeCheckpoint(id=uuid4(), runtime_type='autogen', runtime_version='2',
        run_id=stopped.run_id, tenant_id=stopped.tenant_id, mode=stopped.mode, state=state)
    assert later.state_sha256 != stopped.state_sha256
    keys = []
    # Reset only this synthetic in-memory fixture to inspect deterministic enqueue encoding.
    for checkpoint in (stopped, stopped, later):
        repo.row.status = RunStatus.FAILED.value
        repo.row.routing_decision = dict(route)
        repo.checkpoint = checkpoint
        await repo.continue_project_repair_and_enqueue(tenant_id=repo.row.tenant_id,
            run_id=repo.run_id, version=repo.row.version, checkpoint_sha256=checkpoint.state_sha256)
        entry = repo.outbox[-1]
        assert entry.tenant_id == checkpoint.tenant_id and entry.run_id == checkpoint.run_id
        expected = 'repair-progress:' + hashlib.sha256(
            f'{checkpoint.tenant_id}:{checkpoint.run_id}:{checkpoint.state_sha256}'.encode()).hexdigest()
        assert entry.idempotency_key == expected
        assert len(entry.idempotency_key) == len(entry.idempotency_key.encode('ascii')) == 80
        assert entry.payload == {'run_id': str(checkpoint.run_id)}
        keys.append(entry.idempotency_key)
    assert keys[0] == keys[1] == original.idempotency_key
    assert keys[2] != keys[0]


@pytest.mark.asyncio
async def test_bundle_readonly_tail_cold_restore_only_adds_report_and_keeps_full_ledger() -> None:
    class ReadTailJourney(ProjectJourney):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completed = await super().complete_with_context(request)
            if any(call.name == 'workspace_bundle' for call in completed.response.tool_calls):
                return replace(completed, response=replace(completed.response, tool_calls=(
                    *completed.response.tool_calls,
                    ToolCall(id='post-bundle-list', name='workspace_list', arguments={}),
                )))
            return completed

    journey = ReadTailJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    consumed = repo.row.routing_decision['self_repair_episode_progress']['tool_count']
    runtime.second_fault = 'after_bundle_list'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED, repo.continuation_errors
    stopped = repo.checkpoint
    before = stopped.to_payload()
    tools, models = stopped.state['tool_ledger'], stopped.state['model_ledger']
    assert [row['name'] for row in tools[consumed:]] == ['project.verify', 'workspace.bundle', 'workspace.list']
    assert stopped.state['engineering_tool_prefix'] == len(tools)
    assert stopped.state['project_repair_phase']['model_floor'] == len(models)
    assert all(row['status'] == 'succeeded' and 'report_binding' not in row for row in models)
    assert tools[-1]['status'] == 'succeeded'
    assert repo.accept_count == 1 and len(repo.outbox) == 3
    calls, requests = len(journey.calls), len(journey.requests)
    runtime.second_fault = None
    completed = await service.execute(repo.run_id)
    assert completed.status is RunStatus.COMPLETED, [(e.kind, e.reason) for e in repo.event_log[-6:]]
    assert len(journey.calls) == calls and len(journey.requests) == requests + 1
    request = journey.requests[-1]
    assert request.tools == () and request.required_tool is None
    assert any(m.role == 'system' and 'Main Agent final delivery' in str(m.content) for m in request.messages)
    assert repo.checkpoint.state['tool_ledger'] == tools
    assert repo.checkpoint.state['model_ledger'][:-1] == models
    assert stopped.to_payload() == before
    final = [event.artifact for event in repo.event_log if event.artifact is not None
             and event.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    metadata = final[0].content['engineering_report']
    assert metadata['bundle_position'] == len(tools) - 2
    assert metadata['bundle_artifact_id'] == tools[-2]['artifact_id']
    assert metadata['tool_prefix_length'] == len(tools)
    assert metadata['tool_prefix_sha256'] == journey.digest(before['state']['tool_ledger'])
    assert journey.write_count == 2 and journey.verify_count == 2
    assert sum(call.tool_name == 'workspace.bundle' for call in journey.calls) == 1
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    assert repo.checkpoint.state['usage']['tokens'] >= stopped.state['usage']['tokens']
    assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(stopped.state['usage']['cost_usd'])
    assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= stopped.state['remaining_absolute_timeout_seconds']
    terminal = repo.checkpoint
    cold = journey.runtime()
    await cold.restore_checkpoint(terminal)
    calls, requests = len(journey.calls), len(journey.requests)
    events = [event async for event in cold.run(journey.context.model_copy(update={'checkpoint': terminal}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert len(journey.calls) == calls and len(journey.requests) == requests
    assert (await cold.save_checkpoint()).state_sha256 == terminal.state_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['consumed', 'permission', 'hard_tokens', 'tokens_reset', 'time_reset',
                                   'lease', 'stale_version', 'stale_checkpoint', 'unconfirmed_effect'])
async def test_episode_transaction_refuses_replay_or_changed_authority(defect: str) -> None:
    from agent_hub.runtime.contracts import _mutable_json
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    repo.row.status = RunStatus.FAILED.value
    route = dict(repo.row.routing_decision)
    if defect != 'consumed':
        route.pop('self_repair_episode_progress')
    state = _mutable_json(repo.checkpoint.state)
    if defect == 'permission':
        route['requested_permissions'] = ['host_write']
    elif defect == 'hard_tokens':
        route['runtime_token_absolute_tokens'] += 1
    elif defect == 'tokens_reset':
        state['usage']['tokens'] = 0
    elif defect == 'time_reset':
        state['remaining_absolute_timeout_seconds'] += 1
    elif defect == 'lease':
        repo.row.worker_id = 'still-running'
    elif defect == 'unconfirmed_effect':
        repo.event_log.append(RunEvent(kind=EventKind.TOOL_STARTED, sequence=1000, run_id=repo.run_id,
            actor='implementer', tool_call_id='late', tool_name='workspace.write_text'))
    repo.row.routing_decision = route
    old = repo.checkpoint
    repo.checkpoint = RuntimeCheckpoint(id=old.id, runtime_type=old.runtime_type, runtime_version=old.runtime_version,
        run_id=old.run_id, tenant_id=old.tenant_id, mode=old.mode, state=state)
    version = repo.row.version - (defect == 'stale_version')
    digest = 'f' * 64 if defect == 'stale_checkpoint' else repo.checkpoint.state_sha256
    before, outbox = dict(route), len(repo.outbox)
    with pytest.raises(RunConflict):
        await repo.continue_project_repair_and_enqueue(tenant_id=repo.row.tenant_id, run_id=repo.run_id,
            version=version, checkpoint_sha256=digest)
    assert repo.row.status == RunStatus.FAILED.value
    assert repo.row.routing_decision == before and len(repo.outbox) == outbox


@pytest.mark.asyncio
async def test_actual_noop_repair_write_does_not_buy_another_episode_continuation() -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    journey.repair_content = 'bad draft'
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.FAILED
    assert journey.write_count == 2 and journey.files['ACCEPTANCE.json'] == 'bad draft'
    assert len(repo.outbox) == 1 and repo.accept_count == 1
    assert 'self_repair_episode_progress' not in repo.row.routing_decision
    before = (len(journey.calls), len(journey.requests))
    assert (await service.execute(repo.run_id)).status is RunStatus.FAILED
    assert (len(journey.calls), len(journey.requests)) == before


@pytest.mark.asyncio
async def test_real_strict_soft_progress_renewal_survives_service_cold_resume() -> None:
    journey = ProjectJourney()
    journey.strict_soft = True
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    source = repo.checkpoint
    assert 0 < source.state['remaining_timeout_seconds'] < 0.3
    assert repo.row.routing_decision['runtime_timeout_source'] == 'project_scale_soft_budget'
    runtime.second_fault = 'after_write'
    second = await service.execute(repo.run_id)
    repaired = repo.checkpoint
    assert repaired.state['remaining_timeout_seconds'] > source.state['remaining_timeout_seconds']
    assert repaired.state['remaining_absolute_timeout_seconds'] <= source.state['remaining_absolute_timeout_seconds']
    assert second.status is RunStatus.QUEUED, repo.continuation_errors
    runtime.second_fault = None
    assert (await service.execute(repo.run_id)).status is RunStatus.COMPLETED
    assert journey.write_count == 2 and journey.verify_count == 2 and journey.bundle_bytes
    assert repo.accept_count == 1 and len(repo.outbox) == 2
    assert repo.checkpoint.state['usage']['tokens'] >= repaired.state['usage']['tokens'] > source.state['usage']['tokens']
    assert len(set(journey.plans)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('late_bundle', [False, True])
@pytest.mark.parametrize('owner_status', ['written', 'reserved', 'aborted'])
async def test_sqlite_transaction_uses_real_private_write_ownership_and_sql_predicates(
    owner_status: str, late_bundle: bool,
) -> None:
    from sqlalchemy import JSON, Column, MetaData, String, Table, select
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.dialects.postgresql import UUID as PGUUID
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agent_hub.db.models import (
        RunCheckpointRow,
        RunEventRow,
        RunOutboxRow,
        RuntimeArtifactWriteRow,
    )
    from agent_hub.runs.artifacts import PostgresArtifactRepository

    journey = ProjectJourney()
    fake = ContinuationRepository(journey)
    journey.strict_soft = True
    runtime = BoundaryFailure(journey)
    service = RunService(fake, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(fake.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    await service.execute(fake.run_id)
    route = dict(fake.row.routing_decision)
    if late_bundle:
        runtime.second_fault = 'after_bundle'
        assert (await service.execute(fake.run_id)).status is RunStatus.QUEUED
    else:
        route.pop('self_repair_episode_progress')
    checkpoint = fake.checkpoint
    refs = tuple(ArtifactReference(id=UUID(k), sha256=v) for k, v in checkpoint.state['artifact_registry'].items())
    artifacts = await journey.artifacts.get_many(fake.row.tenant_id, fake.run_id, refs)
    writes = [a for a in artifacts if a.type == 'tool_result']
    assert sum(a.content.get('tool_name') == 'workspace.write_text' for a in writes) == 2
    target_id = UUID(checkpoint.state['tool_ledger'][-1]['artifact_id'])
    if late_bundle:
        assert next(a for a in writes if a.id == target_id).content['tool_name'] == 'workspace.bundle'
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Dialect-only schema adaptation; mapped columns, actual WHERE/EXISTS and transaction logic stay real.
    metadata = MetaData()
    tables = (RunRow, RunCheckpointRow, RunEventRow, RunOutboxRow, RuntimeArtifactRow, RuntimeArtifactWriteRow)
    for model in tables:
        Table(model.__tablename__, metadata, *(Column(c.name,
            JSON() if isinstance(c.type, JSONB) else String(32) if isinstance(c.type, PGUUID) else c.type,
            primary_key=c.primary_key, nullable=c.nullable, server_default=c.server_default)
            for c in model.__table__.columns))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        values = {c.name: getattr(fake.row, c.name) for c in RunRow.__table__.columns}
        values['status'] = RunStatus.FAILED.value
        values['routing_decision'] = route
        async with factory.begin() as session:
            session.add(RunRow(**values))
            session.add(RunCheckpointRow(id=checkpoint.id, tenant_id=fake.row.tenant_id, run_id=fake.run_id,
                sequence=100, runtime_type='autogen', runtime_version='2', mode=TaskMode.DISCUSS.value,
                state=checkpoint.to_payload()['state'], state_sha256=checkpoint.state_sha256,
                payload=checkpoint.to_payload()))
        store = PostgresArtifactRepository(factory)
        write_ids = {}
        for artifact in writes:
            write_ids[artifact.id] = uuid4()
            await store.put(fake.row.tenant_id, fake.run_id, artifact, write_id=write_ids[artifact.id])
        async with factory.begin() as session:
            persisted = (await session.scalars(select(RuntimeArtifactRow))).all()
            assert len(persisted) == len(writes) and all(a.permanent is False for a in persisted)
            owner = await session.get(RuntimeArtifactWriteRow, (fake.row.tenant_id, fake.run_id, write_ids[target_id]))
            assert owner.status == 'written'
            owner.status = owner_status
        real = RunRepository(factory)
        kwargs = {'tenant_id': fake.row.tenant_id, 'run_id': fake.run_id,
                  'version': fake.row.version, 'checkpoint_sha256': checkpoint.state_sha256}
        if owner_status == 'written':
            continued = await real.continue_project_repair_and_enqueue(**kwargs)
            assert continued.status is RunStatus.QUEUED
            assert continued.routing_decision['runtime_token_absolute_tokens'] == 500
            assert continued.routing_decision['self_repair_attempt'] == 1
            assert continued.routing_decision['self_repair_episode_progress']['usage'] == dict(checkpoint.state['usage'])
            # Exercise the SQL-produced durable route through the next real service/runtime execution.
            fake.row.routing_decision = dict(continued.routing_decision)
            fake.row.status, fake.row.version = continued.status.value, continued.version
            runtime.second_fault = None
            calls, models = len(journey.calls), len(journey.requests)
            assert (await service.execute(fake.run_id)).status is RunStatus.COMPLETED
            if late_bundle:
                assert len(journey.calls) == calls and len(journey.requests) == models + 1
                assert any(m.role == 'system' and 'Main Agent final delivery' in str(m.content)
                           for m in journey.requests[-1].messages)
            assert journey.write_count == 2 and journey.verify_count == 2 and journey.bundle_bytes
            assert len(set(journey.plans)) == 1
            assert fake.checkpoint.state['usage']['tokens'] >= checkpoint.state['usage']['tokens']
        else:
            with pytest.raises(RunConflict):
                await real.continue_project_repair_and_enqueue(**kwargs)
        async with factory() as session:
            outbox = (await session.scalars(select(RunOutboxRow))).all()
            assert len(outbox) == (1 if owner_status == 'written' else 0)
            saved = await real.latest_checkpoint(session, tenant_id=fake.row.tenant_id, run_id=fake.run_id)
            assert saved.to_payload() == checkpoint.to_payload()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_forged_soft_renewal_after_actual_noop_is_rejected_before_any_model() -> None:
    from agent_hub.runtime.contracts import _mutable_json
    journey = ProjectJourney()
    journey.strict_soft = True
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    journey.repair_content = 'bad draft'
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.FAILED
    saved = repo.checkpoint
    state = _mutable_json(saved.state)
    state['remaining_timeout_seconds'] = 1.0
    forged = RuntimeCheckpoint(id=saved.id, runtime_type='autogen', runtime_version='2',
        run_id=saved.run_id, tenant_id=saved.tenant_id, mode=saved.mode, state=state)
    cold = journey.runtime()
    await cold.restore_checkpoint(forged)
    before = (len(journey.calls), len(journey.requests))
    context = journey.context.model_copy(update={'checkpoint': forged, 'routing_decision': repo.row.routing_decision})
    events = [e async for e in cold.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert events[-1].reason == 'project repair progress evidence is invalid'
    assert (len(journey.calls), len(journey.requests)) == before
