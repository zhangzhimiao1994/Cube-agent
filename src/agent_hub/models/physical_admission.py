"""Private, process-local physical I/O fence. No budget storage or settlement.

Only a trusted adapter supplies identity, bounds, permits and the original
absolute deadline. HTTP status is evidence of transport, never of model usage.
Pending receipts stay reachable from the scope even when delivery fails.
"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

import httpx

from agent_hub.models.failure_receipt import _valid_usage
from agent_hub.models.types import Deployment, TokenUsage

_PhysicalOutcome = Literal["completed", "rejected", "invalid", "cancelled", "error", "unknown"]
_PhysicalProtocol = Literal["chat_completions", "responses", "messages"]


class _PhysicalAdmissionStopped(RuntimeError):
    """Trusted terminal control result; never a retryable network failure."""

    def __init__(self) -> None:
        super().__init__("physical model admission stopped")


@dataclass(frozen=True)
class _PhysicalPermit:
    # Adapter-owned identity (e.g. the already prepared review attempt UUID).
    attempt_id: UUID


@dataclass(frozen=True)
class _PhysicalSelection:
    """Gateway-owned result after clamp/pricing; contains no request body.

    The producer computes these using its isolated selected request snapshot.
    This carrier validates shape only; it neither estimates nor grants budget.
    """

    request_sha256: str
    deployment: Deployment = field(repr=False)
    token_hold: int
    cost_hold_usd: Decimal

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[a-f0-9]{64}", self.request_sha256)
                or type(self.token_hold) is not int or self.token_hold <= 0
                or not isinstance(self.cost_hold_usd, Decimal)
                or not self.cost_hold_usd.is_finite() or self.cost_hold_usd < 0):
            raise ValueError("trusted selected bounds required")


@dataclass(frozen=True)
class _PhysicalAttemptMetadata:
    deployment_id: str
    provider_model: str
    request_sha256: str
    purpose: Literal["primary", "review"]
    protocol: _PhysicalProtocol
    stream: bool
    ordinal: int
    selection: _PhysicalSelection | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _PhysicalReceipt:
    outcome: _PhysicalOutcome
    usage: TokenUsage | None
    usage_status: Literal["known", "missing", "invalid"]
    http_status: int | None
    delegated: bool


class _PhysicalAdmission(Protocol):
    async def before_send(
        self, meta: _PhysicalAttemptMetadata, *, deadline: float,
    ) -> _PhysicalPermit: ...

    async def after_receipt(
        self, permit: _PhysicalPermit, receipt: _PhysicalReceipt, *, deadline: float,
    ) -> None: ...


@dataclass
class _PhysicalAttempt:
    permit: _PhysicalPermit
    metadata: _PhysicalAttemptMetadata
    delegated: bool = False
    http_status: int | None = None
    usage: TokenUsage | None = None
    usage_status: Literal["known", "missing", "invalid"] = "missing"
    receipt: _PhysicalReceipt | None = None
    delivered: bool = False


class _PhysicalAdmissionScope:
    def __init__(
        self, admission: _PhysicalAdmission, *, request_sha256: str,
        purpose: Literal["primary", "review"], deadline: float, max_attempts: int = 1,
    ) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", request_sha256):
            raise ValueError("trusted request SHA required")
        if purpose not in ("primary", "review"):
            raise ValueError("trusted purpose required")
        if not math.isfinite(deadline) or type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("finite deadline and positive attempt bound required")
        if purpose == "review" and max_attempts != 1:
            raise ValueError("review requires exactly one physical attempt")
        self._admission = admission
        self._request_sha256 = request_sha256
        self._purpose = purpose
        self._deadline = deadline
        self._max_attempts = max_attempts
        self._active = True
        self._stopped = False
        self._lock = asyncio.Lock()
        self._attempts: list[_PhysicalAttempt] = []

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def stopped(self) -> _PhysicalAdmissionStopped | None:
        return _PhysicalAdmissionStopped() if self._stopped else None

    @property
    def attempts(self) -> tuple[_PhysicalAttempt, ...]:
        return tuple(self._attempts)

    def stop(self) -> _PhysicalAdmissionStopped:
        # A boolean latch never retains SDK frames/prompt bytes via a traceback.
        self._stopped = True
        return _PhysicalAdmissionStopped()

    def revoke(self) -> None:
        self._active = False
        self.stop()

    def check(self) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            self.stop()
            raise asyncio.CancelledError
        if (not self._active or self._stopped
                or asyncio.get_running_loop().time() >= self.deadline):
            raise self.stop()

    async def issue(self, call: _PhysicalCall) -> _PhysicalAttempt:
        try:
            async with asyncio.timeout_at(self.deadline), self._lock:
                self.check()
                if len(self._attempts) >= self._max_attempts:
                    raise self.stop()
                meta = _PhysicalAttemptMetadata(
                    call.deployment_id, call.provider_model,
                    self._request_sha256 if call.selection is None else call.selection.request_sha256,
                    self._purpose, call.protocol, call.stream, len(self._attempts) + 1,
                    call.selection,
                )
                permit = await self._admission.before_send(meta, deadline=self.deadline)
                if type(permit) is not _PhysicalPermit or any(
                    a.permit.attempt_id == permit.attempt_id for a in self._attempts
                ):
                    raise self.stop()
                attempt = _PhysicalAttempt(permit, meta)
                self._attempts.append(attempt)
                call.attempts.append(attempt)
                # No await is allowed between this check and the delegate below.
                self.check()
                return attempt
        except asyncio.CancelledError:
            self.stop()
            raise
        except Exception:  # noqa: BLE001 - adapter failures are terminal, redacted controls
            raise self.stop() from None


_scope: ContextVar[_PhysicalAdmissionScope | None] = ContextVar("_physical_scope", default=None)
_call: ContextVar[_PhysicalCall | None] = ContextVar("_physical_call", default=None)
_selection: ContextVar[_PhysicalSelection | None] = ContextVar("_physical_selection", default=None)


@contextmanager
def _bind_physical_selection(selection: _PhysicalSelection) -> Iterator[None]:
    """Continuity data only. It cannot create/revive a physical admission scope."""
    token = _selection.set(selection)
    try:
        yield
    finally:
        _selection.reset(token)


@contextmanager
def _bind_physical_admission(scope: _PhysicalAdmissionScope) -> Iterator[None]:
    scope.check()
    token = _scope.set(scope)
    try:
        yield
    finally:
        scope.revoke()
        _scope.reset(token)


@contextmanager
def _bind_physical_call(call: _PhysicalCall | None) -> Iterator[None]:
    token = _call.set(call)
    try:
        yield
    finally:
        _call.reset(token)


def _current_physical_call() -> _PhysicalCall | None:
    return _call.get()


def _capture_physical_call(
    *, deployment: Deployment, protocol: _PhysicalProtocol, stream: bool,
) -> _PhysicalCall | None:
    scope = _scope.get()
    if scope is None:
        return None
    selection = _selection.get()
    if selection is not None and (
        selection.deployment.id != deployment.id
        or selection.deployment.provider_model != deployment.provider_model
        or selection.deployment.request_model != deployment.request_model
        or selection.deployment.api_base != deployment.api_base
    ):
        scope.stop()
    return _PhysicalCall(scope, deployment_id=deployment.id, provider_model=deployment.provider_model,
                         protocol=protocol, stream=stream, selection=selection)


class _PhysicalCall:
    def __init__(
        self, scope: _PhysicalAdmissionScope, *, deployment_id: str, provider_model: str,
        protocol: _PhysicalProtocol, stream: bool,
        selection: _PhysicalSelection | None = None,
    ) -> None:
        self.scope = scope
        self.deployment_id = deployment_id
        self.provider_model = provider_model
        self.protocol = protocol
        self.stream = stream
        self.selection = selection
        self.attempts: list[_PhysicalAttempt] = []

    def observe_usage(self, usage: TokenUsage | None, *, invalid: bool = False) -> None:
        if not self.attempts:
            return
        attempt = self.attempts[-1]
        if attempt.usage_status == "invalid":
            return  # Later observations cannot repair contradictory billing evidence.
        if invalid or (usage is not None and not _valid_usage(usage)):
            attempt.usage_status = "invalid"
            self.scope.stop()
        elif usage is not None:
            previous = attempt.usage
            if previous is not None and any(new < old for new, old in zip(
                (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
                (previous.prompt_tokens, previous.completion_tokens, previous.total_tokens),
                strict=True,
            )):
                attempt.usage_status = "invalid"
                self.scope.stop()
                return
            # Keep cumulative observations monotonic; None chunks and cleanup
            # do not erase evidence. Invalidity preserves the last trusted values.
            attempt.usage = usage
            attempt.usage_status = "known"

    async def finish(self, outcome: _PhysicalOutcome) -> None:
        for attempt in self.attempts:
            if attempt.delivered:
                continue
            if attempt.receipt is None:
                attempt.receipt = _PhysicalReceipt(
                    outcome, attempt.usage, attempt.usage_status,
                    attempt.http_status, attempt.delegated,
                )
            try:
                async with asyncio.timeout_at(self.scope.deadline):
                    await self.scope._admission.after_receipt(
                        attempt.permit, attempt.receipt, deadline=self.scope.deadline,
                    )
                attempt.delivered = True
                if (attempt.receipt.usage_status != "known"
                        or attempt.receipt.outcome in ("cancelled", "unknown")):
                    # The adapter may later reconcile, but this process cannot
                    # infer a refund or replay from missing/invalid usage.
                    self.scope.stop()
            except asyncio.CancelledError:
                self.scope.stop()
                raise
            except Exception:  # noqa: BLE001 - retain permit and immutable pending receipt
                self.scope.stop()
            if not attempt.delivered:
                # Raise after leaving the callback's exception context. `from
                # None` alone would still expose its private __context__/locals.
                raise self.scope.stop()


class _GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport, call: _PhysicalCall) -> None:
        self._delegate = delegate
        self._call = call

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        call = self._call
        call.scope.check()
        # A previous HTTP error must reach accounting before compatibility I/O.
        await call.finish("error")
        attempt = await call.scope.issue(call)
        try:
            call.scope.check()
            attempt.delegated = True
            response = await self._delegate.handle_async_request(request)
            attempt.http_status = response.status_code
            return response
        except asyncio.CancelledError:
            call.scope.stop()
            raise
        except Exception:
            call.scope.stop()  # issued + unknown is never permission to resend
            raise

    async def aclose(self) -> None:
        await self._delegate.aclose()
