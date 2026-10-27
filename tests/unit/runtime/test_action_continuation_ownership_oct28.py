"""Private continuation metadata proof: no model, tools, authorization or replay."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.capabilities.action_preparation import capture_action_preparation
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.domain.runs import TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.continuation_artifacts import _encode, save_continuation
from agent_hub.runtime.contracts import Artifact, JsonValue, RuntimeCheckpoint
from tests.unit.runs.test_automatic_project_inputs_oct28 import memory_threads, run, selector

__all__ = ['memory_threads']


def fixture(tmp_path: Path, defect: str | None = None) -> tuple[Any, Any, Any]:
    preparer, repository, context, _ = selector(tmp_path, files=False)
    repository.row.mode = 'direct'
    context = context.model_copy(update={'mode': TaskMode.DIRECT})
    context = run(preparer.select_existing(context))
    artifacts = InMemoryArtifactRepository()
    preparer.bind_artifact_repository(artifacts)
    verifier = ProjectVerificationService(root=preparer.root, repository=repository, executor=None)
    preparer.verifier = verifier
    store = ProjectWorkspaceStore(preparer.root)
    content = 'print("owned by this run")\n'
    if defect == 'large_content_ref':
        content = 'X' * (2 * 1024**2)
    metadata = store.write_bytes(context.tenant_id, 'existing', 'session-a', 'src/main.py',
                                 content.encode(), 'text/x-python').to_public_dict()
    arguments = {'path': 'src/main.py', 'content': content}
    if defect == 'large_content_ref':
        ref = run(save_continuation(artifacts, tenant_id=context.tenant_id, run_id=context.run_id, value=content))
        arguments = {'path': 'src/main.py', 'content_reference': ref}
    request = HarnessToolCallRequest(run_id=context.run_id, actor='main_agent',
        tool_name='workspace.write_text', arguments=arguments,
        approval_required=False, sandbox='workspace_write', idempotency_key='written1')
    result = HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
        status='succeeded', payload=cast(Mapping[str, JsonValue], {'summary': 'Wrote file.',
            'file': metadata, 'workspace_files': [metadata]}))
    pending = HarnessToolCallRequest(run_id=context.run_id, actor='main_agent',
        tool_name='workspace.write_text', arguments={'path': 'README.md', 'content': '# Next'},
        approval_required=False, sandbox='workspace_write', idempotency_key='pending1')
    raw_sha = lambda req: hashlib.sha256(_encode({'name': req.tool_name,
        'arguments': {'path': 'src/main.py', 'content': content}
            if defect == 'large_content_ref' and req.idempotency_key == 'written1' else req.arguments,
        'key': req.idempotency_key})).hexdigest()
    action = {'raw_sha256': raw_sha(request), 'request': dict(request.to_payload()),
              'result': dict(result.to_payload())}
    with bind_execution_authority(context, worker_id='test-worker'):
        preparation = run(capture_action_preparation(verifier, context))
    snapshot = context.model_copy(update={'execution_token': None, 'checkpoint': None}).to_payload()
    value = {'context': snapshot, 'models': [], 'actions': [action],
        'pending': {'raw_sha256': raw_sha(pending), 'request': dict(pending.to_payload())},
        'emitted': 1, 'elapsed': 1.0, 'inspection': None, 'preparation': preparation}
    if defect == 'inspection':
        inspection = Artifact(id=uuid4(), type='tool_result', producer='main_agent', content={
            'agent_id': 'main_agent', 'tool_name': 'project.inspect', 'call_id': 'inspect_owned',
            'arguments_sha256': hashlib.sha256(b'{}').hexdigest(), 'status': 'succeeded',
            'result': {'scope': {'tenant_id': str(context.tenant_id), 'run_id': str(context.run_id),
                'project_id': 'existing', 'workspace_session_id': 'session-a'}}})
        value['inspection'] = inspection.to_payload()
        context = context.model_copy(update={'artifacts': (inspection,)})
    elif defect == 'foreign_conversation':
        value['context'] = {**snapshot, 'routing_decision': {
            **snapshot['routing_decision'], 'conversation_id': 'foreign-conversation'}}
    elif defect == 'foreign_actor':
        value['context'] = {**snapshot, 'actor_id': str(uuid4())}
    elif defect == 'unknown_field': value['unknown'] = True
    elif defect == 'pending_tool': pending = replace(pending, tool_name='workspace.bundle')
    elif defect == 'failed_write':
        action['result'] = dict(replace(result, status='failed', failure_reason='write failed').to_payload())
    elif defect == 'truncated_write': action['result'] = dict(replace(result, truncated=True).to_payload())
    elif defect == 'call_id': action['result']['call_id'] = 'other_call'
    elif defect == 'tool_name': action['result']['tool_name'] = 'workspace.list'
    elif defect == 'sha': action['result']['payload']['file']['sha256'] = 'a' * 64
    elif defect == 'raw_hash': action['raw_sha256'] = 'a' * 64
    elif defect == 'read_claims_write':
        req = replace(request, tool_name='workspace.list', arguments={})
        action.update(raw_sha256=raw_sha(req), request=dict(req.to_payload()),
            result=dict(replace(result, call_id=req.call_id, tool_name=req.tool_name).to_payload()))
    elif defect == 'extra_file':
        store.write_bytes(context.tenant_id, 'existing', 'session-a', 'foreign.py', b'foreign', 'text/plain')
        with bind_execution_authority(context, worker_id='test-worker'):
            value['preparation'] = run(capture_action_preparation(verifier, context))
    elif defect == 'content_ref':
        ref = run(save_continuation(artifacts, tenant_id=context.tenant_id, run_id=context.run_id, value=content))
        action['request'] = dict(replace(request, arguments={'path': 'src/main.py', 'content_reference': ref}).to_payload())
    if defect == 'large_content_ref':
        value['actions'] = [action] * 24
    reference = run(save_continuation(artifacts, tenant_id=context.tenant_id, run_id=context.run_id,
                                      value=cast(JsonValue, value)))
    checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type='direct_action', runtime_version='2',
        tenant_id=context.tenant_id, run_id=context.run_id, mode=TaskMode.DIRECT,
        state={'phase': 'concrete_action', 'completed': False, 'approval_id': 'approval_pending1',
            'tool_name': pending.tool_name, 'context_sha256': 'b' * 64, 'reference': reference,
            'elapsed_seconds': 2.0, 'timeout_seconds': context.timeout_seconds})
    context = context.model_copy(update={'checkpoint': checkpoint})
    return preparer, repository, context


@pytest.mark.parametrize('variant', [None, 'content_ref', 'inspection'])
def test_negative_partial_writes_resume_only_with_private_successful_write_coverage(tmp_path: Path, variant: str | None) -> None:
    preparer, repository, context = fixture(tmp_path, variant)
    with bind_execution_authority(context, worker_id='test-worker'):
        restored = run(preparer.select_existing(context))
    assert restored.routing_decision['automatic_project_input_selection']['kind'] == 'generated'
    assert 'project_input' not in restored.routing_decision
    assert 'generated_project_preparation' not in repository.row.routing_decision
    assert restored.execution_token == context.execution_token


@pytest.mark.parametrize('defect', ['foreign_conversation', 'foreign_actor', 'unknown_field', 'pending_tool', 'failed_write',
    'truncated_write', 'call_id', 'tool_name', 'sha', 'raw_hash', 'read_claims_write', 'extra_file'])
def test_partial_proof_cannot_claim_foreign_unknown_failed_pending_or_uncovered_files(tmp_path: Path, defect: str) -> None:
    preparer, _, context = fixture(tmp_path, defect)
    with pytest.raises(RuntimeError), bind_execution_authority(context, worker_id='test-worker'):
        run(preparer.select_existing(context))



def test_large_content_references_retain_only_constant_content_metadata(tmp_path: Path) -> None:
    from agent_hub.runtime.action_continuation import read_action_continuation
    preparer, _, context = fixture(tmp_path, 'large_content_ref')
    frame = run(read_action_continuation(preparer.artifact_repository, context))
    assert len(frame.actions) == 24
    for action in frame.actions:
        assert not hasattr(action, 'content')
        assert action.content_sha256 == hashlib.sha256(b'X' * (2 * 1024**2)).hexdigest()
        assert action.content_size_bytes == 2 * 1024**2
        assert 'content' not in action.request.arguments
