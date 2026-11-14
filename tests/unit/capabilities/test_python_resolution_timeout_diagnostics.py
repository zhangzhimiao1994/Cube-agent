"""Finite timeout diagnostics; no Root, network, DB or provider use."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from agent_hub.capabilities.python_resolution_diagnostics import (
    ResolutionPhase,
    ResolutionProgress,
    observe_resolution_timeout,
    resolution_timeout_payload,
)
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.failure_reason import safe_runtime_failure_diagnostic
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionError
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionFailureDiagnostic,
    ResolutionFailureError,
    ResolutionPlanning,
    ResolutionStatus,
    resolution_failure_diagnostic,
)


@pytest.mark.parametrize('offset,code', [(3600,'resolution_timeout'),(-1,'resolution_deadline')])
def test_root_timeout_requires_real_original_deadline(offset: int, code: str) -> None:
    d=resolution_failure_diagnostic(TimeoutError('password=PRIVATE'),phase='resolution',
        original_deadline=datetime.now(UTC)+timedelta(seconds=offset))
    assert d.code==code and d.timed_out is True
    assert 'PRIVATE' not in d.model_dump_json()


def test_unknown_cutoff_and_known_network_are_distinct() -> None:
    timeout=resolution_failure_diagnostic(TimeoutError('DNS PRIVATE'),phase='resolution')
    assert timeout.code=='resolution_timeout'
    leaf=ResolutionFailureDiagnostic(phase='registry_metadata',code='registry_dns',exit_code=2,timed_out=False)
    same=resolution_failure_diagnostic(ResolutionFailureError(leaf),phase='resolution',
        original_deadline=datetime.now(UTC)-timedelta(seconds=1))
    assert same==leaf
    with pytest.raises(ValueError):
        ResolutionStatus(state='resolved',key='a'*64,diagnostic=leaf)


def client(deadline: datetime) -> PythonResolutionPreparation:
    async def current() -> RunRecord | None:
        raise AssertionError('no actual authority/DB access in this boundary fake')
    return PythonResolutionPreparation(current=current,approved=None,original_deadline=deadline)


def test_outer_timeout_keeps_cancellation_and_no_unproved_completed_state() -> None:
    async def scenario() -> None:
        c=client(datetime.now(UTC)-timedelta(seconds=1))
        cancelled=[]
        async def guarded(*args: object, **kwargs: object) -> ResolutionStatus:
            try:
                await asyncio.Event().wait()
                raise AssertionError("unexpected completed wait")
            except asyncio.CancelledError as error:
                cancelled.append(error)
                raise
        with patch.object(c, "_send_guarded", guarded):
            with pytest.raises(TimeoutError) as caught:
                async with asyncio.timeout(0.01):
                    await c._send('resolve_python_inputs',needs=(),environment={},planning=cast(ResolutionPlanning, None))
            assert type(caught.value) is TimeoutError
            assert caught.value.__cause__ is cancelled[0]
            assert type(cancelled[0]) is asyncio.CancelledError
            payload=safe_runtime_failure_diagnostic(caught.value)
            assert payload['error_code']=='project_verification.resolution.resolution_deadline'
            assert payload['original_deadline_expired'] is True
            assert payload['resolution_progress']=='unobserved'
            assert payload['resolution_progress_evidence']=='none'
            assert payload['retryable'] is False
    asyncio.run(scenario())


def test_freeze_pending_only_after_guarded_root_resolution_return() -> None:
    async def scenario() -> None:
        from tests.unit.skills.sandbox.test_resolution_lease_witness import request

        first=request()
        c=client(datetime.now(UTC)+timedelta(hours=1))
        c._original=first.claim
        root_response=ResolutionStatus(state='resolved',key=first.key,descriptor={'validated-in-guarded-call':True})
        # This fake represents the original guarded method returning after all its validation.
        guarded=AsyncMock(return_value=root_response)
        with patch.object(c, '_send_guarded', guarded):
            result=await c._send('resolve_python_inputs',needs=first.needs,environment=first.environment,planning=first.claim.planning)
            assert result is root_response and guarded.await_count==1
            error=TimeoutError('https://private:password@host/generated_source.py')
            guarded.side_effect=error
            with pytest.raises(TimeoutError) as caught:
                await c._send('freeze_python_resolution',needs=first.needs,environment=first.environment,planning=first.claim.planning)
            assert caught.value is error and guarded.await_count==2
            payload=safe_runtime_failure_diagnostic(error)
            assert payload['resolution_phase']=='freeze'
            assert payload['resolution_progress']=='freeze_pending'
            assert payload['resolution_progress_evidence']=='validated_root_status'
            assert payload['error_code']=='project_verification.resolution.resolution_timeout'
            assert payload['original_deadline_expired'] is False
            assert not any(word in str(payload) for word in ['password@','generated_source.py','DNS','TLS'])
            assert payload['retryable'] is False
    asyncio.run(scenario())


def test_deadline_guard_error_is_preserved_without_retry() -> None:
    async def scenario() -> None:
        from tests.unit.skills.sandbox.test_resolution_lease_witness import request

        first=request()
        c=client(datetime.now(UTC)-timedelta(seconds=1))
        c._original=first.claim.model_copy(update={"original_deadline":datetime.now(UTC)-timedelta(seconds=1)})
        c._diagnostic_progress_key=first.key
        c._diagnostic_progress=ResolutionProgress.RESOLUTION_COMPLETED
        error=ResolutionError('resolution_deadline')
        guarded=AsyncMock(side_effect=error)
        with patch.object(c, '_send_guarded', guarded):
            with pytest.raises(ResolutionError) as caught:
                await c._send('freeze_python_resolution',needs=first.needs,environment=first.environment,planning=first.claim.planning)
            assert caught.value is error and guarded.await_count==1
            payload=safe_runtime_failure_diagnostic(error)
            assert payload['resolution_progress']=='freeze_pending' and payload['original_deadline_expired'] is True
    asyncio.run(scenario())


def test_unknown_result_never_establishes_completed_progress() -> None:
    async def scenario() -> None:
        c=client(datetime.now(UTC)+timedelta(hours=1))
        unknown=ResolutionStatus(state='unknown',key='a'*64)
        guarded=AsyncMock(return_value=unknown)
        with patch.object(c, "_send_guarded", guarded):
            assert await c._send('resolve_python_inputs',needs=(),environment={},planning=cast(ResolutionPlanning, None)) is unknown
            assert c._diagnostic_progress is ResolutionProgress.UNOBSERVED
            error=asyncio.CancelledError('PRIVATE')
            guarded.side_effect=error
            with pytest.raises(asyncio.CancelledError) as caught:
                await c._send('freeze_python_resolution',needs=(),environment={},planning=cast(ResolutionPlanning, None))
            assert caught.value is error
            assert resolution_timeout_payload(error)['resolution_progress']=='unobserved'
    asyncio.run(scenario())


def test_private_exception_text_and_forged_payload_do_not_cross_boundary() -> None:
    for error in [TimeoutError('password=PRIVATE'),ValueError('PRIVATE'),ResolutionError('registry_dns')]:
        vars(error)['_python_resolution_timeout_observation']={'error_summary':'PRIVATE'}
        assert resolution_timeout_payload(error)=={}
    error=TimeoutError('PRIVATE')
    error.__cause__=error
    assert resolution_timeout_payload(error)=={}
    observe_resolution_timeout(error,phase=ResolutionPhase.RESOLUTION,
        progress=ResolutionProgress.UNOBSERVED,original_deadline=None)
    payload=resolution_timeout_payload(error)
    assert payload['original_deadline_expired'] is None and 'PRIVATE' not in str(payload)




def test_fetch_timeout_does_not_claim_whole_run_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Existing native-boundary fake executes the actual fetch-unit cleanup code.
    from tests.unit.runs.test_resolution_failure_propagation import (
        test_fetch_unit_retains_leaf_only_after_confirmed_cleanup,
    )
    asyncio.run(test_fetch_unit_retains_leaf_only_after_confirmed_cleanup(
        monkeypatch, 'valid', True, 'resolution_timeout',
    ))
