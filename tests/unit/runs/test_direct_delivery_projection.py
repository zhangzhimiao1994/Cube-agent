"""Lift only a Direct final delivery's explicit safe summary into public text."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.api.routers.admin import _admin_run_artifact
from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import _public_artifact_payload
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime

SUMMARY = 'Implemented the utility. Use npm test. Checks passed; offline limits remain. See project.zip.'


def delivery() -> dict[str, Any]:
    return {'id': str(uuid4()), 'type': 'tool_result', 'producer': 'main', 'content': {
        'artifact_origin': 'incremental_workspace_delivery', 'summary': SUMMARY,
        'workspace_delivery': {'artifact_id': str(uuid4()), 'presentation': 'final_attachment',
            'file': {'filename': 'project.zip', 'mime_type': 'application/zip',
                     'sha256': 'a' * 64, 'size_bytes': 50},
            'workspace_files': [{'path': 'README.md', 'sha256': 'b' * 64, 'size_bytes': 10}]}}}


@pytest.mark.parametrize('origin', ['incremental_workspace_delivery', 'model_workspace_bundle'])
async def test_direct_delivery_preserves_explicit_summary_without_changing_file_identity(origin: str) -> None:
    artifact = delivery()
    artifact['content']['artifact_origin'] = origin
    before = deepcopy(artifact)
    projected = _admin_run_artifact(artifact, run_id=uuid4())
    assert projected.text == SUMMARY
    assert projected.presentation == 'final_attachment'
    assert projected.filename == 'project.zip' and projected.sha256 == 'a' * 64
    assert projected.download_url is not None
    assert artifact == before


@pytest.mark.parametrize('defect', ['type', 'producer', 'origin', 'missing_origin', 'extra_logs',
    'raw_bundle', 'tool_envelope', 'missing_delivery', 'missing_file', 'mime', 'sha', 'size',
    'presentation', 'missing_summary', 'summary_object', 'summary_blank', 'summary_secret'])
def test_non_direct_or_malformed_summary_is_not_promoted(defect: str) -> None:
    artifact = delivery()
    content = artifact['content']
    result = content['workspace_delivery']
    if defect == 'type':
        artifact['type'] = 'model_result'
    elif defect == 'producer':
        artifact['producer'] = 'conversation_context'
    elif defect == 'origin':
        content['artifact_origin'] = 'model_response'
    elif defect == 'missing_origin':
        content.pop('artifact_origin')
    elif defect == 'extra_logs':
        content['logs'] = 'PRIVATE_LOG_MUST_NOT_BECOME_REPLY'
    elif defect == 'raw_bundle':
        content['workspace_bundle'] = {'files': {'index.js': 'PRIVATE_SOURCE'}}
    elif defect == 'tool_envelope':
        content['result'] = content.pop('workspace_delivery')
    elif defect == 'missing_delivery':
        content.pop('workspace_delivery')
    elif defect == 'missing_file':
        result.pop('file')
    elif defect in ('mime', 'sha', 'size'):
        result['file'][{'mime': 'mime_type', 'sha': 'sha256', 'size': 'size_bytes'}[defect]] = {
            'mime': 'text/plain', 'sha': 'bad', 'size': True}[defect]
    elif defect == 'presentation':
        result['presentation'] = 'internal'
    elif defect == 'missing_summary':
        content.pop('summary')
    else:
        content['summary'] = {'summary_object': {'raw': 'PRIVATE_LOG'}, 'summary_blank': ' ',
                              'summary_secret': 'password=PRIVATE_SECRET'}[defect]
    projected = _admin_run_artifact(artifact, run_id=uuid4())
    assert projected.text is None
    assert projected.presentation != 'final_answer'


def test_arbitrary_summary_and_raw_tool_or_model_content_stay_private() -> None:
    for kind in ('tool_result', 'model_result', 'model_attempt', 'text'):
        artifact: dict[str, object] = {'id': str(uuid4()), 'type': kind, 'producer': 'main',
                    'content': {'summary': 'PRIVATE_LOG', 'result': {'stdout': 'PRIVATE_LOG'}}}
        assert _admin_run_artifact(artifact, run_id=uuid4()).text is None


def test_existing_natural_reply_and_sensitive_text_policy_are_unchanged() -> None:
    for text, expected in [('Natural language reply.', 'Natural language reply.'),
                           ('password=PRIVATE_SECRET', None)]:
        artifact: dict[str, object] = {'id': str(uuid4()), 'type': 'text', 'producer': 'main',
                                      'content': {'text': text}}
        assert _admin_run_artifact(artifact, run_id=uuid4()).text == expected


class FixedModel:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        return GatewayCompletion(response=ModelResponse(text=self.text, usage=TokenUsage(1, 1, 2)),
            deployment_id='fixed', logical_model=request.logical_model, provider_id='fixed',
            provider_model='fixed/offline', cost_usd=Decimal(0))


class MemoryWorkspace:
    """Workspace transport double; no Root verification, files, commands or network."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        return name != 'project.verify'

    def is_replay_safe(self, name: str) -> bool:
        return False

    async def execute(self, *, name: str, arguments: Mapping[str, JsonValue],
                      **kwargs: Any) -> Mapping[str, JsonValue]:
        if name == 'workspace.write_text':
            path, text = arguments['path'], arguments['content']
            assert isinstance(path, str) and isinstance(text, str)
            self.files[path] = text
        metadata: JsonValue = tuple({'path': path, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'size_bytes': len(text.encode())} for path, text in self.files.items())
        if name == 'workspace.bundle':
            return {'artifact_id': str(uuid4()), 'presentation': 'final_attachment',
                'file': {'filename': 'project.zip', 'mime_type': 'application/zip',
                    'sha256': 'a' * 64, 'size_bytes': 50}, 'workspace_files': metadata}
        return {'workspace_files': metadata}

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
            status='succeeded', payload=await self.execute(name=request.tool_name, arguments=request.arguments))


@pytest.mark.parametrize('action', [False, True])
@pytest.mark.parametrize('envelope', ['workspace_bundle', 'workspace_batch'])
async def test_actual_direct_and_action_artifacts_survive_public_redaction(
    action: bool, envelope: str,
) -> None:
    explicit = 'Delivered docs; see README.md. Verification was not run.'
    values: dict[str, object] = {'files': {'README.md': 'UNPUBLISHED_SOURCE_NOT_A_REPLY'}}
    if envelope == 'workspace_batch':
        values.update(complete=True, continuation='')
    model = FixedModel(json.dumps({envelope: values, 'summary': explicit}))
    backend = MemoryWorkspace()
    ctx = TaskContext(run_id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        mode=TaskMode.DIRECT, request='Create a document package.', routing_decision={
            'project_delivery': 'workspace', 'artifact_strategy': 'workspace_bundle',
            'project_scale': 'small', 'sandbox_profile': 'workspace_write',
        } if envelope == 'workspace_batch' else {})
    runtime = (ActionApprovedDirectRuntime(model, logical_model='fixed', capability_gateway=backend,
        harness_tool_gateway=backend, artifact_repository=InMemoryArtifactRepository()) if action
        else DirectRuntime(model, logical_model='fixed', capability_gateway=backend))
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    artifact = next(event.artifact for event in reversed(events) if event.artifact is not None
                    and event.artifact.producer == 'main')
    assert artifact is not None
    assert artifact.content['artifact_origin'] == (
        'incremental_workspace_delivery' if envelope == 'workspace_batch' else 'model_workspace_bundle')
    assert ('workspace_delivery' in artifact.content) is (envelope == 'workspace_batch')
    public = _public_artifact_payload(artifact.to_payload())
    projected = _admin_run_artifact(public, run_id=ctx.run_id)
    assert projected.text == explicit
    assert 'UNPUBLISHED_SOURCE_NOT_A_REPLY' not in projected.text


@pytest.mark.parametrize('encoded', ['missing_summary', 'truncated', 'raw_text', 'sensitive'])
def test_raw_bundle_never_promotes_source_or_an_incomplete_preview(encoded: str) -> None:
    bundle = {'files': {'README.md': 'UNPUBLISHED_SOURCE_NOT_A_REPLY'}}
    content: dict[str, object] = {'artifact_origin': 'model_workspace_bundle', 'workspace_bundle': bundle,
        'summary': {'missing_summary': json.dumps({'workspace_bundle': bundle}),
            'truncated': '{"summary":"must not guess", "workspace_bundle":…',
            'raw_text': 'UNPUBLISHED_SOURCE_NOT_A_REPLY',
            'sensitive': json.dumps({'workspace_bundle': bundle, 'summary': 'password=PRIVATE_SECRET'})}[encoded]}
    artifact: dict[str, object] = {'id': str(uuid4()), 'type': 'tool_result', 'producer': 'main', 'content': content}
    assert _admin_run_artifact(artifact, run_id=uuid4()).text is None
