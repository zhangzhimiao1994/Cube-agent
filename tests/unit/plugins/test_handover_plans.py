"""Composition tests use real source files and SQL doubles, not live DB proof."""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ClauseElement

from agent_hub.auth.models import PermissionDenied
from agent_hub.db.models import AdminResourceRow, RunRow, UserRow
from agent_hub.plugins.handover_grants import DurableSourceGrantService, SourceGrant
from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore
from agent_hub.plugins.handover_sources import _canonical
from agent_hub.plugins.handover_storage import Conflict, PluginHandoverStorage, StorageUnavailable
from tests.unit.plugins import test_handover_grants as grant_tests
from tests.unit.plugins import test_handover_sources as source_tests

source_context = source_tests.context
forbid_execution = source_tests.forbid_execution_and_signing
NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
EXPIRY = NOW + timedelta(hours=1)


@pytest.fixture
async def context(
    source_context: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    sources, repository, workspace, principal = source_context
    db = grant_tests.SQLBoundary()
    user = UserRow(
        id=principal.user_id,
        tenant_id=principal.tenant_id,
        username="creator",
        role="operator",
        disabled=False,
    )
    db.rows.extend(
        [
            user,
            RunRow(
                id=repository.record.id,
                tenant_id=principal.tenant_id,
                actor_id=principal.user_id,
                actor_role="operator",
                request="make plugin",
                status="completed",
                version=1,
                routing_decision=dict(repository.record.routing_decision),
            ),
        ]
    )
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    storage = PluginHandoverStorage(None, clock=lambda: NOW)
    transaction_sessions: list[AsyncSession] = []

    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncSession]:
        # Restore the recorder on rollback; actual SQL atomicity is a separate gate.
        previous = deepcopy(db.rows)
        try:
            async with AsyncSession(engine) as session, session.begin():
                transaction_sessions.append(session)
                monkeypatch.setattr(session, "scalar", AsyncMock(side_effect=db.scalar))
                monkeypatch.setattr(session, "execute", AsyncMock(side_effect=db.execute))
                monkeypatch.setattr(session, "flush", AsyncMock())
                yield session
        except BaseException:
            db.rows = previous
            raise

    monkeypatch.setattr(storage, "_transaction", transaction)
    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    grants = DurableSourceGrantService(clock=lambda: NOW)
    module = importlib.import_module("agent_hub.plugins.handover_plans")
    service = module.GeneratedPluginPlanService(
        sources=sources,
        snapshots=store,
        grants=grants,
        storage=storage,
        clock=lambda: NOW,
    )
    try:
        yield {
            "service": service,
            "storage": storage,
            "store": store,
            "grants": grants,
            "sources": sources,
            "repository": repository,
            "workspace": workspace,
            "principal": principal,
            "db": db,
            "user": user,
            "sessions": transaction_sessions,
        }
    finally:
        await engine.dispose()


async def create(context: dict[str, Any], *, key: str = "same-request") -> Any:
    return await context["service"].create_plan(
        context["principal"],
        context["repository"].record.id,
        source_tests.workspace_source(),
        idempotency_key=key,
        expires_at=EXPIRY,
    )


def rows(context: dict[str, Any]) -> list[AdminResourceRow]:
    return [row for row in context["db"].rows if isinstance(row, AdminResourceRow)]


async def test_frozen_source_plan_and_grant_share_one_transaction(
    context: dict[str, Any],
    forbid_execution: None,
) -> None:
    receipt = await create(context)
    stored = rows(context)
    assert len(context["sessions"]) == 1
    assert len(stored) == 2 and {row.kind for row in stored} == {"plugin_handover"}
    plan = next(row for row in stored if row.resource_id.startswith("gh-"))
    grant = SourceGrant.model_validate_json(
        _canonical(next(row for row in stored if row.resource_id.startswith("sg-")).payload)
    )
    assert plan.payload["source_grant_id"] == grant.grant_id
    assert plan.payload["source_grant_binding_sha256"] == grant.binding_sha256
    assert grant.binding.plan_id == receipt.handover_id
    assert grant.binding.creator_id == context["principal"].user_id
    assert grant.binding.run_id == receipt.run_id and grant.binding.plan_version == 1
    assert receipt.status == "awaiting_signature" and receipt.installer_id is None
    assert receipt.receipt_id is None and receipt.audit_id is None
    assert receipt.signature_payload_sha256 is None and receipt.archive_sha256 is None
    snapshot = context["store"].read(
        tenant_id=context["principal"].tenant_id,
        snapshot_id=grant.binding.snapshot_id,
        blob_sha256=grant.binding.blob_sha256,
        source_manifest_sha256=receipt.source_manifest_sha256,
    )
    assert snapshot.creator_id == context["principal"].user_id
    assert (
        next(file.content for file in snapshot.snapshot.files if file.path == "main.py")
        == source_tests.CODE
    )
    assert set(receipt.risk_summary) >= {"generated_code", "approval_required"}


async def test_repeat_identity_is_idempotent_without_registering_plugin(
    context: dict[str, Any],
) -> None:
    first = await create(context)
    second = await create(context)
    assert first == second and len(rows(context)) == 2
    assert all(row.kind != "plugin" for row in rows(context))
    assert len(context["sessions"]) == 2


async def test_changed_source_cannot_replace_same_confirmation_slot(
    context: dict[str, Any],
) -> None:
    first = await create(context)
    before = deepcopy([(row.resource_id, row.payload) for row in rows(context)])
    principal = context["principal"]
    context["workspace"].write_bytes(
        principal.tenant_id,
        "project-a",
        "session-a",
        "main.py",
        b"changed\n",
        "text/plain",
    )
    with pytest.raises(Conflict):
        await create(context)
    assert [(row.resource_id, row.payload) for row in rows(context)] == before
    assert first.status == "awaiting_signature"


async def test_current_disabled_creator_after_freeze_cannot_publish_plan(
    context: dict[str, Any],
) -> None:
    original = context["sources"].freeze

    async def freeze(*args: Any, **kwargs: Any) -> Any:
        snapshot = await original(*args, **kwargs)
        context["user"].disabled = True
        return snapshot

    context["sources"].freeze = freeze
    with pytest.raises(PermissionDenied):
        await create(context)
    assert rows(context) == []


@pytest.mark.parametrize("hours", [-1, 0, 25])
async def test_invalid_expiry_rejected_before_freeze(
    context: dict[str, Any],
    hours: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = AsyncMock(side_effect=AssertionError("invalid plan must not read files"))
    monkeypatch.setattr(context["sources"], "freeze", freeze)
    with pytest.raises(ValueError):
        await context["service"].create_plan(
            context["principal"],
            context["repository"].record.id,
            source_tests.workspace_source(),
            idempotency_key="invalid-expiry",
            expires_at=NOW + timedelta(hours=hours),
        )
    freeze.assert_not_called()
    assert context["sessions"] == [] and rows(context) == []


async def test_plan_write_failure_rolls_back_same_transaction_grant(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        assert any(row.resource_id.startswith("sg-") for row in rows(context))
        raise RuntimeError("simulated plan write failure")

    monkeypatch.setattr(context["storage"], "create_plan_in_session", fail)
    with pytest.raises(RuntimeError, match="simulated plan write failure"):
        await create(context)
    assert rows(context) == [] and len(context["sessions"]) == 1


async def test_untrusted_authorization_dto_is_not_a_source_selector(
    context: dict[str, Any],
) -> None:
    frozen = await context["sources"].freeze(
        context["principal"],
        context["repository"].record.id,
        source_tests.workspace_source(),
    )
    with pytest.raises(ValueError):
        await context["service"].create_plan(
            context["principal"],
            UUID(str(context["repository"].record.id)),
            frozen.authorized,
            idempotency_key="dto",
            expires_at=EXPIRY,
        )
    assert rows(context) == []


async def test_unavailable_sql_never_freezes_or_publishes_source(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("agent_hub.plugins.handover_plans")
    service = module.GeneratedPluginPlanService(
        sources=context["sources"],
        snapshots=context["store"],
        grants=context["grants"],
        storage=PluginHandoverStorage(None),
        clock=lambda: NOW,
    )
    freeze = AsyncMock(side_effect=AssertionError("SQL must be available first"))
    monkeypatch.setattr(context["sources"], "freeze", freeze)
    with pytest.raises(StorageUnavailable):
        await service.create_plan(
            context["principal"],
            context["repository"].record.id,
            source_tests.workspace_source(),
            idempotency_key="missing-db",
            expires_at=EXPIRY,
        )
    freeze.assert_not_called()
    assert rows(context) == []


async def test_permission_summary_comes_from_actual_frozen_manifest(
    context: dict[str, Any],
) -> None:
    manifest = json.loads(source_tests.manifest_bytes())
    manifest["capabilities"] = [
        {"id": "example.read", "permission_class": "file:read"},
        {"id": "example.write", "permission_class": "file:write"},
        {"id": "example.list", "permission_class": "file:read"},
    ]
    context["workspace"].write_bytes(
        context["principal"].tenant_id,
        "project-a",
        "session-a",
        "plugin.json",
        json.dumps(manifest).encode(),
        "application/json",
    )
    receipt = await create(context)
    assert receipt.permissions == ("file:read", "file:write")


async def test_invalid_key_fails_before_reading_or_opening_transaction(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze = AsyncMock(side_effect=AssertionError("invalid key must not read"))
    monkeypatch.setattr(context["sources"], "freeze", freeze)
    with pytest.raises(ValueError):
        await create(context, key="unsafe/key")
    freeze.assert_not_called()
    assert context["sessions"] == []


async def test_expiry_after_user_lock_wait_precedes_snapshot_publication(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [NOW]
    context["service"]._clock = lambda: now[0]
    original = context["grants"]._user

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        user = await original(*args, **kwargs)
        now[0] = EXPIRY
        return user

    monkeypatch.setattr(context["grants"], "_user", delayed)
    publish = AsyncMock(side_effect=AssertionError("expired plan must not publish"))
    monkeypatch.setattr(context["store"], "publish", publish)
    with pytest.raises(ValueError, match="expired"):
        await create(context)
    publish.assert_not_called()
    assert rows(context) == []


@pytest.mark.parametrize("repeat", [False, True])
async def test_expiry_after_final_storage_lookup_rejects_and_preserves_prior_state(
    context: dict[str, Any],
    repeat: bool,
) -> None:
    if repeat:
        await create(context)
    before = deepcopy([(row.resource_id, row.payload) for row in rows(context)])
    now = [NOW]
    context["service"]._clock = lambda: now[0]
    lookups = 0

    def expire(statement: ClauseElement) -> None:
        nonlocal lookups
        if not isinstance(statement, Select):
            return
        identity = context["db"].params(statement).get("resource_id_1")
        if isinstance(identity, str) and identity.startswith("gh-"):
            lookups += 1
            if lookups == (1 if repeat else 2):
                now[0] = EXPIRY

    context["db"].after = expire
    with pytest.raises(ValueError, match="expired"):
        await create(context)
    assert lookups == (1 if repeat else 2)
    assert [(row.resource_id, row.payload) for row in rows(context)] == before
