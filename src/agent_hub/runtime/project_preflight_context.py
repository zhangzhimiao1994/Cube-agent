"""Bounded prompt context for approved project architecture preflight runs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from agent_hub.runtime.contracts import JsonValue

_EXPECTED_KIND = "project_architecture_preflight"
_DEFAULT_CAPABILITY = "project.preflight_architecture"
_DEFAULT_PLAN_PATH = "PROJECT_ARCHITECTURE_PLAN.md"
_DEFAULT_GRAPH_PATH = "architecture-map.html"
_MAX_SUMMARY_CHARS = 240
_MAX_PATH_CHARS = 160
_MAX_CAPABILITY_CHARS = 96
_MAX_TOTAL_BYTES = 900
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./ -]{1,160}$")


def project_preflight_context_text(
    routing_decision: Mapping[str, JsonValue] | Mapping[str, object],
) -> str:
    """Return execution guidance for an approved large-project preflight."""

    if routing_decision.get("project_preflight_approved") is not True:
        return ""
    proposal = routing_decision.get("project_preflight_proposal")
    if not isinstance(proposal, Mapping):
        return ""
    if proposal.get("kind") != _EXPECTED_KIND:
        return ""

    payload: dict[str, JsonValue] = {
        "kind": _EXPECTED_KIND,
        "capability": _safe_capability(proposal.get("capability")),
        "plan_path": _safe_path(proposal.get("plan_path"), default=_DEFAULT_PLAN_PATH),
        "graph_path": _safe_path(proposal.get("graph_path"), default=_DEFAULT_GRAPH_PATH),
        "requires_constraints_and_skills_reading": (
            proposal.get("requires_constraints_and_skills_reading") is True
        ),
        "implementation_stage_output_fields": (
            "stage_status",
            "verification_evidence",
            "remaining_risks",
            "stage_repair_actions",
        ),
        "review_stage_output_fields": ("acceptance_review",),
    }
    summary = _safe_text(proposal.get("summary"), _MAX_SUMMARY_CHARS)
    if summary:
        payload["summary"] = summary

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_TOTAL_BYTES:
        encoded = encoded.encode("utf-8")[:_MAX_TOTAL_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        "<PROJECT_PREFLIGHT_CONTEXT>"
        "This approved project preflight is required execution guidance. "
        "First read applicable constraints and skill rules, then use "
        "project.preflight_architecture when available to create or update "
        "PROJECT_ARCHITECTURE_PLAN.md and architecture-map.html before staged implementation. "
        "Keep implementation staged, verified, structured with stage evidence fields, "
        "diagnose failed stages before escalating, repair within approved boundaries, "
        "rerun verification, record stage_repair_actions, and stay within existing approval boundaries. "
        f"{encoded}"
        "</PROJECT_PREFLIGHT_CONTEXT>"
    )


def _safe_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_chars]


def _safe_token(value: object, *, default: str, max_chars: int) -> str:
    text = _safe_text(value, max_chars)
    if not text or _SAFE_TOKEN.fullmatch(text) is None:
        return default
    return text


def _safe_capability(value: object) -> str:
    if value != _DEFAULT_CAPABILITY:
        return _DEFAULT_CAPABILITY
    return _safe_token(
        value,
        default=_DEFAULT_CAPABILITY,
        max_chars=_MAX_CAPABILITY_CHARS,
    )


def _safe_path(value: object, *, default: str) -> str:
    text = _safe_text(value, _MAX_PATH_CHARS)
    if not text or _SAFE_PATH.fullmatch(text) is None:
        return default
    return text


__all__ = ["project_preflight_context_text"]
