"""Reject diagnostics distinguish synthetic shapes without changing private evidence."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.models.responses import ResponsesContractError, parse_response
from agent_hub.models.types import (
    ModelCapability,
    ModelRequest,
    RejectedOutputEvidence,
    RejectedOutputReason,
    RejectedOutputStatus,
    RejectedUsageStatus,
    StructuredResponseSchema,
    TokenUsage,
    ToolDefinition,
)
from tests.contracts.test_responses_client import message, request, response

SECRET = "synthetic-private-key-prompt-schema-arguments-error"
LOGGER = "agent_hub.models.responses"
VALID_TEXT = '{"verdict":"approve"}'
CASES = [
    ("top_status", "top_status", "invalid_output", "unknown", None),
    ("top_error", "top_error", "invalid_output", "unknown", None),
    ("top_incomplete_error", "top_error", "invalid_output", "unknown", None),
    ("top_incomplete", "top_incomplete", "incomplete", "incomplete", None),
    ("top_details", "top_incomplete_details", "invalid_output", "unknown", None),
    ("output_scalar", "output_container", "invalid_output", "unknown", None),
    ("output_empty", "output_container", "invalid_output", "unknown", None),
    ("output_many", "output_container", "invalid_output", "unknown", None),
    ("item_type", "item_type", "invalid_output", "unknown", None),
    ("item_role", "message_role", "invalid_output", "unknown", None),
    ("item_status", "item_status", "invalid_output", "unknown", None),
    ("item_error", "item_error", "invalid_output", "unknown", None),
    ("item_incomplete_error", "item_error", "invalid_output", "unknown", None),
    ("item_details", "item_incomplete_details", "invalid_output", "unknown", None),
    ("item_incomplete", "item_incomplete", "incomplete", "incomplete", None),
    ("message_status", "message_status", "invalid_output", "unknown", None),
    ("multiple_final", "multiple_final", "invalid_output", "unknown", None),
    ("content_scalar", "message_content_container", "invalid_output", "unknown", None),
    ("content_empty", "message_content_container", "invalid_output", "unknown", None),
    ("content_many", "message_content_container", "invalid_output", "unknown", None),
    ("content_type", "message_content_type", "invalid_output", "unknown", None),
    ("content_text", "message_content_type", "invalid_output", "unknown", None),
    ("refusal", "refusal", "refusal", "refused", None),
    ("mixed", "mixed_final_tool", "invalid_output", "unknown", None),
    ("no_final", "no_final", "invalid_output", "unknown", None),
    ("tool_name", "invalid_tool", "invalid_tool", "unknown", None),
    ("tool_id", "invalid_tool", "invalid_tool", "unknown", None),
    ("tool_duplicate", "invalid_tool", "invalid_tool", "unknown", None),
    ("tool_limit", "invalid_tool", "invalid_tool", "unknown", None),
    ("tool_json", "tool_json", "invalid_tool", "unknown", None),
    ("tool_json_scalar", "tool_json", "invalid_tool", "unknown", None),
    ("tool_json_duplicate", "tool_json", "invalid_tool", "unknown", None),
    ("tool_json_nonfinite", "tool_json", "invalid_tool", "unknown", None),
    ("tool_contract", "tool_json", "invalid_tool", "unknown", None),
    ("final_json", "final_json", "invalid_json", "completed", SECRET),
    ("final_json_duplicate", "final_json", "invalid_json", "completed", '{"x":1,"x":2}'),
    ("schema_mismatch", "generic", "schema_mismatch", "completed",
     json.dumps({"verdict": SECRET})),
    ("schema_configuration", "generic", "configuration_error", "unknown", None),
    ("internal_contract", "generic", "invalid_output", "unknown", None),
    ("output_limit", "output_limit", "output_limit", "completed", None),
    ("usage_missing", "usage_missing", "usage_missing", "completed", VALID_TEXT),
    ("usage_invalid", "usage_invalid", "usage_invalid", "completed", VALID_TEXT),
]


def _tool(**updates: Any) -> SimpleNamespace:
    return SimpleNamespace(**{
        "type": "function_call", "status": "completed", "call_id": "call_synthetic",
        "name": "synthetic_tool", "arguments": json.dumps({"secret": SECRET}), **updates,
    })


def _request() -> ModelRequest:
    return request(
        required_capabilities=frozenset({
            ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
        }),
        tools=(ToolDefinition(
            name="synthetic_tool", description=SECRET,
            parameters={"type": "object", "description": SECRET},
        ),),
        response_schema=StructuredResponseSchema(name="Synthetic", schema={
            "type": "object", "description": SECRET,
            "properties": {"verdict": {"const": "approve"}},
            "required": ("verdict",), "additionalProperties": False,
        }),
    )


def _payload(case: str) -> tuple[SimpleNamespace, ModelRequest]:
    item = message()
    raw = response(id=SECRET, model=SECRET, output=[item], secret=SECRET)
    req = _request()
    if case == "top_status":
        raw.status = SECRET
    elif case in {"top_error", "top_incomplete_error"}:
        raw.error = SimpleNamespace(code=SECRET, message=SECRET)
        raw.status = "incomplete" if case == "top_incomplete_error" else "completed"
    elif case == "top_incomplete":
        raw.status = "incomplete"
        raw.incomplete_details = {"reason": SECRET}
    elif case == "top_details":
        raw.incomplete_details = {"reason": SECRET}
    elif case.startswith("output_") and case != "output_limit":
        raw.output = {"output_scalar": SECRET, "output_empty": [],
                      "output_many": [item] * 129}[case]
    elif case == "item_type":
        item.type = SECRET
    elif case == "item_role":
        item.role = SECRET
        item.status = "incomplete"
    elif case == "item_status":
        item.status = SECRET
    elif case in {"item_error", "item_incomplete_error"}:
        item.error = {"code": SECRET, "message": SECRET}
        item.status = "incomplete" if case == "item_incomplete_error" else "completed"
    elif case == "item_details":
        item.incomplete_details = {"reason": SECRET}
    elif case == "item_incomplete":
        item.status = "incomplete"
    elif case == "message_status":
        item.status = None
    elif case == "multiple_final":
        raw.output = [item, message(SECRET)]
    elif case.startswith("content_"):
        item.content = {
            "content_scalar": SECRET, "content_empty": [],
            "content_many": item.content * 129,
            "content_type": [{"type": SECRET, "text": SECRET}],
            "content_text": [{"type": "output_text", "text": {"secret": SECRET}}],
        }[case]
    elif case == "refusal":
        item.content = [{"type": "refusal", "refusal": SECRET}]
    elif case == "mixed":
        raw.output = [_tool(), item]
    elif case == "no_final":
        raw.output = [{"type": "reasoning", "summary": SECRET, "content": SECRET}]
    elif case.startswith("tool_"):
        tool = _tool()
        raw.output = [tool]
        if case == "tool_name":
            tool.name = SECRET
        elif case == "tool_id":
            tool.call_id = ""
        elif case == "tool_duplicate":
            raw.output = [tool, _tool()]
        elif case == "tool_limit":
            tool.arguments = SECRET * 1_000
        elif case == "tool_contract":
            tool.call_id = " call_private "
        else:
            tool.arguments = {
                "tool_json": SECRET, "tool_json_scalar": json.dumps(SECRET),
                "tool_json_duplicate": '{"x":1,"x":2}',
                "tool_json_nonfinite": '{"x":NaN}',
            }[case]
    elif case == "final_json":
        item.content = [{"type": "output_text", "text": SECRET}]
    elif case == "final_json_duplicate":
        item.content = [{"type": "output_text", "text": '{"x":1,"x":2}'}]
    elif case == "schema_mismatch":
        item.content = [{"type": "output_text", "text": json.dumps({"verdict": SECRET})}]
    elif case == "schema_configuration":
        req = request(response_schema=StructuredResponseSchema(
            name="Synthetic", schema={"type": "string", "format": SECRET},
        ))
    elif case == "internal_contract":
        item.content = [{"type": "output_text", "text": "\ud800"}]
    elif case == "output_limit":
        item.content = [{"type": "output_text", "text": SECRET * 2_000}]
    elif case == "usage_missing":
        raw.usage = None
    elif case == "usage_invalid":
        raw.usage = {"input_tokens": SECRET, "output_tokens": 1, "total_tokens": 1}
    else:
        raise AssertionError("Unknown synthetic case")
    return raw, req


def _mapping(value: Any) -> Any:
    if isinstance(value, SimpleNamespace):
        return {key: _mapping(child) for key, child in vars(value).items()}
    if isinstance(value, list):
        return [_mapping(child) for child in value]
    return value


@pytest.mark.parametrize("mapping", [False, True], ids=["sdk-shape", "mapping"])
@pytest.mark.parametrize("case,site,reason,status,text", CASES, ids=[case[0] for case in CASES])
def test_synthetic_evidence_matches_pre_diagnostics_contract(
    case: str, site: str, reason: RejectedOutputReason, status: RejectedOutputStatus,
    text: str | None, mapping: bool,
) -> None:
    raw, req = _payload(case)
    with pytest.raises(ResponsesContractError) as caught:
        parse_response(_mapping(raw) if mapping else raw, req)
    evidence = caught.value.evidence
    assert evidence is not None
    usage_status: RejectedUsageStatus = (
        "missing" if case == "usage_missing" else "invalid" if case == "usage_invalid" else "known"
    )
    expected = RejectedOutputEvidence(
        final_text=text, usage=TokenUsage(12, 117, 129) if usage_status == "known" else None,
        usage_status=usage_status, status=status, reason=reason,
    )
    assert evidence == expected
    assert asdict(evidence) == asdict(expected)
    assert set(asdict(evidence)) == {
        "final_text", "usage", "usage_status", "status", "reason", "text_sha256",
    }
    assert evidence.correction_eligible == (case in {
        "final_json", "final_json_duplicate", "schema_mismatch",
    })
    assert str(caught.value) == "Responses output rejected"


@pytest.mark.parametrize("mapping", [False, True], ids=["sdk-shape", "mapping"])
@pytest.mark.parametrize("case,site,reason,status,text", CASES, ids=[case[0] for case in CASES])
def test_reject_warning_distinguishes_shapes_without_logging_provider_data(
    case: str, site: str, reason: str, status: str, text: str | None, mapping: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw, req = _payload(case)
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(ResponsesContractError) as caught,
    ):
        parse_response(_mapping(raw) if mapping else raw, req)
    records = [record for record in caplog.records if record.name == LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"Responses output rejected reject_site={site}"
    assert logging.Formatter("%(message)s").format(record) == record.getMessage()
    assert caught.value.reject_site.value == site
    assert record.exc_info is None and record.stack_info is None
    assert SECRET not in caplog.text + repr(record.__dict__)
    assert "call_private" not in caplog.text + repr(record.__dict__)


class _ForeignSite(Enum):
    GENERIC = "generic"


class _StringSite(str):
    def __str__(self) -> str:
        return SECRET


@pytest.mark.parametrize("site", [
    SECRET, "generic", "top_status\n" + SECRET, _StringSite("generic"),
    _ForeignSite.GENERIC, None, {"code": SECRET},
])
def test_contract_error_rejects_non_internal_codes_without_echoing_them(
    site: object, caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ValueError) as caught:
        ResponsesContractError("Safe message", reject_site=site)  # type: ignore[arg-type]
    assert SECRET not in str(caught.value) + caplog.text


def test_default_site_and_success_do_not_log_payload(caplog: pytest.LogCaptureFixture) -> None:
    assert ResponsesContractError("Safe message").reject_site.value == "generic"
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = parse_response(response(), _request())
        tool_result = parse_response(response(output=[_tool()]), _request())
    assert result.text == VALID_TEXT
    assert len(tool_result.tool_calls) == 1
    assert not caplog.records
