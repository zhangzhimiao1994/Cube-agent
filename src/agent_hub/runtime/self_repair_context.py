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
from agent_hub.runtime.contracts import Artifact, JsonValue

_MAX_INSTRUCTION_CHARS = 240
_MAX_TOTAL_BYTES = 900
_SAFE_CONTRACT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}-to-[A-Za-z0-9_.:-]{1,96}$")
_SAFE_ROLE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}$")
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(authorization:\s*bearer\s+\S+|bearer\s+\S+|secret://\S+|sk-[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_MAX_BLOCKED_CONTRACT_IDS = 8
_MAX_ROLE_CAPABILITY_REQUIREMENTS = 8
_MAX_REQUIRED_CAPABILITIES = 8
_MODEL_CAPABILITY_RECOVERY_STRATEGY = "reassign_tool_role_to_capable_model_and_retry"
_PLUGIN_RUNTIME_RECOVERY_STRATEGY = "repair_plugin_endpoint_or_adapter_and_retry"
_MCP_RUNTIME_RECOVERY_STRATEGY = "repair_mcp_server_or_adapter_and_retry"
_RUNTIME_RECOVERY_SCOPES_BY_STRATEGY = {
    _PLUGIN_RUNTIME_RECOVERY_STRATEGY: "plugin_runtime",
    _MCP_RUNTIME_RECOVERY_STRATEGY: "mcp_runtime",
}


def project_repair_failure_text(
    artifact: Artifact, *, expected_artifact_id: str, expected_artifact_sha256: str,
    validated_feedback: Mapping[str, JsonValue],
) -> str:
    """Project an already scope-validated failed receipt as DATA, never authority.

    The caller must use the normal draft/execution receipt validator first. The
    phase's immutable artifact binding is checked again before projecting it.
    V2 drafts retain only the bounded closure as dependency_closure schema v1;
    Root environment, digests and scope are not part of this prompt projection.
    """
    if (artifact.type != "tool_result" or str(artifact.id) != expected_artifact_id
            or artifact.content_sha256 != expected_artifact_sha256
            or artifact.recompute_content_sha256() != expected_artifact_sha256
            or artifact.content.get("result") != validated_feedback
            or validated_feedback.get("status") != "failed"
            or validated_feedback.get("untrusted") is not True):
        raise ValueError("project repair failure evidence is invalid")
    tool = artifact.content.get("tool_name")
    if not isinstance(tool, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool) is None:
        raise ValueError("project repair failure evidence is invalid")
    diagnostic: dict[str, JsonValue] = {}
    if "generated_draft_rejection" in validated_feedback:
        from agent_hub.capabilities.generated_project_draft import validated_generated_draft_payload

        draft = validated_generated_draft_payload(validated_feedback["generated_draft_rejection"])
        if draft is None:
            raise ValueError("project repair failure evidence is invalid")
        projected_draft: dict[str, JsonValue] = {
            key: draft[key] for key in ("reason", "issues", "observation_sha256", "execution_started")
        }
        if draft["schema_version"] == 2:
            observation = draft["dependency_closure"]
            assert isinstance(observation, Mapping)
            closure = observation["closure"]
            assert isinstance(closure, Mapping)
            projected_draft["schema_version"] = 2
            projected_draft["dependency_closure"] = {"schema_version": 1, **closure}
        diagnostic["generated_draft_rejection"] = projected_draft
    else:
        diagnostic = {key: validated_feedback[key] for key in (
            "execution_diagnostic", "verification_failure", "python_verification_failure", "verification_blocked",
        ) if key in validated_feedback}
    if not diagnostic:
        raise ValueError("project repair failure evidence is invalid")
    redacted = False

    def project(value: JsonValue) -> JsonValue:
        nonlocal redacted
        if isinstance(value, str) and _SENSITIVE_TEXT_PATTERN.search(value):
            redacted = True
            return "[redacted diagnostic]"
        if isinstance(value, Mapping):
            return {key: project(item) for key, item in value.items()}
        if isinstance(value, tuple | list):
            return tuple(project(item) for item in value)
        return value

    projected = project(diagnostic)
    return "UNTRUSTED_PROJECT_REPAIR_FEEDBACK_JSON=" + json.dumps({
        "schema_version": 1, "untrusted": True, "status": "failed", "tool_name": tool,
        "artifact_id": expected_artifact_id, "artifact_sha256": expected_artifact_sha256,
        "diagnostic": projected, "diagnostic_projection_redacted": redacted,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def self_repair_context_text(
    routing_decision: Mapping[str, JsonValue] | Mapping[str, object],
) -> str:
    repair = routing_decision.get("self_repair_context")
    if not isinstance(repair, Mapping):
        return ""
    if repair.get("source") != "self_repair":
        return ""

    automatic_project = (
        type(routing_decision.get("engineering_auto_repair_version")) is int
        and routing_decision.get("engineering_auto_repair_version") == 1
        and routing_decision.get("source") == "self_repair"
        and routing_decision.get("self_repair_accepted") is True
        and _safe_automatic_execution(repair)
        and repair.get("failure_kind") in ("runtime_failure", "step_failure", "tool_failure")
    )
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
    error_code = _safe_diagnostic_code(repair.get("error_code"))
    if error_code is not None:
        payload["error_code"] = error_code
    suggested_action = _safe_text(repair.get("suggested_action"), "", _MAX_INSTRUCTION_CHARS)
    if suggested_action:
        payload["suggested_action"] = suggested_action
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
    if automatic_project:
        payload["instruction"] = (
            "Diagnose the observed failure; try a different evidence-based approach when needed, "
            "then verify the acceptance criteria and deliver."
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_TOTAL_BYTES:
        encoded = encoded.encode("utf-8")[:_MAX_TOTAL_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
    if automatic_project:
        return (
            "<SELF_REPAIR_CONTEXT>"
            "Continue the engineering task within original permissions and remaining hard budgets. "
            "A failed approach is evidence for diagnosis, not a completed task or reason to give up. "
            "Keep confirmed results; replan instead of repeating an unchanged failing action. "
            "Do not replay UNKNOWN outcomes or bypass approvals. Ask for help only when a specific "
            "action needs additional authority, a human-only prerequisite is required, or no safe "
            "executable alternative remains within the budget. Never claim unverified delivery. "
            f"{encoded}</SELF_REPAIR_CONTEXT>"
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
    automatic_execution = _safe_automatic_execution(repair)
    if recovery_strategy != ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY:
        if recovery_strategy != _MODEL_CAPABILITY_RECOVERY_STRATEGY:
            runtime_scope = _RUNTIME_RECOVERY_SCOPES_BY_STRATEGY.get(recovery_strategy)
            if runtime_scope is None:
                return None
            runtime_payload: dict[str, JsonValue] = {
                "schema_version": 1,
                "status": "active",
                "recovery_strategy": recovery_strategy,
                "replan_scope": runtime_scope,
                "reuse_completed_artifacts": True,
                "refresh_runtime_capabilities": True,
                "retry_failed_capability_only": True,
                "automatic_execution": automatic_execution,
            }
            error_code = _safe_diagnostic_code(repair.get("error_code"))
            if error_code is not None:
                runtime_payload["diagnostic_error_code"] = error_code
            return runtime_payload
        model_payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "status": "active",
            "recovery_strategy": _MODEL_CAPABILITY_RECOVERY_STRATEGY,
            "replan_scope": "model_capability_roles",
            "reuse_completed_artifacts": True,
            "automatic_execution": automatic_execution,
        }
        role_capability_requirements = _safe_role_capability_requirements(
            repair.get("role_capability_requirements"),
        )
        if role_capability_requirements:
            model_payload["role_capability_requirements"] = role_capability_requirements
        return model_payload
    if orchestration_recovery_hint != ORCHESTRATION_CONTRACT_RECOVERY_HINT:
        return None
    contract_payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "status": "active",
        "recovery_strategy": ORCHESTRATION_CONTRACT_RECOVERY_STRATEGY,
        "orchestration_recovery_hint": ORCHESTRATION_CONTRACT_RECOVERY_HINT,
        "replan_scope": "blocked_contract_chain",
        "reuse_completed_artifacts": True,
        "retry_blocked_contracts_only": True,
        "automatic_execution": automatic_execution,
    }
    blocked_contract_ids = _safe_contract_ids(repair.get("blocked_contract_ids"))
    if blocked_contract_ids:
        contract_payload["blocked_contract_ids"] = blocked_contract_ids
    return contract_payload


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
    text = _SENSITIVE_TEXT_PATTERN.sub("[redacted]", " ".join(value.split()))[:max_chars]
    return text or default


def _safe_automatic_execution(repair: Mapping[str, object]) -> bool:
    return repair.get("automatic_execution") is True and repair.get("requires_approval") is False


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


def _safe_diagnostic_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:96]
    return text if _SAFE_DIAGNOSTIC_CODE.fullmatch(text) is not None else None


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
