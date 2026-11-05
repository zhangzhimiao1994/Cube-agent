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
from agent_hub.runtime import direct_delivery_report
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    GatewayProvenance,
    JsonValue,
    TaskContext,
)
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


def reported_delivery() -> tuple[dict[str, Any], UUID]:
    """Real Direct final-report shape, including its private runtime binding."""
    run_id, tenant_id = uuid4(), uuid4()
    raw = delivery()
    content = raw['content']
    text = ('已交付项目与 ZIP。\n\n| 阶段 | 结果 |\n| --- | --- |\n| test | passed |\n\n'
            + '使用方法见 README；未验证部署。' * 20)
    preview = ' '.join(text.split())
    content.update(text=text, summary=preview[:239].rstrip() + '…')
    result = content['workspace_delivery']
    result['verification'] = {'status': 'passed', 'checks': [{'phase': 'test', 'status': 'passed'}]}
    content['delivery_report_binding'] = {
        'version': 1, 'tenant_id': str(tenant_id), 'run_id': str(run_id),
        'user_request_sha256': '1' * 64, 'request_sha256': '2' * 64,
        'delivery_sha256': direct_delivery_report.digest(result),
        'verification_sha256': direct_delivery_report.digest(result['verification']),
        'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'logical_model': 'fixed', 'deployment': 'fixed', 'provider': 'fixed', 'upstream_model': 'fixed/offline',
    }
    artifact = Artifact(id=UUID(raw['id']), type='tool_result', producer='main', content=content,
                        provenance=GatewayProvenance(logical_model='fixed', deployment_id='fixed',
                                                     provider_id='fixed', provider_model='fixed/offline'))
    return artifact.to_payload(), run_id


@pytest.mark.parametrize('public', [False, True])
@pytest.mark.parametrize('layout', [False, True])
@pytest.mark.parametrize('origin', ['incremental_workspace_delivery', 'model_workspace_bundle',
                                  'verified_hybrid_final_report'])
def test_verified_direct_report_is_final_attachment_before_and_after_public_redaction(
    public: bool, layout: bool, origin: str,
) -> None:
    artifact, run_id = reported_delivery()
    artifact['content']['artifact_origin'] = origin
    if layout:
        artifact['content']['generated_event_layout'] = {'schema_version': 1, 'model_call_count': 2}
    artifact = Artifact.from_payload({**artifact, 'content_sha256': ''}).to_payload()
    report_content = artifact['content']
    assert isinstance(report_content, dict) and isinstance(report_content.get('text'), str)
    expected = report_content['text']
    if public:
        artifact = _public_artifact_payload(artifact)
        public_content = artifact['content']
        assert isinstance(public_content, dict)
        assert 'delivery_report_binding' not in public_content
        assert artifact['content_redacted'] is True
    before = deepcopy(artifact)
    response = _admin_run_artifact(artifact, run_id=run_id)
    assert response.text == expected
    assert response.presentation == 'final_attachment'
    assert response.kind == 'tool_result' and response.title == 'main'
    assert response.filename == 'project.zip' and response.sha256 == 'a' * 64
    assert response.download_url is not None
    assert artifact == before


@pytest.mark.parametrize('defect', ['untrusted', 'binding_text', 'binding_delivery', 'binding_version',
                                  'binding_run', 'public_hash', 'public_missing_hash', 'public_tampered',
                                  'public_not_redacted', 'extra_logs', 'extra_result', 'preview',
                                  'producer', 'missing_delivery', 'private_text', 'unknown_origin',
                                  'hybrid_unbound', 'verification', 'binding_provenance'])
def test_untrusted_or_noncanonical_direct_reports_are_not_promoted(defect: str) -> None:
    artifact, run_id = reported_delivery()
    if defect.startswith('public_'):
        artifact = _public_artifact_payload(artifact)
        if defect == 'public_hash':
            artifact['public_content_sha256'] = '0' * 64
        elif defect == 'public_missing_hash':
            artifact.pop('public_content_sha256')
        elif defect == 'public_tampered':
            public_content = artifact['content']
            assert isinstance(public_content, dict)
            text = public_content['text']
            assert isinstance(text, str)
            public_content['text'] = text + ' Different body.'
        else:
            artifact['content_redacted'] = False
    elif defect == 'untrusted':
        artifact.pop('provenance')
    elif defect == 'binding_run':
        run_id = uuid4()
    elif defect.startswith('binding_'):
        binding = artifact['content']['delivery_report_binding']
        binding[{'binding_text': 'text_sha256', 'binding_delivery': 'delivery_sha256',
                 'binding_version': 'version', 'binding_provenance': 'deployment'}[defect]] = (
                     2 if defect == 'binding_version' else 'f' * 64)
        artifact = Artifact.from_payload({**artifact, 'content_sha256': ''}).to_payload()
    else:
        if defect == 'extra_logs':
            artifact['content']['logs'] = 'PRIVATE_LOG'
        elif defect == 'extra_result':
            artifact['content']['result'] = {'stdout': 'PRIVATE_LOG'}
        elif defect == 'preview':
            artifact['content']['summary'] = 'Unrelated summary'
        elif defect == 'producer':
            artifact['producer'] = 'project.inspect'
        elif defect == 'missing_delivery':
            artifact['content'].pop('workspace_delivery')
        elif defect == 'private_text':
            artifact['content']['text'] = 'password=PRIVATE_SECRET'
        elif defect == 'unknown_origin':
            artifact['content']['artifact_origin'] = 'model_response'
        elif defect == 'hybrid_unbound':
            artifact['content']['artifact_origin'] = 'verified_hybrid_final_report'
            artifact['content'].pop('text')
            artifact['content'].pop('delivery_report_binding')
        elif defect == 'verification':
            result = artifact['content']['workspace_delivery']
            result['verification']['status'] = 'failed'
            binding = artifact['content']['delivery_report_binding']
            binding['delivery_sha256'] = direct_delivery_report.digest(result)
            binding['verification_sha256'] = direct_delivery_report.digest(result['verification'])
        artifact = Artifact.from_payload({**artifact, 'content_sha256': ''}).to_payload()
    response = _admin_run_artifact(artifact, run_id=run_id)
    assert response.presentation not in {'final_attachment', 'final_answer'}


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
