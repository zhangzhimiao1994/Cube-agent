"""Pure admission and request contracts for one explicitly enabled correction."""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, replace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayRejectedOutput
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    RejectedOutputEvidence,
    StructuredResponseSchema,
    TokenUsage,
    ToolDefinition,
)
from agent_hub.runtime.autogen.adapter import _DiscussionDurability
from agent_hub.runtime.autogen.model_rejection import RejectedModelRecord, rejection_record
from agent_hub.runtime.autogen.model_tool_correction import (
    TOOL_CORRECTION_POLICY,
    eligible_tool_rejection,
    tool_correction_request,
    validate_rejected_model_rows,
)
from agent_hub.runtime.contracts import JsonValue

RUN = UUID("00000000-0000-0000-0000-000000000001")
TENANT = UUID("00000000-0000-0000-0000-000000000002")
ARTIFACT = "00000000-0000-0000-0000-000000000003"
SECRET = "REJECTED_BODY_MUST_NEVER_ENTER_CORRECTIVE_MESSAGES"


def request() -> ModelRequest:
    return ModelRequest(
        logical_model="shared",
        messages=(ModelMessage(role="system", content="Build within the tool grant."),
                  ModelMessage(role="user", content="Inspect the workspace.")),
        required_capabilities=frozenset({ModelCapability.TOOL_CALLING,
                                         ModelCapability.STRUCTURED_OUTPUT}),
        timeout_seconds=20.0, allow_fallback=False, max_output_tokens=128,
        response_schema=None,
        tools=(ToolDefinition(name="workspace_list", description="List workspace",
                              parameters={"type": "object", "additionalProperties": False}),),
        required_tool="workspace_list", native_tool_continuation=False,
    )


def record(*, position: int = 0, digest: str | None = None) -> RejectedModelRecord:
    original = request()
    return rejection_record(
        GatewayRejectedOutput(
            evidence=RejectedOutputEvidence(final_text=SECRET, usage=TokenUsage(7, 3, 10),
                usage_status="known", status="completed", reason="invalid_tool"),
            logical_model="shared", deployment_id="shared", provider_id="test",
            provider_model="test/model", cost_usd=None,
        ),
        run_id=RUN, tenant_id=TENANT, position=position, request=original,
        request_hash=digest or _DiscussionDurability.request_hash(original),
    )


def rejected_row(*, position: int = 0, digest: str | None = None) -> dict[str, Any]:
    receipt = record(position=position, digest=digest)
    return {"status": "rejected", "request_hash": receipt.request_hash,
            "required_tool": "workspace_list", "max_output_tokens": 128,
            "timeout_seconds": 20.0, "rejection": receipt.to_payload()}


def pair(status: str = "succeeded") -> list[dict[str, Any]]:
    second: dict[str, Any] = {
        "status": status, "request_hash": "b" * 64, "required_tool": "workspace_list",
        "max_output_tokens": 64, "timeout_seconds": 10.0, "correction_for": 0,
    }
    if status == "succeeded":
        second.update(artifact_id=ARTIFACT, artifact_sha256="c" * 64)
    elif status == "rejected":
        second["rejection"] = record(position=1, digest="b" * 64).to_payload()
    return [rejected_row(), second]


def validate(rows: list[dict[str, Any]], *, enabled: bool = True) -> tuple[RejectedModelRecord, ...]:
    return validate_rejected_model_rows(
        cast(tuple[Mapping[str, JsonValue], ...], tuple(rows)),
        run_id=RUN, tenant_id=TENANT, repair_enabled=enabled,
    )


def test_corrective_request_preserves_grant_and_changes_actual_durable_hash() -> None:
    original, receipt = request(), record()
    corrected = tool_correction_request(original, receipt, max_output_tokens=64, timeout_seconds=10.0)
    assert TOOL_CORRECTION_POLICY == "engineering_tool_correction_version"
    assert corrected.messages[:-1] == original.messages
    assert corrected.messages[-1].role == "user"
    assert corrected.messages[-1].content
    assert original.required_tool is not None
    assert original.required_tool in str(corrected.messages[-1].content)
    assert "native function" in str(corrected.messages[-1].content)
    assert SECRET not in str(corrected.messages)
    assert corrected.max_output_tokens == 64 and corrected.timeout_seconds == 10.0
    for item in fields(ModelRequest):
        if item.name not in {"messages", "max_output_tokens", "timeout_seconds"}:
            assert getattr(corrected, item.name) == getattr(original, item.name)
    assert corrected.tools is original.tools and corrected.response_schema is original.response_schema
    assert _DiscussionDurability.request_hash(corrected) != receipt.request_hash
    assert original == request()
    other = receipt.model_copy(update={"cost_usd": "0.1", "request_hash": "a" * 64})
    assert tool_correction_request(original, other, max_output_tokens=64,
                                   timeout_seconds=10.0).messages[-1] == corrected.messages[-1]


def test_equal_caps_still_change_content_and_hash() -> None:
    original = request()
    corrected = tool_correction_request(original, record(), max_output_tokens=128, timeout_seconds=20)
    assert corrected != original
    assert _DiscussionDurability.request_hash(corrected) != _DiscussionDurability.request_hash(original)


@pytest.mark.parametrize("native,structured", [(True, False), (False, True), (True, True)])
def test_correction_rejects_unproven_native_or_structured_response_protocol(
    native: bool, structured: bool,
) -> None:
    original = replace(
        request(), native_tool_continuation=native,
        response_schema=(StructuredResponseSchema(name="result", schema={"type": "object"})
                         if structured else None),
    )
    receipt = record().model_copy(update={
        "request_hash": _DiscussionDurability.request_hash(original),
    })
    with pytest.raises(ValueError):
        tool_correction_request(original, receipt, max_output_tokens=64, timeout_seconds=10)
    assert original.messages == request().messages
    assert original.native_tool_continuation is native
    assert (original.response_schema is not None) is structured


@pytest.mark.parametrize("changes", [
    {"evidence_present": False, "reason": None, "output_status": "unknown",
     "usage_status": "missing", "usage": None},
    {"output_status": "unknown"}, {"output_status": "refused"},
    {"output_status": "incomplete"}, {"reason": "invalid_json"},
    {"usage_status": "missing", "usage": None},
    {"usage_status": "invalid", "usage": None}, {"negotiation_exhausted": True},
])
def test_ineligible_output_cannot_authorize_request(changes: dict[str, Any]) -> None:
    receipt = RejectedModelRecord.model_validate({**record().to_payload(), **changes})
    assert not eligible_tool_rejection(receipt)
    with pytest.raises(ValueError):
        tool_correction_request(request(), receipt, max_output_tokens=64, timeout_seconds=10)


def test_nullable_cost_remains_null_and_does_not_disqualify_known_usage() -> None:
    receipt = record()
    assert eligible_tool_rejection(receipt)
    assert receipt.to_payload()["cost_usd"] is None
    assert validate(pair())[0].cost_usd is None
    assert receipt.usage is not None and receipt.usage.total_tokens == 10


@pytest.mark.parametrize("fault", ["no_required_tool", "no_tools", "extra_tool", "other_model"])
def test_request_cannot_change_tool_or_model_scope(fault: str) -> None:
    original = request()
    if fault == "no_required_tool":
        original = replace(original, required_tool=None)
    elif fault == "no_tools":
        original = replace(original, tools=(), required_tool=None)
    elif fault == "extra_tool":
        original = replace(original, tools=(*original.tools, ToolDefinition(
            name="workspace_write", description="Write workspace", parameters={"type": "object"})))
    else:
        original = replace(original, logical_model="other")
    with pytest.raises(ValueError):
        tool_correction_request(original, record(), max_output_tokens=64, timeout_seconds=10)


@pytest.mark.parametrize("cap", [True, False, 0, -1, 129, 10**1000, 64.0, "64", None,
                                 float("nan"), float("inf")])
def test_request_output_cap_is_strict_and_cannot_grow(cap: Any) -> None:
    with pytest.raises(ValueError):
        tool_correction_request(request(), record(), max_output_tokens=cap, timeout_seconds=10)


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 20.1, 10**1000, "10", None,
                                     float("nan"), float("inf"), -float("inf")])
def test_request_timeout_is_finite_positive_and_cannot_grow(timeout: Any) -> None:
    with pytest.raises(ValueError):
        tool_correction_request(request(), record(), max_output_tokens=64, timeout_seconds=timeout)


@pytest.mark.parametrize("status", ["succeeded", "running", "rejected"])
def test_enabled_pair_retains_every_rejection_without_promoting_status(status: str) -> None:
    rows = pair(status)
    before = deepcopy(rows)
    receipts = validate(rows)
    assert rows == before
    assert tuple(item.position for item in receipts) == ((0, 1) if status == "rejected" else (0,))
    assert sum(item.usage.total_tokens for item in receipts if item.usage) == (
        20 if status == "rejected" else 10)
    assert rows[1]["status"] == status


def test_default_legacy_requires_rejection_last_and_does_not_authorize_pair() -> None:
    rows = pair()
    with pytest.raises(ValueError):
        validate_rejected_model_rows(rows, run_id=RUN, tenant_id=TENANT)
    assert validate([rows[0]], enabled=False) == (record(),)
    legacy = {"status": "rejected", "request_hash": record().request_hash,
              "rejection": record().to_payload(), "report_binding": {"legacy": True}}
    assert validate([legacy], enabled=False) == (record(),)


@pytest.mark.parametrize("enabled", [1, 0, "1", None])
def test_repair_switch_does_not_coerce_policy_versions(enabled: Any) -> None:
    with pytest.raises(ValueError):
        validate_rejected_model_rows(pair(), run_id=RUN, tenant_id=TENANT, repair_enabled=enabled)


@pytest.mark.parametrize("status", ["succeeded", "running", "rejected"])
@pytest.mark.parametrize("fault", [
    "bool_link", "wrong_link", "float_link", "missing_link", "same_hash", "bad_hash",
    "different_tool", "empty_tool", "more_tokens", "bool_tokens", "float_tokens", "zero_tokens",
    "more_time", "bool_time", "nan_time", "inf_time", "zero_time", "report_binding",
    "tool_field", "unknown_field", "missing_boundary", "unknown_status", "mixed_record",
])
def test_repair_pair_fails_closed_on_link_scope_schema_or_budget(status: str, fault: str) -> None:
    rows = pair(status)
    second = rows[1]
    changes: dict[str, dict[str, Any]] = {
        "bool_link": {"correction_for": False}, "wrong_link": {"correction_for": 1},
        "float_link": {"correction_for": 0.0}, "same_hash": {"request_hash": rows[0]["request_hash"]},
        "bad_hash": {"request_hash": "B" * 64}, "different_tool": {"required_tool": "workspace_write"},
        "empty_tool": {"required_tool": ""}, "more_tokens": {"max_output_tokens": 129},
        "bool_tokens": {"max_output_tokens": True}, "float_tokens": {"max_output_tokens": 64.0},
        "zero_tokens": {"max_output_tokens": 0}, "more_time": {"timeout_seconds": 21.0},
        "bool_time": {"timeout_seconds": True}, "nan_time": {"timeout_seconds": float("nan")},
        "inf_time": {"timeout_seconds": float("inf")}, "zero_time": {"timeout_seconds": 0},
        "report_binding": {"report_binding": {}}, "tool_field": {"tools": []},
        "unknown_field": {"unknown": True}, "unknown_status": {"status": "unknown"},
        "mixed_record": {"record": record().to_payload()},
    }
    if fault == "missing_link":
        second.pop("correction_for")
    elif fault == "missing_boundary":
        second.pop("max_output_tokens")
    else:
        second.update(changes[fault])
    with pytest.raises(ValueError):
        validate(rows)


@pytest.mark.parametrize("status", ["running", "rejected"])
@pytest.mark.parametrize("artifact_field", ["artifact_id", "artifact_sha256"])
def test_unaccepted_correction_cannot_carry_artifact(status: str, artifact_field: str) -> None:
    rows = pair(status)
    rows[1][artifact_field] = ARTIFACT if artifact_field == "artifact_id" else "c" * 64
    with pytest.raises(ValueError):
        validate(rows)


@pytest.mark.parametrize("changes", [
    {"artifact_id": None}, {"artifact_id": "bad"}, {"artifact_id": True},
    {"artifact_sha256": "bad"}, {"artifact_sha256": "C" * 64},
    {"rejection": record().to_payload()},
])
def test_succeeded_correction_requires_valid_artifact_and_no_rejection(changes: dict[str, Any]) -> None:
    rows = pair()
    rows[1].update(changes)
    with pytest.raises(ValueError):
        validate(rows)


@pytest.mark.parametrize("status", ["succeeded", "running", "rejected"])
def test_orphan_correction_is_never_admitted(status: str) -> None:
    with pytest.raises(ValueError):
        validate([pair(status)[1]])


@pytest.mark.parametrize("fault", [
    "run", "tenant", "position", "hash", "version", "version_bool", "usage_total",
    "usage_bool", "cost_nan", "extra_body", "missing_record", "non_mapping_record",
    "artifact", "report_binding", "bad_tool", "missing_cap", "bad_cap", "bad_time",
    "ineligible", "negotiated",
])
def test_original_receipt_and_boundary_are_validated_before_pair(fault: str) -> None:
    rows = pair()
    first, receipt = rows[0], rows[0]["rejection"]
    if fault in {"run", "tenant"}:
        receipt[fault + "_id"] = ARTIFACT
    elif fault == "position":
        receipt["position"] = 1
    elif fault == "hash":
        receipt["request_hash"] = "d" * 64
    elif fault.startswith("version"):
        receipt["schema_version"] = True if fault == "version_bool" else 2
    elif fault.startswith("usage"):
        receipt["usage"]["total_tokens" if fault == "usage_total" else "prompt_tokens"] = (
            11 if fault == "usage_total" else True)
    elif fault == "cost_nan":
        receipt["cost_usd"] = "NaN"
    elif fault == "extra_body":
        receipt["final_text"] = SECRET
    elif fault == "missing_record":
        first.pop("rejection")
    elif fault == "non_mapping_record":
        first["rejection"] = None
    elif fault == "artifact":
        first["artifact_id"] = ARTIFACT
    elif fault == "report_binding":
        first["report_binding"] = {}
    elif fault == "bad_tool":
        first["required_tool"] = "bad.tool"
        rows[1]["required_tool"] = "bad.tool"
    elif fault == "missing_cap":
        first.pop("max_output_tokens")
    elif fault == "bad_cap":
        first["max_output_tokens"] = True
    elif fault == "bad_time":
        first["timeout_seconds"] = float("inf")
    elif fault == "ineligible":
        receipt["output_status"] = "unknown"
    else:
        receipt["negotiation_exhausted"] = True
    with pytest.raises(ValueError):
        validate(rows)


def test_third_attempt_cannot_chain_from_second_rejection() -> None:
    rows = pair("rejected")
    third = dict(pair("running")[1], correction_for=1, request_hash="d" * 64)
    with pytest.raises(ValueError):
        validate([*rows, third])


@pytest.mark.parametrize("changes", [
    {"run_id": ARTIFACT}, {"tenant_id": ARTIFACT}, {"position": 0}, {"position": True},
    {"request_hash": "d" * 64}, {"schema_version": 2}, {"schema_version": True},
    {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 11}},
    {"cost_usd": "NaN"}, {"final_text": SECRET}, {"fallback_used": True},
])
def test_second_rejected_receipt_is_strictly_validated(changes: dict[str, Any]) -> None:
    rows = pair("rejected")
    rows[1]["rejection"].update(changes)
    with pytest.raises(ValueError):
        validate(rows)


@pytest.mark.parametrize("changes", [
    {"output_status": "unknown"}, {"reason": "invalid_json"},
    {"usage_status": "missing", "usage": None}, {"negotiation_exhausted": True},
])
def test_second_rejection_remains_retained_even_when_it_cannot_be_corrected(
    changes: dict[str, Any],
) -> None:
    rows = pair("rejected")
    rows[1]["rejection"].update(changes)
    receipts = validate(rows)
    assert len(receipts) == 2
    assert not eligible_tool_rejection(receipts[-1])


def test_succeeded_second_attempt_cannot_carry_another_correction() -> None:
    third = dict(pair("running")[1], correction_for=1, request_hash="d" * 64)
    with pytest.raises(ValueError):
        validate([*pair(), third])


def test_link_uses_actual_row_index_and_success_can_be_followed_by_normal_work() -> None:
    rows = pair()
    rows[0]["rejection"]["position"] = 1
    rows[1]["correction_for"] = 1
    normal = {"status": "succeeded", "request_hash": "d" * 64, "ordinary": True}
    receipts = validate([normal, *rows, {"status": "running", "request_hash": "e" * 64}])
    assert tuple(item.position for item in receipts) == (1,)


@pytest.mark.parametrize("enabled", [False, True])
def test_unrelated_rows_keep_existing_semantics(enabled: bool) -> None:
    assert validate([], enabled=enabled) == ()
    assert validate([{"status": "running", "ordinary": True}], enabled=enabled) == ()
    with pytest.raises(ValueError):
        validate([{"status": "succeeded", "rejection": record().to_payload()}], enabled=enabled)


def test_stopped_second_attempt_cannot_have_unlinked_followup() -> None:
    for status in ("running", "rejected"):
        with pytest.raises(ValueError):
            validate([*pair(status), {"status": "running", "request_hash": "d" * 64}])
