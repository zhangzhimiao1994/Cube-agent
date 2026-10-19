"""Preserve native prerequisite observations without granting execution authority."""

from __future__ import annotations

from collections.abc import Mapping

from agent_hub.capabilities.project_prerequisites import validated_project_prerequisite_payload
from agent_hub.runtime.contracts import JsonValue


def project_prerequisite_feedback(
    payload: object, *, tool_name: object,
) -> Mapping[str, JsonValue] | None:
    if tool_name != "project.verify" or not isinstance(payload, Mapping):
        return None
    if set(payload) == {"status", "untrusted", "verification_blocked"}:
        if payload["status"] != "failed" or payload["untrusted"] is not True:
            return None
    elif set(payload) != {"verification_blocked"}:
        return None
    report = validated_project_prerequisite_payload(payload["verification_blocked"])
    if report is None:
        return None
    return {"status": "failed", "untrusted": True, "verification_blocked": report}
