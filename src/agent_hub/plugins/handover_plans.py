"""Creator-side plan composition, not signing, installation or activation.

Private snapshots may outlive a rolled-back SQL transaction. They are immutable
and bounded by the snapshot store quota; do not delete a potentially shared blob
when a DB commit result is unknown. The receipt never exposes private paths.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied
from agent_hub.plugins.handover_grants import DurableSourceGrantService
from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore
from agent_hub.plugins.handover_sources import (
    FrozenSourceSnapshot,
    HandoverSourceService,
    SourceLimits,
    _checked_source,
)
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    HandoverReceipt,
    PlanData,
    PluginHandoverStorage,
    WorkspaceSource,
    _utc,
    acquire_plugin_registration_lock,
    plan_identity,
)
from agent_hub.plugins.handover_verification import _checked_snapshot, _contract, _manifest


def _plan_data(snapshot: FrozenSourceSnapshot) -> PlanData:
    checked, raw_manifest = _checked_snapshot(snapshot, SourceLimits())
    manifest = _contract(_manifest(raw_manifest.content), checked.files)
    return PlanData(
        plugin_id=checked.plugin_id,
        plugin_version=checked.plugin_version,
        permissions=tuple(sorted({item.permission_class for item in manifest.capabilities})),
        risk_summary=("generated_code", "approval_required"),
    )


class GeneratedPluginPlanService:
    def __init__(
        self,
        *,
        sources: HandoverSourceService,
        snapshots: FrozenSourceSnapshotStore,
        grants: DurableSourceGrantService,
        storage: PluginHandoverStorage,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sources = sources
        self._snapshots = snapshots
        self._grants = grants
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create_plan(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource | WorkspaceSource,
        *,
        idempotency_key: str,
        expires_at: datetime,
    ) -> HandoverReceipt:
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(principal.user_id) is not UUID
            or type(principal.tenant_id) is not UUID
            or type(run_id) is not UUID
        ):
            raise PermissionDenied("source access denied")
        selector = _checked_source(source)
        identity = plan_identity(principal, idempotency_key)
        expiry, now = _utc(expires_at), _utc(self._clock())
        if not now < expiry <= now + timedelta(hours=24):
            raise ValueError("plan expiry must be within twenty-four hours")
        # Opening the real transaction first also fails closed when storage is absent.
        async with self._storage._transaction() as session:
            frozen = await self._sources.freeze(principal, run_id, selector)
            plan = _plan_data(frozen)
            await acquire_plugin_registration_lock(session, principal.tenant_id, frozen.plugin_id)
            await self._grants._user(
                session,
                principal.tenant_id,
                principal.user_id,
                "run:create",
                "run:read",
                "plugin:read",
            )
            if expiry <= _utc(self._clock()):
                raise ValueError("plan expired while waiting for authorization")
            reference = self._snapshots.publish(frozen, creator_id=principal.user_id)
            stored = self._snapshots.read(
                tenant_id=principal.tenant_id,
                snapshot_id=reference.snapshot_id,
                blob_sha256=reference.blob_sha256,
                source_manifest_sha256=reference.source_manifest_sha256,
            )
            if stored.creator_id != principal.user_id or stored.snapshot != frozen:
                raise PermissionDenied("stored source differs from frozen source")
            grant = await self._grants.publish(
                session,
                principal,
                stored,
                plan_id=identity,
                plan_version=1,
                expires_at=expiry,
            )
            receipt = await self._storage.create_plan_in_session(
                session,
                principal,
                run_id,
                frozen.authorized,
                plan,
                idempotency_key,
                expiry,
                source_grant_id=grant.grant_id,
                source_grant_binding_sha256=grant.binding_sha256,
            )
            if expiry <= _utc(self._clock()):
                raise ValueError("plan expired while waiting for persistence")
            return receipt
