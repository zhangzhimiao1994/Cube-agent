"""Real admin SQL checks shared with the pinned, pytest-free native runner.

Pytest requires AGENT_HUB_TEST_DATABASE_URL and
AGENT_HUB_HANDOVER_TEST_DB_ISOLATED=1. Its inherited fixtures migrate to
Alembic head and clean the isolated database. Only the pytest adapter is
optional; native checks use the actual admin service and independent sessions.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import Table, delete, event, func, select, text, update
from sqlalchemy.engine import AdaptedConnection, Connection, make_url
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert
from starlette.requests import Request
from starlette.types import Message

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers import admin
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.config.service import ConfigService
from agent_hub.db.models import AdminResourceRow, TenantRow
from agent_hub.plugins.handover_storage import acquire_plugin_registration_lock
from agent_hub.security.secrets import SecretCipher, SecretService

PLUGIN_ID = "generated-tool"
CONTENT_SHA256 = "1" * 64
type Action = Literal["disable", "stop", "approve"]
type AuditPhase = Literal["before", "after"]
type Replacement = Literal["row_id", "marker", "payload"]
type WriteAction = Action | Literal["upsert"]
type RowKey = tuple[UUID, str, str]
type ArchivePause = Literal["before_insert", "after_insert_before_commit"]
type PackageRace = Literal["absent", "same_sha", "different_sha"]
type RestorePublishRace = Literal["same_sha", "different_sha"]
type RegisteredRestoreFault = Literal["none", "displace", "restore"]


@dataclass(frozen=True)
class SavedRow:
    row_id: UUID
    payload: dict[str, object]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AdminContext:
    factory: async_sessionmaker[AsyncSession]
    service: admin.PersistentAdminResourceService
    principal: AuthenticatedPrincipal
    other_tenant_id: UUID


def plugin_payload() -> dict[str, object]:
    plugin = admin.PluginResourceResponse(
        id=PLUGIN_ID,
        name="Isolated admin SQL test",
        version="1.0.0",
        enabled=True,
        status="running",
        health="healthy",
        source_filename="generated.zip",
        content_sha256=CONTENT_SHA256,
        package_metadata=admin.PluginPackageMetadata.model_validate(
            {
                "kind": "adapter_package",
                "package_version": "1.0.0",
                "runtime": "python",
                "entrypoint": "adapter.py",
                "isolation": "local_process",
            }
        ),
    )
    payload: dict[str, object] = plugin.model_dump(mode="json")
    payload["_generated_handover_id"] = "gh-" + "2" * 64
    return payload


async def create_context(factory: async_sessionmaker[AsyncSession]) -> AdminContext:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.ADMIN)
    other_tenant_id = uuid4()
    async with factory() as session, session.begin():
        for tenant_id in (principal.tenant_id, other_tenant_id):
            session.add(TenantRow(id=tenant_id, slug=f"admin-native-{uuid4().hex}", name="Test"))
            session.add(
                AdminResourceRow(
                    tenant_id=tenant_id,
                    kind="plugin",
                    resource_id=PLUGIN_ID,
                    payload=plugin_payload(),
                )
            )
            session.add(
                AdminResourceRow(
                    tenant_id=tenant_id,
                    kind="plugin_handover",
                    resource_id="old-receipt",
                    payload={"status": "pending", "history": "keep"},
                )
            )
    service = admin.PersistentAdminResourceService(
        config_service=ConfigService(factory),
        secret_service=SecretService(factory, SecretCipher(b"x" * 32)),
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        session_factory=factory,
    )
    return AdminContext(factory, service, principal, other_tenant_id)


async def cleanup_context(context: AdminContext) -> None:
    tenants = (context.principal.tenant_id, context.other_tenant_id)
    async with context.factory() as session, session.begin():
        await session.execute(
            delete(AdminResourceRow).where(AdminResourceRow.tenant_id.in_(tenants))
        )
        await session.execute(delete(TenantRow).where(TenantRow.id.in_(tenants)))


async def saved_rows(context: AdminContext) -> dict[RowKey, SavedRow]:
    async with context.factory() as session:
        rows = await session.scalars(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id.in_(
                    (context.principal.tenant_id, context.other_tenant_id)
                )
            )
        )
        return {
            (row.tenant_id, row.kind, row.resource_id): SavedRow(
                row.id, deepcopy(row.payload), row.created_at, row.updated_at
            )
            for row in rows
        }


async def mutate(context: AdminContext, action: Action) -> admin.PluginResourceResponse:
    kwargs = {"tenant_id": context.principal.tenant_id, "actor_id": context.principal.user_id}
    if action == "approve":
        return await context.service.approve_plugin_package(
            PLUGIN_ID, admin.PluginPackageApprovalRequest(), **kwargs
        )
    if action == "disable":
        return await context.service.disable_plugin(PLUGIN_ID, **kwargs)
    return await context.service.stop_plugin(PLUGIN_ID, **kwargs)


async def check_audit_rollback(context: AdminContext, action: Action, phase: AuditPhase) -> None:
    before = await saved_rows(context)
    tenant_id = context.principal.tenant_id
    async with context.factory() as session:
        engine = session.get_bind()
    transaction_ids: list[int] = []
    written_payloads: list[dict[str, object]] = []
    audit_counts: list[int] = []

    def inserted_kind(execution: DefaultExecutionContext) -> str | None:
        compiled = execution.compiled
        if compiled is None or not isinstance(compiled.statement, Insert):
            return None
        if cast(Table, compiled.statement.table).name != AdminResourceRow.__tablename__:
            return None
        for params in execution.compiled_parameters:
            if params.get("tenant_id") == tenant_id:
                kind = params.get("kind")
                return kind if isinstance(kind, str) else None
        return None

    def observe_plugin_write(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        execution: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        if inserted_kind(execution) != "plugin":
            return
        transaction_ids.append(
            cast(int, connection.exec_driver_sql("SELECT txid_current()").scalar_one())
        )
        written_payloads.append(
            deepcopy(
                connection.execute(
                    select(AdminResourceRow.payload).where(
                        AdminResourceRow.tenant_id == tenant_id,
                        AdminResourceRow.kind == "plugin",
                        AdminResourceRow.resource_id == PLUGIN_ID,
                    )
                ).scalar_one()
            )
        )

    def fail_audit_in_postgres(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        execution: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        if inserted_kind(execution) != "audit":
            return
        transaction_ids.append(
            cast(int, connection.exec_driver_sql("SELECT txid_current()").scalar_one())
        )
        audit_counts.append(
            connection.execute(
                select(func.count())
                .select_from(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == tenant_id,
                    AdminResourceRow.kind == "audit",
                )
            ).scalar_one()
        )
        # The server aborts this real transaction, rather than a fake session raising an error.
        connection.exec_driver_sql("SELECT 1 / 0")

    event_name = "before_cursor_execute" if phase == "before" else "after_cursor_execute"
    event.listen(engine, "after_cursor_execute", observe_plugin_write)
    event.listen(engine, event_name, fail_audit_in_postgres)
    try:
        try:
            await asyncio.wait_for(mutate(context, action), timeout=15)
        except DBAPIError as error:
            assert getattr(error.orig, "sqlstate", None) == "22012"
        else:
            raise AssertionError("audit fault was not reached")
    finally:
        event.remove(engine, event_name, fail_audit_in_postgres)
        event.remove(engine, "after_cursor_execute", observe_plugin_write)
    assert len(transaction_ids) == 2 and transaction_ids[0] == transaction_ids[1]
    assert len(written_payloads) == 1
    assert written_payloads[0] != before[(tenant_id, "plugin", PLUGIN_ID)].payload
    assert audit_counts == [0 if phase == "before" else 1]
    assert await saved_rows(context) == before

    # The same service can retry; exactly one correctly scoped audit commits with the state.
    result = await asyncio.wait_for(mutate(context, action), timeout=15)
    after = await saved_rows(context)
    assert (
        after[(tenant_id, "plugin", PLUGIN_ID)].row_id
        == before[(tenant_id, "plugin", PLUGIN_ID)].row_id
    )
    assert (
        after[(tenant_id, "plugin", PLUGIN_ID)].payload["_generated_handover_id"]
        == "gh-" + "2" * 64
    )
    audits = [row.payload for key, row in after.items() if key[1] == "audit"]
    assert len(audits) == 1
    assert audits[0]["action"] == (
        "plugin.package.approved" if action == "approve" else f"plugin.{action}"
    )
    assert audits[0]["actor"] == str(context.principal.user_id)
    assert audits[0]["resource"] == f"plugin:{PLUGIN_ID}"
    assert len(after) == len(before) + 1
    for key, row in before.items():
        if key != (tenant_id, "plugin", PLUGIN_ID):
            assert after[key] == row
    assert "_generated_handover_id" not in result.model_dump()


async def wait_for_blocked_registration[T](
    blocker: AsyncSession, contender: asyncio.Task[T]
) -> None:
    query = text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks held JOIN pg_locks waiting
              ON held.locktype = waiting.locktype
             AND held.database IS NOT DISTINCT FROM waiting.database
             AND held.classid = waiting.classid AND held.objid = waiting.objid
             AND held.objsubid = waiting.objsubid
            WHERE held.locktype = 'advisory' AND held.granted
              AND held.pid = pg_backend_pid() AND NOT waiting.granted
        )
    """)
    async with asyncio.timeout(10):
        while not await blocker.scalar(query):
            assert not contender.done(), "uninstall bypassed the held registration lock"
            await asyncio.sleep(0.01)
    assert not contender.done()


async def check_registration_replacement(
    context: AdminContext,
    store_root: Path,
    changed: Replacement,
    active_replacement: bool,
) -> None:
    before = await saved_rows(context)
    tenant_id = context.principal.tenant_id
    plugin_key = (tenant_id, "plugin", PLUGIN_ID)
    original = before[plugin_key]
    replacement = deepcopy(original.payload)
    row_id = uuid4() if changed == "row_id" else original.row_id
    if changed == "marker":
        replacement["_generated_handover_id"] = "gh-" + "4" * 64
    elif changed == "payload":
        replacement["content_sha256"] = "3" * 64
    if changed == "row_id":
        assert replacement == original.payload  # Identical content is still a new registration.
    if changed == "marker":
        assert admin._plugin_response_from_payload(
            replacement
        ) == admin._plugin_response_from_payload(original.payload)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(plugin_package_store_dir=store_root),
                )
            ),
        }
    )
    package_root = store_root / str(tenant_id) / PLUGIN_ID
    artifact = package_root / CONTENT_SHA256 / "adapter.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"old P")
    pending = store_root / ".uninstalling" / str(tenant_id) / PLUGIN_ID
    runtime_calls: list[UUID] = []

    async def no_runtime_reload(request: Request, tenant_id: UUID) -> None:
        runtime_calls.append(tenant_id)

    contender: asyncio.Task[admin.PluginUninstallResponse] | None = None
    with patch.object(admin, "_reload_plugin_runtime_config_strict", new=no_runtime_reload):
        try:
            async with context.factory() as blocker, blocker.begin():
                await acquire_plugin_registration_lock(blocker, tenant_id, PLUGIN_ID)
                contender = asyncio.create_task(
                    admin.uninstall_plugin(
                        PLUGIN_ID,
                        request,
                        context.principal,
                        context.service,
                    )
                )
                await wait_for_blocked_registration(blocker, contender)
                assert artifact.read_bytes() == b"old P"
                assert not pending.exists()
                old_plugin, _ = await context.service._plugin_for_uninstall(
                    PLUGIN_ID, tenant_id=tenant_id
                )
                old = admin._stage_plugin_package_uninstall(
                    request, old_plugin, tenant_id=tenant_id
                )
                assert not artifact.exists()
                assert (
                    pending / "artifact" / CONTENT_SHA256 / "adapter.py"
                ).read_bytes() == b"old P"
                if changed == "row_id":
                    await blocker.execute(
                        delete(AdminResourceRow).where(
                            AdminResourceRow.tenant_id == tenant_id,
                            AdminResourceRow.kind == "plugin",
                            AdminResourceRow.resource_id == PLUGIN_ID,
                        )
                    )
                    blocker.add(
                        AdminResourceRow(
                            id=row_id,
                            tenant_id=tenant_id,
                            kind="plugin",
                            resource_id=PLUGIN_ID,
                            payload=replacement,
                        )
                    )
                else:
                    await blocker.execute(
                        update(AdminResourceRow)
                        .where(
                            AdminResourceRow.id == row_id,
                        )
                        .values(payload=replacement)
                    )
                blocker.add(
                    AdminResourceRow(
                        tenant_id=tenant_id,
                        kind="plugin_handover",
                        resource_id="new-receipt",
                        payload={"status": "pending", "history": "new Q"},
                    )
                )
                await blocker.flush()
                if active_replacement:
                    artifact.parent.mkdir(parents=True)
                    artifact.write_bytes(b"new Q")
                # A separate observer sees only P until B commits its replacement.
                assert await saved_rows(context) == before
            try:
                await asyncio.wait_for(contender, timeout=10)
            except PublicAPIError as error:
                assert error.status_code == 409 and error.code == "plugin_registration_conflict"
            else:
                raise AssertionError("stale uninstall was not rejected")
        finally:
            if contender is not None:
                if not contender.done():
                    contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
        after = await saved_rows(context)
        assert after[plugin_key].row_id == row_id and after[plugin_key].payload == replacement
        assert after[(tenant_id, "plugin_handover", "new-receipt")].payload == {
            "status": "pending",
            "history": "new Q",
        }
        assert len(after) == len(before) + 1
        assert runtime_calls == []
        assert not any(key[1] == "audit" for key in after)
        for key, row in before.items():
            if key != plugin_key:
                assert after[key] == row
        assert (pending / "artifact" / CONTENT_SHA256 / "adapter.py").read_bytes() == b"old P"
        if active_replacement:
            assert artifact.read_bytes() == b"new Q"
        else:
            assert not package_root.exists()
        response = await asyncio.wait_for(
            admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service),
            timeout=15,
        )
        assert response.status == "uninstalled" and runtime_calls == [tenant_id]
        backups = list((store_root / ".uninstalling").glob("c-*/p/state.json"))
        assert len(backups) == 1
        backup_file = backups[0].parent / "artifact" / CONTENT_SHA256 / "adapter.py"
        assert backup_file.read_bytes() == b"old P"
        assert len(str(backups[0].parent.relative_to(store_root / ".uninstalling"))) <= 14
        assert admin._read_plugin_uninstall_state(
            backups[0],
            tenant_id=tenant_id,
            plugin_id=PLUGIN_ID,
        ) == (old.operation_id, old.plugin_snapshot_sha256, old.created_at)
        try:
            admin._read_plugin_uninstall_state(
                backups[0], tenant_id=context.other_tenant_id, plugin_id=PLUGIN_ID
            )
        except ValueError:
            pass
        else:
            raise AssertionError("backup accepted another tenant")
        assert not artifact.exists() and backup_file.read_bytes() == b"old P"
        final = await saved_rows(context)
        assert plugin_key not in final
        audits = [(key, row) for key, row in final.items() if key[1] == "audit"]
        assert len(audits) == 1 and audits[0][0][0] == tenant_id
        assert audits[0][1].payload["action"] == "plugin.uninstall"
        for key, row in after.items():
            if key != plugin_key:
                assert final[key] == row


def package_request(store_root: Path) -> Request:
    return Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(plugin_package_store_dir=store_root))
            ),
        }
    )


async def replace_registration(
    session: AsyncSession,
    tenant_id: UUID,
    payload: dict[str, object],
    row_id: UUID,
) -> None:
    await session.execute(
        delete(AdminResourceRow).where(
            AdminResourceRow.tenant_id == tenant_id,
            AdminResourceRow.kind == "plugin",
            AdminResourceRow.resource_id == PLUGIN_ID,
        )
    )
    session.add(
        AdminResourceRow(
            id=row_id,
            tenant_id=tenant_id,
            kind="plugin",
            resource_id=PLUGIN_ID,
            payload=deepcopy(payload),
        )
    )
    await session.flush()


async def check_registration_write_aba(
    context: AdminContext,
    action: WriteAction,
    generated: bool,
) -> None:
    tenant_id = context.principal.tenant_id
    plugin_key = (tenant_id, "plugin", PLUGIN_ID)
    if not generated:
        async with context.factory() as session, session.begin():
            await acquire_plugin_registration_lock(session, tenant_id, PLUGIN_ID)
            row = (
                await session.scalars(
                    select(AdminResourceRow).where(
                        AdminResourceRow.tenant_id == tenant_id,
                        AdminResourceRow.kind == "plugin",
                        AdminResourceRow.resource_id == PLUGIN_ID,
                    )
                )
            ).one()
            payload = dict(row.payload)
            payload.pop("_generated_handover_id")
            row.payload = payload
    before = await saved_rows(context)
    replacement = before[plugin_key].payload
    q_id = uuid4()

    async def write() -> admin.PluginResourceResponse:
        if action == "upsert":
            return await context.service.upsert_plugin(
                admin.PluginResourceRequest(id=PLUGIN_ID, name="Edited"),
                tenant_id=tenant_id,
                actor_id=context.principal.user_id,
            )
        return await mutate(context, action)

    contender: asyncio.Task[admin.PluginResourceResponse] | None = None
    try:
        async with context.factory() as blocker, blocker.begin():
            await acquire_plugin_registration_lock(blocker, tenant_id, PLUGIN_ID)
            contender = asyncio.create_task(write())
            await wait_for_blocked_registration(blocker, contender)
            await replace_registration(blocker, tenant_id, replacement, q_id)
            assert await saved_rows(context) == before
        try:
            await asyncio.wait_for(contender, timeout=10)
        except PublicAPIError as error:
            assert error.status_code == 409 and error.code == "plugin_registration_conflict"
        else:
            raise AssertionError("identical JSON ABA write was accepted")
    finally:
        if contender is not None:
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
    after = await saved_rows(context)
    assert set(after) == set(before)
    assert after[plugin_key].row_id == q_id
    assert after[plugin_key].payload == replacement
    for key, saved in before.items():
        if key != plugin_key:
            assert after[key] == saved


async def check_api_retry_preserves_old_pending(
    context: AdminContext,
    store_root: Path,
    active_replacement: bool,
) -> None:
    tenant_id = context.principal.tenant_id
    request = package_request(store_root)
    before = await saved_rows(context)
    key = (tenant_id, "plugin", PLUGIN_ID)
    package_root = store_root / str(tenant_id) / PLUGIN_ID
    artifact = package_root / "adapter.py"
    package_root.mkdir(parents=True)
    artifact.write_bytes(b"old P")
    plugin, _ = await context.service._plugin_for_uninstall(PLUGIN_ID, tenant_id=tenant_id)
    old = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
    q_id = uuid4()
    async with context.factory() as replacement, replacement.begin():
        await acquire_plugin_registration_lock(replacement, tenant_id, PLUGIN_ID)
        await replace_registration(replacement, tenant_id, before[key].payload, q_id)
    if active_replacement:
        package_root.mkdir(parents=True)
        artifact.write_bytes(b"new Q")
    committed_q = await saved_rows(context)
    rename = Path.rename

    def fail_backup(path: Path, target: Path) -> Path:
        if path == old.pending_root:
            raise OSError("temporary rename failure")
        return rename(path, target)

    with patch.object(Path, "rename", new=fail_backup):
        try:
            await admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service)
        except PublicAPIError as error:
            assert error.status_code == 503
        else:
            raise AssertionError("preservation failure was not retryable")
    assert await saved_rows(context) == committed_q
    assert (old.pending_root / "artifact" / "adapter.py").read_bytes() == b"old P"
    assert (artifact.read_bytes() == b"new Q") if active_replacement else not package_root.exists()

    async def no_runtime_reload(_request: Request, _tenant: UUID) -> None:
        return None

    with patch.object(admin, "_reload_plugin_runtime_config_strict", new=no_runtime_reload):
        response = await admin.uninstall_plugin(
            PLUGIN_ID, request, context.principal, context.service
        )
    assert response.status == "uninstalled"
    backups = list((store_root / ".uninstalling").glob("c-*/p/state.json"))
    assert len(backups) == 1
    assert (backups[0].parent / "artifact" / "adapter.py").read_bytes() == b"old P"
    assert admin._read_plugin_uninstall_state(
        backups[0], tenant_id=tenant_id, plugin_id=PLUGIN_ID
    ) == (old.operation_id, old.plugin_snapshot_sha256, old.created_at)
    assert not artifact.exists() and not old.pending_root.exists()
    after = await saved_rows(context)
    assert key not in after
    audits = [row for row_key, row in after.items() if row_key[1] == "audit"]
    assert len(audits) == 1 and audits[0].payload["action"] == "plugin.uninstall"
    for row_key, row in committed_q.items():
        if row_key != key:
            assert after[row_key] == row


async def check_delayed_finish_preserves_next_operation(
    context: AdminContext, store_root: Path
) -> None:
    tenant_id = context.principal.tenant_id
    request = package_request(store_root)
    package_root = store_root / str(tenant_id) / PLUGIN_ID
    package_root.mkdir(parents=True)
    (package_root / "adapter.py").write_bytes(b"old P")
    replacement = plugin_payload()
    q_id = uuid4()
    calls = 0
    staged_q: list[admin._PluginPackageUninstallState] = []

    async def reload_and_replace(_request: Request, _tenant: UUID) -> None:
        nonlocal calls
        calls += 1
        if calls != 1:
            return
        retry = await admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service)
        assert retry.status == "uninstalled"
        async with context.factory() as session, session.begin():
            await acquire_plugin_registration_lock(session, tenant_id, PLUGIN_ID)
            await replace_registration(session, tenant_id, replacement, q_id)
        package_root.mkdir(parents=True)
        (package_root / "adapter.py").write_bytes(b"new Q")
        plugin, _ = await context.service._plugin_for_uninstall(PLUGIN_ID, tenant_id=tenant_id)
        staged_q.append(admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id))

    with patch.object(admin, "_reload_plugin_runtime_config_strict", new=reload_and_replace):
        response = await admin.uninstall_plugin(
            PLUGIN_ID, request, context.principal, context.service
        )
    assert response.status == "uninstalled"
    assert (staged_q[0].pending_root / "artifact" / "adapter.py").read_bytes() == b"new Q"
    assert admin._read_plugin_uninstall_state(
        staged_q[0].pending_root / "state.json", tenant_id=tenant_id, plugin_id=PLUGIN_ID
    ) == (staged_q[0].operation_id, staged_q[0].plugin_snapshot_sha256, staged_q[0].created_at)
    after = await saved_rows(context)
    assert after[(tenant_id, "plugin", PLUGIN_ID)].row_id == q_id
    assert after[(tenant_id, "plugin", PLUGIN_ID)].payload == replacement
    assert sum(key[1] == "audit" for key in after) == 1


async def check_archive_conflict_preserves_shared_artifact(
    context: AdminContext,
    store_root: Path,
    generated: bool,
) -> None:
    tenant_id = context.principal.tenant_id
    key = (tenant_id, "plugin", PLUGIN_ID)
    async with context.factory() as session, session.begin():
        await acquire_plugin_registration_lock(session, tenant_id, PLUGIN_ID)
        row = (
            await session.scalars(
                select(AdminResourceRow).where(
                    AdminResourceRow.tenant_id == tenant_id,
                    AdminResourceRow.kind == "plugin",
                    AdminResourceRow.resource_id == PLUGIN_ID,
                )
            )
        ).one()
        ordinary = dict(row.payload)
        ordinary.pop("_generated_handover_id")
        row.payload = ordinary
    before = await saved_rows(context)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.json", json.dumps({"id": PLUGIN_ID, "name": "Installed"}))
        package.writestr("adapter.py", b"shared artifact")
    body = archive.getvalue()
    sha = hashlib.sha256(body).hexdigest()

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-agent-hub-plugin-filename", b"plugin.zip")],
            "app": package_request(store_root).app,
        },
        receive,
    )
    # Only crypto verification is supplied; extraction, transactions, lock wait, and CAS are real.
    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64
    )
    artifact = store_root / str(tenant_id) / PLUGIN_ID / sha / "adapter.py"
    q_id = uuid4()
    contender: asyncio.Task[admin.PluginArchiveInstallResponse] | None = None
    replacement = plugin_payload() if generated else deepcopy(ordinary)
    replacement["content_sha256"] = sha
    with patch.object(admin, "_verified_plugin_package_metadata", return_value=verified):
        try:
            async with context.factory() as blocker, blocker.begin():
                await acquire_plugin_registration_lock(blocker, tenant_id, PLUGIN_ID)
                contender = asyncio.create_task(
                    admin.install_plugin_archive(request, context.principal, context.service)
                )
                await wait_for_blocked_registration(blocker, contender)
                assert artifact.read_bytes() == b"shared artifact"
                metadata = admin._stored_plugin_package_metadata(
                    request,
                    tenant_id=tenant_id,
                    plugin_id=PLUGIN_ID,
                    package=verified,
                    archive_bytes=body,
                    content_sha256=sha,
                )
                assert metadata.package is not None and not metadata.artifact_created
                replacement["package_metadata"] = metadata.package.model_dump(mode="json")
                await replace_registration(blocker, tenant_id, replacement, q_id)
                assert await saved_rows(context) == before
            try:
                await asyncio.wait_for(contender, timeout=10)
            except PublicAPIError as error:
                assert error.status_code == 409 and error.code == "plugin_registration_conflict"
            else:
                raise AssertionError("stale archive install was accepted")
        finally:
            if contender is not None:
                if not contender.done():
                    contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
    after = await saved_rows(context)
    assert set(after) == set(before)
    assert after[key].row_id == q_id and after[key].payload == replacement
    assert artifact.read_bytes() == b"shared artifact"
    for row_key, saved in before.items():
        if row_key != key:
            assert after[row_key] == saved


def archive_request(context: AdminContext, store_root: Path) -> tuple[Request, bytes, Path]:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.json", json.dumps({"id": PLUGIN_ID, "name": "Installed Q"}))
        package.writestr("adapter.py", b"new Q")
    body = archive.getvalue()

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "headers": [(b"x-agent-hub-plugin-filename", b"plugin.zip")],
            "app": package_request(store_root).app,
        },
        receive,
    )
    artifact = (
        store_root
        / str(context.principal.tenant_id)
        / PLUGIN_ID
        / hashlib.sha256(body).hexdigest()
        / "adapter.py"
    )
    return request, body, artifact


async def make_ordinary_registration(context: AdminContext, *, absent: bool = False) -> None:
    tenant_id = context.principal.tenant_id
    async with context.factory() as session, session.begin():
        await acquire_plugin_registration_lock(session, tenant_id, PLUGIN_ID)
        row = (
            await session.scalars(
                select(AdminResourceRow).where(
                    AdminResourceRow.tenant_id == tenant_id,
                    AdminResourceRow.kind == "plugin",
                    AdminResourceRow.resource_id == PLUGIN_ID,
                )
            )
        ).one()
        if absent:
            await session.delete(row)
        else:
            payload = dict(row.payload)
            payload.pop("_generated_handover_id")
            row.payload = payload


async def no_runtime_reload(_request: Request, _tenant: UUID) -> None:
    return None


async def check_absent_registration_aba(context: AdminContext, store_root: Path) -> None:
    await make_ordinary_registration(context, absent=True)
    before = await saved_rows(context)
    request, _, artifact = archive_request(context, store_root)
    published, resume = asyncio.Event(), asyncio.Event()
    write = context.service.upsert_plugin

    async def paused_write(
        plugin: admin.PluginResourceRequest,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
        source_filename: str | None = None,
        content_sha256: str | None = None,
        package_metadata: admin.PluginPackageMetadata | None = None,
    ) -> admin.PluginResourceResponse:
        assert artifact.read_bytes() == b"new Q"
        published.set()
        await resume.wait()
        return await write(
            plugin,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_filename=source_filename,
            content_sha256=content_sha256,
            package_metadata=package_metadata,
        )

    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64
    )
    contender: asyncio.Task[admin.PluginArchiveInstallResponse] | None = None
    with (
        patch.object(admin, "_verified_plugin_package_metadata", return_value=verified),
        patch.object(context.service, "upsert_plugin", new=paused_write),
        patch.object(admin, "_reload_plugin_runtime_config_strict", new=no_runtime_reload),
    ):
        try:
            contender = asyncio.create_task(
                admin.install_plugin_archive(request, context.principal, context.service)
            )
            await asyncio.wait_for(published.wait(), timeout=10)
            # Each real service call opens independent SQL sessions; B retains its absent baseline.
            await write(
                admin.PluginResourceRequest(id=PLUGIN_ID, name="Intervening P"),
                tenant_id=context.principal.tenant_id,
                actor_id=context.principal.user_id,
            )
            assert artifact.read_bytes() == b"new Q"
            removed = await admin.uninstall_plugin(
                PLUGIN_ID, request, context.principal, context.service
            )
            assert removed.status == "uninstalled" and not artifact.exists()
            resume.set()
            try:
                await asyncio.wait_for(contender, timeout=10)
            except PublicAPIError as error:
                assert error.status_code == 409 and error.code == "plugin_registration_conflict"
            else:
                raise AssertionError("absent ABA committed an archive without canonical bytes")
        finally:
            resume.set()
            if contender is not None:
                if not contender.done():
                    contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
    after = await saved_rows(context)
    assert (context.principal.tenant_id, "plugin", PLUGIN_ID) not in after
    assert not artifact.exists()
    assert not any(
        row.payload.get("action") == "plugin.install"
        for key, row in after.items()
        if key[1] == "audit"
    )
    for key, row in before.items():
        assert after[key] == row


async def check_archive_check_to_commit_blocks_stage(
    context: AdminContext,
    store_root: Path,
    phase: ArchivePause,
) -> None:
    await make_ordinary_registration(context)
    before = await saved_rows(context)
    tenant_id = context.principal.tenant_id
    request, body, artifact = archive_request(context, store_root)
    pending = store_root / ".uninstalling" / str(tenant_id) / PLUGIN_ID
    paused, resume = asyncio.Event(), asyncio.Event()
    owner_pids: list[int] = []
    async with context.factory() as session:
        engine = session.get_bind()

    async def pause_driver(_driver: object) -> None:
        paused.set()
        await resume.wait()

    def pause_insert(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        execution: DefaultExecutionContext,
        executemany: bool,
    ) -> None:
        compiled = execution.compiled
        if compiled is None or not isinstance(compiled.statement, Insert):
            return
        if cast(Table, compiled.statement.table).name != AdminResourceRow.__tablename__:
            return
        if not any(
            params.get("tenant_id") == tenant_id and params.get("kind") == "plugin"
            for params in execution.compiled_parameters
        ):
            return
        assert not owner_pids
        assert artifact.read_bytes() == b"new Q"
        assert admin._plugin_package_artifact_matches(
            artifact.parent, admin._plugin_archive_signature_files(body)
        )
        owner_pids.append(
            cast(int, connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        )
        cast(AdaptedConnection, connection.connection.dbapi_connection).run_async(pause_driver)

    event_name = "before_cursor_execute" if phase == "before_insert" else "after_cursor_execute"
    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64
    )
    installation: asyncio.Task[admin.PluginArchiveInstallResponse] | None = None
    removal: asyncio.Task[admin.PluginUninstallResponse] | None = None
    event.listen(engine, event_name, pause_insert)
    try:
        with patch.object(admin, "_verified_plugin_package_metadata", return_value=verified):
            installation = asyncio.create_task(
                admin.install_plugin_archive(request, context.principal, context.service)
            )
            await asyncio.wait_for(paused.wait(), timeout=10)
            assert await saved_rows(context) == before
            removal = asyncio.create_task(
                admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service)
            )
            async with context.factory() as observer, asyncio.timeout(10):
                owner_pid = owner_pids[0]
                # Both the actual advisory relation and PostgreSQL's blocking backend attribution must agree.
                query = text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_locks held JOIN pg_locks waiting
                          ON held.locktype = waiting.locktype
                         AND held.database IS NOT DISTINCT FROM waiting.database
                         AND held.classid = waiting.classid AND held.objid = waiting.objid
                         AND held.objsubid = waiting.objsubid
                        WHERE held.locktype = 'advisory' AND held.granted AND NOT waiting.granted
                          AND held.pid = :owner_pid AND waiting.pid <> :owner_pid
                          AND :owner_pid = ANY(pg_blocking_pids(waiting.pid))
                    )
                """)
                while not await observer.scalar(query, {"owner_pid": owner_pid}):
                    assert not removal.done(), "uninstall did not wait for the owning SQL backend"
                    await asyncio.sleep(0.01)
            assert not pending.exists()
            assert artifact.read_bytes() == b"new Q"
            assert admin._plugin_package_artifact_matches(
                artifact.parent, admin._plugin_archive_signature_files(body)
            )
            resume.set()
            installed = await asyncio.wait_for(installation, timeout=10)
            assert installed.plugin.content_sha256 == hashlib.sha256(body).hexdigest()
            try:
                await asyncio.wait_for(removal, timeout=10)
            except PublicAPIError as error:
                assert error.status_code == 409 and error.code == "plugin_registration_conflict"
            else:
                raise AssertionError("stale P uninstall accepted Q after commit")
    finally:
        resume.set()
        event.remove(engine, event_name, pause_insert)
        for task in (installation, removal):
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    after = await saved_rows(context)
    key = (tenant_id, "plugin", PLUGIN_ID)
    assert after[key].row_id == before[key].row_id
    assert after[key].payload["content_sha256"] == hashlib.sha256(body).hexdigest()
    assert not pending.exists() and artifact.read_bytes() == b"new Q"
    for row_key, row in before.items():
        if row_key != key:
            assert after[row_key] == row
    assert not any(
        row.payload.get("action") == "plugin.uninstall"
        for row_key, row in after.items()
        if row_key[1] == "audit"
    )


async def check_ordinary_restore_preserves_q(
    context: AdminContext,
    store_root: Path,
    q_files: PackageRace | Literal["pending"],
) -> None:
    before = await saved_rows(context)
    tenant_id = context.principal.tenant_id
    key = (tenant_id, "plugin", PLUGIN_ID)
    request = package_request(store_root)
    package_root = store_root / str(tenant_id) / PLUGIN_ID
    old_file = package_root / CONTENT_SHA256 / "adapter.py"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"old P")
    replacement, q_id = plugin_payload(), uuid4()
    q_sha = "3" * 64 if q_files == "different_sha" else CONTENT_SHA256
    q_file = package_root / q_sha / "adapter.py"
    original_writer = context.service._record_audit_in_session
    fault = True
    old_states: list[admin._PluginPackageUninstallState] = []
    q_states: list[admin._PluginPackageUninstallState] = []

    async def fail_audit(
        session: AsyncSession,
        action: str,
        resource: str,
        payload: dict[str, object] | None = None,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        await original_writer(
            session, action, resource, payload, tenant_id=tenant_id, actor_id=actor_id
        )
        if fault and action == "plugin.uninstall":
            await session.execute(text("SELECT 1 / 0"))

    async def replace_during_probe(
        _service: admin.AdminResourceService,
        _id: str,
        *,
        tenant_id: UUID,
    ) -> admin.PluginResourceResponse:
        nonlocal fault
        assert await saved_rows(context) == before
        old = admin._pending_plugin_package_uninstall_state(
            request, tenant_id=tenant_id, plugin_id=PLUGIN_ID
        )
        assert old is not None
        old_states.append(old)
        fault = False
        if q_files == "pending":
            completed = await admin.uninstall_plugin(
                PLUGIN_ID, request, context.principal, context.service
            )
            assert completed.status == "uninstalled"
        async with context.factory() as owner, owner.begin():
            await acquire_plugin_registration_lock(owner, tenant_id, PLUGIN_ID)
            await replace_registration(owner, tenant_id, replacement, q_id)
            if q_files != "absent":
                q_file.parent.mkdir(parents=True)
                q_file.write_bytes(b"new Q")
            if q_files == "pending":
                # Build the fixture DTO with the actual locked row's durable identity.
                row = (
                    await owner.scalars(select(AdminResourceRow).where(AdminResourceRow.id == q_id))
                ).one()
                plugin = admin._PluginUninstallResponse.model_validate(
                    admin._plugin_response_from_payload(row.payload).model_dump()
                )
                plugin._registration_snapshot = admin._plugin_registration_snapshot(
                    row, tenant_id, PLUGIN_ID
                )
                q_states.append(
                    admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
                )
        return admin._plugin_response_from_payload(replacement)

    with (
        patch.object(context.service, "_record_audit_in_session", new=fail_audit),
        patch.object(admin, "_current_plugin_for_activation_check", new=replace_during_probe),
        patch.object(admin, "_reload_plugin_runtime_config_strict", new=no_runtime_reload),
    ):
        try:
            await admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service)
        except DBAPIError as error:
            assert getattr(error.orig, "sqlstate", None) == "22012"
        else:
            raise AssertionError("server audit rollback did not occur")
    after = await saved_rows(context)
    assert after[key].row_id == q_id and after[key].payload == replacement
    assert len(old_states) == 1
    if q_files == "pending":
        new = q_states[0]
        assert not package_root.exists()
        assert (new.pending_root / "artifact" / q_sha / "adapter.py").read_bytes() == b"new Q"
        assert admin._read_plugin_uninstall_state(
            new.pending_root / "state.json", tenant_id=tenant_id, plugin_id=PLUGIN_ID
        ) == (
            new.operation_id,
            new.plugin_snapshot_sha256,
            new.created_at,
        )
    else:
        backups = list((store_root / ".uninstalling").glob("c-*/p/state.json"))
        assert len(backups) == 1
        assert (
            backups[0].parent / "artifact" / CONTENT_SHA256 / "adapter.py"
        ).read_bytes() == b"old P"
        old = old_states[0]
        assert admin._read_plugin_uninstall_state(
            backups[0], tenant_id=tenant_id, plugin_id=PLUGIN_ID
        ) == (
            old.operation_id,
            old.plugin_snapshot_sha256,
            old.created_at,
        )
        if q_files == "absent":
            assert not package_root.exists()
        else:
            assert q_file.read_bytes() == b"new Q"
    assert sum(row_key[1] == "audit" for row_key in after) == (1 if q_files == "pending" else 0)
    for row_key, row in before.items():
        if row_key != key:
            assert after[row_key] == row


async def check_delayed_audit_marker_preserves_q(
    context: AdminContext,
    store_root: Path,
    q_files: PackageRace,
) -> None:
    tenant_id = context.principal.tenant_id
    request = package_request(store_root)
    package_root = store_root / str(tenant_id) / PLUGIN_ID
    old_file = package_root / CONTENT_SHA256 / "adapter.py"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"old P")
    reached, resume = asyncio.Event(), asyncio.Event()
    query_audit = admin._plugin_uninstall_audit_exists
    calls = 0

    async def delayed_audit(
        service: admin.AdminResourceService,
        *,
        plugin_id: str,
        actor_id: UUID,
        not_before: datetime | None = None,
    ) -> bool:
        nonlocal calls
        calls += 1
        result = await query_audit(
            service, plugin_id=plugin_id, actor_id=actor_id, not_before=not_before
        )
        if calls == 1:
            assert result
            reached.set()
            await resume.wait()
        return result

    contender: asyncio.Task[admin.PluginUninstallResponse] | None = None
    replacement, q_id = plugin_payload(), uuid4()
    q_sha = "3" * 64 if q_files == "different_sha" else CONTENT_SHA256
    with (
        patch.object(admin, "_plugin_uninstall_audit_exists", new=delayed_audit),
        patch.object(admin, "_reload_plugin_runtime_config_strict", new=no_runtime_reload),
    ):
        try:
            contender = asyncio.create_task(
                admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service)
            )
            await asyncio.wait_for(reached.wait(), timeout=10)
            retry = await admin.uninstall_plugin(
                PLUGIN_ID, request, context.principal, context.service
            )
            assert retry.status == "uninstalled"
            async with context.factory() as owner, owner.begin():
                await acquire_plugin_registration_lock(owner, tenant_id, PLUGIN_ID)
                await replace_registration(owner, tenant_id, replacement, q_id)
                if q_files != "absent":
                    q_file = package_root / q_sha / "adapter.py"
                    q_file.parent.mkdir(parents=True)
                    q_file.write_bytes(b"new Q")
                row = (
                    await owner.scalars(select(AdminResourceRow).where(AdminResourceRow.id == q_id))
                ).one()
                plugin = admin._PluginUninstallResponse.model_validate(
                    admin._plugin_response_from_payload(row.payload).model_dump()
                )
                plugin._registration_snapshot = admin._plugin_registration_snapshot(
                    row, tenant_id, PLUGIN_ID
                )
                new = admin._stage_plugin_package_uninstall(request, plugin, tenant_id=tenant_id)
            before_resume = await saved_rows(context)
            metadata = (new.pending_root / "state.json").read_bytes()
            resume.set()
            completed = await asyncio.wait_for(contender, timeout=10)
            assert completed.status == "uninstalled"
        finally:
            resume.set()
            if contender is not None:
                if not contender.done():
                    contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
    assert await saved_rows(context) == before_resume
    assert not package_root.exists() and not new.audit_confirmed_path.exists()
    assert (new.pending_root / "state.json").read_bytes() == metadata
    if q_files != "absent":
        assert (new.pending_root / "artifact" / q_sha / "adapter.py").read_bytes() == b"new Q"


async def check_registered_restore_after_failed_archive(
    context: AdminContext,
    store_root: Path,
    package: RestorePublishRace,
    fault: RegisteredRestoreFault,
) -> None:
    tenant_id = context.principal.tenant_id
    request, body, q_file = archive_request(context, store_root)
    q_sha = hashlib.sha256(body).hexdigest()
    p_sha = q_sha if package == "same_sha" else CONTENT_SHA256
    package_root = store_root / str(tenant_id) / PLUGIN_ID
    p_file = package_root / p_sha / "adapter.py"
    p_extra = package_root / p_sha / "nested" / "keep.txt"
    # Equal storage keys exercise aliasing; rollback must restore the registered bytes.
    p_file.parent.mkdir(parents=True)
    p_file.write_bytes(b"registered P")
    p_extra.parent.mkdir()
    p_extra.write_bytes(b"complete P tree")
    async with context.factory() as session, session.begin():
        await acquire_plugin_registration_lock(session, tenant_id, PLUGIN_ID)
        row = (
            await session.scalars(
                select(AdminResourceRow).where(
                    AdminResourceRow.tenant_id == tenant_id,
                    AdminResourceRow.kind == "plugin",
                    AdminResourceRow.resource_id == PLUGIN_ID,
                )
            )
        ).one()
        plugin = admin._plugin_response_from_payload(row.payload)
        metadata = plugin.package_metadata
        assert metadata is not None
        artifact = admin.PluginPackageArtifactMetadata(
            storage_key=f"{tenant_id}/{PLUGIN_ID}/{p_sha}",
            content_sha256=p_sha,
            file_count=2,
            total_size_bytes=len(b"registered P") + len(b"complete P tree"),
            stored_at=datetime.now().astimezone(),
            quarantine_state="stored",
        )
        payload = dict(row.payload)
        payload["content_sha256"] = p_sha
        payload["package_metadata"] = metadata.model_copy(update={"artifact": artifact}).model_dump(
            mode="json"
        )
        row.payload = payload
    before = await saved_rows(context)
    _, snapshot = await context.service._plugin_for_uninstall(PLUGIN_ID, tenant_id=tenant_id)
    assert snapshot is not None
    reached, abort_audit = asyncio.Event(), asyncio.Event()
    old_states: list[admin._PluginPackageUninstallState] = []
    owner_pids: list[int] = []
    audit_counts: list[int] = []
    original_audit = context.service._record_audit_in_session
    original_replace = Path.replace
    injected: list[str] = []

    async def pause_and_abort_audit(
        session: AsyncSession,
        action: str,
        resource: str,
        payload: dict[str, object] | None = None,
        *,
        tenant_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        await original_audit(
            session, action, resource, payload, tenant_id=tenant_id, actor_id=actor_id
        )
        if action != "plugin.uninstall":
            return
        old = admin._pending_plugin_package_uninstall_state(
            request, tenant_id=context.principal.tenant_id, plugin_id=PLUGIN_ID
        )
        assert old is not None and old.staged_artifact_root is not None
        old_states.append(old)
        assert not package_root.exists()
        assert (old.staged_artifact_root / p_sha / "adapter.py").read_bytes() == b"registered P"
        assert (
            old.staged_artifact_root / p_sha / "nested" / "keep.txt"
        ).read_bytes() == b"complete P tree"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == context.principal.tenant_id,
                    AdminResourceRow.kind == "plugin",
                    AdminResourceRow.resource_id == PLUGIN_ID,
                )
            )
            == 0
        )
        audit_counts.append(
            cast(
                int,
                await session.scalar(
                    select(func.count())
                    .select_from(AdminResourceRow)
                    .where(
                        AdminResourceRow.tenant_id == context.principal.tenant_id,
                        AdminResourceRow.kind == "audit",
                    )
                ),
            )
        )
        owner_pids.append(cast(int, await session.scalar(text("SELECT pg_backend_pid()"))))
        reached.set()
        await abort_audit.wait()
        # Both DELETE and audit INSERT are real; PostgreSQL aborts their owning transaction.
        await session.execute(text("SELECT 1 / 0"))

    def fail_one_restore(path: Path, target: Path) -> Path:
        destination = Path(target)
        if old_states and not injected:
            old = old_states[0]
            displacing = (
                path == package_root
                and destination.name == "artifact"
                and destination.parent.name.startswith("q-")
                and destination.parent.parent == store_root / ".uninstalling"
            )
            restoring = path == old.staged_artifact_root and destination == package_root
            if (fault == "displace" and displacing) or (fault == "restore" and restoring):
                injected.append(fault)
                raise OSError("one owned restore rename failed")
        return original_replace(path, target)

    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64
    )
    removal: asyncio.Task[admin.PluginUninstallResponse] | None = None
    installation: asyncio.Task[admin.PluginArchiveInstallResponse] | None = None
    with (
        patch.object(context.service, "_record_audit_in_session", new=pause_and_abort_audit),
        patch.object(admin, "_verified_plugin_package_metadata", return_value=verified),
        patch.object(Path, "replace", new=fail_one_restore),
    ):
        try:
            removal = asyncio.create_task(
                admin.uninstall_plugin(PLUGIN_ID, request, context.principal, context.service)
            )
            await asyncio.wait_for(reached.wait(), timeout=10)
            assert await saved_rows(context) == before
            installation = asyncio.create_task(
                admin.install_plugin_archive(request, context.principal, context.service)
            )
            async with context.factory() as observer, asyncio.timeout(10):
                query = text("""
                    SELECT waiting.pid FROM pg_locks held JOIN pg_locks waiting
                      ON held.locktype = waiting.locktype
                     AND held.database IS NOT DISTINCT FROM waiting.database
                     AND held.classid = waiting.classid AND held.objid = waiting.objid
                     AND held.objsubid = waiting.objsubid
                    WHERE held.locktype = 'advisory' AND held.granted AND NOT waiting.granted
                      AND held.pid = :owner_pid AND waiting.pid <> :owner_pid
                      AND :owner_pid = ANY(pg_blocking_pids(waiting.pid))
                """)
                while not await observer.scalar(query, {"owner_pid": owner_pids[0]}):
                    assert not installation.done(), (
                        "archive did not reach the real registration lock"
                    )
                    await asyncio.sleep(0.01)
            assert q_file.read_bytes() == b"new Q"
            assert not p_extra.exists()
            assert await saved_rows(context) == before
            abort_audit.set()
            try:
                await asyncio.wait_for(installation, timeout=10)
            except PublicAPIError as error:
                assert error.status_code == 409 and error.code == "plugin_registration_conflict"
            else:
                raise AssertionError("uncommitted Q became installed over registered P")
            try:
                await asyncio.wait_for(removal, timeout=10)
            except DBAPIError as error:
                assert fault == "none" and getattr(error.orig, "sqlstate", None) == "22012"
            except PublicAPIError as error:
                assert fault != "none" and error.status_code == 503
                assert error.code == "plugin_uninstall_cleanup_pending"
            else:
                raise AssertionError("uninstall audit rollback was not observed")
        finally:
            abort_audit.set()
            for task in (installation, removal):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    assert audit_counts == [1] and len(owner_pids) == len(old_states) == 1
    assert await saved_rows(context) == before
    old = old_states[0]
    identity = old.operation_id, old.plugin_snapshot_sha256, old.created_at

    def q_backups() -> list[Path]:
        return [
            path
            for path in (store_root / ".uninstalling").glob("q-*")
            if (path / "artifact").is_dir()
        ]

    if fault != "none":
        assert injected == [fault]
        assert old.staged_artifact_root is not None
        assert (old.staged_artifact_root / p_sha / "adapter.py").read_bytes() == b"registered P"
        assert (
            old.staged_artifact_root / p_sha / "nested" / "keep.txt"
        ).read_bytes() == b"complete P tree"
        assert (
            admin._read_plugin_uninstall_state(
                old.pending_root / "state.json", tenant_id=tenant_id, plugin_id=PLUGIN_ID
            )
            == identity
        )
        if fault == "displace":
            assert q_file.read_bytes() == b"new Q" and q_backups() == []
        else:
            assert not package_root.exists() and len(q_backups()) == 1
            assert (q_backups()[0] / "artifact" / q_sha / "adapter.py").read_bytes() == b"new Q"
        # A fresh real SQL transaction rechecks exact P before retrying the failed filesystem move.
        await context.service._restore_plugin_uninstall(
            request, old, tenant_id=tenant_id, plugin_id=PLUGIN_ID, snapshot=snapshot
        )
    else:
        assert injected == []
    assert p_file.read_bytes() == b"registered P"
    assert p_extra.read_bytes() == b"complete P tree"
    assert not old.pending_root.exists()
    backups = q_backups()
    assert len(backups) == 1
    assert (backups[0] / "artifact" / q_sha / "adapter.py").read_bytes() == b"new Q"
    assert (
        admin._read_plugin_uninstall_state(
            backups[0] / "state.json", tenant_id=tenant_id, plugin_id=PLUGIN_ID
        )
        == identity
    )
    assert await saved_rows(context) == before
    await context.service._restore_plugin_uninstall(
        request, old, tenant_id=tenant_id, plugin_id=PLUGIN_ID, snapshot=snapshot
    )
    assert q_backups() == backups
    assert p_file.read_bytes() == b"registered P" and p_extra.read_bytes() == b"complete P tree"
    assert await saved_rows(context) == before


# Native loads the exact same checks without installing or emulating pytest.
if importlib.util.find_spec("pytest") is not None:
    import pytest

    @asynccontextmanager
    async def safe_db_errors() -> AsyncIterator[None]:
        try:
            yield
        except Exception as error:  # noqa: BLE001 - prevent raw DB errors or private fixtures in pytest output.
            raise AssertionError(type(error).__name__) from None

    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skipif(
            os.environ.get("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED") != "1"
            or not os.environ.get("AGENT_HUB_TEST_DATABASE_URL"),
            reason="requires explicit isolated PostgreSQL testDB; no default connection",
        ),
    ]

    @pytest.fixture(scope="session")
    def database_url() -> str:
        url = os.environ.get("AGENT_HUB_TEST_DATABASE_URL")
        if not url or os.environ.get("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED") != "1":
            pytest.skip("explicit isolated PostgreSQL testDB required")
        try:
            driver = make_url(url).drivername
        except Exception as error:  # noqa: BLE001 - never include the configured URL in diagnostics.
            raise ValueError(type(error).__name__) from None
        if driver != "postgresql+asyncpg":
            pytest.skip("PostgreSQL asyncpg testDB required")
        return url

    @pytest.fixture
    async def admin_context(
        auth_session_factory: async_sessionmaker[AsyncSession],
    ) -> AsyncIterator[AdminContext]:
        async with safe_db_errors():
            context = await create_context(auth_session_factory)
            try:
                yield context
            finally:
                await cleanup_context(context)

    @pytest.mark.parametrize("action", ["disable", "stop", "approve"])
    @pytest.mark.parametrize("phase", ["before", "after"])
    async def test_postgres_admin_state_and_audit_roll_back_together(
        admin_context: AdminContext,
        action: Action,
        phase: AuditPhase,
    ) -> None:
        async with safe_db_errors():
            await check_audit_rollback(admin_context, action, phase)

    @pytest.mark.parametrize("changed", ["row_id", "marker", "payload"])
    @pytest.mark.parametrize("active_replacement", [False, True])
    async def test_postgres_stale_uninstall_preserves_registration_and_packages(
        admin_context: AdminContext,
        tmp_path: Path,
        changed: Replacement,
        active_replacement: bool,
    ) -> None:
        async with safe_db_errors():
            await check_registration_replacement(
                admin_context, tmp_path, changed, active_replacement
            )

    @pytest.mark.parametrize(
        "action,generated",
        [
            (action, generated)
            for action in ("disable", "stop", "approve", "upsert")
            for generated in (False, True)
            if not (action == "upsert" and generated)
        ],
    )
    async def test_postgres_identical_json_recreated_row_rejects_stale_write(
        admin_context: AdminContext,
        action: WriteAction,
        generated: bool,
    ) -> None:
        async with safe_db_errors():
            await check_registration_write_aba(admin_context, action, generated)

    @pytest.mark.parametrize("active_replacement", [False, True])
    async def test_postgres_api_retry_preserves_old_pending(
        admin_context: AdminContext,
        tmp_path: Path,
        active_replacement: bool,
    ) -> None:
        async with safe_db_errors():
            await check_api_retry_preserves_old_pending(admin_context, tmp_path, active_replacement)

    async def test_postgres_delayed_finish_preserves_next_operation(
        admin_context: AdminContext,
        tmp_path: Path,
    ) -> None:
        async with safe_db_errors():
            await check_delayed_finish_preserves_next_operation(admin_context, tmp_path)

    @pytest.mark.parametrize("generated", [False, True])
    async def test_postgres_archive_conflict_preserves_shared_artifact(
        admin_context: AdminContext,
        tmp_path: Path,
        generated: bool,
    ) -> None:
        async with safe_db_errors():
            await check_archive_conflict_preserves_shared_artifact(
                admin_context, tmp_path, generated
            )

    async def test_postgres_absent_registration_aba_never_commits_staged_archive(
        admin_context: AdminContext,
        tmp_path: Path,
    ) -> None:
        async with safe_db_errors():
            await check_absent_registration_aba(admin_context, tmp_path)

    @pytest.mark.parametrize("phase", ["before_insert", "after_insert_before_commit"])
    async def test_postgres_archive_check_to_commit_blocks_uninstall_stage(
        admin_context: AdminContext,
        tmp_path: Path,
        phase: ArchivePause,
    ) -> None:
        async with safe_db_errors():
            await check_archive_check_to_commit_blocks_stage(admin_context, tmp_path, phase)

    @pytest.mark.parametrize("q_files", ["absent", "same_sha", "different_sha", "pending"])
    async def test_postgres_ordinary_audit_rollback_restore_preserves_q(
        admin_context: AdminContext,
        tmp_path: Path,
        q_files: PackageRace | Literal["pending"],
    ) -> None:
        async with safe_db_errors():
            await check_ordinary_restore_preserves_q(admin_context, tmp_path, q_files)

    @pytest.mark.parametrize("q_files", ["absent", "same_sha", "different_sha"])
    async def test_postgres_delayed_audit_marker_preserves_next_operation(
        admin_context: AdminContext,
        tmp_path: Path,
        q_files: PackageRace,
    ) -> None:
        async with safe_db_errors():
            await check_delayed_audit_marker_preserves_q(admin_context, tmp_path, q_files)

    @pytest.mark.parametrize("package", ["same_sha", "different_sha"])
    @pytest.mark.parametrize("fault", ["none", "displace", "restore"])
    async def test_postgres_failed_archive_publish_cannot_prevent_original_registration_restore(
        admin_context: AdminContext,
        tmp_path: Path,
        package: RestorePublishRace,
        fault: RegisteredRestoreFault,
    ) -> None:
        async with safe_db_errors():
            await check_registered_restore_after_failed_archive(
                admin_context, tmp_path, package, fault
            )
