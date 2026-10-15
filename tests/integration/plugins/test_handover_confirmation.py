"""Isolated PostgreSQL confirmation bridge, not public/ordinary plugin acceptance.

No SQL doubles: source authorization uses RunRepository, all services use actual
BEGIN/row/advisory locks. PID observation and failure triggers are test-only.
Without BOTH isolation opt-in and an explicit URL, no fixture accesses a DB.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request

from agent_hub.api.routers.admin import (
    _plugin_archive_manifest_from_archive,
    _plugin_archive_signature_files,
    _plugin_signature_payload,
)
from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, RunRow, TenantRow, UserRow
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.plugins.handover_confirmation import GeneratedPluginConfirmationService
from agent_hub.plugins.handover_grants import DurableSourceGrantService, SourceGrant
from agent_hub.plugins.handover_plans import GeneratedPluginPlanService
from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, StoredSourceSnapshot
from agent_hub.plugins.handover_sources import HandoverSourceService
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    Conflict,
    HandoverReceipt,
    NotFound,
    PluginHandoverStorage,
    StorageUnavailable,
    WorkspaceSource,
    acquire_plugin_registration_lock,
)
from agent_hub.plugins.handover_verification import PackageVerificationError
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime.contracts import Artifact

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED") != "1"
        or not os.environ.get("AGENT_HUB_TEST_DATABASE_URL"),
        reason="requires explicit isolated testDB URL and opt-in; skip is not SQL proof",
    ),
]

PLUGIN_ID = "sql-confirm-tool"
KEY_ID = "sql-confirm-key"
CODE = b"raise RuntimeError('sql-confirm-candidate-must-never-execute')\n"
MEMBERS = {"main.py": CODE, "data/info.txt": b"frozen source bytes\n"}


def _manifest() -> dict[str, object]:
    return {
        "id": PLUGIN_ID,
        "name": "SQL confirmation fixture",
        "version": "1.0.0",
        "enabled": False,
        "package": {
            "kind": "adapter_package",
            "package_version": "1.0.0",
            "adapter_id": PLUGIN_ID,
            "sdk_api_version": "1.0",
            "runtime": "python",
            "entrypoint": "main.py",
            "isolation": "local_process",
            "install_mode": "scan_only",
        },
    }


def _archive(manifest: dict[str, object]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("plugin.json", json.dumps(manifest).encode())
        for name, content in MEMBERS.items():
            zipped.writestr(name, content)
    return buffer.getvalue()


def _signed(private: Ed25519PrivateKey) -> bytes:
    manifest = _manifest()
    package = cast(dict[str, object], manifest["package"])
    signature = {"algorithm": "ed25519", "key_id": KEY_ID, "value": "A" * 86}
    package["signature"] = signature
    unsigned = _archive(manifest)
    payload = _plugin_signature_payload(_plugin_archive_manifest_from_archive(unsigned), unsigned)
    signature["value"] = base64.urlsafe_b64encode(private.sign(payload)).rstrip(b"=").decode()
    return _archive(manifest)


@dataclass
class Context:
    factory: async_sessionmaker[AsyncSession]
    creator: AuthenticatedPrincipal
    installer: AuthenticatedPrincipal
    run_id: UUID
    artifact_id: UUID
    clock: list[datetime]
    package_root: Path
    request: Request
    archive: bytes
    repository: RunRepository
    workspaces: ProjectWorkspaceStore
    snapshots: FrozenSourceSnapshotStore
    grants: DurableSourceGrantService
    storage: PluginHandoverStorage
    plans: GeneratedPluginPlanService
    service: GeneratedPluginConfirmationService
    receipt: HandoverReceipt | None = None

    async def plan(self, *, workspace: bool = False, key: str = "source-plan") -> HandoverReceipt:
        source: WorkspaceSource | ArtifactSource
        if workspace:
            source = WorkspaceSource(
                project_id="project",
                session_id="session",
                paths=tuple(sorted((*MEMBERS, "plugin.json"))),
            )
        else:
            source = ArtifactSource(artifact_id=str(self.artifact_id))
        self.receipt = await self.plans.create_plan(
            self.creator,
            self.run_id,
            source,
            idempotency_key=key,
            expires_at=self.clock[0] + timedelta(hours=1),
        )
        return self.receipt

    async def confirm(self, *, principal: AuthenticatedPrincipal | None = None) -> HandoverReceipt:
        if self.receipt is None:
            raise AssertionError("fixture must create a plan through the real service")
        return await self.service.confirm(
            self.installer if principal is None else principal,
            self.run_id,
            self.receipt.handover_id,
            1,
            self.receipt.plan_hash,
            True,
            "confirm-source",
            self.archive,
            KEY_ID,
            self.request,
        )

    async def rows(self) -> tuple[tuple[UUID, str, str, str], ...]:
        """Security outputs only; externally edited users/keys are intentionally separate."""
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(AdminResourceRow)
                    .where(
                        AdminResourceRow.tenant_id == self.creator.tenant_id,
                        AdminResourceRow.kind.in_(("plugin_handover", "plugin", "audit")),
                    )
                    .order_by(AdminResourceRow.kind, AdminResourceRow.resource_id)
                )
            ).all()
            return tuple(
                (r.id, r.kind, r.resource_id, json.dumps(r.payload, sort_keys=True)) for r in rows
            )

    async def grant(self) -> SourceGrant:
        if self.receipt is None or self.receipt.source_grant_id is None:
            raise AssertionError("actual grant-bound plan required")
        async with self.factory() as session:
            row = await session.scalar(
                select(AdminResourceRow).where(
                    AdminResourceRow.tenant_id == self.creator.tenant_id,
                    AdminResourceRow.kind == "plugin_handover",
                    AdminResourceRow.resource_id == self.receipt.source_grant_id,
                )
            )
            assert row is not None
            return SourceGrant.model_validate_json(json.dumps(row.payload))

    def saved(self, grant: SourceGrant) -> StoredSourceSnapshot:
        return self.snapshots.read(
            tenant_id=self.creator.tenant_id,
            snapshot_id=grant.binding.snapshot_id,
            blob_sha256=grant.binding.blob_sha256,
            source_manifest_sha256=grant.binding.source_manifest_sha256,
        )

    @property
    def canonical(self) -> Path:
        return (
            self.package_root
            / str(self.creator.tenant_id)
            / PLUGIN_ID
            / hashlib.sha256(self.archive).hexdigest()
        )

    def assert_package(self) -> None:
        assert (self.canonical / "main.py").read_bytes() == CODE
        assert (self.canonical / "data" / "info.txt").read_bytes() == MEMBERS["data/info.txt"]


@pytest.fixture
async def context(
    auth_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[Context]:
    factory = auth_session_factory
    owned = tmp_path / "confirmation-owned"
    owned.mkdir(mode=0o700)
    if sys.platform != "win32":
        info = owned.stat()
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
    creator = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    installer = AuthenticatedPrincipal(uuid4(), creator.tenant_id, Role.ADMIN)
    clock = [datetime.now(UTC)]
    run_id = uuid4()
    private = Ed25519PrivateKey.generate()
    unsigned_files = {**MEMBERS, "plugin.json": json.dumps(_manifest()).encode()}
    published = Artifact(
        id=uuid4(),
        type="workspace_bundle",
        producer="sql-confirm-fixture",
        content={
            "workspace_bundle": {
                "files": {name: data.decode() for name, data in unsigned_files.items()}
            }
        },
    )
    try:
        async with factory() as session, session.begin():
            session.add(
                TenantRow(
                    id=creator.tenant_id, slug="confirm-" + uuid4().hex, name="Confirmation test"
                )
            )
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
                ]
            )
            await session.flush()
            session.add(
                RunRow(
                    id=run_id,
                    tenant_id=creator.tenant_id,
                    actor_id=creator.user_id,
                    actor_role="operator",
                    request="fixture source only",
                    status="completed",
                    version=1,
                    routing_decision={"project_id": "project", "workspace_session_id": "session"},
                )
            )
            await session.flush()
            session.add_all(
                [
                    RunArtifactRow(
                        id=published.id,
                        tenant_id=creator.tenant_id,
                        run_id=run_id,
                        type=published.type,
                        producer=published.producer,
                        content_sha256=published.content_sha256,
                        payload=published.to_payload(),
                    ),
                    AdminResourceRow(
                        id=uuid4(),
                        tenant_id=creator.tenant_id,
                        kind="plugin_signing_key",
                        resource_id=KEY_ID,
                        payload={
                            "key_id": KEY_ID,
                            "algorithm": "ed25519",
                            "trusted": True,
                            "public_key": base64.urlsafe_b64encode(
                                private.public_key().public_bytes_raw()
                            )
                            .rstrip(b"=")
                            .decode(),
                        },
                    ),
                ]
            )
        repository = RunRepository(factory)
        workspaces = ProjectWorkspaceStore(owned / "workspaces")
        for name, data in unsigned_files.items():
            workspaces.write_bytes(
                creator.tenant_id, "project", "session", name, data, "text/plain"
            )
        package_root = owned / "packages"
        snapshots = FrozenSourceSnapshotStore(package_root=package_root)
        grants = DurableSourceGrantService(clock=lambda: clock[0])
        storage = PluginHandoverStorage(factory, clock=lambda: clock[0])
        sources = HandoverSourceService(
            runs=repository, workspaces=workspaces, generated_root=owned / "generated"
        )
        plans = GeneratedPluginPlanService(
            sources=sources,
            snapshots=snapshots,
            grants=grants,
            storage=storage,
            clock=lambda: clock[0],
        )
        service = GeneratedPluginConfirmationService(
            storage=storage, snapshots=snapshots, grants=grants, clock=lambda: clock[0]
        )
        app = Starlette()
        app.state.settings = type("Settings", (), {"plugin_package_store_dir": package_root})()
        request = Request({"type": "http", "app": app})
        yield Context(
            factory,
            creator,
            installer,
            run_id,
            published.id,
            clock,
            package_root,
            request,
            _signed(private),
            repository,
            workspaces,
            snapshots,
            grants,
            storage,
            plans,
            service,
        )
    finally:
        # Only this fixture's five-table namespace; do not reclaim source/package bytes.
        async with factory() as session, session.begin():
            for model in (AdminResourceRow, RunArtifactRow, RunRow, UserRow):
                await session.execute(delete(model).where(model.tenant_id == creator.tenant_id))
            await session.execute(delete(TenantRow).where(TenantRow.id == creator.tenant_id))


@pytest.mark.parametrize("workspace", [False, True], ids=["raw-artifact", "workspace"])
async def test_actual_source_plan_confirmation_atomic_pending(
    context: Context, workspace: bool
) -> None:
    c = context
    raw = await c.repository.raw_artifacts(c.creator.tenant_id, c.run_id)
    assert len(raw) == 1 and raw[0]["id"] == str(c.artifact_id)
    plan = await c.plan(workspace=workspace)
    grant = await c.grant()
    saved = c.saved(grant)
    assert plan.creator_id == c.creator.user_id and plan.installer_id is None
    assert grant.state == "active" and grant.binding.plan_id == plan.handover_id
    assert (grant.binding.artifact is None) is workspace
    assert saved.creator_id == c.creator.user_id
    assert plan.source_grant_binding_sha256 == grant.binding_sha256
    assert [r[1] for r in await c.rows()] == ["plugin_handover", "plugin_handover"]
    pending = await c.confirm()
    assert pending.status == "pending" and pending.version == 2
    assert pending.creator_id == c.creator.user_id and pending.installer_id == c.installer.user_id
    consumed = await c.grant()
    assert consumed.state == "consumed" and consumed.receipt_id == pending.receipt_id
    rows = await c.rows()
    assert len(rows) == 4
    plugin = json.loads(next(r[3] for r in rows if r[1] == "plugin"))
    audit = json.loads(next(r[3] for r in rows if r[1] == "audit"))
    assert plugin["_generated_handover_id"] == plan.handover_id
    assert plugin["enabled"] is False and plugin["status"] == "disabled"
    assert plugin["package_metadata"]["approval_state"] == "pending"
    assert plugin["package_metadata"]["activation_state"] == "blocked_pending_approval"
    assert plugin["package_metadata"]["approved_by"] is None
    assert audit["action"] == "plugin.generated_handover.pending"
    assert audit["details"]["receipt_id"] == pending.receipt_id
    assert audit["details"]["creator_id"] == str(c.creator.user_id)
    c.assert_package()
    assert c.saved(consumed) == saved


async def test_actual_restart_advanced_clock_pending_retry(context: Context) -> None:
    c = context
    await c.plan()
    first = await c.confirm()
    before = await c.rows()
    inode = c.canonical.stat().st_ino
    c.clock[0] += timedelta(minutes=10)
    c.service = GeneratedPluginConfirmationService(
        storage=PluginHandoverStorage(c.factory, clock=lambda: c.clock[0]),
        snapshots=FrozenSourceSnapshotStore(package_root=c.package_root),
        grants=DurableSourceGrantService(clock=lambda: c.clock[0]),
        clock=lambda: c.clock[0],
    )
    assert await c.confirm() == first
    assert await c.rows() == before and c.canonical.stat().st_ino == inode
    c.assert_package()


@pytest.mark.parametrize("pending", [False, True], ids=["first-confirm", "pending-retries"])
async def test_actual_concurrent_confirmation_is_single_receipt(
    context: Context, pending: bool
) -> None:
    c = context
    await c.plan()
    original = await c.confirm() if pending else None
    first, second = await asyncio.wait_for(asyncio.gather(c.confirm(), c.confirm()), timeout=20)
    assert first == second and (original is None or original == first)
    assert len(await c.rows()) == 4
    assert (await c.grant()).receipt_id == first.receipt_id


@pytest.mark.parametrize("change", ["deleted", "marker", "hash", "version"])
async def test_actual_pending_retry_requires_exact_live_plugin(
    context: Context, change: str
) -> None:
    c = context
    await c.plan()
    pending = await c.confirm()
    async with c.factory() as session, session.begin():
        row = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == c.creator.tenant_id,
                AdminResourceRow.kind == "plugin",
                AdminResourceRow.resource_id == PLUGIN_ID,
            )
            .with_for_update()
        )
        assert row is not None
        if change == "deleted":
            await session.delete(row)
        else:
            field = {
                "marker": "_generated_handover_id",
                "hash": "content_sha256",
                "version": "version",
            }[change]
            row.payload = {**row.payload, field: "different"}
    before = await c.rows()
    with pytest.raises(Conflict, match="historical handover receipt"):
        await c.confirm()
    assert await c.rows() == before
    assert await c.storage.get_receipt(c.installer, c.run_id, pending.handover_id) == pending
    c.assert_package()


async def _blocked(c: Context, pid: int, holder_pid: int, *, advisory: bool) -> None:
    async with c.factory() as observer, asyncio.timeout(10):
        while True:
            blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pid})
            ungranted = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE pid=:pid AND NOT granted "
                    "AND locktype=:kind"
                ),
                {"pid": pid, "kind": "advisory" if advisory else "transactionid"},
            )
            if holder_pid in blockers and ungranted:
                return
            await asyncio.sleep(0.01)


@asynccontextmanager
async def _observed_waiter(
    c: Context,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[asyncio.Task[HandoverReceipt], asyncio.Future[int]]]:
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    original = c.storage._transaction

    @asynccontextmanager
    async def observed() -> AsyncIterator[AsyncSession]:
        # Wrap the actual product BEGIN only to observe its real backend PID.
        async with original() as session:
            actual_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert type(actual_pid) is int
            pid.set_result(actual_pid)
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(c.storage, "_transaction", observed)
        task = asyncio.create_task(c.confirm())
        try:
            yield task, pid
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "change",
    [
        "creator-disabled",
        "creator-demoted",
        "installer-disabled",
        "key-revoked",
        "key-expired",
        "plan-expired-user",
        "plan-expired-key",
    ],
)
async def test_actual_row_wait_rechecks_current_user_key_and_expiry(
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    c = context
    plan = await c.plan()
    grant = await c.grant()
    saved, before = c.saved(grant), await c.rows()
    key_wait = change in {"key-revoked", "key-expired", "plan-expired-key"}
    async with c.factory() as holder, holder.begin():
        key: AdminResourceRow | None = None
        user: UserRow | None = None
        if key_wait:
            key = await holder.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == c.creator.tenant_id,
                    AdminResourceRow.kind == "plugin_signing_key",
                    AdminResourceRow.resource_id == KEY_ID,
                )
                .with_for_update()
            )
            assert key is not None
        else:
            actor = c.installer if change == "installer-disabled" else c.creator
            user = await holder.scalar(
                select(UserRow)
                .where(
                    UserRow.tenant_id == actor.tenant_id,
                    UserRow.id == actor.user_id,
                )
                .with_for_update()
            )
            assert user is not None
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        assert type(holder_pid) is int
        async with _observed_waiter(c, monkeypatch) as (task, future):
            pid = await asyncio.wait_for(future, timeout=10)
            await _blocked(c, pid, holder_pid, advisory=False)
            assert not task.done()
            if change.startswith("plan-expired"):
                c.clock[0] = plan.expires_at
            elif key_wait:
                assert key is not None
                if change == "key-revoked":
                    key.payload = {**key.payload, "trusted": False}
                else:
                    key.payload = {**key.payload, "not_after": c.clock[0].isoformat()}
            elif change == "creator-demoted":
                assert user is not None
                user.role = "viewer"
            else:
                assert user is not None
                user.disabled = True
            await holder.commit()
            expected: type[Exception]
            if change.startswith("plan-expired"):
                expected = Conflict
            elif change == "key-revoked":
                expected = StorageUnavailable
            elif change == "key-expired":
                expected = PackageVerificationError
            else:
                expected = PermissionDenied
            with pytest.raises(expected):
                await asyncio.wait_for(task, timeout=10)
    assert await c.rows() == before and c.saved(grant) == saved
    assert not c.canonical.exists()


@pytest.mark.parametrize(
    "change", ["grant-revoked", "plan-expired", "actor", "artifact", "creator-disabled"]
)
async def test_actual_registration_wait_revalidates_sql_binding(
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    c = context
    plan = await c.plan()
    grant = await c.grant()
    saved = c.saved(grant)
    async with c.factory() as holder, holder.begin():
        await acquire_plugin_registration_lock(holder, c.creator.tenant_id, PLUGIN_ID)
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        assert type(holder_pid) is int
        async with _observed_waiter(c, monkeypatch) as (task, future):
            pid = await asyncio.wait_for(future, timeout=10)
            await _blocked(c, pid, holder_pid, advisory=True)
            assert not task.done()
            if change == "grant-revoked":
                await c.grants.revoke(holder, c.creator, grant_id=grant.grant_id)
            elif change == "plan-expired":
                c.clock[0] = plan.expires_at
            elif change == "creator-disabled":
                await holder.execute(
                    update(UserRow).where(UserRow.id == c.creator.user_id).values(disabled=True)
                )
            elif change == "actor":
                await holder.execute(
                    update(RunRow).where(RunRow.id == c.run_id).values(actor_id=c.installer.user_id)
                )
            else:
                row = await holder.scalar(
                    select(RunArtifactRow)
                    .where(RunArtifactRow.id == c.artifact_id)
                    .with_for_update()
                )
                assert row is not None
                row.payload = {**row.payload, "producer": "rebound-after-plan"}
            await holder.commit()
            before = await c.rows()
            with pytest.raises(
                Conflict if change in {"grant-revoked", "plan-expired"} else PermissionDenied
            ):
                await asyncio.wait_for(task, timeout=10)
    assert await c.rows() == before and c.saved(grant) == saved
    assert not c.canonical.exists()


@asynccontextmanager
async def _sql_failure(
    c: Context, phase: Literal["plan", "audit", "consume"]
) -> AsyncIterator[list[str]]:
    engine = cast(AsyncEngine, c.factory.kw["bind"])
    suffix = uuid4().hex
    function, trigger = "confirmation_fail_" + suffix, "confirmation_trigger_" + suffix
    errors: list[str] = []

    def capture(error: ExceptionContext) -> None:
        errors.append(str(error.original_exception))

    # Keep this exact connection alive: pg_temp belongs to its backend, not the pool.
    async with engine.connect() as ddl:
        event.listen(engine.sync_engine, "handle_error", capture)
        created = False
        try:
            if phase == "plan":
                condition = "NEW.kind='plugin_handover' AND NEW.resource_id LIKE 'gh-%'"
                ordered = """NOT EXISTS (SELECT 1 FROM agent_hub_admin_resources
                    WHERE tenant_id=NEW.tenant_id AND kind='plugin_handover'
                        AND resource_id=NEW.payload->>'source_grant_id'
                        AND payload->>'state'='active'
                        AND payload->'binding'->>'plan_id'=NEW.resource_id)"""
            else:
                condition = (
                    "NEW.kind = 'audit'"
                    if phase == "audit"
                    else (
                        "NEW.kind = 'plugin_handover' AND NEW.resource_id LIKE 'sg-%' "
                        "AND NEW.payload->>'state' = 'consumed'"
                    )
                )
                audit_required = (
                    ""
                    if phase == "audit"
                    else (
                        "OR NOT EXISTS (SELECT 1 FROM agent_hub_admin_resources "
                        "WHERE tenant_id=NEW.tenant_id AND kind='audit')"
                    )
                )
                ordered = f"""NOT EXISTS (SELECT 1 FROM agent_hub_admin_resources
                    WHERE tenant_id=NEW.tenant_id AND kind='plugin'
                        AND resource_id='{PLUGIN_ID}')
                    OR NOT EXISTS (SELECT 1 FROM agent_hub_admin_resources
                    WHERE tenant_id=NEW.tenant_id AND resource_id LIKE 'gh-%'
                        AND payload->>'status'='pending') {audit_required}"""
            await ddl.execute(
                text(f"""
                CREATE FUNCTION pg_temp.{function}() RETURNS trigger LANGUAGE plpgsql AS $probe$
                BEGIN
                    IF NEW.tenant_id = '{c.creator.tenant_id}'::uuid AND ({condition}) THEN
                        IF {ordered} THEN
                            RAISE EXCEPTION 'confirmation_probe_unexpected_write_order';
                        END IF;
                        RAISE EXCEPTION 'confirmation_probe_{phase}' USING ERRCODE='23514';
                    END IF;
                    RETURN NEW;
                END; $probe$
            """)
            )
            await ddl.execute(
                text(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE "
                    f"ON agent_hub_admin_resources FOR EACH ROW EXECUTE FUNCTION pg_temp.{function}()"
                )
            )
            created = True
            await ddl.commit()
            yield errors
        finally:
            try:
                await ddl.rollback()
                if created:
                    await ddl.execute(text(f"DROP TRIGGER {trigger} ON agent_hub_admin_resources"))
                    await ddl.execute(text(f"DROP FUNCTION pg_temp.{function}()"))
                    await ddl.commit()
            finally:
                event.remove(engine.sync_engine, "handle_error", capture)


async def test_actual_creator_plan_failure_rolls_back_grant_preserving_shared_snapshot(
    context: Context,
) -> None:
    c = context
    original = await c.plan(key="existing-plan")
    grant = await c.grant()
    saved, before = c.saved(grant), await c.rows()
    async with _sql_failure(c, "plan") as errors:
        with pytest.raises(StorageUnavailable):
            await c.plan(key="failing-plan")
    assert any("confirmation_probe_plan" in message for message in errors)
    assert not any("unexpected_write_order" in message for message in errors)
    assert await c.rows() == before and c.saved(grant) == saved
    assert c.receipt == original and not c.canonical.exists()
    retry = await c.plan(key="failing-plan")
    assert retry.handover_id != original.handover_id
    assert len(await c.rows()) == 4 and c.saved(await c.grant()) == saved


@pytest.mark.parametrize("phase", ["audit", "consume"])
async def test_actual_sql_failure_rolls_back_all_outputs_preserving_shared_bytes(
    context: Context,
    phase: Literal["audit", "consume"],
) -> None:
    c = context
    plan = await c.plan()
    grant = await c.grant()
    saved, before = c.saved(grant), await c.rows()
    async with _sql_failure(c, phase) as errors:
        with pytest.raises(StorageUnavailable):
            await c.confirm()
    assert any("confirmation_probe_" + phase in message for message in errors)
    assert not any("unexpected_write_order" in message for message in errors)
    assert await c.rows() == before and c.saved(await c.grant()) == saved
    assert await c.storage.get_receipt(c.installer, c.run_id, plan.handover_id) == plan
    c.assert_package()
    inode = c.canonical.stat().st_ino
    assert (await c.confirm()).status == "pending"
    assert c.canonical.stat().st_ino == inode and len(await c.rows()) == 4


@pytest.mark.parametrize("expiry", ["plan", "key"])
async def test_actual_final_guard_rolls_back_after_sql_consume(
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
    expiry: str,
) -> None:
    c = context
    plan = await c.plan()
    until = plan.expires_at if expiry == "plan" else c.clock[0] + timedelta(minutes=10)
    if expiry == "key":
        async with c.factory() as session, session.begin():
            row = await session.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == c.creator.tenant_id,
                    AdminResourceRow.kind == "plugin_signing_key",
                    AdminResourceRow.resource_id == KEY_ID,
                )
                .with_for_update()
            )
            assert row is not None
            row.payload = {**row.payload, "not_after": until.isoformat()}
    before = await c.rows()
    original = c.storage._commit_pending_in_session
    reached = [False]

    async def late(
        session: AsyncSession,
        *args: object,
        **kwargs: object,
    ) -> HandoverReceipt:
        result = await original(session, *args, **kwargs)  # type: ignore[arg-type]
        rows = (
            await session.scalars(
                select(AdminResourceRow).where(
                    AdminResourceRow.tenant_id == c.creator.tenant_id,
                )
            )
        ).all()
        assert any(r.kind == "plugin" for r in rows) and any(r.kind == "audit" for r in rows)
        assert any(
            r.resource_id.startswith("sg-") and r.payload["state"] == "consumed" for r in rows
        )
        reached[0] = True
        c.clock[0] = until
        return result

    monkeypatch.setattr(c.storage, "_commit_pending_in_session", late)
    with pytest.raises(Conflict if expiry == "plan" else PackageVerificationError):
        await c.confirm()
    assert reached[0] and await c.rows() == before
    assert (await c.grant()).state == "active"
    c.assert_package()


async def test_actual_changed_original_artifact_cannot_replace_private_snapshot(
    context: Context,
) -> None:
    c = context
    await c.plan()
    grant = await c.grant()
    saved = c.saved(grant)
    async with c.factory() as session, session.begin():
        row = await session.scalar(
            select(RunArtifactRow).where(RunArtifactRow.id == c.artifact_id).with_for_update()
        )
        assert row is not None
        row.payload = {**row.payload, "producer": "changed-source"}
    before = await c.rows()
    with pytest.raises(PermissionDenied):
        await c.confirm()
    assert await c.rows() == before and c.saved(grant) == saved
    assert not c.canonical.exists()


async def test_actual_foreign_tenant_does_not_authorize_confirmation(context: Context) -> None:
    c = context
    await c.plan()
    before = await c.rows()
    foreign = AuthenticatedPrincipal(c.installer.user_id, uuid4(), Role.ADMIN)
    with pytest.raises((NotFound, PermissionDenied)):
        await c.confirm(principal=foreign)
    assert await c.rows() == before and not c.canonical.exists()


@pytest.mark.parametrize("change", ["extra", "different", "hardlink", "symlink"])
async def test_actual_existing_unknown_package_is_never_overwritten(
    context: Context, change: str
) -> None:
    c = context
    await c.plan()
    c.canonical.mkdir(parents=True)
    for name, content in _plugin_archive_signature_files(c.archive):
        path = c.canonical / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    victim = c.canonical.parent / "victim.bin"
    victim.write_bytes(b"preserve unknown shared bytes")
    if change == "extra":
        (c.canonical / "unknown.bin").write_bytes(b"preserve extra bytes")
    elif change == "different":
        (c.canonical / "main.py").write_bytes(b"preserve different bytes")
    else:
        leaf = c.canonical / "main.py"
        leaf.unlink()
        if change == "hardlink":
            os.link(victim, leaf)
        else:
            try:
                leaf.symlink_to(victim)
            except OSError:
                pytest.skip("real symlink creation unavailable on this platform/account")
    before, inode = await c.rows(), c.canonical.stat().st_ino
    content = (c.canonical / "main.py").read_bytes()
    with pytest.raises(Conflict):
        await c.confirm()
    assert await c.rows() == before and c.canonical.stat().st_ino == inode
    assert (c.canonical / "main.py").read_bytes() == content
    assert victim.read_bytes() == b"preserve unknown shared bytes"
