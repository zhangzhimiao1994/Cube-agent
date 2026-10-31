from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityRequestTooLarge,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.types import ModelMessage, ModelRequest
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.direct_completion_journal import DirectCompletionJournal, DirectJournalError
from agent_hub.runtime.failure_reason import (
    safe_model_gateway_failure_reason,
    safe_runtime_failure_diagnostic,
)


@pytest.mark.parametrize('failure_type', [
    CapacityRequestTooLarge, CapacityQueueFull, CapacityWaitTimeout,
    CapacityUnavailable, CapacityBackendError, CapacityConfigurationError,
])
async def test_journal_preserves_capacity_classification_without_private_error_or_replay(failure_type):
    private = 'PRIVATE redis://password@private.invalid provider body'
    original = failure_type(private)
    provider = AsyncMock(side_effect=original)
    request = ModelRequest(logical_model='primary', messages=(ModelMessage(role='user', content='Build'),))
    journal = DirectCompletionJournal(InMemoryArtifactRepository(), tenant_id=uuid4(), run_id=uuid4())

    with pytest.raises(failure_type) as caught:
        await journal.complete(request, provider)

    assert caught.value is not original
    assert safe_model_gateway_failure_reason(caught.value) == safe_model_gateway_failure_reason(original)
    assert private not in str(caught.value)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert journal.references == () and not journal.replay_complete
    with pytest.raises(DirectJournalError, match='direct continuation is unavailable'):
        await journal.complete(request, provider)
    provider.assert_awaited_once()


async def test_journal_still_redacts_unknown_failure_and_forbids_replay():
    provider = AsyncMock(side_effect=RuntimeError('PRIVATE source and credential'))
    request = ModelRequest(logical_model='primary', messages=(ModelMessage(role='user', content='Build'),))
    journal = DirectCompletionJournal(InMemoryArtifactRepository(), tenant_id=uuid4(), run_id=uuid4())
    with pytest.raises(DirectJournalError, match='direct continuation is unavailable') as caught:
        await journal.complete(request, provider)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert 'PRIVATE' not in str(caught.value)
    assert not journal.replay_complete and journal.references == ()


async def test_journal_never_calls_unknown_exception_metaclass_equality():
    class ComparisonTrap(type):
        def __eq__(cls, other):
            raise RuntimeError('PRIVATE metaclass comparison')

    class UnknownFailure(Exception, metaclass=ComparisonTrap):
        pass

    provider = AsyncMock(side_effect=UnknownFailure('PRIVATE model body'))
    request = ModelRequest(logical_model='primary', messages=(ModelMessage(role='user', content='Build'),))
    journal = DirectCompletionJournal(InMemoryArtifactRepository(), tenant_id=uuid4(), run_id=uuid4())
    with pytest.raises(DirectJournalError) as caught:
        await journal.complete(request, provider)
    assert str(caught.value) == 'direct continuation is unavailable'
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert not journal.replay_complete


async def test_direct_runtime_reports_journal_capacity_failure_instead_of_generic_gateway():
    run_id, tenant_id = uuid4(), uuid4()
    journal = DirectCompletionJournal(InMemoryArtifactRepository(), tenant_id=tenant_id, run_id=run_id)
    provider = AsyncMock(side_effect=CapacityRequestTooLarge('PRIVATE backend detail'))

    class JournalGateway:
        async def complete_with_context(self, request):
            return await journal.complete(request, provider)

    runtime = DirectRuntime(JournalGateway(), logical_model='primary', available_model_attempts=1)
    context = TaskContext(run_id=run_id, tenant_id=tenant_id, mode=TaskMode.DIRECT,
                          token_budget=50000, request='Answer briefly.')
    with pytest.raises(RuntimeExecutionError) as caught:
        _ = [event async for event in runtime.run(context)]
    fields = safe_runtime_failure_diagnostic(caught.value)
    assert fields['error_code'] == 'model.capacity_request_too_large'
    assert fields['error_stage'] == 'model_capacity'
    assert fields['retryable'] is False
    assert 'PRIVATE' not in str(caught.value)
    provider.assert_awaited_once()
    assert not journal.replay_complete
