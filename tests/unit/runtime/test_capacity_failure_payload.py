"""Only native, typed capacity observations may enter durable runtime events."""
from __future__ import annotations

from uuid import uuid4

import pytest

from agent_hub.models.capacity import CapacityBackendError, CapacityPool
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.runtime import failure_reason as failure
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent
from tests.unit.models.test_capacity import InMemoryCapacityRedis, fingerprint
from tests.unit.models.test_capacity_diagnostic import FailingRedis
from tests.unit.models.test_gateway import SecretStub, TransportStub, deployment, request


def payload() -> dict[str, JsonValue]:
    return {
        **failure.runtime_failure_diagnostic_from_reason('model gateway failed: model capacity backend failed'),
        'gateway_scope_phase': 'pretransport_capacity',
        'gateway_scope_reason': 'capacity_backend_failure',
        'gateway_scope_transport_entered_count': 0, 'gateway_scope_failure_attempt_count': 0,
        'capacity_operation': 'acquire', 'capacity_backend_reason': 'timeout',
        'capacity_exception_category': 'builtin_timeout', 'capacity_timeout_origin': 'local_deadline',
        'capacity_commit_state': 'unknown',
    }


@pytest.mark.parametrize('kind', [EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED])
def test_typed_capacity_details_are_valid_runtime_failure_metadata(kind: EventKind) -> None:
    if kind is EventKind.STEP_FAILED:
        event = RunEvent(kind=kind, sequence=1, run_id=uuid4(), step_id='build', actor='implementer',
                         reason='model gateway failed: model capacity backend failed', payload=payload())
    else:
        event = RunEvent(kind=kind, sequence=1, run_id=uuid4(),
                         reason='model gateway failed: model capacity backend failed', payload=payload())
    assert event.payload['capacity_operation'] == 'acquire'
    assert RunEvent.from_payload(event.to_payload()) == event


@pytest.mark.parametrize('field,value', [
    ('capacity_operation', 'raw_private_body'), ('capacity_timeout_origin', 'secret'),
    ('capacity_commit_state', 'committed'), ('capacity_backend_reason', True),
])
def test_unknown_raw_capacity_values_are_rejected(field: str, value: JsonValue) -> None:
    with pytest.raises(ValueError):
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
                 reason='model gateway failed', payload={**payload(), field: value})


def test_capacity_details_require_complete_paired_scope() -> None:
    for removed in ('gateway_scope_phase', 'capacity_operation'):
        fields = payload()
        del fields[removed]
        with pytest.raises(ValueError):
            RunEvent(kind=EventKind.STEP_FAILED, sequence=1, run_id=uuid4(),
                     step_id='build', actor='implementer', reason='model gateway failed', payload=fields)


async def test_real_gateway_failure_survives_runtime_carrier_without_private_error() -> None:
    from agent_hub.runtime.crew.adapter import _gateway_scope_failure, _gateway_scope_payload

    selected = deployment('primary')
    capacity = CapacityPool(FailingRedis(5, TypeError('PRIVATE_PASSWORD')),
                            deployments=[selected], fingerprint_resolver=fingerprint)
    events: list[object] = []
    secrets, transport = SecretStub(events), TransportStub(events)
    gateway = ModelGateway(ModelRegistry([selected]), capacity, secrets, transport)
    with pytest.raises(CapacityBackendError) as caught:
        await gateway.complete(request())
    fields = failure.safe_runtime_failure_diagnostic(caught.value)
    assert fields['capacity_operation'] == 'acquire'
    assert fields['capacity_exception_category'] == 'type_error'
    wrapped = _gateway_scope_failure('model gateway failed: model capacity backend failed', caught.value)
    caught.value.__traceback__ = caught.value.__context__ = caught.value.__cause__ = None
    projected = _gateway_scope_payload(wrapped)
    assert projected['capacity_operation'] == 'acquire'
    assert 'PRIVATE_PASSWORD' not in str(projected) and not transport.calls
    event = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1, run_id=uuid4(),
        reason='model gateway failed: model capacity backend failed', payload={**fields, **projected})
    assert event.payload['capacity_commit_state'] == 'unknown'


@pytest.mark.parametrize('kind', [EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED])
async def test_native_fallback_backend_failure_preserves_first_capacity_gap(kind: EventKind) -> None:
    primary, backup = deployment('primary'), deployment('backup', 'backup')

    class BusyThenBrokenRedis(InMemoryCapacityRedis):
        primary_key: str
        busy_calls = 0
        failed_calls = 0

        async def eval(self, script: str, key_count: int, *args: object) -> object:
            if key_count == 5:
                if args[0] == self.primary_key:
                    self.busy_calls += 1
                    return [0, 1, 1, 1]
                self.failed_calls += 1
                raise TypeError('PRIVATE_PASSWORD')
            return await super().eval(script, key_count, *args)

    redis = BusyThenBrokenRedis()
    capacity = CapacityPool(redis, deployments=[primary, backup], fingerprint_resolver=fingerprint)
    redis.primary_key = capacity._keys(primary.quota_scope_id)['leases']
    calls: list[object] = []
    transport = TransportStub(calls)
    gateway = ModelGateway(ModelRegistry([primary, backup]), capacity, SecretStub(calls), transport,
                           fallbacks={'primary': 'backup'}, capacity_wait_timeout=0.3)
    with pytest.raises(CapacityBackendError) as caught:
        await gateway.complete(request())
    fields = failure.safe_runtime_failure_diagnostic(caught.value)
    assert redis.busy_calls > 0 and redis.failed_calls == 1 and not transport.calls
    assert fields['gateway_scope_reason'] == 'capacity_unavailable'
    assert fields['capacity_operation'] == 'acquire'
    assert fields['capacity_exception_category'] == 'type_error'
    assert fields['capacity_commit_state'] == 'unknown'
    event = RunEvent(kind=kind, sequence=1, run_id=uuid4(),
                     step_id='build' if kind is EventKind.STEP_FAILED else None,
                     actor='implementer' if kind is EventKind.STEP_FAILED else None,
                     reason='model gateway failed: model capacity backend failed', payload=fields)
    assert RunEvent.from_payload(event.to_payload()) == event
    assert 'PRIVATE_PASSWORD' not in str(event.to_payload())


def test_legacy_backend_failure_keeps_its_exact_payload() -> None:
    error = CapacityBackendError('private body')
    assert failure.safe_runtime_failure_diagnostic(error) == failure.runtime_failure_diagnostic_from_reason(
        failure.safe_runtime_failure_reason(error))


def test_unrelated_capacity_policy_metadata_keeps_legacy_event_semantics() -> None:
    event = RunEvent(kind=EventKind.MODEL_STARTED, sequence=1, run_id=uuid4(),
                     payload={'capacity_boundary': 'model_gateway'})
    assert event.payload['capacity_boundary'] == 'model_gateway'
