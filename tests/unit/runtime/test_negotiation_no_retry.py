from __future__ import annotations

import json

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.litellm_client import ModelNegotiationExhausted
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.crew.adapter import _can_compact_retry_subagent, _gateway_scope_failure
from agent_hub.runtime.direct import _retryable_gateway_failure
from agent_hub.runtime.failure_reason import (
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_reason,
)
from tests.unit.runtime.crew.test_gateway_scope_diagnostic import _failed_events, _Gateway, _runtime


@pytest.mark.parametrize('status', [503, None, 400])
def test_direct_preserves_negotiation_stop_boundary(status: int | None) -> None:
    assert not _retryable_gateway_failure(ModelNegotiationExhausted('PRIVATE_SECRET', status_code=status))


@pytest.mark.parametrize('status', [503, None, 400])
def test_negotiation_exhaustion_keeps_its_stop_semantics_after_runtime_translation(status: int | None) -> None:
    error = ModelNegotiationExhausted('PRIVATE_SECRET', status_code=status)
    wrapped = _gateway_scope_failure(safe_runtime_failure_reason(error), error)
    reason = safe_runtime_failure_reason(wrapped)
    diagnostic = runtime_failure_diagnostic_from_reason(reason)
    assert diagnostic['retryable'] is False
    assert diagnostic['error_code'] == 'model.request_negotiation_exhausted'
    assert diagnostic.get('status_code') == status
    assert not _can_compact_retry_subagent(diagnostic, recovery_attempt=0, remaining_seconds=120)
    assert not _can_compact_retry_subagent(diagnostic, recovery_attempt=1, remaining_seconds=120)
    assert 'PRIVATE_SECRET' not in reason + str(diagnostic)


@pytest.mark.parametrize('status', [503, None])
async def test_real_crew_does_not_compact_retry_or_replay_a_negotiated_failure(status: int | None) -> None:
    class Gateway(_Gateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            self.requests.append(request)
            raise ModelNegotiationExhausted('PRIVATE_SECRET', status_code=status)

    gateway = Gateway('provider')
    runtime = _runtime(gateway)
    events, error = await _failed_events(runtime)
    assert len(gateway.requests) == 1
    failures = [e for e in events if e.kind in {EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED}]
    assert failures and not any(e.kind is EventKind.STEP_RETRYING for e in events)
    step_failures = [e for e in failures if e.kind is EventKind.STEP_FAILED]
    assert step_failures and all(e.payload['error_code'] == 'model.request_negotiation_exhausted'
                                and e.payload['retryable'] is False for e in step_failures)
    assert all(runtime_failure_diagnostic_from_reason(e.reason)['retryable'] is False for e in failures)
    assert 'PRIVATE_SECRET' not in json.dumps([e.to_payload() for e in events], default=str) + str(error)
    checkpoint = await runtime.save_checkpoint()
    restored_gateway = Gateway('provider')
    restored = _runtime(restored_gateway)
    await restored.restore_checkpoint(checkpoint)
    await _failed_events(restored, checkpoint)
    assert restored_gateway.requests == []
