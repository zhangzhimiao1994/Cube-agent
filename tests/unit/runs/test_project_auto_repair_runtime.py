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
from autogen_core import CancellationToken
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, BindParameter

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import _builtin_input_schema
from agent_hub.db.models import RunOutboxRow, RunRow, RuntimeArtifactRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
    _DiscussionDurability,
    _pending_approval_call,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_terminal_hooks import ExecutableFakeRepository, FakeTransaction
from tests.unit.runtime.autogen.test_capability_approval_resume import tool as approval_tool
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
                id_filters = [clause for clause in visitors.iterate(statement.whereclause)
                    if isinstance(clause, BinaryExpression) and clause.operator is operators.in_op
                    and clause.left.shares_lineage(RuntimeArtifactRow.__table__.c.id)
                    and isinstance(clause.right, BindParameter)]
                assert len(id_filters) == 1, 'private artifact query must select explicit artifact ids'
                requested_ids = set(id_filters[0].right.value)
                refs = tuple(ArtifactReference(id=UUID(k), sha256=v)
                    for k, v in repository.checkpoint.state['artifact_registry'].items()
                    if UUID(k) in requested_ids)
                artifacts = await repository.journey.artifacts.get_many(
                    repository.row.tenant_id, repository.run_id, refs)
                rows = []
                for a in artifacts:
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
            if artifact.content.get('tool_name') == 'workspace.read' and hasattr(self.journey, 'read_acks'):
                self.journey.read_acks.append((await self.runner.save_checkpoint(), asdict(self.runner._wall_budget)))
            ack_tool = {'ack_verify': 'project.verify', 'ack_bundle': 'workspace.bundle',
                        'ack_read': 'workspace.read', 'ack_repeat_read': 'workspace.read'}.get(
                getattr(self, 'second_fault', None))
            if (not self.initial and ack_tool == artifact.content.get('tool_name')
                    and (getattr(self, 'second_fault', None) != 'ack_repeat_read'
                         or self.journey.read_count == getattr(self, 'read_stop_count', 2))):
                self.ack_checkpoint = await self.runner.save_checkpoint()
                self.ack_artifact = artifact
                self.ack_counts = (len(self.journey.calls), len(self.journey.requests))
                await freeze()
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


class ReadBeforeRepairJourney(ProjectJourney):
    def __init__(self, *, cross_read_soft: bool = False, repeat_reads: int = 0,
                 repeat_summary: bool = False) -> None:
        super().__init__()
        self.context = self.context.model_copy(update={'routing_decision': {
            **self.context.routing_decision, 'engineering_diagnostic_allowance_version': 1}})
        self.cross_read_soft, self.repeat_reads, self.repeat_summary = cross_read_soft, repeat_reads, repeat_summary
        self.read_count = 0
        self.read_acks: list[Any] = []
        self.crossed_reads: list[Any] = []

    def runtime(self) -> AutoGenDiscussionRuntime:
        original = super().runtime()
        plan = replace(original._plan, participants=tuple(
            replace(participant, allowed_tools=(*participant.allowed_tools, 'workspace.read'))
            if participant.id == 'implementer' else participant for participant in original._plan.participants))
        self.plans[-1] = plan.digest
        self.runner = AutoGenDiscussionRuntime(self, plan, capability_gateway=self,
            harness_tool_gateway=self, artifact_repository=self.artifacts)
        return self.runner

    def is_available(self, tenant_id, name):
        return tenant_id == self.context.tenant_id and (
            name == 'workspace.read' or super().is_available(tenant_id, name))

    def is_replay_safe(self, name):
        return name == 'workspace.read' or super().is_replay_safe(name)

    def manifest(self, tenant_id=None):
        manifests = super().manifest(tenant_id)
        return (*manifests, {'name': 'workspace.read', 'input_schema': _builtin_input_schema('workspace.read')}) \
            if tenant_id is not None else manifests

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        is_report = any(message.role == 'system' and 'Main Agent final delivery' in str(message.content)
                        for message in request.messages)
        if request.logical_model != 'writer' or self.stage < 4 or is_report:
            return await super().complete_with_context(request)
        if self.stage == 4:
            completion = await super().complete_with_context(request)
            return replace(completion, response=replace(completion.response, tool_calls=(ToolCall(
                id='repair-diagnosis-read', name='workspace_read', arguments={'path': 'ACCEPTANCE.json'}),)))
        stage, self.stage = self.stage, self.stage + 1
        self.requests.append(request)
        if stage <= 4 + self.repeat_reads:
            action, arguments = 'workspace_read', {'path': 'ACCEPTANCE.json'}
        else:
            index = stage - 5 - self.repeat_reads
            assert 0 <= index <= 2, 'read recovery must not start unplanned model work'
            action = ('workspace_write_text', 'project_verify', 'workspace_bundle')[index]
            arguments = {'path': 'ACCEPTANCE.json', 'content': self.repair_content} if index == 0 else {}
        assert request.required_tool in {None, action}
        return GatewayCompletion(response=ModelResponse(text=None, usage=TokenUsage(1, 1, 2),
            tool_calls=(ToolCall(id=f'post-read-{stage}', name=action, arguments=arguments),)),
            deployment_id='fixture', logical_model=request.logical_model, provider_id='fixture',
            provider_model='fixture/model', cost_usd=Decimal('0.001'))

    async def invoke(self, tenant_id, request, *, user_id=None, role=None):
        if request.tool_name != 'workspace.read':
            return await super().invoke(tenant_id, request, user_id=user_id, role=role)
        assert tenant_id == self.context.tenant_id and request.run_id == self.context.run_id
        assert user_id == self.context.actor_id and role == self.context.actor_role
        assert request.actor == 'implementer' and request.arguments == {'path': 'ACCEPTANCE.json'}
        self.calls.append(request)
        self.read_count += 1
        if self.cross_read_soft and self.read_count == 1:
            budget = self.runner._wall_budget
            budget.deadline = asyncio.get_running_loop().time() + 0.01
            before = asdict(budget)
            await self.cross_soft()
            now = asyncio.get_running_loop().time()
            assert budget.remaining(now=now) == 0 and budget.absolute_remaining(now=now) > 0
            self.crossed_reads.append((before, asdict(budget)))
        text = self.files['ACCEPTANCE.json']
        payload = {'path': 'ACCEPTANCE.json', 'text': text, 'truncated': False}
        if self.repeat_summary and self.read_count > 1:
            # Adversarial display metadata is not a fresh file observation.
            payload['summary'] = 'another attempt, identical file bytes'
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='succeeded', payload=payload)


async def _read_repair_fixture(**options):
    journey = ReadBeforeRepairJourney(**options)
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    first = await service.execute(repo.run_id)
    assert first.status is RunStatus.QUEUED and journey.write_count == journey.verify_count == 1
    return journey, repo, runtime, service, first, repo.checkpoint


@pytest.mark.asyncio
async def test_first_fresh_read_after_failed_check_can_resume_repair_without_replaying_read() -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_work_progress
    from agent_hub.runtime.autogen.project_repair_phase import completed_round

    journey = ReadBeforeRepairJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert journey.context.routing_decision['engineering_diagnostic_allowance_version'] == 1
    first = await service.execute(repo.run_id)
    assert first.status is RunStatus.QUEUED
    assert repo.row.routing_decision['engineering_diagnostic_allowance_version'] == 1
    source = repo.checkpoint
    assert source.state['tool_ledger'][-1]['name'] == 'project.verify'
    assert source.state['tool_ledger'][-1]['status'] == 'failed'
    assert journey.write_count == journey.verify_count == 1 and repo.accept_count == 1
    runtime.second_fault = 'ack_read'
    continued = await service.execute(repo.run_id)
    assert hasattr(runtime, 'ack_checkpoint'), (
        continued.status, [(event.kind, event.reason) for event in repo.event_log[-8:]])
    stopped = repo.checkpoint
    before = stopped.to_payload()
    tools, models = stopped.state['tool_ledger'], stopped.state['model_ledger']
    assert tools == runtime.ack_checkpoint.state['tool_ledger']
    assert [row['name'] for row in tools[len(source.state['tool_ledger']):]] == ['workspace.read']
    assert all(row['status'] in {'succeeded', 'failed'} for row in tools)
    assert all(row['status'] == 'succeeded' for row in models)
    assert tools[-1]['status'] == 'succeeded' and tools[-1]['artifact_id'] == str(runtime.ack_artifact.id)
    assert tools[-1]['artifact_sha256'] == runtime.ack_artifact.content_sha256
    assert runtime.ack_artifact.content['result']['path'] == 'ACCEPTANCE.json'
    assert runtime.ack_artifact.content['result']['text'] == 'bad draft'
    assert runtime.ack_artifact.content['result']['truncated'] is False
    assert stopped.state['artifact_registry'][str(runtime.ack_artifact.id)] == runtime.ack_artifact.content_sha256
    refs = tuple(ArtifactReference(id=UUID(key), sha256=value)
                 for key, value in stopped.state['artifact_registry'].items())
    artifacts = await journey.artifacts.get_many(stopped.tenant_id, stopped.run_id, refs)
    assert completed_round(journey.context, stopped.state, artifacts)
    allowed = {participant.id: participant.allowed_tools for participant in runtime.runner._plan.participants}
    old_progress = engineering_work_progress(journey.context, tool_entries=source.state['tool_ledger'],
        artifacts=artifacts, allowed_tools=allowed, include_draft_diagnoses=True)
    assert engineering_work_progress(journey.context, tool_entries=tools, artifacts=artifacts,
        allowed_tools=allowed, include_draft_diagnoses=True) == old_progress
    assert stopped.state['usage'] == runtime.ack_checkpoint.state['usage']
    assert source.state['usage']['tokens'] < stopped.state['usage']['tokens'] < 500
    assert 0 < stopped.state['remaining_timeout_seconds'] <= source.state['remaining_timeout_seconds']
    assert 0 < stopped.state['remaining_absolute_timeout_seconds'] <= source.state['remaining_absolute_timeout_seconds']
    assert journey.write_count == journey.verify_count == 1
    assert runtime.ack_counts == (len(journey.calls), len(journey.requests))
    assert continued.id == first.id == repo.run_id
    assert continued.status is RunStatus.QUEUED, repo.continuation_errors

    calls, requests = runtime.ack_counts
    old_runner = runtime.runner
    runtime.second_fault = None
    completed = await service.execute(repo.run_id)
    assert runtime.runner is not old_runner and completed.id == first.id
    assert completed.status is RunStatus.COMPLETED, [(event.kind, event.reason) for event in repo.event_log[-6:]]
    assert [call.tool_name for call in journey.calls[calls:]] == [
        'workspace.write_text', 'project.verify', 'workspace.bundle']
    assert sum(call.tool_name == 'workspace.read' for call in journey.calls) == 1
    new_requests = journey.requests[requests:]
    assert [('main_report' if any(message.role == 'system'
                                 and 'Main Agent final delivery' in str(message.content)
                                 for message in request.messages) else request.logical_model)
            for request in new_requests] == ['selector', 'writer', 'writer', 'selector', 'writer', 'main_report']
    new_models = repo.checkpoint.state['model_ledger'][len(models):]
    refs = tuple(ArtifactReference(id=UUID(row['artifact_id']), sha256=row['artifact_sha256']) for row in new_models)
    private_models = await journey.artifacts.get_many(repo.checkpoint.tenant_id, repo.run_id, refs)
    assert [tuple(call['name'] for call in artifact.content['tool_calls']) for artifact in private_models] == [
        (), ('workspace_write_text',), ('project_verify',), (), ('workspace_bundle',), ()]
    assert [artifact.content['text'] for artifact in private_models if not artifact.content['tool_calls']] == [
        'implementer', 'implementer', journey.report]
    assert [artifact.content['logical_model'] for artifact in private_models] == [
        request.logical_model for request in new_requests]
    assert journey.write_count == journey.verify_count == 2
    assert repo.accept_count == 1 and len(repo.outbox) == 2
    assert repo.checkpoint.state['tool_ledger'][:len(tools)] == tools
    assert repo.checkpoint.state['model_ledger'][:len(models)] == models
    assert stopped.to_payload() == before
    assert repo.checkpoint.state['usage']['tokens'] >= stopped.state['usage']['tokens']
    assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(stopped.state['usage']['cost_usd'])
    assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= stopped.state['remaining_absolute_timeout_seconds']
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    assert len(set(journey.plans)) == 1
    final = [event.artifact for event in repo.event_log if event.artifact is not None
             and event.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    with zipfile.ZipFile(io.BytesIO(journey.bundle_bytes)) as archive:
        assert archive.read('ACCEPTANCE.json') == b'repaired draft'


@pytest.mark.asyncio
async def test_admitted_diagnostic_read_crosses_soft_then_replans_to_zip_and_report() -> None:
    journey, repo, runtime, service, first, source = await _read_repair_fixture(cross_read_soft=True)
    runtime.second_fault = None
    completed = await service.execute(repo.run_id)
    assert journey.read_count == 1 and len(journey.crossed_reads) == len(journey.read_acks) == 1
    before, after = journey.crossed_reads[0]
    assert before['deadline'] == after['deadline'] < asyncio.get_running_loop().time()
    assert before['absolute_deadline'] == after['absolute_deadline']
    acknowledged, granted_budget = journey.read_acks[0]
    assert granted_budget['deadline'] > after['deadline']
    assert granted_budget['absolute_deadline'] == after['absolute_deadline']
    assert granted_budget['progress_units'] == after['progress_units'] + 1
    assert acknowledged.state['engineering_diagnostic_consumed_through'] == len(acknowledged.state['tool_ledger'])
    assert acknowledged.state['remaining_timeout_seconds'] > 0
    assert acknowledged.state['tool_ledger'][-1]['name'] == 'workspace.read'
    assert acknowledged.state['tool_ledger'][-1]['status'] == 'succeeded'
    assert 0 < acknowledged.state['remaining_absolute_timeout_seconds'] <= source.state['remaining_absolute_timeout_seconds']
    assert acknowledged.state['usage']['tokens'] < runtime.runner._plan.token_budget == 500
    assert Decimal(acknowledged.state['usage']['cost_usd']) < runtime.runner._plan.cost_budget_usd
    assert completed.id == first.id
    assert completed.status is RunStatus.COMPLETED, [(event.kind, event.reason) for event in repo.event_log[-7:]]
    assert [call.tool_name for call in journey.calls[len(source.state['tool_ledger']):]] == [
        'workspace.read', 'workspace.write_text', 'project.verify', 'workspace.bundle']
    assert journey.write_count == journey.verify_count == 2
    assert repo.checkpoint.state['tool_ledger'][:len(acknowledged.state['tool_ledger'])] == acknowledged.state['tool_ledger']
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    final = [event.artifact for event in repo.event_log if event.artifact is not None
             and event.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    with zipfile.ZipFile(io.BytesIO(journey.bundle_bytes)) as archive:
        assert archive.read('ACCEPTANCE.json') == b'repaired draft'


@pytest.mark.asyncio
@pytest.mark.parametrize('summary_only', [False, True])
async def test_same_epoch_repeat_read_preserves_saved_window_but_cannot_regrant(summary_only: bool) -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_work_progress
    from agent_hub.runtime.autogen.project_repair_phase import (
        DIAGNOSTIC_WATERMARK,
        completed_round,
        diagnostic_progress,
        productive_boundary,
    )

    journey, repo, runtime, service, _first, source = await _read_repair_fixture(
        repeat_reads=2, repeat_summary=summary_only)
    assert repo.row.routing_decision.get('self_repair_episode_progress') is None
    runtime.second_fault = 'ack_repeat_read'
    stopped_run = await service.execute(repo.run_id)
    assert journey.read_count == 2 and len(journey.read_acks) == 2
    first, first_budget = journey.read_acks[0]
    second, second_budget = journey.read_acks[1]
    assert first.state['tool_ledger'][-1]['name'] == second.state['tool_ledger'][-1]['name'] == 'workspace.read'
    assert first_budget['deadline'] == second_budget['deadline']
    assert first_budget['absolute_deadline'] == second_budget['absolute_deadline']
    assert first_budget['progress_units'] == second_budget['progress_units']
    assert 0 < second.state['remaining_timeout_seconds'] <= first.state['remaining_timeout_seconds']
    assert 0 < second.state['remaining_absolute_timeout_seconds'] <= first.state['remaining_absolute_timeout_seconds']
    elapsed = first.state['remaining_absolute_timeout_seconds'] - second.state['remaining_absolute_timeout_seconds']
    assert abs(second.state['remaining_timeout_seconds'] - (first.state['remaining_timeout_seconds'] - elapsed)) <= 1e-6
    assert first.state['project_repair_phase']['source_checkpoint_sha256'] == source.state_sha256
    assert second.state['project_repair_phase']['source_checkpoint_sha256'] == source.state_sha256
    refs = tuple(ArtifactReference(id=UUID(key), sha256=value)
                 for key, value in second.state['artifact_registry'].items())
    artifacts = await journey.artifacts.get_many(second.tenant_id, second.run_id, refs)
    private = {str(artifact.id): artifact for artifact in artifacts}
    first_result = private[first.state['tool_ledger'][-1]['artifact_id']].content['result']
    second_result = private[second.state['tool_ledger'][-1]['artifact_id']].content['result']
    assert {key: first_result[key] for key in ('path', 'text', 'truncated')} == {
        key: second_result[key] for key in ('path', 'text', 'truncated')}
    assert ('summary' in second_result) is summary_only
    allowed = {participant.id: participant.allowed_tools for participant in runtime.runner._plan.participants}
    assert engineering_work_progress(journey.context, tool_entries=first.state['tool_ledger'],
        artifacts=artifacts, allowed_tools=allowed) == engineering_work_progress(
        journey.context, tool_entries=second.state['tool_ledger'], artifacts=artifacts, allowed_tools=allowed)
    first_keys = diagnostic_progress(journey.context, phase=first.state['project_repair_phase'],
        models=first.state['model_ledger'], tools=first.state['tool_ledger'], artifacts=artifacts)
    second_keys = diagnostic_progress(journey.context, phase=second.state['project_repair_phase'],
        models=second.state['model_ledger'], tools=second.state['tool_ledger'], artifacts=artifacts)
    assert len(first_keys) == 1 and first_keys == second_keys
    boundary = next(iter(first_keys.values()))
    assert boundary == len(first.state['tool_ledger']) < len(second.state['tool_ledger'])
    assert second.state['project_repair_phase']['source_tool_count'] == len(source.state['tool_ledger']) < boundary
    assert first.state[DIAGNOSTIC_WATERMARK] == second.state[DIAGNOSTIC_WATERMARK] == boundary
    assert completed_round(journey.context, second.state, artifacts)
    assert stopped_run.status is RunStatus.QUEUED, repo.continuation_errors
    consumed = dict(repo.row.routing_decision['self_repair_episode_progress'])
    assert consumed == productive_boundary(journey.context, second, artifacts)
    assert consumed['tool_count'] == len(second.state['tool_ledger'])
    assert consumed['model_count'] == len(second.state['model_ledger'])
    consumed_context = journey.context.model_copy(update={'routing_decision': dict(repo.row.routing_decision)})
    with pytest.raises(ValueError):
        productive_boundary(consumed_context, second, artifacts)
    assert len(repo.outbox) == 2 and repo.accept_count == 1
    queued_version = repo.row.version
    queued_route = dict(repo.row.routing_decision)
    with pytest.raises(RunConflict, match='not quiescent'):
        await repo.continue_project_repair_and_enqueue(tenant_id=repo.row.tenant_id,
            run_id=repo.run_id, version=queued_version, checkpoint_sha256=second.state_sha256)
    assert repo.row.version == queued_version and repo.row.status == RunStatus.QUEUED.value
    assert repo.row.routing_decision == queued_route and repo.checkpoint.state_sha256 == second.state_sha256
    assert len(repo.outbox) == 2 and repo.accept_count == 1
    assert journey.write_count == journey.verify_count == 1
    calls, requests = len(journey.calls), len(journey.requests)
    old_runner = runtime.runner
    runtime.read_stop_count = 3
    failed = await service.execute(repo.run_id)
    assert runtime.runner is not old_runner
    assert failed.status is RunStatus.FAILED, 'consumed diagnostic credit cannot authorize another continuation'
    assert journey.read_count == 3 and len(journey.read_acks) == 3
    third, third_budget = journey.read_acks[-1]
    assert [call.tool_name for call in journey.calls[calls:]] == ['workspace.read']
    assert len(journey.requests) > requests
    assert third.state['tool_ledger'][:len(second.state['tool_ledger'])] == second.state['tool_ledger']
    assert third.state['model_ledger'][:len(second.state['model_ledger'])] == second.state['model_ledger']
    assert third_budget['progress_units'] == second_budget['progress_units']
    assert 0 < third.state['remaining_timeout_seconds'] <= second.state['remaining_timeout_seconds']
    assert 0 < third.state['remaining_absolute_timeout_seconds'] <= second.state['remaining_absolute_timeout_seconds']
    assert third.state[DIAGNOSTIC_WATERMARK] == boundary
    refs = tuple(ArtifactReference(id=UUID(key), sha256=value)
                 for key, value in third.state['artifact_registry'].items())
    artifacts = await journey.artifacts.get_many(third.tenant_id, third.run_id, refs)
    assert diagnostic_progress(journey.context, phase=third.state['project_repair_phase'],
        models=third.state['model_ledger'], tools=third.state['tool_ledger'], artifacts=artifacts) == first_keys
    assert completed_round(journey.context, third.state, artifacts)
    with pytest.raises(ValueError):
        productive_boundary(journey.context, third, artifacts)
    assert repo.row.routing_decision['self_repair_episode_progress'] == consumed
    assert len(repo.outbox) == 2 and repo.accept_count == 1
    assert journey.write_count == journey.verify_count == 1
    assert third.state['usage']['tokens'] >= second.state['usage']['tokens']
    assert Decimal(third.state['usage']['cost_usd']) >= Decimal(second.state['usage']['cost_usd'])
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    calls, requests = len(journey.calls), len(journey.requests)
    assert (await service.execute(repo.run_id)).status is RunStatus.FAILED
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
async def test_diagnostic_read_checkpoint_ack_failure_leaves_no_budget_or_consumption() -> None:
    from agent_hub.runtime.autogen.checkpoint_publication import PendingCheckpoint
    from agent_hub.runtime.autogen.project_repair_phase import (
        DIAGNOSTIC_POLICY,
        DIAGNOSTIC_WATERMARK,
        completed_round,
    )

    journey, repo, runtime, service, _first, source = await _read_repair_fixture(cross_read_soft=True)
    runtime.second_fault = None
    original_success, original_ack = _DiscussionDurability.succeed_tool, PendingCheckpoint.acknowledge
    observation: dict[str, Any] = {}
    rejected = []
    failure = OSError('first diagnostic read checkpoint ACK failed')

    def budget_state(durable):
        return (asdict(journey.runner._wall_budget), journey.runner._wall_deadline,
                json.dumps(durable.repair_phase, default=dict, sort_keys=True),
                json.dumps(journey.runner._discussion_control.to_state(), default=dict, sort_keys=True))

    async def record_success(durable, artifact):
        if artifact.content.get('tool_name') != 'workspace.read':
            return await original_success(durable, artifact)
        observation.update(durable=durable, artifact=artifact, before=budget_state(durable),
            usage=dict(journey.runner._last_checkpoint.state['usage']),
            previous_checkpoint=journey.runner._last_checkpoint,
            calls=len(journey.calls), requests=len(journey.requests))
        assert observation['before'][0]['deadline'] <= asyncio.get_running_loop().time()
        try:
            await original_success(durable, artifact)
        except BaseException:
            observation.update(after=budget_state(durable), rolled_back=True,
                rolled_back_checkpoint=journey.runner._last_checkpoint)
            raise

    def reject_read_ack(pending):
        checkpoint = journey.runner._last_checkpoint
        tools = () if checkpoint is None else checkpoint.state['tool_ledger']
        if tools and tools[-1]['name'] == 'workspace.read' and tools[-1]['status'] == 'succeeded':
            durable = observation['durable']
            assert completed_round(journey.context, checkpoint.state, durable.artifacts)
            assert checkpoint.state['usage'] == observation['usage']
            previous_budget = observation['before'][0]
            live_budget = asdict(journey.runner._wall_budget)
            now = asyncio.get_running_loop().time()
            assert previous_budget['deadline'] <= now < live_budget['deadline']
            assert live_budget['progress_units'] == previous_budget['progress_units'] + 1
            assert live_budget['absolute_deadline'] == previous_budget['absolute_deadline']
            assert checkpoint.state[DIAGNOSTIC_POLICY] == 1
            assert checkpoint.state[DIAGNOSTIC_WATERMARK] == len(tools)
            assert checkpoint.state[DIAGNOSTIC_WATERMARK] > observation['previous_checkpoint'].state[DIAGNOSTIC_WATERMARK]
            assert checkpoint.state['remaining_timeout_seconds'] > 0
            rejected.append(checkpoint)
            pending.future.set_exception(failure)
            return False
        return original_ack(pending)

    with patch.object(_DiscussionDurability, 'succeed_tool', record_success), \
            patch.object(PendingCheckpoint, 'acknowledge', reject_read_ack):
        failed = await service.execute(repo.run_id)
    assert len(rejected) == 1 and observation.get('rolled_back') is True
    assert observation['after'] == observation['before']
    assert observation['rolled_back_checkpoint'] is observation['previous_checkpoint']
    assert observation['rolled_back_checkpoint'].state[DIAGNOSTIC_WATERMARK] < rejected[0].state[DIAGNOSTIC_WATERMARK]
    durable = observation['durable']
    assert durable.tool_entries[-1]['status'] == 'running'
    assert str(observation['artifact'].id) not in {str(artifact.id) for artifact in durable.artifacts}
    assert rejected[0].state['usage'] == observation['usage']
    assert journey.read_acks == [] and journey.read_count == 1
    assert (len(journey.calls), len(journey.requests)) == (observation['calls'], observation['requests'])
    assert failed.status is RunStatus.FAILED and len(repo.outbox) == 1
    assert source.state['usage']['tokens'] < observation['usage']['tokens'] < 500
    assert journey.write_count == journey.verify_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining', [0.0, 0.05])
async def test_diagnostic_cold_restore_uses_saved_remaining_without_new_window(remaining: float) -> None:
    journey, repo, runtime, service, _first, _source = await _read_repair_fixture()
    runtime.second_fault = 'ack_read'
    await service.execute(repo.run_id)
    assert journey.read_count == 1 and len(journey.read_acks) == 1
    committed = repo.checkpoint
    state = committed.to_payload()['state']
    assert state['remaining_timeout_seconds'] > remaining
    # Consume saved soft time without changing private evidence or allowance state.
    state['remaining_timeout_seconds'] = remaining
    saved = RuntimeCheckpoint(id=uuid4(), runtime_type=committed.runtime_type,
        runtime_version=committed.runtime_version, run_id=committed.run_id, tenant_id=committed.tenant_id,
        mode=committed.mode, state=state)
    before = saved.to_payload()
    calls, requests = len(journey.calls), len(journey.requests)
    cold = journey.runtime()
    await cold.restore_checkpoint(saved)
    async with aclosing(cold.run(journey.context.model_copy(update={'checkpoint': saved}))) as stream:
        first = await anext(stream)
        assert first.kind is EventKind.DISCUSSION_STARTED, first.reason
        now = asyncio.get_running_loop().time()
        assert cold._wall_budget.remaining(now=now) <= remaining
        assert cold._wall_budget.absolute_remaining(now=now) <= saved.state['remaining_absolute_timeout_seconds']
        if remaining == 0.0:
            events = [event async for event in stream]
            assert events[-1].kind is EventKind.RUNTIME_FAILED, events[-1].reason
            assert all(event.checkpoint.state['remaining_timeout_seconds'] == 0.0
                       for event in events if event.checkpoint is not None)
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)
    assert saved.to_payload() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,pending_tools,pending_models', [
    ('ack_verify', ['workspace.bundle'], ['selector', 'writer', 'main_report']),
    ('ack_bundle', [], ['main_report']),
])
async def test_acknowledged_delivery_before_compact_continues_without_old_replay(
    fault: str, pending_tools: list[str], pending_models: list[str],
) -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    consumed = dict(repo.row.routing_decision['self_repair_episode_progress'])
    source = repo.checkpoint
    runtime.second_fault = fault
    continued = await service.execute(repo.run_id)
    stopped = repo.checkpoint
    before = stopped.to_payload()
    tools, models = stopped.state['tool_ledger'], stopped.state['model_ledger']
    assert hasattr(runtime, 'ack_checkpoint'), (
        continued.status, repo.continuation_errors,
        [(event.kind, event.reason) for event in repo.event_log[-8:]],
        [call.tool_name for call in journey.calls])
    ack = runtime.ack_checkpoint
    assert tools == ack.state['tool_ledger'] and models == ack.state['model_ledger']
    assert tools[-1]['name'] == ('project.verify' if fault == 'ack_verify' else 'workspace.bundle')
    assert tools[-1]['status'] == 'succeeded'
    assert tools[-1]['artifact_id'] == str(runtime.ack_artifact.id)
    assert tools[-1]['artifact_sha256'] == runtime.ack_artifact.content_sha256
    assert ack.state['artifact_registry'][str(runtime.ack_artifact.id)] == runtime.ack_artifact.content_sha256
    assert stopped.state['usage'] == ack.state['usage']
    assert all(row['status'] == 'succeeded' for row in models)
    assert all(row['status'] in {'succeeded', 'failed'} for row in tools)
    assert stopped.state['engineering_tool_prefix'] < len(tools)
    assert stopped.state['project_repair_phase']['model_floor'] < len(models)
    assert tools[:len(source.state['tool_ledger'])] == source.state['tool_ledger']
    assert models[:len(source.state['model_ledger'])] == source.state['model_ledger']
    assert consumed['tool_count'] == len(source.state['tool_ledger'])
    assert stopped.state['usage']['tokens'] >= source.state['usage']['tokens'] > 0
    assert Decimal(stopped.state['usage']['cost_usd']) >= Decimal(source.state['usage']['cost_usd']) > 0
    assert stopped.state['remaining_absolute_timeout_seconds'] <= source.state['remaining_absolute_timeout_seconds']
    assert stopped.state['remaining_timeout_seconds'] <= source.state['remaining_timeout_seconds']
    assert runtime.ack_counts == (len(journey.calls), len(journey.requests))
    assert journey.write_count == 2 and journey.verify_count == 2
    assert continued.status is RunStatus.QUEUED, (
        repo.continuation_errors, [(event.kind, event.reason) for event in repo.event_log[-6:]])

    assert repo.accept_count == 1 and len(repo.outbox) == 3
    calls, requests = runtime.ack_counts
    old_runner = runtime.runner
    runtime.second_fault = None
    completed = await service.execute(repo.run_id)
    assert runtime.runner is not old_runner
    assert completed.status is RunStatus.COMPLETED, [(event.kind, event.reason) for event in repo.event_log[-6:]]
    assert [call.tool_name for call in journey.calls[calls:]] == pending_tools
    assert [('main_report' if any(message.role == 'system'
                                 and 'Main Agent final delivery' in str(message.content)
                                 for message in request.messages) else request.logical_model)
            for request in journey.requests[requests:]] == pending_models
    assert repo.checkpoint.state['tool_ledger'][:len(tools)] == tools
    assert repo.checkpoint.state['model_ledger'][:len(models)] == models
    assert stopped.to_payload() == before
    assert journey.write_count == 2 and journey.verify_count == 2
    assert sum(call.tool_name == 'workspace.bundle' for call in journey.calls) == 1
    final = [event.artifact for event in repo.event_log if event.artifact is not None
             and event.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    assert repo.checkpoint.state['usage']['tokens'] >= stopped.state['usage']['tokens']
    assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(stopped.state['usage']['cost_usd'])
    assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= stopped.state['remaining_absolute_timeout_seconds']
    assert repo.checkpoint.state['remaining_timeout_seconds'] <= stopped.state['remaining_timeout_seconds']
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0
    assert repo.row.routing_decision['self_repair_attempt'] == 1
    with zipfile.ZipFile(io.BytesIO(journey.bundle_bytes)) as archive:
        assert archive.read('ACCEPTANCE.json') == b'repaired draft'
    calls, requests = len(journey.calls), len(journey.requests)
    cold = journey.runtime()
    await cold.restore_checkpoint(repo.checkpoint)
    events = [event async for event in cold.run(journey.context.model_copy(update={'checkpoint': repo.checkpoint}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['ack_verify', 'ack_bundle'])
async def test_completed_round_checkpoint_ack_failure_rolls_back_without_provider_calls(fault: str) -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = fault
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED, repo.continuation_errors
    stopped = runtime.ack_checkpoint
    before = stopped.to_payload()
    state = stopped.state
    references = tuple(ArtifactReference(id=UUID(key), sha256=value)
                       for key, value in state['artifact_registry'].items())
    artifacts = await journey.artifacts.get_many(stopped.tenant_id, stopped.run_id, references)
    calls, requests = len(journey.calls), len(journey.requests)
    published = []
    failure = OSError('drain checkpoint ACK failed')

    async def publish(saved_artifacts, model_entries, tool_entries):
        assert durability._model_cursor == len(model_entries)
        assert durability._tool_cursor == len(tool_entries)
        assert durability.engineering_tool_prefix == len(tool_entries)
        assert durability.repair_phase['model_floor'] == len(model_entries)
        assert tuple(model_entries) == state['model_ledger']
        assert tuple(tool_entries) == state['tool_ledger']
        assert saved_artifacts == artifacts
        published.append((durability._model_cursor, durability._tool_cursor))
        raise failure

    async def unexpected_artifact_write(*args):
        pytest.fail('draining a completed round must not write or abort artifacts')

    def unexpected_finalize(*args):
        pytest.fail('draining a completed round must not finalize artifacts')

    durability = _DiscussionDurability(publish, unexpected_artifact_write,
        unexpected_artifact_write, unexpected_finalize, run_id=stopped.run_id, tenant_id=stopped.tenant_id,
        model_entries=state['model_ledger'], tool_entries=state['tool_ledger'], artifacts=artifacts,
        engineering_tool_prefix=state['engineering_tool_prefix'], repair_phase=state['project_repair_phase'])
    previous_cursors = (durability._model_cursor, durability._tool_cursor, durability.engineering_tool_prefix)
    previous_phase = dict(state['project_repair_phase'])
    previous_models = [dict(entry) for entry in state['model_ledger']]
    previous_tools = [dict(entry) for entry in state['tool_ledger']]
    assert previous_cursors[0] < len(previous_models) and previous_cursors[1] < len(previous_tools)
    with pytest.raises(OSError, match='drain checkpoint ACK failed') as raised:
        await durability.drain_completed_round(journey.context, state)
    assert raised.value is failure
    assert published == [(len(previous_models), len(previous_tools))]
    assert (durability._model_cursor, durability._tool_cursor,
            durability.engineering_tool_prefix) == previous_cursors
    assert durability.repair_phase == previous_phase
    assert durability.model_entries == previous_models and durability.tool_entries == previous_tools
    assert durability.artifacts == artifacts
    assert stopped.to_payload() == before
    assert state['usage'] == before['state']['usage']
    assert state['remaining_timeout_seconds'] == before['state']['remaining_timeout_seconds']
    assert state['remaining_absolute_timeout_seconds'] == before['state']['remaining_absolute_timeout_seconds']
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['missing_intent', 'broken_intent', 'missing_artifact',
                                  'legacy_selector_broken_writer', 'legacy_selector_missing_writer'])
async def test_pending_round_legacy_fallback_does_not_accept_corrupt_private_intent(defect: str) -> None:
    journey = ProjectJourney()
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'ack_verify'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED, repo.continuation_errors
    stopped = runtime.ack_checkpoint
    state = stopped.to_payload()['state']
    refs = tuple(ArtifactReference(id=UUID(key), sha256=value)
                 for key, value in state['artifact_registry'].items())
    artifacts = list(await journey.artifacts.get_many(stopped.tenant_id, stopped.run_id, refs))
    if defect.startswith('legacy_selector_'):
        selection = state['model_ledger'][state['project_repair_phase']['model_floor']]
        index = next(index for index, artifact in enumerate(artifacts) if str(artifact.id) == selection['artifact_id'])
        original_selection = artifacts[index]
        content = original_selection.model_dump(mode='json')['content']
        assert content['project_repair_round']['actor'] == '__selector__' and content['tool_calls'] == []
        del content['project_repair_round']
        legacy = Artifact(id=original_selection.id, type=original_selection.type,
            producer=original_selection.producer, content=content)
        artifacts[index] = legacy
        selection['artifact_sha256'] = legacy.content_sha256
        state['artifact_registry'][str(legacy.id)] = legacy.content_sha256
    row = state['model_ledger'][-1]
    position = next(index for index, artifact in enumerate(artifacts) if str(artifact.id) == row['artifact_id'])
    original = artifacts[position]
    assert original.type == 'model_result' and row['status'] == 'succeeded'
    assert 'project_repair_round' in original.content
    if defect in {'missing_artifact', 'legacy_selector_missing_writer'}:
        artifacts.pop(position)
    else:
        content = original.model_dump(mode='json')['content']
        if defect == 'missing_intent':
            del content['project_repair_round']
        else:
            content['project_repair_round'] = {'version': 1}
        changed = Artifact(id=original.id, type=original.type, producer=original.producer, content=content)
        artifacts[position] = changed
        row['artifact_sha256'] = changed.content_sha256
        state['artifact_registry'][str(changed.id)] = changed.content_sha256
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type=stopped.runtime_type,
        runtime_version=stopped.runtime_version, run_id=stopped.run_id, tenant_id=stopped.tenant_id,
        mode=stopped.mode, state=state)

    async def unexpected(*args):
        pytest.fail('a fallback or rejected round must not publish or write artifacts')

    durable = _DiscussionDurability(unexpected, unexpected, unexpected, lambda _: pytest.fail('no finalize'),
        run_id=stopped.run_id, tenant_id=stopped.tenant_id, model_entries=checkpoint.state['model_ledger'],
        tool_entries=checkpoint.state['tool_ledger'], artifacts=tuple(artifacts),
        engineering_tool_prefix=checkpoint.state['engineering_tool_prefix'],
        repair_phase=checkpoint.state['project_repair_phase'])
    before = (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
              dict(durable.repair_phase), list(durable.model_entries), list(durable.tool_entries))
    calls, requests = len(journey.calls), len(journey.requests)
    if defect == 'missing_intent':
        await durable.drain_completed_round(journey.context, checkpoint.state)
    else:
        with pytest.raises(ValueError, match='project repair phase is invalid'):
            await durable.drain_completed_round(journey.context, checkpoint.state)
    assert (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
            durable.repair_phase, durable.model_entries, durable.tool_entries) == before
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


class ApprovalProjectJourney(ProjectJourney):
    approval_id = 'approval_active_repair_verify'

    def __init__(self) -> None:
        super().__init__()
        self.approved = False

    async def invoke(self, tenant_id, request, *, user_id=None, role=None):
        if request.tool_name == 'project.verify' and self.write_count == 2 and not self.approved:
            self.calls.append(request)
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                status='failed', failure_reason='capability requires approval',
                payload={'approval_id': self.approval_id})
        return await super().invoke(tenant_id, request, user_id=user_id, role=role)


async def _active_approval_fixture(journey=None, *, validate_pending: bool = True):
    journey = ApprovalProjectJourney() if journey is None else journey
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    source = repo.checkpoint
    journey.context = journey.context.model_copy(update={
        'checkpoint': source, 'routing_decision': dict(repo.row.routing_decision)})
    runner = journey.runtime()
    await runner.restore_checkpoint(source)
    events = [event async for event in runner.run(journey.context)]
    saved = await runner.save_checkpoint()
    assert checkpoint_waits_for_approval(saved, journey.approval_id), [
        (event.kind, event.reason) for event in events[-5:]]
    assert saved.state['project_repair_phase']['model_floor'] < len(saved.state['model_ledger'])
    refs = tuple(ArtifactReference(id=UUID(key), sha256=value)
                 for key, value in saved.state['artifact_registry'].items())
    artifacts = await journey.artifacts.get_many(saved.tenant_id, saved.run_id, refs)
    snapshots = []
    writes = {}

    async def publish(saved_artifacts, models, tools):
        state = saved.to_payload()['state']
        state.update(model_ledger=models, tool_ledger=tools,
            engineering_tool_prefix=durable.engineering_tool_prefix,
            project_repair_phase=dict(durable.repair_phase),
            artifact_registry={str(artifact.id): artifact.content_sha256 for artifact in saved_artifacts})
        if durable.approval_batch is None:
            state.pop('approval_batch', None)
        else:
            state['approval_batch'] = dict(durable.approval_batch)
        snapshots.append(RuntimeCheckpoint(id=uuid4(), runtime_type=saved.runtime_type,
            runtime_version=saved.runtime_version, run_id=saved.run_id, tenant_id=saved.tenant_id,
            mode=saved.mode, state=state))

    async def store(artifact):
        write_id = uuid4()
        reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
        writes[write_id] = reference
        await journey.artifacts.reserve_write(saved.tenant_id, saved.run_id, reference, write_id=write_id)
        await journey.artifacts.put(saved.tenant_id, saved.run_id, artifact, write_id=write_id)
        return write_id

    async def abort(write_id):
        return await journey.artifacts.abort_write(saved.tenant_id, saved.run_id,
            writes[write_id], write_id=write_id)

    durable = _DiscussionDurability(publish, store, abort, lambda _: None,
        run_id=saved.run_id, tenant_id=saved.tenant_id, model_entries=saved.state['model_ledger'],
        tool_entries=saved.state['tool_ledger'], artifacts=artifacts,
        engineering_tool_prefix=saved.state['engineering_tool_prefix'],
        repair_phase=saved.state['project_repair_phase'], approval_batch=saved.state['approval_batch'])
    if validate_pending:
        pending = _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'})
        assert pending is not None and pending[0] == 'implementer' and pending[2] == 0
    return journey, durable, saved, snapshots


@pytest.mark.asyncio
async def test_pending_approval_drain_is_noop_and_retains_original_resume_path() -> None:
    journey, durable, saved, snapshots = await _active_approval_fixture()
    before = (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
              dict(durable.repair_phase), dict(durable.approval_batch))
    calls, requests = len(journey.calls), len(journey.requests)
    pending = _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'})
    await durable.drain_completed_round(journey.context, saved.state)
    assert (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
            durable.repair_phase, durable.approval_batch) == before
    assert _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'}) == pending
    assert snapshots == []
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


async def _complete_approved_fixture():
    journey, durable, saved, snapshots = await _active_approval_fixture()
    actor, calls, cursor = _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'})
    assert actor == 'implementer' and cursor == 0 and len(calls) == 1
    journey.approved = True
    await approval_tool(durable, journey, [], name='project.verify').run_json(
        {}, CancellationToken(), call_id=calls[0]['id'])
    assert durable.tool_entries[-1]['status'] == 'succeeded'
    assert durable.approval_batch['next_index'] == len(calls)
    assert journey.verify_count == 2 and journey.write_count == 2
    assert durable._model_cursor == saved.state['project_repair_phase']['model_floor']
    return journey, durable, saved, snapshots


def _replace_approval_model_content(durable, row, content):
    original = durable._artifacts[row['artifact_id']]
    assert original.type == 'model_result' and row['status'] == 'succeeded'
    changed = Artifact(id=original.id, type=original.type, producer=original.producer, content=content)
    durable._artifacts[str(changed.id)] = changed
    row['artifact_sha256'] = changed.content_sha256
    if durable.approval_batch['model_artifact_id'] == str(changed.id):
        durable.approval_batch['model_artifact_sha256'] = changed.content_sha256


class RetriedSelectorApprovalJourney(ApprovalProjectJourney):
    retried_selector = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if (request.logical_model == 'selector' and self.write_count == 2 and self.verify_count == 1
                and not self.retried_selector):
            self.retried_selector = True
            self.requests.append(request)
            return GatewayCompletion(response=ModelResponse(text='no_candidate', usage=TokenUsage(1, 1, 2)),
                deployment_id='fixture', logical_model=request.logical_model,
                provider_id='fixture', provider_model='fixture/model', cost_usd=Decimal('0.001'))
        return await super().complete_with_context(request)


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['legacy', 'intent', 'foreign_prior_actor'])
async def test_real_selector_retry_approval_preserves_legacy_and_strict_intent(variant: str) -> None:
    journey, durable, saved, snapshots = await _active_approval_fixture(
        RetriedSelectorApprovalJourney(), validate_pending=False)
    floor = durable.repair_phase['model_floor']
    rows = durable.model_entries[floor:]
    assert len(rows) == 3 and all(row['status'] == 'succeeded' for row in rows)
    models = [durable._artifacts[row['artifact_id']] for row in rows]
    assert [model.content.get('text') for model in models[:2]] == ['no_candidate', 'implementer']
    assert [model.content['project_repair_round']['actor'] for model in models] == [
        '__selector__', '__selector__', 'implementer']
    assert models[0].content['tool_calls'] == models[1].content['tool_calls'] == ()
    assert models[-1].content['tool_calls'][0]['name'] == 'project_verify'
    participants = {'implementer': 'writer', 'reviewer': 'reviewer'}
    if variant == 'legacy':
        for row, model in zip(rows, models, strict=True):
            content = model.model_dump(mode='json')['content']
            del content['project_repair_round']
            legacy = Artifact(id=uuid4(), type=model.type, producer=model.producer, content=content)
            write_id = await durable._store(legacy)
            durable._finalize(write_id)
            del durable._artifacts[str(model.id)]
            durable._artifacts[str(legacy.id)] = legacy
            row.update(artifact_id=str(legacy.id), artifact_sha256=legacy.content_sha256)
            if durable.approval_batch['model_artifact_id'] == str(model.id):
                durable.approval_batch.update(model_artifact_id=str(legacy.id),
                    model_artifact_sha256=legacy.content_sha256)
        native = _DiscussionDurability(durable._publish, durable._store, durable._abort, durable._finalize,
            run_id=saved.run_id, tenant_id=saved.tenant_id, model_entries=tuple(durable.model_entries),
            tool_entries=tuple(durable.tool_entries), artifacts=durable.artifacts,
            engineering_tool_prefix=durable.engineering_tool_prefix, approval_batch=durable.approval_batch)
        assert _pending_approval_call(native, participants) is not None
    elif variant == 'foreign_prior_actor':
        content = models[0].model_dump(mode='json')['content']
        content['project_repair_round']['actor'] = 'reviewer'
        _replace_approval_model_content(durable, rows[0], content)
    before = (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
              dict(durable.repair_phase), dict(durable.approval_batch))
    calls, requests = len(journey.calls), len(journey.requests)
    if variant == 'foreign_prior_actor':
        with pytest.raises((RuntimeExecutionError, ValueError)):
            _pending_approval_call(durable, participants)
        assert (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
                durable.repair_phase, durable.approval_batch) == before
        assert snapshots == [] and (len(journey.calls), len(journey.requests)) == (calls, requests)
        return
    pending = _pending_approval_call(durable, participants)
    assert pending is not None and pending[0] == 'implementer' and pending[2] == 0
    journey.approved = True
    await approval_tool(durable, journey, [], name='project.verify').run_json(
        {}, CancellationToken(), call_id=pending[1][0]['id'])
    await durable.acknowledge_approved_round(participants)
    assert durable.approval_batch is None and durable.repair_phase['model_floor'] == len(durable.model_entries)
    acknowledged = snapshots[-1]
    calls = len(journey.calls)
    cold = journey.runtime()
    await cold.restore_checkpoint(acknowledged)
    events = [event async for event in cold.run(journey.context.model_copy(update={'checkpoint': acknowledged}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(event.kind, event.reason) for event in events[-5:]]
    assert [call.tool_name for call in journey.calls[calls:]] == ['workspace.bundle']
    assert len(journey.requests) == requests + 3
    assert journey.write_count == 2 and journey.verify_count == 2
    assert tuple(rows) == acknowledged.state['model_ledger'][floor:]


@pytest.mark.asyncio
async def test_real_selector_retry_verify_ack_cold_resume_does_not_replay_old_round() -> None:
    journey = RetriedSelectorApprovalJourney()
    journey.approved = True
    repo = ContinuationRepository(journey)
    runtime = BoundaryFailure(journey)
    service = RunService(repo, runtime_registry=RuntimeRegistry((runtime,)), router=None, task_queue=object())
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'after_write'
    assert (await service.execute(repo.run_id)).status is RunStatus.QUEUED
    runtime.second_fault = 'ack_verify'
    continued = await service.execute(repo.run_id)
    stopped = runtime.ack_checkpoint
    floor = stopped.state['project_repair_phase']['model_floor']
    pending = stopped.state['model_ledger'][floor:]
    assert len(pending) == 3 and all(row['status'] == 'succeeded' for row in pending)
    refs = tuple(ArtifactReference(id=UUID(row['artifact_id']), sha256=row['artifact_sha256']) for row in pending)
    artifacts = await journey.artifacts.get_many(stopped.tenant_id, stopped.run_id, refs)
    assert [artifact.content.get('text') for artifact in artifacts[:2]] == ['no_candidate', 'implementer']
    assert stopped.state['tool_ledger'][-1]['name'] == 'project.verify'
    assert stopped.state['tool_ledger'][-1]['status'] == 'succeeded'
    assert continued.status is RunStatus.QUEUED, repo.continuation_errors
    calls, requests = len(journey.calls), len(journey.requests)
    before = stopped.to_payload()
    runtime.second_fault = None
    assert (await service.execute(repo.run_id)).status is RunStatus.COMPLETED
    assert [call.tool_name for call in journey.calls[calls:]] == ['workspace.bundle']
    assert len(journey.requests) == requests + 3
    assert journey.write_count == 2 and journey.verify_count == 2
    assert stopped.to_payload() == before
    assert repo.checkpoint.state['model_ledger'][:len(stopped.state['model_ledger'])] == stopped.state['model_ledger']
    assert repo.checkpoint.state['tool_ledger'][:len(stopped.state['tool_ledger'])] == stopped.state['tool_ledger']
    assert repo.checkpoint.state['usage']['tokens'] >= stopped.state['usage']['tokens']
    assert Decimal(repo.checkpoint.state['usage']['cost_usd']) >= Decimal(stopped.state['usage']['cost_usd'])
    assert repo.checkpoint.state['remaining_absolute_timeout_seconds'] <= stopped.state['remaining_absolute_timeout_seconds']
    assert repo.row.routing_decision['runtime_token_absolute_tokens'] == 500
    assert repo.row.routing_decision['runtime_timeout_absolute_seconds'] == 20.0


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['actor', 'request_hash', 'tool_start', 'model_position',
                                 'tool_names', 'selector_text', 'selector_actor', 'selector_tool_names'])
async def test_completed_approved_round_rejects_present_intent_binding_corruption(field: str) -> None:
    journey, durable, _saved, snapshots = await _complete_approved_fixture()
    pending = _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'})
    assert pending is not None and pending[2] == len(pending[1])
    index = durable.repair_phase['model_floor'] if field.startswith('selector_') else -1
    row = durable.model_entries[index]
    content = durable._artifacts[row['artifact_id']].model_dump(mode='json')['content']
    binding = content['project_repair_round']
    if field == 'selector_text':
        assert binding['actor'] == '__selector__' and content['text'].strip() == 'implementer'
        content['text'] = 'reviewer'
    elif field == 'selector_actor':
        assert binding['actor'] == '__selector__'
        binding['actor'] = 'implementer'
    elif field == 'selector_tool_names':
        assert binding['tool_names'] == {} and content['tool_calls'] == []
        binding['tool_names'] = {'project_verify': 'project.verify'}
    elif field == 'tool_names':
        binding[field] = {'project_verify': 'workspace.list'}
    elif field == 'actor':
        binding[field] = 'reviewer'
    elif field == 'request_hash':
        binding[field] = '0' * 64 if row['request_hash'] != '0' * 64 else '1' * 64
    else:
        binding[field] += 1
    _replace_approval_model_content(durable, row, content)
    # Rebind hashes so rejection must check intent, not merely a stale artifact digest.
    previous = (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
                dict(durable.repair_phase), dict(durable.approval_batch))
    tools, models, artifacts = list(durable.tool_entries), list(durable.model_entries), durable.artifacts
    calls, requests, publication_count = len(journey.calls), len(journey.requests), len(snapshots)
    with pytest.raises((RuntimeExecutionError, ValueError)):
        await durable.acknowledge_approved_round({'implementer': 'writer', 'reviewer': 'reviewer'})
    assert (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
            durable.repair_phase, durable.approval_batch) == previous
    assert durable.model_entries == models and durable.tool_entries == tools and durable.artifacts == artifacts
    assert len(snapshots) == publication_count
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
async def test_completed_legacy_approved_round_without_intent_remains_compatible() -> None:
    journey, durable, saved, snapshots = await _complete_approved_fixture()
    floor = durable.repair_phase['model_floor']
    for row in durable.model_entries[floor:]:
        assert row['status'] == 'succeeded'
        content = durable._artifacts[row['artifact_id']].model_dump(mode='json')['content']
        del content['project_repair_round']
        _replace_approval_model_content(durable, row, content)
    pending = _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'})
    assert pending is not None and pending[2] == len(pending[1])
    tools, models = list(durable.tool_entries), list(durable.model_entries)
    calls, requests, publication_count = len(journey.calls), len(journey.requests), len(snapshots)
    await durable.acknowledge_approved_round({'implementer': 'writer', 'reviewer': 'reviewer'})
    assert durable._model_cursor == len(models) and durable._tool_cursor == len(tools)
    assert durable.repair_phase['model_floor'] == len(models) and durable.engineering_tool_prefix == len(tools)
    assert durable.approval_batch is None
    assert durable.model_entries == models and durable.tool_entries == tools
    assert len(snapshots) == publication_count + 1
    assert snapshots[-1].state['usage'] == saved.state['usage']
    assert 'approval_batch' not in snapshots[-1].state
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
async def test_completed_approved_round_acknowledges_then_cold_restores_without_old_replay() -> None:
    journey, durable, saved, snapshots = await _complete_approved_fixture()
    calls, requests = len(journey.calls), len(journey.requests)
    tools, models = list(durable.tool_entries), list(durable.model_entries)
    await durable.acknowledge_approved_round({'implementer': 'writer', 'reviewer': 'reviewer'})
    assert durable._model_cursor == len(models) and durable._tool_cursor == len(tools)
    assert durable.engineering_tool_prefix == len(tools)
    assert durable.repair_phase['model_floor'] == len(models) and durable.approval_batch is None
    assert durable.tool_entries == tools and durable.model_entries == models
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)
    acknowledged = snapshots[-1]
    assert 'approval_batch' not in acknowledged.state
    cold = journey.runtime()
    await cold.restore_checkpoint(acknowledged)
    events = [event async for event in cold.run(journey.context.model_copy(update={'checkpoint': acknowledged}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(event.kind, event.reason) for event in events[-5:]]
    assert [call.tool_name for call in journey.calls[calls:]] == ['workspace.bundle']
    assert len(journey.requests) == requests + 3
    assert journey.verify_count == 2 and journey.write_count == 2
    final = [event.artifact for event in events if event.artifact is not None
             and event.artifact.content.get('presentation') == 'final_answer']
    assert len(final) == 1 and final[0].content['text'] == journey.report
    assert acknowledged.state['usage'] == saved.state['usage']
    assert cold._last_checkpoint.state['tool_ledger'][:len(tools)] == tuple(tools)


@pytest.mark.asyncio
async def test_approved_round_ack_failure_restores_batch_markers_but_keeps_acked_tool() -> None:
    journey, durable, saved, snapshots = await _complete_approved_fixture()
    previous = (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
                dict(durable.repair_phase), dict(durable.approval_batch))
    tools, models, artifacts = list(durable.tool_entries), list(durable.model_entries), durable.artifacts
    calls, requests, publication_count = len(journey.calls), len(journey.requests), len(snapshots)
    failure = OSError('approved round checkpoint ACK failed')
    attempted = []

    async def reject(*args):
        assert durable._model_cursor == len(models) and durable.engineering_tool_prefix == len(tools)
        assert durable.repair_phase['model_floor'] == len(models) and durable.approval_batch is None
        attempted.append(True)
        raise failure

    durable._publish = reject
    with pytest.raises(OSError, match='approved round checkpoint ACK failed') as raised:
        await durable.acknowledge_approved_round({'implementer': 'writer', 'reviewer': 'reviewer'})
    assert raised.value is failure and attempted == [True]
    assert (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
            durable.repair_phase, durable.approval_batch) == previous
    assert durable._tool_cursor == len(tools) and tools[-1]['status'] == 'succeeded'
    assert durable.model_entries == models and durable.tool_entries == tools and durable.artifacts == artifacts
    assert len(snapshots) == publication_count and snapshots[-1].state['usage'] == saved.state['usage']
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
async def test_approved_round_with_unexecuted_tail_cannot_advance() -> None:
    journey, durable, _saved, snapshots = await _active_approval_fixture()
    before = (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
              dict(durable.repair_phase), dict(durable.approval_batch))
    calls, requests = len(journey.calls), len(journey.requests)
    with pytest.raises(RuntimeExecutionError):
        await durable.acknowledge_approved_round({'implementer': 'writer', 'reviewer': 'reviewer'})
    assert (durable._model_cursor, durable._tool_cursor, durable.engineering_tool_prefix,
            durable.repair_phase, durable.approval_batch) == before
    assert durable.tool_entries[-1]['status'] == 'waiting_approval' and snapshots == []
    assert (len(journey.calls), len(journey.requests)) == (calls, requests)


@pytest.mark.asyncio
async def test_active_repair_waiting_approval_native_resume_rechecks_policy_then_finishes() -> None:
    journey, _durable, saved, snapshots = await _active_approval_fixture()
    calls, requests = len(journey.calls), len(journey.requests)
    unapproved = journey.runtime()
    await unapproved.restore_checkpoint(saved)
    events = [event async for event in unapproved.run(journey.context.model_copy(update={'checkpoint': saved}))]
    waiting = await unapproved.save_checkpoint()
    assert checkpoint_waits_for_approval(waiting, journey.approval_id), [
        (event.kind, event.reason) for event in events[-5:]]
    assert len(journey.requests) == requests and len(journey.calls) == calls + 1
    assert journey.verify_count == 1 and journey.write_count == 2
    journey.approved = True
    approved = journey.runtime()
    await approved.restore_checkpoint(waiting)
    events = [event async for event in approved.run(journey.context.model_copy(update={'checkpoint': waiting}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(event.kind, event.reason) for event in events[-5:]]
    assert journey.verify_count == 2 and journey.write_count == 2
    assert [call.tool_name for call in journey.calls[calls:]] == ['project.verify', 'project.verify', 'workspace.bundle']
    assert len(journey.requests) == requests + 3
    assert snapshots == []


@pytest.mark.asyncio
async def test_active_approved_native_resume_consumes_models_before_compact() -> None:
    journey, _durable, saved, _snapshots = await _active_approval_fixture()
    journey.approved = True
    requests, calls = len(journey.requests), len(journey.calls)
    original = _DiscussionDurability.drain_completed_round

    async def skip_only_approval_gate(durable, context, state):
        if durable.approval_batch is not None:
            assert _pending_approval_call(durable, {'implementer': 'writer', 'reviewer': 'reviewer'}) is not None
            return
        await original(durable, context, state)

    cold = journey.runtime()
    await cold.restore_checkpoint(saved)
    # Isolate the downstream compact bug from the separately covered entry gate.
    with patch.object(_DiscussionDurability, 'drain_completed_round', skip_only_approval_gate):
        events = [event async for event in cold.run(journey.context.model_copy(update={'checkpoint': saved}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [(event.kind, event.reason) for event in events[-5:]]
    assert journey.write_count == 2 and journey.verify_count == 2
    assert [call.tool_name for call in journey.calls[calls:]] == ['project.verify', 'workspace.bundle']
    assert len(journey.requests) == requests + 3


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
