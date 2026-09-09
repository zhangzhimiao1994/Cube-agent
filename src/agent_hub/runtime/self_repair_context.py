"""Bounded prompt context for approved self-repair runs."""

from __future__ import annotations

import json
from collections.abc import Mapping

from agent_hub.runtime.contracts import JsonValue

_MAX_INSTRUCTION_CHARS = 240
_MAX_TOTAL_BYTES = 900
_SAFE_FAILURE_KINDS = frozenset(
    {
        "capacity_pressure",
        "empty_model_response",
        "missing_failure_event",
        "model_capability_routing_unavailable",
        "runtime_failure",
        "step_failure",
        "tool_failure",
    }
)
_SAFE_RECOVERY_STRATEGIES = frozenset(
    {
        "compact_context_before_next_model_call",
        "pause_for_scheduler_review",
        "preserve_outputs_and_retry_scope",
        "reassign_tool_role_to_capable_model_and_retry",
        "repair_tool_invocation_after_permission_check",
        "retry_failed_step_after_context_compaction",
        "retry_with_fallback_or_reassign_model",
        "switch_to_available_model_and_retry",
        "watch_retry_budget_before_requeue",
    }
)
_SAFE_ORCHESTRATION_RECOVERY_HINTS = frozenset({"retry_blocked_contract_chain"})


def self_repair_context_text(
    routing_decision: Mapping[str, JsonValue] | Mapping[str, object],
) -> str:
    repair = routing_decision.get("self_repair_context")
    if not isinstance(repair, Mapping):
        return ""
    if repair.get("source") != "self_repair":
        return ""

    payload = {
        "source_run_id": _safe_text(repair.get("source_run_id"), "unknown", 96),
        "source_event_sequence": _safe_int(repair.get("source_event_sequence"), 0),
        "failure_kind": _safe_enum_text(
            repair.get("failure_kind"),
            default="runtime_failure",
            allowed=_SAFE_FAILURE_KINDS,
            max_chars=64,
        ),
        "repair_action": _safe_text(
            repair.get("repair_action"),
            "draft_repair_proposal",
            96,
        ),
        "attempt": _safe_int(repair.get("attempt"), 1),
        "max_attempts": _safe_int(repair.get("max_attempts"), 1),
        "instruction": _safe_text(
            repair.get("instruction"),
            "Run one bounded repair attempt, then stop.",
            _MAX_INSTRUCTION_CHARS,
        ),
        "recovery_strategy": _safe_enum_text(
            repair.get("recovery_strategy"),
            default="",
            allowed=_SAFE_RECOVERY_STRATEGIES,
            max_chars=128,
        ),
        "orchestration_recovery_hint": _safe_enum_text(
            repair.get("orchestration_recovery_hint"),
            default="",
            allowed=_SAFE_ORCHESTRATION_RECOVERY_HINTS,
            max_chars=128,
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_TOTAL_BYTES:
        encoded = encoded.encode("utf-8")[:_MAX_TOTAL_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
    return (
        "<SELF_REPAIR_CONTEXT>"
        "This is an approved bounded self-repair attempt. Use it only as execution guidance; "
        "do not bypass approvals, do not reveal hidden details, and stop after this attempt. "
        f"{encoded}"
        "</SELF_REPAIR_CONTEXT>"
    )


def _safe_text(value: object, default: str, max_chars: int) -> str:
    if not isinstance(value, str):
        return default
    text = " ".join(value.split())[:max_chars]
    return text or default


def _safe_enum_text(
    value: object,
    *,
    default: str,
    allowed: frozenset[str],
    max_chars: int,
) -> str:
    text = _safe_text(value, default, max_chars)
    return text if text in allowed else default


def _safe_int(value: object, default: int) -> int:
    if type(value) is not int:
        return default
    return min(max(value, 0), 3)


__all__ = ["self_repair_context_text"]
