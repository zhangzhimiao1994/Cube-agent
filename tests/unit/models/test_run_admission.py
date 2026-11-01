"""Storage admission boundaries: no transport or permission grant."""
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import ModuleType
from uuid import uuid4

import pytest

from agent_hub.models import run_admission_repository as admission_api
from tests.unit.models.review_admission_contract_typed_fixture import (
    invalid_storage_call,
    object_map,
    present,
    raw_call,
    raw_lease,
    raw_usage,
    string,
)


def contracts() -> ModuleType:
    # A missing implementation is an assertion failure during the first RED run.
    name = 'agent_hub.models.run_admission_repository'
    assert importlib.util.find_spec(name) is not None, 'Task2B admission storage is missing'
    return importlib.import_module(name)


def test_private_lease_and_strict_identity() -> None:
    c = admission_api
    assert contracts() is c
    token = uuid4()
    lease = c._WorkerLease('worker-1', token)
    assert str(token) not in repr(lease)
    for worker, secret in [('', token), ('x' * 129, token), ('worker', str(token)),
                           ('worker', True)]:
        with pytest.raises(ValueError, match='invalid admission binding'):
            raw_lease(worker, secret)
    deployment = c.DeploymentIdentity('provider', 'deployment', 'model')
    for field in ('provider_id', 'deployment_id', 'model_id'):
        for value in ('', True, 'a' * 129, 'Bearer secret', 'https://x:y@host'):
            with pytest.raises(ValueError, match='invalid admission binding'):
                raw_call(replace, deployment, **{field: value})


@pytest.mark.parametrize('value', [True, False, -1, 0, 1.0, '1', 10_000_001])
def test_token_holds_are_strict_positive_bounded_integers(value: object) -> None:
    c = admission_api
    assert contracts() is c
    with pytest.raises(ValueError, match='invalid admission binding'):
        c._token_hold(value)


@pytest.mark.parametrize('value', [True, -1, Decimal('NaN'), Decimal('Infinity'),
    Decimal('-0.1'), Decimal('1000000.000001'), Decimal('0.0000001'), '0.1', 0.1])
def test_money_requires_finite_bounded_decimal(value: object) -> None:
    c = admission_api
    assert contracts() is c
    with pytest.raises(ValueError, match='invalid admission binding'):
        c._cost(value)


@pytest.mark.parametrize('value', ['', 'A' * 64, 'a' * 63, 'g' * 64, True, None])
def test_digest_rejects_noncanonical_values(value: object) -> None:
    c = admission_api
    assert contracts() is c
    with pytest.raises(ValueError, match='invalid admission binding'):
        c._digest(value)


@pytest.mark.parametrize('prompt,completion,total,cost,valid', [
    (2, 3, 5, Decimal('.01'), True), (0, 0, 0, Decimal(0), True),
    (True, 0, 1, Decimal(0), False), (-1, 2, 1, Decimal(0), False),
    (1, 2, 4, Decimal(0), False), (None, 2, 2, Decimal(0), False),
    (1, 2, 3, Decimal('NaN'), False), (1, 2, 3, None, False),
])
def test_untrusted_observation_is_retained_but_not_known_usage(prompt: object, completion: object, total: object, cost: object, valid: bool) -> None:
    c = admission_api
    assert contracts() is c
    observation = raw_usage(prompt, completion, total, cost)
    assert c._known_usage(observation) is valid
    assert present(c._observation(observation))['prompt_tokens'] is not None or prompt is None


def execution_binding(c: ModuleType) -> dict[str, object]:
    return {'tenant_id': uuid4(), 'run_id': uuid4(), 'authorization_turn_id': uuid4(),
        'lease': admission_api._WorkerLease('worker', uuid4()), 'config_sha256': 'c' * 64}


@pytest.mark.parametrize('change', [
    {'original_token_grant': True}, {'original_token_grant': -1},
    {'review_token_limit': 101}, {'review_token_limit': False},
    {'review_count_limit': True}, {'review_count_limit': 0},
    {'review_count_limit': 2**63}, {'review_cost_limit': Decimal('Infinity')},
    {'original_deadline': datetime(2030, 1, 1)},  # noqa: DTZ001 -- deliberately invalid naive input
    {'original_deadline': datetime(2030, 1, 1, tzinfo=UTC).astimezone(
        timezone(timedelta(hours=8)))},
    {'authorization_turn_id': 'not-a-uuid'}, {'config_sha256': 'A' * 64},
])
async def test_invalid_open_arguments_reject_before_storage(change: dict[str, object]) -> None:
    c = admission_api
    assert contracts() is c
    arguments = {**execution_binding(c), 'original_token_grant': 100,
        'original_deadline': datetime(2030, 1, 1, tzinfo=UTC), 'review_token_limit': 80,
        'review_count_limit': 2, 'review_cost_limit': Decimal(1), **change}
    # None is deliberately not a session: invalid input must never touch storage.
    with pytest.raises(ValueError, match='invalid admission binding'):
        await invalid_storage_call("open", arguments)


@pytest.mark.parametrize('change', [
    {'purpose': 'other'}, {'purpose': True}, {'token_hold': True},
    {'request_sha256': 'x' * 64}, {'attempt_id': 'not-a-uuid'},
    {'deployment': 'logical-model'}, {'review_cost_hold': Decimal('.1')},
    {'purpose': 'review', 'review_cost_hold': None},
    {'purpose': 'review', 'review_cost_hold': Decimal('NaN')},
])
async def test_invalid_reservation_arguments_never_hold_tokens(change: dict[str, object]) -> None:
    c = admission_api
    assert contracts() is c
    arguments = {**execution_binding(c), 'attempt_id': uuid4(), 'purpose': 'primary',
        'request_sha256': 'a' * 64, 'deployment': c.DeploymentIdentity('p', 'd', 'm'),
        'token_hold': 10, 'review_cost_hold': None, **change}
    with pytest.raises(ValueError, match='invalid admission binding'):
        await invalid_storage_call("reserve", arguments)


def test_malformed_unicode_usage_can_be_recorded_without_skipping_fail_closed_settlement() -> None:
    c = admission_api
    assert contracts() is c
    usage = raw_usage(1, 1, 2, '\ud800' * 1025)
    assert not c._known_usage(usage)
    observation = present(c._observation(usage))
    assert object_map(observation['cost_usd'])['invalid_length'] == 1025
    assert len(string(object_map(observation['cost_usd'])['sha256'])) == 64


@pytest.mark.parametrize('raw', ['\x00', '\ud800'])
def test_invalid_usage_string_is_losslessly_safe_for_jsonb(raw: str) -> None:
    c = admission_api
    assert contracts() is c
    usage = raw_usage(1, 1, 2, raw)
    assert not c._known_usage(usage)
    stored = object_map(present(c._observation(usage))['cost_usd'])
    assert bytes.fromhex(string(stored['invalid_string_utf8_hex'])) == raw.encode('utf-8', 'surrogatepass')


def test_oversize_observations_keep_distinct_receipt_identity() -> None:
    c = admission_api
    assert contracts() is c
    first = raw_usage(1 << 4096, 0, 1 << 4096, Decimal(0))
    second = raw_usage((1 << 4096) + 1, 0, (1 << 4096) + 1, Decimal(0))
    assert c._observation(first) != c._observation(second)


@pytest.mark.parametrize('usage,broken', [
    ((60, 1, 61, None), True),
    ((2**63, 0, 2**63, None), True),
    ((None, None, None, Decimal('.6')), True),
    ((True, 0, 61, Decimal('.1')), False),
    ((1, 1, 2, Decimal('NaN')), False),
    ((1, 1, 2, Decimal('.1')), False),
])
def test_valid_partial_observation_can_prove_bound_broken(usage: tuple[object, object, object, object], broken: bool) -> None:
    c = admission_api
    assert contracts() is c
    assert hasattr(c, '_over_bound_usage'), 'independent bound evidence validation is missing'
    assert c._over_bound_usage(raw_usage(*usage), 60, Decimal('.5')) is broken


@pytest.mark.parametrize('cost,broken', [
    (Decimal('0.6000000'), True), (Decimal(1_000_001), True),
    (Decimal('0.1000000'), False), (Decimal('NaN'), False),
    (Decimal('Infinity'), False), (Decimal('-0.6'), False),
])
def test_actual_cost_bound_comparison_is_independent_of_hold_storage_limits(cost: object, broken: bool) -> None:
    c = admission_api
    assert contracts() is c
    usage = raw_usage(1, 1, 2, cost)
    assert c._over_bound_usage(usage, 60, Decimal('.5')) is broken
    # Storage limits still reject these as new grants/holds, including finer precision.
    with pytest.raises(ValueError, match='invalid admission binding'):
        c._cost(cost)
