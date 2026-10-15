"""Caller-transaction SQL source grants, not an installation or public proof.

Only trusted freezer/private-store outputs enter publish. DTOs are not authority:
every operation queries current stable User UUIDs; validation locks current Run
and published Artifact rows after the shared registration wait. No session
factory, commit/rollback, source cleanup, signature or execution lives here.

The parent still owns the locked plan, package/key validation and atomic audit /
pending registration. Flush its pending receipt before consume in that same
explicit PostgreSQL session.begin(). Exceptions must escape that transaction.
Unknown commit recovery reads the durable receipt; never reclaim a snapshot.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import SessionTransactionOrigin

from agent_hub.auth.models import PERMISSIONS, AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, RunRow, UserRow
from agent_hub.plugins.handover_snapshots import (
    SnapshotReference,
    SnapshotStoreLimits,
    StoredSourceSnapshot,
    _encode,
)
from agent_hub.plugins.handover_sources import _canonical, _checked_source
from agent_hub.plugins.handover_storage import (
    HANDOVER_KIND,
    ArtifactSource,
    Conflict,
    Digest,
    HandoverId,
    Identifier,
    PluginHandoverStorage,
    Source,
    StorageUnavailable,
    WorkspaceSource,
    _digest,
    _utc,
    acquire_plugin_registration_lock,
)
from agent_hub.runtime.contracts import Artifact

GrantId = Annotated[str, Field(pattern=r"^sg-[a-f0-9]{64}$", min_length=67, max_length=67)]
_Segment = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$", max_length=64)]


class _DTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class RunAssociation(_DTO):
    project_id: _Segment | None
    session_id: _Segment | None


class ArtifactBinding(_DTO):
    artifact_id: UUID
    content_sha256: Digest
    payload_sha256: Digest
    type: Identifier
    producer: Identifier


class GrantBinding(_DTO):
    """Immutable confirmation slot / source identity, never final_plan_hash."""

    tenant_id: UUID
    creator_id: UUID
    run_id: UUID
    plan_id: HandoverId
    plan_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    purpose: Literal["plugin_handover"] = "plugin_handover"
    expires_at: datetime
    plugin_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")]
    plugin_version: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$", max_length=128)]
    snapshot_id: Annotated[str, Field(pattern=r"^hs-[a-f0-9]{64}$", min_length=67, max_length=67)]
    blob_sha256: Digest
    blob_size_bytes: Annotated[int, Field(ge=1, le=3_000_000)]
    source_manifest_sha256: Digest
    source: Source
    association: RunAssociation
    artifact: ArtifactBinding | None

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def source_identity(self) -> GrantBinding:
        checked = _checked_source(self.source)
        if self.snapshot_id != "hs-" + self.blob_sha256:
            raise ValueError("snapshot identity does not match blob")
        if isinstance(checked, WorkspaceSource):
            if self.artifact is not None or self.association != RunAssociation(
                project_id=checked.project_id,
                session_id=checked.session_id,
            ):
                raise ValueError("workspace binding does not match source")
        elif self.artifact is None or str(self.artifact.artifact_id) != checked.artifact_id:
            raise ValueError("artifact binding does not match source")
        return self


def _grant_id(binding: GrantBinding) -> str:
    # A different blob/expiry for the same confirmation slot conflicts, not a new grant.
    return "sg-" + _digest(
        "source-grant-key",
        {
            "tenant_id": str(binding.tenant_id),
            "creator_id": str(binding.creator_id),
            "run_id": str(binding.run_id),
            "plan_id": binding.plan_id,
            "plan_version": binding.plan_version,
            "purpose": binding.purpose,
        },
    )


def _binding_hash(binding: GrantBinding) -> str:
    return _digest("generated-handover-source-grant-binding", binding.model_dump(mode="json"))


class SourceGrant(_DTO):
    schema_version: Literal[1] = 1
    domain: Literal["generated-handover-source-grant"] = "generated-handover-source-grant"
    record_type: Literal["source_grant"] = "source_grant"
    grant_id: GrantId
    binding: GrantBinding
    binding_sha256: Digest
    state: Literal["active", "consumed", "revoked"] = "active"
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None
    consumed_at: datetime | None = None
    consumed_by: UUID | None = None
    receipt_id: Identifier | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("invalid grant schema")
        return value

    @field_validator("created_at", "revoked_at", "consumed_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def coherent_record(self) -> SourceGrant:
        consumed = (self.consumed_at, self.consumed_by, self.receipt_id)
        revoked = (self.revoked_at, self.revoked_by)
        if (
            self.grant_id != _grant_id(self.binding)
            or self.binding_sha256 != _binding_hash(self.binding)
            or self.created_at >= self.binding.expires_at
            or (any(v is not None for v in consumed) and not all(v is not None for v in consumed))
            or (any(v is not None for v in revoked) and not all(v is not None for v in revoked))
            or (self.state == "active" and any(v is not None for v in (*consumed, *revoked)))
            or (
                self.state == "consumed"
                and (self.consumed_at is None or self.revoked_at is not None)
            )
            or (self.state == "revoked" and self.revoked_at is None)
            or (
                self.consumed_at is not None
                and not self.created_at <= self.consumed_at < self.binding.expires_at
            )
            or (self.revoked_at is not None and self.revoked_at < self.created_at)
            or (
                self.revoked_at is not None
                and self.consumed_at is not None
                and self.revoked_at < self.consumed_at
            )
            or (
                self.receipt_id is not None
                and self.receipt_id != "gr-" + _digest("receipt", self.binding.plan_id)
            )
        ):
            raise ValueError("inconsistent source grant")
        return self


def _transaction(session: AsyncSession) -> None:
    if not isinstance(session, AsyncSession):
        raise StorageUnavailable("caller PostgreSQL transaction required")
    transaction = session.get_transaction()
    sync_transaction = None if transaction is None else transaction.sync_transaction
    if (
        sync_transaction is None
        or not session.is_active
        or sync_transaction.origin is not SessionTransactionOrigin.BEGIN
        or session.get_bind().dialect.name != "postgresql"
    ):
        raise StorageUnavailable("caller PostgreSQL transaction required")


def _principal(principal: AuthenticatedPrincipal, tenant_id: UUID | None = None) -> None:
    if (
        type(principal) is not AuthenticatedPrincipal
        or type(principal.user_id) is not UUID
        or type(principal.tenant_id) is not UUID
        or (tenant_id is not None and principal.tenant_id != tenant_id)
    ):
        raise PermissionDenied("source grant access denied")


def _permissions(row: UserRow, *permissions: str) -> None:
    try:
        grants = PERMISSIONS[Role(row.role)]
    except (ValueError, KeyError):
        raise PermissionDenied("source grant access denied") from None
    if row.disabled is not False or any(
        "*" not in grants
        and permission not in grants
        and permission.split(":", 1)[0] + ":*" not in grants
        for permission in permissions
    ):
        raise PermissionDenied("source grant access denied")


def _association(run: RunRow) -> RunAssociation:
    routing = run.routing_decision
    if routing is not None and type(routing) is not dict:
        raise PermissionDenied("source grant access denied")
    try:
        return RunAssociation.model_validate(
            {
                "project_id": (routing or {}).get("project_id"),
                "session_id": (routing or {}).get("workspace_session_id"),
            }
        )
    except ValueError:
        raise PermissionDenied("source grant access denied") from None


class DurableSourceGrantService:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return _utc(self._clock())

    async def _user(
        self, session: AsyncSession, tenant: UUID, user_id: UUID, *permissions: str
    ) -> UserRow:
        row = await session.scalar(
            select(UserRow)
            .where(
                UserRow.tenant_id == tenant,
                UserRow.id == user_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None or row.id != user_id or row.tenant_id != tenant:
            raise PermissionDenied("source grant access denied")
        _permissions(row, *permissions)
        return row

    async def _run(
        self, session: AsyncSession, tenant: UUID, run_id: UUID, creator: UUID
    ) -> RunRow:
        row = await session.scalar(
            select(RunRow)
            .where(
                RunRow.tenant_id == tenant,
                RunRow.id == run_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None or (row.id, row.tenant_id, row.actor_id) != (run_id, tenant, creator):
            raise PermissionDenied("source grant access denied")
        return row

    async def _artifact(
        self, session: AsyncSession, tenant: UUID, run: UUID, source: ArtifactSource
    ) -> ArtifactBinding:
        artifact_id = UUID(source.artifact_id)
        row = await session.scalar(
            select(RunArtifactRow)
            .where(
                RunArtifactRow.tenant_id == tenant,
                RunArtifactRow.run_id == run,
                RunArtifactRow.id == artifact_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None or (row.id, row.tenant_id, row.run_id) != (artifact_id, tenant, run):
            raise PermissionDenied("source grant access denied")
        try:
            parsed = Artifact.from_payload(row.payload)
            if (parsed.id, parsed.type, parsed.producer, parsed.content_sha256) != (
                row.id,
                row.type,
                row.producer,
                row.content_sha256,
            ):
                raise ValueError("artifact columns differ from publication")
            return ArtifactBinding(
                artifact_id=row.id,
                content_sha256=row.content_sha256,
                type=row.type,
                producer=row.producer,
                payload_sha256=hashlib.sha256(_canonical(row.payload)).hexdigest(),
            )
        except (ValueError, TypeError):
            raise PermissionDenied("source grant access denied") from None

    async def _load(
        self, session: AsyncSession, tenant: UUID, grant_id: str
    ) -> tuple[AdminResourceRow, SourceGrant]:
        if type(grant_id) is not str or not re.fullmatch(r"sg-[a-f0-9]{64}", grant_id):
            raise PermissionDenied("source grant access denied")
        row = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == tenant,
                AdminResourceRow.kind == HANDOVER_KIND,
                AdminResourceRow.resource_id == grant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise PermissionDenied("source grant access denied")
        try:
            grant = SourceGrant.model_validate_json(_canonical(row.payload))
            if (
                not isinstance(row.id, UUID)
                or row.tenant_id != tenant
                or row.kind != HANDOVER_KIND
                or row.resource_id != grant_id
                or grant.grant_id != grant_id
                or grant.binding.tenant_id != tenant
                or _canonical(row.payload) != _canonical(grant.model_dump(mode="json"))
            ):
                raise ValueError("grant row identity differs")
            return row, grant
        except (ValueError, TypeError):
            raise StorageUnavailable("invalid durable source grant") from None

    def _live(self, grant: SourceGrant, *, consumed: bool = False) -> None:
        now = self._now()
        if (
            grant.state == "revoked"
            or (grant.state == "consumed" and not consumed)
            or not grant.created_at <= now < grant.binding.expires_at
        ):
            raise Conflict("source grant is no longer live")

    async def publish(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        stored: StoredSourceSnapshot,
        *,
        plan_id: str,
        plan_version: int,
        expires_at: datetime,
    ) -> SourceGrant:
        _transaction(session)
        _principal(principal)
        if (
            type(stored) is not StoredSourceSnapshot
            or type(stored.reference) is not SnapshotReference
        ):
            raise ValueError("trusted stored snapshot required")
        frozen = stored.snapshot
        # Canonical blob / freezer provenance checks do not substitute for SQL authority.
        blob = _encode(frozen, stored.creator_id, SnapshotStoreLimits())
        if (
            stored.creator_id != principal.user_id
            or frozen.authorized.tenant_id != principal.tenant_id
        ):
            raise PermissionDenied("source grant access denied")
        sha = hashlib.sha256(blob).hexdigest()
        if stored.reference != SnapshotReference(
            "hs-" + sha, sha, len(blob), frozen.authorized.source_manifest_sha256
        ):
            raise ValueError("snapshot reference differs from canonical bytes")
        source = _checked_source(frozen.authorized.source)
        await acquire_plugin_registration_lock(session, principal.tenant_id, frozen.plugin_id)
        await self._user(
            session, principal.tenant_id, principal.user_id, "run:create", "run:read", "plugin:read"
        )
        run = await self._run(
            session, principal.tenant_id, frozen.authorized.run_id, principal.user_id
        )
        association = _association(run)
        if isinstance(source, WorkspaceSource) and association != RunAssociation(
            project_id=source.project_id,
            session_id=source.session_id,
        ):
            raise PermissionDenied("source grant access denied")
        artifact = (
            await self._artifact(session, principal.tenant_id, run.id, source)
            if isinstance(source, ArtifactSource)
            else None
        )
        source_sha = hashlib.sha256(
            _canonical(
                {
                    "schema_version": 1,
                    "domain": "generated-handover-source",
                    "tenant_id": str(principal.tenant_id),
                    "run_id": str(run.id),
                    "actor_id": str(principal.user_id),
                    "source": source.model_dump(mode="json"),
                    "artifact_sha256": None if artifact is None else artifact.payload_sha256,
                    "files": [
                        {"path": f.path, "size_bytes": f.size_bytes, "sha256": f.sha256}
                        for f in frozen.files
                    ],
                }
            )
        ).hexdigest()
        if source_sha != frozen.authorized.source_manifest_sha256:
            raise PermissionDenied("source grant provenance changed")
        binding = GrantBinding(
            tenant_id=principal.tenant_id,
            creator_id=principal.user_id,
            run_id=run.id,
            plan_id=plan_id,
            plan_version=plan_version,
            expires_at=expires_at,
            plugin_id=frozen.plugin_id,
            plugin_version=frozen.plugin_version,
            snapshot_id=stored.reference.snapshot_id,
            blob_sha256=sha,
            blob_size_bytes=len(blob),
            source_manifest_sha256=source_sha,
            source=source,
            association=association,
            artifact=artifact,
        )
        now = self._now()
        if not now < binding.expires_at <= now + timedelta(hours=24):
            raise Conflict("source grant expiry is invalid")
        proposed = SourceGrant(
            grant_id=_grant_id(binding),
            binding=binding,
            binding_sha256=_binding_hash(binding),
            created_at=now,
        )
        await session.execute(
            insert(AdminResourceRow)
            .values(
                id=uuid4(),
                tenant_id=binding.tenant_id,
                kind=HANDOVER_KIND,
                resource_id=proposed.grant_id,
                payload=proposed.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "kind", "resource_id"])
            .execution_options(autoflush=False)
        )
        _, current = await self._load(session, binding.tenant_id, proposed.grant_id)
        if current.binding != binding:
            raise Conflict("confirmation slot already has a different source grant")
        self._live(current, consumed=True)
        return current

    async def _verify(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        expected: GrantBinding,
        *,
        consumed: bool = False,
    ) -> tuple[AdminResourceRow, SourceGrant]:
        _transaction(session)
        _principal(principal)
        if type(expected) is not GrantBinding:
            raise ValueError("locked plan binding required")
        expected = GrantBinding.model_validate(expected.model_dump(mode="python"))
        _principal(principal, expected.tenant_id)
        await acquire_plugin_registration_lock(session, expected.tenant_id, expected.plugin_id)
        row, grant = await self._load(session, expected.tenant_id, _grant_id(expected))
        if grant.binding != expected:
            raise Conflict("source grant differs from locked plan binding")
        self._live(grant, consumed=consumed)
        permissions = {principal.user_id: {"plugin:write", "plugin:read", "run:read"}}
        permissions.setdefault(expected.creator_id, set()).update(
            {"run:create", "run:read", "plugin:read"}
        )
        for user_id in sorted(permissions, key=lambda value: value.int):
            await self._user(session, expected.tenant_id, user_id, *sorted(permissions[user_id]))
        run = await self._run(session, expected.tenant_id, expected.run_id, expected.creator_id)
        if _association(run) != expected.association:
            raise PermissionDenied("source grant association changed")
        if (
            isinstance(expected.source, ArtifactSource)
            and await self._artifact(session, expected.tenant_id, expected.run_id, expected.source)
            != expected.artifact
        ):
            raise PermissionDenied("source grant artifact changed")
        self._live(grant, consumed=consumed)
        return row, grant

    async def verify(
        self, session: AsyncSession, principal: AuthenticatedPrincipal, *, expected: GrantBinding
    ) -> SourceGrant:
        """Parent must also hold/check its plan; this return is not a reusable capability."""
        return (await self._verify(session, principal, expected))[1]

    async def _persist(
        self,
        session: AsyncSession,
        row: AdminResourceRow,
        previous: SourceGrant,
        current: SourceGrant,
    ) -> None:
        saved = await session.scalar(
            update(AdminResourceRow)
            .where(
                AdminResourceRow.id == row.id,
                AdminResourceRow.tenant_id == previous.binding.tenant_id,
                AdminResourceRow.kind == HANDOVER_KIND,
                AdminResourceRow.resource_id == previous.grant_id,
                AdminResourceRow.payload == previous.model_dump(mode="json"),
            )
            .values(payload=current.model_dump(mode="json"))
            .returning(AdminResourceRow.id)
            .execution_options(autoflush=False, synchronize_session=False)
        )
        if saved != row.id:
            raise Conflict("source grant row changed")

    async def revoke(
        self, session: AsyncSession, principal: AuthenticatedPrincipal, *, grant_id: str
    ) -> SourceGrant:
        """Only sg row + current revoker User SHARE; never plan/registration locks."""
        _transaction(session)
        _principal(principal)
        row, grant = await self._load(session, principal.tenant_id, grant_id)
        own = principal.user_id == grant.binding.creator_id
        await self._user(
            session,
            principal.tenant_id,
            principal.user_id,
            "run:read",
            "plugin:read",
            *(() if own else ("plugin:write",)),
        )
        if grant.state == "revoked":
            return grant
        current = SourceGrant.model_validate(
            {
                **grant.model_dump(mode="python"),
                "state": "revoked",
                "revoked_at": self._now(),
                "revoked_by": principal.user_id,
            }
        )
        await self._persist(session, row, grant, current)
        return current

    async def consume(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        *,
        expected: GrantBinding,
        receipt_id: str,
    ) -> SourceGrant:
        """Lock/check actual pending receipt first, then registration -> grant -> authority.

        The caller must flush the receipt in its transaction; DTO receipts or a
        missing/mismatched plan never consume. No commit or filesystem cleanup.
        """
        _transaction(session)
        if type(expected) is not GrantBinding:
            raise ValueError("locked plan binding required")
        expected = GrantBinding.model_validate(expected.model_dump(mode="python"))
        _principal(principal, expected.tenant_id)
        plan = await session.scalar(
            select(AdminResourceRow)
            .where(
                AdminResourceRow.tenant_id == expected.tenant_id,
                AdminResourceRow.kind == HANDOVER_KIND,
                AdminResourceRow.resource_id == expected.plan_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True, autoflush=False)
        )
        if plan is None:
            raise Conflict("actual pending receipt required")
        record = PluginHandoverStorage._record(plan, expected.tenant_id, expected.run_id)
        if (
            record.status != "pending"
            or record.receipt_id != receipt_id
            or record.creator_id != expected.creator_id
            or record.installer_id != principal.user_id
            or record.version != expected.plan_version + 1
            or record.expires_at != expected.expires_at
            or record.plugin_id != expected.plugin_id
            or record.plugin_version != expected.plugin_version
            or record.source != expected.source
            or record.source_manifest_sha256 != expected.source_manifest_sha256
        ):
            raise Conflict("receipt differs from source grant binding")
        row, grant = await self._verify(session, principal, expected, consumed=True)
        if grant.state == "consumed":
            if grant.receipt_id != receipt_id or grant.consumed_by != principal.user_id:
                raise Conflict("source grant already consumed by another receipt")
            return grant
        current = SourceGrant.model_validate(
            {
                **grant.model_dump(mode="python"),
                "state": "consumed",
                "consumed_at": self._now(),
                "consumed_by": principal.user_id,
                "receipt_id": receipt_id,
            }
        )
        self._live(current, consumed=True)
        await self._persist(session, row, grant, current)
        return current
