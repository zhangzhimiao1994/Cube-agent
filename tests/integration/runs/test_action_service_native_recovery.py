"""Real PG/RunService cold driver proof; no paid model or end-user acceptance.

Real: submit/execute/approve, factory, worker binding, policy, private artifacts,
generated contracts, receipts and offline Node subprocesses. Doubles: model
configuration/credentials/capacity/queue transport and the native report adapter.
NodeFixtureExecutor is not the production Root sandbox; run this fixture only in
the isolated native runner. Actual user Root install/build/test remains required.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import delete, select

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.project_acceptance import parse_acceptance_manifest
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.config.service import ConfigService
from agent_hub.db.models import RunApprovalRow, RunCheckpointRow, RunRow
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import CapacityController
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.project_verification_receipts import (
    RECEIPT_KEY,
    ProjectVerificationReceiptRepository,
    VerificationRecord,
)
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
from agent_hub.runtime.contracts import Artifact, EventKind
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.security.secrets import SecretService
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import (
    ContractProjectInvocation,
    ProjectInvocation,
    project_source_fingerprint,
    project_source_manifest,
)
from agent_hub.skills.sandbox.project_acceptance_node import (
    node_acceptance_command,
    validate_node_acceptance_output,
)
from tests.integration.runs.test_action_direct_native_recovery import FixedPublishedConfig
from tests.unit.runtime.test_configured_runtime import FakeSecretService, ImmediateCapacity
from tests.unit.runtime.test_direct_project_verification import NodeFixtureExecutor, batch

_FIXED = 'exports.add = (a, b) => a + b;\n'
_PROJECT = 'service-native'
_SESSION = 'fresh-driver'


class RecordingQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        # The test calls the real driver explicitly; never dispatch an external worker.
        assert isinstance(run_id, UUID) and idempotency_key


class FixedModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str,
    ) -> ModelResponse:
        assert not self.requests, 'fresh RunService repeated the model'
        assert deployment.logical_model == 'offline' and api_key == 'sk-live'
        self.requests.append(request)
        return ModelResponse(text=batch({'src/index.js': _FIXED}),
            usage=TokenUsage(prompt_tokens=100, completion_tokens=80, total_tokens=180))


class GeneratedNodeFixtureExecutor(NodeFixtureExecutor):
    """Adapt real fixture subprocess output to the generated contract report.

    This is explicitly a test transport, not proof of production sandboxing.
    Contract identity comes from the real private baseline; named successes come
    only from the trusted reporter command's actual subprocess stdout.
    """

    def __init__(self, root: Path, execution_root: Path) -> None:
        super().__init__(root, execution_root)
        self.phase = 'not_called'
        self.error_type: str | None = None

    def diagnostic(self) -> dict[str, object]:
        """Bounded metadata only: never source, subprocess output or exception text."""
        report = self.reports[-1] if self.reports else {}
        runtime = report.get('runtime', {})
        return {'phase': self.phase, 'error_type': self.error_type,
            'invocations': len(self.invocations), 'reports': len(self.reports),
            'runtime': {name: version for name in ('node', 'npm')
                if isinstance(runtime, dict) and isinstance(version := runtime.get(name), str)
                and re.fullmatch(r'v?[0-9][A-Za-z0-9.+-]{0,63}', version)},
            'checks': [{'phase': check.get('phase'), 'status': check.get('status'),
                        'exit_code': check.get('exit_code'), 'timed_out': check.get('timed_out')}
                for check in report.get('checks', [])[:3]]}

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        try:
            return await self._run_generated(invocation)
        except Exception as error:
            # Production deliberately sanitizes executor failures. Preserve only
            # class/stage in this test so the server proof can locate its fixture delta.
            self.error_type = type(error).__name__
            raise

    async def _run_generated(self, invocation: ProjectInvocation) -> SkillResult:
        self.phase = 'validate_invocation'
        assert isinstance(invocation, ContractProjectInvocation)
        assert invocation.source_kind == 'candidate' and invocation.install_allowed
        base = ProjectInvocation.model_validate({
            name: getattr(invocation, name) for name in ProjectInvocation.model_fields
        }, strict=True)
        reference = ProjectBaselineReference.from_payload(invocation.original_reference.model_dump())
        self.phase = 'load_private_baseline'
        stored = await asyncio.to_thread(ProjectBaselineStore(self.root).load, base, reference)
        assert stored.generated_metadata is not None
        self.phase = 'validate_current_contract'
        stored.contract.require_current(await project_source_manifest(self.root, base))
        manifest = parse_acceptance_manifest((stored.snapshot/'ACCEPTANCE.json').read_bytes())
        self.phase = 'native_install_build_test'
        result = await super().run(base)
        report = self.reports[-1]
        check = report['checks'][-1]
        self.phase = 'decode_named_node_tests'
        proof = validate_node_acceptance_output(check['stdout'], manifest.test_ids,
            check['exit_code'], check['timed_out'], False)
        report['generated_test_execution'] = {
            **proof, 'manifest_sha256': stored.generated_metadata['manifest_sha256'],
        }
        report['original_check_contract'] = {
            'schema_version': 1, 'contract_sha256': stored.contract.protected_sha256,
            'protected_sha256': stored.contract.protected_sha256,
        }
        self.phase = 'validate_post_execution_source'
        stored.contract.require_current(await project_source_manifest(self.root, base))
        assert report['source_sha256'] == await project_source_fingerprint(self.root, base)
        self.phase = 'report_ready'
        return result.model_copy(update={'stdout': json.dumps(report)})


def seed_node_fixture(source: Path) -> None:
    (source/'src').mkdir(parents=True)
    (source/'test').mkdir()
    (source/'src/index.js').write_text('exports.add = () => 0;\n', encoding='utf-8')
    (source/'package.json').write_text(json.dumps({
        'name': 'service-native', 'version': '1.0.0',
        'scripts': {'build': 'node build.cjs', 'test': 'node main.cjs'},
    }), encoding='utf-8')
    (source/'ACCEPTANCE.json').write_text(json.dumps({
        'schema_version': 1, 'stack': 'node', 'editable_paths': ['src'],
        'test_files': ['test/business.test.cjs'], 'criteria': [{
            'id': 'AC-001', 'description': 'Adds two numbers', 'tests': ['adds two numbers'],
        }],
    }), encoding='utf-8')
    (source/'test/business.test.cjs').write_text(
        "const assert = require('node:assert/strict');\n"
        "require('node:test').test('adds two numbers', () => {\n"
        "  assert.equal(require('../src/index.js').add(2, 3), 5);\n"
        "});\n", encoding='utf-8')
    (source/'build.cjs').write_text(
        "require('node:assert/strict').equal(typeof require('./src/index.js').add, 'function');\n"
        "console.log('BUILD_PASS');\n", encoding='utf-8')
    command = node_acceptance_command(('test/business.test.cjs',), ('adds two numbers',))
    (source/'main.cjs').write_text(
        "const {spawnSync} = require('node:child_process');\n"
        f"const r = spawnSync(process.execPath, {json.dumps(command[1:])}, {{encoding:'utf8'}});\n"
        "process.stdout.write(r.stdout || ''); process.stderr.write(r.stderr || '');\n"
        "process.exit(r.status === null ? 1 : r.status);\n", encoding='utf-8')


@pytest.mark.skipif(
    shutil.which('node') is None or shutil.which('npm.cmd' if sys.platform == 'win32' else 'npm') is None,
    reason='real offline Node/npm executables are required',
)
async def test_real_run_service_restores_published_inspection_across_fresh_workers(
    database_url: str, tmp_path: Path,
) -> None:
    tenant, actor = uuid4(), uuid4()
    run_id: UUID | None = None
    root = tmp_path/'projects'
    source = root/str(tenant)/'projects'/_PROJECT/'sessions'/_SESSION
    seed_node_fixture(source)
    model = FixedModel()
    executors: list[GeneratedNodeFixtureExecutor] = []
    approvals: list[str] = []
    inspection: dict[str, object] | None = None
    stamp: tuple[int, int] | None = None

    async def capacity(tenant_id: UUID, deployments: tuple[Deployment, ...]) -> CapacityController:
        assert tenant_id == tenant
        return ImmediateCapacity(deployments)

    async def approval_mode(tenant_id: UUID) -> str:
        assert tenant_id == tenant
        return 'ask'

    try:
        for stage in range(4):
            database = build_database(database_url)
            try:
                assert database.engine.url.get_backend_name() == 'postgresql'
                runs = RunRepository(database.session_factory)
                artifacts = PostgresArtifactRepository(database.session_factory)
                stack = build_runtime_capability_stack(tenant_id=tenant, run_repository=runs,
                    skill_store_dir=tmp_path/'skills', workspace_root=None,
                    project_workspace_dir=root, generated_artifact_dir=tmp_path/'generated',
                    artifact_repository=artifacts,
                    receipt_repository=ProjectVerificationReceiptRepository(database.session_factory),
                    tool_approval_mode=approval_mode)
                verifier = stack.runtime_gateway._project_verification
                assert verifier is not None
                executor = GeneratedNodeFixtureExecutor(root, tmp_path/'executions')
                executors.append(executor)
                verifier.executor = executor
                factory = ConfigBackedDirectRuntime(
                    config_service=cast(ConfigService, FixedPublishedConfig(tenant)),
                    secret_service=cast(SecretService, FakeSecretService()),
                    capacity_factory=capacity, transport=model,
                    capability_gateway=stack.runtime_gateway,
                    harness_tool_gateway=stack.harness_tool_gateway, artifact_repository=artifacts,
                    action_capability_gateway=stack.runtime_gateway,
                    action_harness_tool_gateway=stack.harness_tool_gateway)
                service = RunService(runs, runtime_registry=RuntimeRegistry((factory,)),
                    router=None, task_queue=RecordingQueue(), worker_id=f'service-native-{stage}',
                    runtime_timeout_seconds=120, runtime_token_budget=50_000)
                if stage == 0:
                    submitted = await service.submit(tenant_id=tenant, actor_id=actor,
                        actor_role=Role.OPERATOR, mode=TaskMode.DIRECT,
                        message='Build a small Node.js software project that adds two numbers. '
                                'Implement src/index.js, run the tests and deliver a project ZIP.',
                        allow_scale_mode_upgrade=False, project_id=_PROJECT,
                        workspace_session_id=_SESSION, sandbox_profile='workspace_write',
                        execution_backend='systemd', permission_mode='default',
                        requested_permissions=('workspace.read', 'workspace.write', 'command.run'),
                        direct_model='offline', runtime_timeout_seconds=120)
                    run_id = submitted.id
                assert run_id is not None
                before = await runs.get(tenant, run_id, actor_id=actor)
                assert before.status is RunStatus.QUEUED and before.mode is TaskMode.DIRECT
                assert before.routing_decision is not None
                assert before.routing_decision['generated_project_contract_version'] == 1
                assert before.routing_decision['project_delivery'] == 'workspace'
                permissions = before.routing_decision['requested_permissions']
                assert isinstance(permissions, list) and 'network.read' not in permissions
                # No manual claim, restore, TaskContext, artifacts or worker binding.
                await service.execute(run_id)
                record = await runs.get(tenant, run_id, actor_id=actor)
                events = await runs.raw_events(tenant, run_id)
                assert len(model.requests) == 1
                assert len(executor.invocations) == (1 if stage == 2 else 0), executor.diagnostic()
                published = [event.artifact for event in events
                    if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
                    and event.artifact.content.get('tool_name') == 'project.inspect']
                assert len(published) == 1
                current = published[0].to_payload()
                if inspection is not None:
                    assert current == inspection, 'driver replaced the published inspection'
                inspection = current
                if stage:
                    assert (source/'src/index.js').read_text(encoding='utf-8') == _FIXED
                    stat = (source/'src/index.js').stat()
                    current_stamp = (stat.st_mtime_ns, stat.st_size)
                    if stamp is not None:
                        assert current_stamp == stamp, 'cold driver repeated the write'
                    stamp = current_stamp
                async with database.session_factory() as session:
                    checkpoint = await runs.latest_checkpoint(session, tenant_id=tenant, run_id=run_id)
                assert checkpoint is not None
                if stage == 3:
                    if record.status is not RunStatus.COMPLETED:
                        pytest.fail(json.dumps({'stage': stage, 'native': executor.diagnostic()},
                                               sort_keys=True), pytrace=False)
                    assert checkpoint.runtime_type == 'direct' and checkpoint.state['completed'] is True
                    assert record.routing_decision is not None
                    receipt = record.routing_decision['project_verification_receipt']
                    assert isinstance(receipt, dict) and receipt['status'] == 'passed'
                else:
                    if record.status is not RunStatus.WAITING_APPROVAL:
                        pytest.fail(json.dumps({'stage': stage, 'native': executor.diagnostic()},
                                               sort_keys=True), pytrace=False)
                    assert action_checkpoint_valid(checkpoint)
                    assert checkpoint.state['tool_name'] == (
                        'workspace.write_text', 'project.verify', 'workspace.bundle')[stage]
                    approval_id = checkpoint.state['approval_id']
                    assert isinstance(approval_id, str) and approval_id not in approvals
                    assert record.routing_decision is not None
                    assert record.routing_decision['approval_id'] == approval_id
                    assert record.worker_lease_token is None
                    approvals.append(approval_id)
                    approved = await service.approve_capability(tenant_id=tenant, actor_id=actor,
                        run_id=run_id, approval_id=approval_id, version=record.version)
                    assert approved.status is RunStatus.QUEUED
            finally:
                await database.dispose()

        assert run_id is not None
        assert checkpoint is not None
        assert len(executors) == 4 and len(approvals) == 3
        assert sum(len(executor.invocations) for executor in executors) == 1
        report = executors[2].reports[0]
        assert report['status'] == 'passed'
        assert [check['status'] for check in report['checks']] == ['passed'] * 3
        assert report['generated_test_execution']['names'] == ('adds two numbers',)
        bundles = list((tmp_path/'generated').rglob('*.zip'))
        assert len(bundles) == 1
        with ZipFile(bundles[0]) as archive:
            assert archive.testzip() is None
            assert archive.read('src/index.js').decode('utf-8') == _FIXED
            assert archive.read('ACCEPTANCE.json') == (source/'ACCEPTANCE.json').read_bytes()
        database = build_database(database_url)
        try:
            async with database.session_factory() as session:
                grants = (await session.scalars(select(RunApprovalRow).where(
                    RunApprovalRow.tenant_id == tenant, RunApprovalRow.run_id == run_id))).all()
                assert {row.approval_id for row in grants} == set(approvals)
                assert all(row.status == 'approved' for row in grants)
                saved = (await session.scalars(select(RunCheckpointRow).where(
                    RunCheckpointRow.tenant_id == tenant, RunCheckpointRow.run_id == run_id))).all()
                pending = [row for row in saved if row.runtime_type == 'direct_action']
                assert len(pending) == 3 and all(row.runtime_version == '2' for row in pending)
                assert {row.state['approval_id'] for row in pending} == set(approvals)
                assert len([row for row in saved if row.runtime_type == 'direct'
                            and row.state.get('completed') is True]) == 1
                all_artifacts = [Artifact.from_payload(value)
                    for value in await runs.raw_artifacts(tenant, run_id)]
                # Successful Direct publishes a delivery envelope, not the
                # tool_name/result envelope used for inspection/failure feedback.
                deliveries = [artifact for artifact in all_artifacts
                    if artifact.type == 'tool_result'
                    and isinstance(artifact.content.get('workspace_delivery'), Mapping)]
                assert len(deliveries) == 1
                delivered = deliveries[0]
                assert delivered.producer == 'main' and delivered.version == 1
                assert str(delivered.id) == checkpoint.state['artifact_id']
                assert delivered.content_sha256 == checkpoint.state['artifact_sha256']
                publications = [event.artifact for event in events
                    if event.kind is EventKind.ARTIFACT_CREATED and event.artifact is not None
                    and event.artifact.id == delivered.id]
                assert len(publications) == 1
                assert publications[0].to_payload() == delivered.to_payload()
                delivery = delivered.content['workspace_delivery']
                assert isinstance(delivery, Mapping)

                # Read the exact private reference issued by the real verifier.
                # This is post-run auditing, not a new live-worker authority.
                assert record.routing_decision is not None
                issued = VerificationRecord.from_payload(record.routing_decision[RECEIPT_KEY])
                assert issued.status == 'passed' and len(issued.artifact_references) == 1
                assert (issued.authority.tenant_id, issued.authority.run_id,
                        issued.authority.actor_id, issued.authority.actor) == (
                            tenant, run_id, actor, 'main_agent')
                assert issued.source_sha256 == report['source_sha256']
                private = await PostgresArtifactRepository(database.session_factory).get_many(
                    tenant, run_id, issued.artifact_references)
                assert len(private) == 1
                receipt_artifact = private[0]
                assert (receipt_artifact.type, receipt_artifact.producer, receipt_artifact.version) == (
                    'project_verification_receipt', 'trusted_project_verifier', 1)
                assert receipt_artifact.id == issued.artifact_references[0].id
                assert receipt_artifact.content_sha256 == issued.artifact_references[0].sha256
                body = receipt_artifact.content
                assert body['kind'] == 'node_generated' and body['schema_version'] == 1
                assert body['scope'] == issued.authority.stable_scope()
                assert (body['generation'], body['action_key'], body['execution_id'],
                        body['source_sha256']) == (
                            issued.generation, issued.action_key, issued.execution_id, issued.source_sha256)
                result = body['result']
                assert isinstance(result, Mapping)
                verification = result['verification']
                assert isinstance(verification, Mapping)
                assert verification['status'] == 'passed'
                assert verification['source_sha256'] == issued.source_sha256
                assert delivery['verification'] == verification
                assert delivery['generated_project_acceptance'] == result['generated_project_acceptance']
        finally:
            await database.dispose()
    finally:
        if run_id is not None:
            database = build_database(database_url)
            try:
                async with database.session_factory() as session, session.begin():
                    await session.execute(delete(RunRow).where(
                        RunRow.tenant_id == tenant, RunRow.id == run_id))
            finally:
                await database.dispose()
