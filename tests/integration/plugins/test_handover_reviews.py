"""Real PostgreSQL review persistence, rollback and lock evidence.

Requires AGENT_HUB_TEST_DATABASE_URL pointing to a disposable private database
and AGENT_HUB_HANDOVER_TEST_DB_ISOLATED=1. Inherited integration fixtures migrate
and clean that database. Missing opt-in skips before database fixture setup.
No Docker, network provider, model, installer or generated-code execution.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, cast
from uuid import uuid4

import pytest
from sqlalchemy import Table, delete, event, select, text, update
from sqlalchemy.engine import Connection, ExceptionContext
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.dml import Insert, Update

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, UserRow
from agent_hub.plugins.handover_reviews import (
    GENERATED_REVIEW_APPROVAL_MARKER,
    GeneratedPluginReviewService,
    StoredPluginReviewReport,
    validate_generated_approval_in_session,
)
from agent_hub.plugins.handover_storage import (
    HANDOVER_KIND,
    Conflict,
    HandoverReceipt,
    NotFound,
    StorageUnavailable,
    acquire_plugin_registration_lock,
)
from tests.integration.plugins import test_handover_confirmation as sql_tests
from tests.unit.plugins.test_handover_sources import forbid_execution_and_signing

pytestmark = sql_tests.pytestmark
sql_context = sql_tests.context
forbid_execution = forbid_execution_and_signing


@dataclass
class Context:
    sql: sql_tests.Context
    service: GeneratedPluginReviewService
    pending: HandoverReceipt

    async def scan(self, *, key: str = "scan-source") -> StoredPluginReviewReport:
        return await self.service.scan_pending_package(
            self.sql.installer,
            self.sql.run_id,
            self.pending.handover_id,
            archive_bytes=self.sql.archive,
            key_id=sql_tests.KEY_ID,
            idempotency_key=key,
        )

    async def get(self, report: StoredPluginReviewReport) -> StoredPluginReviewReport:
        return await self.service.get_report(
            self.sql.installer,
            self.sql.run_id,
            self.pending.handover_id,
            report.report_id,
        )

    async def approve(self, report: StoredPluginReviewReport) -> None:
        result = await self.service.approve_generated(
            self.sql.installer,
            sql_tests.PLUGIN_ID,
            report_id=report.report_id,
            report_sha256=report.report_sha256,
            reason="reviewed frozen source",
        )
        assert result is not None and result.package_metadata is not None
        assert result.package_metadata.approval_state == "approved"
        assert result.enabled is False

    async def plugin(self, session: AsyncSession, *, lock: bool = False) -> AdminResourceRow:
        query = select(AdminResourceRow).where(
            AdminResourceRow.tenant_id == self.sql.creator.tenant_id,
            AdminResourceRow.kind == "plugin",
            AdminResourceRow.resource_id == sql_tests.PLUGIN_ID,
        )
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query.execution_options(populate_existing=True, autoflush=False))
        assert row is not None
        return row

    async def matching(self, report: StoredPluginReviewReport) -> StoredPluginReviewReport:
        async with self.sql.factory() as session, session.begin():
            # Unlocked discovery only: the service must lock handover first.
            row = await self.plugin(session)
            return await self.service.require_matching_report_in_session(
                session,
                principal=self.sql.installer,
                plugin_row=row,
                report_id=report.report_id,
                report_sha256=report.report_sha256,
            )

    async def validate(self) -> bool:
        async with self.sql.factory() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '2000ms'"))
            await acquire_plugin_registration_lock(
                session, self.sql.creator.tenant_id, sql_tests.PLUGIN_ID
            )
            row = await self.plugin(session, lock=True)
            return await validate_generated_approval_in_session(session, plugin_row=row)


@pytest.fixture
async def context(sql_context: sql_tests.Context) -> Context:
    await sql_context.plan()
    pending = await sql_context.confirm()
    return Context(
        sql_context,
        GeneratedPluginReviewService(
            confirmation=sql_context.service,
            clock=lambda: sql_context.clock[0],
        ),
        pending,
    )


@dataclass
class Writes:
    phases: list[str] = field(default_factory=list)
    transaction_ids: list[int] = field(default_factory=list)
    sqlstates: list[str] = field(default_factory=list)
    approval_visible_at_audit: list[bool] = field(default_factory=list)


@asynccontextmanager
async def _observe_writes(
    c: Context,
    *,
    fail_audit: Literal["before", "after"] | None = None,
) -> AsyncIterator[Writes]:
    """Observe real SQL; faults execute on the product connection inside its BEGIN."""
    engine = cast(AsyncEngine, c.sql.factory.kw["bind"])
    observed = Writes()

    def phase(execution: DefaultExecutionContext) -> str | None:
        compiled = execution.compiled
        if compiled is None or not isinstance(compiled.statement, Insert | Update):
            return None
        if cast(Table, compiled.statement.table).name != AdminResourceRow.__tablename__:
            return None
        for parameters in execution.compiled_parameters:
            tenant = parameters.get("tenant_id", parameters.get("tenant_id_1"))
            if tenant != c.sql.creator.tenant_id:
                continue
            kind = parameters.get("kind", parameters.get("kind_1"))
            identity = parameters.get("resource_id", parameters.get("resource_id_1", ""))
            if kind == "plugin":
                return "plugin"
            if kind == "audit":
                return "audit"
            if (
                kind == HANDOVER_KIND
                and isinstance(identity, str)
                and identity.startswith("ga-review-")
            ):
                return "proof"
        return None

    def inspect_audit(connection: Connection) -> None:
        payload = connection.execute(
            select(AdminResourceRow.payload).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == "plugin",
                AdminResourceRow.resource_id == sql_tests.PLUGIN_ID,
            )
        ).scalar_one()
        proof_id = payload.get(GENERATED_REVIEW_APPROVAL_MARKER)
        proof = connection.execute(
            select(AdminResourceRow.payload).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == HANDOVER_KIND,
                AdminResourceRow.resource_id == proof_id,
            )
        ).scalar_one_or_none()
        package = payload["package_metadata"]
        assert isinstance(package, dict)
        observed.approval_visible_at_audit.append(
            package["approval_state"] == "approved"
            and proof is not None
            and proof["registration_id"] is not None
        )

    def before(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        execution: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        if phase(execution) == "audit" and fail_audit == "before":
            inspect_audit(connection)
            connection.exec_driver_sql("SELECT 1 / 0")

    def after(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        execution: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        current = phase(execution)
        if current is None:
            return
        observed.phases.append(current)
        observed.transaction_ids.append(
            cast(int, connection.exec_driver_sql("SELECT txid_current()").scalar_one())
        )
        if current == "audit":
            inspect_audit(connection)
            if fail_audit == "after":
                connection.exec_driver_sql("SELECT 1 / 0")

    def failed(error: ExceptionContext) -> None:
        state = getattr(error.original_exception, "sqlstate", None)
        if isinstance(state, str):
            observed.sqlstates.append(state)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    event.listen(engine.sync_engine, "handle_error", failed)
    try:
        yield observed
    finally:
        event.remove(engine.sync_engine, "handle_error", failed)
        event.remove(engine.sync_engine, "after_cursor_execute", after)
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@asynccontextmanager
async def _waiter[T](
    c: Context,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[], Awaitable[T]],
) -> AsyncIterator[tuple[asyncio.Task[T], asyncio.Future[int]]]:
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    transaction = c.sql.storage._transaction

    @asynccontextmanager
    async def observed() -> AsyncIterator[AsyncSession]:
        async with transaction() as session:
            actual_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert type(actual_pid) is int
            if not pid.done():
                pid.set_result(actual_pid)
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(c.sql.storage, "_transaction", observed)

        async def run() -> T:
            return await call()

        task = asyncio.create_task(run())
        try:
            yield task, pid
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_actual_scan_get_and_approval_commit_one_proof_and_audit(
    context: Context,
    forbid_execution: None,
) -> None:
    c = context
    original = await c.sql.rows()
    consumed = await c.sql.grant()
    snapshot = c.sql.saved(consumed)
    report = await c.scan()
    assert report.report.coverage == "complete"
    assert report.subject.receipt_id == c.pending.receipt_id
    c.service = GeneratedPluginReviewService(
        confirmation=c.sql.service, clock=lambda: c.sql.clock[0]
    )
    assert await c.get(report) == report
    assert await c.matching(report) == report
    assert await c.scan() == report
    async with c.sql.factory() as session:
        row = await c.plugin(session)
        assert row.id == report.subject.registration_id
        stored = await session.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == HANDOVER_KIND,
                AdminResourceRow.resource_id == report.report_id,
            )
        )
        assert stored is not None and stored.payload["record_type"] == "scan_report"
        assert stored.payload["report_sha256"] == report.report_sha256
    async with _observe_writes(c) as writes:
        await c.approve(report)
    assert set(writes.phases) == {"plugin", "proof", "audit"} and len(writes.phases) == 3
    assert len(set(writes.transaction_ids)) == 1
    assert writes.approval_visible_at_audit == [True]
    assert await c.validate() is True
    after = await c.sql.rows()
    assert len(after) == len(original) + 3
    new = [row for row in after if row[2] not in {old[2] for old in original}]
    assert sum(row[1] == "audit" for row in new) == 1
    proof = next(row for row in new if row[2].startswith("ga-review-") and row[1] == HANDOVER_KIND)
    assert report.report_id in proof[3] and report.report_sha256 in proof[3]
    assert await c.sql.grant() == consumed and c.sql.saved(consumed) == snapshot
    assert (
        await c.sql.storage.get_receipt(c.sql.installer, c.sql.run_id, c.pending.handover_id)
        == c.pending
    )
    await c.approve(report)
    assert await c.sql.rows() == after
    c.sql.assert_package()


@pytest.mark.parametrize("workspace", [False, True], ids=["artifact", "workspace"])
async def test_actual_expired_delivery_still_allows_review(
    sql_context: sql_tests.Context,
    workspace: bool,
) -> None:
    await sql_context.plan(workspace=workspace)
    pending = await sql_context.confirm()
    sql_context.clock[0] = pending.expires_at + timedelta(days=2)
    c = Context(
        sql_context,
        GeneratedPluginReviewService(
            confirmation=sql_context.service,
            clock=lambda: sql_context.clock[0],
        ),
        pending,
    )
    report = await c.scan()
    assert await c.get(report) == report
    await c.approve(report)
    assert await c.validate() is True


async def test_actual_concurrent_scan_and_approval_are_idempotent(context: Context) -> None:
    c = context
    one, two = await asyncio.wait_for(asyncio.gather(c.scan(), c.scan()), timeout=20)
    assert one == two
    await asyncio.wait_for(asyncio.gather(c.approve(one), c.approve(two)), timeout=20)
    assert len(await c.sql.rows()) == 7
    assert await c.validate() is True


@pytest.mark.parametrize("bad", ["unknown", "hash", "foreign", "tampered"])
async def test_actual_wrong_report_cannot_write_approval(context: Context, bad: str) -> None:
    c = context
    report = await c.scan()
    report_id, sha = report.report_id, report.report_sha256
    principal = c.sql.installer
    if bad == "unknown":
        report_id = "gs-" + "0" * 64
    elif bad == "hash":
        sha = "0" * 64
    elif bad == "foreign":
        principal = AuthenticatedPrincipal(principal.user_id, uuid4(), principal.role)
        with pytest.raises(NotFound):
            await c.service.get_report(principal, c.sql.run_id, c.pending.handover_id, report_id)
        # The approval method returns None for a tenant with no such plugin, to
        # allow the legacy route's tenant-scoped lookup. It must create no proof.
        assert (
            await c.service.approve_generated(
                principal,
                sql_tests.PLUGIN_ID,
                report_id=report_id,
                report_sha256=sha,
                reason="reviewed",
            )
            is None
        )
    else:
        async with c.sql.factory() as session, session.begin():
            row = await session.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                    AdminResourceRow.resource_id == report_id,
                    AdminResourceRow.kind == HANDOVER_KIND,
                )
                .with_for_update()
            )
            assert row is not None
            changed = deepcopy(row.payload)
            body = changed["body"]
            assert isinstance(body, dict)
            body["coverage"] = "incomplete"
            row.payload = changed
    before = await c.sql.rows()
    if bad != "foreign":
        with pytest.raises(Conflict if bad != "tampered" else StorageUnavailable):
            await c.service.approve_generated(
                principal,
                sql_tests.PLUGIN_ID,
                report_id=report_id,
                report_sha256=sha,
                reason="reviewed",
            )
    assert await c.sql.rows() == before


@pytest.mark.parametrize("changed", ["registration", "configuration", "source"])
async def test_actual_rebound_registration_or_source_invalidates_review(
    context: Context,
    changed: str,
) -> None:
    c = context
    report = await c.scan()
    async with c.sql.factory() as session, session.begin():
        row = await c.plugin(session, lock=True)
        if changed == "registration":
            payload = deepcopy(row.payload)
            await session.execute(delete(AdminResourceRow).where(AdminResourceRow.id == row.id))
            session.add(
                AdminResourceRow(
                    id=uuid4(),
                    tenant_id=c.sql.creator.tenant_id,
                    kind="plugin",
                    resource_id=sql_tests.PLUGIN_ID,
                    payload=payload,
                )
            )
        elif changed == "configuration":
            row.payload = {
                **row.payload,
                "capabilities": [{"id": "rebound.read", "permission_class": "file:read"}],
            }
        else:
            artifact = await session.scalar(
                select(RunArtifactRow)
                .where(
                    RunArtifactRow.tenant_id == c.sql.creator.tenant_id,
                    RunArtifactRow.id == c.sql.artifact_id,
                )
                .with_for_update()
            )
            assert artifact is not None
            artifact.producer = "changed-producer"
            artifact.payload = {**artifact.payload, "producer": "changed-producer"}
    before = await c.sql.rows()
    for action in (c.get, c.matching, c.approve):
        with pytest.raises(PermissionDenied if changed == "source" else Conflict):
            await action(report)
    assert await c.sql.rows() == before


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_actual_audit_sql_failure_rolls_back_plugin_and_proof(
    context: Context,
    phase: Literal["before", "after"],
) -> None:
    c = context
    report = await c.scan()
    before = await c.sql.rows()
    grant = await c.sql.grant()
    async with _observe_writes(c, fail_audit=phase) as writes:
        with pytest.raises(StorageUnavailable):
            await c.approve(report)
    assert writes.sqlstates == ["22012"]
    assert writes.approval_visible_at_audit == [True]
    assert set(writes.phases) == (
        {"proof", "plugin"} if phase == "before" else {"proof", "plugin", "audit"}
    )
    assert len(set(writes.transaction_ids)) == 1
    assert await c.sql.rows() == before
    assert await c.sql.grant() == grant
    await c.approve(report)
    assert await c.validate() is True
    assert len(await c.sql.rows()) == len(before) + 2


@pytest.mark.parametrize("revoked", ["grant", "key"])
@pytest.mark.parametrize("approved", [False, True], ids=["pending", "approved"])
async def test_actual_revocation_blocks_review_and_existing_approval(
    context: Context,
    revoked: str,
    approved: bool,
) -> None:
    c = context
    report = await c.scan()
    if approved:
        await c.approve(report)
    async with c.sql.factory() as session, session.begin():
        if revoked == "grant":
            grant = await c.sql.grant()
            await c.sql.grants.revoke(session, c.sql.creator, grant_id=grant.grant_id)
        else:
            key = await session.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                    AdminResourceRow.kind == "plugin_signing_key",
                    AdminResourceRow.resource_id == sql_tests.KEY_ID,
                )
                .with_for_update()
            )
            assert key is not None
            key.payload = {**key.payload, "trusted": False}
    before = await c.sql.rows()
    error = Conflict if revoked == "grant" else StorageUnavailable
    with pytest.raises(error):
        await c.get(report)
    with pytest.raises(error):
        await c.approve(report)
    if approved:
        with pytest.raises(error):
            await c.validate()
    assert await c.sql.rows() == before


@pytest.mark.parametrize("change", ["registration", "configuration", "grant", "creator", "source"])
async def test_actual_registration_wait_rechecks_bindings(
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    c = context
    report = await c.scan()
    async with c.sql.factory() as holder, holder.begin():
        await acquire_plugin_registration_lock(holder, c.sql.creator.tenant_id, sql_tests.PLUGIN_ID)
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        assert type(holder_pid) is int
        async with _waiter(c, monkeypatch, lambda: c.approve(report)) as (task, future):
            pid = await asyncio.wait_for(future, timeout=10)
            await sql_tests._blocked(c.sql, pid, holder_pid, advisory=True)
            assert not task.done()
            if change == "grant":
                grant = await c.sql.grant()
                await c.sql.grants.revoke(holder, c.sql.creator, grant_id=grant.grant_id)
            elif change == "creator":
                await holder.execute(
                    update(UserRow).where(UserRow.id == c.sql.creator.user_id).values(disabled=True)
                )
            elif change == "source":
                artifact = await holder.scalar(
                    select(RunArtifactRow)
                    .where(RunArtifactRow.id == c.sql.artifact_id)
                    .with_for_update()
                )
                assert artifact is not None
                artifact.payload = {**artifact.payload, "producer": "changed-after-wait"}
            else:
                row = await c.plugin(holder, lock=True)
                if change == "registration":
                    payload = deepcopy(row.payload)
                    await holder.execute(
                        delete(AdminResourceRow).where(AdminResourceRow.id == row.id)
                    )
                    holder.add(
                        AdminResourceRow(
                            id=uuid4(),
                            tenant_id=c.sql.creator.tenant_id,
                            kind="plugin",
                            resource_id=sql_tests.PLUGIN_ID,
                            payload=payload,
                        )
                    )
                else:
                    row.payload = {**row.payload, "timeout_seconds": 42}
            await holder.commit()
            before = await c.sql.rows()
            with pytest.raises(PermissionDenied if change in {"creator", "source"} else Conflict):
                await asyncio.wait_for(task, timeout=10)
    assert await c.sql.rows() == before


@pytest.mark.parametrize("target", ["key-revoked", "key-expired", "approver-disabled"])
async def test_actual_final_key_or_user_wait_rechecks_authority(
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    c = context
    report = await c.scan()
    async with c.sql.factory() as holder, holder.begin():
        key: AdminResourceRow | None = None
        user: UserRow | None = None
        if target == "approver-disabled":
            user = await holder.scalar(
                select(UserRow).where(UserRow.id == c.sql.installer.user_id).with_for_update()
            )
            assert user is not None
        else:
            key = await holder.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                    AdminResourceRow.kind == "plugin_signing_key",
                    AdminResourceRow.resource_id == sql_tests.KEY_ID,
                )
                .with_for_update()
            )
            assert key is not None
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        assert type(holder_pid) is int
        async with _waiter(c, monkeypatch, lambda: c.approve(report)) as (task, future):
            pid = await asyncio.wait_for(future, timeout=10)
            await sql_tests._blocked(c.sql, pid, holder_pid, advisory=False)
            assert not task.done()
            if user is not None:
                user.disabled = True
            else:
                assert key is not None
                key.payload = {
                    **key.payload,
                    **(
                        {"trusted": False}
                        if target == "key-revoked"
                        else {"not_after": c.sql.clock[0].isoformat()}
                    ),
                }
            await holder.commit()
            before = await c.sql.rows()
            error = (
                PermissionDenied
                if user is not None
                else StorageUnavailable
                if target == "key-revoked"
                else Conflict
            )
            with pytest.raises(error):
                await asyncio.wait_for(task, timeout=10)
    assert await c.sql.rows() == before


async def test_actual_runtime_validator_does_not_reverse_lock_handover(context: Context) -> None:
    c = context
    report = await c.scan()
    await c.approve(report)
    async with c.sql.factory() as holder, holder.begin():
        locked = await holder.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == HANDOVER_KIND,
                AdminResourceRow.resource_id == c.pending.handover_id,
            )
            .with_for_update()
        )
        assert locked is not None
        # Registration/plugin locks are taken by another connection. Acquiring
        # handover FOR UPDATE there would fail lock_timeout while this lock lives.
        assert await asyncio.wait_for(c.validate(), timeout=5) is True


async def test_actual_revocation_between_scan_transactions_prevents_report(
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    released, resume = asyncio.Event(), asyncio.Event()
    transaction = c.sql.storage._transaction
    transactions = 0

    @asynccontextmanager
    async def pause_between_transactions() -> AsyncIterator[AsyncSession]:
        nonlocal transactions
        async with transaction() as session:
            yield session
        transactions += 1
        if transactions == 1:
            # Pause after the actual first BEGIN commits, before parsing starts.
            released.set()
            await resume.wait()

    with monkeypatch.context() as patch:
        patch.setattr(c.sql.storage, "_transaction", pause_between_transactions)
        task = asyncio.create_task(c.scan())
        try:
            await asyncio.wait_for(released.wait(), timeout=10)
            async with c.sql.factory() as holder, holder.begin():
                await holder.execute(text("SET LOCAL lock_timeout = '2000ms'"))
                # These real locks must have been released by scan's first phase.
                await acquire_plugin_registration_lock(
                    holder, c.sql.creator.tenant_id, sql_tests.PLUGIN_ID
                )
                await c.plugin(holder, lock=True)
                grant = await c.sql.grant()
                await c.sql.grants.revoke(holder, c.sql.creator, grant_id=grant.grant_id)
            before = await c.sql.rows()
            resume.set()
            with pytest.raises(Conflict):
                await asyncio.wait_for(task, timeout=10)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await c.sql.rows() == before
