"""Private new-submission execution accounting; never grants or renews authority."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, Protocol, cast
from uuid import UUID


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def positive(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("execution clock amount is invalid")
    number = cast(int | float, value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("execution clock amount is invalid")
    return float(number)


def authorization_seconds(value: object) -> float:
    """Finite server-issued horizon, separate from the legacy active-task cap."""
    number = positive(value)
    if not number.is_integer():
        raise ValueError("execution authorization must contain whole seconds")
    try:
        # Reject amounts that cannot be represented as a durable deadline.
        datetime.now(UTC) + timedelta(seconds=number)
    except (OverflowError, ValueError):
        raise ValueError("execution authorization horizon is not representable") from None
    return number


@dataclass(frozen=True, slots=True)
class ClockPolicy:
    active_seconds: float
    authorization_seconds: float
    initial_soft_seconds: float
    extension_per_unit: float
    policy_sha256: str

    def __post_init__(self) -> None:
        for value in (self.active_seconds, self.authorization_seconds, self.initial_soft_seconds,
                      self.extension_per_unit):
            positive(value)
        if not self.initial_soft_seconds <= self.active_seconds <= self.authorization_seconds:
            raise ValueError("execution clock exceeds its authorized envelope")
        if len(self.policy_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.policy_sha256):
            raise ValueError("execution clock policy identity is invalid")

    def payload(self) -> dict[str, object]:
        return {"active_seconds": self.active_seconds, "authorization_seconds": self.authorization_seconds,
                "initial_soft_seconds": self.initial_soft_seconds, "extension_per_unit": self.extension_per_unit,
                "policy_sha256": self.policy_sha256}

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> ClockPolicy:
        if set(value) != {"active_seconds", "authorization_seconds", "initial_soft_seconds",
                         "extension_per_unit", "policy_sha256"} or type(value["policy_sha256"]) is not str:
            raise ValueError("execution clock policy is invalid")
        return cls(positive(value["active_seconds"]), positive(value["authorization_seconds"]),
                   positive(value["initial_soft_seconds"]), positive(value["extension_per_unit"]),
                   value["policy_sha256"])


@dataclass(frozen=True, slots=True)
class Remaining:
    soft_seconds: float
    active_seconds: float
    authorization_seconds: float

    @property
    def effective_seconds(self) -> float:
        return min(self.soft_seconds, self.active_seconds, self.authorization_seconds)

    @property
    def absolute_seconds(self) -> float:
        return min(self.active_seconds, self.authorization_seconds)


@dataclass(frozen=True, slots=True)
class ClockSnapshot:
    policy: ClockPolicy
    hard_deadline: datetime
    spent_seconds: float
    active_since: datetime | None
    worker_id: str | None
    worker_token: UUID | None
    progress_units: int
    version: int

    def remaining(self, now: datetime, *, progress_units: int | None = None) -> Remaining:
        if now.utcoffset() is None or self.hard_deadline.utcoffset() is None:
            raise ValueError("execution clock requires aware time")
        if not math.isfinite(self.spent_seconds) or self.spent_seconds < 0:
            raise ValueError("execution clock spent amount is invalid")
        if progress_units is not None and (type(progress_units) is not int or progress_units < 0):
            raise ValueError("execution clock progress is invalid")
        units = self.progress_units if progress_units is None else max(self.progress_units, progress_units)
        if type(units) is not int or units < 0:
            raise ValueError("execution clock progress is invalid")
        elapsed = 0. if self.active_since is None else (now-self.active_since).total_seconds()
        if elapsed < 0:
            raise ValueError("execution clock moved backwards")
        spent = self.spent_seconds + elapsed
        soft = min(self.policy.active_seconds,
                   self.policy.initial_soft_seconds+self.policy.extension_per_unit*units)
        return Remaining(max(0., soft-spent), max(0., self.policy.active_seconds-spent),
                         max(0., (self.hard_deadline-now).total_seconds()))


@dataclass(frozen=True, slots=True)
class SubmissionBinding:
    tenant_id: UUID
    actor_id: UUID
    request_sha256: str
    policy: ClockPolicy
    profile_sha256: str
    activation_valid_until: datetime | None
    recheck: Callable[[], Awaitable[bool]]


_SUBMISSION: ContextVar[SubmissionBinding | None] = ContextVar("private_clock_submission", default=None)


def submission_boundary[**P, T](function: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(function)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
        token = _SUBMISSION.set(None)
        try:
            return await function(*args, **kwargs)
        finally:
            _SUBMISSION.reset(token)
    return invoke


def bind_submission(*, tenant_id: UUID, actor_id: UUID, request: str,
                    profile: Mapping[str, object], active_seconds: float,
                    initial_soft_seconds: float, extension_per_unit: float,
                    recheck: Callable[[], Awaitable[bool]]) -> None:
    # Called only by actual authenticated RunService.submit after live server submission_profile.
    horizon = positive(profile.get("timeout_seconds"))
    if not profile.get("config_sha256") or not profile.get("catalog_sha256"):
        raise ValueError("execution clock requires an approved live server profile")
    expiry = None
    activation = profile.get("activation")
    if activation is not None:
        if not isinstance(activation, Mapping) or type(activation.get("valid_until")) is not str:
            raise ValueError("execution clock activation is invalid")
        expiry = datetime.fromisoformat(activation["valid_until"])
        if expiry.utcoffset() is None:
            raise ValueError("execution clock activation is invalid")
    profile_sha = digest(dict(profile))
    policy = ClockPolicy(active_seconds, horizon, initial_soft_seconds, extension_per_unit, profile_sha)
    _SUBMISSION.set(SubmissionBinding(tenant_id, actor_id, hashlib.sha256(request.encode()).hexdigest(),
                                      policy, profile_sha, expiry, recheck))


def submission_binding(tenant_id: UUID, actor_id: UUID, request: str,
                       route: Mapping[str, object]) -> SubmissionBinding:
    value = _SUBMISSION.get()
    if (value is None or (value.tenant_id, value.actor_id, value.request_sha256) !=
            (tenant_id, actor_id, hashlib.sha256(request.encode()).hexdigest())
            or digest(route.get("approval_review_profile")) != value.profile_sha256
            or route.get("runtime_timeout_absolute_seconds") != value.policy.active_seconds):
        raise ValueError("execution clock submission is not authorized")
    return value


@dataclass(frozen=True, slots=True)
class LiveClock:
    run_id: UUID
    worker_token: UUID
    snapshot: ClockSnapshot
    observed_at: datetime
    monotonic_at: float

    def remaining(self, *, progress_units: int | None = None) -> Remaining:
        elapsed = max(0., time.monotonic()-self.monotonic_at)
        return self.snapshot.remaining(self.observed_at+timedelta(seconds=elapsed),
                                       progress_units=progress_units)


_EXECUTION: ContextVar[LiveClock | None] = ContextVar("private_live_execution_clock", default=None)


def execution_boundary[**P, T](function: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(function)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
        token = _EXECUTION.set(None)
        try:
            return await function(*args, **kwargs)
        finally:
            _EXECUTION.reset(token)
    return invoke


def activate(snapshot: ClockSnapshot, observed_at: datetime, *, run_id: UUID, worker_token: UUID) -> None:
    _EXECUTION.set(LiveClock(run_id, worker_token, snapshot, observed_at, time.monotonic()))


def bound_clock(run_id: UUID, worker_token: UUID | None) -> LiveClock:
    current = _EXECUTION.get()
    if current is None or (current.run_id, current.worker_token) != (run_id, worker_token):
        raise ValueError("execution clock private worker binding is missing")
    return current


class ClockRecord(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def worker_lease_token(self) -> UUID | None: ...


def authorization_window(record: ClockRecord) -> float:
    current = bound_clock(record.id, record.worker_lease_token)
    return current.snapshot.policy.authorization_seconds



def live_clock() -> LiveClock | None:
    return _EXECUTION.get()


def live_remaining(*, progress_units: int | None = None) -> Remaining | None:
    current = _EXECUTION.get()
    return None if current is None else current.remaining(progress_units=progress_units)


def clamp_deadline(deadline: float) -> float:
    remaining = live_remaining()
    return deadline if remaining is None else min(deadline, time.monotonic()+remaining.absolute_seconds)


def clamp_soft_deadline(deadline: float, *, progress_units: int) -> float:
    remaining = live_remaining(progress_units=progress_units)
    return deadline if remaining is None else min(deadline, time.monotonic()+remaining.soft_seconds)
