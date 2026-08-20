"""Low-token run observation for scheduler handoff and recovery decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent

_FAILURE_KINDS = frozenset({EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED, EventKind.TOOL_FAILED})
_CAPACITY_MARKERS = frozenset(
    {
        "model capacity unavailable",
        "model capacity queue timeout",
        "model capacity queue is full",
        "model capacity backend failed",
        "model capacity backend unavailable",
        "capacity unavailable",
        "status_code=429",
        "http 429",
    }
)
_EMPTY_RESPONSE_MARKERS = frozenset(
    {
        "model response text is empty",
        "model response is empty",
        "empty model response",
    }
)


@dataclass(frozen=True, slots=True)
class ObserverPolicy:
    """Thresholds for event-only observation.

    The monitor deliberately works from event metadata and counters. It must not
    retain prompts, messages, artifact text, or hidden reasoning.
    """

    max_message_events_before_compaction: int = 32
    max_artifact_events_before_compaction: int = 12
    repeated_failure_threshold: int = 2

    def __post_init__(self) -> None:
        for name in (
            "max_message_events_before_compaction",
            "max_artifact_events_before_compaction",
            "repeated_failure_threshold",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ObserverDecision:
    trigger: str
    action: str
    severity: str
    source_kind: str
    source_sequence: int
    counters: Mapping[str, int]
    actor: str | None = None

    def to_event(self, *, run_id: UUID, sequence: int) -> RunEvent:
        payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "trigger": self.trigger,
            "action": self.action,
            "severity": self.severity,
            "source_kind": self.source_kind,
            "source_sequence": self.source_sequence,
            "event_count": self.counters.get("events", 0),
            "failure_events": self.counters.get("failure_events", 0),
            "retry_events": self.counters.get("retry_events", 0),
            "message_events": self.counters.get("message_events", 0),
            "artifact_events": self.counters.get("artifact_events", 0),
        }
        if self.actor is not None:
            payload["actor"] = self.actor
        return RunEvent(kind="observer.notice", sequence=sequence, run_id=run_id, payload=payload)


@dataclass(slots=True)
class RunMonitor:
    policy: ObserverPolicy = field(default_factory=ObserverPolicy)
    _event_count: int = 0
    _failure_events: int = 0
    _retry_events: int = 0
    _message_events: int = 0
    _artifact_events: int = 0
    _emitted: set[str] = field(default_factory=set)

    def observe(self, event: RunEvent) -> ObserverDecision | None:
        if event.kind == "observer.notice":
            return None
        self._event_count += 1
        if event.kind is EventKind.MESSAGE_CREATED:
            self._message_events += 1
        elif event.kind is EventKind.ARTIFACT_CREATED:
            self._artifact_events += 1
        elif event.kind is EventKind.STEP_RETRYING:
            self._retry_events += 1

        if event.kind in _FAILURE_KINDS:
            self._failure_events += 1
            failure_text = _event_failure_text(event)
            if _contains_marker(failure_text, _CAPACITY_MARKERS):
                return self._emit(
                    "model_capacity_pressure",
                    "reschedule_or_reassign_model",
                    "warning",
                    event,
                )
            if _contains_marker(failure_text, _EMPTY_RESPONSE_MARKERS):
                return self._emit(
                    "empty_model_response",
                    "retry_fallback_or_reassign_model",
                    "warning",
                    event,
                )
            if self._failure_events >= self.policy.repeated_failure_threshold:
                return self._emit(
                    "repeated_failure",
                    "pause_and_request_scheduler_review",
                    "warning",
                    event,
                )
            return self._emit("runtime_failure", "preserve_partial_outputs", "info", event)

        if event.kind is EventKind.STEP_RETRYING:
            return self._emit("step_retrying", "watch_retry_budget", "info", event)

        if (
            self._message_events >= self.policy.max_message_events_before_compaction
            or self._artifact_events >= self.policy.max_artifact_events_before_compaction
        ):
            return self._emit(
                "context_compaction_recommended",
                "compact_context_before_next_model_call",
                "info",
                event,
            )
        return None

    def _emit(
        self,
        trigger: str,
        action: str,
        severity: str,
        event: RunEvent,
    ) -> ObserverDecision | None:
        if trigger in self._emitted:
            return None
        self._emitted.add(trigger)
        return ObserverDecision(
            trigger=trigger,
            action=action,
            severity=severity,
            source_kind=_event_kind_text(event.kind),
            source_sequence=event.sequence,
            counters=self._counters(),
            actor=event.actor,
        )

    def _counters(self) -> Mapping[str, int]:
        return {
            "events": self._event_count,
            "failure_events": self._failure_events,
            "retry_events": self._retry_events,
            "message_events": self._message_events,
            "artifact_events": self._artifact_events,
        }


def _event_kind_text(kind: EventKind | str) -> str:
    return kind.value if isinstance(kind, EventKind) else kind


def _event_failure_text(event: RunEvent) -> str:
    parts: list[str] = []
    if event.reason:
        parts.append(event.reason)
    for key in ("error", "error_type", "status", "status_code", "failure", "stage"):
        value = event.payload.get(key)
        if isinstance(value, str | int):
            parts.append(str(value))
    return " ".join(parts).casefold()


def _contains_marker(text: str, markers: frozenset[str]) -> bool:
    return any(marker in text for marker in markers)


__all__ = ["ObserverDecision", "ObserverPolicy", "RunMonitor"]
