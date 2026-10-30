from __future__ import annotations

import asyncio
import time
from uuid import UUID

import httpx
import pytest

from agent_hub.models.physical_admission import (
    _bind_physical_admission,
    _GuardedTransport,
    _PhysicalAdmissionScope,
    _PhysicalAdmissionStopped,
    _PhysicalAttemptMetadata,
    _PhysicalCall,
    _PhysicalPermit,
    _PhysicalReceipt,
)
from agent_hub.models.types import Deployment, TokenUsage


class Admission:
    def __init__(self) -> None:
        self.metadata: list[_PhysicalAttemptMetadata] = []
        self.receipts: list[tuple[_PhysicalPermit, _PhysicalReceipt]] = []
        self.deadlines: list[float] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.failure: Exception | None = None
        self.receipt_failure = False

    async def before_send(
        self, meta: _PhysicalAttemptMetadata, *, deadline: float,
    ) -> _PhysicalPermit:
        self.metadata.append(meta)
        self.deadlines.append(deadline)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure:
            raise self.failure
        return _PhysicalPermit(UUID(int=len(self.metadata)))

    async def after_receipt(
        self, permit: _PhysicalPermit, receipt: _PhysicalReceipt, *, deadline: float,
    ) -> None:
        self.deadlines.append(deadline)
        if self.receipt_failure:
            raise RuntimeError("private accounting failure")
        self.receipts.append((permit, receipt))


class RemainingAdmission(Admission):
    """Local adapter fixture: UNKNOWN keeps its hold, every permit reserves anew."""

    def __init__(self) -> None:
        super().__init__()
        self.remaining = 200
        self.holds: dict[UUID, int] = {}
        self.reserve_calls = 0

    async def before_send(
        self, meta: _PhysicalAttemptMetadata, *, deadline: float,
    ) -> _PhysicalPermit:
        self.reserve_calls += 1
        if self.remaining < 100:
            raise RuntimeError("remaining budget exhausted")
        permit = await super().before_send(meta, deadline=deadline)
        self.remaining -= 100
        self.holds[permit.attempt_id] = 100
        return permit


def admission_scope(admission: Admission | None = None, **kwargs: object) -> _PhysicalAdmissionScope:
    return _PhysicalAdmissionScope(
        admission or Admission(), **{
            "request_sha256": "a" * 64, "purpose": "review",
            "deadline": asyncio.get_running_loop().time() + 10, **kwargs,
        },
    )


def physical_call(scope: _PhysicalAdmissionScope) -> _PhysicalCall:
    return _PhysicalCall(scope, deployment_id="primary", provider_model="openai/test-model",
                         protocol="chat_completions", stream=False)


async def test_concurrent_sends_share_one_atomic_scope() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(200)

    calls = [physical_call(scope), physical_call(scope)]
    results = await asyncio.gather(*[
        _GuardedTransport(httpx.MockTransport(handle), call).handle_async_request(
            httpx.Request("POST", "https://mock.example"),
        ) for call in calls
    ], return_exceptions=True)
    assert len(wire) == 1
    assert sum(isinstance(r, _PhysicalAdmissionStopped) for r in results) == 1
    assert len(admission.metadata) == 1
    assert len(scope.attempts) == 1


@pytest.mark.parametrize("stop", ["revoke", "deadline", "cancel"])
async def test_after_admission_rechecks_before_delegate(stop: str) -> None:
    admission = Admission()
    admission.release = asyncio.Event()
    scope = admission_scope(admission, deadline=asyncio.get_running_loop().time() + 0.05
                            if stop == "deadline" else asyncio.get_running_loop().time() + 10)
    call = physical_call(scope)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(200)

    task = asyncio.create_task(_GuardedTransport(httpx.MockTransport(handle), call)
                               .handle_async_request(httpx.Request("POST", "https://mock.example")))
    await admission.entered.wait()
    if stop == "revoke":
        scope.revoke()
    elif stop == "deadline":
        await asyncio.sleep(0.06)
    else:
        task.cancel()
    admission.release.set()
    with pytest.raises((_PhysicalAdmissionStopped, asyncio.CancelledError)):
        await task
    assert wire == []
    assert scope.stopped is not None
    if stop == "revoke":
        assert len(scope.attempts) == 1  # issued is retained even without delegation


async def test_admission_failure_is_latched_and_redacted() -> None:
    admission = Admission()
    admission.failure = RuntimeError("secret DB URL")
    scope = admission_scope(admission)
    guard = _GuardedTransport(httpx.MockTransport(lambda r: pytest.fail("no network")),
                              physical_call(scope))
    for _ in range(2):
        with pytest.raises(_PhysicalAdmissionStopped) as caught:
            await guard.handle_async_request(httpx.Request("POST", "https://mock.example"))
        assert "secret" not in str(caught.value)
    assert len(admission.metadata) == 1


async def test_scope_exit_revokes_inherited_child_context() -> None:
    scope = admission_scope()
    ready = asyncio.Event()

    async def child() -> None:
        await ready.wait()
        scope.check()

    with _bind_physical_admission(scope):
        task = asyncio.create_task(child())
    ready.set()
    with pytest.raises(_PhysicalAdmissionStopped):
        await task


async def test_review_constructor_enforces_version_one_attempt_contract() -> None:
    with pytest.raises(ValueError):
        admission_scope(max_attempts=2)
    assert admission_scope(purpose="primary", max_attempts=2).stopped is None


async def test_returned_permit_after_deadline_is_retained_without_delegate() -> None:
    class LateAdmission(Admission):
        async def before_send(
            self, meta: _PhysicalAttemptMetadata, *, deadline: float,
        ) -> _PhysicalPermit:
            permit = await super().before_send(meta, deadline=deadline)
            # Simulate an adapter completing synchronously after the deadline;
            # timeout cancellation cannot run until the loop regains control.
            time.sleep(max(0, deadline - asyncio.get_running_loop().time()) + 0.01)  # noqa: ASYNC251 - deliberately deny the timer a loop turn
            return permit

    scope = admission_scope(LateAdmission(), deadline=asyncio.get_running_loop().time() + 0.03)
    guard = _GuardedTransport(httpx.MockTransport(lambda r: pytest.fail("expired permit")),
                              physical_call(scope))
    with pytest.raises(_PhysicalAdmissionStopped):
        await guard.handle_async_request(httpx.Request("POST", "https://mock.example"))
    assert len(scope.attempts) == 1 and not scope.attempts[0].delegated


async def test_inconsistent_usage_totals_are_invalid_not_missing_or_zero() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    call.observe_usage(TokenUsage(2, 3, 99))
    await call.finish("invalid")
    assert admission.receipts[0][1].usage_status == "invalid"
    assert admission.receipts[0][1].usage is None


@pytest.mark.parametrize("later", [None, TokenUsage(2, 3, 5), TokenUsage(10, 50, 60)])
async def test_usage_invalidity_is_sticky_but_preserves_prior_evidence(later: TokenUsage | None) -> None:
    admission = Admission()
    scope = admission_scope(admission, purpose="primary", max_attempts=2)
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    call.observe_usage(TokenUsage(20, 30, 50))
    call.observe_usage(later, invalid=later is None)
    assert scope.stopped is not None
    call.observe_usage(TokenUsage(30, 40, 70))
    call.observe_usage(None)
    await call.finish("invalid")
    assert admission.receipts[0][1].usage == TokenUsage(20, 30, 50)
    assert admission.receipts[0][1].usage_status == "invalid"


async def test_direct_receipt_failure_clears_exception_context_and_retains_permit() -> None:
    admission = Admission()
    admission.receipt_failure = True
    scope = admission_scope(admission)
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    call.observe_usage(TokenUsage(2, 3, 5))
    with pytest.raises(_PhysicalAdmissionStopped) as caught:
        await call.finish("completed")
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert scope.attempts[0].permit.attempt_id.int == 1
    assert scope.attempts[0].receipt.usage == TokenUsage(2, 3, 5)
    assert not scope.attempts[0].delivered


async def test_monotonic_usage_and_duplicates_remain_known_across_empty_observations() -> None:
    admission = Admission()
    scope = admission_scope(admission)
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    for usage in [TokenUsage(2, 3, 5), None, TokenUsage(2, 3, 5), TokenUsage(4, 6, 10), None]:
        call.observe_usage(usage)
    assert scope.stopped is None
    await call.finish("completed")
    assert admission.receipts[0][1].usage == TokenUsage(4, 6, 10)
    assert admission.receipts[0][1].usage_status == "known"


@pytest.mark.parametrize("outcome", ["completed", "error", "unknown"])
@pytest.mark.parametrize("purpose", ["primary", "review"])
async def test_delivered_unknown_reserves_new_primary_attempt_without_poisoning_pool(
    outcome: str, purpose: str,
) -> None:
    admission = RemainingAdmission()
    scope = admission_scope(admission, purpose=purpose, max_attempts=3 if purpose == "primary" else 1)
    wire: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        wire.append(req)
        return httpx.Response(200)

    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(handle), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    await call.finish(outcome)
    old_receipt = scope.attempts[0].receipt
    second = _GuardedTransport(httpx.MockTransport(handle), physical_call(scope))
    if purpose == "review":
        with pytest.raises(_PhysicalAdmissionStopped):
            await second.handle_async_request(httpx.Request("POST", "https://mock.example"))
        assert admission.remaining == 100 and len(wire) == 1
    else:
        await second.handle_async_request(httpx.Request("POST", "https://mock.example"))
        assert len(wire) == 2 and admission.remaining == 0
        assert scope.attempts[1].permit.attempt_id != scope.attempts[0].permit.attempt_id
    assert scope.attempts[0].receipt is old_receipt
    assert old_receipt.usage is None and old_receipt.usage_status == "missing"
    assert admission.holds[scope.attempts[0].permit.attempt_id] == 100


async def test_resume_keeps_original_scope_counter_deadline_and_restores_binding() -> None:
    from agent_hub.models import physical_admission as physical

    resume = physical._resume_physical_admission
    owner = admission_scope()
    scope = admission_scope(purpose="primary", max_attempts=2)
    deployment = Deployment(id="primary", logical_model="primary")
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    call.observe_usage(TokenUsage(2, 3, 5))
    await call.finish("completed")
    attempts, deadline = scope.attempts, scope.deadline
    with _bind_physical_admission(owner):
        with resume(scope):
            captured = physical._capture_physical_call(deployment=deployment, protocol="chat_completions", stream=False)
            assert captured.scope is scope
        scope.check()
        captured = physical._capture_physical_call(deployment=deployment, protocol="chat_completions", stream=False)
        assert captured.scope is owner
        assert scope.attempts == attempts and scope.deadline == deadline
    with resume(scope):
        scope.check()
    scope.revoke()
    with pytest.raises(_PhysicalAdmissionStopped), resume(scope):
        pass


async def test_resume_cleanup_can_record_revoked_scope_but_cannot_send() -> None:
    from agent_hub.models import physical_admission as physical

    resume = physical._resume_physical_admission
    admission = Admission()
    scope = admission_scope(admission)
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    scope.revoke()
    with resume(scope, check=False):
        captured = physical._capture_physical_call(deployment=Deployment(id="primary", logical_model="primary"),
                                                  protocol="chat_completions", stream=False)
        assert captured.scope is scope
        await call.finish("unknown")
        with pytest.raises(_PhysicalAdmissionStopped):
            await _GuardedTransport(httpx.MockTransport(lambda r: pytest.fail("cleanup cannot send")), captured).handle_async_request(
                httpx.Request("POST", "https://mock.example"))
    assert scope.attempts[0].delivered and admission.receipts[0][0] == scope.attempts[0].permit
    assert len(admission.metadata) == 1
    assert physical._capture_physical_call(deployment=Deployment(id="primary", logical_model="primary"),
                                         protocol="chat_completions", stream=False) is None


@pytest.mark.parametrize("stop", ["invalid", "cancelled", "receipt_failure"])
async def test_primary_terminal_states_still_block_new_permit(stop: str) -> None:
    admission = Admission()
    scope = admission_scope(admission, purpose="primary", max_attempts=2)
    call = physical_call(scope)
    await _GuardedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), call).handle_async_request(
        httpx.Request("POST", "https://mock.example"))
    if stop == "invalid":
        call.observe_usage(None, invalid=True)
    if stop == "receipt_failure":
        admission.receipt_failure = True
        with pytest.raises(_PhysicalAdmissionStopped):
            await call.finish("unknown")
        assert not scope.attempts[0].delivered
    else:
        await call.finish("cancelled" if stop == "cancelled" else "invalid")
    with pytest.raises(_PhysicalAdmissionStopped):
        await _GuardedTransport(httpx.MockTransport(lambda r: pytest.fail("terminal must block")),
                                physical_call(scope)).handle_async_request(
                                    httpx.Request("POST", "https://mock.example"))
    assert len(admission.metadata) == 1 and len(scope.attempts) == 1


async def test_resume_cleanup_does_not_bypass_expired_deadline() -> None:
    from agent_hub.models.physical_admission import _resume_physical_admission

    scope = admission_scope(deadline=asyncio.get_running_loop().time() - 1)
    with pytest.raises(_PhysicalAdmissionStopped), _resume_physical_admission(scope):
        pass
    with _resume_physical_admission(scope, check=False), pytest.raises(_PhysicalAdmissionStopped):
        await _GuardedTransport(httpx.MockTransport(lambda r: pytest.fail("deadline must block")),
                                physical_call(scope)).handle_async_request(
                                    httpx.Request("POST", "https://mock.example"))
    assert scope.attempts == ()
