"""SQL-boundary tests, not evidence of real PostgreSQL row-lock scheduling."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from asyncpg.pgproto.pgproto import UUID as DriverUUID  # type: ignore[import-untyped]
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.sql import Select
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.elements import ClauseElement, TextClause

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, RunRow, UserRow
from agent_hub.plugins.handover_grants import DurableSourceGrantService, GrantBinding, SourceGrant
from agent_hub.plugins.handover_snapshots import (
    SnapshotReference,
    SnapshotStoreLimits,
    StoredSourceSnapshot,
    _encode,
)
from agent_hub.plugins.handover_sources import FrozenSourceSnapshot, SnapshotFile, _canonical
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    AuthorizedSource,
    Conflict,
    PlanData,
    StorageUnavailable,
    WorkspaceSource,
    _digest,
    _HandoverRecord,
    _plan_fingerprint,
)
from agent_hub.runtime.contracts import Artifact

NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
PLAN = "gh-" + "3" * 64
POSTGRES = cast(type[Dialect], postgresql.dialect)


class SQLBoundary:
    def __init__(self) -> None:
        self.rows: list[AdminResourceRow | UserRow | RunRow | RunArtifactRow] = []
        self.statements: list[ClauseElement] = []
        self.after: Callable[[ClauseElement], None] | None = None

    def params(self, statement: ClauseElement) -> dict[str, Any]:
        return cast(dict[str, Any], statement.compile(dialect=POSTGRES()).params)

    async def execute(
        self, statement: ClauseElement, parameters: Mapping[str, object] | None = None
    ) -> Mock:
        self.statements.append(statement)
        if isinstance(statement, Insert):
            values = self.params(statement)
            if not any(
                isinstance(row, AdminResourceRow)
                and (row.tenant_id, row.kind, row.resource_id)
                == (values["tenant_id"], values["kind"], values["resource_id"])
                for row in self.rows
            ):
                self.rows.append(AdminResourceRow(**values))
        if self.after:
            self.after(statement)
        return Mock()

    async def scalar(self, statement: ClauseElement) -> object:
        self.statements.append(statement)
        params = self.params(statement)
        if isinstance(statement, Update):
            for row in self.rows:
                if (
                    isinstance(row, AdminResourceRow)
                    and row.id == params["id_1"]
                    and row.tenant_id == params["tenant_id_1"]
                    and row.kind == params["kind_1"]
                    and row.resource_id == params["resource_id_1"]
                    and row.payload == params["payload_1"]
                ):
                    row.payload = deepcopy(params["payload"])
                    return row.id
            return None
        if not isinstance(statement, Select):
            raise TypeError("unexpected SQL")
        entity = statement.column_descriptions[0]["entity"]
        found = None
        for row in self.rows:
            if not isinstance(row, entity) or row.tenant_id != params["tenant_id_1"]:
                continue
            if "id_1" in params and row.id != params["id_1"]:
                continue
            if isinstance(row, AdminResourceRow) and (
                row.kind != params["kind_1"] or row.resource_id != params["resource_id_1"]
            ):
                continue
            if isinstance(row, RunArtifactRow) and row.run_id != params["run_id_1"]:
                continue
            found = row
            break
        if self.after:
            self.after(statement)
        return found


class Context:
    def __init__(self, session: AsyncSession, db: SQLBoundary) -> None:
        self.session, self.db = session, db
        self.creator = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
        self.installer = AuthenticatedPrincipal(uuid4(), self.creator.tenant_id, Role.ADMIN)
        self.clock = [NOW]
        self.service = DurableSourceGrantService(clock=lambda: self.clock[0])
        self.run = RunRow(
            id=uuid4(),
            tenant_id=self.creator.tenant_id,
            actor_id=self.creator.user_id,
            actor_role="operator",
            request="test",
            status="completed",
            version=1,
            routing_decision={"project_id": "project", "workspace_session_id": "session"},
        )
        db.rows.extend(
            [
                UserRow(
                    id=self.creator.user_id,
                    tenant_id=self.creator.tenant_id,
                    username="creator",
                    role="operator",
                    disabled=False,
                ),
                UserRow(
                    id=self.installer.user_id,
                    tenant_id=self.creator.tenant_id,
                    username="installer",
                    role="admin",
                    disabled=False,
                ),
                self.run,
            ]
        )

    def snapshot(self, *, artifact: bool = False) -> StoredSourceSnapshot:
        raw = {
            "main.py": "raise RuntimeError('grant-source-must-not-execute')\n",
            "plugin.json": '{"id":"grant-tool","version":"1.0.0"}\n',
        }
        selector: ArtifactSource | WorkspaceSource
        artifact_hash = None
        if artifact:
            item = Artifact(
                id=uuid4(),
                type="workspace_bundle",
                producer="unit",
                content={"workspace_bundle": {"files": raw}},
            )
            payload = item.to_payload()
            self.db.rows.append(
                RunArtifactRow(
                    id=item.id,
                    tenant_id=self.creator.tenant_id,
                    run_id=self.run.id,
                    type=item.type,
                    producer=item.producer,
                    content_sha256=item.content_sha256,
                    payload=payload,
                )
            )
            selector = ArtifactSource(artifact_id=str(item.id))
            artifact_hash = hashlib.sha256(_canonical(payload)).hexdigest()
        else:
            selector = WorkspaceSource(
                project_id="project", session_id="session", paths=tuple(sorted(raw))
            )
        files = tuple(
            SnapshotFile(
                name,
                raw[name].encode(),
                len(raw[name].encode()),
                hashlib.sha256(raw[name].encode()).hexdigest(),
            )
            for name in sorted(raw)
        )
        source_hash = hashlib.sha256(
            _canonical(
                {
                    "schema_version": 1,
                    "domain": "generated-handover-source",
                    "tenant_id": str(self.creator.tenant_id),
                    "run_id": str(self.run.id),
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
                run_id=self.run.id,
                source=selector,
                source_manifest_sha256=source_hash,
            ),
            files,
            "grant-tool",
            "1.0.0",
            sum(f.size_bytes for f in files),
        )
        blob = _encode(frozen, self.creator.user_id, SnapshotStoreLimits())
        digest = hashlib.sha256(blob).hexdigest()
        return StoredSourceSnapshot(
            SnapshotReference("hs-" + digest, digest, len(blob), source_hash),
            self.creator.user_id,
            frozen,
        )

    async def publish(self, snapshot: StoredSourceSnapshot | None = None) -> SourceGrant:
        return await self.service.publish(
            self.session,
            self.creator,
            snapshot or self.snapshot(),
            plan_id=PLAN,
            plan_version=1,
            expires_at=NOW + timedelta(hours=1),
        )

    def user(self, principal: AuthenticatedPrincipal) -> UserRow:
        return next(
            row for row in self.db.rows if isinstance(row, UserRow) and row.id == principal.user_id
        )

    def pending_receipt(self, binding: GrantBinding) -> str:
        plan = PlanData(plugin_id=binding.plugin_id, plugin_version=binding.plugin_version)
        fingerprint = _plan_fingerprint(
            binding.run_id, binding.source, binding.source_manifest_sha256, plan
        )
        record = _HandoverRecord(
            tenant_id=binding.tenant_id,
            handover_id=binding.plan_id,
            run_id=binding.run_id,
            creator_id=binding.creator_id,
            installer_id=self.installer.user_id,
            version=2,
            status="pending",
            created_at=NOW,
            updated_at=NOW,
            expires_at=binding.expires_at,
            source=binding.source,
            source_manifest_sha256=binding.source_manifest_sha256,
            plugin_id=binding.plugin_id,
            plugin_version=binding.plugin_version,
            plan_data=plan,
            plan_hash=_digest("plan", {"handover_id": PLAN, "request_fingerprint": fingerprint}),
            request_fingerprint=fingerprint,
            plan_key_digest="3" * 64,
            signature_payload_sha256="4" * 64,
            archive_sha256="5" * 64,
            confirm_key_digest="6" * 64,
            confirm_request_sha256="7" * 64,
            key_id="key",
            verified_public_key_sha256="8" * 64,
            artifact_ref="artifact",
            receipt_id="gr-" + _digest("receipt", PLAN),
            audit_id="ga-" + _digest("audit", PLAN),
            committed_at=NOW,
            result="blocked_pending_approval",
        )
        self.db.rows.append(
            AdminResourceRow(
                id=uuid4(),
                tenant_id=binding.tenant_id,
                kind="plugin_handover",
                resource_id=PLAN,
                payload=record.model_dump(mode="json"),
            )
        )
        return cast(str, record.receipt_id)


@pytest.fixture
async def context(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Context]:
    # The real AsyncSession owns begin(); mocks intercept SQL before any connection.
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    db = SQLBoundary()
    async with AsyncSession(engine) as session, session.begin():
        monkeypatch.setattr(session, "scalar", AsyncMock(side_effect=db.scalar))
        monkeypatch.setattr(session, "execute", AsyncMock(side_effect=db.execute))
        yield Context(session, db)
    await engine.dispose()


async def test_publish_and_restart_verify_actual_authority_queries(context: Context) -> None:
    grant = await context.publish()
    assert grant.grant_id.startswith("sg-") and grant.state == "active"
    restored = await DurableSourceGrantService(clock=lambda: NOW).verify(
        context.session, context.installer, expected=grant.binding
    )
    assert restored == grant
    statements = context.db.statements
    shared = [
        s
        for s in statements
        if isinstance(s, Select) and s.column_descriptions[0]["entity"] in (UserRow, RunRow)
    ]
    assert shared and all("FOR SHARE" in str(s.compile(dialect=POSTGRES())) for s in shared)
    assert all(s.get_execution_options()["populate_existing"] for s in shared)
    first_lock = next(i for i, s in enumerate(statements) if isinstance(s, TextClause))
    first_row = next(i for i, s in enumerate(statements) if isinstance(s, Select))
    assert first_lock < first_row
    assert all(
        cast(Executable, s).get_execution_options().get("autoflush") is False
        for s in statements
        if not isinstance(s, TextClause)
    )


@pytest.mark.parametrize("artifact", [False, True])
async def test_asyncpg_row_uuid_publish_and_verify(context: Context, artifact: bool) -> None:
    def native_rows(statement: ClauseElement) -> None:
        if isinstance(statement, Insert):
            for row in context.db.rows:
                if isinstance(row, AdminResourceRow):
                    row.id = DriverUUID(str(row.id))
                    row.tenant_id = DriverUUID(str(row.tenant_id))

    context.db.after = native_rows
    grant = await context.publish(context.snapshot(artifact=artifact))
    assert grant.state == "active"
    assert grant.binding.tenant_id == context.creator.tenant_id
    assert (
        await DurableSourceGrantService(clock=lambda: NOW).verify(
            context.session, context.installer, expected=grant.binding
        )
        == grant
    )


async def test_grant_row_uuid_string_is_rejected(context: Context) -> None:
    grant = await context.publish()
    row = next(row for row in context.db.rows if isinstance(row, AdminResourceRow))
    row.id = cast(UUID, str(row.id))
    with pytest.raises(StorageUnavailable):
        await context.service.verify(context.session, context.installer, expected=grant.binding)


@pytest.mark.parametrize("field", ["user_id", "tenant_id"])
async def test_asyncpg_principal_uuid_is_rejected(context: Context, field: str) -> None:
    snapshot = context.snapshot()
    principal = context.creator
    context.creator = AuthenticatedPrincipal(
        DriverUUID(str(principal.user_id)) if field == "user_id" else principal.user_id,
        DriverUUID(str(principal.tenant_id)) if field == "tenant_id" else principal.tenant_id,
        principal.role,
    )
    with pytest.raises(PermissionDenied):
        await context.publish(snapshot)
    assert not context.db.statements


@pytest.mark.parametrize("artifact", [False, True])
async def test_both_source_kinds_publish_and_verify(context: Context, artifact: bool) -> None:
    grant = await context.publish(context.snapshot(artifact=artifact))
    assert (grant.binding.artifact is not None) is artifact
    assert (
        await context.service.verify(context.session, context.installer, expected=grant.binding)
        == grant
    )


@pytest.mark.parametrize("change", ["disabled", "role", "uuid", "tenant", "actor", "association"])
async def test_fresh_authority_not_principal_claims(context: Context, change: str) -> None:
    grant = await context.publish()
    user = context.user(context.creator)
    if change == "disabled":
        user.disabled = True
    elif change == "role":
        user.role = "viewer"
    elif change == "uuid":
        user.id = uuid4()
    elif change == "tenant":
        user.tenant_id = uuid4()
    elif change == "actor":
        context.run.actor_id = uuid4()
    else:
        context.run.routing_decision = {"project_id": "project", "workspace_session_id": "other"}
    with pytest.raises(PermissionDenied):
        await context.service.verify(context.session, context.installer, expected=grant.binding)
    assert not any(isinstance(s, Update) for s in context.db.statements)


@pytest.mark.parametrize("change", ["role", "disabled", "missing"])
async def test_installer_authority_from_current_db(context: Context, change: str) -> None:
    grant = await context.publish()
    user = context.user(context.installer)
    if change == "role":
        user.role = "operator"
    elif change == "disabled":
        user.disabled = True
    else:
        context.db.rows.remove(user)
    with pytest.raises(PermissionDenied):
        await context.service.verify(context.session, context.installer, expected=grant.binding)


async def test_artifact_row_actual_payload_and_column_binding(context: Context) -> None:
    grant = await context.publish(context.snapshot(artifact=True))
    artifact = next(row for row in context.db.rows if isinstance(row, RunArtifactRow))
    artifact.payload = {**artifact.payload, "producer": "changed"}
    with pytest.raises(PermissionDenied):
        await context.service.verify(context.session, context.installer, expected=grant.binding)


@pytest.mark.parametrize("operation", ["verify", "consume"])
@pytest.mark.parametrize("phase", ["registration", "run", "artifact"])
async def test_expiry_after_registration_wait_and_after_last_authority_read(
    context: Context, operation: str, phase: str
) -> None:
    grant = await context.publish(context.snapshot(artifact=phase == "artifact"))
    receipt = context.pending_receipt(grant.binding)
    reached = []

    def expire(statement: ClauseElement) -> None:
        if (phase == "registration" and isinstance(statement, TextClause)) or (
            isinstance(statement, Select)
            and statement.column_descriptions[0]["entity"]
            is {"run": RunRow, "artifact": RunArtifactRow}.get(phase)
        ):
            reached.append(phase)
            context.clock[0] = grant.binding.expires_at

    context.db.after = expire
    with pytest.raises(Conflict):
        if operation == "consume":
            await context.service.consume(
                context.session, context.installer, expected=grant.binding, receipt_id=receipt
            )
        else:
            await context.service.verify(context.session, context.installer, expected=grant.binding)
    assert reached == [phase]
    assert not any(isinstance(s, Update) for s in context.db.statements)


async def test_publish_same_slot_idempotent_conflicting_binding_never_replaces(
    context: Context,
) -> None:
    snapshot = context.snapshot()
    first = await context.publish(snapshot)
    assert await context.publish(snapshot) == first
    with pytest.raises(Conflict):
        await context.service.publish(
            context.session,
            context.creator,
            snapshot,
            plan_id=PLAN,
            plan_version=1,
            expires_at=NOW + timedelta(minutes=30),
        )
    assert (
        await context.service.verify(context.session, context.installer, expected=first.binding)
        == first
    )


async def test_revoke_locks_only_sg_not_plan_or_registration(context: Context) -> None:
    grant = await context.publish()
    context.db.statements.clear()
    revoked = await context.service.revoke(
        context.session, context.creator, grant_id=grant.grant_id
    )
    assert revoked.state == "revoked"
    assert not any(isinstance(s, TextClause) for s in context.db.statements)
    assert not any(
        isinstance(s, Select) and s.column_descriptions[0]["entity"] is RunRow
        for s in context.db.statements
    )
    assert (
        await context.service.revoke(context.session, context.creator, grant_id=grant.grant_id)
        == revoked
    )
    with pytest.raises(Conflict):
        await context.service.verify(context.session, context.installer, expected=grant.binding)


async def test_consume_requires_actual_pending_receipt_and_is_idempotent(context: Context) -> None:
    grant = await context.publish()
    receipt = "gr-" + _digest("receipt", PLAN)
    with pytest.raises(Conflict):
        await context.service.consume(
            context.session, context.installer, expected=grant.binding, receipt_id=receipt
        )
    context.pending_receipt(grant.binding)
    consumed = await context.service.consume(
        context.session, context.installer, expected=grant.binding, receipt_id=receipt
    )
    assert consumed.state == "consumed" and consumed.receipt_id == receipt
    assert (
        await context.service.consume(
            context.session, context.installer, expected=grant.binding, receipt_id=receipt
        )
        == consumed
    )
    with pytest.raises(Conflict):
        await context.service.consume(
            context.session, context.installer, expected=grant.binding, receipt_id="wrong-receipt"
        )


@pytest.mark.parametrize(
    "change", ["discriminator", "tenant", "hash", "planhash", "schema_bool", "noncanonical_time"]
)
async def test_persisted_grant_tamper_fails_closed(context: Context, change: str) -> None:
    grant = await context.publish()
    row = next(row for row in context.db.rows if isinstance(row, AdminResourceRow))
    data = deepcopy(row.payload)
    if change == "discriminator":
        data["record_type"] = "plan"
    elif change == "tenant":
        cast(dict[str, object], data["binding"])["tenant_id"] = str(uuid4())
    elif change == "hash":
        data["binding_sha256"] = "f" * 64
    elif change == "planhash":
        cast(dict[str, object], data["binding"])["final_plan_hash"] = "f" * 64
    elif change == "schema_bool":
        data["schema_version"] = True
    else:
        data["created_at"] = "2026-10-12T10:00:00+00:00"
    row.payload = data
    with pytest.raises(StorageUnavailable):
        await context.service.verify(context.session, context.installer, expected=grant.binding)


async def test_forged_snapshot_reference_and_forged_creator_not_proof(context: Context) -> None:
    stored = context.snapshot()
    forged = StoredSourceSnapshot(stored.reference, uuid4(), stored.snapshot)
    with pytest.raises((ValueError, PermissionDenied)):
        await context.publish(forged)
    bad = StoredSourceSnapshot(
        SnapshotReference(
            stored.reference.snapshot_id,
            "f" * 64,
            stored.reference.blob_size_bytes,
            stored.reference.source_manifest_sha256,
        ),
        stored.creator_id,
        stored.snapshot,
    )
    with pytest.raises(ValueError):
        await context.publish(bad)


async def test_noncaller_transaction_and_sqlite_rejected_without_sql() -> None:
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    async with AsyncSession(engine) as session:
        context = Context(session, SQLBoundary())
        with pytest.raises(StorageUnavailable):
            await context.publish()
        session.add(UserRow(id=uuid4(), tenant_id=uuid4(), username="fake", role="viewer"))
        with pytest.raises(StorageUnavailable):
            await context.publish()
        await session.rollback()
        async with session.begin():
            original = engine.dialect.name
            engine.dialect.name = "sqlite"
            try:
                with pytest.raises(StorageUnavailable):
                    await context.publish()
            finally:
                engine.dialect.name = original
    await engine.dispose()


@pytest.mark.parametrize("change", ["missing", "id", "tenant", "run", "content_sha"])
async def test_actual_artifact_query_fail_closed(context: Context, change: str) -> None:
    stored = context.snapshot(artifact=True)
    row = next(row for row in context.db.rows if isinstance(row, RunArtifactRow))
    if change == "missing":
        context.db.rows.remove(row)
    elif change == "id":
        row.id = uuid4()
    elif change == "tenant":
        row.tenant_id = uuid4()
    elif change == "run":
        row.run_id = uuid4()
    else:
        row.content_sha256 = "f" * 64
    with pytest.raises(PermissionDenied):
        await context.publish(stored)
    assert not any(isinstance(s, Insert) for s in context.db.statements)


async def test_cross_tenant_and_non_sg_discriminator_rejected(context: Context) -> None:
    grant = await context.publish()
    other = AuthenticatedPrincipal(context.installer.user_id, uuid4(), Role.SUPER_ADMIN)
    with pytest.raises(PermissionDenied):
        await context.service.verify(context.session, other, expected=grant.binding)
    for grant_id in (PLAN, "sg-short", grant.grant_id.upper()):
        with pytest.raises(PermissionDenied):
            await context.service.revoke(context.session, context.creator, grant_id=grant_id)


async def test_grant_row_uuid_payload_cas_failure_is_not_success(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = await context.publish()
    original = context.db.scalar

    async def lose_update(statement: ClauseElement) -> object:
        if isinstance(statement, Update):
            context.db.statements.append(statement)
            return None
        return await original(statement)

    monkeypatch.setattr(context.session, "scalar", AsyncMock(side_effect=lose_update))
    with pytest.raises(Conflict):
        await context.service.revoke(context.session, context.creator, grant_id=grant.grant_id)
    current = await context.service.verify(
        context.session, context.installer, expected=grant.binding
    )
    assert current == grant


async def test_revoke_consumed_keeps_receipt_and_blob_binding(context: Context) -> None:
    grant = await context.publish()
    receipt = context.pending_receipt(grant.binding)
    consumed = await context.service.consume(
        context.session, context.installer, expected=grant.binding, receipt_id=receipt
    )
    revoked = await context.service.revoke(
        context.session, context.creator, grant_id=grant.grant_id
    )
    assert revoked.state == "revoked" and revoked.receipt_id == consumed.receipt_id
    assert revoked.consumed_by == consumed.consumed_by and revoked.binding == grant.binding


@pytest.mark.parametrize("change", ["bad_receipt", "history"])
async def test_mutable_history_corruption_not_trusted(context: Context, change: str) -> None:
    grant = await context.publish()
    row = next(row for row in context.db.rows if isinstance(row, AdminResourceRow))
    data = grant.model_dump(mode="json")
    data.update(
        state="consumed",
        consumed_at=NOW.isoformat().replace("+00:00", "Z"),
        consumed_by=str(context.installer.user_id),
        receipt_id="wrong-receipt",
    )
    if change == "history":
        data.update(
            state="revoked",
            receipt_id="gr-" + _digest("receipt", PLAN),
            consumed_at=(NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            revoked_at=(NOW + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            revoked_by=str(context.creator.user_id),
        )
    row.payload = data
    with pytest.raises(StorageUnavailable):
        await context.service.revoke(context.session, context.creator, grant_id=grant.grant_id)


async def test_invalid_snapshot_shape_denied_before_db(context: Context) -> None:
    stored = context.snapshot()
    invalid = StoredSourceSnapshot(
        stored.reference, stored.creator_id, cast(FrozenSourceSnapshot, object())
    )
    with pytest.raises(ValueError):
        await context.publish(invalid)
    assert not context.db.statements


@pytest.mark.parametrize("operation", ["verify", "consume"])
@pytest.mark.parametrize("creator_first", [False, True])
async def test_authority_share_locks_follow_stable_uuid_order(
    context: Context, operation: str, creator_first: bool
) -> None:
    creator_row, installer_row = context.user(context.creator), context.user(context.installer)
    creator_id = UUID(int=1 if creator_first else 2)
    installer_id = UUID(int=2 if creator_first else 1)
    creator_row.id, installer_row.id = creator_id, installer_id
    context.creator = AuthenticatedPrincipal(creator_id, context.creator.tenant_id, Role.OPERATOR)
    context.installer = AuthenticatedPrincipal(installer_id, context.creator.tenant_id, Role.ADMIN)
    context.run.actor_id = creator_id
    grant = await context.publish()
    receipt = context.pending_receipt(grant.binding)
    context.db.statements.clear()
    if operation == "consume":
        await context.service.consume(
            context.session, context.installer, expected=grant.binding, receipt_id=receipt
        )
    else:
        await context.service.verify(context.session, context.installer, expected=grant.binding)
    queries = [
        s
        for s in context.db.statements
        if isinstance(s, Select) and s.column_descriptions[0]["entity"] is UserRow
    ]
    assert [context.db.params(s)["id_1"] for s in queries] == [UUID(int=1), UUID(int=2)]
    assert all("FOR SHARE" in str(s.compile(dialect=POSTGRES())) for s in queries)
    assert all(s.get_execution_options()["populate_existing"] for s in queries)


@pytest.mark.parametrize("operation", ["verify", "consume"])
async def test_same_actor_queries_once_with_combined_current_permissions(
    context: Context, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    context.db.rows.remove(context.user(context.installer))
    context.user(context.creator).role = "admin"
    context.installer = context.creator
    grant = await context.publish()
    receipt = context.pending_receipt(grant.binding)
    user = AsyncMock(wraps=context.service._user)
    monkeypatch.setattr(context.service, "_user", user)
    context.db.statements.clear()
    if operation == "consume":
        await context.service.consume(
            context.session, context.installer, expected=grant.binding, receipt_id=receipt
        )
    else:
        await context.service.verify(context.session, context.installer, expected=grant.binding)
    assert user.await_count == 1
    args = user.await_args_list[0].args
    assert args[:3] == (context.session, context.creator.tenant_id, context.creator.user_id)
    assert set(args[3:]) == {"run:create", "run:read", "plugin:read", "plugin:write"}
    context.user(context.creator).role = "operator"
    with pytest.raises(PermissionDenied):
        if operation == "consume":
            await context.service.consume(
                context.session, context.installer, expected=grant.binding, receipt_id=receipt
            )
        else:
            await context.service.verify(context.session, context.installer, expected=grant.binding)
