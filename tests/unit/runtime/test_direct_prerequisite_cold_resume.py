"""Real Direct/gateway/durable verification; SQL, model and executor transport are doubles."""
from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from agent_hub.runtime import direct
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.adaptive_budget import deadline_from_routing
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.worker_execution import worker_runtime_events
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_project_prerequisites import raw_report
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import project_path
from tests.unit.capabilities.test_verification_cold_recovery import Fixture
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map
from tests.unit.runtime.direct_catalog_diagnostic_typed_fixture import Policy as ApprovalPolicy
from tests.unit.runtime.test_direct_project_verification import Model, batch, diagnostic_read_batch

INVALID_MANIFEST = '{"scripts":{"build":"node -v","test":'
REPAIRED_MANIFEST = '{"scripts":{"build":"node -v","test":"node --test"}}\n'
README = 'Preserve this implementation while repairing only its package manifest.\n'


class ManifestExecutor(Executor):
    """Decode the actual written manifest and bind reports to the actual source tree."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.reports: list[dict[str, Any]] = []

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        source = await project_source_fingerprint(self.root, invocation)
        manifest = self.root / project_path() / 'package.json'
        try:
            parsed = json.loads(manifest.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            self.calls.append(invocation)
            report = raw_report(reason='invalid_package_manifest')
            report['source_sha256'] = source
            self.reports.append(report)
            return SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(report), stderr='')
        assert parsed == json.loads(REPAIRED_MANIFEST)
        result = await super().run(invocation)
        report = json.loads(result.stdout)
        assert report['source_sha256'] == source
        self.reports.append(report)
        return result


@pytest.mark.asyncio
async def test_source_bound_manifest_repair_survives_read_approval_on_a_cold_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.executor = executor = ManifestExecutor(fixture.root)
    fixture.db.row.routing_decision.update({
        'project_scale': 'small', 'project_delivery': 'workspace',
        'artifact_strategy': 'workspace_bundle', 'engineering_prerequisite_repair_version': 1,
        'engineering_diagnostic_reads_version': 1,
        'runtime_timeout_source': 'project_scale_soft_budget',
        'runtime_timeout_soft_seconds': 60, 'runtime_timeout_absolute_seconds': 120,
        'runtime_token_soft_base_tokens': 50_000, 'runtime_token_absolute_tokens': 100_000,
        'critical_path_complexity_units': 20,
    })
    assert not {'project_input_contract_version', 'generated_project_contract_version',
                'python_project_history'} & fixture.db.row.routing_decision.keys()
    authority_keys = ('requested_permissions', 'permission_mode', 'sandbox_profile',
                      'execution_backend')
    authority = deepcopy({key: fixture.db.row.routing_decision.get(key) for key in authority_keys})
    initial_context = fixture.context()
    policy = ApprovalPolicy()
    policy.allowed.update({'write', 'run', 'create'})
    executions: list[tuple[str, str, Mapping[str, Any]]] = []
    requests: list[HarnessToolCallRequest] = []
    results: list[HarnessToolCallResult] = []
    identities: list[tuple[Any, ...]] = []
    published: dict[UUID, Artifact] = {}
    runtimes: list[ActionApprovedDirectRuntime] = []
    backends: list[RuntimeCapabilityGateway] = []
    deadlines: list[Any] = []
    token_observations: list[tuple[int, int, int]] = []
    read_windows: list[tuple[Any, ...]] = []
    create_deadline = deadline_from_routing
    token_limit = direct._workspace_delivery_token_limit

    def capture_deadline(*args: Any, **kwargs: Any) -> Any:
        value = create_deadline(*args, **kwargs)
        deadlines.append(value)
        return value

    def capture_token_limit(*args: Any, **kwargs: Any) -> int:
        value = token_limit(*args, **kwargs)
        token_observations.append((kwargs['completed_files'], kwargs['completed_batches'], value))
        return value

    def window() -> tuple[Any, ...]:
        budget = deadlines[-1]
        return (budget.deadline, budget.absolute_deadline, budget.progress_units,
                token_observations[-1], len(token_observations))

    monkeypatch.setattr(direct, 'deadline_from_routing', capture_deadline)
    monkeypatch.setattr(direct, '_workspace_delivery_token_limit', capture_token_limit)

    class RepairModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 2:
                assert window() == read_windows[-1], 'read must not earn budget or a completed batch'
                assert len(executor.calls) == 1, 'cold replay must not invoke the old verification'
            return await super().complete_with_context(request)

    class RecordingBackend(RuntimeCapabilityGateway):
        async def execute(self, *, name: str, idempotency_key: str, **kwargs: Any) -> Any:
            executions.append((name, idempotency_key, dict(kwargs['arguments'])))
            return await super().execute(name=name, idempotency_key=idempotency_key, **kwargs)

    class RecordingShared(HarnessToolGateway):
        async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest,
                         **kwargs: Any) -> HarnessToolCallResult:
            identity = (tenant_id, request.run_id, request.actor,
                        kwargs.get('user_id'), kwargs.get('role'), request.sandbox)
            assert identity == (initial_context.tenant_id, initial_context.run_id, 'main_agent',
                                initial_context.actor_id, initial_context.actor_role,
                                authority['sandbox_profile'])
            identities.append(identity)
            requests.append(request)
            if request.tool_name == 'read_context':
                read_windows.append(window())
            result = await super().invoke(tenant_id, request, **kwargs)
            results.append(result)
            return result

    model = RepairModel(
        batch({'package.json': INVALID_MANIFEST, 'README.md': README}),
        diagnostic_read_batch(path='package.json'),
        batch({'package.json': REPAIRED_MANIFEST}),
    )

    async def step(checkpoint: RuntimeCheckpoint | None) -> list[RunEvent]:
        backend = RecordingBackend(
            skill_store_dir=tmp_path / 'skills', project_workspace_dir=fixture.root,
            generated_artifact_dir=tmp_path / 'generated', run_repository=fixture.runs,
            project_executor=executor, receipt_repository=fixture.db.repository(),
            artifact_repository=fixture.artifacts,
        )
        runtime = ActionApprovedDirectRuntime(
            model, logical_model='main', capability_gateway=backend,
            artifact_repository=fixture.artifacts, harness_tool_gateway=RecordingShared(
                backend, policy_gateway=policy, require_actor_identity=True,
            ),
        )
        runtimes.append(runtime)
        backends.append(backend)
        ctx = fixture.context().model_copy(update={
            'checkpoint': checkpoint, 'token_budget': 50_000,
            'artifacts': tuple(published.values()),
        })
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        async with worker_runtime_events(runtime, ctx, worker_id=fixture.db.row.worker_id) as stream:
            events = [event async for event in stream]
        for event in events:
            if event.artifact is not None:
                published[event.artifact.id] = event.artifact
        return events

    waiting = await step(None)
    checkpoint = waiting[-1].checkpoint
    assert checkpoint is not None and checkpoint.runtime_type == 'direct_action'
    assert checkpoint.state['tool_name'] == 'read_context'
    assert checkpoint.state['approval_id'] == 'approval_read'
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in waiting)
    assert len(model.requests) == 2 and len(executor.calls) == 1
    assert executor.reports[0]['reason'] == 'invalid_package_manifest'
    assert all(check['status'] == 'not_run' for check in executor.reports[0]['checks'])
    failed = deepcopy(fixture.db.row.routing_decision[RECEIPT_KEY])
    assert failed['status'] == 'failed' and failed['generation'] == 1
    assert failed['source_sha256'] == executor.reports[0]['source_sha256']
    assert results[-1].tool_name == 'read_context' and results[-1].status == 'failed'
    assert results[-1].failure_reason == 'capability requires approval'
    blocked = next(result for result in results if result.tool_name == 'project.verify')
    assert blocked.status == 'failed'
    assert read_map(blocked.payload['verification_blocked'])['source_sha256'] == failed['source_sha256']
    assert 'execution_diagnostic' not in blocked.payload and 'verification' not in blocked.payload
    assert [name for name, _, _ in executions] == [
        'workspace.write_text', 'workspace.write_text', 'workspace.write_text', 'project.verify',
    ]
    assert executions[-1][1] == failed['action_key']
    assert window() == read_windows[-1]
    assert not list((tmp_path / 'generated').rglob('*.zip'))
    before_executions, before_models = list(executions), list(model.requests)
    pending_read = requests[-1]
    policy.allowed.add('read')
    fixture.db.new_lease()

    completed = await step(checkpoint)
    assert completed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert runtimes[0] is not runtimes[1] and backends[0] is not backends[1]
    assert model.requests[:2] == before_models and len(model.requests) == 3
    assert len(executor.calls) == 2
    assert executions[:len(before_executions)] == before_executions
    assert [name for name, _, _ in executions[len(before_executions):]] == [
        'read_context', 'workspace.write_text', 'workspace.write_text', 'project.verify',
        'workspace.bundle',
    ]
    keys = [(name, key) for name, key, _ in executions]
    assert len(keys) == len(set(keys)), 'no completed action may reach the backend twice'
    read_requests = [request for request in requests if request.tool_name == 'read_context']
    assert len(read_requests) == 2 and read_requests[0].to_payload() == read_requests[1].to_payload()
    assert read_requests[1].idempotency_key == pending_read.idempotency_key
    read = next(result for result in results
                if result.tool_name == 'read_context' and result.status == 'succeeded')
    assert read.payload == {'path': 'package.json', 'text': INVALID_MANIFEST, 'truncated': False}
    assert len(read_windows) == len(deadlines) == 2
    assert all(runtime._active_capability_task is None for runtime in runtimes)
    assert authority == {key: fixture.db.row.routing_decision.get(key) for key in authority_keys}
    assert len(set(identities)) == 1
    issued = fixture.db.row.routing_decision[RECEIPT_KEY]
    assert issued['status'] == 'passed' and issued['generation'] == 2
    assert issued['source_sha256'] == executor.reports[1]['source_sha256']
    assert issued['source_sha256'] != failed['source_sha256']
    assert issued['action_key'] != failed['action_key'] and issued['artifact_references']
    feedback = '\n'.join(cast(str, message.content) for message in model.requests[-1].messages)
    assert 'UNTRUSTED_WORKSPACE_READ_JSON' in feedback and 'invalid_package_manifest' in feedback
    read_payload = feedback.split('<UNTRUSTED_WORKSPACE_READ_JSON>', 1)[1].split(
        '</UNTRUSTED_WORKSPACE_READ_JSON>', 1,
    )[0]
    assert json.loads(read_payload)[0]['text'] == INVALID_MANIFEST
    archives = list((tmp_path / 'generated').rglob('*.zip'))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read('package.json').decode('utf-8') == REPAIRED_MANIFEST
        assert archive.read('README.md').decode('utf-8') == README
