from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from uuid import uuid4

import pytest

from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent


def diagnostic(phase: str = 'model_response_validation', kind: str = 'tool_call_limit') -> dict[str, JsonValue]:
    from agent_hub.runtime.failure_reason import discussion_failure_diagnostic

    return discussion_failure_diagnostic(phase=phase, kind=kind)


def test_discussion_diagnostic_frozen_event_json_and_public_roundtrip() -> None:
    payload = diagnostic()
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                     reason='discussion_failed', payload=MappingProxyType(payload))
    assert event.payload['discussion_failure_kind'] == 'tool_call_limit'
    assert event.payload['error_code'] == 'runtime.discussion_failed.model_response_validation.tool_call_limit'
    assert RunEvent.model_validate_json(event.model_dump_json()).payload == event.payload
    assert _public_event_payload(event.to_payload())['payload'] == payload


@pytest.mark.parametrize('change', [
    {'discussion_failure_phase': 'PRIVATE_PHASE'},
    {'discussion_failure_kind': 'PRIVATE_EXCEPTION'},
    {'discussion_failure_phase': 'framework', 'discussion_failure_kind': 'type_error'},
    {'error_code': 'runtime.discussion_failed'},
    {'error_summary': 'PRIVATE_BODY'},
    {'retryable': False},
    {'discussion_failure_kind': 'x' * 1000},
    {'discussion_failure_phase': True},
    {'api_key': 'PRIVATE_BODY'},
])
def test_discussion_diagnostic_rejects_unknown_or_misbound_fields(change: Mapping[str, JsonValue]) -> None:
    payload = {**diagnostic(), **change}
    with pytest.raises(ValueError):
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                 reason='discussion_failed', payload=payload)


@pytest.mark.parametrize('missing', ['discussion_failure_phase', 'discussion_failure_kind'])
def test_discussion_diagnostic_requires_both_fields(missing: str) -> None:
    payload = diagnostic()
    del payload[missing]
    with pytest.raises(ValueError):
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                 reason='discussion_failed', payload=payload)


def test_exception_kind_never_reads_exception_text_or_nested_cause() -> None:
    from agent_hub.runtime.failure_reason import discussion_exception_kind

    class Hostile(Exception):
        def __str__(self) -> str:
            raise AssertionError('exception text must not be read')

    error = Hostile()
    error.__cause__ = TypeError('PRIVATE_BODY')
    assert discussion_exception_kind(error) == 'unknown_error'
    assert discussion_exception_kind(TypeError('PRIVATE_BODY')) == 'type_error'
