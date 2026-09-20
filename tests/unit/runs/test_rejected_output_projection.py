"""Public checkpoint boundaries must not depend on future private field names."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from agent_hub.api.routers.admin import _admin_run_artifact, _admin_run_event
from agent_hub.api.routers.runs import RunServiceProtocol, run_events
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import (
    RunConflict,
    RunNotFound,
    RunRecord,
    RunRepository,
    _public_artifact_payload,
    _public_event_payload,
)
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.registry import RuntimeRegistry

Projection = Callable[[dict[str, object]], dict[str, object]]
MARKER = "CustomerNorthForecast17"
REJECTED_TEXT = '{"customer":"' + MARKER + '","amount":17'


def admin_event(event: dict[str, object]) -> dict[str, object]:
    return _admin_run_event(event).model_dump(mode="json")


def repository_then_admin_event(event: dict[str, object]) -> dict[str, object]:
    return admin_event(_public_event_payload(event))


def admin_artifact(artifact: dict[str, object]) -> dict[str, object]:
    return _admin_run_artifact(artifact).model_dump(mode="json")


@pytest.mark.parametrize(
    "project", [_public_event_payload, admin_event, repository_then_admin_event],
    ids=["repository", "admin", "repository-admin"],
)
@pytest.mark.parametrize("opaque", [
    REJECTED_TEXT,
    {"text": REJECTED_TEXT},
    [{"content": {"candidate": REJECTED_TEXT}}],
], ids=["string", "mapping", "nested-list"])
def test_private_checkpoint_content_never_reaches_public_events(
    project: Projection, opaque: object,
) -> None:
    # This is a serialization-boundary fixture, not a proposed Crew state schema.
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), runtime_type="crew", runtime_version="7", run_id=uuid4(),
        tenant_id=uuid4(), mode=TaskMode.DISPATCH,
        state={"extension": cast(JsonValue, opaque)},
    )
    event = RunEvent(
        kind=EventKind.CHECKPOINT_SAVED, sequence=7,
        run_id=checkpoint.run_id, checkpoint=checkpoint,
    )
    stored = event.to_payload()
    before = deepcopy(stored)

    public = project(stored)

    # A read projection must not corrupt the original evidence or resume digest.
    assert stored == before
    original_checkpoint = cast(dict[str, object], stored["checkpoint"])
    restored = RuntimeCheckpoint.from_payload(original_checkpoint)
    assert restored.state_sha256 == checkpoint.state_sha256
    assert restored.to_payload() == checkpoint.to_payload()
    assert MARKER in json.dumps(original_checkpoint, ensure_ascii=False)
    assert public["kind"] == "checkpoint.saved"
    assert public["sequence"] == 7
    assert MARKER not in json.dumps(public, ensure_ascii=False)


@pytest.mark.parametrize(
    "project", [_public_event_payload, admin_event, repository_then_admin_event],
    ids=["repository", "admin", "repository-admin"],
)
def test_checkpoint_privacy_does_not_hide_normal_message_text(project: Projection) -> None:
    public = project({
        "kind": "message.created", "sequence": 1,
        "message": MARKER, "payload": {"note": "normal metadata"},
    })
    assert public["message"] == MARKER
    assert public["payload"] == {"note": "normal metadata"}


@pytest.mark.parametrize(
    "project", [_public_artifact_payload, admin_artifact], ids=["repository", "admin"],
)
def test_checkpoint_privacy_does_not_hide_normal_deliverables(project: Projection) -> None:
    artifact: dict[str, object] = {
        "id": str(uuid4()), "type": "text", "producer": "writer",
        "content": {"text": MARKER},
    }
    before = deepcopy(artifact)
    public = project(artifact)
    assert artifact == before
    assert MARKER in json.dumps(public)


def checkpoint_event() -> RunEvent:
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), runtime_type="crew", runtime_version="7", run_id=uuid4(),
        tenant_id=uuid4(), mode=TaskMode.DISPATCH, state={"extension": REJECTED_TEXT},
    )
    return RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1,
                    run_id=checkpoint.run_id, checkpoint=checkpoint)


def test_public_checkpoint_is_an_idempotent_summary_not_a_recovery_payload() -> None:
    event = checkpoint_event()
    assert event.checkpoint is not None
    stored = event.to_payload()
    public = _public_event_payload(stored)
    assert public["checkpoint"] is None
    summary = cast(dict[str, object], public["checkpoint_summary"])
    assert summary == {key: value for key, value in event.checkpoint.to_payload().items()
                       if key in {"id", "runtime_type", "runtime_version", "mode", "state_sha256"}}
    assert _public_event_payload(public) == public
    with pytest.raises(ValueError):
        RuntimeCheckpoint.from_payload(summary)
    with pytest.raises(ValueError):
        RunEvent.from_payload(public)
    assert RunEvent.from_payload(stored).to_payload() == stored


def test_public_checkpoint_summary_drops_unvalidated_fields() -> None:
    public = _public_event_payload({
        "kind": "checkpoint.saved", "sequence": 1, "checkpoint": None,
        "checkpoint_summary": {
            "id": REJECTED_TEXT, "runtime_type": {"text": REJECTED_TEXT},
            "runtime_version": REJECTED_TEXT, "mode": REJECTED_TEXT,
            "state_sha256": REJECTED_TEXT, "state": {"text": REJECTED_TEXT},
            "body": REJECTED_TEXT,
        },
    })
    assert public["checkpoint"] is None
    assert public["checkpoint_summary"] == {}
    assert MARKER not in json.dumps(public)


@pytest.mark.parametrize("version", ["1", "7", "1.0", "1.2.3"])
@pytest.mark.parametrize("runtime_type", ["crew", "crew.dispatch"])
def test_summary_preserves_contract_valid_runtime_metadata(version: str, runtime_type: str) -> None:
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), runtime_type=runtime_type, runtime_version=version,
        run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH, state={},
    )
    event = RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1,
                     run_id=checkpoint.run_id, checkpoint=checkpoint)
    summary = _public_event_payload(event.to_payload())["checkpoint_summary"]
    assert isinstance(summary, dict)
    assert summary["runtime_type"] == runtime_type
    assert summary["runtime_version"] == version


def repository_for(event: RunEvent) -> tuple[RunRepository, Any, SimpleNamespace]:
    assert event.checkpoint is not None
    row = SimpleNamespace(
        tenant_id=event.checkpoint.tenant_id, run_id=event.run_id,
        payload=event.to_payload(), created_at=datetime.now(UTC),
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.scalar = AsyncMock(return_value=event.run_id)
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [row]))
    return RunRepository(cast(Any, lambda: session)), session, row


def assert_scoped_select(statement: Any, table: str, tenant: UUID, run: UUID) -> None:
    compiled = statement.compile(dialect=cast(Any, postgresql.dialect)())
    sql = str(compiled)
    run_column = "id" if table == "agent_hub_runs" else "run_id"
    assert f"{table}.tenant_id =" in sql
    assert f"{table}.{run_column} =" in sql
    assert set(compiled.params.values()) == {tenant, run}


async def test_raw_events_rebuild_original_checkpoints_using_tenant_and_run_filters() -> None:
    event = checkpoint_event()
    assert event.checkpoint is not None
    repository, session, row = repository_for(event)
    before = deepcopy(row.payload)
    events = await repository.raw_events(event.checkpoint.tenant_id, event.run_id)
    assert len(events) == 1
    assert events[0].to_payload() == before
    assert events[0].checkpoint is not None
    assert events[0].checkpoint.state_sha256 == event.checkpoint.state_sha256
    assert row.payload == before
    assert_scoped_select(session.scalar.call_args.args[0], "agent_hub_runs",
                         row.tenant_id, row.run_id)
    statement = session.scalars.call_args.args[0]
    assert_scoped_select(statement, "agent_hub_run_events", row.tenant_id, row.run_id)
    assert "ORDER BY agent_hub_run_events.sequence" in str(statement)
    public = await repository.events(row.tenant_id, row.run_id)
    assert public[0]["checkpoint"] is None
    assert MARKER not in json.dumps(public, default=str)
    assert row.payload == before


async def test_latest_checkpoint_keeps_original_body_and_resume_hash() -> None:
    event = checkpoint_event()
    assert event.checkpoint is not None
    repository, session, row = repository_for(event)
    original = event.checkpoint.to_payload()
    session.scalar.return_value = SimpleNamespace(payload=deepcopy(original))

    restored = await repository.latest_checkpoint(
        session, tenant_id=row.tenant_id, run_id=row.run_id,
    )

    assert restored is not None
    assert restored.to_payload() == original
    assert restored.state_sha256 == event.checkpoint.state_sha256
    assert MARKER in json.dumps(restored.to_payload())
    statement = session.scalar.call_args.args[0]
    sql = str(statement)
    assert "agent_hub_run_checkpoints.tenant_id =" in sql
    assert "agent_hub_run_checkpoints.run_id =" in sql
    assert "agent_hub_run_checkpoints.sequence DESC" in sql
    assert {row.tenant_id, row.run_id} <= set(statement.compile().params.values())


async def test_public_api_serializes_only_checkpoint_summary_via_real_service() -> None:
    event = checkpoint_event()
    repository, session, row = repository_for(event)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=row.tenant_id, role=Role.ADMIN)

    response = await run_events(
        event.run_id, cast(RunServiceProtocol, service_for(repository)), principal,
    )

    public = response.model_dump(mode="json")["items"][0]
    assert public["checkpoint"] is None
    assert public["checkpoint_summary"] == _public_event_payload(row.payload)["checkpoint_summary"]
    assert MARKER not in response.model_dump_json()
    assert MARKER in json.dumps(row.payload)
    assert_scoped_select(session.scalars.call_args.args[0], "agent_hub_run_events",
                         principal.tenant_id, event.run_id)


@pytest.mark.parametrize("wrong_scope", ["tenant", "run"])
async def test_raw_events_checks_run_ownership_before_loading_rows(wrong_scope: str) -> None:
    event = checkpoint_event()
    repository, session, row = repository_for(event)
    tenant = uuid4() if wrong_scope == "tenant" else row.tenant_id
    run = uuid4() if wrong_scope == "run" else row.run_id
    session.scalar.return_value = None
    with pytest.raises(RunNotFound):
        await repository.raw_events(tenant, run)
    assert_scoped_select(session.scalar.call_args.args[0], "agent_hub_runs", tenant, run)
    session.scalars.assert_not_awaited()


async def test_raw_events_rejects_extra_outer_tenant_even_if_it_matches_row() -> None:
    event = checkpoint_event()
    repository, _, row = repository_for(event)
    row.payload["tenant_id"] = str(row.tenant_id)
    before = deepcopy(row.payload)

    with pytest.raises(RunConflict):
        await repository.raw_events(row.tenant_id, row.run_id)
    assert row.payload == before


@pytest.mark.parametrize("outer_tenant", [str(uuid4()), None, REJECTED_TEXT, 17])
async def test_raw_events_rejects_wrong_or_invalid_outer_tenant(outer_tenant: object) -> None:
    repository, _, row = repository_for(checkpoint_event())
    row.payload["tenant_id"] = outer_tenant
    with pytest.raises(RunConflict) as caught:
        await repository.raw_events(row.tenant_id, row.run_id)
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize("corruption", ["row-tenant", "row-run", "payload-run", "checkpoint-tenant"])
async def test_raw_events_rejects_mismatched_row_or_payload_scope(corruption: str) -> None:
    event = checkpoint_event()
    assert event.checkpoint is not None
    repository, _, row = repository_for(event)
    tenant, run = row.tenant_id, row.run_id
    if corruption == "row-tenant":
        row.tenant_id = uuid4()
    elif corruption == "row-run":
        row.run_id = uuid4()
    elif corruption == "payload-run":
        row.payload["run_id"] = str(uuid4())
    else:
        row.payload["checkpoint"]["tenant_id"] = str(uuid4())
    with pytest.raises(RunConflict) as caught:
        await repository.raw_events(tenant, run)
    assert MARKER not in str(caught.value)


async def test_raw_events_rejects_corrupt_checkpoint_without_logging_body() -> None:
    event = checkpoint_event()
    repository, _, row = repository_for(event)
    row.payload["checkpoint"]["state_sha256"] = "0" * 64
    with pytest.raises(RunConflict) as caught:
        await repository.raw_events(row.tenant_id, row.run_id)
    assert MARKER not in str(caught.value)
    assert caught.value.__cause__ is None


def service_for(repository: object) -> RunService:
    return RunService(cast(RunRepository, repository), runtime_registry=MagicMock(spec=RuntimeRegistry),
                      router=None, task_queue=cast(Any, SimpleNamespace()))


@pytest.mark.parametrize("path", ["snapshot", "self-repair", "empty-closure"])
async def test_internal_failure_paths_use_raw_events_not_public_events(
    path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = checkpoint_event()
    assert event.checkpoint is not None
    repository = SimpleNamespace(
        raw_events=AsyncMock(return_value=(event,)),
        events=AsyncMock(side_effect=AssertionError("public events cannot reconstruct runtime state")),
    )
    service = service_for(repository)
    record = RunRecord(
        id=event.run_id, tenant_id=event.checkpoint.tenant_id, actor_id=None,
        request="normal request", mode=TaskMode.DISPATCH, status=RunStatus.FAILED,
        version=1, created_at=datetime.now(UTC), routing_decision={},
    )
    if path == "snapshot":
        assert await service._safe_load_run_events(
            tenant_id=record.tenant_id, run_id=record.id, fallback=(),
        ) == (event,)
    elif path == "self-repair":
        classify = MagicMock(return_value=None)
        monkeypatch.setattr("agent_hub.runs.service.classify_terminal_run", classify)
        await service._safe_record_self_repair_decision_for_record(record)
        assert classify.call_args.kwargs["events"] == (event,)
    else:
        close = AsyncMock()
        monkeypatch.setattr(service, "_safe_record_empty_response_closure_artifact", close)
        await service._safe_record_empty_response_closure_artifact_for_record(record)
        assert close.call_args.kwargs["events"] == (event,)
    repository.raw_events.assert_awaited_once_with(record.tenant_id, record.id)
    repository.events.assert_not_awaited()


@pytest.mark.parametrize("missing", [False, True])
async def test_raw_read_failure_does_not_fall_back_to_public_events(missing: bool) -> None:
    event = checkpoint_event()
    assert event.checkpoint is not None
    repository = SimpleNamespace(events=AsyncMock(return_value=(event.to_payload(),)))
    if not missing:
        repository.raw_events = AsyncMock(side_effect=RunNotFound("run was not found"))
    service = service_for(repository)
    assert await service._safe_load_run_events(
        tenant_id=event.checkpoint.tenant_id, run_id=event.run_id, fallback=(event,),
    ) == (event,)
    repository.events.assert_not_awaited()
