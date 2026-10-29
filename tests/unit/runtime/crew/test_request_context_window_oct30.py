from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_hub.models.gateway import ConservativeTokenEstimator
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredResponseSchema,
    ToolCall,
    ToolDefinition,
)
from agent_hub.runtime.crew.context_window import fit_request
from agent_hub.runtime.crew.native_history import tool_result_messages
from tests.unit.runtime.crew.test_native_tool_continuation import native


class Window:
    def __init__(self, tokens: int | None) -> None:
        self.tokens = tokens
        self.estimator = ConservativeTokenEstimator()

    def context_window_tokens(self, request: ModelRequest) -> int | None:
        return self.tokens

    def estimate_input_tokens(self, request: ModelRequest) -> int:
        return self.estimator.estimate_input(request)


def request(*messages: ModelMessage) -> ModelRequest:
    return ModelRequest(logical_model='general', messages=messages, max_output_tokens=256,
                        native_tool_continuation=any(m.native_assistant is not None for m in messages))


def feedback(label: str, count: int = 1000) -> ModelMessage:
    return ModelMessage('user', 'UNTRUSTED_CAPABILITY_RESULTS_JSON=' + json.dumps(
        [{'name': 'read_context', 'result': {'label': label, 'text': '{}[],:"' * count}}]))


def test_json_feedback_fits_actual_tokens_without_changing_contract() -> None:
    original = request(ModelMessage('system', 'Keep permissions and role.'),
                       ModelMessage('user', 'Validate the actual project.'),
                       feedback('older'), feedback('latest'))
    gateway = Window(13000)
    assert sum(len(m.content.encode()) for m in original.messages) < 13000 * 4 * 3 // 4
    assert gateway.estimate_input_tokens(original) >= 13000
    fitted = fit_request(original, gateway)
    assert gateway.estimate_input_tokens(fitted) < 13000
    assert original.messages[0] in fitted.messages and original.messages[1] in fitted.messages
    assert original.messages[-1] in fitted.messages and original.messages[-2] not in fitted.messages
    assert replace(fitted, messages=original.messages) == original
    assert any('EARLIER_TOOL_RESULTS_COMPACTED' in m.content for m in fitted.messages)


def test_schema_and_tools_are_part_of_the_same_estimate() -> None:
    original = replace(request(ModelMessage('system', 'role'), ModelMessage('user', 'task'),
                               feedback('old', 500), feedback('new', 250)),
        required_capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}),
        tools=(ToolDefinition(name='read_context', description='Inspect evidence',
                              parameters={'type':'object','properties':{'query':{'type':'string'}}}),),
        response_schema=StructuredResponseSchema(name='result', schema={'type':'object','properties':{'result':{'type':'string'}},'required':['result']}))
    gateway = Window(7000)
    assert gateway.estimate_input_tokens(original) >= 7000
    fitted = fit_request(original, gateway)
    assert gateway.estimate_input_tokens(fitted) < 7000
    assert fitted.tools is original.tools and fitted.response_schema is original.response_schema


def test_native_old_exchange_drops_as_a_whole_latest_pair_survives() -> None:
    old = native(ModelResponse(text='', tool_calls=(ToolCall(id='old', name='read_context', arguments={'query':'old'}),)))
    latest = native(ModelResponse(text='', tool_calls=(ToolCall(id='new', name='read_context', arguments={'query':'new'}),)))
    old_messages = tool_result_messages(old, [{'name':'read_context','result':{'text':'{}[],:"' * 1000}}])
    new_messages = tool_result_messages(latest, [{'name':'read_context','result':{'text':'fresh'}}])
    original = replace(request(ModelMessage('system','role'),ModelMessage('user','task'),*old_messages,*new_messages),native_tool_continuation=True)
    fitted = fit_request(original, Window(3000))
    assert all(m not in fitted.messages for m in old_messages)
    assert all(m in fitted.messages for m in new_messages)


def test_required_human_guidance_cannot_be_dropped_to_fit() -> None:
    original = request(ModelMessage('system','role'), ModelMessage('user','task'),
                       ModelMessage('system','HUMAN_AUTHORIZATION_BOUNDARY=' + '{}[],:"' * 500), feedback('latest', 1))
    with pytest.raises(ValueError, match='required model context'):
        fit_request(original, Window(500))


def test_already_fitting_or_unknown_window_preserves_exact_request() -> None:
    original = request(ModelMessage('system','role'),ModelMessage('user','task'),feedback('new',1))
    assert fit_request(original, Window(5000)) is original
    assert fit_request(original, Window(None)) is original
    assert fit_request(original, object()) is original


def test_irreducible_latest_exchange_is_rejected_before_submission() -> None:
    original = request(ModelMessage('system','role'),ModelMessage('user','task'),feedback('latest'))
    with pytest.raises(ValueError, match='required model context'):
        fit_request(original, Window(100))


def test_current_verification_receipt_is_retained_alongside_latest_read() -> None:
    verified = ModelMessage('user', 'UNTRUSTED_CAPABILITY_RESULTS_JSON=' + json.dumps(
        [{'name':'project.verify', 'result':{'status':'passed','tests':33,'receipt_sha256':'a'*64}}]))
    original = request(ModelMessage('system','role'),ModelMessage('user','task'),
                       feedback('old',1000),verified,feedback('latest',500))
    gateway = Window(6500)
    fitted = fit_request(original,gateway)
    assert gateway.estimate_input_tokens(fitted) < 6500
    assert verified in fitted.messages


def test_verification_is_not_discarded_when_only_reads_fit() -> None:
    verified = ModelMessage('user','UNTRUSTED_CAPABILITY_RESULTS_JSON=' + json.dumps(
        [{'name':'project.verify','result':{'status':'failed','diagnostic':'{}[],:"'*1000}}]))
    original = request(ModelMessage('system','role'),ModelMessage('user','task'),
                       feedback('old'),verified,feedback('latest',1))
    with pytest.raises(ValueError,match='required model context'):
        fit_request(original,Window(1500))


def test_old_read_can_leave_when_latest_result_is_verification() -> None:
    verified = ModelMessage('user','UNTRUSTED_CAPABILITY_RESULTS_JSON=' + json.dumps(
        [{'name':'project.verify','result':{'status':'passed','tests':33}}]))
    original = request(ModelMessage('system','role'),ModelMessage('user','task'),feedback('old'),verified)
    fitted = fit_request(original,Window(1000))
    assert verified in fitted.messages and original.messages[2] not in fitted.messages


def test_real_gateway_uses_its_admission_estimator_and_authorized_window() -> None:
    from tests.unit.runtime.crew.test_dynamic_content_limits import _configured_gateway

    gateway = _configured_gateway()
    original = request(ModelMessage('user', '{}[],:"' * 3000))
    assert gateway.context_window_tokens(original) == 32768
    assert gateway.estimate_input_tokens(original) == ConservativeTokenEstimator().estimate_input(original)


def test_changing_remaining_timeout_does_not_change_compressed_request_hash() -> None:
    from agent_hub.models.registry import ModelRegistry
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
    from tests.unit.runtime.crew.test_dynamic_content_limits import _configured_gateway

    def observation(label: str, count: int) -> ModelMessage:
        return ModelMessage('user','UNTRUSTED_CAPABILITY_RESULTS_JSON=' + json.dumps(
            [{'name':'read_context','result':{'label':label,'text':'{}[],:"'*count}}],separators=(',',':')))

    gateway = _configured_gateway()
    gateway._registry = ModelRegistry(tuple(replace(d,context_window_tokens=1505) for d in gateway._registry.deployments))
    original = replace(request(ModelMessage('system','keep role'),ModelMessage('user','task'),
        observation('a',100),observation('b',100),observation('latest',10)),timeout_seconds=100.0)
    retimed = replace(original,timeout_seconds=99.123456789)
    assert CrewDispatchRuntime._model_request_sha256(original) == CrewDispatchRuntime._model_request_sha256(retimed)
    first,second = fit_request(original,gateway),fit_request(retimed,gateway)
    assert first.messages != original.messages
    assert first.timeout_seconds == original.timeout_seconds and second.timeout_seconds == retimed.timeout_seconds
    assert CrewDispatchRuntime._model_request_sha256(first) == CrewDispatchRuntime._model_request_sha256(second)


async def test_fresh_policy_hashes_the_actual_request_and_restores_old_policy() -> None:
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository
    from agent_hub.runtime.contracts import EventKind
    from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        _workspace_delivery_bundle_response,
    )
    from tests.unit.runtime.crew.test_meaningful_workspace_progress import Observations, write
    from tests.unit.runtime.crew.test_native_post_tool_incomplete_oct29 import (
        FINAL,
        NativeGateway,
        context,
        runtime_for,
    )

    class EstimatedGateway(NativeGateway):
        estimates = 0

        def context_window_tokens(self, value: ModelRequest) -> int:
            return 1000000

        def estimate_input_tokens(self, value: ModelRequest) -> int:
            self.estimates += 1
            return ConservativeTokenEstimator().estimate_input(value)

    gateway = EstimatedGateway(native(write()), native(_workspace_delivery_bundle_response()), native(FINAL))
    private = InMemoryProviderContinuationRepository()
    artifacts = InMemoryArtifactRepository()
    runtime = runtime_for(gateway, Observations(), private, artifacts)
    task = context()
    events = [event async for event in runtime.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    assert checkpoint.state['context_window_compaction_version'] == 2
    assert gateway.estimates >= len(gateway.requests)
    for row in checkpoint.state['models'].values():
        assert row['request_sha256'] == runtime._model_request_sha256(gateway.requests[row['call_index']])

    for version in (0, 1, 2):
        state = dict(checkpoint.state)
        if version == 0:
            state.pop('context_window_compaction_version')
        else:
            state['context_window_compaction_version'] = version
        restored_checkpoint = type(checkpoint)(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
            runtime_version=checkpoint.runtime_version, run_id=checkpoint.run_id,
            tenant_id=checkpoint.tenant_id, mode=checkpoint.mode, state=state)
        unused = EstimatedGateway()
        restored = runtime_for(unused, Observations(), private, artifacts)
        await restored.restore_checkpoint(restored_checkpoint)
        restored_events = [event async for event in restored.run(task.model_copy(update={'checkpoint': restored_checkpoint}))]
        assert restored_events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert unused.requests == []
        saved = await restored.save_checkpoint()
        assert saved.state.get('context_window_compaction_version', 0) == version


async def test_recovery_reservation_binds_the_context_fitted_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.runtime.contracts import EventKind
    from agent_hub.runtime.crew import context_window
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        _workspace_delivery_bundle_response,
    )
    from tests.unit.runtime.crew.test_meaningful_workspace_progress import Observations, write
    from tests.unit.runtime.crew.test_native_post_tool_incomplete_oct29 import (
        FINAL,
        HANDOFF,
        NativeGateway,
        context,
        runtime_for,
    )

    # Force the boundary case where adding the recovery control needs another
    # projection. The pure tests above exercise the actual token calculation.
    def fit(value: ModelRequest, gateway: object, *, reserve_output: bool = False) -> ModelRequest:
        if (any('POST_TOOL_FINALIZATION:' in m.content for m in value.messages)
                and not any(m.content == 'CONTEXT_FIT_TEST_MARKER' for m in value.messages)):
            return replace(value, messages=(*value.messages, ModelMessage('system','CONTEXT_FIT_TEST_MARKER')))
        return value

    monkeypatch.setattr(context_window,'fit_request',fit)
    gateway = NativeGateway(native(write()), 'incomplete', native(HANDOFF),
                            native(_workspace_delivery_bundle_response()), native(FINAL))
    runtime = runtime_for(gateway,Observations())
    task = context()
    events = [event async for event in runtime.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    checkpoint = await runtime.save_checkpoint()
    repair = checkpoint.state['structured_repairs']['implementer_step']
    correction = checkpoint.state['models'][repair['correction_key']]
    assert repair['correction_request_sha256'] == correction['request_sha256']
    assert correction['request_sha256'] == runtime._model_request_sha256(gateway.requests[2])
    unused = NativeGateway()
    restored = runtime_for(unused, Observations(), runtime._provider_continuation_repository,
                           runtime._artifact_repository)
    await restored.restore_checkpoint(checkpoint)
    replay = [event async for event in restored.run(task.model_copy(update={'checkpoint':checkpoint}))]
    assert replay[-1].kind is EventKind.RUNTIME_COMPLETED
    assert unused.requests == []
