"""Validated failure details reach the ordinary owner's existing event projection."""

from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, Column, MetaData, String, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import RunArtifactRow, RunEventRow, RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.registry import RuntimeRegistry
from tests.api.test_runs_api import StubAuthService
from tests.unit.runs.checkpoint_project_failure_typed_fixture import model_table
from tests.unit.runtime.autogen.test_execution_diagnostic_continuation import runtime
from tests.unit.runtime.crew.test_execution_diagnostic_continuation import (
    Policy,
    ProcessBackend,
    ReceiptHarness,
    RepairModel,
    context,
    malformed_receipt,
)
from tests.unit.runtime.test_generated_draft_autogen import (
    DraftGateway,
    DraftModel,
)
from tests.unit.runtime.test_generated_draft_autogen import (
    context as draft_context,
)
from tests.unit.runtime.test_generated_draft_autogen import (
    runtime as draft_runtime,
)


class NoQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        pytest.fail("Reading failure details must not enqueue a run")


@pytest.mark.parametrize("failure", ["execution", "draft", "malformed"])
async def test_failed_tool_details_are_projected_only_after_validation(
    tmp_path: Path, failure: str,
) -> None:
    ctx = draft_context() if failure == "draft" else context(TaskMode.DISCUSS)
    assert ctx.actor_id is not None
    if failure == "draft":
        runner = draft_runtime(DraftModel(), DraftGateway(ctx))
    else:
        backend, model, policy = ProcessBackend(tmp_path), RepairModel(autogen=True), Policy(ctx)
        runner = runtime(model, backend, policy)
        if failure == "malformed":
            runner._tool_gateway = ReceiptHarness(
                backend, policy, lambda result: malformed_receipt(result, "shape"),
            )
    events = [event async for event in runner.run(ctx)]
    failed = next(event for event in events if event.kind is EventKind.TOOL_FAILED)
    if failure != "draft":
        assert backend.results[0].exit_code == 3
        assert backend.results[0].stderr.strip() == "FIXTURE_FAILURE"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model_type in (RunRow, RunEventRow, RunArtifactRow):
        table = model_table(model_type)
        columns = [Column(
            column.name,
            JSON() if isinstance(column.type, JSONB) else String(32)
            if isinstance(column.type, PGUUID) else column.type,
            primary_key=column.primary_key, nullable=column.nullable,
            server_default=column.server_default,
        ) for column in table.columns]
        constraints = [UniqueConstraint(*(column.name for column in constraint.columns))
            for constraint in table.constraints if isinstance(constraint, UniqueConstraint)]
        Table(table.name, metadata, *columns, *constraints)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = RunRepository(factory)
        async with factory.begin() as session:
            session.add(RunRow(
                id=ctx.run_id, tenant_id=ctx.tenant_id, actor_id=ctx.actor_id,
                actor_role="operator", request=ctx.request, mode="discuss", status="running",
                routing_decision=dict(ctx.routing_decision), version=1,
            ))
        async with factory.begin() as session:
            for event in events:
                if event.kind in {EventKind.TOOL_FAILED, EventKind.TOOL_COMPLETED, EventKind.ARTIFACT_CREATED}:
                    await repository.persist_event(
                        session, tenant_id=ctx.tenant_id, run_id=ctx.run_id, event=event,
                    )
        service = RunService(repository, runtime_registry=RuntimeRegistry([runner]), router=None, task_queue=NoQueue())
        principal = AuthenticatedPrincipal(user_id=ctx.actor_id, tenant_id=ctx.tenant_id, role=Role.OPERATOR)
        app = create_app(auth_service=StubAuthService(principal), rate_limiter=object(), config_service=object(), run_service=service)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{ctx.run_id}/events", headers={"Authorization": "Bearer valid-token"})
        assert response.status_code == 200
        projected = response.json()["items"]
        artifacts = [item["artifact"] for item in projected if item.get("artifact") is not None]
        if failure == "malformed":
            assert artifacts == []
            assert "FIXTURE_FAILURE" not in response.text
        else:
            matching = [artifact for artifact in artifacts if artifact["id"] == failed.payload["artifact_id"]]
            assert len(matching) == 1, "Validated failed tool details disappeared from owner events"
            artifact = matching[0]
            assert artifact["type"] == "tool_result"
            if failure == "draft":
                rejection = artifact["content"]["result"]["generated_draft_rejection"]
                assert rejection["execution_started"] is False
                assert rejection["reason"] == "generated_acceptance_manifest_invalid"
            else:
                diagnostic = artifact["content"]["result"]["execution_diagnostic"]
                assert diagnostic["exit_code"] == 3
                assert "FIXTURE_FAILURE" in str(diagnostic)
            assert any(item["kind"] == "artifact.created" and item.get("artifact", {}).get("id") == artifact["id"] for item in projected)
    finally:
        await engine.dispose()
