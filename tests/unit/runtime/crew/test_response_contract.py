from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import JsonValue, RunEvent
from agent_hub.runtime.crew.adapter import (
    CrewAIObjectFactory,
    CrewDispatchRuntime,
    RuntimeExecutionError,
)
from agent_hub.runtime.instruction_context import model_request_sha256
from tests.integration.runtime.test_crew_adapter import (
    FakeCapabilities,
    FakeGateway,
    FastFactory,
    ToolGateway,
    one_step_plan,
)
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    SequenceGateway,
    _reviewed_step_plan,
    _structured_dependent_final_plan,
)
from tests.unit.runtime.crew.test_instruction_context import injections, task

SCHEMA_MARKER = 'INTERNAL_RESPONSE_SCHEMA_JSON='


def assert_contract(request: ModelRequest) -> None:
    contracts = [message for message in request.messages if SCHEMA_MARKER in str(message.content)]
    if request.response_schema is None:
        assert contracts == []
        return
    assert len(contracts) == 1
    contract = contracts[0]
    assert contract.role == 'system'
    content = str(contract.content)
    actual = json.loads(content.split(SCHEMA_MARKER, 1)[1])
    expected = json.loads(json.dumps(request.response_schema.schema, default=dict))
    assert actual == expected
    assert 'internal role result' in content
    assert 'user-facing' in content
    assert 'Do not invent evidence' in content


@pytest.mark.parametrize('guidance', [False, True])
@pytest.mark.parametrize('real_crew', [False, True])
async def test_internal_schema_matches_worker_and_reviewer_hard_schema(
    tmp_path: Path, guidance: bool, real_crew: bool,
) -> None:
    context = await task(tmp_path)
    context = context.model_copy(update={
        'request': 'Reply in one short line; no tools. Keep GUIDE_APPLIED_TEST.',
        'instruction_context': context.instruction_context if guidance else None,
    })
    plan = _reviewed_step_plan()
    before = plan.model_dump_json()
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=(
        CrewAIObjectFactory(storage_dir=tmp_path / 'crew') if real_crew else FastFactory()
    ))
    events = [event async for event in runtime.run(context)]
    assert len(gateway.requests) == 3
    assert {request.response_schema.name for request in gateway.requests
            if request.response_schema} == {'DispatchRoleOutput', 'DispatchReviewVerdict'}
    checkpoint = await runtime.save_checkpoint()
    ledger = cast(Mapping[str, Mapping[str, JsonValue]], checkpoint.state['models'])
    for request in gateway.requests:
        assert_contract(request)
        text = '\n'.join(str(message.content) for message in request.messages)
        if guidance or request.logical_model != 'review':
            assert context.request in text
        else:
            assert 'REVIEWER. Return only JSON' in text
        assert request.tools == ()
        assert request.logical_model in {agent.logical_model for agent in plan.agents}
        assert any(entry['request_sha256'] == runtime._model_request_sha256(request)
                   for entry in ledger.values())
        assert any(
            entry['request_sha256'] == runtime._model_request_sha256(request)
            and entry['status'] == 'running'
            for event in events if event.checkpoint
            for entry in cast(Mapping[str, Mapping[str, JsonValue]],
                              event.checkpoint.state['models']).values()
        )
        if request.response_schema:
            assert SCHEMA_MARKER in str(request.messages[-1].content)
            without_contract = replace(request, messages=request.messages[:-1])
            assert runtime._model_request_sha256(request) != runtime._model_request_sha256(
                without_contract,
            )
    if guidance:
        for request, event in zip(gateway.requests, injections(events), strict=True):
            assert event.payload['request_sha256'] == model_request_sha256(request)
    else:
        assert injections(events) == []
    assert plan.model_dump_json() == before


@pytest.mark.parametrize('guidance', [False, True])
async def test_tool_rounds_keep_contract_once_and_keep_authority(
    tmp_path: Path, guidance: bool,
) -> None:
    class StructuredToolGateway(ToolGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            if completion.response.tool_calls:
                return completion
            return replace(completion, response=replace(
                completion.response, text='{"summary":"tool-grounded result"}',
            ))

    context = await task(tmp_path)
    if not guidance:
        context = context.model_copy(update={'instruction_context': None})
    plan = one_step_plan(tools=('web.search',))
    plan = plan.model_copy(update={'agents': (
        plan.agents[0].model_copy(update={'output_schema': {'summary': 'string'}}),
    )})
    before = plan.model_dump_json()
    gateway, capabilities = StructuredToolGateway(), FakeCapabilities()
    runtime = CrewDispatchRuntime(
        gateway, plan, crew_factory=FastFactory(), capability_gateway=capabilities,
    )
    _ = [event async for event in runtime.run(context)]
    assert len(gateway.requests) == 2
    for request in gateway.requests:
        assert_contract(request)
        assert request.logical_model == 'general'
        assert request.tools == gateway.requests[0].tools
        assert request.required_capabilities == gateway.requests[0].required_capabilities
    assert capabilities.calls == [('writer', 'web.search')]
    assert plan.model_dump_json() == before


async def test_schema_message_size_rejected_before_gateway_and_running_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.crew import adapter

    context = (await task(tmp_path)).model_copy(update={'instruction_context': None})
    plan = one_step_plan()
    plan = plan.model_copy(update={'agents': (
        plan.agents[0].model_copy(update={'output_schema': {'summary': 'x' * 512}}),
    )})
    monkeypatch.setattr(adapter, '_MAX_PROMPT_BYTES', 768)
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match='limit'):
        async for event in runtime.run(context):
            events.append(event)
    assert gateway.requests == []
    assert injections(events) == []
    assert all(not event.checkpoint.state['models'] for event in events if event.checkpoint)


async def test_reviewer_contract_counts_toward_size_before_its_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.runtime.crew import adapter

    context = (await task(tmp_path)).model_copy(update={'instruction_context': None})
    plan = _reviewed_step_plan()
    baseline_gateway = FakeGateway()
    baseline = CrewDispatchRuntime(baseline_gateway, plan, crew_factory=FastFactory())
    _ = [event async for event in baseline.run(context)]
    _, review = baseline_gateway.requests[:2]
    assert review.logical_model == 'review'
    assert_contract(review)
    limit = sum(len(str(message.content).encode()) for message in review.messages) + 1024
    monkeypatch.setattr(adapter, '_MAX_PROMPT_BYTES', limit)
    monkeypatch.setattr(adapter, '_REVIEW_RESPONSE_SCHEMA', replace(
        adapter._REVIEW_RESPONSE_SCHEMA,
        schema={**adapter._REVIEW_RESPONSE_SCHEMA.schema, 'description': 'x' * limit},
    ))
    gateway = FakeGateway()
    runtime = CrewDispatchRuntime(gateway, plan, crew_factory=FastFactory())
    events = [event async for event in runtime.run(context)]
    assert all(request.logical_model != 'review' for request in gateway.requests)
    skipped = next(event for event in events if event.kind == 'review.completed')
    # The existing outer policy skips review failures; this test checks the submission boundary.
    assert skipped.payload['review_status'] == 'skipped'
    assert 'limit' in str(skipped.payload['warning'])
    assert all(entry['purpose'] != 'review'
               for event in events if event.checkpoint
               for entry in cast(Mapping[str, Mapping[str, JsonValue]],
                                 event.checkpoint.state['models']).values())


@pytest.mark.parametrize('guidance', [False, True])
async def test_prose_still_fails_without_fabricated_handoff(
    tmp_path: Path, guidance: bool,
) -> None:
    context = await task(tmp_path)
    if not guidance:
        context = context.model_copy(update={'instruction_context': None})
    gateway = SequenceGateway('Planner result: one-line reply GUIDE_APPLIED_TEST; verified.')
    runtime = CrewDispatchRuntime(
        gateway, _structured_dependent_final_plan(), crew_factory=FastFactory(),
    )
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError, match='structured handoff output is not valid json'):
        async for event in runtime.run(context):
            events.append(event)
    assert len(gateway.requests) == 1
    assert_contract(gateway.requests[0])
    assert not any(event.step_id == 'final_response' for event in events)
    assert not any(event.kind == 'step.completed' for event in events)
