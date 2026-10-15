"""PostgreSQL-only generated handover storage, with no execution side effects.

AuthorizedSource and VerifiedPendingPlugin are internal service outputs, not API
request bodies or proof that a caller-supplied archive was verified. The package
service must authorize/freeze sources and verify signatures before constructing
them. Admin registration writers must acquire the exported lock and preserve the
top-level _generated_handover_id marker during legitimate lifecycle mutations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import SessionTransactionOrigin

from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied
from agent_hub.db.models import AdminResourceRow, RunRow

HANDOVER_KIND = "plugin_handover"
GENERATED_HANDOVER_MARKER = "_generated_handover_id"
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = r"^[a-f0-9]{64}$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
Digest = Annotated[str, Field(pattern=_SHA, min_length=64, max_length=64)]
Identifier = Annotated[str, Field(pattern=_ID, min_length=1, max_length=128)]
HandoverId = Annotated[str, Field(pattern=r"^gh-[a-f0-9]{64}$", min_length=67, max_length=67)]
SourceGrantId = Annotated[str, Field(pattern=r"^sg-[a-f0-9]{64}$", min_length=67, max_length=67)]
RiskCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


class NotFound(RuntimeError):
    pass


class Conflict(RuntimeError):
    pass


class StorageUnavailable(RuntimeError):
    pass


class _InternalDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


class ArtifactSource(_InternalDTO):
    kind: Literal["artifact"] = "artifact"
    artifact_id: Identifier


class WorkspaceSource(_InternalDTO):
    kind: Literal["workspace"] = "workspace"
    project_id: Identifier
    session_id: Identifier
    paths: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = Field(
        min_length=1, max_length=200
    )

    @field_validator("paths")
    @classmethod
    def normalized_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            if (
                path.startswith("/")
                or any(c in path for c in "\\:%\x00")
                or any(ord(c) < 32 for c in path)
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ValueError("source paths must be normalized relative paths")
        if len(set(paths)) != len(paths):
            raise ValueError("source paths must be unique")
        return tuple(sorted(paths))


Source = Annotated[ArtifactSource | WorkspaceSource, Field(discriminator="kind")]


class AuthorizedSource(_InternalDTO):
    """A source service's authorized, immutable snapshot reference."""

    tenant_id: UUID
    run_id: UUID
    source: Source
    source_manifest_sha256: Digest


class PlanData(_InternalDTO):
    plugin_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")]
    plugin_version: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
    ]
    permissions: tuple[RiskCode, ...] = Field(default=(), max_length=256)
    risk_summary: tuple[RiskCode, ...] = Field(default=(), max_length=64)
    signature_payload_sha256: Digest | None = None
    archive_sha256: Digest | None = None

    @field_validator("permissions", "risk_summary")
    @classmethod
    def normalize_summary(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("summary values must be unique")
        return tuple(sorted(values))


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _digest(domain: str, value: object) -> str:
    payload = {"schema_version": 1, "domain": domain, "data": value}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _checked_dto[DTO: _InternalDTO](value: object, cls: type[DTO]) -> DTO:
    if type(value) is not cls:
        raise ValueError("an internal service DTO is required")
    # Revalidate copies and mutable nested payloads rather than trusting model_copy.
    return cls.model_validate(value.model_dump(mode="python"))


class VerifiedPendingPlugin(_InternalDTO):
    """Output of server source/package/signature verification, never a client schema."""

    tenant_id: UUID
    run_id: UUID
    handover_id: HandoverId
    plan_hash: Digest
    source_manifest_sha256: Digest
    signature_payload_sha256: Digest
    archive_sha256: Digest
    key_id: Identifier
    verified_public_key_sha256: Digest
    artifact_ref: Identifier
    plugin_payload: dict[str, object] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_pending_metadata(self) -> VerifiedPendingPlugin:
        _pending_payload(self)
        return self


def _pending_payload(verified: VerifiedPendingPlugin) -> dict[str, object]:
    # Lazy import lets admin import the shared lock without a module import cycle.
    from agent_hub.api.routers.admin import PluginResourceResponse

    raw = verified.plugin_payload
    if len(_canonical(raw).encode("utf-8")) > 65_536:
        raise ValueError("plugin metadata is too large")
    if set(raw) - set(PluginResourceResponse.model_fields):
        raise ValueError("unsupported plugin metadata fields")
    package = raw.get("package_metadata")
    if (
        raw.get("enabled") is not False
        or raw.get("status") != "disabled"
        or raw.get("health") != "disabled"
        or not isinstance(package, dict)
    ):
        raise ValueError("verified plugin must be disabled")
    signature = package.get("signature")
    artifact = package.get("artifact")
    if (
        package.get("kind") != "adapter_package"
        or package.get("signature_verification") != "verified"
        or package.get("approval_state") != "pending"
        or package.get("activation_state") != "blocked_pending_approval"
        or package.get("verified_public_key_sha256") != verified.verified_public_key_sha256
        or not isinstance(signature, dict)
        or signature.get("key_id") != verified.key_id
        or not isinstance(artifact, dict)
        or artifact.get("content_sha256") != verified.archive_sha256
        or raw.get("content_sha256") not in {None, verified.archive_sha256}
        or package.get("approved_by") is not None
        or package.get("approved_at") is not None
    ):
        raise ValueError("package verification facts do not match")
    response = PluginResourceResponse.model_validate(raw)
    if (
        response.package_metadata is None
        or response.package_metadata.package_version != response.version
        or response.package_metadata.activation_state != "blocked_pending_approval"
        or response.package_metadata.artifact is None
        or response.package_metadata.artifact.storage_key
        != f"{verified.tenant_id}/{response.id}/{verified.archive_sha256}"
    ):
        raise ValueError("package identity does not match")
    payload = response.model_dump(mode="json")
    payload["content_sha256"] = verified.archive_sha256
    return payload


class _GrantPair(_InternalDTO):
    source_grant_id: SourceGrantId | None = None
    source_grant_binding_sha256: Digest | None = None

    @model_validator(mode="after")
    def paired_source_grant(self) -> _GrantPair:
        if (self.source_grant_id is None) != (self.source_grant_binding_sha256 is None):
            raise ValueError("source grant identity and binding must be paired")
        return self


class _HandoverRecord(_GrantPair):
    schema_version: Literal[1] = 1
    tenant_id: UUID
    handover_id: HandoverId
    run_id: UUID
    creator_id: UUID
    installer_id: UUID | None = None
    version: Annotated[int, Field(ge=1)] = 1
    status: Literal["planned", "awaiting_signature", "pending", "cancelled", "failed"]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    source: Source
    source_manifest_sha256: Digest
    plugin_id: Identifier
    plugin_version: Annotated[str, Field(min_length=1, max_length=128)]
    plan_data: PlanData
    plan_hash: Digest
    request_fingerprint: Digest
    plan_key_digest: Digest
    signature_payload_sha256: Digest | None = None
    archive_sha256: Digest | None = None
    confirm_key_digest: Digest | None = None
    confirm_request_sha256: Digest | None = None
    cancel_request_sha256: Digest | None = None
    cancelled_by: UUID | None = None
    key_id: Identifier | None = None
    verified_public_key_sha256: Digest | None = None
    artifact_ref: Identifier | None = None
    receipt_id: Identifier | None = None
    audit_id: Identifier | None = None
    committed_at: datetime | None = None
    result: Literal["blocked_pending_approval"] | None = None

    @field_validator("created_at", "updated_at", "expires_at", "committed_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def coherent_history(self) -> _HandoverRecord:
        if (
            self.plugin_id != self.plan_data.plugin_id
            or self.plugin_version != self.plan_data.plugin_version
            or self.expires_at <= self.created_at
            or self.updated_at < self.created_at
        ):
            raise ValueError("invalid handover history")
        commit_fields = (
            self.installer_id,
            self.confirm_key_digest,
            self.confirm_request_sha256,
            self.key_id,
            self.verified_public_key_sha256,
            self.artifact_ref,
            self.receipt_id,
            self.audit_id,
            self.committed_at,
            self.result,
        )
        if self.status == "pending":
            if (
                any(value is None for value in commit_fields)
                or self.version != 2
                or self.archive_sha256 is None
                or self.signature_payload_sha256 is None
                or self.receipt_id != "gr-" + _digest("receipt", self.handover_id)
                or self.audit_id != "ga-" + _digest("audit", self.handover_id)
            ):
                raise ValueError("incomplete pending receipt")
        elif any(value is not None for value in commit_fields):
            raise ValueError("unexpected commit facts")
        if self.status == "cancelled" and (
            self.cancel_request_sha256 is None or self.cancelled_by is None or self.version != 2
        ):
            raise ValueError("incomplete cancellation receipt")
        return self


class HandoverReceipt(_GrantPair):
    """Safe, bounded historical projection, independent of live plugin lifecycle."""

    schema_version: Literal[1] = 1
    handover_id: HandoverId
    run_id: UUID
    creator_id: UUID
    installer_id: UUID | None
    version: int
    status: Literal["planned", "awaiting_signature", "pending", "cancelled", "failed"]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    source: Source
    source_manifest_sha256: Digest
    plan_hash: Digest
    plugin_id: str
    plugin_version: str
    permissions: tuple[str, ...]
    risk_summary: tuple[str, ...]
    signature_payload_sha256: Digest | None
    archive_sha256: Digest | None
    key_id: str | None
    receipt_id: str | None
    audit_id: str | None
    committed_at: datetime | None
    result: Literal["blocked_pending_approval"] | None


def _receipt(record: _HandoverRecord) -> HandoverReceipt:
    data = record.model_dump(mode="python", include=set(HandoverReceipt.model_fields))
    data.update(
        plugin_id=record.plan_data.plugin_id,
        plugin_version=record.plan_data.plugin_version,
        permissions=record.plan_data.permissions,
        risk_summary=record.plan_data.risk_summary,
    )
    return HandoverReceipt.model_validate(data)


_BridgeValidate = Callable[[AsyncSession, HandoverReceipt, VerifiedPendingPlugin], Awaitable[None]]
_BridgeConsume = Callable[[AsyncSession, HandoverReceipt], Awaitable[None]]


def _require_postgres_transaction(session: AsyncSession) -> None:
    if not session.in_transaction() or session.get_bind().dialect.name != "postgresql":
        raise StorageUnavailable("storage unavailable")


def _require_caller_begin(session: AsyncSession) -> None:
    if not isinstance(session, AsyncSession):
        raise StorageUnavailable("caller PostgreSQL BEGIN required")
    transaction = session.get_transaction()
    sync = None if transaction is None else transaction.sync_transaction
    if (
        sync is None
        or sync.origin is not SessionTransactionOrigin.BEGIN
        or not session.is_active
        or session.in_nested_transaction()
        or session.get_bind().dialect.name != "postgresql"
    ):
        raise StorageUnavailable("caller PostgreSQL BEGIN required")


async def acquire_plugin_registration_lock(
    session: AsyncSession, tenant_id: UUID, plugin_id: str
) -> None:
    """Shared registration protocol; caller owns a PostgreSQL session.begin()."""
    _require_postgres_transaction(session)
    if (
        not isinstance(tenant_id, UUID)
        or not isinstance(plugin_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", plugin_id)
    ):
        raise ValueError("invalid plugin registration identity")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"plugin-registration:{tenant_id}:{plugin_id}"},
    )


class PluginHandoverStorage:
    """Legacy wrappers own BEGIN; bridge seams borrow it without managing lifetime."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._authorizer = Authorizer()

    def _require(self, principal: AuthenticatedPrincipal, *permissions: str) -> None:
        for permission in permissions:
            self._authorizer.require(principal, permission)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            raise StorageUnavailable("storage unavailable")
        try:
            async with self._session_factory() as session, session.begin():
                _require_postgres_transaction(session)
                yield session
        except IntegrityError as error:
            original = error.orig
            code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            if code == "23505":
                raise Conflict("resource already exists") from None
            raise StorageUnavailable("storage unavailable") from None
        except (SQLAlchemyError, OSError):
            raise StorageUnavailable("storage unavailable") from None

    async def _require_run(self, session: AsyncSession, tenant_id: UUID, run_id: UUID) -> None:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        row = await session.scalar(
            select(RunRow)
            .where(RunRow.tenant_id == tenant_id, RunRow.id == run_id)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise NotFound("handover was not found")

    async def _load(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        run_id: UUID,
        handover_id: str,
        *,
        lock: bool = False,
    ) -> tuple[AdminResourceRow, _HandoverRecord]:
        statement = select(AdminResourceRow).where(
            AdminResourceRow.tenant_id == tenant_id,
            AdminResourceRow.kind == HANDOVER_KIND,
            AdminResourceRow.resource_id == handover_id,
            AdminResourceRow.payload["run_id"].astext == str(run_id),
        )
        if lock:
            statement = statement.with_for_update().execution_options(
                populate_existing=True, autoflush=False
            )
        row = await session.scalar(statement)
        if row is None:
            raise NotFound("handover was not found")
        return row, self._record(row, tenant_id, run_id)

    @staticmethod
    def _record(row: AdminResourceRow, tenant_id: UUID, run_id: UUID | None) -> _HandoverRecord:
        try:
            record = _HandoverRecord.model_validate_json(_canonical(row.payload))
            fingerprint = _plan_fingerprint(
                record.run_id,
                record.source,
                record.source_manifest_sha256,
                record.plan_data,
                source_grant_id=record.source_grant_id,
                source_grant_binding_sha256=record.source_grant_binding_sha256,
            )
            if (
                record.tenant_id != tenant_id
                or (run_id is not None and record.run_id != run_id)
                or record.handover_id != row.resource_id
                or record.handover_id != "gh-" + record.plan_key_digest
                or fingerprint != record.request_fingerprint
                or record.plan_hash
                != _digest(
                    "plan", {"handover_id": record.handover_id, "request_fingerprint": fingerprint}
                )
            ):
                raise ValueError("invalid stored plan")
            return record
        except (ValueError, TypeError):
            raise StorageUnavailable("storage unavailable") from None

    @asynccontextmanager
    async def _operation(self, session: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        if session is None:
            async with self._transaction() as owned:
                yield owned
        else:
            _require_caller_begin(session)
            yield session

    async def create_plan(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        authorized_source: AuthorizedSource,
        plan_data: PlanData,
        idempotency_key: str,
        expires_at: datetime,
    ) -> HandoverReceipt:
        return await self._create_plan(
            None, principal, run_id, authorized_source, plan_data, idempotency_key, expires_at
        )

    async def create_plan_in_session(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        authorized_source: AuthorizedSource,
        plan_data: PlanData,
        idempotency_key: str,
        expires_at: datetime,
        *,
        source_grant_id: str | None = None,
        source_grant_binding_sha256: str | None = None,
    ) -> HandoverReceipt:
        """Internal plan persistence, not grant/auth proof; caller owns BEGIN and publication.

        Publish/lock the actual grant in the same transaction. Its immutable
        binding uses plan_identity, never this plan's final fingerprint/hash.
        No commit, rollback, error mapping, source cleanup or registration here.
        """
        _require_caller_begin(session)
        return await self._create_plan(
            session,
            principal,
            run_id,
            authorized_source,
            plan_data,
            idempotency_key,
            expires_at,
            source_grant_id=source_grant_id,
            source_grant_binding_sha256=source_grant_binding_sha256,
        )

    async def _create_plan(
        self,
        caller_session: AsyncSession | None,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        authorized_source: AuthorizedSource,
        plan_data: PlanData,
        idempotency_key: str,
        expires_at: datetime,
        *,
        source_grant_id: str | None = None,
        source_grant_binding_sha256: str | None = None,
    ) -> HandoverReceipt:
        self._require(principal, "run:create", "run:read", "plugin:read")
        source = _checked_dto(authorized_source, AuthorizedSource)
        plan = _checked_dto(plan_data, PlanData)
        handover_id = plan_identity(principal, idempotency_key)
        key_digest = handover_id[3:]
        pair = _GrantPair(
            source_grant_id=source_grant_id, source_grant_binding_sha256=source_grant_binding_sha256
        )
        if source.tenant_id != principal.tenant_id or source.run_id != run_id:
            raise NotFound("handover was not found")
        fingerprint = _plan_fingerprint(
            run_id,
            source.source,
            source.source_manifest_sha256,
            plan,
            source_grant_id=pair.source_grant_id,
            source_grant_binding_sha256=pair.source_grant_binding_sha256,
        )
        expiry = _utc(expires_at)
        async with self._operation(caller_session) as session:
            await self._require_run(session, principal.tenant_id, run_id)
            # Look up identity without run filtering so a key reused on another run conflicts.
            identity_select = (
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == principal.tenant_id,
                    AdminResourceRow.kind == HANDOVER_KIND,
                    AdminResourceRow.resource_id == handover_id,
                )
                .execution_options(populate_existing=True, autoflush=False)
            )
            existing = await session.scalar(identity_select)
            if existing is not None:
                stored = self._record(existing, principal.tenant_id, None)
                if stored.request_fingerprint != fingerprint:
                    raise Conflict("plan key was used for a different request")
                return _receipt(stored)
            now = _utc(self._clock())
            if expiry <= now or expiry > now + timedelta(days=7):
                raise ValueError("plan expiry must be within seven days")
            record = _HandoverRecord(
                tenant_id=principal.tenant_id,
                handover_id=handover_id,
                run_id=run_id,
                creator_id=principal.user_id,
                created_at=now,
                updated_at=now,
                expires_at=expiry,
                status="awaiting_signature" if plan.signature_payload_sha256 is None else "planned",
                source=source.source,
                source_manifest_sha256=source.source_manifest_sha256,
                plugin_id=plan.plugin_id,
                plugin_version=plan.plugin_version,
                plan_data=plan,
                request_fingerprint=fingerprint,
                plan_key_digest=key_digest,
                plan_hash=_digest(
                    "plan", {"handover_id": handover_id, "request_fingerprint": fingerprint}
                ),
                signature_payload_sha256=plan.signature_payload_sha256,
                archive_sha256=plan.archive_sha256,
                source_grant_id=pair.source_grant_id,
                source_grant_binding_sha256=pair.source_grant_binding_sha256,
            )
            await session.execute(
                insert(AdminResourceRow)
                .values(
                    tenant_id=principal.tenant_id,
                    kind=HANDOVER_KIND,
                    resource_id=handover_id,
                    payload=record.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        AdminResourceRow.tenant_id,
                        AdminResourceRow.kind,
                        AdminResourceRow.resource_id,
                    ]
                )
                .execution_options(autoflush=False)
            )
            stored_row = await session.scalar(identity_select)
            if stored_row is None:
                raise StorageUnavailable("storage unavailable")
            stored = self._record(stored_row, principal.tenant_id, None)
            if stored.request_fingerprint != fingerprint:
                raise Conflict("plan key was used for a different request")
            return _receipt(stored)

    async def get_receipt(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str
    ) -> HandoverReceipt:
        return await self._get_receipt(None, principal, run_id, handover_id)

    async def get_receipt_in_session(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
    ) -> HandoverReceipt:
        """Read stored history in the caller's BEGIN without managing its lifetime."""
        _require_caller_begin(session)
        return await self._get_receipt(session, principal, run_id, handover_id)

    async def _get_receipt(
        self,
        caller_session: AsyncSession | None,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
    ) -> HandoverReceipt:
        self._require(principal, "run:read", "plugin:read")
        async with self._operation(caller_session) as session:
            await self._require_run(session, principal.tenant_id, run_id)
            _, record = await self._load(session, principal.tenant_id, run_id, handover_id)
            return _receipt(record)

    async def cancel_plan(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
    ) -> HandoverReceipt:
        return await self._cancel_plan(
            None, principal, run_id, handover_id, expected_version, plan_hash
        )

    async def cancel_plan_in_session(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
    ) -> HandoverReceipt:
        """Cancel in the caller's BEGIN; no commit, rollback or error mapping."""
        _require_caller_begin(session)
        return await self._cancel_plan(
            session, principal, run_id, handover_id, expected_version, plan_hash
        )

    async def _cancel_plan(
        self,
        caller_session: AsyncSession | None,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
    ) -> HandoverReceipt:
        self._require(principal, "run:create", "run:read", "plugin:read")
        _check_confirmation_identity(expected_version, plan_hash)
        fingerprint = _digest(
            "cancel", {"expected_version": expected_version, "plan_hash": plan_hash}
        )
        async with self._operation(caller_session) as session:
            await self._require_run(session, principal.tenant_id, run_id)
            row, record = await self._load(
                session, principal.tenant_id, run_id, handover_id, lock=True
            )
            if record.creator_id != principal.user_id and not self._authorizer.is_allowed(
                principal, "plugin:write"
            ):
                raise PermissionDenied("permission denied")
            if record.status == "cancelled":
                if record.cancel_request_sha256 != fingerprint:
                    raise Conflict("cancellation request changed")
                return _receipt(record)
            self._require_mutable(record, expected_version, plan_hash)
            changed = record.model_copy(
                update={
                    "status": "cancelled",
                    "version": record.version + 1,
                    "updated_at": _utc(self._clock()),
                    "cancelled_by": principal.user_id,
                    "cancel_request_sha256": fingerprint,
                }
            )
            row.payload = changed.model_dump(mode="json")
            await session.flush()
            return _receipt(changed)

    def _require_mutable(
        self, record: _HandoverRecord, expected_version: int, plan_hash: str
    ) -> None:
        if (
            record.status not in {"planned", "awaiting_signature"}
            or record.version != expected_version
            or record.plan_hash != plan_hash
            or record.expires_at <= _utc(self._clock())
        ):
            raise Conflict("plan is no longer confirmable")

    async def commit_pending(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
        confirm: bool,
        idempotency_key: str,
        verified_pending_plugin: VerifiedPendingPlugin,
    ) -> HandoverReceipt:
        return await self._commit_pending(
            None,
            principal,
            run_id,
            handover_id,
            expected_version,
            plan_hash,
            confirm,
            idempotency_key,
            verified_pending_plugin,
        )

    async def _commit_pending_in_session(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
        confirm: bool,
        idempotency_key: str,
        verified_pending_plugin: VerifiedPendingPlugin,
        *,
        validate_bridge: _BridgeValidate,
        consume_bridge: _BridgeConsume,
    ) -> HandoverReceipt:
        """Trusted bridge only; callbacks are code, never DTO/checked-flag auth proof.

        Parent validation must query/lock the actual grant and current users/run,
        compare both persisted grant fields, and check current signing key plus
        exact snapshot/package equivalence after the registration wait. If key
        validation waits, recheck grant/authority/expiry after that final wait.
        consume_bridge must consume the actual grant against this flushed receipt
        in the same transaction. Both callbacks must propagate failures; caller
        must let them escape BEGIN, not catch and commit partial writes.
        This seam never manages a transaction, maps SQL errors or reclaims bytes.
        Legacy records are readable, but cannot enter the strict bridge.
        """
        _require_caller_begin(session)
        if not callable(validate_bridge) or not callable(consume_bridge):
            raise StorageUnavailable("source grant bridge callbacks required")
        return await self._commit_pending(
            session,
            principal,
            run_id,
            handover_id,
            expected_version,
            plan_hash,
            confirm,
            idempotency_key,
            verified_pending_plugin,
            validate_bridge=validate_bridge,
            consume_bridge=consume_bridge,
        )

    async def _commit_pending(
        self,
        caller_session: AsyncSession | None,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
        confirm: bool,
        idempotency_key: str,
        verified_pending_plugin: VerifiedPendingPlugin,
        *,
        validate_bridge: _BridgeValidate | None = None,
        consume_bridge: _BridgeConsume | None = None,
    ) -> HandoverReceipt:
        self._require(principal, "plugin:write", "run:read", "plugin:read")
        if confirm is not True:
            raise ValueError("explicit confirmation is required")
        _check_confirmation_identity(expected_version, plan_hash)
        verified = _checked_dto(verified_pending_plugin, VerifiedPendingPlugin)
        plugin_payload = _pending_payload(verified)
        key_digest = _digest(
            "confirm-key",
            {
                "tenant_id": str(principal.tenant_id),
                "handover_id": handover_id,
                "installer_id": str(principal.user_id),
                "key": _safe_key(idempotency_key),
            },
        )
        fingerprint = _digest(
            "confirm",
            {
                "plan_hash": plan_hash,
                "expected_version": expected_version,
                "verified": _confirm_facts(verified, plugin_payload),
            },
        )
        async with self._operation(caller_session) as session:
            await self._require_run(session, principal.tenant_id, run_id)
            row, record = await self._load(
                session, principal.tenant_id, run_id, handover_id, lock=True
            )
            bound = record.source_grant_id is not None
            if bound and (validate_bridge is None or consume_bridge is None):
                raise StorageUnavailable("source grant bridge required")
            if not bound and (validate_bridge is not None or consume_bridge is not None):
                raise Conflict("source grant binding required for strict bridge")
            if record.status == "pending":
                if (
                    record.confirm_key_digest != key_digest
                    or record.confirm_request_sha256 != fingerprint
                ):
                    raise Conflict("handover already has a pending receipt")
                if validate_bridge is not None and consume_bridge is not None:
                    await acquire_plugin_registration_lock(
                        session, principal.tenant_id, record.plan_data.plugin_id
                    )
                    await validate_bridge(session, _receipt(record), verified)
                    await consume_bridge(session, _receipt(record))
                    if record.expires_at <= _utc(self._clock()):
                        raise Conflict("plan is no longer confirmable")
                return _receipt(record)
            self._require_mutable(record, expected_version, plan_hash)
            if (
                verified.tenant_id != principal.tenant_id
                or verified.run_id != run_id
                or verified.handover_id != handover_id
                or verified.plan_hash != record.plan_hash
                or verified.source_manifest_sha256 != record.source_manifest_sha256
                or plugin_payload["id"] != record.plan_data.plugin_id
                or plugin_payload["version"] != record.plan_data.plugin_version
                or record.plan_data.signature_payload_sha256
                not in {None, verified.signature_payload_sha256}
                or record.plan_data.archive_sha256 not in {None, verified.archive_sha256}
            ):
                raise Conflict("verified package does not match the plan")
            # All multi-lock callers take the handover row before this shared lock.
            await acquire_plugin_registration_lock(
                session, principal.tenant_id, record.plan_data.plugin_id
            )
            existing = await session.scalar(
                select(AdminResourceRow)
                .where(
                    AdminResourceRow.tenant_id == principal.tenant_id,
                    AdminResourceRow.kind == "plugin",
                    AdminResourceRow.resource_id == record.plan_data.plugin_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True, autoflush=False)
            )
            if existing is not None:
                raise Conflict("plugin is already registered; read its historical handover receipt")
            if validate_bridge is not None:
                await validate_bridge(session, _receipt(record), verified)
            # Both registration locks and the row query can outlive the plan's deadline.
            self._require_mutable(record, expected_version, plan_hash)
            now = _utc(self._clock())
            receipt_id = "gr-" + _digest("receipt", handover_id)
            audit_id = "ga-" + _digest("audit", handover_id)
            plugin_payload[GENERATED_HANDOVER_MARKER] = handover_id
            await session.execute(
                insert(AdminResourceRow).values(
                    tenant_id=principal.tenant_id,
                    kind="plugin",
                    resource_id=record.plan_data.plugin_id,
                    payload=plugin_payload,
                )
            )
            changed = record.model_copy(
                update={
                    "status": "pending",
                    "version": record.version + 1,
                    "updated_at": now,
                    "committed_at": now,
                    "installer_id": principal.user_id,
                    "signature_payload_sha256": verified.signature_payload_sha256,
                    "archive_sha256": verified.archive_sha256,
                    "confirm_key_digest": key_digest,
                    "confirm_request_sha256": fingerprint,
                    "key_id": verified.key_id,
                    "verified_public_key_sha256": verified.verified_public_key_sha256,
                    "artifact_ref": verified.artifact_ref,
                    "receipt_id": receipt_id,
                    "audit_id": audit_id,
                    "result": "blocked_pending_approval",
                }
            )
            row.payload = changed.model_dump(mode="json")
            await session.flush()
            audit = {
                "id": audit_id,
                "actor": str(principal.user_id),
                "action": "plugin.generated_handover.pending",
                "resource": f"plugin:{record.plan_data.plugin_id}",
                "created_at": now.isoformat(),
                "details": {
                    "handover_id": handover_id,
                    "run_id": str(run_id),
                    "creator_id": str(record.creator_id),
                    "installer_id": str(principal.user_id),
                    "plan_hash": record.plan_hash,
                    "source_manifest_sha256": record.source_manifest_sha256,
                    "signature_payload_sha256": verified.signature_payload_sha256,
                    "archive_sha256": verified.archive_sha256,
                    "key_id": verified.key_id,
                    "receipt_id": receipt_id,
                    "result": "blocked_pending_approval",
                },
            }
            await session.execute(
                insert(AdminResourceRow).values(
                    tenant_id=principal.tenant_id,
                    kind="audit",
                    resource_id=audit_id,
                    payload=audit,
                )
            )
            await session.flush()
            if consume_bridge is not None:
                await consume_bridge(session, _receipt(changed))
                self._require_mutable(record, expected_version, plan_hash)
            return _receipt(changed)


def _safe_key(key: str) -> str:
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise ValueError("invalid idempotency key")
    return key


def plan_identity(principal: AuthenticatedPrincipal, idempotency_key: str) -> str:
    """Pure stable identity only; no authorization or grant/plan-hash dependency."""
    if (
        type(principal) is not AuthenticatedPrincipal
        or type(principal.tenant_id) is not UUID
        or type(principal.user_id) is not UUID
    ):
        raise ValueError("stable tenant and creator UUID required")
    return "gh-" + _digest(
        "plan-key",
        {
            "tenant_id": str(principal.tenant_id),
            "creator_id": str(principal.user_id),
            "key": _safe_key(idempotency_key),
        },
    )


def _check_confirmation_identity(version: int, plan_hash: str) -> None:
    if (
        type(version) is not int
        or version < 1
        or not isinstance(plan_hash, str)
        or not re.fullmatch(r"[a-f0-9]{64}", plan_hash)
    ):
        raise ValueError("invalid confirmation identity")


def _plan_fingerprint(
    run_id: UUID,
    source: Source,
    source_hash: str,
    plan: PlanData,
    *,
    source_grant_id: str | None = None,
    source_grant_binding_sha256: str | None = None,
) -> str:
    pair = _GrantPair(
        source_grant_id=source_grant_id, source_grant_binding_sha256=source_grant_binding_sha256
    )
    data: dict[str, object] = {
        "run_id": str(run_id),
        "source": source.model_dump(mode="json"),
        "source_manifest_sha256": source_hash,
        "plan_data": plan.model_dump(mode="json"),
    }
    if pair.source_grant_id is not None:
        data.update(
            source_grant_id=pair.source_grant_id,
            source_grant_binding_sha256=pair.source_grant_binding_sha256,
        )
    return _digest("plan-request", data)


def _confirm_facts(
    verified: VerifiedPendingPlugin, payload: dict[str, object]
) -> dict[str, object]:
    facts = verified.model_dump(mode="json")
    normalized = json.loads(_canonical(payload))
    # Staging retry timestamps are not package content or confirmation identity.
    normalized["package_metadata"]["artifact"].pop("stored_at", None)
    facts["plugin_payload"] = normalized
    return facts


__all__ = [
    "GENERATED_HANDOVER_MARKER",
    "HANDOVER_KIND",
    "ArtifactSource",
    "AuthorizedSource",
    "Conflict",
    "HandoverReceipt",
    "NotFound",
    "PermissionDenied",
    "PlanData",
    "PluginHandoverStorage",
    "StorageUnavailable",
    "VerifiedPendingPlugin",
    "WorkspaceSource",
    "acquire_plugin_registration_lock",
    "plan_identity",
]
