"""PG/Node composed recovery proof, not paid-model or end-user acceptance.

Real: ConfigBacked factory, capability stack/policy/ApprovalService, RunRepository,
worker authority, private PG artifacts/receipts and offline install/build/test.
Doubles: published model configuration, credentials, capacity and a fixed model
transport; NodeFixtureExecutor runs real subprocesses, not production isolation.
The runner may supply systemd/PrivateNetwork isolation around this test process.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.service import ConfigService
from agent_hub.db.models import RunApprovalRow, RunCheckpointRow, RunRow
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import CapacityController
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.project_verification_receipts import ProjectVerificationReceiptRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, TaskContext, _freeze_object
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
from agent_hub.runtime.worker_execution import worker_runtime_events
from agent_hub.security.secrets import SecretService
from tests.integration.runs.test_verification_receipt_recovery import Store
from tests.integration.runs.test_verification_receipt_recovery import (
    receipt_store as receipt_store,  # noqa: PLC0414 - register the shared real PG fixture.
)
from tests.unit.runtime.test_configured_runtime import FakeSecretService, ImmediateCapacity
from tests.unit.runtime.test_direct_project_verification import NodeFixtureExecutor, batch


class FixedPublishedConfig:
    """Offline config double, restricted to this test's newly created tenant."""

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        assert tenant_id == self.tenant_id
        return ConfigRevision(
            id=uuid4(), tenant_id=tenant_id, version=1, status=ConfigStatus.PUBLISHED,
            created_by=uuid4(), created_at=datetime.now(UTC), document={
                'models': {'offline': {'deployments': [{
                    'provider': 'openai', 'model': 'offline-fixed-response',
                    'api_base': 'http://127.0.0.1:1/v1',
                    'credential_ref': 'secret://offline-test-only',
                    'quota_scope_id': 'offline-native-recovery',
                    'max_concurrency': 1, 'target_utilization': 0.8, 'reserved_slots': 0,
                    'capabilities': ['text', 'structured_output', 'tool_calling'],
                }]}}, 'agents': [],
            },
        )


class FixedModelTransport:
    """Never uses an API; any repeated model call fails the test immediately."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str,
    ) -> ModelResponse:
        assert not self.requests, 'cold recovery repeated the model transport'
        assert deployment.logical_model == 'offline' and api_key == 'sk-live'
        self.requests.append(request)
        return ModelResponse(text=batch({'README.md': 'Native cold recovery delivery.\n'}),
            usage=TokenUsage(prompt_tokens=100, completion_tokens=80, total_tokens=180))


@pytest.mark.skipif(
    shutil.which('node') is None or shutil.which('npm.cmd' if sys.platform == 'win32' else 'npm') is None,
    reason='real offline Node/npm executables are required',
)
async def test_native_write_verify_bundle_approvals_restore_across_four_fresh_stacks(
    receipt_store: Store, tmp_path: Path,
) -> None:
    store = receipt_store
    # Start from the real PG fixture, not the actor-less assembly TaskContext helper.
    async with store.database.session_factory() as session, session.begin():
        row = await session.get(RunRow, store.run)
        assert row is not None
        row.routing_decision = {**(row.routing_decision or {}),
            'project_scale': 'small', 'project_delivery': 'workspace',
            'artifact_strategy': 'workspace_bundle', 'direct_model': 'offline',
            'sandbox_profile': 'workspace_write', 'execution_backend': 'systemd',
            'permission_mode': 'default', 'runtime_timeout_seconds': 120,
            'requested_permissions': ['workspace.read', 'workspace.write', 'command.run', 'network.read']}

    root = tmp_path / 'projects'
    source = root / str(store.tenant) / 'projects/native-receipt/sessions/same-task'
    source.mkdir(parents=True)
    (source / 'package.json').write_text(json.dumps({
        'name': 'native-receipt', 'version': '1.0.0',
        'scripts': {'build': 'node build.cjs', 'test': 'node main.cjs'},
    }), encoding='utf-8')
    (source / 'build.cjs').write_text("console.log('BUILD_PASS');", encoding='utf-8')
    (source / 'main.cjs').write_text(
        "require('node:assert/strict').equal(2+3, 5); console.log('BUSINESS_PASS');", encoding='utf-8')

    model = FixedModelTransport()
    executors: list[NodeFixtureExecutor] = []
    approval_ids: list[str] = []
    lease_tokens: set[UUID] = set()
    readme_stamp: tuple[int, int] | None = None
    expected_tools = ('workspace.write_text', 'project.verify', 'workspace.bundle')

    async def capacity(tenant_id: UUID, deployments: tuple[Deployment, ...]) -> CapacityController:
        assert tenant_id == store.tenant
        return ImmediateCapacity(deployments)

    async def approval_mode(tenant_id: UUID) -> str:
        assert tenant_id == store.tenant
        return 'ask'

    for stage in range(4):
        # Dispose every pool, factory, gateway, ApprovalService and verifier before
        # the next worker. Only PG/files and the model call counter survive.
        database = build_database(store.url)
        try:
            runs = RunRepository(database.session_factory)
            if stage:
                async with database.session_factory() as session, session.begin():
                    claimed = await runs.claim_for_execution(session, store.run,
                        allow_running_recovery=False, worker_id=f'native-action-{stage}',
                        worker_lease_token=uuid4(),
                        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5))
                    assert isinstance(claimed, tuple), 'approved run was not claimable'
            record = await runs.get(store.tenant, store.run)
            assert record.status is RunStatus.RUNNING and record.mode is TaskMode.DIRECT
            assert record.actor_role == Role.OPERATOR.value and record.worker_id is not None
            assert record.worker_lease_token is not None and record.worker_lease_token not in lease_tokens
            lease_tokens.add(record.worker_lease_token)
            assert record.routing_decision is not None
            async with database.session_factory() as session:
                saved = await runs.latest_checkpoint(session, tenant_id=store.tenant, run_id=store.run)
            assert (saved is None) == (stage == 0)
            artifacts = PostgresArtifactRepository(database.session_factory)
            stack = build_runtime_capability_stack(tenant_id=store.tenant, run_repository=runs,
                skill_store_dir=tmp_path/'skills', workspace_root=None,
                project_workspace_dir=root, generated_artifact_dir=tmp_path/'generated',
                artifact_repository=artifacts,
                receipt_repository=ProjectVerificationReceiptRepository(database.session_factory),
                tool_approval_mode=approval_mode)
            verifier = stack.runtime_gateway._project_verification
            assert verifier is not None
            # Only the native execution adapter is substituted. Policy, Root,
            # receipt coordination and source fences remain production code.
            executor = NodeFixtureExecutor(root, tmp_path/'executions')
            executors.append(executor)
            verifier.executor = executor
            factory = ConfigBackedDirectRuntime(
                config_service=cast(ConfigService, FixedPublishedConfig(store.tenant)),
                secret_service=cast(SecretService, FakeSecretService()),
                capacity_factory=capacity, transport=model,
                capability_gateway=stack.runtime_gateway,
                harness_tool_gateway=stack.harness_tool_gateway, artifact_repository=artifacts,
                action_capability_gateway=stack.runtime_gateway,
                action_harness_tool_gateway=stack.harness_tool_gateway)
            context = TaskContext(tenant_id=store.tenant, run_id=store.run,
                actor_id=record.actor_id, actor_role=record.actor_role,
                execution_token=record.worker_lease_token, mode=record.mode, request=record.request,
                timeout_seconds=120, token_budget=50_000,
                routing_decision=_freeze_object(record.routing_decision, name='native action scope'),
                checkpoint=saved, artifacts=tuple(Artifact.from_payload(value)
                    for value in await runs.raw_artifacts(store.tenant, store.run)))
            if saved is not None:
                await factory.restore_checkpoint(saved)
            events: list[RunEvent] = []
            async with worker_runtime_events(factory, context, worker_id=record.worker_id) as stream:
                async for event in stream:
                    events.append(event)
                    # Drive the real persistence API with DB-global sequencing;
                    # policy may also append approval events during this stream.
                    async with database.session_factory() as session, session.begin():
                        sequence = await runs.next_event_sequence(session, store.run)
                        await runs.persist_event(session, tenant_id=store.tenant, run_id=store.run,
                            event=event.model_copy(update={'sequence': sequence}))

            assert len(model.requests) == 1
            assert len(executor.invocations) == (1 if stage == 2 else 0)
            if stage >= 1:
                readme = source / 'README.md'
                assert readme.read_text(encoding='utf-8') == 'Native cold recovery delivery.\n'
                stat = readme.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if readme_stamp is not None:
                    assert stamp == readme_stamp, 'historical workspace write was repeated'
                readme_stamp = stamp
            if stage == 3:
                assert events[-1].kind is EventKind.RUNTIME_COMPLETED
                break

            assert events[-1].kind is EventKind.CHECKPOINT_SAVED, [
                (event.kind, event.tool_name, event.reason) for event in events]
            checkpoint = events[-1].checkpoint
            assert checkpoint is not None and action_checkpoint_valid(checkpoint)
            assert checkpoint.state['tool_name'] == expected_tools[stage]
            waiting = await runs.get(store.tenant, store.run)
            assert waiting.status is RunStatus.WAITING_APPROVAL
            assert waiting.worker_lease_token is None and waiting.routing_decision is not None
            approval_id = waiting.routing_decision['approval_id']
            assert isinstance(approval_id, str) and approval_id == checkpoint.state['approval_id']
            assert approval_id not in approval_ids
            approval_ids.append(approval_id)
            async with database.session_factory() as session:
                persisted = await runs.latest_checkpoint(session, tenant_id=store.tenant, run_id=store.run)
                assert persisted is not None and persisted.to_payload() == checkpoint.to_payload()
            # Private segment hydration must work through PG, not process memory.
            reference = checkpoint.state['reference']
            assert isinstance(reference, Mapping)
            segments = reference['segments']
            assert isinstance(segments, tuple)
            references: list[ArtifactReference] = []
            for segment in segments:
                assert isinstance(segment, Mapping)
                segment_id, digest = segment['id'], segment['sha256']
                assert isinstance(segment_id, str) and isinstance(digest, str)
                references.append(ArtifactReference(id=UUID(segment_id), sha256=digest))
            assert len(await artifacts.get_many(store.tenant, store.run, tuple(references))) == len(references)
            # Approve only this newly created run's exact pending request. No
            # preseeded grants, permissive policy double or changes to old rows.
            approved = await runs.approve_capability_and_enqueue(tenant_id=store.tenant,
                run_id=store.run, actor_id=record.actor_id, approval_id=approval_id,
                version=waiting.version)
            assert approved.status is RunStatus.QUEUED
        finally:
            await database.dispose()

    assert len(lease_tokens) == len(executors) == 4 and len(approval_ids) == 3
    assert sum(len(executor.invocations) for executor in executors) == 1
    report = executors[2].reports[0]
    assert report['status'] == 'passed'
    assert [check['phase'] for check in report['checks']] == ['install', 'build', 'test']
    assert [check['status'] for check in report['checks']] == ['passed'] * 3
    assert 'BUILD_PASS' in report['checks'][1]['stdout']
    assert 'BUSINESS_PASS' in report['checks'][2]['stdout']
    bundles = list((tmp_path/'generated').rglob('*.zip'))
    assert len(bundles) == 1
    with ZipFile(bundles[0]) as archive:
        assert archive.read('README.md').decode('utf-8') == 'Native cold recovery delivery.\n'
        assert archive.read('main.cjs').decode('utf-8') == (source/'main.cjs').read_text(encoding='utf-8')
    async with store.database.session_factory() as session:
        approvals = (await session.scalars(select(RunApprovalRow).where(
            RunApprovalRow.tenant_id == store.tenant, RunApprovalRow.run_id == store.run))).all()
        assert {row.approval_id for row in approvals} == set(approval_ids)
        assert all(row.status == 'approved' for row in approvals)
        checkpoints = (await session.scalars(select(RunCheckpointRow).where(
            RunCheckpointRow.tenant_id == store.tenant, RunCheckpointRow.run_id == store.run))).all()
        assert len(checkpoints) == 4
        pending_checkpoints = [row for row in checkpoints if row.runtime_type == 'direct_action']
        assert len(pending_checkpoints) == 3
        assert all(row.runtime_version == '2' and row.state['completed'] is False
                   for row in pending_checkpoints)
        assert {row.state['approval_id'] for row in pending_checkpoints} == set(approval_ids)
        completed_checkpoints = [row for row in checkpoints if row.runtime_type == 'direct']
        assert len(completed_checkpoints) == 1 and completed_checkpoints[0].state['completed'] is True
