"""Bounded prompt context for approved self-repair runs."""

from __future__ import annotations

import json
from collections.abc import Mapping

from agent_hub.runtime.contracts import JsonValue

_MAX_INSTRUCTION_CHARS = 240
_MAX_TOTAL_BYTES = 900


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
        "failure_kind": _safe_text(repair.get("failure_kind"), "runtime_failure", 64),
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


def _safe_int(value: object, default: int) -> int:
    if type(value) is not int:
        return default
    return min(max(value, 0), 3)


__all__ = ["self_repair_context_text"]
