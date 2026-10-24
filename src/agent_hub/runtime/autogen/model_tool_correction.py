"""Pure contracts for one policy-gated, same-tool correction."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast
from uuid import UUID

from agent_hub.models.types import ModelMessage, ModelRequest
from agent_hub.runtime.autogen.model_rejection import RejectedModelRecord, validate_rejection
from agent_hub.runtime.contracts import JsonValue

TOOL_CORRECTION_POLICY = "engineering_tool_correction_version"
_CORRECTIVE_MESSAGE = (
    "The previous response did not satisfy the required tool contract. "
    "Call the required tool exactly once using its existing parameter schema. "
    "Keep the current tool grant and return no other tool calls or final report. "
    "Use a native function call, not prose or JSON describing a tool call. "
)
_BOUNDARY_KEYS = {"required_tool", "max_output_tokens", "timeout_seconds"}
_REJECTION_KEYS = {"status", "request_hash", "rejection"}
_HASH = re.compile(r"[a-f0-9]{64}")
_TOOL = re.compile(r"[A-Za-z0-9_-]{1,64}")


def eligible_tool_rejection(record: RejectedModelRecord) -> bool:
    """Recognize observed output only; this grants no run budget or retry authority."""
    return (
        record.evidence_present and record.output_status == "completed"
        and record.reason == "invalid_tool" and record.usage_status == "known"
        and record.usage is not None and not record.negotiation_exhausted
    )


def _positive_timeout(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(cast(int | float, value)) and cast(int | float, value) > 0
    except OverflowError:
        return False


def _positive_cap(value: object) -> bool:
    return type(value) is int and 1 <= value <= 1_000_000


def tool_correction_request(
    request: ModelRequest, record: RejectedModelRecord, *,
    max_output_tokens: int, timeout_seconds: float,
) -> ModelRequest:
    """Append static guidance only for plain, non-native tool requests."""
    if (
        not eligible_tool_rejection(record)
        or request.native_tool_continuation
        or request.response_schema is not None
        or not request.required_tool or len(request.tools) != 1
        or request.tools[0].name != request.required_tool
        or request.logical_model != record.requested_logical_model
        or not _positive_cap(max_output_tokens)
        or max_output_tokens > request.max_output_tokens
        or not _positive_timeout(timeout_seconds)
        or timeout_seconds > request.timeout_seconds
    ):
        raise ValueError("tool correction request is outside the rejection boundary")
    return replace(
        request,
        messages=(*request.messages, ModelMessage(role="user", content=(
            _CORRECTIVE_MESSAGE + f"Required native function name: {request.required_tool}."))),
        max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
    )


def _validate_boundary(row: Mapping[str, JsonValue]) -> None:
    tool, digest = row.get("required_tool"), row.get("request_hash")
    if (
        type(tool) is not str or _TOOL.fullmatch(tool) is None
        or type(digest) is not str or _HASH.fullmatch(digest) is None
        or not _positive_cap(row.get("max_output_tokens"))
        or not _positive_timeout(row.get("timeout_seconds"))
    ):
        raise ValueError("tool correction boundary is invalid")


def _validate_pair(
    previous: Mapping[str, JsonValue], current: Mapping[str, JsonValue],
    record: RejectedModelRecord,
) -> None:
    if (
        "correction_for" in previous or not eligible_tool_rejection(record)
        or set(previous) != _REJECTION_KEYS | _BOUNDARY_KEYS
    ):
        raise ValueError("tool correction predecessor is invalid")
    _validate_boundary(previous)
    _validate_boundary(current)
    if (
        current["required_tool"] != previous["required_tool"]
        or current["request_hash"] == previous["request_hash"]
        or cast(int, current["max_output_tokens"]) > cast(int, previous["max_output_tokens"])
        or cast(float, current["timeout_seconds"]) > cast(float, previous["timeout_seconds"])
    ):
        raise ValueError("tool correction expands or repeats the rejected request")
    required = {"status", "request_hash", "correction_for"} | _BOUNDARY_KEYS
    status = current.get("status")
    if status == "succeeded":
        required |= {"artifact_id", "artifact_sha256"}
        artifact_id, digest = current.get("artifact_id"), current.get("artifact_sha256")
        if (
            type(artifact_id) is not str or str(UUID(artifact_id)) != artifact_id
            or type(digest) is not str or _HASH.fullmatch(digest) is None
        ):
            raise ValueError("tool correction artifact reference is invalid")
    elif status == "rejected":
        required.add("rejection")
    elif status != "running":
        raise ValueError("tool correction outcome is invalid")
    if set(current) != required:
        raise ValueError("tool correction ledger schema is invalid")


def validate_rejected_model_rows(
    rows: Sequence[Mapping[str, JsonValue]], *, run_id: UUID, tenant_id: UUID,
    repair_enabled: bool = False,
) -> tuple[RejectedModelRecord, ...]:
    """Validate retained rejections, allowing one linked successor only with opt-in.

    Ordinary ledger rows remain the caller's responsibility. Returned receipts
    count retained completed output, never additional execution allowance.
    """
    if type(repair_enabled) is not bool:
        raise ValueError("tool correction policy switch must be a boolean")
    records: dict[int, RejectedModelRecord] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError("model ledger row is invalid")  # noqa: TRY004 - persisted data
        correction = "correction_for" in row
        if correction:
            link = row["correction_for"]
            if (
                not repair_enabled or type(link) is not int or link != position - 1
                or position == 0 or position - 1 not in records
            ):
                raise ValueError("tool correction linkage is invalid")
            _validate_pair(rows[position - 1], row, records[position - 1])
            if row.get("status") == "running" and position != len(rows) - 1:
                raise ValueError("running tool correction must be last")
        if row.get("status") != "rejected" and "rejection" not in row:
            continue
        optional = _BOUNDARY_KEYS | {"report_binding"}
        if correction:
            optional |= {"correction_for"}
        value = row.get("rejection")
        if (
            row.get("status") != "rejected" or not isinstance(value, Mapping)
            or not _REJECTION_KEYS <= set(row) <= _REJECTION_KEYS | optional
        ):
            raise ValueError("rejection ledger schema is invalid")
        try:
            records[position] = validate_rejection(
                value, run_id=run_id, tenant_id=tenant_id, position=position,
                request_hash=row.get("request_hash"),
            )
        except (ArithmeticError, KeyError, TypeError, ValueError):
            raise ValueError("rejection record is invalid") from None
        if position != len(rows) - 1:
            following = rows[position + 1]
            if (
                not repair_enabled or correction or not isinstance(following, Mapping)
                or type(following.get("correction_for")) is not int
                or following["correction_for"] != position
            ):
                raise ValueError("rejected model row must be last or have one linked correction")
    return tuple(records.values())
