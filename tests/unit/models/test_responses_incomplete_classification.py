"""Only explicit, error-free incomplete output can authorize recovery."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_hub.models.responses import ResponsesContractError, parse_response
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    RejectedOutputEvidence,
    TokenUsage,
    ToolDefinition,
)
from tests.contracts.test_responses_client import message, request, response


def tool_request() -> ModelRequest:
    return request(required_capabilities=frozenset({
        ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
    }), tools=(ToolDefinition(
        name="web_search", description="Search", parameters={
            "type": "object", "properties": {"q": {"type": "string"}},
            "required": ("q",), "additionalProperties": False,
        },
    ),))


def valid_tool() -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call", status="completed", call_id="call-owned",
        name="web_search", arguments='{"q":"safe"}',
    )


def rejected_evidence(raw: SimpleNamespace) -> RejectedOutputEvidence:
    # A valid preceding tool item must not escape a rejected mixed response.
    with pytest.raises(ResponsesContractError) as caught:
        parse_response(raw, tool_request())
    evidence = caught.value.evidence
    assert evidence is not None
    assert evidence.usage_status == "known"
    assert evidence.usage == TokenUsage(12, 117, 129)
    assert evidence.final_text is None and evidence.text_sha256 is None
    return evidence


@pytest.mark.parametrize("boundary", [
    "top_incomplete_error", "item_failed", "item_incomplete_error", "item_completed_error",
    "item_completed_incomplete_details", "message_wrong_role", "message_missing_role",
    "message_missing_status", "incomplete_message_wrong_role", "incomplete_message_missing_role",
    "incomplete_unsupported_item",
])
def test_malformed_or_failed_output_is_not_qualified_incomplete(boundary: str) -> None:
    item = message()
    raw = response(output=[valid_tool(), item])
    if boundary == "top_incomplete_error":
        raw.status = "incomplete"
        raw.error = SimpleNamespace(code="provider_failed", message="synthetic error")
    elif boundary == "item_failed":
        item.status = "failed"
    elif boundary in {"item_incomplete_error", "item_completed_error"}:
        item.status = "incomplete" if boundary == "item_incomplete_error" else "completed"
        item.error = SimpleNamespace(code="provider_failed", message="synthetic error")
    elif boundary == "item_completed_incomplete_details":
        item.incomplete_details = SimpleNamespace(reason="max_output_tokens")
    elif boundary == "message_wrong_role":
        item.role = "user"
    elif boundary == "message_missing_role":
        item.role = None
    elif boundary == "message_missing_status":
        item.status = None
    elif boundary in {"incomplete_message_wrong_role", "incomplete_message_missing_role"}:
        item.status = "incomplete"
        item.role = "user" if boundary == "incomplete_message_wrong_role" else None
    elif boundary == "incomplete_unsupported_item":
        item.type = "web_search_call"
        item.status = "incomplete"
    evidence = rejected_evidence(raw)
    assert evidence.reason == "invalid_output"
    assert evidence.status != "incomplete"


@pytest.mark.parametrize("boundary", ["top", "item"])
def test_explicit_error_free_incomplete_preserves_usage_without_accepting_tools(
    boundary: str,
) -> None:
    item = message(status="incomplete" if boundary == "item" else "completed")
    raw = response(output=[valid_tool(), item])
    if boundary == "top":
        raw.status = "incomplete"
        raw.incomplete_details = SimpleNamespace(reason="max_output_tokens")
    evidence = rejected_evidence(raw)
    assert evidence.reason == "incomplete" and evidence.status == "incomplete"
