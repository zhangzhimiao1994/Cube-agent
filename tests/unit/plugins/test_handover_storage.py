"""Storage boundary tests; SQL doubles do not prove PostgreSQL concurrency."""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, Self, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement, TextClause

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunRow
from agent_hub.plugins import handover_storage
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    AuthorizedSource,
    Conflict,
    HandoverReceipt,
    NotFound,
    PlanData,
    PluginHandoverStorage,
    StorageUnavailable,
    VerifiedPendingPlugin,
    WorkspaceSource,
)

POSTGRESQL_DIALECT = cast(type[Dialect], postgresql.dialect)


class StorageModule(Protocol):
    ArtifactSource: type[ArtifactSource]
    AuthorizedSource: type[AuthorizedSource]
    Conflict: type[Conflict]
    NotFound: type[NotFound]
    PlanData: type[PlanData]
    PluginHandoverStorage: type[PluginHandoverStorage]
    StorageUnavailable: type[StorageUnavailable]
    VerifiedPendingPlugin: type[VerifiedPendingPlugin]
    WorkspaceSource: type[WorkspaceSource]

    async def acquire_plugin_registration_lock(
        self, session: AsyncSession, tenant_id: UUID, plugin_id: str
    ) -> None: ...


def payload_mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.fixture(autouse=True)
def reject_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.api.routers.admin import PersistentAdminResourceService
    from agent_hub.capability_installer.environment import CapabilityEnvironmentManager
    from agent_hub.capability_installer.service import CapabilityInstallerService
    from agent_hub.plugins.runtime import PythonSubprocessPluginPackageRunner, RuntimePluginService
    from agent_hub.runs.repository import RunRepository

    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("handover storage attempted execution or run/outbox mutation")

    for name in (
        "start_plugin",
        "enable_plugin",
        "reload_plugin",
        "upsert_plugin",
        "record_audit_event",
    ):
        monkeypatch.setattr(PersistentAdminResourceService, name, forbidden)
    monkeypatch.setattr(CapabilityInstallerService, "install", forbidden)
    monkeypatch.setattr(RunRepository, "create_run", forbidden)
    monkeypatch.setattr(RunRepository, "deliver_outbox", forbidden)
    for name in ("start", "reload", "reload_strict", "preflight_plugin", "invoke"):
        monkeypatch.setattr(RuntimePluginService, name, forbidden)
    for name in ("preflight", "invoke"):
        monkeypatch.setattr(PythonSubprocessPluginPackageRunner, name, forbidden)

    def forbidden_environment(*args: object, **kwargs: object) -> None:
        pytest.fail("handover storage attempted environment preparation")

    for name in ("build_python", "deploy_cli"):
        monkeypatch.setattr(CapabilityEnvironmentManager, name, forbidden_environment)


@pytest.fixture
def hs() -> StorageModule:
    return handover_storage


class SQLBoundary:
    """Only a unit SQL recorder, deliberately not a production repository."""

    def __init__(self, principal: AuthenticatedPrincipal, run_id: UUID) -> None:
        self.runs = {(principal.tenant_id, run_id)}
        self.rows: dict[tuple[UUID, str, str], AdminResourceRow] = {}
        self.statements: list[ClauseElement] = []
        self.transactions = 0
        self.active = False
        self.fail_kind: str | None = None
        self.dialect = "postgresql"
        self.lock_key: str = ""

    def __call__(self) -> SQLBoundary:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def get_bind(self) -> Mock:
        return Mock(dialect=type("Dialect", (), {"name": self.dialect})())

    def in_transaction(self) -> bool:
        return self.active

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        previous = deepcopy(self.rows)
        self.transactions += 1
        self.active = True
        try:
            yield
        except BaseException:
            self.rows = previous
            raise
        finally:
            self.active = False

    def params(self, statement: ClauseElement) -> dict[str, Any]:
        params = statement.compile(dialect=POSTGRESQL_DIALECT()).params
        assert params is not None
        return cast(dict[str, Any], params)

    async def scalar(self, statement: Select[tuple[Any, ...]]) -> RunRow | AdminResourceRow | None:
        assert self.active
        self.statements.append(statement)
        params = self.params(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is RunRow:
            pair = (params["tenant_id_1"], params["id_1"])
            return RunRow(id=pair[1], tenant_id=pair[0]) if pair in self.runs else None
        row = self.rows.get((params["tenant_id_1"], params["kind_1"], params["resource_id_1"]))
        if row and "param_1" in params and row.payload["run_id"] != params["param_1"]:
            return None
        return row

    async def execute(
        self, statement: TextClause | Insert, parameters: Mapping[str, object] | None = None
    ) -> None:
        assert self.active
        self.statements.append(statement)
        if isinstance(statement, TextClause):
            assert str(statement) == (
                "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
            )
            assert parameters is not None
            lock_key = parameters["lock_key"]
            assert isinstance(lock_key, str)
            self.lock_key = lock_key
            return
        assert isinstance(statement, Insert)
        params = self.params(statement)
        if params["kind"] == self.fail_kind:
            raise OperationalError("injected", None, Exception("private database detail"))
        key = (params["tenant_id"], params["kind"], params["resource_id"])
        if key in self.rows:
            if getattr(statement, "_post_values_clause", None) is not None:
                return
            raise IntegrityError("insert", None, Exception("duplicate"))
        self.rows[key] = AdminResourceRow(**params)

    async def flush(self) -> None:
        assert self.active
        if self.fail_kind == "receipt" and any(
            row.kind == "plugin_handover" and row.payload["status"] == "pending"
            for row in self.rows.values()
        ):
            raise OperationalError("injected receipt failure", None, Exception("private detail"))


UnitContext = tuple[
    PluginHandoverStorage,
    SQLBoundary,
    AuthenticatedPrincipal,
    UUID,
    AuthorizedSource,
    PlanData,
    list[datetime],
]


@pytest.fixture
def context(hs: StorageModule) -> UnitContext:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    run_id = uuid4()
    clock = [datetime(2026, 10, 12, tzinfo=UTC)]
    db = SQLBoundary(principal, run_id)
    storage = hs.PluginHandoverStorage(
        cast(async_sessionmaker[AsyncSession], db), clock=lambda: clock[0]
    )
    source = hs.AuthorizedSource(
        tenant_id=principal.tenant_id,
        run_id=run_id,
        source=hs.ArtifactSource(artifact_id="artifact-1"),
        source_manifest_sha256="1" * 64,
    )
    plan_data = hs.PlanData(
        plugin_id="generated-tool",
        plugin_version="1.0.0",
        permissions=("plugin.use",),
        risk_summary=("local_process",),
    )
    return storage, db, principal, run_id, source, plan_data, clock


async def create(context: UnitContext, **overrides: Any) -> HandoverReceipt:
    storage, _, principal, run_id, source, plan_data, clock = context
    args: dict[str, Any] = {
        "principal": principal,
        "run_id": run_id,
        "authorized_source": source,
        "plan_data": plan_data,
        "idempotency_key": "plan-1",
        "expires_at": clock[0] + timedelta(hours=1),
    }
    args.update(overrides)
    return await storage.create_plan(**args)


def admin(principal: AuthenticatedPrincipal) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), principal.tenant_id, Role.ADMIN)


def verified(
    hs: StorageModule, context: UnitContext, plan: HandoverReceipt, **overrides: Any
) -> VerifiedPendingPlugin:
    _, _, principal, run_id, source, _, clock = context
    payload: dict[str, object] = {
        "id": plan.plugin_id,
        "name": "Generated tool",
        "version": plan.plugin_version,
        "enabled": False,
        "status": "disabled",
        "health": "disabled",
        "resource_config": {},
        "capabilities": [],
        "package_metadata": {
            "kind": "adapter_package",
            "package_version": plan.plugin_version,
            "signature": {"algorithm": "ed25519", "key_id": "key-1", "value": "A" * 86},
            "signature_verification": "verified",
            "verified_public_key_sha256": "4" * 64,
            "approval_state": "pending",
            "activation_state": "blocked_pending_approval",
            "runtime": "python",
            "entrypoint": "adapter.py",
            "isolation": "local_process",
            "install_mode": "scan_only",
            "artifact": {
                "storage_key": f"{principal.tenant_id}/{plan.plugin_id}/{'3' * 64}",
                "content_sha256": "3" * 64,
                "file_count": 2,
                "total_size_bytes": 512,
                "stored_at": clock[0].isoformat(),
                "quarantine_state": "stored",
            },
        },
    }
    args: dict[str, Any] = {
        "tenant_id": principal.tenant_id,
        "run_id": run_id,
        "handover_id": plan.handover_id,
        "plan_hash": plan.plan_hash,
        "source_manifest_sha256": source.source_manifest_sha256,
        "signature_payload_sha256": "2" * 64,
        "archive_sha256": "3" * 64,
        "key_id": "key-1",
        "verified_public_key_sha256": "4" * 64,
        "artifact_ref": "internal-artifact-1",
        "plugin_payload": payload,
    }
    args.update(overrides)
    return hs.VerifiedPendingPlugin(**args)


async def commit(
    hs: StorageModule, context: UnitContext, plan: HandoverReceipt, **overrides: Any
) -> HandoverReceipt:
    storage, _, principal, run_id, *_ = context
    args: dict[str, Any] = {
        "principal": admin(principal),
        "run_id": run_id,
        "handover_id": plan.handover_id,
        "expected_version": plan.version,
        "plan_hash": plan.plan_hash,
        "confirm": True,
        "idempotency_key": "confirm-1",
        "verified_pending_plugin": verified(hs, context, plan),
    }
    args.update(overrides)
    return await storage.commit_pending(**args)


async def test_plan_replay_preserves_identity_expiry_and_safe_projection(
    context: UnitContext,
) -> None:
    storage, db, principal, run_id, _, _, clock = context
    plan = await create(context)
    clock[0] += timedelta(hours=2)
    replay = await create(context)
    assert replay == plan
    assert len(plan.handover_id) == 67 and plan.handover_id.startswith("gh-")
    assert plan.status == "awaiting_signature" and plan.installer_id is None
    assert plan.version == 1 and plan.source_manifest_sha256 == "1" * 64
    assert len(db.rows) == 1
    viewer = AuthenticatedPrincipal(uuid4(), principal.tenant_id, Role.VIEWER)
    assert await storage.get_receipt(viewer, run_id, plan.handover_id) == plan
    public = plan.model_dump(mode="json")
    for hidden in (
        "plan_key_digest",
        "confirm_key_digest",
        "artifact_ref",
        "plugin_payload",
        "confirm_request_sha256",
        "request_fingerprint",
    ):
        assert hidden not in public
    sql = str(
        next(s for s in db.statements if isinstance(s, Insert)).compile(
            dialect=POSTGRESQL_DIALECT()
        )
    )
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql and "DO UPDATE" not in sql


async def test_same_plan_key_changed_content_or_run_conflicts(
    context: UnitContext, hs: StorageModule
) -> None:
    await create(context)
    changed = hs.PlanData(plugin_id="other", plugin_version="1.0.0")
    with pytest.raises(hs.Conflict):
        await create(context, plan_data=changed)
    _, db, principal, _, source, *_ = context
    other_run = uuid4()
    db.runs.add((principal.tenant_id, other_run))
    with pytest.raises(hs.Conflict):
        await create(
            context,
            run_id=other_run,
            authorized_source=source.model_copy(update={"run_id": other_run}),
        )
    assert len(db.rows) == 1


async def test_plan_keys_are_scoped_to_creator_and_tenant(context: UnitContext) -> None:
    first = await create(context)
    _, db, principal, run_id, source, *_ = context
    second = await create(
        context, principal=AuthenticatedPrincipal(uuid4(), principal.tenant_id, Role.OPERATOR)
    )
    assert first.handover_id != second.handover_id
    foreign = AuthenticatedPrincipal(principal.user_id, uuid4(), Role.OPERATOR)
    db.runs.add((foreign.tenant_id, run_id))
    third = await create(
        context,
        principal=foreign,
        authorized_source=source.model_copy(update={"tenant_id": foreign.tenant_id}),
    )
    assert third.handover_id not in (first.handover_id, second.handover_id)


async def test_real_run_and_authorized_source_bindings_are_required(
    context: UnitContext, hs: StorageModule
) -> None:
    _, db, _, _, source, *_ = context
    db.runs.clear()
    with pytest.raises(hs.NotFound):
        await create(context)
    assert not db.rows
    with pytest.raises(ValueError):
        await create(context, authorized_source=source.model_dump())


@pytest.mark.parametrize("field", ["tenant_id", "run_id"])
async def test_source_scope_swap_is_rejected_before_writes(
    context: UnitContext, hs: StorageModule, field: str
) -> None:
    source = context[4].model_copy(update={field: uuid4()})
    with pytest.raises(hs.NotFound):
        await create(context, authorized_source=source)
    assert not context[1].rows


@pytest.mark.parametrize("key", ["", "a" * 129, "bad/key", "bad key", "\u4e2d", "x\n"])
async def test_unsafe_idempotency_keys_are_rejected(context: UnitContext, key: str) -> None:
    with pytest.raises(ValueError):
        await create(context, idempotency_key=key)
    assert not context[1].rows


@pytest.mark.parametrize(
    "paths", [("../a.py",), ("/a.py",), ("C:/a.py",), ("a\\b.py",), ("a//b.py",), ("a.py", "a.py")]
)
def test_workspace_paths_cannot_expose_or_escape_server_paths(
    hs: StorageModule, paths: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        hs.WorkspaceSource(project_id="project-1", session_id="session-1", paths=paths)


def test_dtos_reject_client_controlled_state_and_unbounded_fields(
    hs: StorageModule, context: UnitContext
) -> None:
    with pytest.raises(ValidationError):
        hs.PlanData.model_validate(
            {"plugin_id": "p", "plugin_version": "1.0.0", "creator_id": uuid4()}
        )
    with pytest.raises(ValidationError):
        hs.PlanData(plugin_id="p", plugin_version="1.0.0", risk_summary=("x" * 129,))
    with pytest.raises(ValidationError):
        hs.ArtifactSource(artifact_id="/private/source.py")


async def test_permission_matrix_precedes_storage_and_replays(
    context: UnitContext, hs: StorageModule
) -> None:
    storage, _, principal, run_id, *_ = context
    viewer = AuthenticatedPrincipal(principal.user_id, principal.tenant_id, Role.VIEWER)
    unavailable = hs.PluginHandoverStorage(None)
    with pytest.raises(PermissionDenied):
        await create(context, principal=viewer)
    plan = await create(context)
    for current in (principal, viewer):
        with pytest.raises(PermissionDenied):
            await commit(hs, context, plan, principal=current)
    with pytest.raises(PermissionDenied):
        await unavailable.cancel_plan(viewer, run_id, plan.handover_id, 1, plan.plan_hash)
    with pytest.raises(hs.StorageUnavailable):
        await unavailable.get_receipt(principal, run_id, plan.handover_id)
    context[1].dialect = "sqlite"
    with pytest.raises(hs.StorageUnavailable):
        await storage.get_receipt(principal, run_id, plan.handover_id)


async def test_read_receipt_hides_cross_tenant_and_cross_run(
    context: UnitContext, hs: StorageModule
) -> None:
    storage, _, principal, run_id, *_ = context
    plan = await create(context)
    with pytest.raises(hs.NotFound):
        await storage.get_receipt(principal, uuid4(), plan.handover_id)
    foreign = AuthenticatedPrincipal(principal.user_id, uuid4(), Role.ADMIN)
    with pytest.raises(hs.NotFound):
        await storage.get_receipt(foreign, run_id, plan.handover_id)


async def test_operator_can_cancel_only_own_plan_and_replay_after_expiry(
    context: UnitContext, hs: StorageModule
) -> None:
    storage, db, principal, run_id, _, _, clock = context
    plan = await create(context)
    other = AuthenticatedPrincipal(uuid4(), principal.tenant_id, Role.OPERATOR)
    with pytest.raises(PermissionDenied):
        await storage.cancel_plan(other, run_id, plan.handover_id, 1, plan.plan_hash)
    cancelled = await storage.cancel_plan(principal, run_id, plan.handover_id, 1, plan.plan_hash)
    assert cancelled.status == "cancelled" and cancelled.version == 2
    clock[0] += timedelta(days=2)
    assert (
        await storage.cancel_plan(principal, run_id, plan.handover_id, 1, plan.plan_hash)
        == cancelled
    )
    with pytest.raises(hs.Conflict):
        await storage.cancel_plan(principal, run_id, plan.handover_id, 2, "0" * 64)
    with pytest.raises(hs.Conflict):
        await commit(hs, context, plan)
    assert len(db.rows) == 1


@pytest.mark.parametrize(
    "version,hash_value,expired", [(2, None, False), (1, "0" * 64, False), (1, None, True)]
)
async def test_cancel_checks_version_hash_and_expiry(
    context: UnitContext, hs: StorageModule, version: int, hash_value: str | None, expired: bool
) -> None:
    storage, _, principal, run_id, _, _, clock = context
    plan = await create(context)
    if expired:
        clock[0] += timedelta(hours=1)
    with pytest.raises(hs.Conflict):
        await storage.cancel_plan(
            principal, run_id, plan.handover_id, version, hash_value or plan.plan_hash
        )


async def test_admin_can_cancel_another_creators_plan(context: UnitContext) -> None:
    storage, _, principal, run_id, *_ = context
    plan = await create(context)
    result = await storage.cancel_plan(
        admin(principal), run_id, plan.handover_id, 1, plan.plan_hash
    )
    assert result.creator_id == principal.user_id and result.status == "cancelled"


async def test_commit_writes_disabled_plugin_marker_receipt_and_single_audit(
    context: UnitContext, hs: StorageModule
) -> None:
    storage, db, principal, run_id, _, _, clock = context
    plan = await create(context)
    installer = admin(principal)
    result = await commit(hs, context, plan, principal=installer)
    assert result.status == "pending" and result.version == 2
    assert result.creator_id == principal.user_id and result.installer_id == installer.user_id
    assert result.archive_sha256 == "3" * 64 and result.signature_payload_sha256 == "2" * 64
    assert len(db.rows) == 3 and db.transactions == 2
    plugin = db.rows[(principal.tenant_id, "plugin", plan.plugin_id)].payload
    assert plugin["_generated_handover_id"] == plan.handover_id
    assert "_generated_handover_id" not in payload_mapping(plugin["resource_config"])
    assert plugin["enabled"] is False and plugin["status"] == "disabled"
    package = payload_mapping(plugin["package_metadata"])
    assert package["signature_verification"] == "verified"
    assert package["approval_state"] == "pending"
    assert package["activation_state"] == "blocked_pending_approval"
    assert db.lock_key == f"plugin-registration:{principal.tenant_id}:{plan.plugin_id}"
    assert result.audit_id is not None
    audit = db.rows[(principal.tenant_id, "audit", result.audit_id)].payload
    assert audit["actor"] == str(installer.user_id)
    assert payload_mapping(audit["details"])["handover_id"] == plan.handover_id
    assert "storage_key" not in str(audit)
    assert "storage_key" not in result.model_dump_json()
    clock[0] += timedelta(days=2)
    replay = await commit(hs, context, plan, principal=installer)
    assert replay == result and len(db.rows) == 3
    recreated = hs.PluginHandoverStorage(
        cast(async_sessionmaker[AsyncSession], db), clock=lambda: clock[0]
    )
    assert await recreated.get_receipt(principal, run_id, plan.handover_id) == result
    viewer = AuthenticatedPrincipal(installer.user_id, installer.tenant_id, Role.VIEWER)
    with pytest.raises(PermissionDenied):
        await commit(hs, context, plan, principal=viewer)
    with pytest.raises(hs.Conflict):
        await storage.cancel_plan(principal, run_id, plan.handover_id, 2, plan.plan_hash)


@pytest.mark.parametrize("expiry_boundary", ["registration_lock", "registration_read"])
async def test_commit_rechecks_expiry_after_registration_wait(
    context: UnitContext,
    hs: StorageModule,
    monkeypatch: pytest.MonkeyPatch,
    expiry_boundary: str,
) -> None:
    _, db, _, _, _, _, clock = context
    plan = await create(context)
    before = {key: deepcopy(row.payload) for key, row in db.rows.items()}
    statement_count = len(db.statements)
    acquire = hs.acquire_plugin_registration_lock
    scalar = db.scalar

    async def acquire_then_expire(
        session: AsyncSession,
        tenant_id: UUID,
        plugin_id: str,
    ) -> None:
        await acquire(session, tenant_id, plugin_id)
        if expiry_boundary == "registration_lock":
            clock[0] = plan.expires_at

    async def read_then_expire(
        statement: Select[tuple[Any, ...]],
    ) -> RunRow | AdminResourceRow | None:
        result = await scalar(statement)
        if (
            expiry_boundary == "registration_read"
            and db.params(statement).get("kind_1") == "plugin"
        ):
            clock[0] = plan.expires_at
        return result

    monkeypatch.setattr(hs, "acquire_plugin_registration_lock", acquire_then_expire)
    monkeypatch.setattr(db, "scalar", read_then_expire)
    with pytest.raises(hs.Conflict, match="no longer confirmable"):
        await commit(hs, context, plan)
    assert {key: row.payload for key, row in db.rows.items()} == before
    assert not any(isinstance(statement, Insert) for statement in db.statements[statement_count:])


async def test_pending_replays_reject_changed_key_actor_or_content(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    installer = admin(context[2])
    await commit(hs, context, plan, principal=installer)
    for overrides in (
        {"idempotency_key": "new-key"},
        {"principal": admin(context[2])},
        {"expected_version": 2},
        {"plan_hash": "0" * 64},
        {"verified_pending_plugin": verified(hs, context, plan, signature_payload_sha256="5" * 64)},
    ):
        args = {"principal": installer, **overrides}
        with pytest.raises(hs.Conflict):
            await commit(hs, context, plan, **args)
    assert len(context[1].rows) == 3


@pytest.mark.parametrize("confirm", [False, 1, "true", None])
async def test_confirmation_is_strict_boolean(
    context: UnitContext, hs: StorageModule, confirm: object
) -> None:
    plan = await create(context)
    with pytest.raises(ValueError):
        await commit(hs, context, plan, confirm=confirm)
    assert len(context[1].rows) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", uuid4()),
        ("run_id", uuid4()),
        ("source_manifest_sha256", "5" * 64),
        ("plan_hash", "5" * 64),
        ("handover_id", "gh-" + "5" * 64),
    ],
)
async def test_verified_result_must_match_plan(
    context: UnitContext, hs: StorageModule, field: str, value: object
) -> None:
    plan = await create(context)
    overrides: dict[str, object] = {field: value}
    if field == "tenant_id":
        payload = deepcopy(verified(hs, context, plan).plugin_payload)
        payload_mapping(payload_mapping(payload["package_metadata"])["artifact"])["storage_key"] = (
            f"{value}/{plan.plugin_id}/{'3' * 64}"
        )
        overrides["plugin_payload"] = payload
    metadata = verified(hs, context, plan, **overrides)
    with pytest.raises(hs.Conflict):
        await commit(hs, context, plan, verified_pending_plugin=metadata)
    assert len(context[1].rows) == 1


async def test_raw_verified_dict_and_enabled_or_untrusted_package_are_rejected(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    good = verified(hs, context, plan)
    with pytest.raises(ValueError):
        await commit(hs, context, plan, verified_pending_plugin=good.model_dump())
    for change in ("enabled", "signature_verification", "approval_state", "content_sha256"):
        payload = deepcopy(good.plugin_payload)
        if change == "enabled":
            payload[change] = True
        elif change == "content_sha256":
            payload_mapping(payload_mapping(payload["package_metadata"])["artifact"])[change] = (
                "9" * 64
            )
        else:
            payload_mapping(payload["package_metadata"])[change] = (
                "untrusted_key" if (change == "signature_verification") else "approved"
            )
        with pytest.raises(ValueError):
            await commit(
                hs,
                context,
                plan,
                verified_pending_plugin=good.model_copy(update={"plugin_payload": payload}),
            )
    assert len(context[1].rows) == 1


async def test_existing_plugin_is_never_overwritten(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    _, db, principal, *_ = context
    key = (principal.tenant_id, "plugin", plan.plugin_id)
    db.rows[key] = AdminResourceRow(
        tenant_id=principal.tenant_id,
        kind="plugin",
        resource_id=plan.plugin_id,
        payload={"enabled": True},
    )
    with pytest.raises(hs.Conflict):
        await commit(hs, context, plan)
    assert db.rows[key].payload == {"enabled": True} and len(db.rows) == 2


@pytest.mark.parametrize("kind", ["plugin", "receipt", "audit"])
async def test_database_write_failure_never_returns_pending(
    context: UnitContext, hs: StorageModule, kind: str
) -> None:
    plan = await create(context)
    context[1].fail_kind = kind
    with pytest.raises(hs.StorageUnavailable, match="storage unavailable") as captured:
        await commit(hs, context, plan)
    assert "private database" not in str(captured.value)
    receipt = await context[0].get_receipt(context[2], context[3], plan.handover_id)
    assert receipt == plan and len(context[1].rows) == 1


async def test_registration_helper_requires_transaction_and_postgres(
    context: UnitContext, hs: StorageModule
) -> None:
    db = context[1]
    with pytest.raises(hs.StorageUnavailable):
        await hs.acquire_plugin_registration_lock(
            cast(AsyncSession, db), context[2].tenant_id, "generated-tool"
        )
    async with db.begin():
        await hs.acquire_plugin_registration_lock(
            cast(AsyncSession, db), context[2].tenant_id, "generated-tool"
        )
    assert db.lock_key.endswith(":generated-tool")


async def test_handover_payload_exposes_stable_plugin_identity_for_historical_lookup(
    context: UnitContext,
) -> None:
    plan = await create(context)
    record = next(iter(context[1].rows.values())).payload
    assert record["plugin_id"] == plan.plugin_id
    assert record["plugin_version"] == plan.plugin_version


async def test_plan_request_has_fixed_canonical_identity_vector(context: UnitContext) -> None:
    principal = AuthenticatedPrincipal(UUID(int=1), UUID(int=2), Role.OPERATOR)
    context[1].runs.add((principal.tenant_id, context[3]))
    source = context[4].model_copy(update={"tenant_id": principal.tenant_id})
    plan = await create(context, principal=principal, authorized_source=source)
    assert plan.handover_id == (
        "gh-50b185cd2d10f17c6adf01c77b9226f6426a4f44c33a49fe39725d1e5b03a86d"
    )


async def test_commit_checks_row_lock_before_shared_plugin_lock(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    start = len(context[1].statements)
    await commit(hs, context, plan)
    statements = context[1].statements[start:]
    lock_index = next(i for i, s in enumerate(statements) if isinstance(s, TextClause))
    row_lock = str(statements[lock_index - 1].compile(dialect=POSTGRESQL_DIALECT()))
    assert "FOR UPDATE" in row_lock
    params = context[1].params(statements[lock_index - 1])
    assert params["payload_1"] == "run_id" and params["param_1"] == str(context[3])
    plugin_lock = str(statements[lock_index + 1].compile(dialect=POSTGRESQL_DIALECT()))
    assert "FOR UPDATE" in plugin_lock


async def test_completed_receipt_cannot_silently_accept_corrupt_terminal_metadata(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    await commit(hs, context, plan)
    key = (context[2].tenant_id, "plugin_handover", plan.handover_id)
    payload = deepcopy(context[1].rows[key].payload)
    payload["audit_id"] = None
    context[1].rows[key].payload = payload
    with pytest.raises(hs.StorageUnavailable):
        await context[0].get_receipt(context[2], context[3], plan.handover_id)


async def test_cancel_replay_still_rejects_downgraded_creator(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    await context[0].cancel_plan(context[2], context[3], plan.handover_id, 1, plan.plan_hash)
    viewer = AuthenticatedPrincipal(context[2].user_id, context[2].tenant_id, Role.VIEWER)
    with pytest.raises(PermissionDenied):
        await context[0].cancel_plan(viewer, context[3], plan.handover_id, 1, plan.plan_hash)


async def test_signed_plan_digests_cannot_be_substituted_at_confirmation(
    context: UnitContext, hs: StorageModule
) -> None:
    signed = hs.PlanData(
        plugin_id="generated-tool",
        plugin_version="1.0.0",
        signature_payload_sha256="6" * 64,
        archive_sha256="7" * 64,
    )
    plan = await create(context, plan_data=signed)
    assert plan.status == "planned"
    with pytest.raises(hs.Conflict):
        await commit(hs, context, plan)
    assert len(context[1].rows) == 1


@pytest.mark.parametrize(
    "code,expected_error", [("23505", "Conflict"), ("23514", "StorageUnavailable")]
)
async def test_postgres_unique_and_schema_errors_are_mapped_safely(
    context: UnitContext,
    hs: StorageModule,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected_error: str,
) -> None:
    plan = await create(context)

    async def fail_statement(*args: object, **kwargs: object) -> None:
        original = type("DatabaseFailure", (Exception,), {"sqlstate": code})("private SQL")
        raise IntegrityError("private statement", None, original)

    monkeypatch.setattr(context[1], "execute", fail_statement)
    with pytest.raises(getattr(hs, expected_error)) as captured:
        await commit(hs, context, plan)
    assert "private" not in str(captured.value)
    assert len(context[1].rows) == 1


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(days=8)])
async def test_new_plans_require_bounded_future_expiry(
    context: UnitContext, offset: timedelta
) -> None:
    with pytest.raises(ValueError):
        await create(context, expires_at=context[6][0] + offset)
    assert not context[1].rows


async def test_workspace_normalization_produces_one_immutable_plan(
    context: UnitContext, hs: StorageModule
) -> None:
    source = context[4].model_copy(
        update={
            "source": hs.WorkspaceSource(
                project_id="project-1", session_id="session-1", paths=("src/b.py", "src/a.py")
            )
        }
    )
    first = await create(context, authorized_source=source)
    assert isinstance(first.source, WorkspaceSource)
    assert first.source.paths == ("src/a.py", "src/b.py")
    reordered = source.model_copy(
        update={
            "source": hs.WorkspaceSource(
                project_id="project-1", session_id="session-1", paths=("src/a.py", "src/b.py")
            )
        }
    )
    assert await create(context, authorized_source=reordered) == first


async def test_replay_cannot_change_registered_plugin_manifest(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    installer = admin(context[2])
    await commit(hs, context, plan, principal=installer)
    metadata = verified(hs, context, plan)
    payload = deepcopy(metadata.plugin_payload)
    payload["name"] = "Different manifest"
    changed = metadata.model_copy(update={"plugin_payload": payload})
    with pytest.raises(hs.Conflict):
        await commit(hs, context, plan, principal=installer, verified_pending_plugin=changed)
    assert len(context[1].rows) == 3


@pytest.mark.parametrize("changed", ["tenant", "plugin", "hash"])
async def test_package_artifact_storage_reference_is_bound_to_tenant_plugin_and_hash(
    context: UnitContext, hs: StorageModule, changed: str
) -> None:
    plan = await create(context)
    metadata = verified(hs, context, plan)
    payload = deepcopy(metadata.plugin_payload)
    tenant = str(uuid4()) if changed == "tenant" else str(context[2].tenant_id)
    plugin = "different-plugin" if changed == "plugin" else plan.plugin_id
    digest = "9" * 64 if changed == "hash" else "3" * 64
    payload_mapping(payload_mapping(payload["package_metadata"])["artifact"])["storage_key"] = (
        f"{tenant}/{plugin}/{digest}"
    )
    with pytest.raises(ValueError):
        await commit(
            hs,
            context,
            plan,
            verified_pending_plugin=metadata.model_copy(update={"plugin_payload": payload}),
        )
    assert len(context[1].rows) == 1


async def test_historical_pending_receipt_is_independent_of_live_plugin_lifecycle(
    context: UnitContext, hs: StorageModule
) -> None:
    plan = await create(context)
    original = await commit(hs, context, plan)
    plugin_key = (context[2].tenant_id, "plugin", plan.plugin_id)
    live = deepcopy(context[1].rows[plugin_key].payload)
    payload_mapping(live["package_metadata"])["approval_state"] = "approved"
    payload_mapping(live["package_metadata"])["activation_state"] = "eligible"
    context[1].rows[plugin_key].payload = live
    assert await context[0].get_receipt(context[2], context[3], plan.handover_id) == original
    assert original.result == "blocked_pending_approval"


def test_storage_contract_is_available() -> None:
    assert importlib.util.find_spec("agent_hub.plugins.handover_storage") is not None


GRANT_ID = "sg-" + "a" * 64
GRANT_HASH = "b" * 64


def test_plan_identity_is_legacy_pure_key_digest(context: UnitContext) -> None:
    creator = context[2]
    expected = "gh-" + handover_storage._digest(
        "plan-key",
        {
            "tenant_id": str(creator.tenant_id),
            "creator_id": str(creator.user_id),
            "key": "plan-1",
        },
    )
    assert handover_storage.plan_identity(creator, "plan-1") == expected
    with pytest.raises(ValueError):
        handover_storage.plan_identity(creator, "bad/key")


@pytest.fixture
async def caller_session(
    context: UnitContext, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    # Actual caller-owned BEGIN, intercepted SQL only; not actual PG lock evidence.
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    db = context[1]
    try:
        async with AsyncSession(engine) as session, session.begin(), db.begin():
            monkeypatch.setattr(session, "scalar", AsyncMock(side_effect=db.scalar))
            monkeypatch.setattr(session, "execute", AsyncMock(side_effect=db.execute))
            monkeypatch.setattr(session, "flush", AsyncMock(side_effect=db.flush))
            yield session
    finally:
        await engine.dispose()


async def create_in_session(
    context: UnitContext, session: AsyncSession, **overrides: Any
) -> HandoverReceipt:
    storage, _, principal, run_id, source, plan_data, clock = context
    args: dict[str, Any] = {
        "source_grant_id": GRANT_ID,
        "source_grant_binding_sha256": GRANT_HASH,
    }
    args.update(overrides)
    return await storage.create_plan_in_session(
        session,
        principal,
        run_id,
        source,
        plan_data,
        "plan-1",
        clock[0] + timedelta(hours=1),
        **args,
    )


async def commit_in_session(
    context: UnitContext,
    session: AsyncSession,
    receipt: HandoverReceipt,
    *,
    validate: Callable[[AsyncSession, HandoverReceipt, VerifiedPendingPlugin], Awaitable[None]],
    consume: Callable[[AsyncSession, HandoverReceipt], Awaitable[None]],
    principal: AuthenticatedPrincipal | None = None,
) -> HandoverReceipt:
    storage, _, creator, run_id, *_ = context
    return await storage._commit_pending_in_session(
        session,
        principal or admin(creator),
        run_id,
        receipt.handover_id,
        1,
        receipt.plan_hash,
        True,
        "confirm-1",
        verified(handover_storage, context, receipt),
        validate_bridge=validate,
        consume_bridge=consume,
    )


async def test_grant_pair_persists_and_changes_only_new_plan_fingerprint(
    context: UnitContext,
    caller_session: AsyncSession,
) -> None:
    source, data, run = context[4], context[5], context[3]
    legacy = handover_storage._digest(
        "plan-request",
        {
            "run_id": str(run),
            "source": source.source.model_dump(mode="json"),
            "source_manifest_sha256": source.source_manifest_sha256,
            "plan_data": data.model_dump(mode="json"),
        },
    )
    assert (
        handover_storage._plan_fingerprint(run, source.source, source.source_manifest_sha256, data)
        == legacy
    )
    receipt = await create_in_session(context, caller_session)
    row = next(iter(context[1].rows.values()))
    assert row.payload["source_grant_id"] == receipt.source_grant_id == GRANT_ID
    assert (
        row.payload["source_grant_binding_sha256"]
        == receipt.source_grant_binding_sha256
        == GRANT_HASH
    )
    assert row.payload["request_fingerprint"] != legacy
    assert await create_in_session(context, caller_session) == receipt
    with pytest.raises(Conflict):
        await create_in_session(context, caller_session, source_grant_binding_sha256="c" * 64)
    assert caller_session.in_transaction() and context[1].transactions == 1


@pytest.mark.parametrize(
    "grant_id,binding_sha",
    [
        (GRANT_ID, None),
        (None, GRANT_HASH),
        ("gh-" + "a" * 64, GRANT_HASH),
        (GRANT_ID.upper(), GRANT_HASH),
        ("sg-short", GRANT_HASH),
        (GRANT_ID, "short"),
    ],
)
async def test_grant_pair_strict_domain_and_pairing_before_sql(
    context: UnitContext,
    caller_session: AsyncSession,
    grant_id: str | None,
    binding_sha: str | None,
) -> None:
    with pytest.raises(ValueError):
        await create_in_session(
            context,
            caller_session,
            source_grant_id=grant_id,
            source_grant_binding_sha256=binding_sha,
        )
    assert not context[1].statements and not context[1].rows


async def test_bound_record_cannot_confirm_through_legacy_wrapper(
    context: UnitContext,
    caller_session: AsyncSession,
) -> None:
    receipt = await create_in_session(context, caller_session)
    # Wrapper starts another SQL-double tx, so inspect the shared core refusal directly.
    with pytest.raises(StorageUnavailable, match="bridge"):
        await commit(handover_storage, context, receipt)
    assert len(context[1].rows) == 1


async def test_bound_pending_replay_also_rejects_legacy_wrapper(
    context: UnitContext,
    caller_session: AsyncSession,
) -> None:
    receipt = await create_in_session(context, caller_session)
    installer = admin(context[2])

    async def validate(
        session: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
    ) -> None:
        assert locked.source_grant_id == GRANT_ID

    async def consume(session: AsyncSession, pending: HandoverReceipt) -> None:
        assert pending.receipt_id

    await commit_in_session(
        context, caller_session, receipt, validate=validate, consume=consume, principal=installer
    )
    with pytest.raises(StorageUnavailable, match="bridge"):
        await commit(handover_storage, context, receipt, principal=installer)
    assert len(context[1].rows) == 3


async def test_strict_bridge_rejects_legacy_plan_before_callbacks(
    context: UnitContext,
    caller_session: AsyncSession,
) -> None:
    receipt = await create_in_session(
        context, caller_session, source_grant_id=None, source_grant_binding_sha256=None
    )

    async def validate(
        session: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
    ) -> None:
        pytest.fail("legacy plan reached strict authorization callback")

    async def consume(session: AsyncSession, pending: HandoverReceipt) -> None:
        pytest.fail("legacy plan reached strict consume callback")

    with pytest.raises(Conflict, match="source grant binding"):
        await commit_in_session(
            context, caller_session, receipt, validate=validate, consume=consume
        )
    assert len(context[1].rows) == 1


@pytest.mark.parametrize("field", ["source_grant_id", "source_grant_binding_sha256"])
async def test_persisted_pair_is_part_of_plan_integrity(
    context: UnitContext,
    caller_session: AsyncSession,
    field: str,
) -> None:
    receipt = await create_in_session(context, caller_session)
    row = next(iter(context[1].rows.values()))
    row.payload = {**row.payload, field: ("sg-" if field == "source_grant_id" else "") + "c" * 64}
    with pytest.raises(StorageUnavailable):
        handover_storage.PluginHandoverStorage._record(row, context[2].tenant_id, context[3])
    assert receipt.source_grant_id == GRANT_ID


async def test_missing_legacy_fields_preserve_original_hash_and_readability(
    context: UnitContext,
    caller_session: AsyncSession,
) -> None:
    receipt = await create_in_session(
        context, caller_session, source_grant_id=None, source_grant_binding_sha256=None
    )
    row = next(iter(context[1].rows.values()))
    old_payload = deepcopy(row.payload)
    old_payload.pop("source_grant_id")
    old_payload.pop("source_grant_binding_sha256")
    row.payload = old_payload
    restored = handover_storage.PluginHandoverStorage._record(row, context[2].tenant_id, context[3])
    assert restored.plan_hash == receipt.plan_hash
    assert restored.request_fingerprint == old_payload["request_fingerprint"]
    assert restored.source_grant_id is None and restored.source_grant_binding_sha256 is None


async def test_bridge_callbacks_use_same_tx_after_fresh_locks_then_flushed_receipt(
    context: UnitContext,
    caller_session: AsyncSession,
) -> None:
    receipt = await create_in_session(context, caller_session)
    context[1].statements.clear()
    phases: list[str] = []

    async def validate(
        session: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
    ) -> None:
        assert session is caller_session and session.in_transaction()
        assert locked == receipt and package.plan_hash == locked.plan_hash
        assert len(context[1].rows) == 1
        statements = context[1].statements
        lock_at = next(i for i, s in enumerate(statements) if isinstance(s, TextClause))
        plan_lock, plugin_lock = statements[lock_at - 1], statements[lock_at + 1]
        assert isinstance(plan_lock, Select) and isinstance(plugin_lock, Select)
        assert "FOR UPDATE" in str(plan_lock.compile(dialect=POSTGRESQL_DIALECT()))
        assert "FOR UPDATE" in str(plugin_lock.compile(dialect=POSTGRESQL_DIALECT()))
        assert plan_lock.get_execution_options()["populate_existing"] is True
        assert plugin_lock.get_execution_options()["populate_existing"] is True
        phases.append("validate")

    async def consume(session: AsyncSession, pending: HandoverReceipt) -> None:
        assert session is caller_session and session.in_transaction()
        assert pending.status == "pending" and pending.receipt_id
        assert sorted(row.kind for row in context[1].rows.values()) == [
            "audit",
            "plugin",
            "plugin_handover",
        ]
        phases.append("consume")

    result = await commit_in_session(
        context, caller_session, receipt, validate=validate, consume=consume
    )
    assert result.status == "pending" and phases == ["validate", "consume"]
    assert caller_session.in_transaction() and context[1].transactions == 1


@pytest.mark.parametrize("phase", ["validate", "consume"])
async def test_bridge_expiry_after_callback_propagates_without_own_rollback(
    context: UnitContext,
    caller_session: AsyncSession,
    phase: str,
) -> None:
    receipt = await create_in_session(context, caller_session)

    async def validate(
        session: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
    ) -> None:
        if phase == "validate":
            context[6][0] = receipt.expires_at

    async def consume(session: AsyncSession, pending: HandoverReceipt) -> None:
        if phase == "consume":
            context[6][0] = receipt.expires_at

    with pytest.raises(Conflict):
        await commit_in_session(
            context, caller_session, receipt, validate=validate, consume=consume
        )
    assert caller_session.in_transaction()
    assert len(context[1].rows) == (1 if phase == "validate" else 3)


@pytest.mark.parametrize("phase", ["validate", "consume", "audit"])
async def test_bridge_raw_exception_propagates_and_caller_rolls_back_all_writes(
    context: UnitContext,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    await create(context)
    db = context[1]
    # Old plan creation is only setup; new pair must be hash-bound in the actual seam.
    db.rows.clear()
    before: dict[tuple[UUID, str, str], AdminResourceRow] = {}
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    failure = OperationalError("internal injected statement", None, Exception("private SQL"))
    try:
        with pytest.raises(OperationalError) as caught:
            async with AsyncSession(engine) as session, session.begin(), db.begin():
                monkeypatch.setattr(session, "scalar", AsyncMock(side_effect=db.scalar))
                monkeypatch.setattr(session, "execute", AsyncMock(side_effect=db.execute))
                monkeypatch.setattr(session, "flush", AsyncMock(side_effect=db.flush))
                receipt = await create_in_session(context, session)
                if phase == "audit":
                    db.fail_kind = "audit"

                async def validate(
                    current: AsyncSession, locked: HandoverReceipt, package: VerifiedPendingPlugin
                ) -> None:
                    if phase == "validate":
                        raise failure

                async def consume(current: AsyncSession, pending: HandoverReceipt) -> None:
                    raise failure

                try:
                    await commit_in_session(
                        context, session, receipt, validate=validate, consume=consume
                    )
                except OperationalError:
                    assert session.in_transaction()
                    raise
        if phase != "audit":
            assert caught.value is failure
        assert db.rows == before
    finally:
        await engine.dispose()


@pytest.mark.parametrize("mode", ["absent", "autobegin", "sqlite"])
async def test_caller_seam_requires_actual_explicit_postgres_begin(
    context: UnitContext,
    mode: str,
) -> None:
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    try:
        async with AsyncSession(engine) as session:
            if mode == "autobegin":
                session.add(
                    AdminResourceRow(
                        tenant_id=context[2].tenant_id, kind="audit", resource_id="test", payload={}
                    )
                )
            if mode == "sqlite":
                async with session.begin():
                    original = engine.dialect.name
                    engine.dialect.name = "sqlite"
                    try:
                        with pytest.raises(StorageUnavailable):
                            await create_in_session(context, session)
                    finally:
                        engine.dialect.name = original
            else:
                with pytest.raises(StorageUnavailable):
                    await create_in_session(context, session)
            assert not context[1].rows and not context[1].statements
    finally:
        await engine.dispose()


@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_receipt_seams_borrow_same_begin_without_owning_transaction(
    context: UnitContext,
    caller_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    receipt = await create_in_session(context, caller_session)
    storage, db, principal, run_id, *_ = context

    @asynccontextmanager
    async def forbidden() -> AsyncIterator[AsyncSession]:
        pytest.fail("caller seam opened an owned transaction")
        yield caller_session

    monkeypatch.setattr(storage, "_transaction", forbidden)
    if operation == "get":
        result = await storage.get_receipt_in_session(
            caller_session, principal, run_id, receipt.handover_id
        )
        assert result == receipt
    else:
        result = await storage.cancel_plan_in_session(
            caller_session, principal, run_id, receipt.handover_id, 1, receipt.plan_hash
        )
        assert result.status == "cancelled" and result.version == 2
        assert await storage.cancel_plan_in_session(
            caller_session, principal, run_id, receipt.handover_id, 1, receipt.plan_hash
        ) == result
    assert caller_session.in_transaction() and db.transactions == 1


@pytest.mark.parametrize("operation", ["get", "cancel"])
@pytest.mark.parametrize("mode", ["absent", "autobegin", "sqlite", "nested"])
async def test_receipt_seams_require_explicit_postgres_begin(
    context: UnitContext, operation: str, mode: str
) -> None:
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    storage, db, principal, run_id, *_ = context

    async def invoke(session: AsyncSession) -> None:
        if operation == "get":
            await storage.get_receipt_in_session(session, principal, run_id, "gh-" + "a" * 64)
        else:
            await storage.cancel_plan_in_session(
                session, principal, run_id, "gh-" + "a" * 64, 1, "b" * 64
            )

    try:
        async with AsyncSession(engine) as session:
            if mode == "autobegin":
                session.add(AdminResourceRow(
                    tenant_id=principal.tenant_id, kind="audit", resource_id="unit", payload={}
                ))
            if mode in {"sqlite", "nested"}:
                async with session.begin():
                    if mode == "nested":
                        async with session.begin_nested():
                            with pytest.raises(StorageUnavailable):
                                await invoke(session)
                    else:
                        original = engine.dialect.name
                        engine.dialect.name = "sqlite"
                        try:
                            with pytest.raises(StorageUnavailable):
                                await invoke(session)
                        finally:
                            engine.dialect.name = original
            else:
                with pytest.raises(StorageUnavailable):
                    await invoke(session)
            assert not db.rows and not db.statements
    finally:
        await engine.dispose()


@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_receipt_seams_propagate_raw_sql_error_without_rollback(
    context: UnitContext,
    caller_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    receipt = await create_in_session(context, caller_session)
    failure = OperationalError("private statement", None, Exception("private parameters"))
    monkeypatch.setattr(caller_session, "scalar", AsyncMock(side_effect=failure))
    storage, db, principal, run_id, *_ = context
    with pytest.raises(OperationalError) as caught:
        if operation == "get":
            await storage.get_receipt_in_session(
                caller_session, principal, run_id, receipt.handover_id
            )
        else:
            await storage.cancel_plan_in_session(
                caller_session, principal, run_id, receipt.handover_id, 1, receipt.plan_hash
            )
    assert caught.value is failure
    assert caller_session.in_transaction() and db.transactions == 1
    assert len(db.rows) == 1
