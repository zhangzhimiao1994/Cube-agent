"""Original opened continuity evidence, with no PG/model/server execution."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType
from uuid import uuid4

import pytest

from agent_hub.db.models import RunAdmissionEventRow
from agent_hub.models import run_admission_repository as api
from tests.integration.models.continuity_seal_fixture import (
    SealStore,
    profile_value,
    untrusted_open,
)


def test_profile_hash_normalizes_frozen_and_mutable_JSON() -> None:
    profile = profile_value(datetime(2030, 1, 1, tzinfo=UTC))
    frozen = MappingProxyType({**profile, 'activation': MappingProxyType({'valid_until':
        datetime(2030, 1, 1, tzinfo=UTC).isoformat()}), 'modes': ('direct','dispatch','discuss','hybrid')})
    assert api.continuity_profile_fingerprint(profile) == api.continuity_profile_fingerprint(frozen)
    assert api.continuity_profile_fingerprint(profile) != api.continuity_profile_fingerprint({**profile, 'version': 1})


@pytest.mark.parametrize('profile', [None, [], {1:'bad'}, {'bad':float('nan')}, {'bad':object()},
                                    {'bad':'x'*20000}, {'bad':[0]*1024}])
def test_profile_hash_rejects_invalid_or_unbounded_JSON(profile: object) -> None:
    with pytest.raises(ValueError, match='invalid admission binding'):
        api.continuity_profile_fingerprint(profile)


async def test_v2_cannot_omit_both_new_arguments() -> None:
    store = SealStore()
    with pytest.raises(api.AdmissionConflict, match='admission state conflict'):
        await store.repository.open_pool(**store.opening())
    assert store.pool is None and store.events == []


@pytest.mark.parametrize('which', ['digest', 'expiry'])
async def test_cannot_supply_only_one_pair_field(which: str) -> None:
    store = SealStore()
    change: dict[str, object] = ({'continuity_profile_sha256':'a'*64} if which == 'digest'
                                else {'activation_valid_until':store.expiry})
    with pytest.raises(ValueError, match='invalid admission binding'):
        await untrusted_open(store.repository, {**store.opening(), **change})
    assert store.pool is None and store.events == [] and store.timeline == []


async def test_v2_rejects_forged_later_expiry_without_creating_pool() -> None:
    store = SealStore()
    with pytest.raises(api.AdmissionConflict, match='admission state conflict'):
        await untrusted_open(store.repository, {**store.opening(), 'continuity_profile_sha256':'a'*64,
                                                'activation_valid_until':store.expiry+timedelta(days=1)})
    assert store.pool is None and store.events == []


async def test_legacy_no_pair_call_keeps_original_opened_evidence() -> None:
    store = SealStore()
    store.run.routing_decision = {'runtime_token_absolute_tokens':100, 'runtime_plan_token_budget':100,
                                  'runtime_timeout_absolute_seconds':600}
    opened = await store.repository.open_pool(**store.opening())
    assert opened.original_token_grant == 100 and opened.token_charged == 0
    assert len(store.events) == 1 and store.events[0].kind == 'opened'
    assert 'continuity_profile_sha256' not in store.events[0].evidence
    assert 'admitted_at' not in store.events[0].evidence


async def test_first_open_records_actual_post_lock_clock_and_does_not_change_route() -> None:
    store = SealStore()
    before = deepcopy(store.run.routing_decision)
    digest = api.continuity_profile_fingerprint(profile_value(store.expiry))
    # The actual _fence reads this clock only after run/pool locks and original input.
    expected = store.now+timedelta(seconds=2)
    store.after_pool_read = lambda:setattr(store, 'now', expected)
    arguments: api._OpenArguments = {**store.opening(), 'continuity_profile_sha256':digest,
                                    'activation_valid_until':store.expiry}
    opened = await store.repository.open_pool(**arguments)
    assert store.timeline[:4] == ['run_lock','pool_lock','authorization','clock_timestamp']
    assert len(store.events) == 1
    assert store.events[0].evidence['continuity_profile_sha256'] == digest
    assert store.events[0].evidence['admitted_at'] == expected.isoformat()
    assert opened.token_charged == opened.review_count_charged == 0
    assert store.run.routing_decision == before
    assert await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id) == (digest, expected)


def sealed_arguments(store: SealStore) -> api._OpenArguments:
    return {**store.opening(), 'continuity_profile_sha256':api.continuity_profile_fingerprint(profile_value(store.expiry)),
            'activation_valid_until':store.expiry}


@pytest.mark.parametrize('change', [
    {'continuity_profile_sha256':None}, {'continuity_profile_sha256':True},
    {'continuity_profile_sha256':'A'*64}, {'continuity_profile_sha256':'a'*63},
    {'activation_valid_until':None}, {'activation_valid_until':'2030-01-01T00:00:30Z'},
    {'activation_valid_until':datetime(2030,1,1)},  # noqa: DTZ001 -- negative input
    {'activation_valid_until':datetime(2030,1,1,tzinfo=timezone(timedelta(hours=8)))},
])
async def test_invalid_pair_types_reject_before_storage(change: dict[str, object]) -> None:
    store = SealStore()
    with pytest.raises(ValueError, match='invalid admission binding'):
        await untrusted_open(store.repository, {**sealed_arguments(store), **change})
    assert store.timeline == [] and store.pool is None and store.events == []


@pytest.mark.parametrize('shift', [-1, 1, 86400])
async def test_correct_profile_sha_cannot_substitute_activation_expiry(shift: int) -> None:
    store = SealStore()
    arguments = sealed_arguments(store)
    arguments['activation_valid_until'] = store.expiry+timedelta(seconds=shift)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**arguments)
    assert store.pool is None and store.events == []


@pytest.mark.parametrize('late_by', [0, 0.000001, 5])
async def test_expiry_during_lock_wait_uses_actual_fence_clock_and_writes_nothing(late_by: float) -> None:
    store = SealStore()
    store.after_pool_read = lambda:setattr(store, 'now', store.expiry+timedelta(seconds=late_by))
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**sealed_arguments(store))
    assert store.timeline == ['run_lock','pool_lock','authorization','clock_timestamp']
    assert store.pool is None and store.events == []


@pytest.mark.parametrize('version', [1, True, '2', None])
async def test_pair_does_not_seal_a_non_v2_profile(version: object) -> None:
    store = SealStore()
    value = {**profile_value(store.expiry), 'version':version}
    assert store.run.routing_decision is not None
    store.run.routing_decision['approval_review_profile'] = value
    arguments = sealed_arguments(store)
    arguments['continuity_profile_sha256'] = api.continuity_profile_fingerprint(value)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**arguments)
    assert store.pool is None and store.events == []


async def test_profile_config_must_match_original_pool_source() -> None:
    store = SealStore()
    value = {**profile_value(store.expiry), 'config_sha256':'f'*64}
    assert store.run.routing_decision is not None
    store.run.routing_decision['approval_review_profile'] = value
    arguments = sealed_arguments(store)
    arguments['continuity_profile_sha256'] = api.continuity_profile_fingerprint(value)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**arguments)
    assert store.pool is None and store.events == []


async def test_event_flush_failure_rolls_back_pool_and_seal_together() -> None:
    store = SealStore()
    store.fail_event_flush = True
    before = deepcopy(store.run.routing_decision)
    with pytest.raises(api.AdmissionConflict, match='^admission state conflict$') as failure:
        await store.repository.open_pool(**sealed_arguments(store))
    assert 'PRIVATE_FLUSH_DETAILS' not in str(failure.value)
    assert store.timeline[-2:] == ['pool_flush','event_flush']
    assert store.pool is None and store.events == [] and store.run.routing_decision == before


async def test_in_session_open_never_commits_callers_transaction() -> None:
    store = SealStore()
    with pytest.raises(RuntimeError, match='caller rollback'):
        async with store.factory() as session, session.begin():
            await store.repository._open_pool_in_session(session, **sealed_arguments(store))
            assert store.pool is None and store.events == []
            raise RuntimeError('caller rollback')
    assert store.pool is None and store.events == []


async def test_existing_sealed_pool_reuses_original_admission_after_activation_expiry() -> None:
    store = SealStore()
    arguments = sealed_arguments(store)
    first = await store.repository.open_pool(**arguments)
    assert store.pool is not None
    store.pool.token_charged, store.pool.review_token_charged = 60, 20
    store.pool.review_count_charged = 1
    store.events.append(RunAdmissionEventRow(run_id=store.run.id, tenant_id=store.run.tenant_id,
        kind='unknown', attempt_id=uuid4(), evidence={'outcome':'unknown'}))
    prior = [(row.kind, deepcopy(row.evidence)) for row in store.events]
    store.now = store.expiry+timedelta(seconds=1)
    reopened = await store.repository.open_pool(**arguments)
    assert reopened.token_charged == 60 and reopened.review_token_charged == 20 and reopened.review_count_charged == 1
    assert reopened.original_deadline == first.original_deadline and reopened.original_token_grant == first.original_token_grant
    assert [(row.kind, row.evidence) for row in store.events] == prior
    assert await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id) == (
        arguments['continuity_profile_sha256'], datetime(2030,1,1,tzinfo=UTC))


async def test_existing_v2_still_cannot_omit_both_pair_arguments() -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    before = deepcopy(store.events[0].evidence)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**store.opening())
    assert store.events[0].evidence == before and len(store.events) == 1


async def test_existing_legacy_pool_cannot_be_backfilled_with_v2_seal() -> None:
    store = SealStore()
    assert store.run.routing_decision is not None
    store.run.routing_decision.pop('approval_review_profile')
    await store.repository.open_pool(**store.opening())
    before = deepcopy(store.events[0].evidence)
    store.run.routing_decision['approval_review_profile'] = profile_value(store.expiry)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**sealed_arguments(store))
    assert store.events[0].evidence == before and len(store.events) == 1
    assert await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id) is None


async def test_existing_profile_drift_cannot_replace_the_original_seal() -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    before = deepcopy(store.events[0].evidence)
    value = {**profile_value(store.expiry), 'catalog_version':12}
    assert store.run.routing_decision is not None
    store.run.routing_decision['approval_review_profile'] = value
    arguments = sealed_arguments(store)
    arguments['continuity_profile_sha256'] = api.continuity_profile_fingerprint(value)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**arguments)
    assert store.events[0].evidence == before and len(store.events) == 1


@pytest.mark.parametrize('field,value', [
    ('continuity_profile_sha256','e'*64), ('admitted_at','invalid'),
    ('admitted_at','2030-01-01T00:00:30+00:00'), ('admitted_at','2030-01-01T00:00:00'),
    ('authorization_turn_id',str(uuid4())), ('config_sha256','e'*64),
    ('original_token_grant',True), ('original_token_grant',101),
    ('review_count_limit',4), ('review_cost_limit','2'), ('original_deadline','2031-01-01T00:00:00+00:00'),
])
async def test_seal_reader_refuses_corrupt_opened_evidence(field: str, value: object) -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    store.events[0].evidence[field] = value
    with pytest.raises(api.AdmissionConflict, match='^admission state conflict$'):
        await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id)


@pytest.mark.parametrize('field', ['continuity_profile_sha256','admitted_at'])
async def test_partial_seal_is_never_an_admission(field: str) -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    store.events[0].evidence.pop(field)
    with pytest.raises(api.AdmissionConflict):
        await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id)


async def test_duplicate_opened_evidence_is_not_resolved_by_selecting_first() -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    store.events.append(RunAdmissionEventRow(run_id=store.run.id, tenant_id=store.run.tenant_id,
        kind='opened', attempt_id=None, evidence=deepcopy(store.events[0].evidence)))
    with pytest.raises(api.AdmissionConflict):
        await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id)


@pytest.mark.parametrize('defect', ['actor','turn','link','message_hash','scope_hash','attempt'])
async def test_seal_reader_checks_pool_and_authenticated_original_identity(defect: str) -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    assert store.pool is not None
    if defect == 'actor': store.pool.actor_id = uuid4()
    elif defect == 'turn': store.pool.authorization_turn_id = uuid4()
    elif defect == 'link': store.link.turn_id = uuid4()
    elif defect == 'message_hash': store.pool.message_sha256 = 'f'*64
    elif defect == 'scope_hash': store.pool.scope_sha256 = 'f'*64
    else: store.events[0].attempt_id = uuid4()
    with pytest.raises(api.AdmissionConflict):
        await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id)


async def test_seal_reader_is_scoped_and_historical_without_live_worker_fence() -> None:
    store = SealStore()
    assert await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id) is None
    arguments = sealed_arguments(store)
    await store.repository.open_pool(**arguments)
    assert await store.repository.get_continuity_seal(uuid4(), store.run.id) is None
    assert await store.repository.get_continuity_seal(store.run.tenant_id, uuid4()) is None
    store.run.status = 'cancelled'
    store.run.worker_id = None
    store.run.worker_lease_token = None
    store.run.worker_lease_expires_at = None
    store.now += timedelta(days=1)
    store.timeline.clear()
    assert await store.repository.get_continuity_seal(store.run.tenant_id, store.run.id) == (
        arguments['continuity_profile_sha256'], datetime(2030,1,1,tzinfo=UTC))
    assert 'clock_timestamp' not in store.timeline


def test_profile_hash_rejects_cycles_depth_and_total_bytes() -> None:
    cycle: dict[str, object] = {}
    cycle['self'] = cycle
    for profile in [cycle, {'values':['x'*4096]*5}, {'bad':'\ud800'}, {'number':1<<100}]:
        with pytest.raises(ValueError, match='invalid admission binding'):
            api.continuity_profile_fingerprint(profile)


@pytest.mark.parametrize('drop', [True, False])
async def test_existing_v2_seal_cannot_fall_back_by_dropping_or_downgrading_profile(drop: bool) -> None:
    store = SealStore()
    await store.repository.open_pool(**sealed_arguments(store))
    before = deepcopy(store.events[0].evidence)
    assert store.run.routing_decision is not None
    if drop:
        store.run.routing_decision.pop('approval_review_profile')
    else:
        store.run.routing_decision['approval_review_profile'] = {**profile_value(store.expiry),'version':1}
    with pytest.raises(api.AdmissionConflict):
        await store.repository.open_pool(**store.opening())
    assert store.events[0].evidence == before and len(store.events) == 1
