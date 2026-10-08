"""Integrity and lifetime checks for verified, run-local gateway scope carriers."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayScopeDiagnostic,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
)
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.crew import adapter
from tests.unit.runtime.crew.test_gateway_scope_diagnostic import (
    RAW_BODY,
    _context,
    _failure,
    _ReviewGateway,
    _runtime,
)

_REASON = "model gateway failed: model transport failed (status=408)"


def test_registered_carrier_dict_tampering_cannot_replace_verified_diagnostic() -> None:
    carrier = adapter._gateway_scope_failure(_REASON, _failure("deadline"))
    expected = {
        "gateway_scope_phase": "outer_deadline",
        "gateway_scope_reason": "deadline_exhausted",
        "gateway_scope_transport_entered_count": 0,
        "gateway_scope_failure_attempt_count": 0,
    }
    assert adapter._gateway_scope_payload(carrier) == expected
    forged = GatewayScopeDiagnostic(
        ScopeIncompletePhase.PRETRANSPORT_CAPACITY,
        ScopeIncompleteReason.CAPACITY_UNAVAILABLE,
        0,
        0,
    )
    carrier.__dict__.update(
        scope_diagnostic=forged,
        _gateway_scope_diagnostic=(object(), carrier, forged),
    )

    assert adapter._gateway_scope_payload(carrier) == expected
    propagated = adapter._gateway_scope_failure(_REASON, carrier)
    reviewed = adapter._gateway_scope_review_failure(_REASON, carrier)
    assert adapter._gateway_scope_payload(propagated) == expected
    assert adapter._gateway_scope_payload(reviewed) == expected


def test_registered_scope_carrier_is_collectible_after_last_owner_releases_it() -> None:
    carrier = adapter._gateway_scope_failure(_REASON, _failure("deadline"))
    assert adapter._gateway_scope_payload(carrier)["gateway_scope_phase"] == "outer_deadline"
    reference = weakref.ref(carrier)

    del carrier
    gc.collect()

    assert reference() is None


class _PausingReviewGateway(_ReviewGateway):
    def __init__(self) -> None:
        super().__init__("deadline")
        self.recovery_entered = asyncio.Event()

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        # Pause recovery after the worker succeeds and the first review failure is cached.
        if len(self.requests) == 2:
            self.recovery_entered.set()
            await asyncio.Event().wait()
        return await super().complete_with_context(request)


async def test_cancelled_run_clears_populated_gateway_scope_cache() -> None:
    gateway = _PausingReviewGateway()
    runtime = _runtime(
        gateway,
        repository=InMemoryArtifactRepository(),
        reviewer_retries=1,
    )
    stream = runtime.run(_context())
    assert isinstance(stream, adapter.CrewRunStream)
    cache = stream._state.gateway_scope_failures

    async def consume() -> None:
        async for _ in stream:
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(gateway.recovery_entered.wait(), timeout=5.0)
        assert len(gateway.requests) == 2
        assert cache
        for carrier in cache.values():
            assert adapter._gateway_scope_payload(carrier)["gateway_scope_phase"] == "outer_deadline"
            assert carrier.__traceback__ is None
            assert carrier.__cause__ is None
            assert carrier.__context__ is None
            assert RAW_BODY not in str(carrier) + repr(carrier) + repr(carrier.__dict__)

        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        assert cache == {}
        assert not stream._state.open
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()
