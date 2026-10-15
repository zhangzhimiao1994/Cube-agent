"""Actual SQL grants in the explicit isolated testDB; no public/installation credit.

The shared integration fixture owns database migration/cleanup. Without both
the dedicated URL and handover isolation opt-in these cases do not access DB.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, RunRow, TenantRow, UserRow
from agent_hub.plugins.handover_grants import DurableSourceGrantService, SourceGrant
from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, StoredSourceSnapshot
from agent_hub.plugins.handover_sources import FrozenSourceSnapshot, SnapshotFile, _canonical
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    AuthorizedSource,
    Conflict,
    PlanData,
    WorkspaceSource,
    _digest,
    _HandoverRecord,
    _plan_fingerprint,
    acquire_plugin_registration_lock,
)
from agent_hub.runtime.contracts import Artifact

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED") != "1"
        or not os.environ.get("AGENT_HUB_TEST_DATABASE_URL"),
        reason="requires explicit isolated testDB URL and opt-in; no SQL proved locally",
    ),
]


@dataclass
class Context:
    factory: async_sessionmaker[AsyncSession]
    creator: AuthenticatedPrincipal
    installer: AuthenticatedPrincipal
    run_id: UUID
    plan_id: str
    clock: list[datetime]
    service: DurableSourceGrantService
    store: FrozenSourceSnapshotStore

    async def snapshot(self, *, artifact: bool = False) -> StoredSourceSnapshot:
        raw = {
            "main.py": b"raise RuntimeError('sql-grant-source-never-execute')\n",
            "plugin.json": b'{"id":"sql-grant-tool","version":"1.0.0"}\n',
        }
        selector: ArtifactSource | WorkspaceSource
        artifact_hash = None
        if artifact:
            published = Artifact(
                id=uuid4(),
                type="workspace_bundle",
                producer="sql-test",
                content={
                    "workspace_bundle": {
                        "files": {name: data.decode() for name, data in raw.items()}
                    }
                },
            )
            payload = published.to_payload()
            async with self.factory() as session, session.begin():
                session.add(
                    RunArtifactRow(
                        id=published.id,
                        tenant_id=self.creator.tenant_id,
                        run_id=self.run_id,
                        type=published.type,
                        producer=published.producer,
                        content_sha256=published.content_sha256,
                        payload=payload,
                    )
                )
            selector = ArtifactSource(artifact_id=str(published.id))
            artifact_hash = hashlib.sha256(_canonical(payload)).hexdigest()
        else:
            selector = WorkspaceSource(
                project_id="project", session_id="session", paths=tuple(sorted(raw))
            )
        files = tuple(
            SnapshotFile(name, raw[name], len(raw[name]), hashlib.sha256(raw[name]).hexdigest())
            for name in sorted(raw)
        )
        source_hash = hashlib.sha256(
            _canonical(
                {
                    "schema_version": 1,
                    "domain": "generated-handover-source",
                    "tenant_id": str(self.creator.tenant_id),
                    "run_id": str(self.run_id),
                    "actor_id": str(self.creator.user_id),
                    "source": selector.model_dump(mode="json"),
                    "artifact_sha256": artifact_hash,
                    "files": [
                        {"path": f.path, "size_bytes": f.size_bytes, "sha256": f.sha256}
                        for f in files
                    ],
                }
            )
        ).hexdigest()
        frozen = FrozenSourceSnapshot(
            AuthorizedSource(
                tenant_id=self.creator.tenant_id,
                run_id=self.run_id,
                source=selector,
                source_manifest_sha256=source_hash,
            ),
            files,
            "sql-grant-tool",
            "1.0.0",
            sum(f.size_bytes for f in files),
        )
        reference = self.store.publish(frozen, creator_id=self.creator.user_id)
        return self.store.read(
            tenant_id=self.creator.tenant_id,
            snapshot_id=reference.snapshot_id,
            blob_sha256=reference.blob_sha256,
            source_manifest_sha256=reference.source_manifest_sha256,
        )

    async def publish(self, stored: StoredSourceSnapshot) -> SourceGrant:
        async with self.factory() as session, session.begin():
            return await self.service.publish(
                session,
                self.creator,
                stored,
                plan_id=self.plan_id,
                plan_version=1,
                expires_at=self.clock[0] + timedelta(hours=1),
            )

    async def rows(self) -> tuple[dict[str, object], ...]:
        async with self.factory() as session:
            records = (
                await session.scalars(
                    select(AdminResourceRow)
                    .where(
                        AdminResourceRow.tenant_id == self.creator.tenant_id,
                    )
                    .order_by(AdminResourceRow.resource_id)
                )
            ).all()
            return tuple(dict(row.payload) for row in records)

    def receipt(self, grant: SourceGrant) -> tuple[AdminResourceRow, AdminResourceRow, str]:
        binding = grant.binding
        plan = PlanData(plugin_id=binding.plugin_id, plugin_version=binding.plugin_version)
        fingerprint = _plan_fingerprint(
            binding.run_id, binding.source, binding.source_manifest_sha256, plan
        )
        receipt_id = "gr-" + _digest("receipt", binding.plan_id)
        audit_id = "ga-" + _digest("audit", binding.plan_id)
        record = _HandoverRecord(
            tenant_id=binding.tenant_id,
            handover_id=binding.plan_id,
            run_id=binding.run_id,
            creator_id=binding.creator_id,
            installer_id=self.installer.user_id,
            version=2,
            status="pending",
            created_at=self.clock[0],
            updated_at=self.clock[0],
            expires_at=binding.expires_at,
            source=binding.source,
            source_manifest_sha256=binding.source_manifest_sha256,
            plugin_id=binding.plugin_id,
            plugin_version=binding.plugin_version,
            plan_data=plan,
            plan_hash=_digest(
                "plan", {"handover_id": binding.plan_id, "request_fingerprint": fingerprint}
            ),
            request_fingerprint=fingerprint,
            plan_key_digest=binding.plan_id[3:],
            signature_payload_sha256="4" * 64,
            archive_sha256="5" * 64,
            confirm_key_digest="6" * 64,
            confirm_request_sha256="7" * 64,
            key_id="key",
            verified_public_key_sha256="8" * 64,
            artifact_ref="internal-artifact",
            receipt_id=receipt_id,
            audit_id=audit_id,
            committed_at=self.clock[0],
            result="blocked_pending_approval",
        )
        return (
            AdminResourceRow(
                id=uuid4(),
                tenant_id=binding.tenant_id,
                kind="plugin_handover",
                resource_id=binding.plan_id,
                payload=record.model_dump(mode="json"),
            ),
            AdminResourceRow(
                id=uuid4(),
                tenant_id=binding.tenant_id,
                kind="audit",
                resource_id=audit_id,
                payload={"action": "grant-test-pending", "run_id": str(binding.run_id)},
            ),
            receipt_id,
        )


@pytest.fixture
async def context(
    auth_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[Context]:
    factory = auth_session_factory
    creator = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    installer = AuthenticatedPrincipal(uuid4(), creator.tenant_id, Role.ADMIN)
    clock = [datetime.now(UTC)]
    run_id = uuid4()
    async with factory() as session, session.begin():
        session.add(TenantRow(id=creator.tenant_id, slug="grant-" + uuid4().hex, name="Grant test"))
        await session.flush()
        session.add_all(
            [
                UserRow(
                    id=creator.user_id,
                    tenant_id=creator.tenant_id,
                    username="creator",
                    role="operator",
                    disabled=False,
                ),
                UserRow(
                    id=installer.user_id,
                    tenant_id=creator.tenant_id,
                    username="installer",
                    role="admin",
                    disabled=False,
                ),
                RunRow(
                    id=run_id,
                    tenant_id=creator.tenant_id,
                    actor_id=creator.user_id,
                    actor_role="operator",
                    request="grant SQL only",
                    status="completed",
                    version=1,
                    routing_decision={"project_id": "project", "workspace_session_id": "session"},
                ),
            ]
        )
    value = Context(
        factory,
        creator,
        installer,
        run_id,
        "gh-" + uuid4().hex * 2,
        clock,
        DurableSourceGrantService(clock=lambda: clock[0]),
        FrozenSourceSnapshotStore(package_root=tmp_path / "packages"),
    )
    try:
        yield value
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(AdminResourceRow).where(AdminResourceRow.tenant_id == creator.tenant_id)
            )


@pytest.mark.parametrize("artifact", [False, True])
async def test_actual_restart_grant_and_canonical_snapshot(
    context: Context, artifact: bool
) -> None:
    stored = await context.snapshot(artifact=artifact)
    grant = await context.publish(stored)
    assert (grant.binding.artifact is not None) is artifact
    service = DurableSourceGrantService(clock=lambda: context.clock[0])
    async with context.factory() as session, session.begin():
        assert await service.verify(session, context.installer, expected=grant.binding) == grant
    assert (
        context.store.read(
            tenant_id=context.creator.tenant_id,
            snapshot_id=stored.reference.snapshot_id,
            blob_sha256=stored.reference.blob_sha256,
            source_manifest_sha256=stored.reference.source_manifest_sha256,
        )
        == stored
    )


async def test_actual_publish_rollback_preserves_shared_blob(context: Context) -> None:
    stored = await context.snapshot()
    with pytest.raises(RuntimeError, match="known rollback"):
        async with context.factory() as session, session.begin():
            await context.service.publish(
                session,
                context.creator,
                stored,
                plan_id=context.plan_id,
                plan_version=1,
                expires_at=context.clock[0] + timedelta(hours=1),
            )
            raise RuntimeError("known rollback")
    assert await context.rows() == ()
    assert (await context.publish(stored)).binding.snapshot_id == stored.reference.snapshot_id


async def test_actual_identity_map_refresh_after_demotion(context: Context) -> None:
    grant = await context.publish(await context.snapshot())
    async with context.factory() as stale, stale.begin():
        cached = await stale.scalar(select(UserRow).where(UserRow.id == context.creator.user_id))
        assert cached is not None and cached.role == "operator"
        async with context.factory() as editor, editor.begin():
            await editor.execute(
                update(UserRow).where(UserRow.id == context.creator.user_id).values(role="viewer")
            )
        before = await context.rows()
        with pytest.raises(PermissionDenied):
            await context.service.verify(stale, context.installer, expected=grant.binding)
        assert cached.role == "viewer"
    assert await context.rows() == before


@pytest.mark.parametrize("change", ["disabled", "actor", "association", "artifact"])
async def test_actual_authority_and_artifact_changes_are_not_dto_proof(
    context: Context, change: str
) -> None:
    grant = await context.publish(await context.snapshot(artifact=change == "artifact"))
    before = await context.rows()
    async with context.factory() as session, session.begin():
        if change == "disabled":
            await session.execute(
                update(UserRow).where(UserRow.id == context.creator.user_id).values(disabled=True)
            )
        elif change == "actor":
            await session.execute(
                update(RunRow).where(RunRow.id == context.run_id).values(actor_id=uuid4())
            )
        elif change == "association":
            await session.execute(
                update(RunRow)
                .where(RunRow.id == context.run_id)
                .values(
                    routing_decision={"project_id": "project", "workspace_session_id": "changed"}
                )
            )
        else:
            await session.execute(
                update(RunArtifactRow)
                .where(RunArtifactRow.run_id == context.run_id)
                .values(content_sha256="f" * 64)
            )
    async with context.factory() as session, session.begin():
        with pytest.raises(PermissionDenied):
            await context.service.verify(session, context.installer, expected=grant.binding)
    assert await context.rows() == before


@pytest.mark.parametrize("change", ["expiry", "demotion", "artifact"])
async def test_actual_registration_wait_revalidates_after_pg_blocking_evidence(
    context: Context, change: str
) -> None:
    grant = await context.publish(await context.snapshot(artifact=change == "artifact"))
    before = await context.rows()
    waiter_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def waiter() -> None:
        async with context.factory() as session, session.begin():
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert type(pid) is int
            waiter_pid.set_result(pid)
            with pytest.raises(Conflict if change == "expiry" else PermissionDenied):
                await context.service.verify(session, context.installer, expected=grant.binding)

    async with context.factory() as holder, holder.begin():
        await acquire_plugin_registration_lock(
            holder, grant.binding.tenant_id, grant.binding.plugin_id
        )
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(waiter())
        try:
            pid = await asyncio.wait_for(waiter_pid, timeout=10)
            async with context.factory() as observer:
                async with asyncio.timeout(10):
                    while True:
                        blockers = await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                        )
                        advisory = await observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND NOT granted"
                            ),
                            {"pid": pid},
                        )
                        if holder_pid in blockers and advisory:
                            break
                        await asyncio.sleep(0.01)
            assert not task.done()
            if change == "expiry":
                context.clock[0] = grant.binding.expires_at
            else:
                async with context.factory() as editor, editor.begin():
                    if change == "demotion":
                        await editor.execute(
                            update(UserRow)
                            .where(UserRow.id == context.creator.user_id)
                            .values(role="viewer")
                        )
                    else:
                        await editor.execute(
                            update(RunArtifactRow)
                            .where(RunArtifactRow.run_id == context.run_id)
                            .values(content_sha256="f" * 64)
                        )
            await holder.commit()
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert await context.rows() == before


async def test_actual_receipt_audit_consume_rollback_and_retry(context: Context) -> None:
    stored = await context.snapshot()
    grant = await context.publish(stored)
    before = await context.rows()
    with pytest.raises(RuntimeError, match="audit failure"):
        async with context.factory() as session, session.begin():
            plan, audit, receipt_id = context.receipt(grant)
            session.add_all([plan, audit])
            await session.flush()
            consumed = await context.service.consume(
                session, context.installer, expected=grant.binding, receipt_id=receipt_id
            )
            assert consumed.state == "consumed"
            raise RuntimeError("audit failure")
    assert await context.rows() == before
    async with context.factory() as session, session.begin():
        plan, audit, receipt_id = context.receipt(grant)
        session.add_all([plan, audit])
        await session.flush()
        consumed = await context.service.consume(
            session, context.installer, expected=grant.binding, receipt_id=receipt_id
        )
    async with context.factory() as session, session.begin():
        assert (
            await context.service.consume(
                session, context.installer, expected=grant.binding, receipt_id=receipt_id
            )
            == consumed
        )
    assert len(await context.rows()) == 3
    assert (
        context.store.read(
            tenant_id=context.creator.tenant_id,
            snapshot_id=stored.reference.snapshot_id,
            blob_sha256=stored.reference.blob_sha256,
            source_manifest_sha256=stored.reference.source_manifest_sha256,
        )
        == stored
    )


async def test_actual_revoke_does_not_wait_for_plan_lock(context: Context) -> None:
    grant = await context.publish(await context.snapshot())
    async with context.factory() as owner, owner.begin():
        plan, _, _ = context.receipt(grant)
        owner.add(plan)
        await owner.flush()
        async with context.factory() as revoker, revoker.begin():
            revoked = await asyncio.wait_for(
                context.service.revoke(revoker, context.creator, grant_id=grant.grant_id), timeout=5
            )
            assert revoked.state == "revoked"
