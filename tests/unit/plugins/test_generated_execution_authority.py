"""Real signed source/review proofs with recorded SQL, not PostgreSQL race evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import TextClause

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
    PersistentAdminResourceService,
    _plugin_response_from_payload,
)
from agent_hub.config.service import ConfigService
from agent_hub.db.models import RunRow
from agent_hub.plugins.execution_authority import (
    GeneratedExecutionAuthority,
    PackageExecutionMember,
)
from agent_hub.plugins.handover_storage import Conflict, StorageUnavailable
from agent_hub.security.secrets import SecretService
from tests.unit.plugins import test_handover_confirmation as confirmation_tests
from tests.unit.plugins import test_handover_reviews as reviews

source_context = reviews.source_context
plan_context = reviews.plan_context
confirmation_context = reviews.confirmation_context
review_context = reviews.context
forbid_execution = reviews.forbid_execution


@pytest.fixture
async def context(review_context: dict[str, Any]) -> dict[str, Any]:
    c = review_context
    c["report"] = await reviews.scan(c)
    await reviews.approve(c, c["report"])
    return c


async def load(c: dict[str, Any]) -> GeneratedExecutionAuthority:
    async with c["storage"]._transaction() as session:
        return cast(GeneratedExecutionAuthority, await c["module"].load_generated_execution_authority_in_session(
            session, plugin_row=reviews.plugin(c),
        ))


async def test_authority_contains_only_the_verified_installed_members(
    context: dict[str, Any], forbid_execution: None,
) -> None:
    c = context
    report = c["report"]
    expected = tuple(
        PackageExecutionMember(m.path, m.size_bytes, m.sha256)
        for m in report.subject.verified_package.members
        if Path(m.path).name != "plugin.json"
    )
    authority = await load(c)
    assert authority == GeneratedExecutionAuthority(
        tenant_id=c["installer"].tenant_id,
        plugin_id=reviews.plugin(c).resource_id,
        registration_id=reviews.plugin(c).id,
        archive_sha256=c["pending"].archive_sha256,
        report_id=report.report_id,
        report_sha256=report.report_sha256,
        members=expected,
    )
    assert len(expected) == 1 and expected[0].path == "main.py"
    with pytest.raises(FrozenInstanceError):
        cast(Any, authority).members = ()
    with pytest.raises(FrozenInstanceError):
        cast(Any, authority.members[0]).sha256 = "0" * 64
    assert await reviews.validate(c) is True
    async with c["storage"]._transaction() as session:
        assert await c["module"].validate_generated_approval_in_session(
            session, plugin_row=reviews.plugin(c),
        ) is True


async def test_approved_config_cannot_replace_the_report_member_authority(
    review_context: dict[str, Any],
) -> None:
    c = review_context
    config = cast(dict[str, Any], reviews.plugin(c).payload["resource_config"])
    config["execution_authority"] = {
        "members": [{"path": "injected.py", "size": 1, "sha256": "0" * 64}],
    }
    report = await reviews.scan(c)
    await reviews.approve(c, report)
    authority = await load(c)
    assert [m.path for m in authority.members] == ["main.py"]
    assert authority.members[0].sha256 == next(
        m.sha256 for m in report.subject.verified_package.members if m.path == "main.py"
    )


async def test_sql_authority_does_not_rebaseline_from_disk(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    expected = await load(c)
    artifact = reviews.package(c)["artifact"]
    entrypoint = c["package_root"] / artifact["storage_key"] / "main.py"
    entrypoint.write_bytes(b"changed installed bytes")

    def forbidden(*args: object, **kwargs: object) -> Any:
        pytest.fail("SQL authority read filesystem source or installed bytes")

    with monkeypatch.context() as patch:
        patch.setattr(type(c["store"]), "read", forbidden)
        patch.setattr(Path, "read_bytes", forbidden)
        assert await load(c) == expected


@pytest.mark.parametrize("change", [
    "configuration", "registration", "tenant", "proof_missing", "proof_pointer",
    "report_missing", "report_member", "grant", "grant_active", "trust", "key_bytes",
    "key_expired", "association", "archive", "storage_key", "rejected",
])
async def test_authority_and_bool_wrappers_reject_the_same_stale_proof(
    context: dict[str, Any], change: str,
) -> None:
    c = context
    row = reviews.plugin(c)
    if change == "configuration":
        row.payload["timeout_seconds"] = 42
    elif change == "registration":
        row.id = uuid4()
    elif change == "tenant":
        row.tenant_id = uuid4()
    elif change == "proof_missing":
        row.payload.pop("_generated_review_approval")
    elif change == "proof_pointer":
        row.payload["_generated_review_approval"] = "ga-review-" + "0" * 64
    elif change == "report_missing":
        c["db"].rows.remove(reviews.report_rows(c)[0])
    elif change == "report_member":
        reviews.body(c)["subject"]["verified_package"]["members"][0]["sha256"] = "0" * 64
    elif change in {"grant", "grant_active"}:
        grant = next(r for r in confirmation_tests.resources(c) if r.resource_id.startswith("sg-"))
        grant.payload.update(
            {"state": "active", "consumed_at": None, "consumed_by": None, "receipt_id": None}
            if change == "grant_active" else {
                "state": "revoked", "revoked_at": c["clock"][0].isoformat(),
                "revoked_by": str(c["installer"].user_id),
            }
        )
    elif change == "trust":
        c["key"].payload["trusted"] = False
    elif change == "key_bytes":
        c["key"].payload["public_key"] = "A" * 43
    elif change == "key_expired":
        c["key"].payload["not_after"] = c["clock"][0].isoformat()
    elif change == "association":
        next(r for r in c["db"].rows if isinstance(r, RunRow)).routing_decision = {}
    elif change == "archive":
        row.payload["content_sha256"] = "0" * 64
    elif change == "storage_key":
        reviews.package(c)["artifact"]["storage_key"] = "untrusted"
    else:
        reviews.package(c)["approval_state"] = "rejected"
    for validator in (
        c["module"].load_generated_execution_authority_in_session,
        c["module"].validate_generated_approval_in_session,
        c["reviews"].validate_generated_approval_in_session,
    ):
        async with c["storage"]._transaction() as session:
            with pytest.raises((Conflict, StorageUnavailable)):
                await validator(session, plugin_row=reviews.plugin(c))


async def test_legacy_has_exact_false_bool_semantics_and_no_generated_authority(
    context: dict[str, Any],
) -> None:
    c = context
    reviews.plugin(c).payload.pop("_generated_handover_id")
    assert await reviews.validate(c) is False
    async with c["storage"]._transaction() as session:
        assert await c["module"].validate_generated_approval_in_session(
            session, plugin_row=reviews.plugin(c),
        ) is False
        with pytest.raises(Conflict):
            await c["module"].load_generated_execution_authority_in_session(
                session, plugin_row=reviews.plugin(c),
            )


async def test_authority_requires_the_callers_explicit_transaction(context: dict[str, Any]) -> None:
    with pytest.raises(StorageUnavailable):
        await context["module"].load_generated_execution_authority_in_session(
            object(), plugin_row=reviews.plugin(context),
        )


async def test_authority_borrows_proof_locks_and_reads_the_report_once(
    context: dict[str, Any],
) -> None:
    c = context
    c["db"].statements.clear()
    await load(c)
    reports = 0
    shared: set[str] = set()
    for statement in c["db"].statements:
        assert not isinstance(statement, TextClause)
        assert isinstance(statement, Select)
        params = c["db"].params(statement)
        identity = params.get("resource_id_1", "")
        if identity.startswith(("gh-", "gs-", "ga-review-")):
            assert statement._for_update_arg is None
            reports += int(identity.startswith("gs-"))
        elif identity.startswith("sg-") or params.get("kind_1") == "plugin_signing_key":
            assert statement._for_update_arg is not None
            assert statement._for_update_arg.read is True
            shared.add("grant" if identity.startswith("sg-") else "key")
    assert reports == 1 and shared == {"grant", "key"}


def scoped_service(c: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> PersistentAdminResourceService:
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(c["sessions"][-1].bind) as session:
            async def execute(statement: Any, *args: Any) -> Any:
                if isinstance(statement, Select):
                    row = await c["db"].scalar(statement)
                    return SimpleNamespace(scalar_one_or_none=lambda: row)
                return await c["db"].execute(statement, *args)

            monkeypatch.setattr(session, "scalar", AsyncMock(side_effect=c["db"].scalar))
            monkeypatch.setattr(session, "execute", AsyncMock(side_effect=execute))
            c["scope_session"] = session
            yield session

    return PersistentAdminResourceService(
        config_service=cast(ConfigService, object()),
        secret_service=cast(SecretService, object()),
        tenant_id=c["installer"].tenant_id, actor_id=c["installer"].user_id,
        session_factory=cast(async_sessionmaker[AsyncSession], factory),
        generated_plugin_approval_validator=c["reviews"].validate_generated_approval_in_session,
    )


async def test_invocation_scope_yields_verified_authority_without_public_dto_members(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    service = scoped_service(c, monkeypatch)
    row = reviews.plugin(c)
    row.payload.update(enabled=True, status="running", health="healthy")
    cached = _plugin_response_from_payload(row.payload)
    before = cached.model_dump(mode="json")
    async with service.plugin_invocation_scope(cached, tenant_id=c["installer"].tenant_id) as authority:
        assert authority == await load(c)
        assert authority is not None
        assert c["scope_session"].in_transaction()
    assert not c["scope_session"].in_transaction()
    assert cached.model_dump(mode="json") == before
    assert "members" not in before and "execution_authority" not in before
    assert "members" not in before["package_metadata"]


async def test_legacy_scope_yields_none_without_loading_generated_authority(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    service = scoped_service(c, monkeypatch)
    row = reviews.plugin(c)
    row.payload.pop("_generated_handover_id")
    row.payload.pop("_generated_review_approval")
    row.payload.update(enabled=True, status="running", health="healthy")

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Legacy scope requested generated authority")

    monkeypatch.setattr(c["module"], "load_generated_execution_authority_in_session", forbidden)
    cached = _plugin_response_from_payload(row.payload)
    async with service.plugin_invocation_scope(cached, tenant_id=c["installer"].tenant_id) as authority:
        assert authority is None


@pytest.mark.parametrize("change", ["cached_configuration", "disabled", "stopped", "no_validator"])
async def test_valid_authority_cannot_bypass_current_activation_or_snapshot_checks(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    c = context
    service = scoped_service(c, monkeypatch)
    row = reviews.plugin(c)
    row.payload.update(enabled=True, status="running", health="healthy")
    cached = _plugin_response_from_payload(row.payload)
    if change == "cached_configuration":
        cached = cached.model_copy(update={"description": "stale configuration"})
    elif change == "disabled":
        row.payload["enabled"] = False
    elif change == "stopped":
        row.payload["status"] = "stopped"
    else:
        service._generated_plugin_approval_validator = None
    with pytest.raises(PublicAPIError):
        async with service.plugin_invocation_scope(cached, tenant_id=c["installer"].tenant_id):
            pytest.fail("Valid report bypassed the current activation gate")
    assert not c["scope_session"].in_transaction()


async def test_invocation_scope_maps_revoked_proof_to_the_existing_safe_error(
    context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    service = scoped_service(c, monkeypatch)
    row = reviews.plugin(c)
    row.payload.update(enabled=True, status="running", health="healthy")
    cached = _plugin_response_from_payload(row.payload)
    loader = c["module"].load_generated_execution_authority_in_session

    async def revoke_after_initial_check(session: AsyncSession, **kwargs: Any) -> Any:
        c["key"].payload["trusted"] = False
        return await loader(session, **kwargs)

    monkeypatch.setattr(c["module"], "load_generated_execution_authority_in_session", revoke_after_initial_check)
    with pytest.raises(PublicAPIError) as error:
        async with service.plugin_invocation_scope(cached, tenant_id=c["installer"].tenant_id):
            pytest.fail("revoked proof reached the invocation body")
    assert error.value.status_code == 503
