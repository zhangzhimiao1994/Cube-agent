"""Strict persisted suggestion types, without DB/Root execution."""
import copy
from collections.abc import Awaitable
from dataclasses import replace

import pytest

from agent_hub.capabilities.python_project_lifecycle import (
    _historical_nonblocking_repair_suggestion,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runtime.contracts import RunEvent
from tests.unit.capabilities.test_python_expired_nonblocking_repair_nov07 import Session, run


@pytest.mark.parametrize(('field', 'value'), [
    ('attempt', True), ('attempt', 1.0), ('max_attempts', '3'),
    ('trigger', 17), ('action', False), ('severity', []), ('failure_category', {}),
    ('recovery_strategy', 17), ('orchestration_recovery_hint', {}),
    ('error_code', 17), ('suggested_action', ['unsafe']),
    ('role_capability_requirements', 'not-roles'),
    ('role_capability_requirements', [17]),
    ('role_capability_requirements', [None]),
])
def test_heterogeneous_persisted_payload_is_not_a_proved_suggestion(field: str, value: object) -> None:
    session = Session()
    session.events[0].payload['payload'][field] = copy.deepcopy(value)
    session.row.routing_decision['repair_proposal'][field] = copy.deepcopy(value)
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events[0].payload)

    async def check() -> bool:
        async with session, session.begin():
            # Deliberately heterogeneous row fields are not a typed RunRow.
            # Call the real validator without normalizing the rejected data.
            target: object = _historical_nonblocking_repair_suggestion
            assert callable(target)
            pending: object = target(session.sql_session, session.row, session.row.routing_decision)
            assert isinstance(pending, Awaitable)
            result: object = await pending
            assert isinstance(result, bool)
            return result

    assert run(check()) is False
    assert (vars(session.row), session.events[0].payload) == before


def test_persisted_suggestion_with_missing_mode_is_rejected() -> None:
    session = Session()
    session.row.mode = None
    session.events[0].payload['payload']['mode'] = None

    async def check() -> bool:
        async with session, session.begin():
            # Deliberately heterogeneous row fields are not a typed RunRow.
            # Call the real validator without normalizing the rejected data.
            target: object = _historical_nonblocking_repair_suggestion
            assert callable(target)
            pending: object = target(session.sql_session, session.row, session.row.routing_decision)
            assert isinstance(pending, Awaitable)
            result: object = await pending
            assert isinstance(result, bool)
            return result

    assert run(check()) is False


@pytest.mark.parametrize('with_optional_values', [False, True])
def test_producer_payload_with_valid_optional_fields_keeps_original_binding(with_optional_values: bool) -> None:
    session = Session()
    assert session.source is not None
    decision = classify_terminal_run(
        status=RunStatus.FAILED, mode=TaskMode.HYBRID, routing_decision={},
        events=(RunEvent.from_payload(session.source.payload),), policy=SelfRepairPolicy())
    assert decision is not None
    if with_optional_values:
        decision = replace(decision, recovery_strategy='manual',
            orchestration_recovery_hint='inspect', error_code='runtime.failed',
            suggested_action='review', role_capability_requirements=({'role': 'reader'},))
    session.events[0].payload = decision.to_event(run_id=session.owner, sequence=6).to_payload()
    session.row.routing_decision['repair_proposal'] = decision.to_proposal(run_id=session.owner)
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events[0].payload)

    async def check() -> bool:
        async with session, session.begin():
            # Deliberately heterogeneous row fields are not a typed RunRow.
            # Call the real validator without normalizing the rejected data.
            target: object = _historical_nonblocking_repair_suggestion
            assert callable(target)
            pending: object = target(session.sql_session, session.row, session.row.routing_decision)
            assert isinstance(pending, Awaitable)
            result: object = await pending
            assert isinstance(result, bool)
            return result

    assert run(check()) is True
    assert (vars(session.row), session.events[0].payload) == before
