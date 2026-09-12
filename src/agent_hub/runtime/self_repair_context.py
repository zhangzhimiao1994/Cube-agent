"""Bounded prompt context for approved self-repair runs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from agent_hub.models.types import ModelCapability
from agent_hub.recovery_metadata import (
    ORCHESTRATION_CONTRACT_RECOVERY_HINT,
    ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY,
    SAFE_SELF_REPAIR_FAILURE_KINDS,
    SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS,
    SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
)
from agent_hub.runtime.contracts import JsonValue

_MAX_INSTRUCTION_CHARS = 240
_MAX_TOTAL_BYTES = 900
_SAFE_CONTRACT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}-to-[A-Za-z0-9_.:-]{1,96}$")
_SAFE_ROLE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_BLOCKED_CONTRACT_IDS = 8
_MAX_ROLE_CAPABILITY_REQUIREMENTS = 8
_MAX_REQUIRED_CAPABILITIES = 8
_MODEL_CAPABILITY_RECOVERY_STRATEGY = "reassign_tool_role_to_capable_model_and_retry"


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
            allowed=SAFE_SELF_REPAIR_FAILURE_KINDS,
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
            allowed=SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
            max_chars=128,
        ),
        "orchestration_recovery_hint": _safe_enum_text(
            repair.get("orchestration_recovery_hint"),
            default="",
            allowed=SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS,
            max_chars=128,
        ),
    }
    blocked_contract_ids = (
        _safe_contract_ids(repair.get("blocked_contract_ids"))
        if routing_decision.get("source") == "self_repair"
        and payload["recovery_strategy"] == ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY
        and payload["orchestration_recovery_hint"] == ORCHESTRATION_CONTRACT_RECOVERY_HINT
        else ()
    )
    if blocked_contract_ids:
        payload["blocked_contract_ids"] = blocked_contract_ids
    role_capability_requirements = (
        _safe_role_capability_requirements(repair.get("role_capability_requirements"))
        if routing_decision.get("source") == "self_repair"
        and payload["recovery_strategy"] == _MODEL_CAPABILITY_RECOVERY_STRATEGY
        else ()
    )
    if role_capability_requirements:
        payload["role_capability_requirements"] = role_capability_requirements
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


def self_repair_recovery_plan_payload(
    routing_decision: Mapping[str, JsonValue] | Mapping[str, object],
) -> Mapping[str, JsonValue] | None:
    repair = routing_decision.get("self_repair_context")
    if routing_decision.get("source") != "self_repair" or not isinstance(repair, Mapping):
        return None
    if repair.get("source") != "self_repair":
        return None
    recovery_strategy = _safe_enum_text(
        repair.get("recovery_strategy"),
        default="",
        allowed=SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
        max_chars=128,
    )
    orchestration_recovery_hint = _safe_enum_text(
        repair.get("orchestration_recovery_hint"),
        default="",
        allowed=SAFE_SELF_REPAIR_ORCHESTRATION_RECOVERY_HINTS,
        max_chars=128,
    )
    if recovery_strategy != ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY:
        if recovery_strategy != _MODEL_CAPABILITY_RECOVERY_STRATEGY:
            return None
        payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "status": "active",
            "recovery_strategy": _MODEL_CAPABILITY_RECOVERY_STRATEGY,
            "replan_scope": "model_capability_roles",
            "reuse_completed_artifacts": True,
            "automatic_execution": False,
        }
        role_capability_requirements = _safe_role_capability_requirements(
            repair.get("role_capability_requirements"),
        )
        if role_capability_requirements:
            payload["role_capability_requirements"] = role_capability_requirements
        return payload
    if orchestration_recovery_hint != ORCHESTRATION_CONTRACT_RECOVERY_HINT:
        return None
    payload = {
        "schema_version": 1,
        "status": "active",
        "recovery_strategy": ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY,
        "orchestration_recovery_hint": ORCHESTRATION_CONTRACT_RECOVERY_HINT,
        "replan_scope": "blocked_contract_chain",
        "reuse_completed_artifacts": True,
        "retry_blocked_contracts_only": True,
        "automatic_execution": False,
    }
    blocked_contract_ids = _safe_contract_ids(repair.get("blocked_contract_ids"))
    if blocked_contract_ids:
        payload["blocked_contract_ids"] = blocked_contract_ids
    return payload


def self_repair_role_capability_requirements(
    routing_decision: Mapping[str, JsonValue] | Mapping[str, object],
) -> Mapping[str, frozenset[ModelCapability]]:
    repair = routing_decision.get("self_repair_context")
    if routing_decision.get("source") != "self_repair" or not isinstance(repair, Mapping):
        return {}
    if repair.get("source") != "self_repair":
        return {}
    recovery_strategy = _safe_enum_text(
        repair.get("recovery_strategy"),
        default="",
        allowed=SAFE_SELF_REPAIR_RECOVERY_STRATEGIES,
        max_chars=128,
    )
    if recovery_strategy != _MODEL_CAPABILITY_RECOVERY_STRATEGY:
        return {}
    requirements: dict[str, frozenset[ModelCapability]] = {}
    for item in _safe_role_capability_requirements(repair.get("role_capability_requirements")):
        role_id = item.get("role_id")
        capabilities = item.get("required_capabilities")
        if not isinstance(role_id, str) or not isinstance(capabilities, tuple):
            continue
        parsed = frozenset(
            ModelCapability(capability)
            for capability in capabilities
            if isinstance(capability, str)
        )
        if parsed:
            requirements[role_id] = parsed
    return requirements


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


def _safe_contract_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    safe: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if _SAFE_CONTRACT_ID.fullmatch(text) is None:
            continue
        if text in safe:
            continue
        safe.append(text)
        if len(safe) >= _MAX_BLOCKED_CONTRACT_IDS:
            break
    return tuple(safe)


def _safe_role_capability_requirements(value: object) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    safe: list[Mapping[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        role_id = item.get("role_id")
        if not isinstance(role_id, str):
            continue
        role_id = role_id.strip()
        if _SAFE_ROLE_ID.fullmatch(role_id) is None:
            continue
        capabilities = _safe_model_capabilities(item.get("required_capabilities"))
        if not capabilities:
            continue
        if any(existing.get("role_id") == role_id for existing in safe):
            continue
        safe.append(
            {
                "role_id": role_id,
                "required_capabilities": capabilities,
            }
        )
        if len(safe) >= _MAX_ROLE_CAPABILITY_REQUIREMENTS:
            break
    return tuple(safe)


def _safe_model_capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    safe: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            capability = ModelCapability(item.strip())
        except ValueError:
            continue
        if capability.value in safe:
            continue
        safe.append(capability.value)
        if len(safe) >= _MAX_REQUIRED_CAPABILITIES:
            break
    return tuple(safe)


__all__ = [
    "self_repair_context_text",
    "self_repair_recovery_plan_payload",
    "self_repair_role_capability_requirements",
]
