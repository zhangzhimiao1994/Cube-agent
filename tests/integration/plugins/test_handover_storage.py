"""Real PostgreSQL tests, exclusively for an explicitly isolated test database.

Set AGENT_HUB_TEST_DATABASE_URL to a fresh, dedicated testDB and set
AGENT_HUB_HANDOVER_TEST_DB_ISOLATED=1 before running. The parent integration
conftest migrates that database; auth_session_factory owns its test data cleanup.
Never point this suite at production. No DB was exercised during local delivery.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Table, delete, event, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert, Update

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import (
    AdminResourceRow,
    RunArtifactRow,
    RunOutboxRow,
    RunRow,
    TenantRow,
    UserRow,
)
from agent_hub.plugins.handover_grants import DurableSourceGrantService, SourceGrant
from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, StoredSourceSnapshot
from agent_hub.plugins.handover_sources import FrozenSourceSnapshot, SnapshotFile, _canonical
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    AuthorizedSource,
    Conflict,
    HandoverReceipt,
    PlanData,
    PluginHandoverStorage,
    StorageUnavailable,
    VerifiedPendingPlugin,
    acquire_plugin_registration_lock,
    plan_identity,
)
from agent_hub.runtime.contracts import Artifact

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED") != "1"
        or not os.environ.get("AGENT_HUB_TEST_DATABASE_URL"),
        reason="requires explicit isolated testDB URL and isolation opt-in; no DB verified locally",
    ),
]


HandoverContext = tuple[
    async_sessionmaker[AsyncSession],
    PluginHandoverStorage,
    AuthenticatedPrincipal,
    AuthenticatedPrincipal,
    UUID,
    AuthorizedSource,
    list[datetime],
]


@pytest.fixture
async def handover_context(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[HandoverContext]:
    """All persistent rows belong to the isolated testDB owned by this fixture."""
    factory = auth_session_factory
    creator = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    installer = AuthenticatedPrincipal(uuid4(), creator.tenant_id, Role.ADMIN)
    run_id = uuid4()
    clock = [datetime.now(UTC)]
    async with factory() as session, session.begin():
        session.add(TenantRow(id=creator.tenant_id, slug=f"handover-{uuid4().hex}", name="Test"))
        session.add(
            RunRow(
                id=run_id,
                tenant_id=creator.tenant_id,
                actor_id=creator.user_id,
                actor_role=creator.role.value,
                request="isolated storage test",
                status="completed",
                version=1,
            )
        )
    source = AuthorizedSource(
        tenant_id=creator.tenant_id,
        run_id=run_id,
        source=ArtifactSource(artifact_id="test-artifact"),
        source_manifest_sha256="1" * 64,
    )
    store = PluginHandoverStorage(factory, clock=lambda: clock[0])
    try:
        yield factory, store, creator, installer, run_id, source, clock
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(AdminResourceRow).where(AdminResourceRow.tenant_id == creator.tenant_id)
            )


async def plan(
    context: HandoverContext, key: str = "plan-1", plugin_id: str = "generated-tool"
) -> HandoverReceipt:
    _, store, creator, _, run_id, source, clock = context
    return await store.create_plan(
        creator,
        run_id,
        source,
        PlanData(plugin_id=plugin_id, plugin_version="1.0.0"),
        key,
        clock[0] + timedelta(hours=1),
    )


def verified(
    context: HandoverContext, receipt: HandoverReceipt, signature_hash: str = "2" * 64
) -> VerifiedPendingPlugin:
    _, _, creator, _, run_id, source, clock = context
    return VerifiedPendingPlugin(
        tenant_id=creator.tenant_id,
        run_id=run_id,
        handover_id=receipt.handover_id,
        plan_hash=receipt.plan_hash,
        source_manifest_sha256=source.source_manifest_sha256,
        signature_payload_sha256=signature_hash,
        archive_sha256="3" * 64,
        key_id="test-key",
        verified_public_key_sha256="4" * 64,
        artifact_ref="test-internal-artifact",
        plugin_payload={
            "id": receipt.plugin_id,
            "name": "Generated test tool",
            "version": "1.0.0",
            "enabled": False,
            "status": "disabled",
            "health": "disabled",
            "resource_config": {},
            "package_metadata": {
                "kind": "adapter_package",
                "package_version": "1.0.0",
                "signature": {"algorithm": "ed25519", "key_id": "test-key", "value": "A" * 86},
                "signature_verification": "verified",
                "verified_public_key_sha256": "4" * 64,
                "approval_state": "pending",
                "activation_state": "blocked_pending_approval",
                "runtime": "python",
                "entrypoint": "adapter.py",
                "isolation": "local_process",
                "install_mode": "scan_only",
                "artifact": {
                    "storage_key": f"{creator.tenant_id}/{receipt.plugin_id}/{'3' * 64}",
                    "content_sha256": "3" * 64,
                    "file_count": 2,
                    "total_size_bytes": 512,
                    "stored_at": clock[0].isoformat(),
                    "quarantine_state": "stored",
                },
            },
        },
    )


async def confirm(
    context: HandoverContext,
    receipt: HandoverReceipt,
    *,
    key: str = "confirm-1",
    metadata: VerifiedPendingPlugin | None = None,
    store: PluginHandoverStorage | None = None,
    principal: AuthenticatedPrincipal | None = None,
) -> HandoverReceipt:
    _, original_store, _, installer, run_id, *_ = context
    return await (store or original_store).commit_pending(
        principal or installer,
        run_id,
        receipt.handover_id,
        receipt.version,
        receipt.plan_hash,
        True,
        key,
        metadata or verified(context, receipt),
    )


async def concurrent[Result](
    *operations: Callable[[], Awaitable[Result]],
) -> list[Result | BaseException]:
    """Release independent sessions together and await them with a bounded timeout."""
    gate = asyncio.Event()

    async def worker(operation: Callable[[], Awaitable[Result]]) -> Result:
        await gate.wait()
        return await operation()

    tasks = [asyncio.create_task(worker(op)) for op in operations]
    gate.set()
    try:
        return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def rows(context: HandoverContext) -> list[AdminResourceRow]:
    factory, _, creator, *_ = context
    async with factory() as session:
        resources = list(
            await session.scalars(
                select(AdminResourceRow).where(AdminResourceRow.tenant_id == creator.tenant_id)
            )
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(RunOutboxRow)
            .where(RunOutboxRow.tenant_id == creator.tenant_id)
        )
    assert outbox == 0
    return resources


async def test_parallel_plan_key_is_unique_and_changed_content_conflicts(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    results = await concurrent(*(lambda: plan(context) for _ in range(4)))
    assert all(not isinstance(value, BaseException) for value in results)
    assert all(value == results[0] for value in results)
    assert len(await rows(context)) == 1
    with pytest.raises(Conflict):
        await plan(context, plugin_id="other-plugin")


async def test_real_parallel_same_confirm_key_has_one_plugin_receipt_and_audit(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    receipt = await plan(context)
    results = await concurrent(*(lambda: confirm(context, receipt) for _ in range(6)))
    assert all(not isinstance(value, BaseException) for value in results)
    assert all(value == results[0] for value in results)
    saved = await rows(context)
    assert sorted(row.kind for row in saved) == ["audit", "plugin", "plugin_handover"]
    plugin = next(row.payload for row in saved if row.kind == "plugin")
    assert plugin["_generated_handover_id"] == receipt.handover_id
    assert plugin["enabled"] is False
    package = plugin["package_metadata"]
    assert isinstance(package, dict)
    assert package["approval_state"] == "pending"
    handover = next(row.payload for row in saved if row.kind == "plugin_handover")
    result = results[0]
    assert isinstance(result, HandoverReceipt)
    assert handover["receipt_id"] == result.receipt_id
    assert handover["audit_id"] == result.audit_id


@pytest.mark.parametrize("changed", ["key", "content"])
async def test_real_parallel_changed_confirm_key_or_content_cannot_overwrite(
    handover_context: HandoverContext, changed: str
) -> None:
    context = handover_context
    receipt = await plan(context)
    metadata = None if changed == "key" else verified(context, receipt, signature_hash="5" * 64)

    async def changed_confirmation() -> HandoverReceipt:
        if changed == "key":
            return await confirm(context, receipt, key="changed-key")
        return await confirm(context, receipt, metadata=metadata)

    results = await concurrent(lambda: confirm(context, receipt), changed_confirmation)
    assert sum(isinstance(value, Conflict) for value in results) == 1
    assert sum(not isinstance(value, BaseException) for value in results) == 1
    assert len(await rows(context)) == 3


async def test_real_parallel_cancel_commit_has_one_legal_terminal_state(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    receipt = await plan(context)
    _, store, creator, _, run_id, *_ = context
    results = await concurrent(
        lambda: confirm(context, receipt),
        lambda: store.cancel_plan(creator, run_id, receipt.handover_id, 1, receipt.plan_hash),
    )
    assert sum(isinstance(value, Conflict) for value in results) == 1
    successful = [value for value in results if not isinstance(value, BaseException)]
    assert len(successful) == 1 and successful[0].status in {"cancelled", "pending"}
    saved = await store.get_receipt(creator, run_id, receipt.handover_id)
    assert saved == successful[0]
    assert len(await rows(context)) == (3 if saved.status == "pending" else 1)


async def test_two_handovers_competing_for_absent_plugin_have_only_one_winner(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    first = await plan(context)
    second = await plan(context, key="plan-2")
    results = await concurrent(lambda: confirm(context, first), lambda: confirm(context, second))
    assert sum(isinstance(value, Conflict) for value in results) == 1
    assert sum(not isinstance(value, BaseException) for value in results) == 1
    assert sorted(row.kind for row in await rows(context)) == [
        "audit",
        "plugin",
        "plugin_handover",
        "plugin_handover",
    ]


@pytest.mark.parametrize("failed_write", ["plugin", "receipt", "audit"])
async def test_each_real_write_failure_rolls_back_plugin_receipt_and_audit(
    handover_context: HandoverContext, failed_write: str
) -> None:
    context = handover_context
    factory, store, creator, _, run_id, *_ = context
    receipt = await plan(context)
    async with factory() as session:
        engine = session.get_bind()
    injected: list[str] = []

    def fail_write(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        execution_context: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        compiled = execution_context.compiled
        if compiled is None:
            return
        command = compiled.statement
        if (
            not isinstance(command, Insert | Update)
            or cast(Table, command.table).name != AdminResourceRow.__tablename__
        ):
            return
        for params in execution_context.compiled_parameters:
            is_insert = isinstance(command, Insert) and params.get("kind") == failed_write
            is_receipt = (
                failed_write == "receipt"
                and isinstance(command, Update)
                and params.get("payload", {}).get("status") == "pending"
            )
            if is_insert or is_receipt:
                injected.append(failed_write)
                raise OperationalError(
                    "injected testDB write failure", None, Exception("test-only")
                )

    event.listen(engine, "before_cursor_execute", fail_write)
    try:
        with pytest.raises(StorageUnavailable):
            await confirm(context, receipt)
    finally:
        event.remove(engine, "before_cursor_execute", fail_write)
    assert injected == [failed_write]
    resources = await rows(context)
    assert len(resources) == 1 and resources[0].kind == "plugin_handover"
    assert await store.get_receipt(creator, run_id, receipt.handover_id) == receipt
    retry = await confirm(context, receipt)
    assert retry.status == "pending" and len(await rows(context)) == 3


async def test_recreated_storage_reads_original_receipt_after_expiry_and_downgrade(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    factory, _, creator, installer, run_id, _, clock = context
    receipt = await plan(context)
    result = await confirm(context, receipt)
    clock[0] += timedelta(days=2)
    recreated = PluginHandoverStorage(factory, clock=lambda: clock[0])
    assert await confirm(context, receipt, store=recreated) == result
    assert await recreated.get_receipt(creator, run_id, receipt.handover_id) == result
    downgraded = AuthenticatedPrincipal(installer.user_id, installer.tenant_id, Role.OPERATOR)
    with pytest.raises(PermissionDenied):
        await confirm(context, receipt, store=recreated, principal=downgraded)
    with pytest.raises(Conflict):
        await confirm(context, receipt, store=recreated, key="changed")
    assert len(await rows(context)) == 3


async def test_shared_registration_lock_serializes_legacy_writer_and_handover(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    factory, _, creator, *_ = context
    receipt = await plan(context)
    async with factory() as blocker, blocker.begin():
        await acquire_plugin_registration_lock(blocker, creator.tenant_id, receipt.plugin_id)
        contender = asyncio.create_task(confirm(context, receipt))
        try:
            done, _ = await asyncio.wait({contender}, timeout=0.15)
            assert not done
            blocker.add(
                AdminResourceRow(
                    tenant_id=creator.tenant_id,
                    kind="plugin",
                    resource_id=receipt.plugin_id,
                    payload={"enabled": True},
                )
            )
            await blocker.flush()
        except BaseException:
            contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
            raise
    with pytest.raises(Conflict):
        await asyncio.wait_for(contender, timeout=10)
    saved = await rows(context)
    plugin = next(row for row in saved if row.kind == "plugin")
    assert plugin.payload == {"enabled": True} and len(saved) == 2


async def test_expiry_during_real_registration_lock_wait_prevents_all_writes(
    handover_context: HandoverContext,
) -> None:
    context = handover_context
    factory, store, creator, _, run_id, _, clock = context
    receipt = await plan(context)
    query = text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks held JOIN pg_locks waiting
              ON held.locktype = waiting.locktype
             AND held.database IS NOT DISTINCT FROM waiting.database
             AND held.classid = waiting.classid AND held.objid = waiting.objid
             AND held.objsubid = waiting.objsubid
            WHERE held.locktype = 'advisory' AND held.granted
              AND held.pid = pg_backend_pid() AND NOT waiting.granted
              AND pg_backend_pid() = ANY(pg_blocking_pids(waiting.pid))
        )
    """)
    contender: asyncio.Task[HandoverReceipt] | None = None
    try:
        async with factory() as blocker, blocker.begin():
            await acquire_plugin_registration_lock(blocker, creator.tenant_id, receipt.plugin_id)
            contender = asyncio.create_task(confirm(context, receipt))
            async with asyncio.timeout(10):
                while not await blocker.scalar(query):
                    assert not contender.done(), "confirmation bypassed registration lock"
                    await asyncio.sleep(0.01)
            clock[0] = receipt.expires_at
        with pytest.raises(Conflict):
            await asyncio.wait_for(contender, timeout=10)
        resources = await rows(context)
        assert len(resources) == 1 and resources[0].kind == "plugin_handover"
        assert await store.get_receipt(creator, run_id, receipt.handover_id) == receipt
    finally:
        if contender is not None:
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)


@dataclass
class StorageBridgeContext:
    """Real SQL seam fixture, not proof of signed archive/member equivalence.

    Grant/current-user/key row checks and receipt consumption are actual SQL.
    VerifiedPendingPlugin remains the existing test-boundary package fixture;
    full cryptographic package/bridge authorization belongs to parent tests.
    """

    context: HandoverContext
    grants: DurableSourceGrantService
    grant: SourceGrant
    receipt: HandoverReceipt
    snapshot_store: FrozenSourceSnapshotStore
    stored: StoredSourceSnapshot
    public_key: bytes

    def package(self) -> VerifiedPendingPlugin:
        original = verified(self.context, self.receipt)
        digest = hashlib.sha256(self.public_key).hexdigest()
        payload = deepcopy(original.plugin_payload)
        package = payload["package_metadata"]
        assert isinstance(package, dict)
        package["verified_public_key_sha256"] = digest
        return VerifiedPendingPlugin.model_validate(
            {
                **original.model_dump(mode="python"),
                "verified_public_key_sha256": digest,
                "plugin_payload": payload,
            }
        )

    async def validate(
        self, session: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
    ) -> None:
        from agent_hub.api.routers.admin import (
            PluginSigningKeyResponse,
            _plugin_signing_key_is_active,
        )

        _, _, _, installer, _, _, clock = self.context
        if (locked.source_grant_id, locked.source_grant_binding_sha256) != (
            self.grant.grant_id,
            self.grant.binding_sha256,
        ):
            raise Conflict("actual grant pair differs")
        await self.grants._verify(
            session, installer, self.grant.binding, consumed=locked.status == "pending"
        )
        row = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == installer.tenant_id,
                AdminResourceRow.kind == "plugin_signing_key",
                AdminResourceRow.resource_id == package.key_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise Conflict("actual signing key absent")
        key = PluginSigningKeyResponse.model_validate(row.payload)
        public = base64.urlsafe_b64decode(key.public_key + "=" * (-len(key.public_key) % 4))
        if (
            not key.trusted
            or not _plugin_signing_key_is_active(key, clock[0])
            or hashlib.sha256(public).hexdigest() != package.verified_public_key_sha256
        ):
            raise Conflict("actual signing key changed")
        # The key SHARE lock can wait after the first grant check.
        await self.grants._verify(
            session, installer, self.grant.binding, consumed=locked.status == "pending"
        )

    async def consume(self, session: AsyncSession, pending: HandoverReceipt) -> None:
        assert (pending.source_grant_id, pending.source_grant_binding_sha256) == (
            self.grant.grant_id,
            self.grant.binding_sha256,
        )
        assert pending.receipt_id is not None
        await self.grants.consume(
            session, self.context[3], expected=self.grant.binding, receipt_id=pending.receipt_id
        )

    async def confirm(
        self,
        session: AsyncSession,
        *,
        validate: Callable[[AsyncSession, HandoverReceipt, VerifiedPendingPlugin], Awaitable[None]]
        | None = None,
        consume: Callable[[AsyncSession, HandoverReceipt], Awaitable[None]] | None = None,
    ) -> HandoverReceipt:
        _, store, _, installer, run_id, *_ = self.context
        return await store._commit_pending_in_session(
            session,
            installer,
            run_id,
            self.receipt.handover_id,
            1,
            self.receipt.plan_hash,
            True,
            "bridge-confirm",
            self.package(),
            validate_bridge=validate or self.validate,
            consume_bridge=consume or self.consume,
        )

    async def security_snapshot(self) -> tuple[tuple[UUID, str, str, dict[str, object]], ...]:
        return tuple(
            (row.id, row.kind, row.resource_id, deepcopy(row.payload))
            for row in sorted(await rows(self.context), key=lambda row: str(row.id))
        )


@pytest.fixture
async def storage_bridge_context(
    handover_context: HandoverContext,
    tmp_path: Path,
) -> StorageBridgeContext:
    from agent_hub.api.routers.admin import PluginSigningKeyResponse

    factory, store, creator, installer, run_id, _, clock = handover_context
    raw = {
        "main.py": b"raise RuntimeError('SQL-seam-never-execute')\n",
        "plugin.json": b'{"id":"generated-tool","version":"1.0.0"}\n',
    }
    artifact = Artifact(
        id=uuid4(),
        type="workspace_bundle",
        producer="storage-test",
        content={
            "workspace_bundle": {"files": {name: data.decode() for name, data in raw.items()}}
        },
    )
    artifact_payload = artifact.to_payload()
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    key = PluginSigningKeyResponse(
        key_id="test-key",
        algorithm="ed25519",
        public_key=base64.urlsafe_b64encode(public_key).rstrip(b"=").decode(),
        trusted=True,
    )
    async with factory() as session, session.begin():
        session.add_all(
            [
                UserRow(
                    id=creator.user_id,
                    tenant_id=creator.tenant_id,
                    username="bridge-creator",
                    role="operator",
                    disabled=False,
                ),
                UserRow(
                    id=installer.user_id,
                    tenant_id=creator.tenant_id,
                    username="bridge-installer",
                    role="admin",
                    disabled=False,
                ),
                RunArtifactRow(
                    id=artifact.id,
                    tenant_id=creator.tenant_id,
                    run_id=run_id,
                    type=artifact.type,
                    producer=artifact.producer,
                    content_sha256=artifact.content_sha256,
                    payload=artifact_payload,
                ),
                AdminResourceRow(
                    tenant_id=creator.tenant_id,
                    kind="plugin_signing_key",
                    resource_id=key.key_id,
                    payload=key.model_dump(mode="json"),
                ),
            ]
        )
    source = ArtifactSource(artifact_id=str(artifact.id))
    files = tuple(
        SnapshotFile(name, raw[name], len(raw[name]), hashlib.sha256(raw[name]).hexdigest())
        for name in sorted(raw)
    )
    source_hash = hashlib.sha256(
        _canonical(
            {
                "schema_version": 1,
                "domain": "generated-handover-source",
                "tenant_id": str(creator.tenant_id),
                "run_id": str(run_id),
                "actor_id": str(creator.user_id),
                "source": source.model_dump(mode="json"),
                "artifact_sha256": hashlib.sha256(_canonical(artifact_payload)).hexdigest(),
                "files": [
                    {"path": f.path, "size_bytes": f.size_bytes, "sha256": f.sha256} for f in files
                ],
            }
        )
    ).hexdigest()
    authorized = AuthorizedSource(
        tenant_id=creator.tenant_id,
        run_id=run_id,
        source=source,
        source_manifest_sha256=source_hash,
    )
    frozen = FrozenSourceSnapshot(
        authorized, files, "generated-tool", "1.0.0", sum(f.size_bytes for f in files)
    )
    snapshots = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    reference = snapshots.publish(frozen, creator_id=creator.user_id)
    stored = snapshots.read(
        tenant_id=creator.tenant_id,
        snapshot_id=reference.snapshot_id,
        blob_sha256=reference.blob_sha256,
        source_manifest_sha256=source_hash,
    )
    grants = DurableSourceGrantService(clock=lambda: clock[0])
    async with factory() as session, session.begin():
        grant = await grants.publish(
            session,
            creator,
            stored,
            plan_id=plan_identity(creator, "bridge-plan"),
            plan_version=1,
            expires_at=clock[0] + timedelta(hours=1),
        )
        receipt = await store.create_plan_in_session(
            session,
            creator,
            run_id,
            authorized,
            PlanData(plugin_id="generated-tool", plugin_version="1.0.0"),
            "bridge-plan",
            grant.binding.expires_at,
            source_grant_id=grant.grant_id,
            source_grant_binding_sha256=grant.binding_sha256,
        )
    context = (factory, store, creator, installer, run_id, authorized, clock)
    return StorageBridgeContext(context, grants, grant, receipt, snapshots, stored, public_key)


async def test_actual_seam_consumes_with_plugin_receipt_audit_and_idempotent_retry(
    storage_bridge_context: StorageBridgeContext,
) -> None:
    bridge = storage_bridge_context
    async with bridge.context[0]() as session, session.begin():
        result = await bridge.confirm(session)
        assert session.in_transaction() and result.status == "pending"
        grant_row = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == bridge.context[2].tenant_id,
                AdminResourceRow.resource_id == bridge.grant.grant_id,
            )
            .execution_options(populate_existing=True)
        )
        assert grant_row is not None and grant_row.payload["state"] == "consumed"
        assert grant_row.payload["receipt_id"] == result.receipt_id
    before = await bridge.security_snapshot()
    async with bridge.context[0]() as session, session.begin():
        assert await bridge.confirm(session) == result
    assert await bridge.security_snapshot() == before
    assert sorted(row.kind for row in await rows(bridge.context)) == [
        "audit",
        "plugin",
        "plugin_handover",
        "plugin_handover",
        "plugin_signing_key",
    ]


@pytest.mark.parametrize("phase", ["validate", "consume", "late_sql"])
async def test_actual_seam_raw_failure_leaves_rollback_to_caller(
    storage_bridge_context: StorageBridgeContext,
    phase: str,
) -> None:
    bridge = storage_bridge_context
    before = await bridge.security_snapshot()

    async def validate(
        session: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
    ) -> None:
        await bridge.validate(session, locked, package)
        if phase == "validate":
            raise Conflict("current authority failure")

    async def consume(session: AsyncSession, pending: HandoverReceipt) -> None:
        await bridge.consume(session, pending)
        if phase == "consume":
            raise RuntimeError("known late failure")
        if phase == "late_sql":
            session.add(
                AdminResourceRow(
                    tenant_id=bridge.context[2].tenant_id,
                    kind="not-a-supported-kind",
                    resource_id="reject",
                    payload={},
                )
            )
            await session.flush()

    error = (
        Conflict if phase == "validate" else IntegrityError if phase == "late_sql" else RuntimeError
    )
    with pytest.raises(error):
        async with bridge.context[0]() as session, session.begin():
            await bridge.confirm(session, validate=validate, consume=consume)
    assert await bridge.security_snapshot() == before
    assert (
        bridge.snapshot_store.read(
            tenant_id=bridge.context[2].tenant_id,
            snapshot_id=bridge.stored.reference.snapshot_id,
            blob_sha256=bridge.stored.reference.blob_sha256,
            source_manifest_sha256=bridge.stored.reference.source_manifest_sha256,
        )
        == bridge.stored
    )
    async with bridge.context[0]() as session, session.begin():
        assert (await bridge.confirm(session)).status == "pending"


async def test_actual_seam_refreshes_preloaded_plan_after_independent_cancellation(
    storage_bridge_context: StorageBridgeContext,
) -> None:
    bridge = storage_bridge_context
    factory, store, creator, _, run_id, *_ = bridge.context
    async with factory() as stale, stale.begin():
        cached = await stale.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == creator.tenant_id,
                AdminResourceRow.kind == "plugin_handover",
                AdminResourceRow.resource_id == bridge.receipt.handover_id,
            )
        )
        assert cached is not None and cached.payload["version"] == 1
        await store.cancel_plan(
            creator, run_id, bridge.receipt.handover_id, 1, bridge.receipt.plan_hash
        )
        before = await bridge.security_snapshot()
        with pytest.raises(Conflict):
            await bridge.confirm(stale)
        assert cached.payload["status"] == "cancelled"
    assert await bridge.security_snapshot() == before


@pytest.mark.parametrize("pending", [False, True])
async def test_actual_seam_bound_record_is_never_confirmed_by_legacy_wrapper(
    storage_bridge_context: StorageBridgeContext,
    pending: bool,
) -> None:
    bridge = storage_bridge_context
    factory, store, _, installer, run_id, *_ = bridge.context
    if pending:
        async with factory() as session, session.begin():
            await bridge.confirm(session)
    before = await bridge.security_snapshot()
    with pytest.raises(StorageUnavailable, match="bridge"):
        await store.commit_pending(
            installer,
            run_id,
            bridge.receipt.handover_id,
            1,
            bridge.receipt.plan_hash,
            True,
            "bridge-confirm",
            bridge.package(),
        )
    assert await bridge.security_snapshot() == before


@pytest.mark.parametrize("lock", ["registration", "key"])
async def test_actual_seam_expiry_after_real_registration_or_final_key_wait(
    storage_bridge_context: StorageBridgeContext,
    lock: str,
) -> None:
    bridge = storage_bridge_context
    factory, _, creator, _, _, _, clock = bridge.context
    before = await bridge.security_snapshot()
    waiter_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def waiter() -> None:
        async with factory() as session, session.begin():
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert type(pid) is int
            waiter_pid.set_result(pid)
            await bridge.confirm(session)

    async with factory() as blocker, blocker.begin():
        if lock == "registration":
            await acquire_plugin_registration_lock(
                blocker, creator.tenant_id, bridge.receipt.plugin_id
            )
        else:
            await blocker.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == creator.tenant_id,
                    AdminResourceRow.kind == "plugin_signing_key",
                    AdminResourceRow.resource_id == "test-key",
                )
                .with_for_update()
            )
        task = asyncio.create_task(waiter())
        try:
            pid = await asyncio.wait_for(waiter_pid, timeout=10)
            evidence = text("""
                SELECT pg_backend_pid() = ANY(pg_blocking_pids(:pid)) AND EXISTS (
                    SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted
                    AND locktype=CASE WHEN :advisory THEN 'advisory' ELSE 'transactionid' END
                )
            """)
            async with asyncio.timeout(10):
                while not await blocker.scalar(
                    evidence, {"pid": pid, "advisory": lock == "registration"}
                ):
                    assert not task.done(), "confirmation bypassed actual lock"
                    await asyncio.sleep(0.01)
            clock[0] = bridge.receipt.expires_at
            await blocker.commit()
            with pytest.raises(Conflict):
                await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await bridge.security_snapshot() == before


async def test_actual_grant_and_plan_creation_share_caller_rollback_preserving_blob(
    storage_bridge_context: StorageBridgeContext,
) -> None:
    bridge = storage_bridge_context
    factory, store, creator, _, run_id, source, _ = bridge.context
    before = await bridge.security_snapshot()
    with pytest.raises(RuntimeError, match="caller rollback"):
        async with factory() as session, session.begin():
            grant = await bridge.grants.publish(
                session,
                creator,
                bridge.stored,
                plan_id=plan_identity(creator, "new-plan"),
                plan_version=1,
                expires_at=bridge.grant.binding.expires_at,
            )
            await store.create_plan_in_session(
                session,
                creator,
                run_id,
                source,
                PlanData(plugin_id="generated-tool", plugin_version="1.0.0"),
                "new-plan",
                grant.binding.expires_at,
                source_grant_id=grant.grant_id,
                source_grant_binding_sha256=grant.binding_sha256,
            )
            raise RuntimeError("caller rollback")
    assert await bridge.security_snapshot() == before
    assert (
        bridge.snapshot_store.read(
            tenant_id=creator.tenant_id,
            snapshot_id=bridge.stored.reference.snapshot_id,
            blob_sha256=bridge.stored.reference.blob_sha256,
            source_manifest_sha256=source.source_manifest_sha256,
        )
        == bridge.stored
    )
