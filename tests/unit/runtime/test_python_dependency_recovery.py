"""Dependency recovery observations preserve lock details without buying budget."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from agent_hub.runtime.autogen.engineering_delivery import (
    engineering_delivery_ready,
    engineering_progress,
    engineering_work_progress,
)
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.generated_draft_feedback import generated_draft_artifact_feedback
from agent_hub.runtime.self_repair_context import project_repair_failure_text
from tests.unit.capabilities.test_generated_dependency_draft import observation
from tests.unit.runtime.autogen.test_draft_action_progress import (
    ALLOWED,
    append,
    draft_case,
    result,
)
from tests.unit.runtime.test_generated_draft_autogen import draft_artifact, rejection


def _closure():
    return {'status': 'incomplete', 'issues': [
        {'code': 'missing_dependency', 'source': 'demo-dep', 'dependency': 'demo-helper',
            'constraint': '>=2', 'locked_version': None, 'candidates': [
                {'name': 'demo-helper', 'version': '2.1',
                    'filename': 'demo_helper-2.1-py3-none-any.whl', 'sha256': '1' * 64}]},
        {'code': 'version_conflict', 'source': 'demo-dep', 'dependency': 'demo-conflict',
            'constraint': '==2.1', 'locked_version': '1.0', 'candidates': [
                {'name': 'demo-conflict', 'version': '2.1',
                    'filename': 'demo_conflict-2.1-py3-none-any.whl', 'sha256': '2' * 64}]},
    ]}


def _case():
    ctx, rows, artifacts = draft_case()
    ctx = ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'python_dependency_preflight_version': 1}})
    return ctx, rows[:-1], artifacts[:-1]


def _dependency_artifact(ctx, *, digest='7' * 64, observed=None):
    data = rejection(ctx, observation=digest)
    if observed is None:
        observed = observation() | {'closure': _closure()}
    data.update(schema_version=2, reason='generated_acceptance_python_dependency_incomplete',
        issues=['python_dependency_incomplete'], dependency_closure=observed)
    return result('project.verify', {
        'status': 'failed', 'untrusted': True, 'generated_draft_rejection': data})


def _hints(ctx, rows, artifacts):
    return engineering_work_progress(ctx, tool_entries=rows, artifacts=artifacts,
        allowed_tools=ALLOWED, include_draft_diagnoses=True)


def _project(artifact, *, expected_id=None, expected_sha=None, feedback=None):
    return project_repair_failure_text(artifact,
        expected_artifact_id=str(artifact.id) if expected_id is None else expected_id,
        expected_artifact_sha256=artifact.content_sha256 if expected_sha is None else expected_sha,
        validated_feedback=artifact.content['result'] if feedback is None else feedback)


def test_v2_dependency_observation_never_earns_draft_diagnosis_credit() -> None:
    ctx, rows, artifacts = _case()
    before = _hints(ctx, rows, artifacts)
    assert len(before) == 1 and all(key.startswith('workspace.write_text:') for key in before)
    artifact = _dependency_artifact(ctx)
    assert generated_draft_artifact_feedback(artifact, context=ctx, actor='implementer') is not None
    append(ctx, rows, artifacts, artifact, status='failed')
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts, allowed_tools=ALLOWED)
    assert not progress.uncertain and progress.verification_failed_current
    assert not progress.verification_current and not progress.bundle_current
    assert _hints(ctx, rows, artifacts) == before
    assert not engineering_delivery_ready(run_id=ctx.run_id, tool_entries=rows,
        artifacts=artifacts, allowed_tools=ALLOWED, context=ctx)
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert _hints(ctx, [dict(row) for row in rows], replay) == before


def test_changed_dependency_failures_and_noop_writes_cannot_renew_budget() -> None:
    ctx, rows, artifacts = _case()
    expected = _hints(ctx, rows, artifacts)
    for index in range(3):
        observed = observation() | {'closure': _closure()}
        observed['closure']['issues'][0]['constraint'] = f'>={index + 2}'
        append(ctx, rows, artifacts, _dependency_artifact(ctx,
            digest=str(index + 7) * 64, observed=observed), status='failed')
        append(ctx, rows, artifacts, result('workspace.write_text', {
            'path': 'ACCEPTANCE.json', 'sha256': 'b' * 64, 'size_bytes': 10}))
        assert _hints(ctx, rows, artifacts) == expected
    append(ctx, rows, artifacts, result('workspace.write_text', {
        'path': 'requirements.lock', 'sha256': 'c' * 64, 'size_bytes': 200}))
    productive = _hints(ctx, rows, artifacts)
    assert len(productive) == len(expected) + 1
    append(ctx, rows, artifacts, _dependency_artifact(ctx, digest='a' * 64), status='failed')
    assert _hints(ctx, rows, artifacts) == productive


def test_v2_observation_consumes_intervening_write_before_later_v1_diagnosis() -> None:
    ctx, rows, artifacts = _case()
    before = _hints(ctx, rows, artifacts)
    append(ctx, rows, artifacts, _dependency_artifact(ctx), status='failed')
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='8' * 64), status='failed')
    assert _hints(ctx, rows, artifacts) == before
    append(ctx, rows, artifacts, result('workspace.write_text', {
        'path': 'pyproject.toml', 'sha256': 'd' * 64, 'size_bytes': 50}))
    productive = _hints(ctx, rows, artifacts)
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='9' * 64), status='failed')
    assert _hints(ctx, rows, artifacts) == productive | {'draft.diagnosis:' + '9' * 64}


def test_v1_diagnosis_retains_its_causal_credit() -> None:
    ctx, rows, artifacts = draft_case()
    hints = _hints(ctx, rows, artifacts)
    assert len(hints) == 2
    assert 'draft.diagnosis:' + 'a' * 64 in hints
    append(ctx, rows, artifacts, draft_artifact(ctx, observation='b' * 64), status='failed')
    assert _hints(ctx, rows, artifacts) == hints


def test_v2_recovery_projection_preserves_concrete_lock_correction_data() -> None:
    ctx, _, _ = _case()
    artifact = _dependency_artifact(ctx)
    validated = generated_draft_artifact_feedback(artifact, context=ctx, actor='implementer')
    assert validated is not None
    original = artifact.content_sha256
    text = _project(artifact, feedback=validated)
    prefix = 'UNTRUSTED_PROJECT_REPAIR_FEEDBACK_JSON='
    assert text.startswith(prefix)
    payload = json.loads(text.removeprefix(prefix))
    draft = payload['diagnostic']['generated_draft_rejection']
    assert draft.get('dependency_closure') == {'schema_version': 1, **_closure()}
    assert draft['schema_version'] == 2
    assert draft['reason'] == 'generated_acceptance_python_dependency_incomplete'
    assert draft['issues'] == ['python_dependency_incomplete']
    assert draft['execution_started'] is False and draft['observation_sha256'] == '7' * 64
    assert payload['schema_version'] == 1 and payload['untrusted'] is True
    assert payload['artifact_id'] == str(artifact.id) and payload['artifact_sha256'] == original
    for value in (str(ctx.tenant_id), str(ctx.run_id), str(ctx.actor_id),
                  'draft-project', 'draft-session', hashlib.sha256(ctx.request.encode()).hexdigest()):
        assert value not in text
    assert not {'target_environment', 'source_sha256', 'manifest_sha256', 'lock_sha256',
        'catalog_sha256', 'metadata_index_sha256', 'target_environment_sha256'} & draft.keys()
    assert all(token not in text for token in ('"scope"', '"path"', '"url"', '://'))
    assert len(text.encode('utf-8')) < 10 * 1024
    assert artifact.recompute_content_sha256() == original


@pytest.mark.parametrize('status,code', [
    ('metadata_unavailable', 'metadata_missing'), ('unsupported', 'unsupported_dependency'),
])
def test_projection_preserves_noncorrectable_dependency_issue_without_inventing_a_pin(status, code):
    ctx, _, _ = _case()
    closure = {'status': status, 'issues': [{'code': code, 'source': 'demo-dep',
        'dependency': '', 'candidates': [], 'constraint': '', 'locked_version': None}]}
    artifact = _dependency_artifact(ctx, observed=observation() | {'closure': closure})
    text = _project(artifact)
    payload = json.loads(text.split('=', 1)[1])
    assert payload['diagnostic']['generated_draft_rejection']['dependency_closure'] == {
        'schema_version': 1, **closure}


def test_large_but_bounded_root_environment_is_validated_then_omitted_from_prompt() -> None:
    ctx, _, _ = _case()
    observed = observation() | {'closure': _closure()}
    observed['target_environment']['platform_version'] = 'private-runtime-' + 'x' * 20000
    observed['target_environment_sha256'] = hashlib.sha256(json.dumps(observed['target_environment'],
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    artifact = _dependency_artifact(ctx, observed=observed)
    assert generated_draft_artifact_feedback(artifact, context=ctx, actor='implementer') is not None
    text = _project(artifact)
    assert 'private-runtime-' not in text and 'target_environment' not in text
    assert len(text.encode('utf-8')) < 10 * 1024


@pytest.mark.parametrize('defect', [
    'root_extra_scope', 'root_extra_url', 'root_extra_path', 'root_environment_digest',
    'root_oversized', 'closure_status', 'closure_extra', 'issue_code', 'issue_source',
    'constraint_url', 'constraint_arbitrary', 'locked_version', 'candidate_url',
    'candidate_path', 'candidate_extra', 'candidate_count', 'issue_count', 'draft_schema',
])
def test_untrusted_v2_projection_revalidates_closed_and_bounded_dependency_schema(defect) -> None:
    ctx, _, _ = _case()
    observed = observation() | {'closure': _closure()}
    closure = observed['closure']
    issue = closure['issues'][0]
    candidate = issue['candidates'][0]
    if defect.startswith('root_extra_'):
        observed[defect.removeprefix('root_extra_')] = 'https://private.invalid/secret'
    elif defect == 'root_environment_digest':
        observed['target_environment_sha256'] = 'f' * 64
    elif defect == 'root_oversized':
        observed['target_environment']['platform_version'] = 'x' * (32 * 1024)
        observed['target_environment_sha256'] = hashlib.sha256(json.dumps(
            observed['target_environment'], sort_keys=True, separators=(',', ':'),
            ensure_ascii=True).encode()).hexdigest()
    elif defect == 'closure_status':
        closure['status'] = 'complete'
    elif defect == 'closure_extra':
        closure['execution_receipt'] = 'forged'
    elif defect == 'issue_code':
        issue['code'] = 'run_installer'
    elif defect == 'issue_source':
        issue['source'] = '../private'
    elif defect == 'constraint_url':
        issue['constraint'] = 'demo-helper @ https://private.invalid/wheel'
    elif defect == 'constraint_arbitrary':
        issue['constraint'] = '===private'
    elif defect == 'locked_version':
        closure['issues'][1]['locked_version'] = False
    elif defect == 'candidate_url':
        candidate['filename'] = 'https://private.invalid/demo.whl'
    elif defect == 'candidate_path':
        candidate['filename'] = '../demo_helper-2.1-py3-none-any.whl'
    elif defect == 'candidate_extra':
        candidate['approval'] = 'forged'
    elif defect == 'candidate_count':
        issue['candidates'] *= 4
    elif defect == 'issue_count':
        closure['issues'] *= 17
    artifact = _dependency_artifact(ctx, observed=observed)
    if defect == 'draft_schema':
        content = json.loads(artifact.model_dump_json())['content']
        content['result']['generated_draft_rejection']['schema_version'] = True
        artifact = Artifact(id=artifact.id, type='tool_result', producer='implementer', content=content)
    assert generated_draft_artifact_feedback(artifact, context=ctx, actor='implementer') is None
    with pytest.raises(ValueError, match='^project repair failure evidence is invalid$'):
        _project(artifact)


@pytest.mark.parametrize('defect', ['artifact_id', 'artifact_sha', 'feedback', 'content_sha', 'untrusted'])
def test_dependency_projection_keeps_the_artifact_and_untrusted_binding(defect) -> None:
    ctx, _, _ = _case()
    artifact = _dependency_artifact(ctx)
    arguments = {}
    if defect == 'artifact_id':
        arguments['expected_id'] = str(uuid4())
    elif defect == 'artifact_sha':
        arguments['expected_sha'] = 'f' * 64
    elif defect == 'feedback':
        feedback = deepcopy(json.loads(artifact.model_dump_json())['content']['result'])
        feedback['generated_draft_rejection']['observation_sha256'] = '8' * 64
        arguments['feedback'] = feedback
    elif defect == 'content_sha':
        artifact = artifact.model_copy(update={'content_sha256': 'f' * 64})
    else:
        content = json.loads(artifact.model_dump_json())['content']
        content['result']['untrusted'] = False
        artifact = Artifact(id=artifact.id, type='tool_result', producer='implementer', content=content)
    with pytest.raises(ValueError, match='^project repair failure evidence is invalid$'):
        _project(artifact, **arguments)
