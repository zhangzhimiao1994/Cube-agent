"""Fixed evidence errors survive preview fitting; all execution uses offline fakes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_hub.runs import project_source_context
from agent_hub.runtime import python_project_history
from agent_hub.runtime.contracts import Artifact, RuntimeContractError
from tests.unit.runs.test_python_service_preparation import case


async def test_independent_64k_history_overflow_preserves_diagnostic_before_preview_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository, context, executor = await case(tmp_path)
    error = ValueError('python_project_history_budget_exceeded')

    def over_budget(original_context: Any, artifact: Artifact) -> Any:
        raise error

    def unexpected_read(*args: Any, **kwargs: Any) -> bytes:
        pytest.fail('fixed evidence must be checked before preview source reads')

    monkeypatch.setattr(project_source_context, '_read_no_links', unexpected_read)
    monkeypatch.setattr(python_project_history, 'build_python_history', over_budget)
    with pytest.raises(ValueError, match='^python_project_history_budget_exceeded$') as caught:
        await service._prepare_original_project_context(context)
    assert caught.value is error
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert len(executor.calls) == len(executor.probes) == 1


async def test_fixed_context_contract_error_survives_empty_preview_preflight(
    tmp_path: Path,
) -> None:
    _, preparer, repository, context, executor = await case(tmp_path)
    prepared = await preparer(context)
    # Existing evidence with the same deterministic ID cannot be appended again.
    resumed = context.model_copy(update={
        'artifacts': prepared.artifacts,
        'routing_decision': repository.row.routing_decision,
    }).validated_internal_clone()
    with pytest.raises(RuntimeContractError, match='^invalid runtime contract$'):
        await preparer(resumed)
    assert len(executor.calls) == len(executor.probes) == 1


@pytest.mark.parametrize('error', [
    ValueError('python_project_history_invalid'),
    TypeError('fixed history schema invalid'),
    RuntimeContractError('invalid runtime contract'),
    ValidationError.from_exception_data('history', [{
        'type': 'value_error', 'loc': ('scope',), 'input': None,
        'ctx': {'error': ValueError('history scope mismatch')},
    }]),
    ValidationError.from_exception_data('history', [{
        'type': 'value_error', 'loc': ('files',), 'input': None,
        'ctx': {'error': ValueError('JSON value exceeds structural limits')},
    }, {
        'type': 'value_error', 'loc': ('scope',), 'input': None,
        'ctx': {'error': ValueError('history scope mismatch')},
    }]),
])
async def test_nonbudget_candidate_failure_is_not_hidden_by_omitting_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    _, preparer, repository, context, executor = await case(tmp_path)
    original = python_project_history.build_python_history

    def history_with_failure(original_context: Any, artifact: Artifact) -> Any:
        history = original(original_context, artifact)
        preview = artifact.content['source_context']
        assert isinstance(preview, Mapping)
        if preview['included_count']:
            raise error
        return history

    monkeypatch.setattr(python_project_history, 'build_python_history', history_with_failure)
    with pytest.raises(type(error)) as caught:
        await preparer(context)
    assert caught.value is error
    assert repository.row.routing_decision['project_input_preparation']['phase'] == 'observed'
    assert len(executor.calls) == len(executor.probes) == 1
