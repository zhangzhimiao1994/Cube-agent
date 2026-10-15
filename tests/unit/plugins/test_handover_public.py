"""Public composition uses actual services and SQL doubles, never live PG."""

from __future__ import annotations

import importlib
import importlib.util
from copy import deepcopy
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import Select
from starlette.requests import Request

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow
from agent_hub.plugins.handover_storage import Conflict, NotFound
from tests.unit.plugins import test_handover_confirmation as confirmation_tests
from tests.unit.plugins import test_handover_plans as plan_tests
from tests.unit.plugins import test_handover_sources as source_tests

source_context = confirmation_tests.source_context
plan_context = confirmation_tests.plan_context
confirmation_context = confirmation_tests.context
forbid_execution = confirmation_tests.forbid_execution


def test_public_service_is_available() -> None:
    assert importlib.util.find_spec("agent_hub.plugins.handover_public") is not None


@pytest.fixture
def context(confirmation_context: dict[str, Any]) -> dict[str, Any]:
    assert importlib.util.find_spec("agent_hub.plugins.handover_public") is not None
    module = importlib.import_module("agent_hub.plugins.handover_public")
    c = confirmation_context
    c["public"] = module.GeneratedPluginHandoverService(
        plans=c["service"], confirmation=c["confirmation"],
        storage=c["storage"], grants=c["grants"],
    )
    return c


async def get(c: dict[str, Any], principal: AuthenticatedPrincipal | None = None) -> Any:
    receipt = c["receipt"]
    return await c["public"].get_receipt(
        c["principal"] if principal is None else principal, receipt.run_id, receipt.handover_id
    )


async def cancel(c: dict[str, Any], principal: AuthenticatedPrincipal | None = None) -> Any:
    receipt = c["receipt"]
    return await c["public"].cancel_plan(
        c["principal"] if principal is None else principal,
        receipt.run_id, receipt.handover_id, 1, receipt.plan_hash,
    )


async def confirm(c: dict[str, Any]) -> Any:
    receipt = c["receipt"]
    return await c["public"].confirm(
        c["installer"], receipt.run_id, receipt.handover_id, 1, receipt.plan_hash,
        True, "confirm-source", c["archive"], "tenant-key", c["request"],
    )


async def test_create_plan_delegates_original_signature_and_result(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = context
    delegated = AsyncMock(wraps=c["service"].create_plan)
    monkeypatch.setattr(c["service"], "create_plan", delegated)
    source = source_tests.workspace_source()
    result = await c["public"].create_plan(
        c["principal"], c["receipt"].run_id, source,
        idempotency_key="same-request", expires_at=plan_tests.EXPIRY,
    )
    assert result == c["receipt"]
    delegated.assert_awaited_once_with(
        c["principal"], c["receipt"].run_id, source,
        idempotency_key="same-request", expires_at=plan_tests.EXPIRY,
    )


async def test_confirm_delegates_actual_request_and_stays_disabled(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = context
    delegated = AsyncMock(wraps=c["confirmation"].confirm)
    monkeypatch.setattr(c["confirmation"], "confirm", delegated)
    assert isinstance(c["request"], Request)
    result = await confirm(c)
    receipt = c["receipt"]
    delegated.assert_awaited_once_with(
        c["installer"], receipt.run_id, receipt.handover_id, 1, receipt.plan_hash,
        True, "confirm-source", c["archive"], "tenant-key", c["request"],
    )
    plugin = next(row for row in c["db"].rows
                  if isinstance(row, AdminResourceRow) and row.kind == "plugin")
    assert result.status == "pending" and result.result == "blocked_pending_approval"
    assert plugin.payload["enabled"] is False and plugin.payload["status"] == "disabled"
    package = plugin.payload["package_metadata"]
    assert isinstance(package, dict) and package["approval_state"] == "pending"


@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_current_user_share_and_storage_seams_share_one_begin(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    c = context
    c["sessions"].clear()
    c["db"].statements.clear()
    seen: list[tuple[str, object, AuthenticatedPrincipal | None]] = []
    user = c["grants"]._user
    getter = c["storage"].get_receipt_in_session
    canceller = c["storage"].cancel_plan_in_session

    async def current(session: Any, tenant: UUID, actor: UUID, *permissions: str) -> Any:
        assert session.in_transaction()
        seen.append(("user", session, None))
        assert {"run:read", "plugin:read"} <= set(permissions)
        return await user(session, tenant, actor, *permissions)

    async def receipt(session: Any, principal: AuthenticatedPrincipal, *args: Any) -> Any:
        assert session.in_transaction()
        seen.append(("get", session, principal))
        return await getter(session, principal, *args)

    async def cancelled(session: Any, principal: AuthenticatedPrincipal, *args: Any) -> Any:
        assert session.in_transaction()
        seen.append(("cancel", session, principal))
        return await canceller(session, principal, *args)

    monkeypatch.setattr(c["grants"], "_user", current)
    monkeypatch.setattr(c["storage"], "get_receipt_in_session", receipt)
    monkeypatch.setattr(c["storage"], "cancel_plan_in_session", cancelled)
    await (get(c) if operation == "get" else cancel(c))
    assert len(c["sessions"]) == 1 and seen[0][0] == "user"
    assert all(item[1] is c["sessions"][0] for item in seen)
    for _, _, principal in seen[1:]:
        assert principal is not None
        assert principal.user_id is c["principal"].user_id
        assert principal.tenant_id is c["principal"].tenant_id
        assert principal.role is Role.OPERATOR
    statement = c["db"].statements[0]
    assert isinstance(statement, Select)
    dialect = cast(type[Dialect], postgresql.dialect)()
    assert "FOR SHARE" in str(statement.compile(dialect=dialect))
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize("operation", ["get", "cancel"])
@pytest.mark.parametrize("current_role,allowed", [("operator", False), ("admin", True)])
async def test_visibility_uses_sql_role_not_stale_principal(
    context: dict[str, Any], operation: str, current_role: str, allowed: bool
) -> None:
    c = context
    c["installer_row"].role = current_role
    stale = AuthenticatedPrincipal(c["installer"].user_id, c["installer"].tenant_id,
                                   Role.SUPER_ADMIN if not allowed else Role.VIEWER)
    before = confirmation_tests.security(c)
    if allowed:
        result = await (get(c, stale) if operation == "get" else cancel(c, stale))
        assert result.status == (c["receipt"].status if operation == "get" else "cancelled")
    else:
        with pytest.raises(NotFound):
            await (get(c, stale) if operation == "get" else cancel(c, stale))
        assert confirmation_tests.security(c) == before


@pytest.mark.parametrize("operation", ["get", "cancel"])
@pytest.mark.parametrize("change", ["disabled", "missing", "wrong_tenant", "invalid_role"])
async def test_current_user_gate_fails_closed_without_mutation(
    context: dict[str, Any], operation: str, change: str
) -> None:
    c = context
    if change == "disabled":
        c["user"].disabled = True
    elif change == "missing":
        c["db"].rows.remove(c["user"])
    elif change == "wrong_tenant":
        c["user"].tenant_id = uuid4()
    else:
        c["user"].role = "untrusted-role"
    before = confirmation_tests.security(c)
    with pytest.raises(PermissionDenied):
        await (get(c) if operation == "get" else cancel(c))
    assert confirmation_tests.security(c) == before


async def test_creator_viewer_can_read_but_cannot_cancel(context: dict[str, Any]) -> None:
    c = context
    c["user"].role = "viewer"
    assert await get(c) == c["receipt"]
    with pytest.raises(PermissionDenied):
        await cancel(c)


async def test_historical_expired_revoked_missing_plugin_receipt_remains_readable(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = context
    result = await confirm(c)
    c["clock"][0] = result.expires_at + timedelta(days=2)
    c["db"].rows[:] = [row for row in c["db"].rows
                        if not isinstance(row, AdminResourceRow)
                        or row.kind not in {"plugin", "plugin_signing_key"}]
    grant_row = next(row for row in c["db"].rows
                     if isinstance(row, AdminResourceRow) and row.resource_id.startswith("sg-"))
    grant_row.payload = {**grant_row.payload, "state": "revoked"}

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("historical receipt tried to reauthorize a grant or read private snapshot")

    monkeypatch.setattr(c["grants"], "_verify", forbidden)
    monkeypatch.setattr(c["store"], "read", forbidden)
    assert await get(c) == result


async def test_cancel_is_idempotent_but_changed_request_conflicts(context: dict[str, Any]) -> None:
    c = context
    result = await cancel(c)
    assert await cancel(c) == result
    assert result.version == 2 and result.status == "cancelled"
    with pytest.raises(Conflict):
        await c["public"].cancel_plan(
            c["principal"], result.run_id, result.handover_id, 1, "f" * 64
        )
    assert await get(c) == result


@pytest.mark.parametrize("operation", ["get", "cancel"])
@pytest.mark.parametrize("field", ["user", "tenant", "run", "principal"])
async def test_public_read_cancel_reject_nonexact_input_ids_before_sql(
    context: dict[str, Any], operation: str, field: str
) -> None:
    c = context
    principal = c["principal"]
    run = c["receipt"].run_id
    if field == "principal":
        principal = object()
    elif field == "run":
        run = str(run)
    else:
        principal = AuthenticatedPrincipal(
            cast(UUID, str(principal.user_id)) if field == "user" else principal.user_id,
            cast(UUID, str(principal.tenant_id)) if field == "tenant" else principal.tenant_id,
            principal.role,
        )
    c["sessions"].clear()
    with pytest.raises(PermissionDenied):
        if operation == "get":
            await c["public"].get_receipt(principal, run, c["receipt"].handover_id)
        else:
            await c["public"].cancel_plan(
                principal, run, c["receipt"].handover_id, 1, c["receipt"].plan_hash
            )
    assert not c["sessions"]


@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_same_begin_failure_preserves_sql_state(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    c = context
    before = deepcopy(confirmation_tests.security(c))
    failure = RuntimeError("private injected detail")
    method = "get_receipt_in_session" if operation == "get" else "cancel_plan_in_session"
    monkeypatch.setattr(c["storage"], method, AsyncMock(side_effect=failure))
    with pytest.raises(RuntimeError) as caught:
        await (get(c) if operation == "get" else cancel(c))
    assert caught.value is failure
    assert confirmation_tests.security(c) == before


@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_native_user_uuid_subclasses_do_not_replace_exact_authenticated_ids(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    c = context

    class NativeUUID(UUID):
        pass

    c["user"].id = NativeUUID(c["principal"].user_id.hex)
    c["user"].tenant_id = NativeUUID(c["principal"].tenant_id.hex)
    getter = c["storage"].get_receipt_in_session
    observed = AsyncMock(wraps=getter)
    monkeypatch.setattr(c["storage"], "get_receipt_in_session", observed)
    await (get(c) if operation == "get" else cancel(c))
    assert observed.await_args is not None
    principal = observed.await_args.args[1]
    assert type(principal.user_id) is type(principal.tenant_id) is UUID
    assert principal.user_id is c["principal"].user_id
    assert principal.tenant_id is c["principal"].tenant_id


async def test_cancel_flush_failure_rolls_back_actual_receipt_mutation(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = context
    before = confirmation_tests.security(c)
    original = c["storage"].cancel_plan_in_session
    failure = RuntimeError("private flush failure")

    async def cancelled(session: Any, *args: Any) -> Any:
        monkeypatch.setattr(session, "flush", AsyncMock(side_effect=failure))
        return await original(session, *args)

    monkeypatch.setattr(c["storage"], "cancel_plan_in_session", cancelled)
    with pytest.raises(RuntimeError) as caught:
        await cancel(c)
    assert caught.value is failure and confirmation_tests.security(c) == before


@pytest.mark.parametrize("operation", ["get", "cancel"])
@pytest.mark.parametrize("change", ["run", "handover"])
async def test_public_wrong_run_or_handover_is_not_found(
    context: dict[str, Any], operation: str, change: str
) -> None:
    c = context
    receipt = c["receipt"]
    run = uuid4() if change == "run" else receipt.run_id
    handover = "gh-" + "f" * 64 if change == "handover" else receipt.handover_id
    before = confirmation_tests.security(c)
    with pytest.raises(NotFound):
        if operation == "get":
            await c["public"].get_receipt(c["principal"], run, handover)
        else:
            await c["public"].cancel_plan(c["principal"], run, handover, 1, receipt.plan_hash)
    assert confirmation_tests.security(c) == before
