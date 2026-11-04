"""Dependency observations stay scoped across all actual execution modes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.generated_draft_feedback import (
    generated_draft_artifact_feedback,
    generated_draft_feedback,
)
from agent_hub.runtime.generated_project_guidance import generated_project_guidance
from tests.unit.capabilities.test_generated_dependency_draft import dependency_rejection
from tests.unit.runs.project_auto_repair_typed_fixture import json_map
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map
from tests.unit.runtime.test_generated_draft_feedback import case


@pytest.mark.parametrize('mode',[TaskMode.DIRECT,TaskMode.DISPATCH,TaskMode.DISCUSS,TaskMode.HYBRID])
@pytest.mark.parametrize('kind', ['closure', 'pins'])
def test_selected_modes_receive_concrete_dependency_feedback_without_receipts(mode: TaskMode, kind: str) -> None:
    context, request, result = case()
    context = context.model_copy(update={'mode':mode, 'routing_decision':
        dict(context.routing_decision) | {'python_dependency_preflight_version':1}})
    data = dependency_rejection().to_payload()
    if kind == 'pins':
        data.pop('dependency_closure')
        data.update(schema_version=3, reason='generated_acceptance_python_declaration_invalid',
                    issues=['python_lock_hashes_required'], dependency_pins=[{
                        'name': 'click', 'version': '8.1.8', 'filename': 'click-8.1.8-py3-none-any.whl',
                        'sha256': 'd' * 64, 'size_bytes': 100}])
    for field,value in {'tenant_id':str(context.tenant_id),'run_id':str(context.run_id),
        'user_id':str(context.actor_id),'request_sha256':hashlib.sha256(context.request.encode()).hexdigest(),
        'project_id':'project','session_id':'session','proposer_actor':'main_agent'}.items():
        data[field] = value
    payload = dict(result.payload) | {'generated_draft_rejection':data}
    result = replace(result, payload=json_map(payload))
    feedback = generated_draft_feedback(result, context=context, request=request)
    assert feedback is not None
    draft = read_map(feedback['generated_draft_rejection'])
    assert ('dependency_pins' if kind == 'pins' else 'dependency_closure') in draft
    assert set(feedback) == {'status','untrusted','generated_draft_rejection'}
    artifact = Artifact(id=uuid4(),type='tool_result',producer=request.actor,content={
        'result':feedback,'tool_name':'project.verify','arguments_sha256':hashlib.sha256(b'{}').hexdigest(),
        'call_id':request.call_id})
    assert generated_draft_artifact_feedback(artifact,context=context,actor=request.actor) == feedback
    legacy = context.model_copy(update={'routing_decision':{k:v for k,v in context.routing_decision.items()
        if k != 'python_dependency_preflight_version'}})
    assert generated_draft_feedback(result,context=legacy,request=request) is None


def test_dependency_guidance_is_fresh_only_and_requires_concrete_resolution() -> None:
    context, _, _ = case()
    legacy = generated_project_guidance(context)
    selected = context.model_copy(update={'routing_decision':dict(context.routing_decision) |
        {'python_dependency_preflight_version':1}})
    current = generated_project_guidance(selected)
    assert current.startswith(legacy)
    assert 'dependency_closure' in current and 'specific resolution action' in current
    assert 'trusted dependency data through a real artifact_ref' in current
    assert 'write actual exact pins and hashes to pyproject.toml and requirements.lock' in current
    assert 'never invent hashes, preinstall global packages or replace a dependency with a fake module' in current
    assert 'Publication, input supply and offline recipe installation retain their own concrete approvals' in current
    assert 'An UNKNOWN operation is inspect-only; do not retry a download or extend the original budget/deadline' in current
