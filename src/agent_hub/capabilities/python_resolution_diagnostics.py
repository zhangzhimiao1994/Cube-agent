"""Finite resolution observations; never retry, deadline or execution authority."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

type JsonScalar = str | bool | None


class ResolutionPhase(StrEnum):
    INSPECTION = "inspection"
    RESOLUTION = "resolution"
    FREEZE = "freeze"
    PUBLICATION = "publication"


class ResolutionProgress(StrEnum):
    UNOBSERVED = "unobserved"
    RESOLUTION_COMPLETED = "resolution_completed"
    FREEZE_PENDING = "freeze_pending"
    FREEZE_COMPLETED = "freeze_completed"
    PUBLICATION_PENDING = "publication_pending"
    PUBLICATION_COMPLETED = "publication_completed"


@dataclass(frozen=True, slots=True)
class _Observation:
    phase: ResolutionPhase
    progress: ResolutionProgress
    original_deadline_expired: bool | None


_ATTRIBUTE = "_python_resolution_timeout_observation"


def observe_resolution_timeout(
    error: BaseException, *, phase: ResolutionPhase, progress: ResolutionProgress,
    original_deadline: datetime | None,
) -> None:
    """Attach no private text; re-raising the same exception preserves cancellation."""
    from agent_hub.skills.sandbox.python_registry_protocol import ResolutionError

    if type(error) not in (TimeoutError, asyncio.CancelledError, ResolutionError):
        return
    if type(error) is ResolutionError and error.args != ("resolution_deadline",):
        return
    if type(phase) is not ResolutionPhase or type(progress) is not ResolutionProgress:
        return
    expired = None
    if (type(original_deadline) is datetime and original_deadline.utcoffset() is not None):
        expired = datetime.now(UTC) >= original_deadline
    setattr(error, _ATTRIBUTE, _Observation(phase, progress, expired))


def resolution_timeout_payload(error: BaseException) -> dict[str, JsonScalar]:
    """A bounded TimeoutError -> CancelledError cause chain, never arbitrary context/text."""
    from agent_hub.skills.sandbox.python_registry_protocol import ResolutionError

    trusted = (TimeoutError, asyncio.CancelledError, ResolutionError)
    if type(error) not in trusted:
        return {}
    if type(error) is ResolutionError and error.args != ("resolution_deadline",):
        return {}
    current: BaseException | None = error
    visited: set[int] = set()
    observation = None
    for _ in range(4):
        if (current is None or id(current) in visited
                or type(current) not in trusted):
            break
        visited.add(id(current))
        value = vars(current).get(_ATTRIBUTE)
        if (type(value) is _Observation and type(value.phase) is ResolutionPhase
                and type(value.progress) is ResolutionProgress
                and (value.original_deadline_expired is None
                     or type(value.original_deadline_expired) is bool)):
            observation = value
            break
        current = current.__cause__
    if observation is None:
        return {}
    return _observation_payload(observation)


def _observation_payload(observation: _Observation) -> dict[str, JsonScalar]:
    expired = observation.original_deadline_expired
    cause = "resolution_deadline" if expired is True else "resolution_timeout"
    progress = observation.progress
    return {
        "error_stage": "project_verification",
        "error_category": "deadline_exceeded" if expired is True else "timeout",
        "error_code": "project_verification.resolution." + cause,
        "error_summary": "project resolution stopped: " + cause,
        "retryable": False,
        "suggested_action": "保留原意图、回执和期限；结果未知时仅检查，禁止重放或续期。",
        "resolution_phase": observation.phase.value,
        "resolution_progress": progress.value,
        "resolution_progress_evidence": (
            "none" if progress is ResolutionProgress.UNOBSERVED else "validated_root_status"
        ),
        "original_deadline_expired": expired,
    }


def validate_resolution_timeout_payload(payload: Mapping[str, object]) -> None:
    """Validate a public projection as finite facts, never as execution authority."""
    phase = payload.get("resolution_phase")
    progress = payload.get("resolution_progress")
    expired = payload.get("original_deadline_expired")
    if (type(phase) is not str or type(progress) is not str
            or (expired is not None and type(expired) is not bool)
            or "original_deadline_expired" not in payload
            or "resolution_failure" in payload):
        raise ValueError("resolution timeout diagnostic fields are invalid")
    try:
        observation = _Observation(ResolutionPhase(phase), ResolutionProgress(progress), expired)
    except ValueError:
        raise ValueError("resolution timeout diagnostic fields are invalid") from None
    expected = _observation_payload(observation)
    if any(type(payload.get(key)) is not type(value) or payload[key] != value
           for key, value in expected.items()):
        raise ValueError("resolution timeout diagnostic fields are inconsistent")
