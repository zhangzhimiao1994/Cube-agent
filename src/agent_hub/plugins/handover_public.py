"""Public handover composition; stored receipts are not installation authority."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.plugins.handover_confirmation import GeneratedPluginConfirmationService
from agent_hub.plugins.handover_grants import DurableSourceGrantService
from agent_hub.plugins.handover_plans import GeneratedPluginPlanService
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    HandoverReceipt,
    NotFound,
    PluginHandoverStorage,
    WorkspaceSource,
)


class GeneratedPluginHandoverService:
    def __init__(
        self,
        *,
        plans: GeneratedPluginPlanService,
        confirmation: GeneratedPluginConfirmationService,
        storage: PluginHandoverStorage,
        grants: DurableSourceGrantService,
    ) -> None:
        self._plans = plans
        self._confirmation = confirmation
        self._storage = storage
        self._grants = grants

    async def create_plan(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        source: ArtifactSource | WorkspaceSource,
        *,
        idempotency_key: str,
        expires_at: datetime,
    ) -> HandoverReceipt:
        return await self._plans.create_plan(
            principal, run_id, source, idempotency_key=idempotency_key, expires_at=expires_at
        )

    async def confirm(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
        confirm: bool,
        idempotency_key: str,
        archive_bytes: bytes,
        key_id: str,
        request: Request,
    ) -> HandoverReceipt:
        return await self._confirmation.confirm(
            principal, run_id, handover_id, expected_version, plan_hash, confirm,
            idempotency_key, archive_bytes, key_id, request,
        )

    @staticmethod
    def _identity(principal: AuthenticatedPrincipal, run_id: UUID) -> None:
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(principal.user_id) is not UUID
            or type(principal.tenant_id) is not UUID
            or type(run_id) is not UUID
        ):
            raise PermissionDenied("handover access denied")

    async def _current(
        self, session: AsyncSession, principal: AuthenticatedPrincipal, *permissions: str
    ) -> AuthenticatedPrincipal:
        user = await self._grants._user(
            session, principal.tenant_id, principal.user_id, *permissions
        )
        # Native DB UUIDs may be subclasses. Preserve exact authenticated IDs;
        # only the locked SQL role, never the input role, grants current authority.
        return AuthenticatedPrincipal(principal.user_id, principal.tenant_id, Role(user.role))

    async def _visible(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
    ) -> HandoverReceipt:
        receipt = await self._storage.get_receipt_in_session(
            session, principal, run_id, handover_id
        )
        if receipt.creator_id != principal.user_id and not self._storage._authorizer.is_allowed(
            principal, "plugin:write"
        ):
            raise NotFound("handover was not found")
        return receipt

    async def get_receipt(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str
    ) -> HandoverReceipt:
        self._identity(principal, run_id)
        async with self._storage._transaction() as session:
            current = await self._current(session, principal, "run:read", "plugin:read")
            return await self._visible(session, current, run_id, handover_id)

    async def cancel_plan(
        self,
        principal: AuthenticatedPrincipal,
        run_id: UUID,
        handover_id: str,
        expected_version: int,
        plan_hash: str,
    ) -> HandoverReceipt:
        self._identity(principal, run_id)
        async with self._storage._transaction() as session:
            current = await self._current(
                session, principal, "run:create", "run:read", "plugin:read"
            )
            await self._visible(session, current, run_id, handover_id)
            return await self._storage.cancel_plan_in_session(
                session, current, run_id, handover_id, expected_version, plan_hash
            )
