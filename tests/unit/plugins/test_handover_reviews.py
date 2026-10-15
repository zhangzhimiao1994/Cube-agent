"""Real frozen bytes/signatures; mocked SQL boundary, not PostgreSQL race proof."""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import zipfile
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.elements import TextClause

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunRow
from agent_hub.plugins.handover_storage import Conflict, NotFound, StorageUnavailable
from agent_hub.plugins.handover_verification import PackageVerificationError
from tests.unit.plugins import test_handover_confirmation as confirmation_tests

source_context = confirmation_tests.source_context
plan_context = confirmation_tests.plan_context
confirmation_context = confirmation_tests.context
forbid_execution = confirmation_tests.forbid_execution


def test_review_service_is_available() -> None:
    assert importlib.util.find_spec("agent_hub.plugins.handover_reviews") is not None


@pytest.fixture
async def context(confirmation_context: dict[str, Any]) -> dict[str, Any]:
    c = confirmation_context
    c["pending"] = await confirmation_tests.confirm(c)
    # SQL INSERT defaults are not evaluated by the existing SQL recorder.
    for row in confirmation_tests.resources(c):
        if row.id is None:
            row.id = uuid4()
    c["module"] = importlib.import_module("agent_hub.plugins.handover_reviews")
    c["reviews"] = c["module"].GeneratedPluginReviewService(
        confirmation=c["confirmation"], clock=lambda: c["clock"][0]
    )
    return c


async def scan(c: dict[str, Any], **changes: Any) -> Any:
    args = {
        "principal": c["installer"],
        "run_id": c["pending"].run_id,
        "handover_id": c["pending"].handover_id,
        "archive_bytes": c["archive"],
        "key_id": "tenant-key",
        "idempotency_key": "review-source",
    }
    args.update(changes)
    return await c["reviews"].scan_pending_package(**args)


def plugin(c: dict[str, Any]) -> AdminResourceRow:
    return next(r for r in confirmation_tests.resources(c) if r.kind == "plugin")


def package(c: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], plugin(c).payload["package_metadata"])


def body(c: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], report_rows(c)[0].payload["body"])


def report_rows(c: dict[str, Any]) -> list[AdminResourceRow]:
    return [r for r in confirmation_tests.resources(c) if r.resource_id.startswith("gs-")]


async def get(c: dict[str, Any], report: Any, **changes: Any) -> Any:
    args = {
        "principal": c["installer"],
        "run_id": c["pending"].run_id,
        "handover_id": c["pending"].handover_id,
        "report_id": report.report_id,
    }
    args.update(changes)
    return await c["reviews"].get_report(**args)


async def matching(c: dict[str, Any], report: Any, **changes: Any) -> Any:
    args = {
        "principal": c["installer"],
        "plugin_row": plugin(c),
        "report_id": report.report_id,
        "report_sha256": report.report_sha256,
    }
    args.update(changes)
    async with c["storage"]._transaction() as session:
        return await c["reviews"].require_matching_report_in_session(session, **args)


async def test_persist_reload_immutable_idempotent_without_changing_receipt_or_grant(
    context: dict[str, Any],
    forbid_execution: None,
) -> None:
    c = context
    before = confirmation_tests.security(c)
    report = await scan(c)
    assert report.report_id.startswith("gs-") and report.report.coverage == "complete"
    assert report.subject.registration_id == plugin(c).id
    assert report.subject.verified_package.archive_sha256 == c["pending"].archive_sha256
    assert report.created_at == c["clock"][0]
    assert len(report_rows(c)) == 1
    assert report_rows(c)[0].payload["record_type"] == "scan_report"
    assert [
        (r.kind, r.resource_id, r.payload)
        for r in confirmation_tests.resources(c)
        if not r.resource_id.startswith("gs-")
    ] == before
    with pytest.raises(TypeError):
        report.report.body["coverage"] = "incomplete"
    c["reviews"] = c["module"].GeneratedPluginReviewService(
        confirmation=c["confirmation"], clock=lambda: c["clock"][0]
    )
    c["clock"][0] += timedelta(minutes=1)
    assert await get(c, report) == report
    assert await scan(c) == report
    assert await matching(c, report) == report


async def test_delivery_expiry_does_not_expire_committed_review(context: dict[str, Any]) -> None:
    c = context
    c["clock"][0] = c["pending"].expires_at + timedelta(days=1)
    report = await scan(c)
    assert await get(c, report) == report
    assert await matching(c, report) == report


@pytest.mark.parametrize(
    "change",
    [
        "uuid",
        "capability",
        "credential",
        "endpoint",
        "source",
        "grant",
        "archive",
        "payload",
        "key",
        "receipt",
    ],
)
async def test_changed_binding_invalidates_report(context: dict[str, Any], change: str) -> None:
    c = context
    report = await scan(c)
    row = plugin(c)
    if change == "uuid":
        row.id = uuid4()
    elif change == "capability":
        row.payload["capabilities"] = [{"id": "changed.alias", "permission_class": "file:read"}]
    elif change == "credential":
        row.payload["credential_ref"] = "secret-reference-do-not-export"
    elif change == "endpoint":
        row.payload["endpoint_url"] = "https://example.invalid/changed"
    else:
        plan = next(r for r in confirmation_tests.resources(c) if r.resource_id.startswith("gh-"))
        field = {
            "source": "source_manifest_sha256",
            "grant": "source_grant_binding_sha256",
            "archive": "archive_sha256",
            "payload": "signature_payload_sha256",
            "key": "verified_public_key_sha256",
            "receipt": "receipt_id",
        }[change]
        plan.payload[field] = "gr-" + "0" * 64 if change == "receipt" else "0" * 64
    with pytest.raises((Conflict, StorageUnavailable, PermissionDenied)):
        await matching(c, report)
    with pytest.raises((Conflict, StorageUnavailable, PermissionDenied)):
        await get(c, report)


async def test_lifecycle_metadata_excluded_credentials_only_hashed(context: dict[str, Any]) -> None:
    c = context
    plugin(c).payload["credential_ref"] = "secret-reference-do-not-export"
    report = await scan(c)
    assert "secret-reference-do-not-export" not in json.dumps(report_rows(c)[0].payload)
    row = plugin(c)
    row.payload.update(enabled=True, status="running", health="healthy")
    package(c).update(approval_state="approved", activation_state="active")
    assert await matching(c, report) == report


@pytest.mark.parametrize(
    "change",
    [
        "grant_active",
        "grant_revoked",
        "creator",
        "approver",
        "association",
        "trust",
        "key_bytes",
        "key_expired",
    ],
)
async def test_current_authority_required_for_scan_and_read(
    context: dict[str, Any], change: str
) -> None:
    c = context
    report = await scan(c)
    if change.startswith("grant_"):
        grant = next(r for r in confirmation_tests.resources(c) if r.resource_id.startswith("sg-"))
        if change == "grant_active":
            grant.payload.update(
                state="active", consumed_at=None, consumed_by=None, receipt_id=None
            )
        else:
            grant.payload.update(
                state="revoked",
                revoked_at=c["clock"][0].isoformat().replace("+00:00", "Z"),
                revoked_by=str(c["installer"].user_id),
            )
    elif change == "creator":
        c["user"].disabled = True
    elif change == "approver":
        c["installer_row"].role = "operator"
    elif change == "association":
        next(r for r in c["db"].rows if isinstance(r, RunRow)).routing_decision = {}
    elif change == "trust":
        c["key"].payload["trusted"] = False
    elif change == "key_bytes":
        c["key"].payload["public_key"] = "A" * 43
    else:
        c["key"].payload["not_after"] = c["clock"][0].isoformat()
    actions: tuple[Callable[[], Awaitable[Any]], ...] = (
        lambda: scan(c),
        lambda: get(c, report),
        lambda: matching(c, report),
    )
    for action in actions:
        with pytest.raises(
            (Conflict, PermissionDenied, StorageUnavailable, PackageVerificationError)
        ):
            await action()
    assert len(report_rows(c)) == 1


async def test_wrong_hash_forgery_cross_tenant_and_no_begin(context: dict[str, Any]) -> None:
    c = context
    report = await scan(c)
    with pytest.raises(Conflict):
        await matching(c, report, report_sha256="0" * 64)
    with pytest.raises((Conflict, NotFound)):
        await matching(c, report, report_id="gs-" + "0" * 64)
    outsider = AuthenticatedPrincipal(uuid4(), uuid4(), Role.ADMIN)
    with pytest.raises((PermissionDenied, NotFound, Conflict)):
        await get(c, report, principal=outsider)
    with pytest.raises(StorageUnavailable):
        await c["reviews"].require_matching_report_in_session(
            object(),
            principal=c["installer"],
            plugin_row=plugin(c),
            report_id=report.report_id,
            report_sha256=report.report_sha256,
        )


async def test_tampered_body_rejected(context: dict[str, Any]) -> None:
    c = context
    report = await scan(c)
    body(c)["coverage"] = "incomplete"
    with pytest.raises((StorageUnavailable, Conflict)):
        await get(c, report)


@pytest.mark.parametrize("change", ["config", "user", "key", "grant"])
async def test_changes_during_unlocked_scan_prevent_publication(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    c = context
    original = c["module"].scan_source

    def delayed(*args: Any, **kwargs: Any) -> Any:
        # First transaction has ended before potentially expensive parsing.
        assert all(not s.in_transaction() for s in c["sessions"])
        result = original(*args, **kwargs)
        if change == "config":
            plugin(c).payload["timeout_seconds"] = 42
        elif change == "user":
            c["user"].disabled = True
        elif change == "key":
            c["key"].payload["trusted"] = False
        else:
            grant = next(
                r for r in confirmation_tests.resources(c) if r.resource_id.startswith("sg-")
            )
            grant.payload.update(
                state="revoked",
                revoked_at=c["clock"][0].isoformat().replace("+00:00", "Z"),
                revoked_by=str(c["installer"].user_id),
            )
        return result

    monkeypatch.setattr(c["module"], "scan_source", delayed)
    with pytest.raises((Conflict, PermissionDenied, StorageUnavailable)):
        await scan(c)
    assert report_rows(c) == []


async def test_incomplete_report_stored_but_never_approval_evidence(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context
    from agent_hub.plugins.source_scanner import PluginScanLimits

    original = c["module"].scan_source
    monkeypatch.setattr(
        c["module"],
        "scan_source",
        lambda files, subject: original(files, subject, limits=PluginScanLimits(max_nodes=1)),
    )
    report = await scan(c)
    assert report.report.coverage == "incomplete"
    assert await get(c, report) == report
    with pytest.raises(Conflict):
        await matching(c, report)


async def test_report_read_lock_order(context: dict[str, Any]) -> None:
    c = context
    report = await scan(c)
    c["db"].statements.clear()
    await matching(c, report)
    ordered = []
    for statement in c["db"].statements:
        if isinstance(statement, TextClause):
            ordered.append("registration")
        elif isinstance(statement, Select):
            params = c["db"].params(statement)
            identity = params.get("resource_id_1", "")
            ordered.append(
                "plan"
                if identity.startswith("gh-")
                else "report"
                if identity.startswith("gs-")
                else "grant"
                if identity.startswith("sg-")
                else params.get("kind_1", "authority")
            )
    assert ordered.index("plan") < ordered.index("registration") < ordered.index("plugin")
    assert ordered.index("plugin") < ordered.index("report") < ordered.index("grant")
    assert ordered.index("grant") < ordered.index("authority") < ordered.index("plugin_signing_key")


async def approve(c: dict[str, Any], report: Any, **changes: Any) -> Any:
    args = {
        "principal": c["installer"],
        "plugin_id": plugin(c).resource_id,
        "report_id": report.report_id,
        "report_sha256": report.report_sha256,
        "reason": "reviewed",
    }
    args.update(changes)
    return await c["reviews"].approve_generated(**args)


async def validate(c: dict[str, Any]) -> bool:
    async with c["storage"]._transaction() as session:
        result = await c["reviews"].validate_generated_approval_in_session(
            session, plugin_row=plugin(c)
        )
        assert type(result) is bool
        return result


async def test_approval_commits_proof_and_audit_without_execution(
    context: dict[str, Any],
    forbid_execution: None,
) -> None:
    c = context
    report = await scan(c)
    before = len(confirmation_tests.resources(c))
    approved = await approve(c, report)
    assert approved.package_metadata.approval_state == "approved"
    assert approved.enabled is False
    proof_id = plugin(c).payload["_generated_review_approval"]
    assert isinstance(proof_id, str)
    proof = next(r for r in confirmation_tests.resources(c) if r.resource_id == proof_id)
    assert proof.kind == "plugin_handover" and proof_id.startswith("ga-review-")
    assert proof.payload["record_type"] == "review_approval"
    assert proof.payload["report_id"] == report.report_id
    assert proof.payload["report_sha256"] == report.report_sha256
    assert proof.payload["registration_id"] == str(plugin(c).id)
    assert proof.payload["approved_by"] == str(c["installer"].user_id)
    assert proof.payload["approved_at"] == c["clock"][0].isoformat()
    assert len(confirmation_tests.resources(c)) == before + 2
    assert await validate(c) is True
    assert await approve(c, report) == approved
    assert len(confirmation_tests.resources(c)) == before + 2


@pytest.mark.parametrize(
    "change", ["config", "uuid", "grant", "key", "report", "proof", "rejected"]
)
async def test_runtime_invalidates_changed_proof(context: dict[str, Any], change: str) -> None:
    c = context
    report = await scan(c)
    await approve(c, report)
    if change == "config":
        plugin(c).payload["capabilities"] = [{"id": "changed", "sandbox_profile": "changed"}]
    elif change == "uuid":
        plugin(c).id = uuid4()
    elif change == "key":
        c["key"].payload["trusted"] = False
    elif change == "report":
        body(c)["coverage"] = "incomplete"
    elif change == "proof":
        plugin(c).payload["_generated_review_approval"] = "ga-review-" + "0" * 64
    elif change == "rejected":
        package(c)["approval_state"] = "rejected"
    else:
        grant = next(r for r in confirmation_tests.resources(c) if r.resource_id.startswith("sg-"))
        grant.payload.update(
            state="revoked",
            revoked_at=c["clock"][0].isoformat().replace("+00:00", "Z"),
            revoked_by=str(c["installer"].user_id),
        )
    with pytest.raises((Conflict, StorageUnavailable, PermissionDenied)):
        await validate(c)


async def test_runtime_does_not_revalidate_historical_approver_or_lock_plan(
    context: dict[str, Any],
) -> None:
    c = context
    report = await scan(c)
    await approve(c, report)
    c["installer_row"].disabled = True
    c["user"].disabled = True
    c["clock"][0] = c["pending"].expires_at + timedelta(days=1)
    c["db"].statements.clear()
    assert await validate(c) is True
    for statement in c["db"].statements:
        if isinstance(statement, Select):
            identity = c["db"].params(statement).get("resource_id_1", "")
            if identity.startswith(("gh-", "gs-", "ga-review-")):
                assert statement._for_update_arg is None


async def test_missing_report_and_historical_flag_cannot_approve(context: dict[str, Any]) -> None:
    c = context
    with pytest.raises(Conflict):
        await c["reviews"].approve_generated(
            c["installer"], plugin(c).resource_id, report_id=None, report_sha256=None, reason=""
        )
    package(c)["approval_state"] = "approved"
    with pytest.raises(Conflict):
        await validate(c)


async def test_non_generated_falls_back_and_has_no_proof_requirement(
    context: dict[str, Any],
) -> None:
    c = context
    del plugin(c).payload["_generated_handover_id"]
    assert (
        await c["reviews"].approve_generated(
            c["installer"], plugin(c).resource_id, report_id=None, report_sha256=None, reason=""
        )
        is None
    )
    assert await validate(c) is False


async def test_real_clock_commit_precedes_consumption(context: dict[str, Any]) -> None:
    c = context
    grant = next(r for r in confirmation_tests.resources(c) if r.resource_id.startswith("sg-"))
    consumed_at = c["pending"].committed_at + timedelta(microseconds=50)
    grant.payload["consumed_at"] = consumed_at.isoformat().replace("+00:00", "Z")
    c["clock"][0] = consumed_at + timedelta(microseconds=50)
    report = await scan(c)
    assert await matching(c, report) == report


@pytest.mark.parametrize("stage", ["cas", "audit", "flush"])
async def test_approval_failure_rolls_back_plugin_proof_and_audit(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    c = context
    report = await scan(c)
    before = deepcopy(confirmation_tests.security(c))
    execute = c["db"].execute
    scalar = c["db"].scalar

    async def fail_execute(statement: Any, *args: Any) -> Any:
        if isinstance(statement, Insert) and c["db"].params(statement)["kind"] == "audit":
            raise RuntimeError("unit audit write failure")
        return await execute(statement, *args)

    async def fail_cas(statement: Any) -> Any:
        if isinstance(statement, Update):
            return None
        return await scalar(statement)

    if stage == "audit":
        monkeypatch.setattr(c["db"], "execute", fail_execute)
    elif stage == "cas":
        monkeypatch.setattr(c["db"], "scalar", fail_cas)
    else:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock

        transaction = c["storage"]._transaction

        @asynccontextmanager
        async def fail_flush() -> Any:
            async with transaction() as session:
                monkeypatch.setattr(
                    session, "flush", AsyncMock(side_effect=RuntimeError("unit flush failure"))
                )
                yield session

        monkeypatch.setattr(c["storage"], "_transaction", fail_flush)
    with pytest.raises((Conflict, RuntimeError)):
        await approve(c, report)
    assert confirmation_tests.security(c) == before


async def test_scanner_exception_is_safe_and_never_persisted(
    context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = context

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secret-source-value")

    monkeypatch.setattr(c["module"], "scan_source", fail)
    with pytest.raises(Conflict) as error:
        await scan(c)
    assert "secret-source-value" not in str(error.value)
    assert report_rows(c) == []


async def test_asyncpg_registration_uuid_supported(context: dict[str, Any]) -> None:
    DriverUUID = importlib.import_module("asyncpg.pgproto.pgproto").UUID

    c = context
    for row in confirmation_tests.resources(c):
        row.id = DriverUUID(str(row.id))
        row.tenant_id = DriverUUID(str(row.tenant_id))
    report = await scan(c)
    assert str(report.subject.registration_id) == str(plugin(c).id)
    await approve(c, report)
    assert await validate(c) is True


async def test_module_level_runtime_validator_requires_no_service(context: dict[str, Any]) -> None:
    c = context
    report = await scan(c)
    await approve(c, report)
    async with c["storage"]._transaction() as session:
        assert (
            await c["module"].validate_generated_approval_in_session(session, plugin_row=plugin(c))
            is True
        )


async def test_same_manifest_and_signature_in_different_zip_cannot_scan(
    context: dict[str, Any],
) -> None:
    c = context
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(c["archive"])) as source,
        zipfile.ZipFile(buffer, "w") as archive,
    ):
        for name in source.namelist():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), source.read(name)
            )
    data = buffer.getvalue()
    assert data != c["archive"]
    with pytest.raises((Conflict, PackageVerificationError)):
        await scan(c, archive_bytes=data)
    assert report_rows(c) == []


async def test_legacy_lookup_cannot_parse_report_as_plan_or_grant(context: dict[str, Any]) -> None:
    c = context
    report = await scan(c)
    with pytest.raises(StorageUnavailable):
        await c["storage"].get_receipt(c["installer"], c["pending"].run_id, report.report_id)
    async with c["storage"]._transaction() as session:
        with pytest.raises(PermissionDenied):
            await c["grants"]._load(session, c["installer"].tenant_id, report.report_id)
