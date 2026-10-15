"""Admin SQL boundary regressions, not live PostgreSQL concurrency verification."""

import importlib
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from types import ModuleType, SimpleNamespace, TracebackType
from typing import Any, Literal, Self, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Delete, Insert

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
    PersistentAdminResourceService,
    PluginPackageApprovalRequest,
    PluginPackageMetadata,
    PluginResourceRequest,
    PluginResourceResponse,
    _PluginWritePayload,
)
from agent_hub.db.models import AdminResourceRow

type Payload = dict[str, Any]
type RowKey = tuple[UUID, str, str]
type AuditFailure = Literal["before", "after", "commit"]


class SQLBoundary:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.rows: dict[RowKey, Payload] = {}
        self.row_ids: dict[RowKey, UUID] = {}
        self.events: list[tuple[object, ...]] = []
        self.active = False
        self.on_lock: Callable[[], None] | None = None
        self.fail_audit: AuditFailure | None = None

    def __call__(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        return False

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        self.rollback_rows = deepcopy(self.rows)
        self.rollback_ids = deepcopy(self.row_ids)
        self.active = True
        event_start = len(self.events)
        try:
            yield
            # Read-only review transactions must not consume mutation commit faults.
            if self.fail_audit == "commit" and any(
                event[0] in {"write", "delete", "audit"} for event in self.events[event_start:]
            ):
                raise RuntimeError("commit failed")
        except BaseException:
            self.rows = self.rollback_rows
            self.row_ids = self.rollback_ids
            raise
        finally:
            self.active = False

    async def lock(self, session: object, tenant_id: UUID, plugin_id: str) -> None:
        assert session is self and self.active
        self.events.append(("lock", tenant_id, plugin_id))
        if self.on_lock is not None:
            self.on_lock()
            # A competing committed registration is outside this transaction's rollback.
            self.rollback_rows = deepcopy(self.rows)
            self.rollback_ids = deepcopy(self.row_ids)

    async def execute(self, statement: Select[Any] | Insert | Delete) -> SimpleNamespace:
        dialect_factory = cast(Callable[[], Dialect], postgresql.dialect)
        params = statement.compile(dialect=dialect_factory()).params
        if isinstance(statement, Insert):
            key = (params["tenant_id"], params["kind"], params["resource_id"])
            audit = params["kind"] == "audit"
            assert self.active
            self.events.append(("audit" if audit else "write", *key))
            if audit and self.fail_audit == "before":
                raise RuntimeError("audit failed")
            self.rows[key] = deepcopy(params["payload"])
            self.row_ids.setdefault(key, params["id"])
            if audit and self.fail_audit == "after":
                raise RuntimeError("audit failed")
            return SimpleNamespace(rowcount=1)
        if isinstance(statement, Select) and params["kind_1"] == "audit":
            assert params["payload_1"] == "action" and params["payload_2"] == "resource"
            rows = [
                SimpleNamespace(id=self.row_ids[key], payload=deepcopy(payload))
                for key, payload in self.rows.items()
                if key[0] == params["tenant_id_1"]
                and key[1] == "audit"
                and payload["action"] == params["param_1"]
                and payload["resource"] == params["param_2"]
            ]
            self.events.append(("audit_read", params["tenant_id_1"], self.active))
            return SimpleNamespace(scalars=lambda: rows)
        key = (params["tenant_id_1"], params["kind_1"], params["resource_id_1"])
        if isinstance(statement, Select):
            self.events.append(("read", *key, self.active))
            payload = self.rows.get(key)
            row = (
                None
                if payload is None
                else SimpleNamespace(
                    id=self.row_ids[key],
                    payload=deepcopy(payload),
                )
            )
            return SimpleNamespace(scalar_one_or_none=lambda: row)
        assert isinstance(statement, Delete)
        self.events.append(("delete", *key))
        deleted = self.rows.pop(key, None) is not None
        self.row_ids.pop(key, None)
        return SimpleNamespace(rowcount=int(deleted))

    def put(
        self,
        payload: Payload,
        *,
        kind: str = "plugin",
        resource_id: str = "generated-tool",
        tenant_id: UUID | None = None,
        row_id: UUID | None = None,
    ) -> None:
        key = (tenant_id or self.tenant_id, kind, resource_id)
        self.rows[key] = deepcopy(payload)
        if row_id is not None:
            self.row_ids[key] = row_id
        else:
            self.row_ids.setdefault(key, uuid4())

    def plugin(self, plugin_id: str = "generated-tool", tenant_id: UUID | None = None) -> Payload:
        return self.rows[(tenant_id or self.tenant_id, "plugin", plugin_id)]


type AdminContext = tuple[PersistentAdminResourceService, SQLBoundary, UUID]


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> AdminContext:
    tenant_id = uuid4()
    db = SQLBoundary(tenant_id)
    try:
        storage = importlib.import_module("agent_hub.plugins.handover_storage")
    except ModuleNotFoundError as error:
        if error.name != "agent_hub.plugins.handover_storage":
            raise
        storage = ModuleType("agent_hub.plugins.handover_storage")
        monkeypatch.setitem(sys.modules, storage.__name__, storage)
    monkeypatch.setattr(storage, "acquire_plugin_registration_lock", db.lock, raising=False)
    service = PersistentAdminResourceService(
        config_service=Mock(),
        secret_service=Mock(),
        tenant_id=tenant_id,
        actor_id=uuid4(),
        model_transport=Mock(),
        session_factory=cast(async_sessionmaker[AsyncSession], db),
    )
    monkeypatch.setattr(service, "_record_audit", AsyncMock())
    return service, db, tenant_id


def plugin_payload(*, generated: bool = True, enabled: bool = False) -> Payload:
    payload = PluginResourceResponse(
        id="generated-tool",
        name="Generated tool",
        version="1.0.0",
        enabled=enabled,
        source_filename="generated.zip",
        content_sha256="1" * 64,
        resource_config={"adapter_id": "generated.adapter"},
        status="stopped" if enabled else "disabled",
        health="stopped" if enabled else "disabled",
        package_metadata=PluginPackageMetadata.model_validate(
            {
                "kind": "adapter_package",
                "package_version": "1.0.0",
                "runtime": "python",
                "entrypoint": "adapter.py",
                "isolation": "local_process",
            }
        ),
    ).model_dump(mode="json")
    if generated:
        payload["_generated_handover_id"] = "gh-" + "2" * 64
    return payload


def configure_trusted_generated_review(
    context: AdminContext, *, tenant_id: UUID | None = None
) -> None:
    """Stub only the review boundary; this does not verify real approval proofs."""
    service, db, own_tenant = context
    target_tenant = own_tenant if tenant_id is None else tenant_id

    async def validate(session: AsyncSession, *, plugin_row: AdminResourceRow) -> bool:
        assert session is cast(AsyncSession, db) and db.active
        key = (target_tenant, "plugin", "generated-tool")
        assert plugin_row.id == db.row_ids[key]
        assert plugin_row.payload == db.rows[key]
        db.events.append(("review", target_tenant, plugin_row.id))
        return True

    service._generated_plugin_approval_validator = validate


def reviewed_plugin_payload(*, enabled: bool = True) -> Payload:
    """Synthetic prior approval/signature state, not real cryptographic review evidence."""
    payload = plugin_payload(enabled=enabled)
    payload["package_metadata"] = PluginPackageMetadata.model_validate(
        {
            **payload["package_metadata"],
            "approval_state": "approved",
            "signature": {"algorithm": "ed25519", "key_id": "test-key", "value": "A" * 86},
            "signature_verification": "verified",
            "verified_public_key_sha256": "a" * 64,
            "sdk_api_version": "1.0",
            "install_mode": "runtime_registered",
        }
    ).model_dump(mode="json")
    payload["_generated_review_approval"] = "ga-review-" + "5" * 64
    return payload


@pytest.mark.parametrize("batch", [False, True])
async def test_ordinary_upsert_rechecks_generated_owner_after_lock(
    context: AdminContext,
    batch: bool,
) -> None:
    service, db, tenant_id = context
    payload = plugin_payload(generated=False)
    db.on_lock = lambda: db.put(plugin_payload())
    with pytest.raises(PublicAPIError) as error:
        if batch:
            await service._mutate_admin_payloads(upserts=(("plugin", "generated-tool", payload),))
        else:
            await service._upsert_admin_payload("plugin", "generated-tool", payload)
    assert error.value.status_code == 409
    assert not any(event[0] == "write" for event in db.events)
    assert db.events[0] == ("lock", tenant_id, "generated-tool")
    assert db.events[1][0] == "read" and db.events[1][-1] is True


@pytest.mark.parametrize("batch", [False, True])
async def test_ordinary_upsert_cannot_forge_even_empty_marker(
    context: AdminContext,
    batch: bool,
) -> None:
    service, db, _ = context
    payload = plugin_payload(generated=False)
    payload["_generated_handover_id"] = ""
    with pytest.raises(PublicAPIError) as error:
        if batch:
            await service._mutate_admin_payloads(upserts=(("plugin", "generated-tool", payload),))
        else:
            await service._upsert_admin_payload("plugin", "generated-tool", payload)
    assert error.value.status_code == 409
    assert db.rows == {}


async def test_batch_acquires_sorted_unique_plugin_locks_before_writes(
    context: AdminContext,
) -> None:
    service, db, tenant_id = context
    db.put({"history": "keep"}, kind="generated_plugin_handover", resource_id="receipt")
    db.put(plugin_payload(), resource_id="a")
    await service._mutate_admin_payloads(
        upserts=(("plugin", "z", plugin_payload(generated=False)), ("setting", "s", {})),
        deletes=(("plugin", "z"), ("plugin", "a")),
    )
    assert db.events[:2] == [("lock", tenant_id, "a"), ("lock", tenant_id, "z")]
    assert db.rows[(tenant_id, "generated_plugin_handover", "receipt")] == {"history": "keep"}
    assert not any(key[1] == "plugin" for key in db.rows)


async def test_plugin_delete_reads_and_deletes_inside_same_tenant_lock(
    context: AdminContext,
) -> None:
    service, db, tenant_id = context
    other_tenant = uuid4()
    db.put(plugin_payload())
    db.put(plugin_payload(), tenant_id=other_tenant)
    db.put({"history": "keep"}, kind="generated_plugin_handover", resource_id="receipt")
    assert await service._delete_admin_payload("plugin", "generated-tool") is True
    assert db.events[0] == ("lock", tenant_id, "generated-tool")
    assert all(event[-1] is True for event in db.events if event[0] == "read")
    assert db.plugin(tenant_id=other_tenant)["_generated_handover_id"]
    assert db.rows[(tenant_id, "generated_plugin_handover", "receipt")] == {"history": "keep"}
    assert await service._delete_admin_payload("plugin", "absent") is False


async def test_plugin_lock_uses_explicit_tenant(context: AdminContext) -> None:
    service, db, tenant_id = context
    other_tenant = uuid4()
    await service._upsert_admin_payload(
        "plugin",
        "generated-tool",
        plugin_payload(generated=False),
        tenant_id=other_tenant,
    )
    assert db.events[0] == ("lock", other_tenant, "generated-tool")
    assert (tenant_id, "plugin", "generated-tool") not in db.rows


@pytest.mark.parametrize(
    "action",
    ["disable", "stop", "preflight_failed", "approve", "reject", "start", "enable", "reload"],
)
@pytest.mark.parametrize("generated", [False, True])
async def test_controlled_states_preserve_generated_identity_and_marker(
    context: AdminContext,
    action: str,
    generated: bool,
) -> None:
    service, db, _ = context
    original = plugin_payload(generated=generated, enabled=action != "enable")
    if action in {"start", "enable", "reload"}:
        if generated:
            original = reviewed_plugin_payload(enabled=action != "enable")
            configure_trusted_generated_review(context)
        else:
            original["package_metadata"]["kind"] = "manifest_only"
    db.put(original)
    if generated and action == "approve":
        before, before_ids = deepcopy(db.rows), deepcopy(db.row_ids)
        with pytest.raises(PublicAPIError) as error:
            await service.approve_plugin_package("generated-tool", PluginPackageApprovalRequest())
        assert error.value.status_code == 409
        assert error.value.code == "plugin_generated_review_required"
        assert db.rows == before and db.row_ids == before_ids
        assert not any(event[0] in {"write", "audit"} for event in db.events)
        return
    if action == "approve":
        await service.approve_plugin_package("generated-tool", PluginPackageApprovalRequest())
    elif action == "reject":
        await service.reject_plugin_package("generated-tool", PluginPackageApprovalRequest())
    elif action == "preflight_failed":
        await service.mark_plugin_preflight_failed("generated-tool")
    elif action in {"start", "enable", "reload"}:
        await getattr(service, action + "_plugin")(
            "generated-tool",
            activation_preflight=AsyncMock(return_value=None),
        )
    else:
        await getattr(service, action + "_plugin")("generated-tool")
    stored = db.plugin()
    for key in ("source_filename", "content_sha256", "resource_config"):
        assert stored[key] == original[key]
    if generated:
        assert stored["_generated_handover_id"] == original["_generated_handover_id"]
        if "_generated_review_approval" in original:
            assert stored["_generated_review_approval"] == original["_generated_review_approval"]
    else:
        assert "_generated_handover_id" not in stored
    for key in ("runtime", "entrypoint", "isolation", "package_version"):
        assert stored["package_metadata"][key] == original["package_metadata"][key]
    if action in {"approve", "reject"}:
        assert stored["package_metadata"]["approval_state"] == (
            "approved" if action == "approve" else "rejected"
        )
    elif action == "preflight_failed":
        assert stored["status"] == "failed"
    else:
        expected = (
            "running"
            if action in {"start", "reload"}
            else ("disabled" if action == "disable" else "stopped")
        )
        assert stored["status"] == expected
    assert any(event[0] == "lock" for event in db.events)


@pytest.mark.parametrize(
    "generated,action",
    [
        (generated, action)
        for generated in (False, True)
        for action in ("stop", "approve", "reject", "start")
        if not (generated and action == "approve")
    ],
)
@pytest.mark.parametrize("changed", ["content_sha256", "approval_state", "status"])
async def test_old_state_snapshot_cannot_overwrite_concurrent_package_or_state(
    context: AdminContext,
    generated: bool,
    action: str,
    changed: str,
) -> None:
    service, db, _ = context
    original = plugin_payload(generated=generated, enabled=True)
    original["package_metadata"]["kind"] = "manifest_only"
    if action in {"approve", "reject"}:
        original["package_metadata"]["kind"] = "adapter_package"
    if generated and action == "start":
        original = reviewed_plugin_payload()
        configure_trusted_generated_review(context)
    db.put(original)
    replacement = deepcopy(original)
    if changed == "content_sha256":
        replacement[changed] = "3" * 64
    elif changed == "approval_state":
        replacement["package_metadata"][changed] = "rejected"
    else:
        replacement[changed] = "running"

    def replace_on_write_lock(_plugin: PluginResourceResponse | None = None) -> None:
        db.on_lock = lambda: db.put(replacement)

    if action != "start":
        replace_on_write_lock()
    with pytest.raises(PublicAPIError) as error:
        if action in {"approve", "reject"}:
            await getattr(service, action + "_plugin_package")(
                "generated-tool", PluginPackageApprovalRequest()
            )
        elif action == "start":
            await service.start_plugin(
                "generated-tool",
                activation_preflight=AsyncMock(side_effect=replace_on_write_lock),
            )
        else:
            await getattr(service, action + "_plugin")("generated-tool")
    assert error.value.status_code == 409
    assert error.value.code == "plugin_registration_conflict"
    assert db.plugin() == replacement
    assert not any(event[0] in {"write", "audit"} for event in db.events)


@pytest.mark.parametrize(
    "field",
    [
        "source_filename",
        "content_sha256",
        "resource_config",
        "_generated_handover_id",
        "entrypoint",
    ],
)
async def test_controlled_state_cannot_replace_immutable_identity(
    context: AdminContext,
    field: str,
) -> None:
    service, db, _ = context
    original = plugin_payload()
    db.put(original)
    updated = deepcopy(original)
    if field == "entrypoint":
        updated["package_metadata"][field] = "replacement.py"
    else:
        updated[field] = {"adapter_id": "other"} if field == "resource_config" else "other"
    with pytest.raises(PublicAPIError) as error:
        await service._upsert_admin_payload(
            "plugin",
            "generated-tool",
            _PluginWritePayload(updated, state_snapshot=original),
        )
    assert error.value.status_code == 409
    assert db.plugin() == original


async def test_regular_registration_cannot_restore_cached_package_after_upgrade(
    context: AdminContext,
) -> None:
    service, db, _ = context
    original = plugin_payload(generated=False)
    db.put(original)
    replacement = deepcopy(original)
    replacement["content_sha256"] = "3" * 64
    db.on_lock = lambda: db.put(replacement)
    with pytest.raises(PublicAPIError) as error:
        await service.upsert_plugin(PluginResourceRequest(id="generated-tool", name="Edited"))
    assert error.value.status_code == 409
    assert not any(event[0] == "write" for event in db.events)


async def test_regular_registration_cannot_edit_existing_generated_plugin(
    context: AdminContext,
) -> None:
    service, db, _ = context
    original = plugin_payload()
    db.put(original)
    with pytest.raises(PublicAPIError) as error:
        await service.upsert_plugin(PluginResourceRequest(id="generated-tool", name="Edited"))
    assert error.value.status_code == 409
    assert db.plugin() == original


async def test_listing_and_lookup_hide_internal_marker(
    context: AdminContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, _ = context
    original = plugin_payload()
    db.put(original)
    monkeypatch.setattr(service, "_list_admin_payloads", AsyncMock(return_value=[original]))
    monkeypatch.setattr(service, "list_plugin_signing_keys", AsyncMock(return_value=()))
    listed = await service.list_plugins()
    lookup = await service._plugin_response("generated-tool")
    assert lookup is not None
    assert listed[0].id == lookup.id == "generated-tool"
    assert "_generated_handover_id" not in listed[0].model_dump()
    assert db.plugin()["_generated_handover_id"] == original["_generated_handover_id"]


async def test_failed_preflight_does_not_overwrite_package_changed_before_write_lock(
    context: AdminContext,
) -> None:
    service, db, _ = context
    original = reviewed_plugin_payload()
    configure_trusted_generated_review(context)
    db.put(original)
    replacement = deepcopy(original)
    replacement["content_sha256"] = "3" * 64

    async def failed_preflight(_plugin: PluginResourceResponse) -> None:
        db.on_lock = lambda: db.put(replacement)
        raise PublicAPIError(409, "preflight", "failed")

    with pytest.raises(PublicAPIError) as error:
        await service.start_plugin(
            "generated-tool",
            activation_preflight=failed_preflight,
        )
    assert error.value.code == "plugin_registration_conflict"
    assert db.plugin() == replacement
    assert not any(event[0] in {"write", "audit"} for event in db.events)


@pytest.mark.parametrize("failed", [False, True])
async def test_preflight_cannot_write_replaced_generated_owner(
    context: AdminContext,
    failed: bool,
) -> None:
    service, db, _ = context
    original = reviewed_plugin_payload()
    configure_trusted_generated_review(context)
    db.put(original)

    async def preflight(_plugin: PluginResourceResponse) -> None:
        replacement = deepcopy(original)
        replacement["_generated_handover_id"] = "gh-" + "4" * 64
        db.put(replacement)
        if failed:
            raise PublicAPIError(409, "preflight", "failed")

    with pytest.raises(PublicAPIError) as error:
        await service.start_plugin("generated-tool", activation_preflight=preflight)
    assert error.value.code == "plugin_registration_conflict"
    assert not any(event[0] == "write" for event in db.events)
    assert db.plugin()["_generated_handover_id"] == "gh-" + "4" * 64


async def test_controlled_state_requires_existing_row(context: AdminContext) -> None:
    service, db, _ = context
    with pytest.raises(PublicAPIError) as error:
        await service._upsert_admin_payload(
            "plugin",
            "generated-tool",
            _PluginWritePayload(plugin_payload(), state_snapshot={}),
        )
    assert error.value.status_code == 409
    assert db.rows == {}


@pytest.mark.parametrize("concurrent", [False, True])
async def test_config_json_types_are_immutable_and_part_of_snapshot(
    context: AdminContext,
    concurrent: bool,
) -> None:
    service, db, _ = context
    original = plugin_payload()
    original["resource_config"] = {"flag": True}
    db.put(original)
    updated = deepcopy(original)
    updated["resource_config"] = {"flag": 1}
    if concurrent:
        db.on_lock = lambda: db.put(updated)
    with pytest.raises(PublicAPIError) as error:
        await service._upsert_admin_payload(
            "plugin",
            "generated-tool",
            _PluginWritePayload(original if concurrent else updated, state_snapshot=original),
        )
    assert error.value.status_code == 409
    assert not any(event[0] == "write" for event in db.events)


async def test_other_kinds_and_no_database_legacy_fallback_remain(context: AdminContext) -> None:
    service, db, _ = context
    assert await service._upsert_admin_payload("setting", "x", {"value": 1})
    assert not any(event[0] == "lock" for event in db.events)
    service._session_factory = None
    assert await service._upsert_admin_payload("plugin", "x", {}) is False
    assert await service._mutate_admin_payloads(deletes=(("plugin", "x"),)) is False
    assert await service._delete_admin_payload("plugin", "x") is None
    registered = await service.upsert_plugin(PluginResourceRequest(id="legacy", name="Legacy"))
    assert registered.id == "legacy"


@pytest.mark.parametrize(
    "generated,action",
    [
        (generated, action)
        for generated in (False, True)
        for action in (
            "disable",
            "stop",
            "preflight_failed",
            "approve",
            "reject",
            "start",
            "enable",
            "reload",
            "upsert",
        )
        if not (generated and action in {"upsert", "approve"})
    ],
)
async def test_identical_json_new_row_cannot_receive_old_write(
    context: AdminContext,
    generated: bool,
    action: str,
) -> None:
    service, db, _ = context
    original = plugin_payload(generated=generated, enabled=True)
    if action in {"start", "enable", "reload"}:
        if generated:
            original = reviewed_plugin_payload()
            configure_trusted_generated_review(context)
        else:
            original["package_metadata"]["kind"] = "manifest_only"
    db.put(original)
    new_id = uuid4()

    def replace_on_write_lock(_plugin: PluginResourceResponse | None = None) -> None:
        db.on_lock = lambda: db.put(deepcopy(original), row_id=new_id)

    if action not in {"start", "enable", "reload"}:
        replace_on_write_lock()
    with pytest.raises(PublicAPIError) as error:
        if action in {"approve", "reject"}:
            await getattr(service, action + "_plugin_package")(
                "generated-tool", PluginPackageApprovalRequest()
            )
        elif action == "preflight_failed":
            await service.mark_plugin_preflight_failed("generated-tool")
        elif action == "upsert":
            await service.upsert_plugin(PluginResourceRequest(id="generated-tool", name="Edited"))
        elif action in {"start", "enable", "reload"}:
            await getattr(service, action + "_plugin")(
                "generated-tool", activation_preflight=AsyncMock(side_effect=replace_on_write_lock)
            )
        else:
            await getattr(service, action + "_plugin")("generated-tool")
    assert error.value.code == "plugin_registration_conflict"
    assert db.plugin() == original
    assert db.row_ids[(db.tenant_id, "plugin", "generated-tool")] == new_id
    assert not any(event[0] in {"write", "audit"} for event in db.events)


@pytest.mark.parametrize("action", ["start", "enable", "reload"])
async def test_generated_activation_rechecks_review_at_write_lock(
    context: AdminContext, action: str
) -> None:
    service, db, tenant_id = context
    original = reviewed_plugin_payload()
    db.put(original)
    configure_trusted_generated_review(context)
    before, before_ids = deepcopy(db.rows), deepcopy(db.row_ids)

    async def revoked(session: AsyncSession, *, plugin_row: AdminResourceRow) -> bool:
        assert session is cast(AsyncSession, db) and db.active
        assert plugin_row.id == before_ids[(tenant_id, "plugin", "generated-tool")]
        return False

    async def preflight(_plugin: PluginResourceResponse) -> None:
        service._generated_plugin_approval_validator = revoked

    with pytest.raises(PublicAPIError) as error:
        await getattr(service, action + "_plugin")(
            "generated-tool", activation_preflight=preflight
        )
    assert error.value.status_code == 409
    assert error.value.code == "plugin_generated_review_required"
    assert db.rows == before and db.row_ids == before_ids
    assert not any(event[0] in {"write", "audit"} for event in db.events)
