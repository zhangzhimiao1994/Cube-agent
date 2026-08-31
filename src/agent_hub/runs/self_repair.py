"""Read-only self-repair classification for terminal run failures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent

_FAILURE_KINDS = frozenset({EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED, EventKind.TOOL_FAILED})
_REPAIR_EVENT_KINDS = frozenset({"repair.classified", "repair.skipped"})
_UNCERTAIN_MARKERS = frozenset(
    {
        "model_outcome_uncertain",
        "tool_outcome_uncertain",
        "outcome requires confirmation",
        "capability outcome requires confirmation",
        "model outcome requires confirmation",
    }
)
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
class SelfRepairPolicy:
    enabled: bool = True
    requires_approval: bool = True
    max_attempts: int = 1

    def __post_init__(self) -> None:
        for name in ("enabled", "requires_approval"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")


@dataclass(frozen=True, slots=True)
class SelfRepairDecision:
    kind: Literal["repair.classified", "repair.skipped"]
    trigger: str
    action: str
    severity: str
    status: RunStatus
    mode: TaskMode | None
    source_kind: str
    source_sequence: int
    fingerprint: str
    failure_category: str
    requires_approval: bool
    automatic_execution: bool
    attempt: int
    max_attempts: int
    skip_reason: str | None = None

    def with_source_sequence(self, source_sequence: int) -> SelfRepairDecision:
        return SelfRepairDecision(
            kind=self.kind,
            trigger=self.trigger,
            action=self.action,
            severity=self.severity,
            status=self.status,
            mode=self.mode,
            source_kind=self.source_kind,
            source_sequence=source_sequence,
            fingerprint=self.fingerprint,
            failure_category=self.failure_category,
            requires_approval=self.requires_approval,
            automatic_execution=self.automatic_execution,
            attempt=self.attempt,
            max_attempts=self.max_attempts,
            skip_reason=self.skip_reason,
        )

    def to_event(self, *, run_id: UUID, sequence: int) -> RunEvent:
        payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "trigger": self.trigger,
            "action": self.action,
            "severity": self.severity,
            "status": self.status.value,
            "source_kind": self.source_kind,
            "source_sequence": self.source_sequence,
            "fingerprint": self.fingerprint,
            "failure_category": self.failure_category,
            "requires_approval": self.requires_approval,
            "automatic_execution": self.automatic_execution,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
        }
        if self.mode is not None:
            payload["mode"] = self.mode.value
        if self.skip_reason is not None:
            payload["skip_reason"] = self.skip_reason
        return RunEvent(kind=self.kind, sequence=sequence, run_id=run_id, payload=payload)

    def to_proposal(self, *, run_id: UUID) -> dict[str, object] | None:
        if self.kind != "repair.classified":
            return None
        return {
            "kind": "self_repair",
            "title": "受控自修复建议",
            "summary": "运行失败已分类，可在审批后创建一次受控修复重试。",
            "repair_action": self.action,
            "failure_kind": self.failure_category,
            "source_run_id": str(run_id),
            "source_event_sequence": self.source_sequence,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "instruction": _repair_instruction(self.failure_category),
            "requires_approval": self.requires_approval,
            "replay_safe": False,
            "automatic_execution": False,
            "fingerprint": self.fingerprint,
        }


def classify_terminal_run(
    *,
    status: RunStatus,
    mode: TaskMode | None,
    routing_decision: Mapping[str, object] | None,
    events: Sequence[RunEvent],
    policy: SelfRepairPolicy,
) -> SelfRepairDecision | None:
    if not policy.enabled or status is not RunStatus.FAILED or _already_classified(events):
        return None
    failure = _supervisor_source_failure(events)
    source_kind = "run.failed" if failure is None else _kind_text(failure.kind)
    source_sequence = 0 if failure is None else failure.sequence
    failure_category = "missing_failure_event" if failure is None else _failure_category(failure)
    fingerprint = _fingerprint(
        status=status,
        mode=mode,
        source_kind=source_kind,
        failure_category=failure_category,
        failure=failure,
    )
    if _routing_source(routing_decision) == "self_repair":
        return _skipped_decision(
            status=status,
            mode=mode,
            source_kind=source_kind,
            source_sequence=source_sequence,
            fingerprint=fingerprint,
            failure_category=failure_category,
            requires_approval=policy.requires_approval,
            attempt=1,
            max_attempts=policy.max_attempts,
            skip_reason="recursive_self_repair",
        )
    if failure_category == "outcome_uncertain":
        return _skipped_decision(
            status=status,
            mode=mode,
            source_kind=source_kind,
            source_sequence=source_sequence,
            fingerprint=fingerprint,
            failure_category=failure_category,
            requires_approval=policy.requires_approval,
            attempt=1,
            max_attempts=policy.max_attempts,
            skip_reason="side_effect_outcome_uncertain",
            severity="warning",
        )
    return SelfRepairDecision(
        kind="repair.classified",
        trigger="runtime_failure",
        action="draft_repair_proposal",
        severity="warning",
        status=status,
        mode=mode,
        source_kind=source_kind,
        source_sequence=source_sequence,
        fingerprint=fingerprint,
        failure_category=failure_category,
        requires_approval=policy.requires_approval,
        automatic_execution=False,
        attempt=1,
        max_attempts=policy.max_attempts,
    )


def _skipped_decision(
    *,
    status: RunStatus,
    mode: TaskMode | None,
    source_kind: str,
    source_sequence: int,
    fingerprint: str,
    failure_category: str,
    requires_approval: bool,
    attempt: int,
    max_attempts: int,
    skip_reason: str,
    severity: str = "info",
) -> SelfRepairDecision:
    return SelfRepairDecision(
        kind="repair.skipped",
        trigger="runtime_failure",
        action="manual_review",
        severity=severity,
        status=status,
        mode=mode,
        source_kind=source_kind,
        source_sequence=source_sequence,
        fingerprint=fingerprint,
        failure_category=failure_category,
        requires_approval=requires_approval,
        automatic_execution=False,
        attempt=attempt,
        max_attempts=max_attempts,
        skip_reason=skip_reason,
    )


def repair_context_from_proposal(proposal: Mapping[str, object]) -> dict[str, object]:
    """Build a bounded repair context from an approved safe proposal."""

    failure_kind = _safe_text(proposal.get("failure_kind"), default="runtime_failure")
    attempt = _safe_int(proposal.get("attempt"), default=1, minimum=1, maximum=3)
    max_attempts = _safe_int(
        proposal.get("max_attempts"),
        default=1,
        minimum=attempt,
        maximum=3,
    )
    return {
        "schema_version": 1,
        "source": "self_repair",
        "source_run_id": _safe_text(proposal.get("source_run_id"), default="unknown"),
        "source_event_sequence": _safe_int(
            proposal.get("source_event_sequence"),
            default=0,
            minimum=0,
            maximum=2**63 - 1,
        ),
        "fingerprint": _safe_text(proposal.get("fingerprint"), default=""),
        "failure_kind": failure_kind,
        "repair_action": _safe_text(
            proposal.get("repair_action"),
            default="draft_repair_proposal",
        ),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "instruction": _safe_text(
            proposal.get("instruction"),
            default=_repair_instruction(failure_kind),
            max_chars=240,
        ),
        "requires_approval": True,
        "automatic_execution": False,
    }


def _repair_instruction(failure_category: str) -> str:
    if failure_category == "capacity_pressure":
        return "用更小的输入和更低负载重试，必要时标记模型 fallback，但不要绕过审批或隐藏失败。"
    if failure_category == "empty_model_response":
        return (
            "先压缩输入和历史上下文，再拆分提示或降负载重试；必要时标记模型 "
            "fallback/切换备用模型，重试后仍为空则保留中断前输出并闭环失败。"
        )
    if failure_category == "tool_failure":
        return "先检查工具权限、参数和产物状态，只执行可审计的最小修复步骤。"
    if failure_category == "step_failure":
        return "定位失败步骤，压缩上下文后只重做必要步骤，并保留原始交付目标。"
    return "定位失败原因，提出并执行一次受控的最小修复重试，失败后关闭循环交由人工处理。"


def _safe_text(value: object, *, default: str, max_chars: int = 128) -> str:
    if not isinstance(value, str):
        return default
    text = " ".join(value.split())[:max_chars]
    return text or default


def _safe_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is int:
        return min(max(value, minimum), maximum)
    return default


def _already_classified(events: Sequence[RunEvent]) -> bool:
    return any(_kind_text(event.kind) in _REPAIR_EVENT_KINDS for event in events)


def _supervisor_source_failure(events: Sequence[RunEvent]) -> RunEvent | None:
    uncertain = _first_uncertain_failure_event(events)
    return uncertain if uncertain is not None else _last_failure_event(events)


def _first_uncertain_failure_event(events: Sequence[RunEvent]) -> RunEvent | None:
    for event in events:
        if event.kind in _FAILURE_KINDS and _failure_category(event) == "outcome_uncertain":
            return event
    return None


def _last_failure_event(events: Sequence[RunEvent]) -> RunEvent | None:
    for event in reversed(events):
        if event.kind in _FAILURE_KINDS:
            return event
    return None


def _failure_category(event: RunEvent) -> str:
    if event.payload.get("failure_kind") == "uncertain":
        return "outcome_uncertain"
    text = _failure_text(event)
    if _contains_marker(text, _UNCERTAIN_MARKERS):
        return "outcome_uncertain"
    if _contains_marker(text, _EMPTY_RESPONSE_MARKERS):
        return "empty_model_response"
    if _contains_marker(text, _CAPACITY_MARKERS):
        return "capacity_pressure"
    if event.kind is EventKind.TOOL_FAILED:
        return "tool_failure"
    if event.kind is EventKind.STEP_FAILED:
        return "step_failure"
    return "runtime_failure"


def _fingerprint(
    *,
    status: RunStatus,
    mode: TaskMode | None,
    source_kind: str,
    failure_category: str,
    failure: RunEvent | None,
) -> str:
    payload = {
        "status": status.value,
        "mode": None if mode is None else mode.value,
        "source_kind": source_kind,
        "failure_category": failure_category,
        "reason_digest": None
        if failure is None
        else hashlib.sha256(_failure_text(failure).encode()).hexdigest(),
        "tool_name": None if failure is None else failure.tool_name,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _failure_text(event: RunEvent) -> str:
    parts: list[str] = []
    if event.reason:
        parts.append(event.reason)
    for key in ("error", "error_type", "status", "status_code", "failure", "failure_kind", "stage"):
        value = event.payload.get(key)
        if type(value) in {str, int}:
            parts.append(str(value))
    return " ".join(parts).casefold()


def _contains_marker(text: str, markers: frozenset[str]) -> bool:
    return any(marker in text for marker in markers)


def _kind_text(kind: EventKind | str) -> str:
    return kind.value if isinstance(kind, EventKind) else kind


def _routing_source(routing_decision: Mapping[str, object] | None) -> str | None:
    if routing_decision is None:
        return None
    source = routing_decision.get("source")
    return source if type(source) is str else None


__all__ = [
    "SelfRepairDecision",
    "SelfRepairPolicy",
    "classify_terminal_run",
    "repair_context_from_proposal",
]
