import json
from uuid import UUID

import pytest

from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_tool_envelope_requires_idempotency_and_redacts_sensitive_arguments() -> None:
    with pytest.raises(ValueError, match="sensitive"):
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="main_agent",
            tool_name="workspace_read",
            arguments={"api_key": "sk-secret"},
            approval_required=False,
            sandbox="read_only",
            idempotency_key="tool:abc",
        )


def test_tool_request_derives_stable_call_id_from_replay_inputs() -> None:
    first = HarnessToolCallRequest(
        run_id=RUN_ID,
        actor="main_agent",
        tool_name="workspace_read",
        arguments={"path": "README.md"},
        approval_required=True,
        sandbox="read_only",
        idempotency_key="tool:abc",
    )
    second = HarnessToolCallRequest(
        run_id=RUN_ID,
        actor="main_agent",
        tool_name="workspace_read",
        arguments={"path": "README.md"},
        approval_required=True,
        sandbox="read_only",
        idempotency_key="tool:abc",
    )

    assert first.call_id == second.call_id
    assert first.to_payload()["approval_required"] is True
    assert first.to_payload()["sandbox"] == "read_only"


def test_tool_result_truncates_large_payloads_for_replay() -> None:
    result = HarnessToolCallResult(
        call_id="call-1",
        tool_name="workspace_read",
        status="succeeded",
        payload={"text": "x" * 70_000},
    )

    assert result.truncated is True
    assert len(str(result.payload["text"]).encode("utf-8")) <= 65_536
    assert result.to_payload()["truncated"] is True


def test_tool_result_replaces_oversized_aggregate_payload_for_replay() -> None:
    result = HarnessToolCallResult(
        call_id="call-1",
        tool_name="workspace_read",
        status="succeeded",
        payload={f"field_{index}": "x" * 2048 for index in range(80)},
    )

    serialized = json.dumps(result.to_payload()["payload"], separators=(",", ":")).encode()
    assert result.truncated is True
    assert len(serialized) <= 65_536
    assert result.payload["_truncated"] is True


def test_tool_result_rejects_failed_status_without_reason() -> None:
    with pytest.raises(ValueError, match="failure reason"):
        HarnessToolCallResult(
            call_id="call-1",
            tool_name="workspace_read",
            status="failed",
            payload={},
        )
