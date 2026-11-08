"""Synthetic request growth and authoritative admission metadata; no provider or Redis."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import ConservativeTokenEstimator, GatewayCompletion, ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import ModelMessage, ModelRequest
from agent_hub.runtime import direct
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from tests.unit.models.test_gateway import (
    CapacityStub,
    SecretStub,
    TransportStub,
    deployment,
    request,
)
from tests.unit.runtime.test_direct_generated_entry import Model, response
from tests.unit.runtime.test_direct_original_entry_protocol_regression_nov07 import entry_context


class HandoffAdmissionModel(Model):
    def __init__(self, limit: int | None, window: int | None = None, factor: int = 1) -> None:
        super().__init__()
        self.limit, self.window, self.factor = limit, window, factor

    def admission_token_limit(self, value: ModelRequest) -> int | None:
        return self.limit

    def context_window_tokens(self, value: ModelRequest) -> int | None:
        return self.window

    def estimate_input_tokens(self, value: ModelRequest) -> int:
        return ConservativeTokenEstimator().estimate_input(value)

    def estimate_request_tokens(self, value: ModelRequest) -> int:
        return self.factor * self.estimate_input_tokens(value) + value.max_output_tokens


@pytest.mark.parametrize('hybrid', [True, False])
@pytest.mark.parametrize('failure_stage', ['history', 'output'])
def test_handoff_tries_smaller_owned_preview_after_local_capacity_rejection(
    hybrid: bool, failure_stage: str,
) -> None:
    from agent_hub.models.capacity import CapacityRequestTooLarge
    from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
    from agent_hub.runtime.autogen.context_budget import fit_request_history
    from agent_hub.runtime.request_admission import fit_output
    from tests.unit.runtime.autogen.test_hybrid_handoff_context_oct30 import plan, task

    context = task(hybrid=hybrid)
    owned = AutoGenDiscussionRuntime._task_text(context)
    original = ModelRequest(logical_model='analyst', max_output_tokens=512,
        messages=(ModelMessage('system', 'Keep original authority and protected tests.'),
                  ModelMessage('user', owned)))
    size = ConservativeTokenEstimator().estimate_input(original)
    model = HandoffAdmissionModel(size // 2 if failure_stage == 'history' else size * 2,
                                  factor=1 if failure_stage == 'history' else 3)
    if failure_stage == 'history':
        with pytest.raises(CapacityRequestTooLarge):
            fit_request_history(original, context_window_tokens=None,
                admission_token_limit=model.limit, estimate_input=model.estimate_input_tokens)
    else:
        assert fit_request_history(original, context_window_tokens=None,
            admission_token_limit=model.limit, estimate_input=model.estimate_input_tokens) is original
        with pytest.raises(CapacityRequestTooLarge):
            fit_output(model, original)
    hashes = tuple(item.content_sha256 for item in context.artifacts)
    runtime = AutoGenDiscussionRuntime(model, plan())
    fitted = runtime._fit_handoff_request(original, context, owned_task_positions=(1,), actor='analyst')
    assert fitted.messages[0] == original.messages[0] and fitted.messages[1] != original.messages[1]
    assert context.request in str(fitted.messages[1].content)
    assert replace(fitted, messages=original.messages, max_output_tokens=original.max_output_tokens) == original
    assert model.limit is not None and model.estimate_request_tokens(fitted) <= model.limit
    assert tuple(item.content_sha256 for item in context.artifacts) == hashes
    assert not model.requests


@pytest.mark.parametrize('restriction', ['admission', 'context', 'output'])
def test_handoff_exhaustion_retains_actual_local_limit_classification(restriction: str) -> None:
    from agent_hub.models.capacity import CapacityRequestTooLarge
    from agent_hub.models.gateway import ModelContextWindowExceeded
    from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
    from tests.unit.runtime.autogen.test_hybrid_handoff_context_oct30 import plan, task

    context = task().model_copy(update={'request': 'mandatory user scope ' * 1_000})
    owned = AutoGenDiscussionRuntime._task_text(context)
    original = ModelRequest(logical_model='analyst', messages=(ModelMessage('user', owned),))
    model = HandoffAdmissionModel(None if restriction == 'context' else
        (1_000 if restriction == 'admission' else 10_000_000),
        window=1_000 if restriction == 'context' else None,
        factor=10_000 if restriction == 'output' else 1)
    expected = ModelContextWindowExceeded if restriction == 'context' else CapacityRequestTooLarge
    with pytest.raises(expected):
        AutoGenDiscussionRuntime(model, plan())._fit_handoff_request(
            original, context, owned_task_positions=(0,), actor='analyst')
    assert not model.requests


@pytest.mark.parametrize('failure_stage', ['history', 'output'])
def test_handoff_does_not_continue_after_unknown_estimator_failure(failure_stage: str) -> None:
    from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
    from tests.unit.runtime.autogen.test_hybrid_handoff_context_oct30 import plan, task

    class UnknownEstimator(HandoffAdmissionModel):
        def estimate_input_tokens(self, value: ModelRequest) -> int:
            if failure_stage == 'history':
                raise RuntimeError('unknown estimator outcome must propagate')
            return super().estimate_input_tokens(value)

        def estimate_request_tokens(self, value: ModelRequest) -> int:
            raise RuntimeError('unknown estimator outcome must propagate')

    context = task()
    owned = AutoGenDiscussionRuntime._task_text(context)
    original = ModelRequest(logical_model='analyst', messages=(ModelMessage('user', owned),))
    model = UnknownEstimator(10_000_000)
    with pytest.raises(RuntimeError, match='unknown estimator outcome must propagate'):
        AutoGenDiscussionRuntime(model, plan())._fit_handoff_request(
            original, context, owned_task_positions=(0,), actor='analyst')
    assert not model.requests


def test_gateway_admission_bound_is_separate_from_context_and_does_no_io() -> None:
    selected = deployment('selected', tpm=100_000, context_window_tokens=1_048_576)
    shared = replace(selected, id='shared-peer', logical_model='unrelated', tpm=90_000)
    foreign = deployment('foreign', 'unrelated', tpm=10)
    capacity = CapacityStub([])
    gateway = ModelGateway(ModelRegistry([selected, shared, foreign]), capacity,
                           SecretStub(capacity.events), TransportStub(capacity.events))
    query = getattr(gateway, 'admission_token_limit', None)
    assert callable(query), 'The runtime needs the token-bucket bound before acquiring a lease'
    assert query(request()) == 90_000
    assert gateway.context_window_tokens(request()) == 1_048_576
    assert not capacity.events


class ReadOnlyFixture:
    def __init__(self, context: TaskContext, text: str) -> None:
        self.context, self.text = context, text
        self.calls: list[HarnessToolCallRequest] = []

    def is_available(self, tenant_id: object, name: str) -> bool:
        return True

    def is_replay_safe(self, name: str) -> bool:
        return False

    async def invoke(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                     user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        assert tenant_id == self.context.tenant_id and request.run_id == self.context.run_id
        assert user_id == self.context.actor_id and role == self.context.actor_role
        assert request.tool_name == 'read_context' and request.actor == 'main_agent'
        self.calls.append(request)
        return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                     status='succeeded', payload={
            'path': request.arguments['path'], 'text': self.text, 'truncated': False})

    async def execute(self, **kwargs: object) -> dict[str, JsonValue]:
        raise AssertionError('This growth regression must not execute a write or verification')


class AdmissionModel(Model):
    def admission_token_limit(self, request: ModelRequest) -> int:
        return 10_000

    def estimate_input_tokens(self, request: ModelRequest) -> int:
        return ConservativeTokenEstimator().estimate_input(request)


@pytest.mark.parametrize('window', [None, 1_048_576])
async def test_read_growth_fits_full_request_before_next_model_scope_without_mutating_files(window: int | None) -> None:
    context = entry_context().model_copy(update={'token_budget': 500_000,
        'routing_decision': {**entry_context().routing_decision,
                             'main_agent_context_window_tokens': window}})
    text = "value = '\\\"'\n" * 2_000
    harness = ReadOnlyFixture(context, text)
    batch = json.dumps({'workspace_batch': {'files': {}, 'read_paths': ['src/core.py'],
        'complete': False, 'continuation': 'Repair using this observed source.'}})
    model = AdmissionModel(response(batch), response('done'), response('done'))
    runtime = direct.DirectRuntime(model, logical_model='primary', capability_gateway=harness,
                                   harness_tool_gateway=harness)
    with pytest.raises(direct.RuntimeExecutionError, match='workspace batch is invalid'):
        _ = [event async for event in runtime.run(context)]
    assert len(model.requests) == 3 and len(harness.calls) == 1
    assert model.estimate_input_tokens(replace(model.requests[1], max_output_tokens=1)) < 10_000
    for index, value in enumerate(model.requests):
        assert model.estimate_input_tokens(replace(value, max_output_tokens=1)) < 10_000
        if index:
            assert ConservativeTokenEstimator().estimate(value) <= 10_000
        assert context.request in '\n'.join(str(message.content) for message in value.messages)
    assert harness.text == text and not any(call.tool_name != 'read_context' for call in harness.calls)
    assert context.token_budget == 500_000
    assert context.routing_decision['main_agent_context_window_tokens'] == window
    assert '"entries_complete":false' in '\n'.join(str(m.content) for m in model.requests[1].messages)


def test_action_checkpoint_gateway_forwards_pure_admission_metadata_before_journaling() -> None:
    model = AdmissionModel()
    harness = ReadOnlyFixture(entry_context(), 'unchanged')
    runtime = ActionApprovedDirectRuntime(model, logical_model='primary',
        artifact_repository=InMemoryArtifactRepository(), capability_gateway=harness,
        harness_tool_gateway=harness)
    query = getattr(runtime._gateway, 'admission_token_limit', None)
    estimator = getattr(runtime._gateway, 'estimate_input_tokens', None)
    assert callable(query) and callable(estimator)
    value = ModelRequest(logical_model='primary', messages=(ModelMessage(role='user', content='task'),))
    assert query(value) == 10_000
    assert estimator(value) == model.estimate_input_tokens(value)
    assert not model.requests and runtime._journal is None


def test_failed_verification_source_compacts_to_hashes_not_a_false_success() -> None:
    text = 'do not lose this acknowledged file\n' * 100
    evidence = {'tool_result': {'status': 'failed', 'reason': 'original_tests_failed'},
                'workspace_files': {'src/core.py': text}}
    value = ModelMessage(role='user', content='Keep protections. <UNTRUSTED_PROJECT_VERIFICATION_JSON>'
        + json.dumps(evidence) + '</UNTRUSTED_PROJECT_VERIFICATION_JSON> Preserve tests.')
    compact = getattr(direct, '_compact_workspace_feedback', None)
    assert callable(compact)
    output = compact(value)
    assert isinstance(output.content, str) and text not in output.content
    assert hashlib.sha256(text.encode()).hexdigest() in output.content
    assert 'original_tests_failed' in output.content and 'Preserve tests.' in output.content
    assert evidence['workspace_files']['src/core.py'] == text


@pytest.mark.parametrize('window', [None, 1_048_576])
def test_crew_uses_admission_bound_without_changing_context_or_latest_receipt(window: int | None) -> None:
    from agent_hub.runtime.crew.context_window import fit_request
    from tests.unit.runtime.crew.test_request_context_window_oct30 import Window, feedback

    class AdmissionWindow(Window):
        def admission_token_limit(self, value: ModelRequest) -> int:
            return 5_000

    gateway = AdmissionWindow(window)
    original = ModelRequest(logical_model='primary', messages=(ModelMessage('system', 'Keep actor and approval.'),
        ModelMessage('user', 'Repair original source; preserve tests.'), feedback('older'), feedback('latest', 1)))
    assert gateway.estimate_input_tokens(original) > 5_000
    fitted = fit_request(original, gateway)
    assert gateway.estimate_input_tokens(fitted) < 5_000
    assert all(message in fitted.messages for message in (*original.messages[:2], original.messages[-1]))
    assert original.messages[2] not in fitted.messages
    assert gateway.context_window_tokens(fitted) == window


@pytest.mark.parametrize('window', [None, 1_048_576])
async def test_discussion_compiles_admission_before_durable_intent_without_unknown_replay(window: int | None) -> None:
    from agent_hub.runtime.autogen.adapter import GatewayChatCompletionClient
    from agent_hub.runtime.autogen.termination import DiscussionUsage
    from tests.unit.runtime.autogen.test_context_budget_client import (
        WindowModel,
        large_request,
    )

    class AdmissionWindowModel:
        def __init__(self, backend: WindowModel) -> None:
            self.backend = backend

        def admission_token_limit(self, value: ModelRequest) -> int:
            return 5_000

        def context_window_tokens(self, value: ModelRequest) -> int | None:
            return window

        async def complete_with_context(self, value: ModelRequest) -> GatewayCompletion:
            return await self.backend.complete_with_context(value)

    from tests.unit.runtime.autogen.test_tool_correction_client import Journal

    journal, usage = Journal(), DiscussionUsage()
    backend = WindowModel(journal)
    backend.window = 1_048_576
    model = AdmissionWindowModel(backend)
    original = large_request()
    assert ConservativeTokenEstimator().estimate_input(original) > 5_000
    from tests.unit.runtime.autogen.test_tool_correction_client import TOOLS

    client = GatewayChatCompletionClient(model, 'shared', usage, durability=journal.durability(),
        supports_tool_calls=True, tool_correction_enabled=True,
        engineering_budget=lambda: (100 - usage.tokens, 5.0), engineering_context_compaction=True)
    await client._complete_model_request(original, TOOLS, None)
    fitted = backend.requests[0]
    assert ConservativeTokenEstimator().estimate_input(fitted) < 5_000
    assert fitted.messages[:2] == original.messages[:2] and fitted.messages[-2:] == original.messages[-2:]
    assert model.context_window_tokens(fitted) == window and backend.window == 1_048_576


def test_irreducible_user_instruction_fails_without_provider_or_quota_relaxation() -> None:
    model = AdmissionModel()
    context = entry_context().model_copy(update={'request': '{}[],:"' * 20_000,
                                               'token_budget': 500_000})
    runtime = direct.DirectRuntime(model, logical_model='primary')
    outcome = runtime._build_request(context)
    assert outcome.request is None and outcome.error_code == 'runtime context exceeds size limit'
    assert not model.requests and context.token_budget == 500_000
    assert model.admission_token_limit(request()) == 10_000


def test_compaction_never_rewrites_failed_tool_receipt_or_malformed_envelope() -> None:
    failed = {'status': 'failed', 'receipt': 'PRIVATE_FAILURE_BODY' * 100, 'passed': False}
    content = {'tool_result': failed, 'workspace_files': {'src/core.py': 'acknowledged source'}}
    start, end = '<UNTRUSTED_PROJECT_VERIFICATION_JSON>', '</UNTRUSTED_PROJECT_VERIFICATION_JSON>'
    output = direct._compact_workspace_feedback(ModelMessage('user', start + json.dumps(content) + end))
    assert isinstance(output.content, str)
    body = json.loads(output.content.split(start)[1].split(end)[0])
    assert body['tool_result'] == failed
    assert body['workspace_file_manifest']['content_included'] is False
    invalid = ModelMessage('user', start + '{"workspace_files":["not a file map"]}' + end)
    assert direct._compact_workspace_feedback(invalid) is invalid


def test_unknown_admission_metadata_keeps_legacy_shape_and_invalid_metadata_fails_closed() -> None:
    from agent_hub.runtime.request_admission import admission_token_limit, fit_output

    original = request()
    assert fit_output(object(), original) is original

    class InvalidBound:
        def admission_token_limit(self, value: ModelRequest) -> object:
            return True

    with pytest.raises(ValueError, match='invalid model admission limit'):
        admission_token_limit(InvalidBound(), original)


def native_exchange(label: str, repeats: int) -> tuple[ModelMessage, ModelMessage]:
    from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall

    native = NativeAssistantMessage('exact-deployment', '', (
        NativeToolCall(label, 'read_context', json.dumps({'path': 'src/core.py'})),),
        reasoning_content='synthetic-private-signature-' + label)
    return (ModelMessage('assistant', '', native_assistant=native),
            ModelMessage('tool', 'frozen data []{},:' * repeats, tool_call_id=label))


def test_native_discussion_drops_only_old_whole_exchange_and_preserves_latest_exact_bytes() -> None:
    from agent_hub.runtime.autogen.context_budget import fit_request_history

    old, latest = native_exchange('old-call', 2_000), native_exchange('latest-call', 1)
    original = ModelRequest(logical_model='primary', native_tool_continuation=True,
        messages=(ModelMessage('system', 'Keep actor and grants.'), ModelMessage('user', 'Preserve tests.'),
                  *old, *latest), max_output_tokens=100)
    fitted = fit_request_history(original, context_window_tokens=None, admission_token_limit=5_000)
    assert fitted.messages[:2] == original.messages[:2] and fitted.messages[-2:] == latest
    assert fitted.messages[-2].native_assistant is latest[0].native_assistant
    assert old[0] not in fitted.messages and old[1] not in fitted.messages
    assert 'synthetic-private-signature-old-call' not in '\n'.join(str(m.content) for m in fitted.messages)
    assert ConservativeTokenEstimator().estimate_input(fitted) < 5_000
    assert original.messages == (original.messages[0], original.messages[1], *old, *latest)


@pytest.mark.parametrize('defect', ['orphan', 'unclosed', 'latest-too-large'])
def test_native_discussion_invalid_or_mandatory_exchange_fails_closed(defect: str) -> None:
    from agent_hub.runtime.autogen.context_budget import ContextBudgetExceeded, fit_request_history

    old, latest = native_exchange('old-call', 2_000), native_exchange('latest-call', 2_000 if defect == 'latest-too-large' else 1)
    original = ModelRequest(logical_model='primary', native_tool_continuation=True,
        messages=(ModelMessage('system', 'Policy'), ModelMessage('user', 'Task'), *old, *latest))
    if defect == 'orphan':
        object.__setattr__(original, 'messages', (*original.messages[:2], old[1], *latest))
    elif defect == 'unclosed':
        object.__setattr__(original, 'messages', (*original.messages[:2], old[0], *latest))
    from agent_hub.models.capacity import CapacityRequestTooLarge

    with pytest.raises((ContextBudgetExceeded, CapacityRequestTooLarge)):
        fit_request_history(original, context_window_tokens=None, admission_token_limit=5_000)


def test_pure_direct_resume_compiler_preserves_failed_receipt_and_acknowledged_source() -> None:
    model = AdmissionModel()
    context = entry_context().model_copy(update={'token_budget': 500_000})
    runtime = direct.DirectRuntime(model, logical_model='primary')
    initial = runtime._build_request(context).request
    assert initial is not None
    current_source = 'acknowledged-current-core = []\n' * 1_000
    reads = direct.WorkspaceReadContext(max_bytes=500_000)
    records: list[dict[str, JsonValue]] = [
        {'path': f'src/other_{i}.py', 'text': 'cached observation []{},:\n' * 100,
         'truncated': False, 'sha256': hashlib.sha256(str(i).encode()).hexdigest()}
        for i in range(8)]
    reads.observe(records)
    before = reads.payload()
    evidence = {'tool_result': {'status': 'failed', 'reason': 'original_tests_failed'},
                'workspace_files': {'src/core.py': current_source}}
    progress = ModelMessage('user', '<UNTRUSTED_PROJECT_VERIFICATION_JSON>' + json.dumps(evidence)
        + '</UNTRUSTED_PROJECT_VERIFICATION_JSON> Preserve all four protected tests.')
    fitted, _ = runtime._compile_workspace_request(context, initial_request=initial,
        base_messages=initial.messages, progress_message=progress, read_context=reads,
        catalog_read=None, remaining_run_tokens=400_000, remaining_seconds=30)
    assert ConservativeTokenEstimator().estimate(fitted) <= 10_000
    assert not model.requests and reads.payload() == before and reads.retained_count == 8
    content = '\n'.join(str(m.content) for m in fitted.messages)
    assert current_source not in content and hashlib.sha256(current_source.encode()).hexdigest() in content
    assert 'original_tests_failed' in content and 'Preserve all four protected tests.' in content
    assert evidence['workspace_files']['src/core.py'] == current_source


def test_history_fitter_uses_supplied_actual_estimator_not_a_false_default_fit() -> None:
    from agent_hub.runtime.autogen.context_budget import fit_request_history
    from tests.unit.runtime.autogen.test_context_budget_client import large_request

    original = large_request()
    def actual(value: ModelRequest) -> int:
        return ConservativeTokenEstimator().estimate_input(value) * 3
    fitted = fit_request_history(original, context_window_tokens=None,
                                 admission_token_limit=10_000, estimate_input=actual)
    assert actual(fitted) <= 8_750
    assert fitted.messages[:2] == original.messages[:2] and fitted.messages[-2:] == original.messages[-2:]
