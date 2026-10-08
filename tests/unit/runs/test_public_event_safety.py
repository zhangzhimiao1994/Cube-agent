import json
from collections.abc import Mapping
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.runs.repository import (
    RunRepository,
    _public_artifact_payload,
    _public_event_payload,
)
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason


def test_public_event_payload_redacts_sensitive_values_under_safe_keys() -> None:
    payload = _public_event_payload(
        {
            "safe_summary": "failed after Bearer sk-private-token reached provider",
            "nested": {
                "safe_hint": "Authorization: Bearer sk-private-token",
                "ok": "retry with model fallback",
            },
            "items": ["private-token value", {"note": "normal"}],
            "credential_ref": "secret://must-drop",
        }
    )

    assert payload == {
        "safe_summary": "[redacted]",
        "nested": {
            "safe_hint": "[redacted]",
            "ok": "retry with model fallback",
        },
        "items": ["[redacted]", {"note": "normal"}],
    }


def test_public_event_payload_does_not_redact_task_manager_as_sk_secret() -> None:
    text = (
        "### `README.md`\n\n```markdown\n"
        "# task-manager-direct-fixture\n\n"
        "A normal package name containing task-manager must remain visible.\n"
        "```\n"
        "### `src/main.py`\n\n```python\nprint('ready')\n```\n"
    )

    payload = _public_event_payload(
        {
            "kind": "artifact.created",
            "artifact": {
                "content": {"text": text},
            },
        }
    )

    artifact = payload["artifact"]
    assert isinstance(artifact, Mapping)
    content = artifact["content"]
    assert isinstance(content, Mapping)
    redacted = content["text"]
    assert isinstance(redacted, str)
    assert "README.md" in redacted
    assert "task-manager-direct-fixture" in redacted
    assert "src/main.py" in redacted
    assert "[redacted]" not in redacted


def test_public_artifact_payload_removes_generated_file_storage_key() -> None:
    payload = _public_artifact_payload(
        {
            "id": "artifact-1",
            "type": "tool_result",
            "producer": "document_writer",
            "content": {
                "result": {
                    "file": {
                        "filename": "delivery-plan.docx",
                        "download_url": "/api/v1/admin/runs/run-1/artifacts/artifact-1/download",
                    },
                    "metadata": {
                        "filename": "delivery-plan.docx",
                        "storage_key": "tenant/run/artifact/delivery-plan.docx",
                    },
                }
            },
        }
    )

    content = payload["content"]
    assert isinstance(content, Mapping)
    result = content["result"]
    assert isinstance(result, dict)
    metadata = result["metadata"]
    assert isinstance(metadata, dict)
    assert "storage_key" not in metadata


@pytest.mark.parametrize("kind", [EventKind.TOOL_FAILED, EventKind.STEP_FAILED])
@pytest.mark.parametrize(
    ("status", "failure_kind", "reason", "diagnostic_metadata"),
    [
        pytest.param(
            "rejected",
            "invalid_arguments",
            "workspace.read scoped-unavailable",
            {},
            id="rejected-read",
        ),
        pytest.param(
            "failed",
            "tool_error",
            "model gateway failed: model response failed (status=400)",
            {"status_code": 400},
            id="tool-error",
        ),
        pytest.param(
            "failed",
            "timeout",
            "CrewAI step timed out: step=inspect actor=architect",
            {"step_id": "inspect", "actor": "architect"},
            id="timeout",
        ),
        pytest.param(
            "rejected",
            "invalid_arguments",
            "workspace.read failed: Bearer sk-private-token",
            {},
            id="secret-reason",
        ),
    ],
)
async def test_persisted_tool_and_step_failure_diagnostics_survive_json_replay(
    kind: EventKind,
    status: str,
    failure_kind: str,
    reason: str,
    diagnostic_metadata: dict[str, object],
) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    original_payload = {"status": status, "failure_kind": failure_kind}
    if kind is EventKind.TOOL_FAILED:
        original = RunEvent(
            kind=kind,
            sequence=10,
            run_id=run_id,
            actor="architect",
            tool_call_id="read-1",
            tool_name="workspace.read",
            reason=reason,
            payload=original_payload,
        )
    else:
        original = RunEvent(
            kind=kind,
            sequence=10,
            run_id=run_id,
            actor="architect",
            step_id="inspect",
            reason=reason,
            payload=original_payload,
        )
    assert RunEvent.from_payload(original.to_payload()) == original
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = uuid4()
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock()))

    await repository.persist_event(session, tenant_id=tenant_id, run_id=run_id, event=original)

    assert session.scalar.await_args is not None
    statement = session.scalar.await_args.args[0]
    # Replay the actual enriched INSERT envelope after the JSON storage boundary.
    stored = json.loads(json.dumps(statement.compile().params["payload"]))
    expected_diagnostic = runtime_failure_diagnostic_from_reason(reason)
    diagnostic_keys = {
        "error_code",
        "error_stage",
        "error_category",
        "error_summary",
        "retryable",
        "suggested_action",
    }
    assert set(expected_diagnostic) == diagnostic_keys | set(diagnostic_metadata)
    assert set(stored["payload"]) == set(original_payload) | set(expected_diagnostic)
    assert stored["payload"] == original_payload | expected_diagnostic
    for key, value in (original_payload | diagnostic_metadata).items():
        assert stored["payload"][key] == value
    assert dict(original.payload) == original_payload
    assert stored["reason"] == reason
    assert "sk-private-token" not in json.dumps(stored["payload"])
    public = _public_event_payload(stored)
    assert "sk-private-token" not in json.dumps(public)
    if "sk-private-token" in reason:
        assert public["reason"] == "[redacted]"

    rebuilt = RunEvent.from_payload(stored)

    assert rebuilt.kind is kind
    assert rebuilt.run_id == run_id and rebuilt.sequence == 10
    assert rebuilt.reason == reason
    assert dict(rebuilt.payload) == stored["payload"]
    assert rebuilt.actor == "architect"
    assert rebuilt.step_id == original.step_id
    assert rebuilt.tool_call_id == original.tool_call_id
    assert rebuilt.tool_name == original.tool_name


def _bounded_tool_failure_diagnostic() -> dict[str, str | int | bool]:
    return {
        "error_code": "crew.step_timeout",
        "error_stage": "crew_step",
        "error_category": "step_timeout",
        "error_summary": "CrewAI step timed out",
        "retryable": True,
        "suggested_action": "Retry the affected step.",
    }


def _tool_failure_safety_event(
    payload: dict[str, str | int | bool],
    *,
    reason: str = "workspace.read scoped-unavailable",
) -> RunEvent:
    return RunEvent(
        kind=EventKind.TOOL_FAILED,
        sequence=10,
        run_id=uuid4(),
        actor="architect",
        tool_call_id="read-1",
        tool_name="workspace.read",
        reason=reason,
        payload=payload,
    )


@pytest.mark.parametrize("pre_enriched", [False, True], ids=["enrich", "already-enriched"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("retryable", 1, id="integer-retryable"),
        pytest.param("retryable", "false", id="string-retryable"),
        pytest.param("error_summary", "x" * 241, id="oversized-summary"),
        pytest.param("suggested_action", "x" * 1025, id="oversized-action"),
        pytest.param("status_code", True, id="boolean-status-code"),
        pytest.param("actor", "Architect", id="uppercase-actor"),
        pytest.param("uncontrolled_extension", "private", id="unknown-field"),
    ],
)
async def test_tool_failure_persistence_rejects_copied_invalid_diagnostic_before_insert(
    pre_enriched: bool,
    key: str,
    value: object,
) -> None:
    receipt: dict[str, str | int | bool] = {"status": "failed", "failure_kind": "tool_error"}
    payload = receipt | _bounded_tool_failure_diagnostic() if pre_enriched else receipt
    original = _tool_failure_safety_event(payload)
    assert RunEvent.from_payload(original.to_payload()) == original
    forged = original.model_copy(update={"payload": {**original.payload, key: value}})
    session = AsyncMock(spec=AsyncSession)
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock()))

    with pytest.raises(ValueError):
        await repository.persist_event(
            session, tenant_id=uuid4(), run_id=original.run_id, event=forged,
        )

    session.scalar.assert_not_awaited()
    assert dict(original.payload) == payload


@pytest.mark.parametrize(
    ("step", "actor", "optional_identifiers"),
    [
        pytest.param("Inspect", "architect", {"actor": "architect"}, id="uppercase-step"),
        pytest.param(".inspect", "architect", {"actor": "architect"}, id="dotleading-step"),
        pytest.param("inspect", "Architect", {"step_id": "inspect"}, id="uppercase-actor"),
        pytest.param("inspect", ".architect", {"step_id": "inspect"}, id="dotleading-actor"),
        pytest.param("Inspect", ".architect", {}, id="both-invalid"),
        pytest.param(
            "inspect", "architect", {"step_id": "inspect", "actor": "architect"},
            id="both-valid",
        ),
    ],
)
async def test_tool_timeout_persistence_omits_unsafe_optional_identifiers_and_replays(
    step: str,
    actor: str,
    optional_identifiers: dict[str, str],
) -> None:
    reason = f"CrewAI step timed out: step={step} actor={actor}"
    receipt: dict[str, str | int | bool] = {"status": "failed", "failure_kind": "timeout"}
    original = _tool_failure_safety_event(receipt, reason=reason)
    assert RunEvent.from_payload(original.to_payload()) == original
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = uuid4()
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock()))

    await repository.persist_event(
        session, tenant_id=uuid4(), run_id=original.run_id, event=original,
    )

    assert session.scalar.await_args is not None
    statement = session.scalar.await_args.args[0]
    stored = json.loads(json.dumps(statement.compile().params["payload"]))
    rebuilt = RunEvent.from_payload(stored)
    diagnostic = rebuilt.payload
    assert diagnostic["error_code"] == "crew.step_timeout"
    assert diagnostic["error_stage"] == "crew_step"
    assert diagnostic["error_category"] == "step_timeout"
    assert diagnostic["retryable"] is True
    assert {
        key: diagnostic[key] for key in ("step_id", "actor") if key in diagnostic
    } == optional_identifiers
    assert rebuilt.reason == reason
    assert rebuilt.actor == "architect"
    assert rebuilt.tool_call_id == original.tool_call_id
    assert dict(original.payload) == receipt


@pytest.mark.parametrize("field", ["error_summary", "suggested_action"])
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Cookie: sessionid=review-only", id="cookie-header"),
        pytest.param("cOoKiE: sid=review-only", id="mixed-case-cookie-header"),
        pytest.param(
            "Set-Cookie: session=review-only; HttpOnly; Secure", id="set-cookie-header",
        ),
        pytest.param("set-cookie: sid=review-only; Path=/", id="lowercase-set-cookie-header"),
        pytest.param("sessionid=review-only", id="sessionid-assignment"),
        pytest.param("session_id=review-only", id="session-id-assignment"),
    ],
)
def test_public_tool_failure_diagnostic_redacts_direct_cookie_credentials(
    field: str,
    text: str,
) -> None:
    diagnostic = _bounded_tool_failure_diagnostic()
    diagnostic[field] = text
    original = _tool_failure_safety_event(diagnostic)

    public = _public_event_payload(original.to_payload())

    projected = public["payload"]
    assert isinstance(projected, Mapping)
    assert projected[field] == "[redacted]"
    assert "review-only" not in json.dumps(public)
    assert public["reason"] == original.reason
    assert original.payload[field] == text


@pytest.mark.parametrize("field", ["error_summary", "suggested_action"])
@pytest.mark.parametrize(
    "text",
    [
        "The cookie parser rejected an invalid delimiter.",
        "Set-Cookie headers require a valid expiration date.",
        "Cookie support is required for this feature.",
        "Review the session cookie configuration before retrying.",
        "session = Session(engine)",
        "sid = 42",
    ],
)
def test_public_tool_failure_diagnostic_preserves_cookie_text_without_credentials(
    field: str,
    text: str,
) -> None:
    diagnostic = _bounded_tool_failure_diagnostic()
    diagnostic[field] = text
    original = _tool_failure_safety_event(diagnostic)

    public = _public_event_payload(original.to_payload())

    projected = public["payload"]
    assert isinstance(projected, Mapping)
    assert projected[field] == text
    assert dict(projected) == diagnostic
