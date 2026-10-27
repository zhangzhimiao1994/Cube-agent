"""Historical delivery is scoped reference data, never new execution authority."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime import direct_delivery_report as report
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    GatewayProvenance,
    RunEvent,
    TaskContext,
)

README = b'# Usage\npython -m demo --help\n'

def _run(coroutine: Any) -> Any:
    """Memory-repository unit calls must finish without scheduling external IO."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError('unit fixture unexpectedly suspended')

class Repository:

    def __init__(self, current: Any, source: Any, events: tuple[RunEvent, ...]) -> None:
        self.rows = {current.id: current, source.id: source}
        self.events = {source.id: events}
        self.raw_reads: list[Any] = []

    async def get(self, tenant: Any, run: Any) -> Any:
        row = self.rows[run]
        if row.tenant_id != tenant:
            raise LookupError('private')
        return row

    async def raw_events(self, tenant: Any, run: Any) -> tuple[RunEvent, ...]:
        self.raw_reads.append(run)
        return self.events[run]

def fixture(tmp_path: Path, variant: str='normal') -> tuple[Repository, Any, Any, Artifact]:
    tenant, actor = (uuid4(), uuid4())
    route = {'conversation_id': 'same-conversation', 'project_id': 'demo', 'workspace_session_id': 'session-a', 'sandbox_profile': 'read_only', 'requested_permissions': ['workspace.read'], 'engineering_direct_report_version': 1}
    now = datetime.now(UTC)
    common = {'tenant_id': tenant, 'actor_id': actor, 'actor_role': Role.OPERATOR, 'routing_decision': route, 'request': 'Deliver the project.', 'mode': TaskMode.DIRECT, 'worker_id': None, 'worker_lease_token': None, 'worker_lease_expires_at': None, 'worker_heartbeat_at': None}
    current = SimpleNamespace(**common, id=uuid4(), status=RunStatus.RUNNING, created_at=now)
    source = SimpleNamespace(**common, id=uuid4(), status=RunStatus.COMPLETED, created_at=now - timedelta(minutes=1))
    checks = [{'phase': p, 'status': 'passed', 'execution_diagnostic': {'schema_version': 1, 'backend': 'systemd', 'exit_code': 0, 'timed_out': False}} for p in ('install', 'build', 'test')]
    if variant == 'partial_checks':
        checks = checks[1:]
    verification = {'schema_version': 1, 'status': 'passed', 'source_sha256': 'c' * 64, 'execution_id': 'old_execution', 'checks': checks}
    delivery = {'artifact_id': str(uuid4()), 'presentation': 'final_attachment', 'file': {'filename': 'actual-project.zip', 'sha256': 'a' * 64, 'size_bytes': 16139}, 'verification': verification, 'verification_source_sha256': 'c' * 64, 'verification_receipt_sha256': hashlib.sha256(json.dumps(verification, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest(), 'workspace_files': [{'path': 'README.md', 'filename': 'README.md', 'sha256': hashlib.sha256(README).hexdigest(), 'size_bytes': len(README)}], 'python_product': {'filename': 'demo-1.0-py3-none-any.whl', 'sha256': 'b' * 64, 'size': 6879, 'source_sha256': 'c' * 64, 'execution_id': 'old_execution'}, 'python_product_path': 'dist/demo-1.0-py3-none-any.whl'}
    if variant == 'unicode_large':
        delivery['file']['filename'] = '真实项目.zip'
        delivery['workspace_files'].extend({'path': f'模块/文件{i}.py', 'sha256': 'd' * 64, 'size_bytes': 0} for i in range(1))
    context = TaskContext(run_id=source.id, tenant_id=tenant, actor_id=actor, mode=TaskMode.DIRECT, request=source.request, routing_decision=route)
    request = ModelRequest(logical_model='main', messages=(ModelMessage(role='user', content='report'),), max_output_tokens=128, timeout_seconds=1.0)
    completion = GatewayCompletion(response=ModelResponse(text='Prior report.', usage=TokenUsage(1, 1, 2)), logical_model='main', deployment_id='fixture', provider_id='fixture', provider_model='fixture/model', cost_usd=Decimal(0))
    binding = report.binding(context, delivery, request, completion)
    artifact = Artifact(id=uuid4(), type='tool_result', producer='main', content={'workspace_delivery': delivery, 'text': 'Prior report.', 'delivery_report_binding': binding}, provenance=GatewayProvenance(logical_model='main', deployment_id='fixture', provider_id='fixture', provider_model='fixture/model'))
    events = (RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=source.id, artifact=artifact), RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=2, run_id=source.id, payload={'artifact_id': str(artifact.id)}, inputs=(artifact,)))
    workspace = tmp_path / str(tenant) / 'projects/demo/sessions/session-a'
    workspace.mkdir(parents=True)
    (workspace / 'README.md').write_bytes(README)
    return (Repository(current, source, events), current, source, artifact)

async def project(repo: Repository, root: Path, current: Any, ids: tuple[Any, ...], budget: int=8000) -> tuple[Artifact, ...]:
    name = 'agent_hub.runs.delivery_context'
    assert importlib.util.find_spec(name) is not None, 'historical delivery projection is missing'
    module = importlib.import_module(name)
    return await module.delivery_context_artifacts(repo, root, current.tenant_id, current.id, ids, budget)

def view(artifacts: tuple[Artifact, ...]) -> dict[str, Any]:
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == 'text' and artifact.producer == 'historical_delivery_evidence'
    text = str(artifact.content['text'])
    return json.loads(text.split('\n', 1)[1].rsplit('\n', 1)[0])

@pytest.mark.parametrize('variant', ['normal', 'unicode_large', 'partial_checks'])
def test_real_bound_delivery_projects_actual_zip_and_matching_readme(tmp_path: Path, variant: str) -> None:
    repo, current, source, original = fixture(tmp_path, variant)
    artifacts = _run(project(repo, tmp_path, current, (source.id,)))
    value = view(artifacts)
    assert value['archive'] == {'filename': '真实项目.zip' if variant == 'unicode_large' else 'actual-project.zip', 'sha256': 'a' * 64, 'size_bytes': 16139}
    assert value['manifest']['file_count'] == (2 if variant == 'unicode_large' else 1)
    assert [r['phase'] for r in value['verification']['checks']] == (['build', 'test'] if variant == 'partial_checks' else ['install', 'build', 'test'])
    if variant == 'unicode_large':
        module = importlib.import_module('agent_hub.runs.delivery_context')
        large = module._manifest([{'path': f'模块/文件{i}.py', 'sha256': 'd' * 64, 'size_bytes': 0} for i in range(4101)])
        assert large['file_count'] == 4101 and len(large['sample']) <= 8
    assert value['wheel']['sha256'] == 'b' * 64
    assert value['readme']['status'] == 'same_as_delivery'
    assert value['readme']['text'] == README.decode()
    assert value['observation'] == 'historical_delivery_not_current_verification'
    assert artifacts[0].id != original.id and artifacts[0].source_ids == (str(original.id),)
    assert value['source']['run_id'] == str(source.id)
    assert value['scope']['current_run_id'] == str(current.id)
    encoded = json.dumps(value)
    assert 'delivery_sha256' not in encoded and 'verification_receipt_sha256' not in encoded
    assert 'delivery_report_binding' not in encoded and 'Prior report.' not in encoded

@pytest.mark.parametrize('defect', ['actor', 'tenant', 'conversation', 'project', 'session', 'failed', 'lease', 'no-read', 'future', 'no-actor'])
def test_scope_rejected_before_private_event_reads(tmp_path: Path, defect: str) -> None:
    repo, current, source, _ = fixture(tmp_path)
    if defect == 'actor':
        source.actor_id = uuid4()
    elif defect == 'tenant':
        source.tenant_id = uuid4()
    elif defect in {'conversation', 'project', 'session'}:
        key = {'conversation': 'conversation_id', 'project': 'project_id', 'session': 'workspace_session_id'}[defect]
        source.routing_decision = {**source.routing_decision, key: 'foreign'}
    elif defect == 'failed':
        source.status = RunStatus.FAILED
    elif defect == 'lease':
        source.worker_heartbeat_at = datetime.now(UTC)
    elif defect == 'no-read':
        current.routing_decision = {**current.routing_decision, 'requested_permissions': []}
    elif defect == 'future':
        source.created_at = current.created_at + timedelta(seconds=1)
    else:
        current.actor_id = source.actor_id = None
    assert _run(project(repo, tmp_path, current, (source.id,))) == ()
    assert repo.raw_reads == []

@pytest.mark.parametrize('defect', ['duplicate-completion', 'wrong-reference', 'foreign-event', 'binding', 'receipt', 'bad-sha', 'bad-size', 'bad-manifest', 'conflicting-artifact'])
def test_unbound_or_corrupt_publication_never_becomes_evidence(tmp_path: Path, defect: str) -> None:
    repo, current, source, original = fixture(tmp_path)
    events = list(repo.events[source.id])
    if defect == 'duplicate-completion':
        events.append(events[-1].model_copy(update={'sequence': 3}))
    elif defect == 'wrong-reference':
        events[-1] = events[-1].model_copy(update={'payload': {'artifact_id': str(uuid4())}})
    elif defect == 'foreign-event':
        events[0] = events[0].model_copy(update={'run_id': uuid4()})
    else:
        content = json.loads(json.dumps(original.to_payload()['content']))
        delivery = content['workspace_delivery']
        if defect == 'binding':
            content.pop('delivery_report_binding')
        elif defect == 'receipt':
            delivery['verification_receipt_sha256'] = 'f' * 64
        elif defect == 'bad-sha':
            delivery['file']['sha256'] = 'invalid'
        elif defect == 'bad-size':
            delivery['file']['size_bytes'] = True
        elif defect == 'bad-manifest':
            delivery['workspace_files'].append(delivery['workspace_files'][0])
        else:
            content['text'] = 'Conflicting report.'
        changed = Artifact(id=original.id, type=original.type, producer=original.producer, content=content, provenance=original.provenance)
        if defect == 'conflicting-artifact':
            events.insert(1, RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=3, run_id=source.id, artifact=changed))
        else:
            events[0] = events[0].model_copy(update={'artifact': changed})
            events[-1] = events[-1].model_copy(update={'inputs': (changed,)})
    repo.events[source.id] = tuple(events)
    assert _run(project(repo, tmp_path, current, (source.id,))) == ()

@pytest.mark.parametrize('state', ['changed', 'unavailable', 'truncated', 'invalid_utf8', 'symlink'])
def test_readme_is_current_scoped_observation_not_guessed_usage(tmp_path: Path, state: str) -> None:
    repo, current, source, _ = fixture(tmp_path)
    path = tmp_path / str(current.tenant_id) / 'projects/demo/sessions/session-a/README.md'
    if state == 'unavailable':
        path.unlink()
    elif state == 'symlink':
        victim = tmp_path / 'outside'
        victim.write_text('OUTSIDE_PRIVATE', encoding='utf8')
        path.unlink()
        try:
            path.symlink_to(victim)
        except OSError:
            pytest.skip('host does not permit symlink fixture')
    else:
        path.write_bytes({'changed': b'unverified usage', 'truncated': b'x' * 9000, 'invalid_utf8': b'\xff'}[state])
    value = view(_run(project(repo, tmp_path, current, (source.id,))))
    assert value['readme']['status'] == ('unavailable' if state == 'symlink' else state)
    assert 'text' not in value['readme']
    assert 'OUTSIDE_PRIVATE' not in json.dumps(value)

def test_latest_plain_followup_does_not_hide_older_delivery(tmp_path: Path) -> None:
    repo, current, source, _ = fixture(tmp_path)
    plain = SimpleNamespace(**{**vars(source), 'id': uuid4()})
    repo.rows[plain.id] = plain
    report_artifact = Artifact(id=uuid4(), type='text', producer='main', content={'text': 'Only prose'})
    repo.events[plain.id] = (RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=plain.id, artifact=report_artifact), RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=2, run_id=plain.id, payload={'artifact_id': str(report_artifact.id)}, inputs=(report_artifact,)))
    assert view(_run(project(repo, tmp_path, current, (source.id, plain.id))))['source']['run_id'] == str(source.id)

def test_budget_omits_readme_then_whole_artifact_without_slicing_hashes(tmp_path: Path) -> None:
    repo, current, source, _ = fixture(tmp_path)
    full = _run(project(repo, tmp_path, current, (source.id,)))
    from agent_hub.context.builder import estimate_tokens
    size = estimate_tokens(str(full[0].content['text']))
    smaller = view(_run(project(repo, tmp_path, current, (source.id,), size - 1)))
    assert smaller['archive']['sha256'] == 'a' * 64
    assert 'text' not in smaller['readme']
    assert _run(project(repo, tmp_path, current, (source.id,), 1)) == ()

def test_projection_id_is_stable_new_scope_bound_and_discuss_is_not_inferred(tmp_path: Path) -> None:
    repo, current, source, original = fixture(tmp_path)
    first = _run(project(repo, tmp_path, current, (source.id,)))
    second = _run(project(repo, tmp_path, current, (source.id,)))
    assert first == second and first[0].id != original.id
    source.mode = TaskMode.DISCUSS
    assert _run(project(repo, tmp_path, current, (source.id,))) == ()

def test_projection_is_available_for_service_wiring() -> None:
    assert importlib.util.find_spec('agent_hub.runs.delivery_context') is not None
