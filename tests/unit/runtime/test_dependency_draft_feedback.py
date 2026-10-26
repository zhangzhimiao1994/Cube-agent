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
from tests.unit.runtime.test_generated_draft_feedback import case


@pytest.mark.parametrize('mode',[TaskMode.DIRECT,TaskMode.DISPATCH,TaskMode.DISCUSS,TaskMode.HYBRID])
def test_selected_modes_receive_concrete_dependency_feedback_without_receipts(mode: TaskMode) -> None:
    context, request, result = case()
    context = context.model_copy(update={'mode':mode, 'routing_decision':
        dict(context.routing_decision) | {'python_dependency_preflight_version':1}})
    data = dependency_rejection().to_payload()
    for field,value in {'tenant_id':str(context.tenant_id),'run_id':str(context.run_id),
        'user_id':str(context.actor_id),'request_sha256':hashlib.sha256(context.request.encode()).hexdigest(),
        'project_id':'project','session_id':'session','proposer_actor':'main_agent'}.items():
        data[field] = value
    payload = dict(result.payload) | {'generated_draft_rejection':data}
    result = replace(result, payload=payload)
    feedback = generated_draft_feedback(result, context=context, request=request)
    assert feedback is not None and 'dependency_closure' in feedback['generated_draft_rejection']
    assert set(feedback) == {'status','untrusted','generated_draft_rejection'}
    artifact = Artifact(id=uuid4(),type='tool_result',producer=request.actor,content={
        'result':feedback,'tool_name':'project.verify','arguments_sha256':hashlib.sha256(b'{}').hexdigest(),
        'call_id':request.call_id})
    assert generated_draft_artifact_feedback(artifact,context=context,actor=request.actor) == feedback
    legacy = context.model_copy(update={'routing_decision':{k:v for k,v in context.routing_decision.items()
        if k != 'python_dependency_preflight_version'}})
    assert generated_draft_feedback(result,context=legacy,request=request) is None


def test_dependency_guidance_is_fresh_only_and_requires_an_executable_alternative() -> None:
    context, _, _ = case()
    legacy = generated_project_guidance(context)
    selected = context.model_copy(update={'routing_decision':dict(context.routing_decision) |
        {'python_dependency_preflight_version':1}})
    current = generated_project_guidance(selected)
    assert current.startswith(legacy)
    assert 'dependency_closure' in current and 'supported alternative' in current
