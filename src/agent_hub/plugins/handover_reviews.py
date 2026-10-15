"""Persist static review evidence and generated approvals without executing source.

Scan owns two short transactions around parsing frozen bytes. Approval borrows
the existing handover -> registration -> plugin -> report/grant/authority/key
protocol. Runtime callers already hold registration/plugin locks: their immutable
handover/report/approval reads deliberately never lock those rows in reverse.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied
from agent_hub.db.models import AdminResourceRow
from agent_hub.plugins.execution_authority import (
    GeneratedExecutionAuthority,
    PackageExecutionMember,
)
from agent_hub.plugins.handover_confirmation import GeneratedPluginConfirmationService, _pair
from agent_hub.plugins.handover_grants import (
    DurableSourceGrantService,
    SourceGrant,
    _association,
    _principal,
)
from agent_hub.plugins.handover_keys import load_locked_signing_key
from agent_hub.plugins.handover_snapshots import StoredSourceSnapshot
from agent_hub.plugins.handover_sources import SourceLimits
from agent_hub.plugins.handover_storage import (
    GENERATED_HANDOVER_MARKER,
    HANDOVER_KIND,
    ArtifactSource,
    Conflict,
    HandoverReceipt,
    NotFound,
    PluginHandoverStorage,
    StorageUnavailable,
    _canonical,
    _digest,
    _receipt,
    _require_caller_begin,
    _safe_key,
    _utc,
    acquire_plugin_registration_lock,
)
from agent_hub.plugins.handover_verification import (
    FrozenSigningKey,
    VerifiedMember,
    VerifiedPackageFacts,
    verify_signed_snapshot_package,
)
from agent_hub.plugins.source_scanner import (
    PluginScanSubject,
    PluginSourceFinding,
    PluginSourceScanReport,
    scan_source,
)
from agent_hub.runtime.contracts import _canonical_json_bytes

if TYPE_CHECKING:
    from agent_hub.api.routers.admin import PluginResourceResponse


GENERATED_REVIEW_APPROVAL_MARKER = "_generated_review_approval"
_REPORT_ID = re.compile(r"gs-[a-f0-9]{64}\Z")
_APPROVAL_ID = re.compile(r"ga-review-[a-f0-9]{64}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True, slots=True)
class StoredPluginReviewReport:
    report_id: str
    report_sha256: str
    report: PluginSourceScanReport
    created_at: datetime
    subject: PluginScanSubject


@dataclass(frozen=True, slots=True)
class _ReviewContext:
    plugin: AdminResourceRow
    receipt: HandoverReceipt
    grant: SourceGrant
    snapshot: StoredSourceSnapshot | None
    key: FrozenSigningKey
    public_key_sha256: str
    report_row: AdminResourceRow | None


def _contract_sha256(payload: dict[str, Any]) -> str:
    from agent_hub.api.routers.admin import PluginResourceRequest

    try:
        request = PluginResourceRequest.model_validate(
            {
                key: value
                for key, value in payload.items()
                if key in PluginResourceRequest.model_fields
            }
        )
        return hashlib.sha256(
            _canonical(request.model_dump(mode="json", exclude={"enabled"})).encode()
        ).hexdigest()
    except (ValueError, TypeError, RecursionError):
        raise Conflict("plugin review configuration differs") from None


def _report_id(tenant: UUID, actor: UUID, run: UUID, handover: str, key_sha: str) -> str:
    return "gs-" + _digest(
        "generated-review-key",
        {
            "tenant_id": str(tenant),
            "created_by": str(actor),
            "run_id": str(run),
            "handover_id": handover,
            "idempotency_key_sha256": key_sha,
        },
    )


def _handover_id(payload: dict[str, object]) -> str:
    identity = payload.get(GENERATED_HANDOVER_MARKER)
    if type(identity) is not str or not re.fullmatch(r"gh-[a-f0-9]{64}", identity):
        raise Conflict("generated handover review required")
    return identity


def _subject_identity(subject: PluginScanSubject) -> dict[str, Any]:
    # The scanner owns its canonical subject projection; observations are separate.
    facts = subject.verified_package
    return {
        "tenant_id": str(subject.tenant_id),
        "run_id": str(subject.run_id),
        "registration_id": str(subject.registration_id),
        "handover_id": subject.handover_id,
        "receipt_id": subject.receipt_id,
        "snapshot_id": subject.snapshot_id,
        "source_grant_id": subject.source_grant_id,
        "plan_hash": subject.plan_hash,
        "blob_sha256": subject.blob_sha256,
        "source_grant_binding_sha256": subject.source_grant_binding_sha256,
        "registration_contract_sha256": subject.registration_contract_sha256,
        "plan_version": subject.plan_version,
        "verified_package": {
            "plugin_id": facts.plugin_id,
            "plugin_version": facts.plugin_version,
            "source_manifest_sha256": facts.source_manifest_sha256,
            "frozen_manifest_sha256": facts.frozen_manifest_sha256,
            "archive_sha256": facts.archive_sha256,
            "signature_payload_sha256": facts.signature_payload_sha256,
            "key_id": facts.key_id,
            "public_key_sha256": facts.public_key_sha256,
            "members": [
                {"path": member.path, "size_bytes": member.size_bytes, "sha256": member.sha256}
                for member in sorted(facts.members, key=lambda member: member.path)
            ],
        },
    }


def _subject(context: _ReviewContext, facts: VerifiedPackageFacts) -> PluginScanSubject:
    receipt, grant = context.receipt, context.grant
    if receipt.receipt_id is None:
        raise Conflict("committed review receipt required")
    return PluginScanSubject(
        tenant_id=grant.binding.tenant_id,
        run_id=receipt.run_id,
        registration_id=UUID(str(context.plugin.id)),
        handover_id=receipt.handover_id,
        receipt_id=receipt.receipt_id,
        snapshot_id=grant.binding.snapshot_id,
        source_grant_id=grant.grant_id,
        plan_hash=receipt.plan_hash,
        blob_sha256=grant.binding.blob_sha256,
        source_grant_binding_sha256=grant.binding_sha256,
        registration_contract_sha256=_contract_sha256(context.plugin.payload),
        plan_version=grant.binding.plan_version,
        verified_package=facts,
    )


def _key_live(key: FrozenSigningKey, now: datetime) -> None:
    if (key.not_before is not None and now < key.not_before) or (
        key.not_after is not None and now >= key.not_after
    ):
        raise Conflict("current review signing key unavailable")


class GeneratedPluginReviewService:
    def __init__(
        self,
        *,
        confirmation: GeneratedPluginConfirmationService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._confirmation = confirmation
        self._storage = confirmation._storage
        self._snapshots = confirmation._snapshots
        self._grants = confirmation._grants
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return _utc(self._clock())

    @staticmethod
    async def _row(
        session: AsyncSession,
        tenant: UUID,
        kind: str,
        identity: str,
        *,
        lock: bool = False,
        share: bool = False,
    ) -> AdminResourceRow | None:
        statement = select(AdminResourceRow).where(
            AdminResourceRow.tenant_id == tenant,
            AdminResourceRow.kind == kind,
            AdminResourceRow.resource_id == identity,
        )
        if lock:
            statement = statement.with_for_update(read=share)
        row = await session.scalar(
            statement.execution_options(populate_existing=True, autoflush=False)
        )
        if row is not None and (
            not isinstance(row, AdminResourceRow)
            or not isinstance(row.id, UUID)
            or (row.tenant_id, row.kind, row.resource_id) != (tenant, kind, identity)
        ):
            raise Conflict("plugin review row identity differs")
        return row

    @staticmethod
    def _check_ids(report_id: str, report_sha256: str | None = None) -> None:
        if type(report_id) is not str or _REPORT_ID.fullmatch(report_id) is None:
            raise Conflict("plugin review report required")
        if report_sha256 is not None and (
            type(report_sha256) is not str or _SHA.fullmatch(report_sha256) is None
        ):
            raise Conflict("plugin review report required")

    def _access(self, principal: AuthenticatedPrincipal) -> None:
        _principal(principal)
        self._storage._require(principal, "plugin:approve", "plugin:read", "run:read")

    async def _authority(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        grant: SourceGrant,
    ) -> None:
        b = grant.binding
        permissions = {principal.user_id: {"plugin:approve", "plugin:read", "run:read"}}
        permissions.setdefault(b.creator_id, set()).update(
            {"run:create", "run:read", "plugin:read"}
        )
        for user_id in sorted(permissions, key=lambda value: value.int):
            await self._grants._user(session, b.tenant_id, user_id, *sorted(permissions[user_id]))
        run = await self._grants._run(session, b.tenant_id, b.run_id, b.creator_id)
        if _association(run) != b.association:
            raise PermissionDenied("plugin review source association changed")
        if (
            isinstance(b.source, ArtifactSource)
            and await self._grants._artifact(session, b.tenant_id, b.run_id, b.source) != b.artifact
        ):
            raise PermissionDenied("plugin review source artifact changed")

    @staticmethod
    def _committed(receipt: HandoverReceipt, grant: SourceGrant, now: datetime) -> None:
        _pair(receipt, grant)
        # A new review purpose: original delivery expiry is historical here.
        # Never accept active/unconsumed grants or manufacture a new authorization.
        if (
            receipt.status != "pending"
            or receipt.committed_at is None
            or receipt.version != grant.binding.plan_version + 1
            or grant.state != "consumed"
            or grant.receipt_id != receipt.receipt_id
            or grant.consumed_by != receipt.installer_id
            or grant.consumed_at is None
            or not grant.created_at <= receipt.committed_at <= grant.consumed_at <= now
        ):
            raise Conflict("committed consumed review receipt required")

    async def _context(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        handover_id: str,
        *,
        run_id: UUID | None = None,
        report_id: str | None = None,
    ) -> _ReviewContext:
        _require_caller_begin(session)
        self._access(principal)
        if type(handover_id) is not str or not re.fullmatch(r"gh-[a-f0-9]{64}", handover_id):
            raise Conflict("generated handover review required")
        plan = await self._row(session, principal.tenant_id, HANDOVER_KIND, handover_id, lock=True)
        if plan is None:
            raise NotFound("plugin review handover was not found")
        record = self._storage._record(plan, principal.tenant_id, run_id)
        receipt = _receipt(record)
        if receipt.status != "pending" or receipt.source_grant_id is None or record.key_id is None:
            raise Conflict("committed generated review required")
        await acquire_plugin_registration_lock(session, principal.tenant_id, receipt.plugin_id)
        await self._confirmation._plugin(session, principal.tenant_id, receipt)
        plugin = await self._row(
            session, principal.tenant_id, "plugin", receipt.plugin_id, lock=True
        )
        if plugin is None:
            raise Conflict("plugin review registration absent")
        report = (
            None
            if report_id is None
            else await self._row(session, principal.tenant_id, HANDOVER_KIND, report_id, lock=True)
        )
        _, grant = await self._grants._load(session, principal.tenant_id, receipt.source_grant_id)
        self._committed(receipt, grant, self._now())
        await self._authority(session, principal, grant)
        key = await load_locked_signing_key(
            session, tenant_id=principal.tenant_id, key_id=record.key_id
        )
        # Recheck authority after the last key wait; all locks stay in this BEGIN.
        await self._authority(session, principal, grant)
        key = await load_locked_signing_key(
            session, tenant_id=principal.tenant_id, key_id=record.key_id
        )
        _key_live(key, self._now())
        if hashlib.sha256(key.public_key).hexdigest() != record.verified_public_key_sha256:
            raise Conflict("plugin review signing key differs")
        return _ReviewContext(
            plugin,
            receipt,
            grant,
            self._confirmation._snapshot(principal.tenant_id, grant.binding),
            key,
            record.verified_public_key_sha256,
            report,
        )

    @staticmethod
    def _bound_identity(current: _ReviewContext, subject: PluginScanSubject) -> None:
        facts = subject.verified_package
        receipt = current.receipt
        if _subject_identity(_subject(current, facts)) != _subject_identity(subject) or (
            facts.plugin_id,
            facts.plugin_version,
            facts.source_manifest_sha256,
            facts.archive_sha256,
            facts.signature_payload_sha256,
            facts.key_id,
            facts.public_key_sha256,
        ) != (
            receipt.plugin_id,
            receipt.plugin_version,
            receipt.source_manifest_sha256,
            receipt.archive_sha256,
            receipt.signature_payload_sha256,
            receipt.key_id,
            current.public_key_sha256,
        ):
            raise Conflict("plugin review subject differs from current registration")

    def _bound(self, current: _ReviewContext, subject: PluginScanSubject) -> None:
        self._bound_identity(current, subject)
        if current.snapshot is None:
            raise Conflict("plugin review snapshot unavailable")
        facts = subject.verified_package
        frozen = current.snapshot.snapshot.files
        expected = {f.path: f for f in frozen}
        if len(facts.members) != len(expected) or {m.path for m in facts.members} != set(expected):
            raise Conflict("plugin review snapshot differs")
        for member in facts.members:
            original = expected[member.path]
            if member.path == "plugin.json":
                if original.sha256 != facts.frozen_manifest_sha256:
                    raise Conflict("plugin review manifest differs")
            elif (member.sha256, member.size_bytes) != (original.sha256, original.size_bytes):
                raise Conflict("plugin review member differs")

    @staticmethod
    def _decode(row: AdminResourceRow | None, tenant: UUID) -> StoredPluginReviewReport:
        if row is None:
            raise Conflict("plugin review report was not found")
        try:
            raw = cast(dict[str, Any], row.payload)
            if (
                set(raw)
                != {
                    "schema_version",
                    "record_type",
                    "tenant_id",
                    "report_id",
                    "report_sha256",
                    "created_at",
                    "created_by",
                    "idempotency_key_sha256",
                    "body",
                    "verified_at",
                    "key_not_after",
                }
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
            ):
                raise ValueError
            if (raw["record_type"], raw["tenant_id"], raw["report_id"], row.kind) != (
                "scan_report",
                str(tenant),
                row.resource_id,
                HANDOVER_KIND,
            ):
                raise ValueError
            body = raw["body"]
            data = dict(body["subject"])
            package = dict(data.pop("verified_package"))
            package["members"] = tuple(VerifiedMember(**member) for member in package["members"])
            package["verified_at"] = _utc(datetime.fromisoformat(raw["verified_at"]))
            package["key_not_after"] = (
                None
                if raw["key_not_after"] is None
                else _utc(datetime.fromisoformat(raw["key_not_after"]))
            )
            for field in ("tenant_id", "run_id", "registration_id"):
                data[field] = UUID(data[field])
            subject = PluginScanSubject(**data, verified_package=VerifiedPackageFacts(**package))
            GeneratedPluginReviewService._check_ids(row.resource_id, raw["report_sha256"])
            if (
                subject.tenant_id != tenant
                or _subject_identity(subject) != body["subject"]
                or type(raw["idempotency_key_sha256"]) is not str
                or _SHA.fullmatch(raw["idempotency_key_sha256"]) is None
                or row.resource_id
                != _report_id(
                    tenant,
                    UUID(raw["created_by"]),
                    subject.run_id,
                    subject.handover_id,
                    raw["idempotency_key_sha256"],
                )
                or hashlib.sha256(_canonical_json_bytes(body)).hexdigest() != raw["report_sha256"]
                or body["coverage"] not in {"complete", "incomplete"}
                or (body["coverage"] == "complete" and body["incomplete_reasons"])
            ):
                raise ValueError
            findings = tuple(
                PluginSourceFinding(
                    **{key: value for key, value in finding.items() if key != "explanation"}
                )
                for finding in body["findings"]
            )
            report = PluginSourceScanReport(body, raw["report_sha256"], body["coverage"], findings)
            return StoredPluginReviewReport(
                row.resource_id,
                report.report_sha256,
                report,
                _utc(datetime.fromisoformat(raw["created_at"])),
                subject,
            )
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise StorageUnavailable("invalid stored plugin review report") from None

    async def scan_pending_package(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        *,
        archive_bytes: bytes,
        key_id: str,
        idempotency_key: str,
    ) -> StoredPluginReviewReport:
        self._access(principal)
        if type(run_id) is not UUID:
            raise PermissionDenied("plugin review access denied")
        key_sha = hashlib.sha256(_safe_key(idempotency_key).encode()).hexdigest()
        if (
            type(archive_bytes) is not bytes
            or not archive_bytes
            or len(archive_bytes) > SourceLimits().max_archive_bytes
        ):
            raise ValueError("bounded review archive required")
        identity = _report_id(principal.tenant_id, principal.user_id, run_id, handover_id, key_sha)
        async with self._storage._transaction() as session:
            initial = await self._context(session, principal, handover_id, run_id=run_id)
            if key_id != initial.receipt.key_id:
                raise Conflict("plugin review signing key differs")
            # Copy the contract before releasing the row lock.
            contract_sha = _contract_sha256(initial.plugin.payload)
            registration_id = initial.plugin.id
            initial = replace(
                initial,
                plugin=AdminResourceRow(
                    id=initial.plugin.id,
                    tenant_id=initial.plugin.tenant_id,
                    kind=initial.plugin.kind,
                    resource_id=initial.plugin.resource_id,
                    payload=json.loads(_canonical(initial.plugin.payload)),
                ),
            )
        if initial.snapshot is None:
            raise Conflict("plugin review snapshot unavailable")
        facts = verify_signed_snapshot_package(
            snapshot=initial.snapshot.snapshot,
            archive_bytes=archive_bytes,
            key=initial.key,
            now=self._now(),
        )
        subject = _subject(initial, facts)
        if (subject.registration_id, subject.registration_contract_sha256) != (
            registration_id,
            contract_sha,
        ):
            raise Conflict("plugin review registration changed")
        self._bound(initial, subject)
        try:
            report = scan_source(initial.snapshot.snapshot.files, subject)
        except (ValueError, TypeError, RuntimeError, MemoryError):
            raise Conflict("plugin source scan failed") from None
        if report.report_sha256 != hashlib.sha256(
            _canonical_json_bytes(report.body)
        ).hexdigest() or json.loads(_canonical_json_bytes(report.body))[
            "subject"
        ] != _subject_identity(subject):
            raise Conflict("plugin source scan binding differs")
        async with self._storage._transaction() as session:
            current = await self._context(
                session, principal, handover_id, run_id=run_id, report_id=identity
            )
            self._bound(current, subject)
            if current.report_row is not None:
                existing = self._decode(current.report_row, principal.tenant_id)
                if existing.report_sha256 != report.report_sha256 or _subject_identity(
                    existing.subject
                ) != _subject_identity(subject):
                    raise Conflict("plugin review idempotency key already used")
                return existing
            now = self._now()
            payload = {
                "schema_version": 1,
                "record_type": "scan_report",
                "tenant_id": str(principal.tenant_id),
                "report_id": identity,
                "report_sha256": report.report_sha256,
                "created_at": now.isoformat(),
                "created_by": str(principal.user_id),
                "idempotency_key_sha256": key_sha,
                "body": json.loads(_canonical_json_bytes(report.body)),
                "verified_at": facts.verified_at.isoformat(),
                "key_not_after": None
                if facts.key_not_after is None
                else facts.key_not_after.isoformat(),
            }
            await session.execute(
                insert(AdminResourceRow)
                .values(
                    id=uuid4(),
                    tenant_id=principal.tenant_id,
                    kind=HANDOVER_KIND,
                    resource_id=identity,
                    payload=payload,
                )
                .execution_options(autoflush=False)
            )
            await session.flush()
            _key_live(current.key, self._now())
            return self._decode(
                AdminResourceRow(
                    id=uuid4(),
                    tenant_id=principal.tenant_id,
                    kind=HANDOVER_KIND,
                    resource_id=identity,
                    payload=payload,
                ),
                principal.tenant_id,
            )

    async def get_report(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        report_id: str,
    ) -> StoredPluginReviewReport:
        self._access(principal)
        if type(run_id) is not UUID:
            raise PermissionDenied("plugin review access denied")
        self._check_ids(report_id)
        async with self._storage._transaction() as session:
            current = await self._context(
                session, principal, handover_id, run_id=run_id, report_id=report_id
            )
            report = self._decode(current.report_row, principal.tenant_id)
            self._bound(current, report.subject)
            return report

    async def require_matching_report_in_session(
        self,
        session: AsyncSession,
        *,
        principal: AuthenticatedPrincipal,
        plugin_row: AdminResourceRow,
        report_id: str,
        report_sha256: str,
    ) -> StoredPluginReviewReport:
        _require_caller_begin(session)
        self._access(principal)
        self._check_ids(report_id, report_sha256)
        identity, contract = plugin_row.id, _contract_sha256(plugin_row.payload)
        if plugin_row.tenant_id != principal.tenant_id or plugin_row.kind != "plugin":
            raise PermissionDenied("plugin review access denied")
        current = await self._context(
            session,
            principal,
            _handover_id(plugin_row.payload),
            report_id=report_id,
        )
        report = self._decode(current.report_row, principal.tenant_id)
        self._bound(current, report.subject)
        if (
            current.plugin.id != identity
            or _contract_sha256(current.plugin.payload) != contract
            or report.report_sha256 != report_sha256
            or report.report.coverage != "complete"
        ):
            raise Conflict("complete matching plugin review required")
        return report

    async def approve_generated(
        self,
        principal: AuthenticatedPrincipal,
        plugin_id: str,
        *,
        report_id: str | None,
        report_sha256: str | None,
        reason: str,
    ) -> PluginResourceResponse | None:
        from agent_hub.api.routers.admin import (
            PluginPackageApprovalRequest,
            _plugin_response_from_payload,
            _plugin_with_package_approval,
        )

        self._access(principal)
        if type(plugin_id) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", plugin_id):
            raise Conflict("plugin review registration required")
        if type(reason) is not str or len(reason) > 1000:
            raise ValueError("invalid plugin review reason")
        async with self._storage._transaction() as session:
            # Discovery is intentionally unlocked. Recheck under handover-first locks.
            discovered = await self._row(session, principal.tenant_id, "plugin", plugin_id)
            if discovered is None or GENERATED_HANDOVER_MARKER not in discovered.payload:
                return None
            if report_id is None or report_sha256 is None:
                raise Conflict("complete matching plugin review required")
            self._check_ids(report_id, report_sha256)
            registration_id = discovered.id
            handover_id = _handover_id(discovered.payload)
            current = await self._context(session, principal, handover_id, report_id=report_id)
            if current.plugin.id != registration_id or current.plugin.resource_id != plugin_id:
                raise Conflict("plugin review registration changed")
            report = self._decode(current.report_row, principal.tenant_id)
            self._bound(current, report.subject)
            if report.report_sha256 != report_sha256 or report.report.coverage != "complete":
                raise Conflict("complete matching plugin review required")
            row = current.plugin
            package = cast(dict[str, Any], row.payload.get("package_metadata", {}))
            if (
                GENERATED_REVIEW_APPROVAL_MARKER in row.payload
                and package.get("approval_state") == "approved"
                and package.get("approved_by") == str(principal.user_id)
                and package.get("approval_reason")
                == (reason.strip() or "adapter package approved for activation")
            ):
                proof = await self._row(
                    session,
                    principal.tenant_id,
                    HANDOVER_KIND,
                    str(row.payload[GENERATED_REVIEW_APPROVAL_MARKER]),
                )
                if (
                    proof is not None
                    and proof.payload.get("report_id") == report_id
                    and proof.payload.get("report_sha256") == report_sha256
                ):
                    await self.validate_generated_approval_in_session(session, plugin_row=row)
                    return _plugin_response_from_payload(row.payload)
            previous = json.loads(_canonical(row.payload))
            now = self._now()
            updated = _plugin_with_package_approval(
                _plugin_response_from_payload(previous),
                "approved",
                PluginPackageApprovalRequest(reason=reason),
                actor_id=principal.user_id,
            )
            if updated.package_metadata is None:
                raise Conflict("plugin review package unavailable")
            # The existing helper owns lifecycle/eligibility projection; this service
            # samples and binds its own single decision timestamp.
            approved_package = updated.package_metadata.model_copy(update={"approved_at": now})
            updated = updated.model_copy(update={"package_metadata": approved_package})
            proof_payload = {
                "schema_version": 1,
                "record_type": "review_approval",
                "tenant_id": str(principal.tenant_id),
                "plugin_id": plugin_id,
                "handover_id": handover_id,
                "registration_id": str(row.id),
                "registration_contract_sha256": report.subject.registration_contract_sha256,
                "report_id": report.report_id,
                "report_sha256": report.report_sha256,
                "subject_sha256": _digest(
                    "generated-review-subject", _subject_identity(report.subject)
                ),
                "approved_by": str(principal.user_id),
                "approved_at": now.isoformat(),
                "reason_sha256": hashlib.sha256(
                    approved_package.approval_reason.encode()
                ).hexdigest(),
            }
            proof_id = "ga-review-" + _digest("generated-review-approval", proof_payload)
            proof_payload["approval_id"] = proof_id
            audit_id = "ga-review-audit-" + _digest("generated-review-audit", proof_id)
            payload = {
                **updated.model_dump(mode="json"),
                GENERATED_HANDOVER_MARKER: handover_id,
                GENERATED_REVIEW_APPROVAL_MARKER: proof_id,
            }
            await session.execute(
                insert(AdminResourceRow)
                .values(
                    id=uuid4(),
                    tenant_id=principal.tenant_id,
                    kind=HANDOVER_KIND,
                    resource_id=proof_id,
                    payload=proof_payload,
                )
                .execution_options(autoflush=False)
            )
            saved = await session.scalar(
                update(AdminResourceRow)
                .where(
                    AdminResourceRow.id == row.id,
                    AdminResourceRow.tenant_id == principal.tenant_id,
                    AdminResourceRow.kind == "plugin",
                    AdminResourceRow.resource_id == plugin_id,
                    AdminResourceRow.payload == previous,
                )
                .values(payload=payload)
                .returning(AdminResourceRow.id)
                .execution_options(autoflush=False, synchronize_session=False)
            )
            if saved != row.id:
                raise Conflict("plugin review approval registration changed")
            set_committed_value(row, "payload", payload)
            await session.execute(
                insert(AdminResourceRow)
                .values(
                    id=uuid4(),
                    tenant_id=principal.tenant_id,
                    kind="audit",
                    resource_id=audit_id,
                    payload={
                        "id": audit_id,
                        "actor": str(principal.user_id),
                        "action": "plugin.package.approved",
                        "resource": "plugin:" + plugin_id,
                        "created_at": now.isoformat(),
                        "details": {
                            "approval_id": proof_id,
                            "report_id": report.report_id,
                            "report_sha256": report.report_sha256,
                            "registration_id": str(row.id),
                            "handover_id": handover_id,
                        },
                    },
                )
                .execution_options(autoflush=False)
            )
            await session.flush()
            _key_live(current.key, self._now())
            return updated

    async def validate_generated_approval_in_session(
        self,
        session: AsyncSession,
        *,
        plugin_row: AdminResourceRow,
    ) -> bool:
        return await _validate_generated_approval_in_session(
            session,
            plugin_row=plugin_row,
            clock=self._now,
        )


async def validate_generated_approval_in_session(
    session: AsyncSession,
    *,
    plugin_row: AdminResourceRow,
) -> bool:
    """SQL-only proof check for existing registration/plugin lock holders."""
    return await _validate_generated_approval_in_session(
        session,
        plugin_row=plugin_row,
        clock=lambda: datetime.now(UTC),
    )


async def _validate_generated_approval_in_session(
    session: AsyncSession,
    *,
    plugin_row: AdminResourceRow,
    clock: Callable[[], datetime],
) -> bool:
    return await _load_generated_execution_authority_in_session(
        session, plugin_row=plugin_row, clock=clock,
    ) is not None


async def load_generated_execution_authority_in_session(
    session: AsyncSession,
    *,
    plugin_row: AdminResourceRow,
) -> GeneratedExecutionAuthority:
    """Borrow the caller's locks and return only fully validated report authority."""
    authority = await _load_generated_execution_authority_in_session(
        session, plugin_row=plugin_row, clock=lambda: datetime.now(UTC),
    )
    if authority is None:
        raise Conflict("generated plugin execution authority required")
    return authority


async def _load_generated_execution_authority_in_session(
    session: AsyncSession,
    *,
    plugin_row: AdminResourceRow,
    clock: Callable[[], datetime],
) -> GeneratedExecutionAuthority | None:
    """Caller holds registration/plugin locks; never acquire handover FOR UPDATE.

    A committed approver decision survives account departure. Current grant,
    source association/artifact and tenant signing trust still gate use.
    Pending/rejected generated rows raise Conflict; ordinary plugins return None.
    """
    _require_caller_begin(session)
    service = GeneratedPluginReviewService
    grants = DurableSourceGrantService(clock=clock)
    row = plugin_row
    if (
        not isinstance(row, AdminResourceRow)
        or row.kind != "plugin"
        or not isinstance(row.id, UUID)
    ):
        raise Conflict("plugin review registration required")
    if GENERATED_HANDOVER_MARKER not in row.payload:
        return None
    try:
        tenant = row.tenant_id
        if not isinstance(tenant, UUID):
            raise TypeError
        tenant = UUID(str(tenant))
        handover_id = _handover_id(row.payload)
        proof_id = row.payload.get(GENERATED_REVIEW_APPROVAL_MARKER)
        if type(proof_id) is not str or _APPROVAL_ID.fullmatch(proof_id) is None:
            raise ValueError
        plan = await service._row(session, tenant, HANDOVER_KIND, handover_id)
        proof_row = await service._row(session, tenant, HANDOVER_KIND, proof_id)
        if plan is None or proof_row is None:
            raise ValueError
        record = PluginHandoverStorage._record(plan, tenant, None)
        receipt = _receipt(record)
        proof = cast(dict[str, Any], proof_row.payload)
        if (
            set(proof)
            != {
                "schema_version",
                "record_type",
                "tenant_id",
                "plugin_id",
                "handover_id",
                "registration_id",
                "registration_contract_sha256",
                "report_id",
                "report_sha256",
                "subject_sha256",
                "approved_by",
                "approved_at",
                "reason_sha256",
                "approval_id",
            }
            or type(proof["schema_version"]) is not int
            or proof["schema_version"] != 1
            or proof_id
            != "ga-review-"
            + _digest(
                "generated-review-approval",
                {key: value for key, value in proof.items() if key != "approval_id"},
            )
            or (
                proof["record_type"],
                proof["approval_id"],
                proof["tenant_id"],
                proof["registration_id"],
                proof["plugin_id"],
                proof["handover_id"],
            )
            != (
                "review_approval",
                proof_id,
                str(tenant),
                str(row.id),
                row.resource_id,
                handover_id,
            )
        ):
            raise ValueError
        service._check_ids(proof["report_id"], proof["report_sha256"])
        report_row = await service._row(session, tenant, HANDOVER_KIND, proof["report_id"])
        report = service._decode(report_row, tenant)
        if receipt.source_grant_id is None:
            raise ValueError
        # Grant revocation never takes registration/handover locks, so SHARE
        # here cannot create the reverse handover/registration lock cycle.
        grant_row = await service._row(
            session, tenant, HANDOVER_KIND, receipt.source_grant_id, lock=True, share=True
        )
        if grant_row is None:
            raise ValueError
        grant = SourceGrant.model_validate_json(_canonical(grant_row.payload))
        if (
            _canonical(grant.model_dump(mode="json")) != _canonical(grant_row.payload)
            or grant.grant_id != grant_row.resource_id
            or grant.binding.tenant_id != tenant
        ):
            raise ValueError
        service._committed(receipt, grant, _utc(clock()))
        run = await grants._run(session, tenant, grant.binding.run_id, grant.binding.creator_id)
        if _association(run) != grant.binding.association:
            raise ValueError
        if (
            isinstance(grant.binding.source, ArtifactSource)
            and await grants._artifact(session, tenant, grant.binding.run_id, grant.binding.source)
            != grant.binding.artifact
        ):
            raise ValueError
        if record.key_id is None:
            raise ValueError
        key = await load_locked_signing_key(session, tenant_id=tenant, key_id=record.key_id)
        _key_live(key, _utc(clock()))
        key_sha = hashlib.sha256(key.public_key).hexdigest()
        if key_sha != record.verified_public_key_sha256:
            raise ValueError
        current = _ReviewContext(
            row,
            receipt,
            grant,
            None,
            key,
            key_sha,
            report_row,
        )
        service._bound_identity(current, report.subject)
        package = cast(dict[str, Any], row.payload.get("package_metadata", {}))
        artifact = package.get("artifact", {})
        if (
            report.report.coverage != "complete"
            or report.report_sha256 != proof["report_sha256"]
            or proof["subject_sha256"]
            != _digest("generated-review-subject", _subject_identity(report.subject))
            or proof["registration_contract_sha256"] != report.subject.registration_contract_sha256
            or (
                row.payload.get("id"),
                row.payload.get("version"),
                row.payload.get("content_sha256"),
            )
            != (receipt.plugin_id, receipt.plugin_version, receipt.archive_sha256)
            or artifact.get("content_sha256") != receipt.archive_sha256
            or artifact.get("storage_key")
            != f"{tenant}/{receipt.plugin_id}/{receipt.archive_sha256}"
            or package.get("approval_state") != "approved"
            or package.get("approved_by") != proof["approved_by"]
            or _utc(datetime.fromisoformat(package["approved_at"]))
            != _utc(datetime.fromisoformat(proof["approved_at"]))
            or hashlib.sha256(package["approval_reason"].encode()).hexdigest()
            != proof["reason_sha256"]
        ):
            raise ValueError
        return GeneratedExecutionAuthority(
            tenant_id=tenant,
            plugin_id=receipt.plugin_id,
            registration_id=UUID(str(row.id)),
            archive_sha256=report.subject.verified_package.archive_sha256,
            report_id=report.report_id,
            report_sha256=report.report_sha256,
            members=tuple(
                PackageExecutionMember(member.path, member.size_bytes, member.sha256)
                for member in sorted(
                    report.subject.verified_package.members, key=lambda member: member.path,
                )
                if PurePosixPath(member.path).name != "plugin.json"
            ),
        )
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise Conflict("valid generated plugin review approval required") from None
