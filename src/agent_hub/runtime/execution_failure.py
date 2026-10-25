"""Pure validation of bounded execution-failure observations."""

from __future__ import annotations

import re
from collections.abc import Mapping

from agent_hub.capabilities.project_verification import validated_project_failure_payload
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.project_prerequisite_feedback import project_prerequisite_feedback


def execution_failure_result(
    payload: object, *, tool_name: object = None,
) -> Mapping[str, JsonValue] | None:
    """Accept only the bounded F01 receipt supplied by the trusted tool gateway."""
    if not isinstance(payload, Mapping) or {"approval_id", "generated_draft_rejection"} & payload.keys():
        return None
    if "python_verification_failure" in payload:
        from agent_hub.runtime.python_project_history import python_failure_feedback

        return python_failure_feedback(payload, tool_name=tool_name)
    if "verification_blocked" in payload:
        return project_prerequisite_feedback(payload, tool_name=tool_name)
    diagnostic = payload.get("execution_diagnostic")
    if not isinstance(diagnostic, Mapping) or set(diagnostic) != {
        "schema_version", "backend", "exit_code", "timed_out", "stdout", "stderr",
        "stdout_metadata", "stderr_metadata",
    }:
        return None
    exit_code, timed_out = diagnostic["exit_code"], diagnostic["timed_out"]
    if (
        type(diagnostic["schema_version"]) is not int or diagnostic["schema_version"] != 1
        or type(timed_out) is not bool
        or (exit_code is not None and type(exit_code) is not int)
        or (not timed_out and (exit_code is None or exit_code == 0))
        or type(diagnostic["backend"]) is not str
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", diagnostic["backend"]) is None
    ):
        return None
    for stream in ("stdout", "stderr"):
        text, metadata = diagnostic[stream], diagnostic[f"{stream}_metadata"]
        if (
            type(text) is not str or len(text.encode("utf-8")) > 4096
            or not isinstance(metadata, Mapping)
            or set(metadata) != {
                "sanitized_bytes", "returned_bytes", "sha256", "truncated", "redacted", "redacted_lines",
            }
            or any(type(metadata[key]) is not int or metadata[key] < 0
                   for key in ("sanitized_bytes", "returned_bytes", "redacted_lines"))
            or metadata["returned_bytes"] != len(text.encode("utf-8"))
            or metadata["sanitized_bytes"] < metadata["returned_bytes"]
            or any(type(metadata[key]) is not bool for key in ("truncated", "redacted"))
            or type(metadata["sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is None
        ):
            return None
    result: dict[str, JsonValue] = {
        "status": "failed", "untrusted": True, "execution_diagnostic": diagnostic,
    }
    if "verification_failure" in payload:
        report = validated_project_failure_payload(payload["verification_failure"], diagnostic=diagnostic)
        if report is None:
            return None
        result["verification_failure"] = report
    return result
