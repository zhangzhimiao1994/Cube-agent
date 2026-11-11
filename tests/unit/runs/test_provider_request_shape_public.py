"""Only canonical, fully validated provider request shapes bypass text redaction."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.models.provider_diagnostics import RequestShape
from agent_hub.runs.repository import RunRepository, _public_event_payload, _sanitize_public_json
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
from tests.unit.runtime.test_provider_failure_diagnostic import FINITE, REASON
from tests.unit.runtime.test_provider_failure_diagnostic import event as failure_event


def source(parameter: str = "max_completion_tokens") -> tuple[RunEvent, dict[str, object]]:
    shape = RequestShape("chat_completions", 1, False, (1, 0, 1, 0, 0), 2, "named", True,
                         False, False, 0, parameter, 321).to_payload()
    event = failure_event({**runtime_failure_diagnostic_from_reason(REASON), **FINITE, "provider_request_shape": shape})
    return event, shape


def diagnostic(public: dict[str, object]) -> dict[str, object]:
    value = public["payload"]
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("parameter", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("token_family", [False, True])
def test_legal_shape_survives_json_database_and_public_reader_exactly(parameter: str, token_family: bool) -> None:
    event, shape = source(parameter)
    if token_family:
        event = failure_event({**event.payload, "provider_rejection_class": "unsupported_parameter",
                               "provider_parameter_family": parameter})
    row = json.loads(event.model_dump_json())
    public = _public_event_payload(row)
    assert diagnostic(public)["provider_request_shape"] == shape
    assert diagnostic(public)["provider_parameter_family"] == event.payload["provider_parameter_family"]
    assert RunEvent.from_payload(public).payload == event.payload
    assert _public_event_payload(json.loads(json.dumps(public))) == public
    redacted = _sanitize_public_json(shape)
    assert isinstance(redacted, dict) and redacted["output_token_parameter"] == "[redacted]"


@pytest.mark.parametrize("mutation", [
    "extra", "secret", "arbitrary_parameter", "bool_count", "core_missing", "protocol",
    "status", "retryable", "source", "kind", "null", "missing_event_identity",
])
def test_invalid_or_misbound_shape_is_omitted_never_returned_raw(mutation: str) -> None:
    event, _ = source()
    row = json.loads(event.model_dump_json())
    payload = row["payload"]
    shape = payload["provider_request_shape"]
    if mutation in {"extra", "secret"}:
        shape["uncontrolled" if mutation == "extra" else "api_key"] = "OPAQUE_PRIVATE_VALUE"
    elif mutation == "arbitrary_parameter":
        shape["output_token_parameter"] = "OPAQUE_PRIVATE_VALUE"
    elif mutation == "bool_count":
        shape["tool_count"] = True
    elif mutation == "core_missing":
        del payload["provider_body_state"]
    elif mutation == "protocol":
        payload["provider_protocol"] = "responses"
    elif mutation == "status":
        payload["status_code"] = 401
    elif mutation == "retryable":
        payload["retryable"] = True
    elif mutation == "source":
        payload["provider_classification_source"] = "status_only"
    elif mutation == "kind":
        row["kind"] = EventKind.RUNTIME_COMPLETED.value
    elif mutation == "null":
        payload["provider_request_shape"] = None
    else:
        del row["run_id"]
    public = _public_event_payload(row)
    assert "provider_request_shape" not in diagnostic(public)
    assert "OPAQUE_PRIVATE_VALUE" not in json.dumps(public)


async def test_actual_repository_events_preserves_canonical_shape_and_db_timestamp() -> None:
    event, shape = source()
    row = SimpleNamespace(payload=json.loads(event.model_dump_json()), created_at=datetime.now(UTC))
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = SimpleNamespace(actor_id=uuid4())
    session.scalars.return_value = MagicMock(all=lambda: [row])
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], factory))
    public = (await repository.events(uuid4(), event.run_id))[0]
    assert diagnostic(public)["provider_request_shape"] == shape
    assert public["created_at"] == row.created_at
    assert diagnostic(_public_event_payload(public))["provider_request_shape"] == shape
