"""Fresh-service configured dispatch recovery over private PostgreSQL artifacts."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CursorResult, MetaData, Table, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.db.models import RunEventRow, RunRow
from agent_hub.db.session import Database, build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.capacity import CapacityLease
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.defaults import ConfigBackedDispatchRuntime
from agent_hub.runtime.registry import RuntimeRegistry

COMPOSE_DATABASE_URL = (
    "postgresql+asyncpg://agent_hub_test:agent_hub_test@127.0.0.1:{port}/agent_hub_test"
)
HISTORY_MARKER = "history-input-must-not-hydrate-checkpoint"
HISTORY_SUMMARY_LINE = f"第 1 轮 history：{HISTORY_MARKER}"
GITHUB_ACTIONS_DURABLE_RECOVERY_SKIP = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("AGENT_HUB_DURABLE_RECOVERY_GATE") != "true",
    reason=(
        "GitHub Actions full-suite scheduling makes this Crew checkpoint-boundary "
        "contract order-sensitive; the focused durable recovery gate runs it."
    ),
)


def database_url() -> str:
    """Manual-import helper for running this contract without root conftest."""
    return os.environ.get(
        "AGENT_HUB_TEST_DATABASE_URL",
        COMPOSE_DATABASE_URL.format(
            port=os.environ.get("AGENT_HUB_TEST_POSTGRES_PORT", "54329")
        ),
    )


@pytest.fixture
async def database(database_url: str) -> AsyncIterator[Database]:
    instance = build_database(database_url)
    assert instance.engine.dialect.name == "postgresql", "These tests require PostgreSQL"
    try:
        yield instance
    finally:
        await instance.dispose()


class FixedConfigService:
    def __init__(self, *, tenant_id: UUID) -> None:
        self._tenant_id = tenant_id

    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        assert tenant_id == self._tenant_id
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=1,
            status=ConfigStatus.PUBLISHED,
            document=_platform_config_document(),
            created_by=uuid4(),
            created_at=datetime.now(UTC),
        )


class StaticSecretService:
    def __init__(self, *, tenant_id: UUID) -> None:
        self._tenant_id = tenant_id

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == self._tenant_id
        assert reference == "secret://dispatch-durable-test"
        return "sk-test-dispatch-durable"

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == self._tenant_id
        assert reference == "secret://dispatch-durable-test"
        return "d" * 64


class ImmediateCapacity:
    def __init__(self, deployments: tuple[Deployment, ...]) -> None:
        self.deployments = deployments

    async def initialize(self) -> None:
        return None

    def validate_configuration(self, deployments: Sequence[Deployment]) -> None:
        assert tuple(deployments) == self.deployments

    async def acquire(
        self,
        candidates: Sequence[Deployment],
        wait_timeout: float,
        *,
        estimated_tokens: int,
    ) -> CapacityLease:
        del wait_timeout
        assert estimated_tokens > 0
        candidate = candidates[0]
        return CapacityLease(
            id=str(uuid4()),
            deployment_id=candidate.id,
            quota_scope_id=candidate.quota_scope_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            renew_after_seconds=30,
        )

    async def renew(self, lease: CapacityLease) -> CapacityLease | None:
        return lease

    async def release(self, lease: CapacityLease) -> bool:
        del lease
        return True

    async def record_outcome(
        self,
        quota_scope_id: str,
        *,
        status_code: int | None,
        latency_seconds: float,
        succeeded: bool,
    ) -> None:
        del quota_scope_id, status_code, latency_seconds, succeeded


class ScriptedTransport:
    def __init__(
        self,
        *,
        label: str,
        block_final_requests: bool = False,
    ) -> None:
        self.label = label
        self.block_final_requests = block_final_requests
        self.requests: list[ModelRequest] = []
        self.completed_requests: list[ModelRequest] = []

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        assert api_key == "sk-test-dispatch-durable"
        self.requests.append(request)
        if self.block_final_requests and _is_final_request(request):
            await asyncio.Event().wait()
        evidence = [f"deployment:{deployment.id}"]
        if _is_worker_request(request):
            assert HISTORY_SUMMARY_LINE in _request_text(request)
            evidence.append(HISTORY_MARKER)
        text = _structured_answer(
            status="done",
            summary=f"{self.label}:{request.logical_model}",
            evidence=evidence,
        )
        self.completed_requests.append(request)
        return ModelResponse(
            text=text,
            usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class QueueStub:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        del run_id, idempotency_key


class CheckpointBoundaryRuntime(ConfigBackedDispatchRuntime):
    def __init__(self, *, checkpoint_committed: asyncio.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._checkpoint_committed = checkpoint_committed

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        stream = cast(AsyncGenerator[RunEvent, None], super().run(context))
        async with aclosing(stream):
            async for event in stream:
                yield event
                if event.checkpoint is None:
                    continue
                models = event.checkpoint.state.get("models")
                if not isinstance(models, Mapping) or not any(
                    isinstance(state, Mapping) and state.get("status") == "succeeded"
                    for state in models.values()
                ):
                    continue
                # RunService commits the yielded event before requesting the next one.
                self._checkpoint_committed.set()
                await asyncio.Event().wait()


@dataclass(frozen=True)
class PartialRun:
    tenant_id: UUID
    run_id: UUID
    history_run_id: UUID
    checkpoint: RuntimeCheckpoint
    history_artifact_id: str
    event_kinds_before_recovery: tuple[str, ...]
    first_transport: ScriptedTransport


def _platform_config_document() -> dict[str, object]:
    deployment = {
        "provider": "openai",
        "model": "gpt-5-test",
        "api_base": "https://api.openai.test/v1",
        "credential_ref": "secret://dispatch-durable-test",
        "quota_scope_id": "dispatch_durable",
        "max_concurrency": 10,
        "target_utilization": 0.8,
        "reserved_slots": 0,
        "capabilities": ["text", "structured_output"],
    }
    return {
        "models": {
            "worker_model": {"deployments": [deployment]},
            "final_model": {"deployments": [deployment]},
        },
        "agents": [
            {
                "id": "durable_worker",
                "role": "Durable Worker",
                "prompt": "Return the durable worker result.",
                "model": "worker_model",
                "skills": [],
            }
        ],
    }


def _structured_answer(*, status: str, summary: str, evidence: list[str]) -> str:
    return json.dumps(
        {
            "status": status,
            "summary": summary,
            "evidence": evidence,
            "risks": [],
            "artifacts": [],
            "verification": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _request_text(request: ModelRequest) -> str:
    return "\n".join(str(message.content) for message in request.messages)


def _is_worker_request(request: ModelRequest) -> bool:
    return "Role mission: Return the durable worker result." in _request_text(request)


def _is_final_request(request: ModelRequest) -> bool:
    return "Synthesize all role outputs into the final answer" in _request_text(request)


def _private_repository(database: Database) -> ArtifactRepository:
    module = import_module("agent_hub.runs.artifacts")
    constructor = cast(
        Callable[[async_sessionmaker[AsyncSession]], ArtifactRepository],
        module.PostgresArtifactRepository,
    )
    return constructor(database.session_factory)


def _runtime_registry(
    database: Database,
    transport: ScriptedTransport,
    *,
    tenant_id: UUID,
    checkpoint_committed: asyncio.Event | None = None,
) -> RuntimeRegistry:
    async def capacity_factory(
        requested_tenant_id: UUID,
        deployments: tuple[Deployment, ...],
    ) -> ImmediateCapacity:
        assert requested_tenant_id == tenant_id
        return ImmediateCapacity(deployments)

    constructor = (
        ConfigBackedDispatchRuntime if checkpoint_committed is None else CheckpointBoundaryRuntime
    )
    boundary = {} if checkpoint_committed is None else {"checkpoint_committed": checkpoint_committed}
    runtime = cast(Any, constructor)(
        config_service=FixedConfigService(tenant_id=tenant_id),
        secret_service=StaticSecretService(tenant_id=tenant_id),
        capacity_factory=capacity_factory,
        transport=transport,
        artifact_repository=_private_repository(database),
        **boundary,
    )
    return RuntimeRegistry((runtime,))


def _service(
    database: Database,
    transport: ScriptedTransport,
    *,
    tenant_id: UUID,
    worker_id: str,
    repository: RunRepository | None = None,
    checkpoint_committed: asyncio.Event | None = None,
) -> RunService:
    return RunService(
        repository or RunRepository(database.session_factory),
        runtime_registry=_runtime_registry(
            database, transport, tenant_id=tenant_id, checkpoint_committed=checkpoint_committed
        ),
        router=None,
        task_queue=QueueStub(),
        worker_id=worker_id,
        run_worker_lease_seconds=60,
    )


async def _seed_history_artifact(
    repository: RunRepository,
    *,
    tenant_id: UUID,
    conversation_id: str,
) -> tuple[UUID, str]:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="history",
        content={"text": HISTORY_MARKER},
    )
    record = await repository.create_run(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        request="previous completed run",
        mode=TaskMode.DISPATCH,
        status=RunStatus.COMPLETED,
        idempotency_key=None,
        routing_decision={"conversation_id": conversation_id},
        enqueue=False,
    )
    async with await repository.run_transaction() as session, session.begin():
        await repository.persist_event(
            session,
            tenant_id=tenant_id,
            run_id=record.id,
            event=RunEvent(
                kind=EventKind.ARTIFACT_CREATED,
                sequence=1,
                run_id=record.id,
                artifact=artifact,
            ),
        )
    return record.id, str(artifact.id)


async def _expire_worker_lease(database: Database, run_id: UUID) -> None:
    repository = RunRepository(database.session_factory)
    async with await repository.run_transaction() as session, session.begin():
        row = await repository.get_for_update(session, run_id)
        row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def _prepare_partial_run(
    database: Database, *, tenant_id: UUID, stop_after_step_completed: bool = False
) -> PartialRun:
    repository = RunRepository(database.session_factory)
    conversation_id = f"dispatch-durable-{uuid4().hex}"
    history_run_id, history_artifact_id = await _seed_history_artifact(
        repository, tenant_id=tenant_id, conversation_id=conversation_id
    )
    first_transport = ScriptedTransport(
        label="first",
        block_final_requests=True,
    )
    checkpoint_committed = None if stop_after_step_completed else asyncio.Event()
    service = _service(
        database,
        first_transport,
        tenant_id=tenant_id,
        worker_id="worker-first",
        checkpoint_committed=checkpoint_committed,
    )
    submitted = await service.submit(
        tenant_id=tenant_id,
        actor_id=uuid4(),
        message="Run the durable dispatch recovery contract.",
        mode=TaskMode.DISPATCH,
        agent_ids=("durable_worker",),
        conversation_id=conversation_id,
        direct_model="final_model",
        idempotency_key=f"durable-{uuid4().hex}",
    )

    if checkpoint_committed is None:
        partial = await service.execute(
            submitted.id, crash_after_event_kind=EventKind.STEP_COMPLETED
        )
        assert partial.status is RunStatus.RUNNING
    else:
        execution = asyncio.create_task(service.execute(submitted.id))
        try:
            await asyncio.wait_for(checkpoint_committed.wait(), timeout=60)
        finally:
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        async with database.session_factory() as session:
            row = await session.get(RunRow, submitted.id)
            assert row is not None and row.status == RunStatus.RUNNING.value
    await _expire_worker_lease(database, submitted.id)

    checkpoint = await _latest_checkpoint(database, tenant_id=tenant_id, run_id=submitted.id)
    assert checkpoint is not None
    assert checkpoint.state["terminal"] is False
    assert checkpoint.state["phase"] == "running"
    _assert_worker_model_succeeded_with_known_usage(checkpoint)
    assert HISTORY_SUMMARY_LINE in _request_text(first_transport.requests[0])
    input_artifacts = await _checkpoint_input_artifacts(database, checkpoint)
    assert len(input_artifacts) == 1
    input_artifact = input_artifacts[0]
    assert str(input_artifact.id) != history_artifact_id
    assert input_artifact.type == "text"
    assert input_artifact.producer == "conversation_history"
    assert HISTORY_SUMMARY_LINE in str(input_artifact.content["text"])
    return PartialRun(
        tenant_id=tenant_id,
        run_id=submitted.id,
        history_run_id=history_run_id,
        checkpoint=checkpoint,
        history_artifact_id=history_artifact_id,
        event_kinds_before_recovery=tuple(await _event_kinds(database, tenant_id, submitted.id)),
        first_transport=first_transport,
    )


async def _latest_checkpoint(
    database: Database,
    *,
    tenant_id: UUID,
    run_id: UUID,
) -> RuntimeCheckpoint | None:
    repository = RunRepository(database.session_factory)
    async with database.session_factory() as session:
        return await repository.latest_checkpoint(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
        )


async def _private_artifact_table(database: Database) -> Table:
    metadata = MetaData()

    async with database.engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: metadata.reflect(bind=sync_connection))
    table = metadata.tables.get("agent_hub_runtime_artifacts")
    assert table is not None, "private runtime artifact table is required"
    return table


def _checkpoint_artifact_ids(checkpoint: RuntimeCheckpoint) -> tuple[UUID, ...]:
    raw = checkpoint.state["artifact_registry"]
    assert isinstance(raw, Mapping)
    return tuple(UUID(str(item)) for item in raw)


def _assert_worker_model_succeeded_with_known_usage(checkpoint: RuntimeCheckpoint) -> None:
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage == {"tokens": 18, "cost_usd": "0"}
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    worker_states = [
        state
        for state in models.values()
        if isinstance(state, Mapping)
        and state.get("actor") == "durable_worker"
        and state.get("step_id") == "durable_worker_step"
    ]
    assert len(worker_states) == 1
    assert worker_states[0]["status"] == "succeeded"
    assert UUID(str(worker_states[0]["artifact_id"]))
    assert isinstance(worker_states[0]["sha256"], str)


def _checkpoint_input_references(checkpoint: RuntimeCheckpoint) -> tuple[Mapping[str, str], ...]:
    raw = checkpoint.state["input_refs"]
    assert isinstance(raw, tuple)
    return cast(tuple[Mapping[str, str], ...], raw)


async def _checkpoint_input_artifacts(
    database: Database,
    checkpoint: RuntimeCheckpoint,
) -> tuple[Artifact, ...]:
    module = import_module("agent_hub.runtime.artifacts")
    reference_constructor = module.ArtifactReference
    references = tuple(
        reference_constructor(id=UUID(reference["id"]), sha256=reference["sha256"])
        for reference in _checkpoint_input_references(checkpoint)
    )
    hydrated = await _private_repository(database).get_many(
        checkpoint.tenant_id,
        checkpoint.run_id,
        references,
    )
    return hydrated


def _private_payload_text_column(table: Table) -> str:
    for name in ("payload_text", "canonical_payload", "payload"):
        column = table.c.get(name)
        if column is not None and str(column.type).casefold().startswith("text"):
            return name
    raise AssertionError("private runtime artifact table must expose canonical text payload")


async def _delete_checkpoint_private_artifacts(
    database: Database,
    checkpoint: RuntimeCheckpoint,
) -> None:
    table = await _private_artifact_table(database)
    artifact_ids = _checkpoint_artifact_ids(checkpoint)
    id_column = table.c.get("artifact_id")
    if id_column is None:
        id_column = table.c.get("id")
    assert id_column is not None
    async with database.session_factory() as session, session.begin():
        await session.execute(
            table.delete().where(
                table.c.tenant_id == checkpoint.tenant_id,
                table.c.run_id == checkpoint.run_id,
                id_column.in_(artifact_ids),
            )
        )


async def _corrupt_one_checkpoint_private_artifact(
    database: Database,
    checkpoint: RuntimeCheckpoint,
) -> None:
    table = await _private_artifact_table(database)
    artifact_id = _checkpoint_artifact_ids(checkpoint)[0]
    id_column = table.c.get("artifact_id")
    if id_column is None:
        id_column = table.c.get("id")
    assert id_column is not None
    payload_column = _private_payload_text_column(table)
    async with database.session_factory() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(table)
                .where(
                    table.c.tenant_id == checkpoint.tenant_id,
                    table.c.run_id == checkpoint.run_id,
                    id_column == artifact_id,
                )
                .values({payload_column: '{"tampered":true}'}),
            ),
        )
        assert result.rowcount == 1


async def _event_kinds(database: Database, tenant_id: UUID, run_id: UUID) -> list[str]:
    service = _service(
        database,
        ScriptedTransport(label="reader"),
        tenant_id=tenant_id,
        worker_id="reader",
    )
    return [str(event["kind"]) for event in await service.events(tenant_id, run_id)]


async def _run_diagnostics(
    database: Database,
    *,
    tenant_id: UUID,
    run_id: UUID,
) -> dict[str, object]:
    repository = RunRepository(database.session_factory)
    public_events = await _service(
        database,
        ScriptedTransport(label="diagnostics"),
        tenant_id=tenant_id,
        worker_id="diagnostics",
    ).events(tenant_id, run_id)
    async with database.session_factory() as session:
        run_row = await session.scalar(
            select(RunRow).where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)
        )
        event_rows = (
            await session.execute(
                select(RunEventRow.sequence, RunEventRow.kind, RunEventRow.payload)
                .where(RunEventRow.tenant_id == tenant_id, RunEventRow.run_id == run_id)
                .order_by(RunEventRow.sequence)
            )
        ).all()
        checkpoint = await repository.latest_checkpoint(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
        )
    return {
        "public_events": tuple(
            {
                "kind": event.get("kind"),
                "sequence": event.get("sequence"),
                "reason": event.get("reason"),
                "payload": event.get("payload"),
                "checkpoint_summary": event.get("checkpoint_summary"),
            }
            for event in public_events
        ),
        "run_row": _run_row_diagnostic_summary(run_row),
        "raw_event_rows": _raw_event_row_diagnostics(event_rows),
        "latest_checkpoint": None
        if checkpoint is None
        else _checkpoint_diagnostic_summary(checkpoint),
    }


def _run_row_diagnostic_summary(row: RunRow | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "status": row.status,
        "version": row.version,
        "worker_id": row.worker_id,
        "worker_lease_expires_at": row.worker_lease_expires_at,
        "routing_decision": row.routing_decision,
    }


def _raw_event_row_diagnostics(rows: Sequence[object]) -> tuple[dict[str, object], ...]:
    diagnostics: list[dict[str, object]] = []
    for row in rows:
        sequence, kind, payload = cast(tuple[int, str, object], row)
        payload_summary = _payload_summary(payload)
        try:
            event = RunEvent.from_payload(payload)
        except Exception as error:  # noqa: BLE001 - diagnostics must expose parser failure type
            diagnostics.append(
                {
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload_summary,
                    "parse_error_type": type(error).__name__,
                    "parse_error": str(error),
                }
            )
            continue
        if _is_diagnostic_event_kind(event.kind):
            diagnostics.append(
                {
                    "sequence": sequence,
                    "kind": str(event.kind),
                    "reason": event.reason,
                    "payload": dict(event.payload),
                }
            )
    return tuple(diagnostics)


def _payload_summary(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        return {"type": type(payload).__name__}
    return {
        "keys": tuple(sorted(str(key) for key in payload)),
        "kind": payload.get("kind"),
        "reason": payload.get("reason"),
        "payload": payload.get("payload"),
        "checkpoint_state": _checkpoint_state_summary(payload.get("checkpoint")),
    }


def _checkpoint_state_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    state = value.get("state")
    if not isinstance(state, Mapping):
        return None
    return {
        "phase": state.get("phase"),
        "terminal": state.get("terminal"),
        "completed": state.get("completed"),
        "usage": state.get("usage"),
        "input_refs": state.get("input_refs"),
        "review_refs": state.get("review_refs"),
    }


def _is_diagnostic_event_kind(kind: EventKind | str) -> bool:
    if kind in {EventKind.RUNTIME_FAILED, EventKind.STEP_FAILED}:
        return True
    return type(kind) is str and ("fail" in kind or "repair" in kind)


def _checkpoint_diagnostic_summary(checkpoint: RuntimeCheckpoint) -> dict[str, object]:
    models = checkpoint.state.get("models")
    return {
        "id": str(checkpoint.id),
        "state_sha256": checkpoint.state_sha256,
        "phase": checkpoint.state.get("phase"),
        "terminal": checkpoint.state.get("terminal"),
        "completed": checkpoint.state.get("completed"),
        "usage": checkpoint.state.get("usage"),
        "input_refs": checkpoint.state.get("input_refs"),
        "artifact_registry_keys": tuple(
            sorted(cast(Mapping[str, object], checkpoint.state.get("artifact_registry", {})))
        ),
        "models": {
            str(key): {
                "actor": value.get("actor"),
                "status": value.get("status"),
                "artifact_id": value.get("artifact_id"),
                "sha256": value.get("sha256"),
            }
            for key, value in cast(Mapping[str, Mapping[str, object]], models).items()
        }
        if isinstance(models, Mapping)
        else models,
        "review_refs": checkpoint.state.get("review_refs"),
    }


async def _assert_completed_with_diagnostics(
    database: Database,
    recovered_status: RunStatus,
    *,
    tenant_id: UUID,
    run_id: UUID,
) -> None:
    if recovered_status is RunStatus.COMPLETED:
        return
    diagnostics = await _run_diagnostics(database, tenant_id=tenant_id, run_id=run_id)
    raise AssertionError(
        "fresh recovery did not complete: "
        f"status={recovered_status.value} diagnostics={diagnostics!r}"
    )


async def _delete_seeded_runs(database: Database, partial: PartialRun) -> None:
    repository = RunRepository(database.session_factory)
    service = _service(
        database,
        ScriptedTransport(label="cleanup"),
        tenant_id=partial.tenant_id,
        worker_id="cleanup",
    )
    try:
        await service.cancel(partial.tenant_id, partial.run_id)
    except (RunConflict, RunNotFound):
        pass
    for run_id in (partial.run_id, partial.history_run_id):
        try:
            await repository.delete_run(partial.tenant_id, run_id)
        except RunNotFound:
            pass


@GITHUB_ACTIONS_DURABLE_RECOVERY_SKIP
async def test_fresh_service_configured_runtime_recovers_partial_checkpoint_without_repeating_calls(
    database: Database,
) -> None:
    partial = await _prepare_partial_run(database, tenant_id=uuid4())
    try:
        assert sum(_is_worker_request(request) for request in partial.first_transport.requests) == 1
        assert (
            sum(_is_worker_request(request) for request in partial.first_transport.completed_requests)
            == 1
        )
        assert sum(_is_final_request(request) for request in partial.first_transport.requests) <= 1
        assert not any(
            _is_final_request(request) for request in partial.first_transport.completed_requests
        )
        before_events = partial.event_kinds_before_recovery
        original_input_refs = _checkpoint_input_references(partial.checkpoint)
        original_input_artifacts = await _checkpoint_input_artifacts(database, partial.checkpoint)
        assert len(original_input_artifacts) == 1
        original_input_artifact = original_input_artifacts[0]
        original_input_text = str(original_input_artifact.content["text"])

        fresh_database = build_database(database_url())
        fresh_transport = ScriptedTransport(label="fresh")
        try:
            recovered = await _service(
                fresh_database,
                fresh_transport,
                tenant_id=partial.tenant_id,
                worker_id="worker-fresh",
            ).recover(partial.run_id)
            events = await _event_kinds(fresh_database, partial.tenant_id, partial.run_id)
            await _assert_completed_with_diagnostics(
                fresh_database,
                recovered.status,
                tenant_id=partial.tenant_id,
                run_id=partial.run_id,
            )
            fresh_checkpoint = await _latest_checkpoint(
                fresh_database,
                tenant_id=partial.tenant_id,
                run_id=partial.run_id,
            )
            assert fresh_checkpoint is not None
            fresh_input_artifacts = await _checkpoint_input_artifacts(
                fresh_database,
                fresh_checkpoint,
            )
        finally:
            await fresh_database.dispose()

        assert sum(_is_worker_request(request) for request in fresh_transport.requests) == 0
        fresh_final_requests = [
            request for request in fresh_transport.completed_requests if _is_final_request(request)
        ]
        assert fresh_final_requests
        assert any(
            HISTORY_MARKER in _request_text(request)
            and "first:worker_model" in _request_text(request)
            for request in fresh_final_requests
        )
        assert events.count("artifact.created") > before_events.count("artifact.created")
        assert events.count("runtime.completed") == before_events.count("runtime.completed") + 1
        assert _checkpoint_input_references(fresh_checkpoint) == original_input_refs
        assert len(fresh_input_artifacts) == 1
        assert fresh_input_artifacts[0].to_payload() == original_input_artifact.to_payload()
        assert str(fresh_input_artifacts[0].id) == original_input_refs[0]["id"]
        assert str(fresh_input_artifacts[0].content["text"]) == original_input_text
        assert HISTORY_SUMMARY_LINE in original_input_text
    finally:
        await _delete_seeded_runs(database, partial)


@GITHUB_ACTIONS_DURABLE_RECOVERY_SKIP
async def test_fresh_service_configured_runtime_fails_closed_when_private_artifact_is_missing(
    database: Database,
) -> None:
    partial = await _prepare_partial_run(database, tenant_id=uuid4())
    try:
        before_events = partial.event_kinds_before_recovery
        await _delete_checkpoint_private_artifacts(database, partial.checkpoint)
        fresh_database = build_database(database_url())
        fresh_transport = ScriptedTransport(label="missing")
        try:
            recovered = await _service(
                fresh_database,
                fresh_transport,
                tenant_id=partial.tenant_id,
                worker_id="worker-missing",
            ).recover(partial.run_id)
            events = await _event_kinds(fresh_database, partial.tenant_id, partial.run_id)
        finally:
            await fresh_database.dispose()

        assert recovered.status is RunStatus.FAILED
        assert fresh_transport.requests == []
        assert events.count("runtime.completed") == before_events.count("runtime.completed")
        assert events.count("artifact.created") == before_events.count("artifact.created")
    finally:
        await _delete_seeded_runs(database, partial)


@GITHUB_ACTIONS_DURABLE_RECOVERY_SKIP
async def test_fresh_service_configured_runtime_fails_closed_when_private_artifact_is_corrupt(
    database: Database,
) -> None:
    partial = await _prepare_partial_run(database, tenant_id=uuid4())
    try:
        before_events = partial.event_kinds_before_recovery
        await _corrupt_one_checkpoint_private_artifact(database, partial.checkpoint)
        fresh_database = build_database(database_url())
        fresh_transport = ScriptedTransport(label="corrupt")
        try:
            recovered = await _service(
                fresh_database,
                fresh_transport,
                tenant_id=partial.tenant_id,
                worker_id="worker-corrupt",
            ).recover(partial.run_id)
            events = await _event_kinds(fresh_database, partial.tenant_id, partial.run_id)
        finally:
            await fresh_database.dispose()

        assert recovered.status is RunStatus.FAILED
        assert fresh_transport.requests == []
        assert events.count("runtime.completed") == before_events.count("runtime.completed")
        assert events.count("artifact.created") == before_events.count("artifact.created")
    finally:
        await _delete_seeded_runs(database, partial)


@GITHUB_ACTIONS_DURABLE_RECOVERY_SKIP
async def test_fresh_service_configured_runtime_blocks_uncheckpointed_side_effects(
    database: Database,
) -> None:
    partial = await _prepare_partial_run(
        database, tenant_id=uuid4(), stop_after_step_completed=True
    )
    try:
        fresh_transport = ScriptedTransport(label="uncertain")
        fresh_database = build_database(database_url())
        try:
            recovered = await _service(
                fresh_database,
                fresh_transport,
                tenant_id=partial.tenant_id,
                worker_id="worker-uncertain",
            ).recover(partial.run_id)
            repository = RunRepository(fresh_database.session_factory)
            events = await repository.events(partial.tenant_id, partial.run_id)
            raw_events = await repository.raw_events(partial.tenant_id, partial.run_id)
        finally:
            await fresh_database.dispose()
        assert recovered.status is RunStatus.FAILED
        assert fresh_transport.requests == []
        assert any(
            event["kind"] == EventKind.RUNTIME_FAILED.value
            and event.get("reason")
            == "runtime recovery blocked: non-replayable event after checkpoint"
            for event in events
        )
        assert any(
            event.kind is EventKind.RUNTIME_FAILED
            and event.payload.get("error_category") == "non_replayable_event_after_checkpoint"
            for event in raw_events
        )
        classifications = [event for event in raw_events if event.kind == "repair.classified"]
        assert classifications
        assert all(
            event.payload.get("failure_category") != "missing_failure_event"
            for event in classifications
        )
    finally:
        await _delete_seeded_runs(database, partial)
