"""A2 selectors: real PG/service/DISCUSS/Root, deterministic SDK HTTP only.

Uses an already migrated, explicitly bound dedicated PG and the existing owned
Redis fixture when native selectors are deliberately executed. Offline selectors
start neither; they are not PG/native acceptance. No SSH/migrations/real provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import shutil
import sys
from collections.abc import AsyncGenerator, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, Unpack
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.capabilities.approvals import PluginApprovalIdentity
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.base import Base
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunOutboxRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RuntimeArtifactRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.run_admission_repository import (
    RunAdmissionRepository,
    _WorkerLease,
)
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.self_repair import project_repair_checkpoint_is_safe
from agent_hub.runs.toolchain_supply import ToolchainSupplyAuthority
from agent_hub.runtime import review_assembly
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen import project_repair_phase
from agent_hub.runtime.autogen.adapter import DiscussionParticipant
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
    _mutable_json,
)
from agent_hub.runtime.review_assembly import ReviewAssemblyUnavailable
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414 - explicit dedicated PG fixture
)
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import storage as storage  # noqa: PLC0414
from tests.integration.runtime._model_review_repair_fixture import (
    ORIGINAL,
    PATCHED,
    TARGET,
    AcceptedCase,
    RedisCards,
    RepairComponents,
    RepairWire,
    capacity_clean,
    components,
    pg_case,
    projected_worker,
    projection_case,
)
from tests.integration.runtime._model_review_repair_typed_fixture import (
    RepairDB,
    claim,
    pool_values,
    productive_checkpoint,
    repair_fixture,
    service_runtime_context,
)
from tests.integration.runtime.test_model_review_modes import (
    own_redis as own_redis,  # noqa: PLC0414
)
from tests.unit.harness.review_typed_fixture import StreamRuntimeStub, present
from tests.unit.runs.project_auto_repair_typed_fixture import (
    UnexpectedTaskQueue,
    integer,
    json_map,
    ledger,
    mapping,
    mutable,
    mutable_json,
    sequence,
    string,
)
from tests.unit.runtime.test_review_assembly import evidence_document
from tests.unit.runtime.test_review_assembly import fixture as offline_assembly


class BeginApprovalArguments(TypedDict):
    approval_id: str
    approval_fingerprint: str
    approval_scope: NotRequired[str | None]
    actor_id: NotRequired[UUID | None]
    plugin_identity: NotRequired[PluginApprovalIdentity | None]
    toolchain_authority: NotRequired[ToolchainSupplyAuthority | None]
    go_recipe_authority: NotRequired[ToolchainSupplyAuthority | None]
    expected_go_recipe: NotRequired[Mapping[str, object] | None]
    approval_request: NotRequired[CapabilityRequest | None]


class PoolMetadata(Protocol):
    @property
    def original_token_grant(self) -> int: ...
    @property
    def original_deadline(self) -> datetime: ...


@dataclass
class MetadataPool:
    original_token_grant: int
    original_deadline: datetime


class WorkerArguments(TypedDict, total=False):
    worker_id: str
    run_repository: RunRepository
    review_broker: review_assembly.ReviewAssemblyBroker


class ParticipantArguments(TypedDict):
    id: str
    role: str
    goal: str
    logical_model: str
    allowed_tools: tuple[str, ...]


async def latest_local_checkpoint(
    repository: RunRepository, db: RepairDB, tenant: UUID, run: UUID
) -> RuntimeCheckpoint | None:
    async with db() as session:
        return await repository.latest_checkpoint(session, tenant_id=tenant, run_id=run)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Override integration's auto-migration for this module only.

    The PG selectors use storage's explicit identity/URL guards and require the
    parent's existing schema; offline execution must not connect to any database.
    """


async def rows[T: Base](
    factory: async_sessionmaker[AsyncSession], model: type[T], run: UUID
) -> list[T]:
    async with factory() as session:
        return list(
            (await session.scalars(select(model).where(model.__table__.c.run_id == run))).all()
        )


def _metadata_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            mutable_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _recovery_restore_metadata(
    saved: RuntimeCheckpoint | None, loaded: RuntimeCheckpoint | None, phase: object, scope: object
) -> dict[str, object]:
    assert saved is not None and loaded is not None and isinstance(phase, Mapping)
    assert (saved.id, saved.run_id, saved.tenant_id, saved.mode) == (
        loaded.id,
        loaded.run_id,
        loaded.tenant_id,
        loaded.mode,
    )
    assert saved.state_sha256 == saved.recompute_state_sha256()
    assert loaded.state_sha256 == loaded.recompute_state_sha256() == saved.state_sha256
    assert phase["source_checkpoint_id"] == str(loaded.id)
    assert phase["source_checkpoint_sha256"] == loaded.state_sha256
    assert phase["source_scope_sha256"] == scope
    return {
        "saved_checkpoint_sha256": saved.state_sha256,
        "loaded_checkpoint_sha256": loaded.state_sha256,
        "prepared_phase_sha256": _metadata_hash(phase),
        "phase_source_checkpoint_sha256": phase["source_checkpoint_sha256"],
        "source_scope_sha256": mapping(phase)["source_scope_sha256"],
    }


def _recovery_pool_metadata(pool: PoolMetadata, lease: _WorkerLease) -> dict[str, object]:
    return {
        "token_grant": pool.original_token_grant,
        "deadline_sha256": _metadata_hash(pool.original_deadline.astimezone(UTC).isoformat()),
        "lease_sha256": _metadata_hash({"worker_id": lease.worker_id, "token": str(lease.token)}),
    }


async def _record_recovery_metadata(
    f: AcceptedCase,
    repair: RepairComponents,
    request: pytest.FixtureRequest,
    record_property: Callable[[str, object], None],
    *,
    retry_calls: tuple[int | None, int | None] = (None, None),
) -> None:
    """Record actual asserted objects only; native PASS/cleanup belongs to collector."""
    contexts = f.wire.contexts
    assert len(contexts) == 2 and all(context.run_id == f.record.id for context in contexts)
    assert contexts[0].execution_token != contexts[1].execution_token
    after = present(await RunAdmissionRepository(f.factory).get_pool(f.user.tenant_id, f.record.id))
    assert (
        (after.tenant_id, after.run_id, after.authorization_turn_id)
        == (f.before.tenant_id, f.before.run_id, f.before.authorization_turn_id)
        == (f.user.tenant_id, f.record.id, f.evidence.turn_id)
    )
    assert (after.original_token_grant, after.original_deadline) == (
        f.before.original_token_grant,
        f.before.original_deadline,
    )
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    unknown = next(item for item in attempts if item.attempt_id == f.unknown[0])
    assert (unknown.state, unknown.token_hold, unknown.receipt_sha256) == (
        f.unknown_state_before,
        *f.unknown[1:],
    )
    assert {item.attempt_id for item in attempts} == set(f.wire.sent)
    leases = []
    for context in contexts:
        sent = [
            value
            for value in f.wire.sent.values()
            if value["lease"].token == context.execution_token
        ]
        assert sent and all(value["lease"] == sent[0]["lease"] for value in sent)
        lease = sent[0]["lease"]
        for item in attempts:
            observed = f.wire.sent[item.attempt_id]
            assert (item.worker_id, item.worker_lease_token) == (
                observed["lease"].worker_id,
                observed["lease"].token,
            )
            assert item.request_sha256 == observed["request_sha256"]
            assert item.physical_transport_id == observed["physical_id"]
        leases.append(lease)
    assert after.token_charged == sum(
        item.token_hold
        if item.state == "unknown"
        else integer(mapping(mapping(item.receipt)["usage"])["total_tokens"])
        for item in attempts
    )
    intents = await rows(f.factory, RunReviewIntentRow, f.record.id)
    assert len(intents) == 1 and len(f.wire.reviews) == 1
    intent = intents[0]
    reviews = [item for item in attempts if item.purpose == "review"]
    assert len(reviews) == 1 and reviews[0].deployment["deployment_id"] == "reviewer_1"
    assert all(
        item.deployment != reviews[0].deployment for item in attempts if item.purpose == "primary"
    )
    before_sha = f.root_before_sha
    after_sha = hashlib.sha256((f.root / TARGET).read_bytes()).hexdigest()
    if intent.status == "allow":
        assert intent.consumed and len(repair.root_effects) == 1 and before_sha != after_sha
        assert retry_calls == (None, None)
    else:
        assert intent.status == "unknown" and not intent.consumed and not repair.root_effects
        assert before_sha == after_sha
        assert retry_calls[0] is not None and retry_calls[0] == retry_calls[1]
    checkpoint = repair.runtime.phase_checkpoint
    assert checkpoint is not None
    # Canonical SOURCE scope, not the current waiting-approval DTO with its new
    # approval ID/fingerprint/locator. The phase was prepared inside the real
    # worker proof and its exact checkpoint must have been committed below.
    scope_after = mapping(checkpoint.state[project_repair_phase.PHASE_KEY])["source_scope_sha256"]
    assert scope_after == f.scope_before
    async with f.factory() as session:
        durable = await f.repository.latest_checkpoint(
            session, tenant_id=f.user.tenant_id, run_id=f.record.id
        )
    assert durable is not None and durable.state_sha256 == checkpoint.state_sha256
    restore = _recovery_restore_metadata(
        f.saved_checkpoint,
        repair.runtime.loaded_checkpoint,
        checkpoint.state[project_repair_phase.PHASE_KEY],
        f.scope_before,
    )
    assert review_assembly.runtime_source_fingerprint() == f.source_sha
    snapshots = [
        _recovery_pool_metadata(pool, lease)
        for pool, lease in zip((f.before, after), leases, strict=True)
    ]
    record_property(
        "review_recovery_observed",
        json.dumps(
            {
                "phase": "actual_runtime_restore"
                if intent.status == "allow"
                else "unknown_review_no_replay",
                "mode": contexts[1].mode.value,
                "selector": request.node.nodeid,
                "runtime_source_sha256": f.source_sha,
                "run_id": str(f.record.id),
                "pool_id": str(after.run_id),
                "authorization_turn_id": str(after.authorization_turn_id),
                "scope_sha256_before": f.scope_before,
                "scope_sha256_after": scope_after,
                "original": snapshots[0],
                "restored": snapshots[1],
                "restore": restore,
                "unknown_hold": {
                    "attempt_id": str(unknown.attempt_id),
                    "state_before": f.unknown_state_before,
                    "state_after": unknown.state,
                    "token_hold_before": f.unknown[1],
                    "token_hold_after": unknown.token_hold,
                    "receipt_sha256_before": f.unknown[2],
                    "receipt_sha256_after": unknown.receipt_sha256,
                },
                "review": {
                    "attempts": len(reviews),
                    "calls": len(f.wire.reviews),
                    "intent_status": intent.status,
                    "consumed_intents": sum(int(item.consumed) for item in intents),
                    "action_digest": intent.action_digest,
                    "receipt_sha256": intent.receipt_sha256,
                },
                "root": {
                    "before_sha256": before_sha,
                    "after_sha256": after_sha,
                    "effects": len(repair.root_effects),
                },
                "retry": {"calls_before": retry_calls[0], "calls_after": retry_calls[1]},
            },
            sort_keys=True,
        ),
    )


@pytest.mark.parametrize(
    "defect", [None, "checkpoint", "checkpoint_identity", "phase_source", "scope", "missing_phase"]
)
async def test_recovery_metadata_restore_binds_actual_checkpoint_and_prepared_phase_without_pg(
    tmp_path: Path, defect: str | None
) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    prepared: dict[str, JsonValue] | None = project_repair_phase.prepare(
        context, f.cp, ledger(f.cp.state["model_ledger"]), ledger(f.cp.state["tool_ledger"])
    )
    loaded = f.cp
    if defect == "checkpoint":
        loaded = f.cp.model_copy(update={"state_sha256": "f" * 64})
    elif defect == "checkpoint_identity":
        loaded = f.cp.model_copy(update={"run_id": uuid4()})
    elif defect == "phase_source":
        present(prepared)["source_checkpoint_sha256"] = "f" * 64
    elif defect == "scope":
        present(prepared)["source_scope_sha256"] = "f" * 64
    elif defect == "missing_phase":
        prepared = None
    scope = mapping(mapping(context.routing_decision)["repair_proposal"])["automatic_scope_sha256"]
    if defect is not None:
        with pytest.raises(AssertionError):
            _recovery_restore_metadata(f.cp, loaded, prepared, scope)
    else:
        facts = _recovery_restore_metadata(f.cp, loaded, prepared, scope)
        assert facts == {
            "saved_checkpoint_sha256": f.cp.state_sha256,
            "loaded_checkpoint_sha256": f.cp.state_sha256,
            "prepared_phase_sha256": hashlib.sha256(
                json.dumps(
                    _mutable_json(prepared), sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest(),
            "phase_source_checkpoint_sha256": f.cp.state_sha256,
            "source_scope_sha256": scope,
        }


@pytest.mark.parametrize("change", ["token", "owner", "deadline"])
async def test_recovery_metadata_hashes_bind_lease_deadline_without_exposing_them(
    change: str,
) -> None:
    pool = MetadataPool(1000, datetime(2026, 1, 1, tzinfo=UTC))
    lease = _WorkerLease("local-protocol-owner", uuid4())
    before = _recovery_pool_metadata(pool, lease)
    if change == "deadline":
        pool.original_deadline += timedelta(seconds=1)
    else:
        lease = _WorkerLease(
            "other-owner" if change == "owner" else lease.worker_id,
            uuid4() if change == "token" else lease.token,
        )
    after = _recovery_pool_metadata(pool, lease)
    field = "deadline_sha256" if change == "deadline" else "lease_sha256"
    assert before[field] != after[field]
    encoded = json.dumps(after)
    assert str(lease.token) not in encoded and lease.worker_id not in encoded
    assert pool.original_deadline.isoformat() not in encoded


@pytest.mark.parametrize(
    "delay_after_begin", [False, True], ids=["ordinary", "committed-before-return"]
)
async def test_actual_service_repair_manual_approval_drains_real_checkpoint_without_pg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay_after_begin: bool
) -> None:
    """Actual accept/worker/adapter/Root pause and service drain; local SQL seam only."""

    from agent_hub.db import models
    from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
    from agent_hub.runs.self_repair import project_repair_scope_sha256
    from agent_hub.runs.service import RunService
    from agent_hub.runtime.registry import RuntimeRegistry
    from agent_hub.runtime.repair_projection import _Projection
    from tests.integration.runtime._model_review_repair_typed_fixture import decision

    f = await repair_fixture(
        tmp_path,
        accept=False,
        route_extras={
            "runtime_timeout_absolute_seconds": 60,
            "runtime_token_soft_base_tokens": 40000,
            "runtime_plan_timeout_seconds": 60,
            "engineering_final_report_version": 1,
            "permission_mode": "default",
        },
    )
    original_pool = present(
        await RunAdmissionRepository(f.db).get_pool(f.cp.tenant_id, f.cp.run_id)
    )
    held = next(row for row in f.db.rows if type(row) is models.RunAdmissionAttemptRow)
    original_hold = (held.attempt_id, held.state, held.token_hold, held.receipt_sha256)
    from tests.integration.runtime._model_review_repair_fixture import local_service_sql

    local_service_sql(f.db)
    f.db.rows[:] = [
        row for row in f.db.rows if type(row) not in (models.RunEventRow, models.RunCheckpointRow)
    ]
    f.db.run.routing_decision = dict(f.original_task.routing_decision)
    f.db.run.status = "running"
    f.db.run.worker_id = f.original.budget.lease.worker_id
    f.db.run.worker_lease_token = f.original_task.execution_token
    f.db.run.worker_lease_expires_at = datetime.now(UTC) + timedelta(seconds=90)
    root = ProjectWorkspaceStore(tmp_path).session_root(f.cp.tenant_id, "demo", "session")
    root.mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    wire = RepairWire(unknown_review=True, authorization_message=f.original_task.request)
    common = (
        tmp_path,
        f.runs,
        PostgresArtifactRepository(f.db),
        f.f.broker,
        f.f.state.platform,
        f.f.config_service,
        f.f.secrets,
        f.f.capacity,
        wire,
    )
    initial = await components(*common, tenant_id=f.cp.tenant_id)
    initial.runtime.prime_unknown = False
    async with worker_runtime_events(
        initial.runtime, f.original_task, worker_id=f.db.run.worker_id, review_broker=f.f.broker
    ) as stream:
        async for event in stream:
            async with f.db() as session:
                sequence = await f.runs.next_event_sequence(session, f.cp.run_id)
                await f.runs.persist_event(
                    session,
                    tenant_id=f.cp.tenant_id,
                    run_id=f.cp.run_id,
                    event=event.model_copy(update={"sequence": sequence}),
                )
    checkpoint = initial.runtime.last_checkpoint
    assert checkpoint is not None, [
        (row.kind, row.payload.get("reason"))
        for row in f.db.rows
        if type(row) is models.RunEventRow
    ]
    assert project_repair_checkpoint_is_safe(
        checkpoint,
        routing_decision=f.db.run.routing_decision,
        run_id=f.cp.run_id,
        tenant_id=f.cp.tenant_id,
        mode=TaskMode.DISCUSS,
    ), {
        "terminal": checkpoint.state.get("terminal"),
        "soft": checkpoint.state.get("remaining_timeout_seconds"),
        "absolute": checkpoint.state.get("remaining_absolute_timeout_seconds"),
        "ceiling": f.db.run.routing_decision.get("runtime_timeout_absolute_seconds"),
        "tool_status": [
            row.get("status") for row in ledger(checkpoint.state.get("tool_ledger", ()))
        ],
    }
    proposal = replace(
        decision(checkpoint),
        requires_approval=False,
        automatic_execution=True,
        automatic_checkpoint_sha256=checkpoint.state_sha256,
        automatic_scope_sha256=project_repair_scope_sha256(f.db.run.routing_decision),
    )
    f.db.run.routing_decision = {
        **mapping(f.db.run.routing_decision),
        "approval_kind": "self_repair",
        "decision_token": "local-real-checkpoint",
        "repair_proposal": proposal.to_proposal(run_id=f.cp.run_id),
    }
    f.db.run.status = "failed"
    f.runs.clear_worker_lease(f.db.run)
    await f.runs.accept_self_repair_and_enqueue(
        tenant_id=f.cp.tenant_id,
        run_id=f.cp.run_id,
        version=f.db.run.version,
        decision_token="local-real-checkpoint",
        automatic_checkpoint_sha256=checkpoint.state_sha256,
    )
    repair = await components(*common, tenant_id=f.cp.tenant_id)
    begin_approval = f.runs.begin_capability_approval
    waiting_call_counts = []

    async def observed_begin(
        tenant_id: UUID, run_id: UUID, **kwargs: Unpack[BeginApprovalArguments]
    ) -> RunRecord:
        record = await begin_approval(tenant_id, run_id, **kwargs)
        waiting_call_counts.append(len(wire.calls))
        if delay_after_begin:
            await asyncio.sleep(
                0.05
            )  # SQL transaction committed, actual response not returned yet.
        return record

    monkeypatch.setattr(f.runs, "begin_capability_approval", observed_begin)
    refreshed_statuses = []
    refresh = _Projection.refresh

    async def observe_refresh(self: _Projection) -> None:
        refreshed_statuses.append(f.db.run.status)
        await refresh(self)

    monkeypatch.setattr(_Projection, "refresh", observe_refresh)

    async def trusted_window() -> int:
        return 131072

    service = RunService(
        f.runs,
        runtime_registry=RuntimeRegistry((repair.runtime,)),
        router=None,
        task_queue=UnexpectedTaskQueue(),
        worker_id="local-repair-drain",
        runtime_timeout_seconds=60,
        runtime_token_budget=40000,
        review_broker=f.f.broker,
        main_agent_context_window_getter=trusted_window,
    )
    result = await asyncio.wait_for(service.execute(f.cp.run_id), timeout=15)
    approval_id = mapping(f.db.run.routing_decision).get("approval_id")
    assert approval_id, {
        "events": [
            (row.kind, row.payload.get("tool_name"), row.payload.get("reason"))
            for row in f.db.rows
            if type(row) is models.RunEventRow
        ][-8:],
        "tools": [(phase, name) for phase, name, _ in wire.tool_calls],
        "reviews": len(wire.reviews),
    }
    assert result.status is RunStatus.WAITING_APPROVAL
    saved = await latest_local_checkpoint(f.runs, f.db, f.cp.tenant_id, f.cp.run_id)
    assert checkpoint_waits_for_approval(saved, string(approval_id))
    assert set(refreshed_statuses) == {"running"}
    assert waiting_call_counts == [len(wire.calls)], "waiting may not start any new model calls"
    assert (repair.root_entries, repair.root_effects) == ([], [])
    assert (root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    after_pool = present(await RunAdmissionRepository(f.db).get_pool(f.cp.tenant_id, f.cp.run_id))
    assert (
        after_pool.authorization_turn_id,
        after_pool.original_token_grant,
        after_pool.original_deadline,
    ) == (
        original_pool.authorization_turn_id,
        original_pool.original_token_grant,
        original_pool.original_deadline,
    )
    assert (held.attempt_id, held.state, held.token_hold, held.receipt_sha256) == original_hold


async def accepted_case(
    storage: Storage, tmp_path: Path, redis_client: RedisCards, *, unknown_review: bool = False
) -> AcceptedCase:
    f = await pg_case(storage, tmp_path, redis_client, unknown_review=unknown_review)
    source_sha = review_assembly.runtime_source_fingerprint()
    root_before_sha = hashlib.sha256((f.root / TARGET).read_bytes()).hexdigest()
    initial = await f.service("a2-original-worker")
    result = await asyncio.wait_for(initial.service.execute(f.record.id), timeout=75)
    assert result.status is RunStatus.QUEUED, (
        "known stopped failure must really classify/auto-accept"
    )
    accepted = await f.repository.get(f.user.tenant_id, f.record.id)
    assert mapping(accepted.routing_decision)["source"] == "self_repair"
    assert mapping(accepted.routing_decision)["self_repair_source_run_id"] == str(f.record.id)
    outbox = await rows(f.factory, RunOutboxRow, f.record.id)
    assert any(":self-repair:" in item.idempotency_key for item in outbox)
    assert len(f.wire.unknown_ids) == 1
    assert (initial.root_entries, initial.root_effects) == ([], [])
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    unknown = next(item for item in attempts if item.attempt_id in f.wire.unknown_ids)
    assert unknown.state == "unknown" and unknown.token_hold > 0
    before = present(
        await RunAdmissionRepository(f.factory).get_pool(f.user.tenant_id, f.record.id)
    )
    unknown_observation = (unknown.attempt_id, unknown.token_hold, unknown.receipt_sha256)
    unknown_state_before = unknown.state
    scope_before = project_repair_phase.authority_digest(mapping(accepted.routing_decision))
    assert (
        scope_before
        == mapping(mapping(accepted.routing_decision)["repair_proposal"])["automatic_scope_sha256"]
    )
    async with f.factory() as session:
        saved_checkpoint = await f.repository.latest_checkpoint(
            session, tenant_id=f.user.tenant_id, run_id=f.record.id
        )
    assert saved_checkpoint is not None
    assert (
        saved_checkpoint.state_sha256
        == mapping(mapping(accepted.routing_decision)["repair_proposal"])[
            "automatic_checkpoint_sha256"
        ]
    )
    return AcceptedCase(
        f.factory,
        f.user,
        f.repository,
        f.wire,
        f.record,
        f.root,
        f.evidence,
        f.capacities,
        f.redis_client,
        f.service,
        source_sha,
        root_before_sha,
        before,
        unknown_observation,
        unknown_state_before,
        scope_before,
        saved_checkpoint,
        initial,
    )


async def test_discuss_service_repair_new_diagnosis_review_and_single_root_effect(
    storage: Storage,
    own_redis: RedisCards,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    record_property: Callable[[str, object], None],
) -> None:
    f = await accepted_case(storage, tmp_path, own_redis)
    assert (f.root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    repair = await f.service("a2-repair-worker")  # cold actual adapter, same durable DAO facts
    await asyncio.wait_for(repair.service.execute(f.record.id), timeout=75)
    assert f.wire.diagnosed and len(f.wire.reviews) == 1
    assert (f.root / TARGET).read_text(encoding="utf-8") == PATCHED
    assert repair.root_entries == [(f.record.id, {"path": TARGET, "content": PATCHED})]
    assert len(repair.root_effects) == 1
    contexts = f.wire.contexts
    assert len(contexts) == 2 and all(context.run_id == f.record.id for context in contexts)
    assert contexts[0].execution_token != contexts[1].execution_token
    assert contexts[1].routing_decision["source"] == "self_repair"
    assert [name for phase, name, _ in f.wire.tool_calls if phase == "repair"] == [
        "workspace.read",
        "workspace.write_text",
    ]
    intents = await rows(f.factory, RunReviewIntentRow, f.record.id)
    assert len(intents) == 1 and intents[0].status == "allow" and intents[0].consumed
    events = await rows(f.factory, RunReviewEventRow, f.record.id)
    assert sum(item.kind == "consumed" for item in events) == 1
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    unknown = next(item for item in attempts if item.attempt_id == f.unknown[0])
    assert (unknown.token_hold, unknown.receipt_sha256, unknown.state) == (
        *f.unknown[1:],
        "unknown",
    )
    review = next(item for item in attempts if item.purpose == "review")
    assert review.state == "completed" and review.deployment["deployment_id"] == "reviewer_1"
    assert all(
        sent["deployment_id"] == "primary_1"
        for sent in f.wire.sent.values()
        if sent["purpose"] == "primary"
    )
    assert {item.attempt_id for item in attempts} == set(f.wire.sent)
    assert len({item.physical_transport_id for item in attempts}) == len(attempts)
    for item in attempts:
        assert item.request_sha256 == f.wire.sent[item.attempt_id]["request_sha256"]
        assert item.physical_transport_id == f.wire.sent[item.attempt_id]["physical_id"]
    after = present(await RunAdmissionRepository(f.factory).get_pool(f.user.tenant_id, f.record.id))
    assert (after.authorization_turn_id, after.original_token_grant, after.original_deadline) == (
        f.before.authorization_turn_id,
        f.before.original_token_grant,
        f.before.original_deadline,
    )
    assert after.token_charged == sum(
        item.token_hold
        if item.state == "unknown"
        else integer(mapping(mapping(item.receipt)["usage"])["total_tokens"])
        for item in attempts
    )
    assert f.wire.bodies and all(body.closed for body in f.wire.bodies)
    await capacity_clean(f)
    await _record_recovery_metadata(f, repair, request, record_property)


async def test_discuss_repair_unknown_review_never_executes_or_replays(
    storage: Storage,
    own_redis: RedisCards,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    record_property: Callable[[str, object], None],
) -> None:
    f = await accepted_case(storage, tmp_path, own_redis, unknown_review=True)
    repair = await f.service("a2-repair-worker")
    result = await asyncio.wait_for(repair.service.execute(f.record.id), timeout=75)
    # Concrete UNKNOWN is denied by the existing gate, not an ordinary human ASK.
    assert result.status is RunStatus.FAILED
    assert f.wire.diagnosed and len(f.wire.reviews) == 1
    assert (f.root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    assert (repair.root_entries, repair.root_effects) == ([], [])
    intents = await rows(f.factory, RunReviewIntentRow, f.record.id)
    assert len(intents) == 1 and intents[0].status == "unknown" and not intents[0].consumed
    before = len(f.wire.calls)
    retry = await f.service("a2-retry-worker")
    await asyncio.wait_for(retry.service.execute(f.record.id), timeout=75)
    assert len(f.wire.calls) == before and len(f.wire.reviews) == 1
    assert (f.root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    attempts = await rows(f.factory, RunAdmissionAttemptRow, f.record.id)
    assert len([item for item in attempts if item.purpose == "review"]) == 1
    assert len([item for item in attempts if item.state == "unknown"]) == 2
    assert (retry.root_entries, retry.root_effects) == ([], [])
    await capacity_clean(f)
    await _record_recovery_metadata(
        f, repair, request, record_property, retry_calls=(before, len(f.wire.calls))
    )


async def test_discuss_sdk_stopped_checkpoint_protocol_without_pg(tmp_path: Path) -> None:
    """Actual adapter/SDK/Root diagnostic only; no claim of PG/service coverage."""
    f = await offline_assembly(tmp_path, mode=TaskMode.DISCUSS)
    route: dict[str, object] = {
        **f.s.task.routing_decision,
        "engineering_auto_repair_version": 1,
        "engineering_delivery_contract_version": 1,
        "engineering_checkpoint_ack_version": 1,
        "engineering_final_report_version": 1,
    }
    f.s.task = f.s.task.model_copy(update={"routing_decision": route})
    f.s.runs.record = replace(f.s.runs.record, routing_decision=route)
    f.db.run.routing_decision = route
    root = ProjectWorkspaceStore(tmp_path).session_root(f.s.task.tenant_id, "demo", "session")
    root.mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    case = await components(
        tmp_path,
        f.s.runs,
        InMemoryArtifactRepository(),
        f.broker,
        f.state.platform,
        f.config_service,
        f.secrets,
        f.capacity,
        RepairWire(),
        tenant_id=f.s.task.tenant_id,
    )
    events = []
    async with worker_runtime_events(
        case.runtime, f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as stream:
        async for event in stream:
            events.append(event)
    assert any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    checkpoint = case.runtime.last_checkpoint
    assert checkpoint is not None, (
        "runtime fault must occur after a real ACKed observation boundary; "
        f"events={[(str(event.kind), event.reason) for event in events]}; "
        f"calls={len(case.wire.calls)}; tools={case.wire.tool_calls}"
    )
    assert project_repair_checkpoint_is_safe(
        checkpoint,
        routing_decision=route,
        run_id=f.s.task.run_id,
        tenant_id=f.s.task.tenant_id,
        mode=TaskMode.DISCUSS,
    )
    assert len(case.wire.calls) >= 2
    assert case.wire.tool_calls == [("initial", "workspace.read", {"path": TARGET})]
    assert not case.wire.reviews
    assert len(case.wire.unknown_ids) == 1
    unknown = next(
        row
        for row in f.db.rows
        if type(row) is RunAdmissionAttemptRow and row.attempt_id in case.wire.unknown_ids
    )
    assert unknown.state == "unknown" and unknown.token_hold > 0
    assert (case.root_entries, case.root_effects) == ([], [])
    assert (root / TARGET).read_text(encoding="utf-8") == ORIGINAL


async def test_service_window_projection_preserves_actual_repair_phase_without_pg(
    tmp_path: Path,
) -> None:
    """A2's next production gate, after A1 has admitted the actual service projection."""
    f = await repair_fixture(tmp_path)
    original = await claim(f)
    context = await service_runtime_context(f, original, window=10000)
    assert await f.f.broker.prepare(context, worker_id="repair-worker") is not None
    checkpoint = present(context.checkpoint)
    assert project_repair_phase.prepare(
        original,
        checkpoint,
        ledger(checkpoint.state["model_ledger"]),
        ledger(checkpoint.state["tool_ledger"]),
    )

    async def check(actual: TaskContext) -> object:
        return prepare_projection_phase(actual, checkpoint)

    assert await projected_worker(f, context, check)


async def test_projected_phase_keeps_original_scope_through_productive_and_cold_resume_without_pg(
    tmp_path: Path,
) -> None:
    f = await repair_fixture(tmp_path)
    original = await claim(f)
    runtime = await service_runtime_context(f, original, window=10000)
    expected = mapping(mapping(original.routing_decision)["repair_proposal"])[
        "automatic_scope_sha256"
    ]
    assert project_repair_phase.authority_digest(original.routing_decision) == expected
    assert project_repair_phase.authority_digest(runtime.routing_decision) != expected

    async def work(actual: TaskContext) -> tuple[RuntimeCheckpoint, object]:
        checkpoint = await productive_checkpoint(f, actual)
        artifacts = tuple(
            PostgresArtifactRepository._hydrate(row)
            for row in f.db.rows
            if type(row) is RuntimeArtifactRow
        )
        return checkpoint, project_repair_phase.productive_boundary(actual, checkpoint, artifacts)

    checkpoint, before = await projected_worker(f, runtime, work)
    phase = checkpoint.state[project_repair_phase.PHASE_KEY]
    assert mapping(phase)["source_scope_sha256"] == expected
    artifacts = tuple(
        PostgresArtifactRepository._hydrate(row)
        for row in f.db.rows
        if type(row) is RuntimeArtifactRow
    )
    persisted = original.model_copy(update={"execution_token": None})
    assert project_repair_phase.productive_boundary(persisted, checkpoint, artifacts) == before
    f.runs.clear_worker_lease(f.db.run)
    f.db.run.status = "failed"
    continued = await f.runs.continue_project_repair_and_enqueue(
        tenant_id=original.tenant_id,
        run_id=original.run_id,
        version=f.db.run.version,
        checkpoint_sha256=checkpoint.state_sha256,
    )
    assert "main_agent_context_window_tokens" not in mapping(continued.routing_decision)
    cold = await service_runtime_context(f, await claim(f), window=20000)
    assert cold.execution_token != original.execution_token
    assert await f.f.broker.prepare(cold, worker_id="repair-worker") is not None

    async def restored(actual: TaskContext) -> object:
        return project_repair_phase.validate(
            phase,
            context=actual,
            models=ledger(checkpoint.state["model_ledger"]),
            tools=ledger(checkpoint.state["tool_ledger"]),
            tool_prefix=integer(checkpoint.state["engineering_tool_prefix"]),
            state=checkpoint.state,
            artifacts=artifacts,
        )["source_scope_sha256"]

    assert await projected_worker(f, cold, restored) == expected
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("value", [None, True, 0, -1, "10000", 10000.0, 10000001])
async def test_invalid_runtime_window_cannot_match_original_phase_scope_without_pg(
    tmp_path: Path, value: object
) -> None:
    f = await repair_fixture(tmp_path)
    context = await claim(f)
    context = context.model_copy(
        update={
            "routing_decision": {
                **context.routing_decision,
                "main_agent_context_window_tokens": value,
            }
        }
    )
    with pytest.raises(ValueError, match="project repair phase is invalid"):
        project_repair_phase.prepare(
            context, f.cp, ledger(f.cp.state["model_ledger"]), ledger(f.cp.state["tool_ledger"])
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("permission_mode", "full_access"),
        ("requested_permissions", ["*"]),
        ("project_id", "other"),
        ("runtime_plan_token_budget", 40001),
        ("runtime_token_absolute_tokens", 40001),
        ("runtime_timeout_absolute_seconds", 61),
        ("context_window_tokens", 8000),
        ("source_run_id", "00000000-0000-4000-8000-000000000001"),
        ("unknown_authority", "added"),
    ],
)
async def test_window_candidate_does_not_hide_any_other_phase_authority_without_pg(
    tmp_path: Path, key: str, value: object
) -> None:
    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    context = context.model_copy(
        update={"routing_decision": {**context.routing_decision, key: value}}
    )
    with pytest.raises(ValueError, match="project repair phase is invalid"):
        project_repair_phase.prepare(
            context, f.cp, ledger(f.cp.state["model_ledger"]), ledger(f.cp.state["tool_ledger"])
        )


@pytest.mark.parametrize("key", ["main_agent_context_window_tokens", "context_window_tokens"])
@pytest.mark.parametrize("change", ["unchanged", "changed", "removed"])
async def test_original_persisted_window_scope_is_never_dropped_without_pg(
    tmp_path: Path, key: str, change: str
) -> None:
    f = await repair_fixture(tmp_path, route_extras={key: 8000})
    context = await service_runtime_context(f, await claim(f))
    route = dict(context.routing_decision)
    if change == "changed":
        route[key] = 10000
    elif change == "removed":
        route.pop(key)
    context = context.model_copy(update={"routing_decision": route})
    if change == "unchanged":
        assert project_repair_phase.prepare(
            context, f.cp, ledger(f.cp.state["model_ledger"]), ledger(f.cp.state["tool_ledger"])
        )
    else:
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            project_repair_phase.prepare(
                context, f.cp, ledger(f.cp.state["model_ledger"]), ledger(f.cp.state["tool_ledger"])
            )


async def test_persisted_reader_cannot_adopt_runtime_only_window_candidate_without_pg(
    tmp_path: Path,
) -> None:
    f = await repair_fixture(tmp_path)
    runtime = await service_runtime_context(f, await claim(f))
    assert await f.f.broker.prepare(runtime, worker_id="repair-worker") is not None
    # The read-only DAO deliberately has no worker token in its persisted context.
    f.db.run.routing_decision = {
        **mapping(f.db.run.routing_decision),
        "main_agent_context_window_tokens": 10000,
    }
    assert await f.runs.load_same_run_repair_continuity(runtime.tenant_id, runtime.run_id) is None
    with pytest.raises(ReviewAssemblyUnavailable):
        await f.f.broker.prepare(runtime, worker_id="repair-worker")


@pytest.mark.parametrize("defect", ["profile", "role", "source", "phase_scope", "prefix"])
async def test_projected_phase_retains_profile_role_source_and_prefix_fences_without_pg(
    tmp_path: Path, defect: str | None
) -> None:
    from agent_hub.auth.models import Role

    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    if defect == "role":
        with pytest.raises(ReviewAssemblyUnavailable):
            await f.f.broker.prepare(
                context.model_copy(update={"actor_role": Role.ADMIN}), worker_id="repair-worker"
            )
        return
    if defect in {"profile", "source"}:
        route = mutable(mutable_json(context.routing_decision))
        if defect == "profile":
            profile = mutable(route["approval_review_profile"])
            profile["timeout_seconds"] = integer(profile["timeout_seconds"]) + 1
        else:
            route["source"] = "other"
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            project_repair_phase.prepare(
                context.model_copy(update={"routing_decision": route}),
                f.cp,
                ledger(f.cp.state["model_ledger"]),
                ledger(f.cp.state["tool_ledger"]),
            )
        return

    async def check(actual: TaskContext) -> None:
        phase = prepare_projection_phase(actual, f.cp)
        if defect == "phase_scope":
            phase["source_scope_sha256"] = project_repair_phase.authority_digest(
                actual.routing_decision
            )
        else:
            phase["source_tool_sha256"] = "f" * 64
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            project_repair_phase.validate(
                phase,
                context=actual,
                models=ledger(f.cp.state["model_ledger"]),
                tools=ledger(f.cp.state["tool_ledger"]),
                tool_prefix=len(ledger(f.cp.state["tool_ledger"])),
                state=f.cp.state,
            )

    await projected_worker(f, context, check)


@pytest.mark.parametrize(
    "relative", ["runtime/autogen/project_repair_phase.py", "runtime/repair_projection.py"]
)
async def test_phase_copy_source_change_invalidates_runtime_evidence_without_pg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    """Mutate actual copied bytes, then exercise the real fingerprint/evidence fences."""
    source = Path(review_assembly.__file__).resolve().parents[1]
    original_phase = (source / relative).read_bytes()
    copied = tmp_path / "copied-package" / "agent_hub"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns("__pycache__"))
    name = "agent_hub.runtime._a2_fingerprint_copy"
    spec = importlib.util.spec_from_file_location(name, copied / "runtime/review_assembly.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    before = module.runtime_source_fingerprint()
    assert before == review_assembly.runtime_source_fingerprint()
    f = await offline_assembly(tmp_path / "fixture")
    document = evidence_document(f)
    mutable(document["worker_verification"])["source_sha256"] = before
    evidence = tmp_path / "installed-evidence.json"
    body = json.dumps(document).encode()
    evidence.write_bytes(body)
    pin = hashlib.sha256(body).hexdigest()
    contracts, verification = module.load_server_review_evidence(evidence, expected_sha256=pin)
    broker = module.ReviewAssemblyBroker(
        config_service=f.config_service,
        config_getter=f.broker.current_config,
        run_repository=f.s.runs,
        session_factory=f.db,
        secret_service=f.secrets,
        capacity_factory=f.capacity,
        transport=f.broker._transport,
        billing_contracts=contracts,
        verification=verification,
    )
    snapshot, reason = await broker._compile(f.s.task.tenant_id)
    assert snapshot is not None and reason == "ready"
    broker._require_activation_current(snapshot)
    phase = copied / relative
    phase.write_bytes(phase.read_bytes() + b"\n# A2 copied scope gate change\n")
    assert (source / relative).read_bytes() == original_phase
    assert module.runtime_source_fingerprint() != before
    with pytest.raises(ValueError, match="invalid server review evidence"):
        module.load_server_review_evidence(evidence, expected_sha256=pin)
    current, reason = await broker._compile(f.s.task.tenant_id)
    assert current is None and reason == "current_mode_fence_unverified"
    with pytest.raises(module.ReviewAssemblyUnavailable, match="review_activation_changed"):
        broker._require_activation_current(snapshot)
    assert not f.state.calls


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_worker_strip_window_and_cold_resume_without_pg(
    tmp_path: Path, legacy: bool
) -> None:
    from agent_hub.models.physical_budget import _run_budget

    f = await projection_case(tmp_path, legacy=legacy)
    contexts, checkpoints = [], []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            contexts.append(actual)
            assert not f.private.keys() & actual.routing_decision.keys()
            assert actual.routing_decision["main_agent_context_window_tokens"] == 10000
            budget = _run_budget.get()
            if legacy:
                assert budget is None and "approval_review_profile" not in actual.routing_decision
            else:
                await present(budget).require_current()
            checkpoint = await productive_checkpoint(f, actual)
            checkpoints.append(checkpoint)
            phase = checkpoint.state[project_repair_phase.PHASE_KEY]
            expected = mapping(mapping(f.db.run.routing_decision)["repair_proposal"])[
                "automatic_scope_sha256"
            ]
            assert mapping(phase)["source_scope_sha256"] == expected
            artifacts = tuple(
                PostgresArtifactRepository._hydrate(row)
                for row in f.db.rows
                if type(row) is RuntimeArtifactRow
            )
            project_repair_phase.productive_boundary(actual, checkpoint, artifacts)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    kwargs: WorkerArguments = {"worker_id": "repair-worker"}
    if legacy:
        kwargs["run_repository"] = f.runs
    else:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Runtime(), f.context, **kwargs) as events:
        async for _ in events:
            pass
    checkpoint = checkpoints[0]
    f.runs.clear_worker_lease(f.db.run)
    f.db.run.status = "failed"
    await f.runs.continue_project_repair_and_enqueue(
        tenant_id=f.context.tenant_id,
        run_id=f.context.run_id,
        version=f.db.run.version,
        checkpoint_sha256=checkpoint.state_sha256,
    )
    cold = await service_runtime_context(f, await claim(f), window=10000)

    class Cold(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert actual.execution_token != contexts[0].execution_token
            artifacts = tuple(
                PostgresArtifactRepository._hydrate(row)
                for row in f.db.rows
                if type(row) is RuntimeArtifactRow
            )
            project_repair_phase.validate(
                checkpoint.state[project_repair_phase.PHASE_KEY],
                context=actual,
                models=ledger(checkpoint.state["model_ledger"]),
                tools=ledger(checkpoint.state["tool_ledger"]),
                tool_prefix=2,
                state=checkpoint.state,
                artifacts=artifacts,
            )
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(Cold(), cold, **kwargs) as events:
        async for _ in events:
            pass
    assert pool_values(f.db) == f.before
    assert not f.f.state.calls


def prepare_projection_phase(
    context: TaskContext, checkpoint: RuntimeCheckpoint
) -> dict[str, JsonValue]:
    return project_repair_phase.prepare(
        context,
        checkpoint,
        ledger(checkpoint.state["model_ledger"]),
        ledger(checkpoint.state["tool_ledger"]),
    )


@pytest.mark.parametrize(
    "defect", ["lease", "role", "child", "permission", "profile", "token", "time", "unknown"]
)
async def test_private_projection_exact_task_binding_rejects_mutations_without_pg(
    tmp_path: Path, defect: str | None
) -> None:
    from agent_hub.auth.models import Role

    f = await projection_case(tmp_path)

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            updates: dict[str, object] = {}
            route = mutable(mutable_json(actual.routing_decision))
            if defect == "lease":
                updates["execution_token"] = uuid4()
            elif defect == "role":
                updates["actor_role"] = Role.ADMIN
            elif defect == "child":
                updates.update(run_id=uuid4(), checkpoint=None)
            elif defect == "permission":
                route["requested_permissions"] = ["*"]
            elif defect == "profile":
                profile = mutable(route["approval_review_profile"])
                profile["timeout_seconds"] = integer(profile["timeout_seconds"]) + 1
            elif defect == "token":
                route["runtime_token_absolute_tokens"] = (
                    integer(route["runtime_token_absolute_tokens"]) + 1
                )
            elif defect == "time":
                route["runtime_timeout_absolute_seconds"] = (
                    integer(route["runtime_timeout_absolute_seconds"]) + 1
                )
            else:
                route["unknown_authority"] = "new"
            updates["routing_decision"] = route
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(actual.model_copy(update=updates), f.cp)
            public = json.dumps(actual.to_payload())
            assert not any(key in public or value in public for key, value in f.private.items())
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
@pytest.mark.parametrize("defect", ["receipt", "locator", "authority", "owner", "expiry", "actor"])
async def test_private_projection_refresh_rejects_persisted_drift_without_pg(
    tmp_path: Path, legacy: bool, defect: str | None
) -> None:
    f = await projection_case(tmp_path, legacy=legacy)
    reached = []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )
            reached.append("stale work")
            prepare_projection_phase(actual, f.cp)

    kwargs: WorkerArguments = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Runtime(), f.context, **kwargs) as stream:
        await anext(stream)
        if defect in {"receipt", "locator", "authority"}:
            key = {
                "receipt": "project_verification_receipt",
                "locator": "approval_checkpoint_worker_token",
                "authority": "unknown_authority",
            }[defect]
            f.db.run.routing_decision = {**mapping(f.db.run.routing_decision), key: "changed"}
        elif defect == "owner":
            f.db.run.worker_lease_token = uuid4()
        elif defect == "expiry":
            f.db.run.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            f.db.run.actor_id = uuid4()
        with pytest.raises(ValueError, match="repair_projection_unavailable"):
            await anext(stream)
    assert not reached
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_cannot_escape_owner_or_child_task_binding_without_pg(
    tmp_path: Path, legacy: bool
) -> None:
    from agent_hub.capabilities.execution_authority import bind_execution_authority

    f = await projection_case(tmp_path, legacy=legacy)
    release, escaped = asyncio.Event(), []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            child = actual.model_copy(
                update={"run_id": uuid4(), "checkpoint": None, "execution_token": uuid4()}
            )
            with (
                bind_execution_authority(child, worker_id="child-worker"),
                pytest.raises(ValueError, match="project repair phase is invalid"),
            ):
                prepare_projection_phase(actual, f.cp)
            with (
                bind_execution_authority(actual, worker_id="repair-worker", actor="other_agent"),
                pytest.raises(ValueError, match="project repair phase is invalid"),
            ):
                prepare_projection_phase(actual, f.cp)

            async def late() -> None:
                await release.wait()
                with pytest.raises(ValueError, match="project repair phase is invalid"):
                    prepare_projection_phase(actual, f.cp)

            escaped.append(asyncio.create_task(late()))
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    kwargs: WorkerArguments = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    try:
        async with worker_runtime_events(Runtime(), f.context, **kwargs) as stream:
            await anext(stream)
    finally:
        release.set()
        await asyncio.gather(*escaped)
    assert pool_values(f.db) == f.before


async def test_private_projection_json_and_unsealed_objects_cannot_forge_proof_without_pg(
    tmp_path: Path,
) -> None:
    from agent_hub.runtime import repair_projection
    from agent_hub.runtime.contracts import TaskContext

    f = await projection_case(tmp_path)

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            public = TaskContext.from_payload(actual.to_payload())
            assert public.execution_token is None
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(public, f.cp)
            route = {**actual.routing_decision, "repair_projection_proof": {"approved": True}}
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(
                    actual.model_copy(update={"routing_decision": route}), f.cp
                )
            current = present(repair_projection._CURRENT.get())
            for fake in (
                {"scope": current.proof.scope, "approved": True},
                replace(current, proof=replace(current.proof, seal=object())),
            ):
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(
                        repair_projection,
                        "_CURRENT",
                        ContextVar("injected_projection", default=fake),
                    )
                    with pytest.raises(ValueError, match="project repair phase is invalid"):
                        prepare_projection_phase(actual, f.cp)
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass


async def test_private_projection_cached_lease_expiry_rejects_without_refresh_without_pg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.runtime import repair_projection

    f = await projection_case(tmp_path)

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)

            class Expired(datetime):
                @classmethod
                def now(cls, tz: tzinfo | None = None) -> Expired:
                    return cls.fromisoformat((datetime.now(UTC) + timedelta(hours=1)).isoformat())

            with monkeypatch.context() as patch:
                patch.setattr(repair_projection, "datetime", Expired)
                with pytest.raises(ValueError, match="project repair phase is invalid"):
                    prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_refreshes_same_lease_but_not_original_pool_without_pg(
    tmp_path: Path, legacy: bool
) -> None:
    f = await projection_case(tmp_path, legacy=legacy)
    reached = []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )
            assert prepare_projection_phase(actual, f.cp)
            reached.append(True)
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=2,
                run_id=actual.run_id,
                reason="bounded probe",
            )
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                prepare_projection_phase(actual, f.cp)

    kwargs: WorkerArguments = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Runtime(), f.context, **kwargs) as stream:
        await anext(stream)
        f.db.run.worker_lease_expires_at = present(f.db.run.worker_lease_expires_at) + timedelta(
            seconds=60
        )
        await anext(stream)
        f.db.run.status = "completed"  # terminal consumer update must permit natural cleanup
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert reached and pool_values(f.db) == f.before


async def test_private_projection_does_not_downgrade_configuration_or_unknown_fences_without_pg(
    tmp_path: Path,
) -> None:
    from agent_hub.models.physical_budget import _run_budget

    f = await projection_case(tmp_path)
    reached = []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            checkpoint = await productive_checkpoint(f, actual)
            state = mutable(checkpoint.to_payload()["state"])
            mutable(sequence(state["tool_ledger"])[-1])["status"] = "unknown"
            with pytest.raises(ValueError, match="project repair phase is invalid"):
                project_repair_phase.validate(
                    state[project_repair_phase.PHASE_KEY],
                    context=actual,
                    models=ledger(state["model_ledger"]),
                    tools=ledger(state["tool_ledger"]),
                    tool_prefix=2,
                    state=json_map(state),
                )
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )
            await present(_run_budget.get()).require_current()
            reached.append("downgraded work")

    async with worker_runtime_events(
        Runtime(), f.context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        await anext(stream)
        f.f.state.version += 1
        with pytest.raises(ReviewAssemblyUnavailable, match="review_configuration_changed"):
            await anext(stream)
    assert not reached and pool_values(f.db) == f.before
    unknown = next(row for row in f.db.rows if type(row) is RunAdmissionAttemptRow)
    assert (unknown.state, unknown.token_hold) == ("unknown", 18000)


async def test_private_projection_legacy_service_adapter_passes_repository_without_pg(
    tmp_path: Path,
) -> None:
    from agent_hub.runs.service import _adaptive_runtime_events

    f = await projection_case(tmp_path, legacy=True)
    reached = []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            reached.append(True)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async for _ in _adaptive_runtime_events(
        Runtime(),
        f.context,
        configured_tokens=40000,
        routing_decision=f.context.routing_decision,
        initial_progress_units=0,
        worker_id="repair-worker",
        run_repository=f.runs,
    ):
        pass
    assert reached and pool_values(f.db) == f.before


@pytest.mark.parametrize("change", ["lease", "role", "owner_closed"])
async def test_private_projection_window_only_cannot_fallback_after_failure_without_pg(
    tmp_path: Path, change: str
) -> None:
    from agent_hub.auth.models import Role

    f = await repair_fixture(tmp_path)
    context = await service_runtime_context(f, await claim(f))
    saved = []

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            saved.append(actual)
            if change != "owner_closed":
                updates = (
                    {"execution_token": uuid4()}
                    if change == "lease"
                    else {"actor_role": Role.ADMIN}
                )
                with pytest.raises(ValueError, match="project repair phase is invalid"):
                    prepare_projection_phase(actual.model_copy(update=updates), f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    async with worker_runtime_events(
        Runtime(), context, worker_id="repair-worker", review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass
    if change == "owner_closed":
        with pytest.raises(ValueError, match="project repair phase is invalid"):
            prepare_projection_phase(saved[0], f.cp)


async def test_private_projection_legacy_without_trusted_repository_fails_closed_without_pg(
    tmp_path: Path,
) -> None:
    f = await projection_case(tmp_path, legacy=True)

    class Runtime(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started", sequence=1, run_id=actual.run_id, payload={"phase": "probe"}
            )

    with pytest.raises(ValueError, match="project repair phase is invalid"):
        async with worker_runtime_events(Runtime(), f.context, worker_id="repair-worker") as stream:
            await anext(stream)


@pytest.mark.parametrize("legacy", [False, True], ids=["model_profile", "legacy"])
async def test_private_projection_nested_worker_cannot_borrow_parent_without_pg(
    tmp_path: Path, legacy: bool
) -> None:
    f = await projection_case(tmp_path, legacy=legacy)
    child_seen = []

    class Parent(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            assert prepare_projection_phase(actual, f.cp)
            child = actual.model_copy(
                update={"run_id": uuid4(), "checkpoint": None, "execution_token": uuid4()}
            )

            class Child(StreamRuntimeStub):
                async def run(self, own: TaskContext) -> AsyncGenerator[RunEvent, None]:
                    child_seen.append(True)
                    with pytest.raises(ValueError, match="project repair phase is invalid"):
                        prepare_projection_phase(actual, f.cp)
                    yield RunEvent(
                        kind="runtime.started",
                        sequence=1,
                        run_id=own.run_id,
                        payload={"phase": "child-probe"},
                    )

            if legacy:
                async with worker_runtime_events(
                    Child(), child, worker_id="child-worker"
                ) as stream:
                    await anext(stream)
            else:
                with pytest.raises(
                    ReviewAssemblyUnavailable, match="derived_run_pool_continuity_unsupported"
                ):
                    async with worker_runtime_events(
                        Child(), child, worker_id="child-worker"
                    ) as stream:
                        await anext(stream)
            assert prepare_projection_phase(actual, f.cp)
            yield RunEvent(
                kind="runtime.started",
                sequence=1,
                run_id=actual.run_id,
                payload={"phase": "parent-probe"},
            )

    kwargs: WorkerArguments = {"worker_id": "repair-worker", "run_repository": f.runs}
    if not legacy:
        kwargs["review_broker"] = f.f.broker
    async with worker_runtime_events(Parent(), f.context, **kwargs) as stream:
        async for _ in stream:
            pass
    assert bool(child_seen) is legacy
    assert pool_values(f.db) == f.before


@pytest.mark.parametrize(
    "unknown_review,omission",
    [(False, None), (True, None), (False, "inspect"), (False, "conversation")],
    ids=["allow", "unknown", "missing-inspect", "missing-conversation"],
)
async def test_native_initial_service_classifies_and_accepts_actual_stopped_boundary_without_pg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_review: bool, omission: str | None
) -> None:
    """Catch a participant/route that cannot reach native claim's engineering ACK."""
    from agent_hub.api.routers.runs import CreateRunRequest
    from agent_hub.db import models
    from agent_hub.runs.authorization import _persist_authenticated_input
    from agent_hub.runs.repository import RunRepository
    from agent_hub.runs.service import RunService
    from agent_hub.runs.workspace import workspace_selection
    from agent_hub.runtime.registry import RuntimeRegistry
    from tests.integration.runtime._model_review_repair_fixture import (
        initial_route,
        local_service_sql,
    )
    from tests.integration.runtime._model_review_repair_typed_fixture import (
        capture_submission_input,
    )

    f = await offline_assembly(tmp_path, mode=TaskMode.DISCUSS)
    db = RepairDB(f.s)
    db.rows.remove(db.pool)  # a fresh run; only the real broker may open its original pool
    db.run.created_at = datetime.now(UTC)
    db.run.version = 1
    db.run.status = "queued"
    RunRepository.clear_worker_lease(db.run)
    selection = workspace_selection(
        project_id="demo",
        session_id="session",
        sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "command.run"),
        permission_mode="auto_review",
    )
    profile = await f.broker.submission_profile(db.run.tenant_id, timeout_seconds=120)
    body = CreateRunRequest(
        message=db.run.request,
        mode=TaskMode.DISCUSS,
        conversation_id="a2-local-" + uuid4().hex,
        project_id="demo",
        workspace_session_id="session",
        sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "command.run"),
        permission_mode="auto_review",
        execution_backend="systemd",
        runtime_timeout_seconds=120,
    )
    route = initial_route(selection, profile, conversation_id=body.conversation_id)
    if omission == "conversation":
        route.pop("conversation_id")
    db.run.routing_decision = route
    db.rows[:] = [
        row
        for row in db.rows
        if type(row)
        not in (
            models.RunAuthenticatedInputRow,
            models.RunInputAuthorizationRow,
        )
    ]
    local_service_sql(db)
    async with db() as session:
        await _persist_authenticated_input(session, db.run, capture_submission_input(db.run, body))
    repository = RunRepository(db)
    prior_broker = f.broker
    f.broker = review_assembly.ReviewAssemblyBroker(
        config_service=f.config_service,
        config_getter=prior_broker.current_config,
        run_repository=repository,
        session_factory=db,
        secret_service=f.secrets,
        capacity_factory=f.capacity,
        transport=prior_broker._transport,
        billing_contracts=prior_broker._contracts,
        verification=prior_broker._verification,
    )
    root = ProjectWorkspaceStore(tmp_path).session_root(db.run.tenant_id, "demo", "session")
    root.mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    wire = RepairWire(authorization_message=db.run.request, unknown_review=unknown_review)
    case = await components(
        tmp_path,
        repository,
        PostgresArtifactRepository(db),
        f.broker,
        f.state.platform,
        f.config_service,
        f.secrets,
        f.capacity,
        wire,
        tenant_id=db.run.tenant_id,
    )

    if omission == "inspect":
        import tests.integration.runtime._model_review_repair_fixture as fixture_module

        participant = fixture_module.DiscussionParticipant

        def without_inspect(**kwargs: Unpack[ParticipantArguments]) -> DiscussionParticipant:
            if kwargs["id"] == "implementer":
                kwargs["allowed_tools"] = tuple(
                    name for name in kwargs["allowed_tools"] if name != "project.inspect"
                )
            return participant(**kwargs)

        monkeypatch.setattr(fixture_module, "DiscussionParticipant", without_inspect)

    async def trusted_main_window() -> int:
        return 10000  # actual fixed local primary catalog; native catalog is 131072

    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((case.runtime,)),
        router=None,
        task_queue=UnexpectedTaskQueue(),
        worker_id="a2-local-initial",
        runtime_timeout_seconds=120,
        runtime_token_budget=1000000,
        review_broker=f.broker,
        main_agent_context_window_getter=trusted_main_window,
    )
    result = await asyncio.wait_for(service.execute(db.run.id), timeout=30)
    events = await repository.raw_events(db.run.tenant_id, db.run.id)
    diagnostic = [
        (str(event.kind), event.reason, dict(event.payload))
        for event in events
        if str(event.kind) in {"runtime.failed", "repair.classified", "repair.skipped"}
    ]
    if omission == "inspect":
        assert result.status is RunStatus.FAILED
        assert case.runtime.last_checkpoint is None and not wire.tool_calls and not wire.reviews
        assert (case.root_entries, case.root_effects) == ([], [])
        assert any(
            str(event.kind) == "repair.skipped"
            and event.payload.get("skip_reason") == "project_repair_checkpoint_not_resumable"
            for event in events
        )
        return
    assert result.status is RunStatus.QUEUED, diagnostic
    assert case.runtime.last_checkpoint is not None
    assert wire.tool_calls == [("initial", "project.inspect", {})]
    assert len(wire.unknown_ids) == 1
    assert (case.root_entries, case.root_effects) == ([], [])
    assert (root / TARGET).read_text(encoding="utf-8") == ORIGINAL
    assert db.run.routing_decision["source"] == "self_repair"
    assert db.run.routing_decision["self_repair_source_run_id"] == str(db.run.id)
    classified = [event for event in events if str(event.kind) == "repair.classified"]
    assert len(classified) == 1 and classified[0].payload["failure_category"] == "runtime_failure"
    assert classified[0].payload["automatic_execution"] is True
    assert any(
        type(row) is models.RunOutboxRow and ":self-repair:" in row.idempotency_key
        for row in db.rows
    )
    pool = next(row for row in db.rows if type(row) is models.RunAdmissionPoolRow)
    assert pool.original_token_grant == 1000000
    unknown = next(
        row
        for row in db.rows
        if type(row) is models.RunAdmissionAttemptRow and row.attempt_id in wire.unknown_ids
    )
    assert unknown.state == "unknown" and unknown.token_hold > 0
    assert wire.bodies and all(body.closed for body in wire.bodies)

    original_budget = (
        pool.authorization_turn_id,
        pool.original_token_grant,
        pool.original_deadline,
    )
    original_unknown = (
        unknown.attempt_id,
        unknown.token_hold,
        unknown.receipt_sha256,
        unknown.state,
    )
    original_lease = wire.contexts[0].execution_token
    original_scope = mapping(mapping(db.run.routing_decision)["repair_proposal"])[
        "automatic_scope_sha256"
    ]
    saved = await latest_local_checkpoint(repository, db, db.run.tenant_id, db.run.id)
    assert saved is not None and saved.state_sha256 == case.runtime.last_checkpoint.state_sha256
    repair = await components(
        tmp_path,
        repository,
        PostgresArtifactRepository(db),
        f.broker,
        f.state.platform,
        f.config_service,
        f.secrets,
        f.capacity,
        wire,
        tenant_id=db.run.tenant_id,
    )
    resumed = RunService(
        repository,
        runtime_registry=RuntimeRegistry((repair.runtime,)),
        router=None,
        task_queue=UnexpectedTaskQueue(),
        worker_id="a2-local-restored",
        runtime_timeout_seconds=120,
        runtime_token_budget=1000000,
        review_broker=f.broker,
        main_agent_context_window_getter=trusted_main_window,
    )
    result = await asyncio.wait_for(resumed.execute(db.run.id), timeout=30)
    assert len(wire.contexts) == 2 and wire.contexts[1].execution_token != original_lease
    assert wire.contexts[1].run_id == wire.contexts[0].run_id == db.run.id
    assert present(repair.runtime.loaded_checkpoint).state_sha256 == saved.state_sha256
    assert (
        mapping(present(repair.runtime.phase_checkpoint).state[project_repair_phase.PHASE_KEY])[
            "source_scope_sha256"
        ]
        == original_scope
    )
    if omission == "conversation":
        assert result.status is RunStatus.FAILED
        assert wire.diagnosed and not wire.reviews
        assert (repair.root_entries, repair.root_effects) == ([], [])
        assert (root / TARGET).read_text(encoding="utf-8") == ORIGINAL
        assert (
            unknown.attempt_id,
            unknown.token_hold,
            unknown.receipt_sha256,
            unknown.state,
        ) == original_unknown
        return
    assert wire.diagnosed and len(wire.reviews) == 1, {
        "status": result.status,
        "tools": wire.tool_calls,
        "events": [
            (str(e.kind), e.reason, dict(e.payload))
            for e in await repository.raw_events(db.run.tenant_id, db.run.id)
        ],
    }
    assert [name for phase, name, _ in wire.tool_calls if phase == "repair"] == [
        "workspace.read",
        "workspace.write_text",
    ]
    assert (
        pool.authorization_turn_id,
        pool.original_token_grant,
        pool.original_deadline,
    ) == original_budget
    assert (
        unknown.attempt_id,
        unknown.token_hold,
        unknown.receipt_sha256,
        unknown.state,
    ) == original_unknown
    intents = [row for row in db.rows if type(row) is models.RunReviewIntentRow]
    assert len(intents) == 1
    attempts = [row for row in db.rows if type(row) is models.RunAdmissionAttemptRow]
    reviews = [row for row in attempts if row.purpose == "review"]
    assert len(reviews) == 1 and reviews[0].deployment["deployment_id"] == "reviewer_1"
    assert all(
        row.deployment != reviews[0].deployment for row in attempts if row.purpose == "primary"
    )
    assert {row.attempt_id for row in attempts} == set(wire.sent)
    if unknown_review:
        assert result.status is RunStatus.FAILED
        assert intents[0].status == "unknown" and not intents[0].consumed
        assert (repair.root_entries, repair.root_effects) == ([], [])
        assert (root / TARGET).read_text(encoding="utf-8") == ORIGINAL
        before_retry = len(wire.calls)
        await resumed.execute(db.run.id)
        assert len(wire.calls) == before_retry and len(wire.reviews) == 1
    else:
        assert intents[0].status == "allow" and intents[0].consumed
        assert repair.root_entries == [(db.run.id, {"path": TARGET, "content": PATCHED})]
        assert len(repair.root_effects) == 1
        assert (root / TARGET).read_text(encoding="utf-8") == PATCHED
    assert wire.bodies and all(body.closed for body in wire.bodies)
