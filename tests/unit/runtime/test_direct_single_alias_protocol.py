"""Real config/Gateway/action journal; provider and Harness results are offline fixtures.

This catches alias-candidate count incorrectly disabling known-response correction.
No provider HTTP, native execution or real-project acceptance is claimed here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.continuation_artifacts import load_continuation
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
from tests.unit.runtime.test_action_admission_helper import OfflineTransport, configured_document
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeConfigService,
    FakeSecretService,
    ImmediateCapacity,
)
from tests.unit.runtime.test_direct_generated_entry import Harness, batch, response
from tests.unit.runtime.test_generated_project_history import evidence


async def test_single_alias_factory_corrects_known_response_before_workspace_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, verified, bundle = await evidence(tmp_path)
    context = context.model_copy(update={
        'token_budget': 50_000, 'timeout_seconds': 60,
        'routing_decision': {**context.routing_decision,
            'main_agent_model': 'main', 'sandbox_profile': 'workspace_write',
            'requested_permissions': ('workspace.read', 'workspace.write', 'command.run'),
            'permission_mode': 'default'},
    })

    class ScopedConfig(FakeConfigService):
        async def get_current(self, tenant_id: Any) -> Any:
            assert tenant_id == context.tenant_id
            revision = await super().get_current(TENANT_ID)
            assert revision is not None
            return replace(revision, tenant_id=tenant_id)

    class WorkspaceHarness(Harness):
        async def invoke(self, tenant: Any, request: Any, **kwargs: Any) -> HarnessToolCallResult:
            assert tenant == context.tenant_id and request.run_id == context.run_id
            assert request.actor == 'main_agent'
            assert kwargs['user_id'] == context.actor_id and kwargs['role'] == context.actor_role
            if request.tool_name in {'workspace.write_text', 'workspace.bundle'}:
                payload = await self.execute(name=request.tool_name)
                return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                    status='succeeded', payload=payload)
            return await super().invoke(tenant, request, **kwargs)

    harness = WorkspaceHarness(context, verified, bundle)
    repository = InMemoryArtifactRepository()
    children: list[ActionApprovedDirectRuntime] = []
    observations: list[tuple[int, bool, tuple[str, ...]]] = []
    events: list[RunEvent] = []

    class ObservedTransport(OfflineTransport):
        async def complete(self, deployment: Any, request: Any, api_key: str) -> Any:
            journal = children[0]._journal
            assert journal is not None
            observations.append((len(journal.references), journal.replay_complete,
                tuple(event.kind for event in events)))
            assert harness.calls == ['project.inspect']  # Neither rejected output nor correction ran tools.
            assert deployment.logical_model == request.logical_model == 'main'
            return await super().complete(deployment, request, api_key)

    # Conflicting controls remain invalid even though unambiguous root controls
    # are now supported by the separate batch-envelope compatibility path.
    malformed = json.dumps({'complete': True, 'continuation': '', 'summary': 'Synthetic draft.',
        'workspace_batch': {'files': {
            f'synthetic/file-{index}.txt': f'Synthetic fixture {index}.\n' for index in range(10)
        }, 'complete': False, 'continuation': 'Conflicting unfinished control.'}})
    transport = ObservedTransport([response(malformed), response(batch())])

    async def capacity(tenant_id: Any, deployments: Any) -> ImmediateCapacity:
        assert tenant_id == context.tenant_id and len(deployments) == 1
        return ImmediateCapacity(deployments)

    factory = ConfigBackedDirectRuntime(
        config_service=cast(Any, ScopedConfig(configured_document())),
        secret_service=cast(Any, FakeSecretService()), capacity_factory=cast(Any, capacity),
        transport=transport, artifact_repository=repository,
        action_capability_gateway=cast(Any, harness), action_harness_tool_gateway=cast(Any, harness),
    )
    build = factory._runtime_for

    async def observe_child(task: Any) -> Any:
        child = await build(task)
        assert isinstance(child, ActionApprovedDirectRuntime)
        assert child._available_model_attempts == 1
        children.append(child)
        return child

    monkeypatch.setattr(factory, '_runtime_for', observe_child)
    async for event in factory.run(context):
        events.append(event)

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(children) == 1 and len(transport.requests) == 2 and not transport.replies
    assert harness.calls == ['project.inspect', 'workspace.write_text', 'workspace.write_text',
        'project.verify', 'workspace.bundle']
    assert [(count, complete) for count, complete, _ in observations] == [(0, True), (1, True)]
    assert observations[1][2].count(EventKind.MODEL_COMPLETED) == 1
    assert observations[1][2].count(EventKind.MODEL_STARTED) == 2
    assert all(event.run_id == context.run_id for event in events)
    for kind in (EventKind.MODEL_STARTED, EventKind.MODEL_COMPLETED):
        assert sum(event.kind is kind for event in events) == 2
    starts = [event for event in events if event.kind is EventKind.MODEL_STARTED]
    completions = [event for event in events if event.kind is EventKind.MODEL_COMPLETED]
    assert len({event.payload['call_id'] for event in starts}) == 2
    assert [(event.payload['call_id'], event.payload['request_sha256']) for event in starts] == [
        (event.payload['call_id'], event.payload['request_sha256']) for event in completions]
    first, corrected = transport.requests
    assert corrected.messages[:-1] == first.messages
    first_prompt = '\n'.join(str(message.content) for message in first.messages)
    assert context.request in first_prompt and 'UNTRUSTED_PROJECT_INSPECTION_JSON' in first_prompt
    prompt = '\n'.join(str(message.content) for message in corrected.messages)
    assert context.request in prompt and 'UNTRUSTED_PROJECT_INSPECTION_JSON' in prompt
    assert 'UNTRUSTED_REJECTED_DIRECT_RESPONSE' in prompt
    # Inspect only the new trusted correction, never the initial schema or rejected response.
    correction = str(corrected.messages[-1].content).split('<UNTRUSTED_REJECTED_DIRECT_RESPONSE>', 1)[0]
    examples = []
    decoder = json.JSONDecoder()
    for index, character in enumerate(correction):
        if character != '{':
            continue
        try:
            example, _ = decoder.raw_decode(correction, index)
        except json.JSONDecodeError:
            continue
        if isinstance(example, dict) and isinstance(example.get('workspace_batch'), dict):
            examples.append(example)
    assert any(
        isinstance(example['workspace_batch'].get('files'), dict)
        and type(example['workspace_batch'].get('complete')) is bool
        and type(example['workspace_batch'].get('continuation')) is str
        and not {'complete', 'continuation'} & example.keys()
        for example in examples
    ), 'Correction must explicitly show complete and continuation nested inside workspace_batch'
    assert not first.tools and not corrected.tools
    assert 0 < corrected.timeout_seconds <= first.timeout_seconds <= context.timeout_seconds
    assert 0 < corrected.max_output_tokens <= first.max_output_tokens
    assert sum(cast(int, event.payload['total_tokens']) for event in completions) == 360

    journal = children[0]._journal
    assert journal is not None and journal.replay_complete and len(journal.references) == 2
    saved = [await load_continuation(repository, tenant_id=context.tenant_id, run_id=context.run_id,
        reference=cast(Any, reference)) for reference in journal.references]
    assert [cast(Any, item)['position'] for item in saved] == [0, 1]
    assert [cast(Any, item)['response']['text'] for item in saved] == [malformed, batch()]
    assert cast(Any, saved[0])['request_sha256'] != cast(Any, saved[1])['request_sha256']
