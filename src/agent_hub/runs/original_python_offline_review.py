"""Private once-only review for the original, hash-locked offline Python phase."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.execution_authority import (
    ExecutionAuthorityError,
    get_verification_authority,
)
from agent_hub.db.models import RunApprovalRow, RunCheckpointRow, UserRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import (
    RunConflict,
    RunNotFound,
    RunRepository,
    _pre_model_original_capture,
    _python_project_approval_binding,
)
from agent_hub.runtime.contracts import _canonical_json_bytes, _freeze_object

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from agent_hub.api.routers.admin import PersistentToolApprovalMode
    from agent_hub.auth.models import Role
    from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
    from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
    from agent_hub.capabilities.types import CapabilityRequest
    from agent_hub.db.models import RunRow

_ACTION = 'original_python_offline_review'
_ID = 'original-python-offline-review-v1'
_ERROR = 'python_project_install_approval_required'
ORIGINAL_PYTHON_OFFLINE_MODES = frozenset({
    TaskMode.DIRECT, TaskMode.DISCUSS, TaskMode.DISPATCH, TaskMode.HYBRID,
})


@dataclass(frozen=True, slots=True)
class OriginalPythonOfflineReview:
    repository: RunRepository
    policy: DefaultRuntimeCapabilityPolicyGateway
    request: CapabilityRequest
    role: Role
    inputs: PythonProjectInputs

    @property
    def execution_id(self) -> str:
        return 'original_' + self.request.run_id.hex

    def _rows(self) -> Select[tuple[RunApprovalRow]]:
        return select(RunApprovalRow).where(RunApprovalRow.tenant_id == self.request.tenant_id,
                                            RunApprovalRow.run_id == self.request.run_id)

    async def _has_history(self, session: AsyncSession) -> bool:
        # Prior human pending/decisions for this exact install always stay manual.
        manual = and_(RunApprovalRow.action == 'capability_tool', or_(
            RunApprovalRow.payload['approval_scope'].astext == capability_approval_scope(self.request),
            RunApprovalRow.payload['approval_fingerprint'].astext == fingerprint_capability_request(self.request),
        ))
        rows = await session.scalars(self._rows().where(or_(RunApprovalRow.approval_id == _ID, manual)).limit(1))
        return bool(rows.all())

    async def eligible(self) -> bool:
        if self._settings_reader() is None:
            return False
        try:
            async with await self.repository.run_transaction() as session, session.begin():
                row = await self.repository.get_for_update(session, self.request.run_id)
                self._binding(row, phase='captured')
                return await self._locked_policy_allows(session, row) and not await self._has_history(session)
        except (ExecutionAuthorityError, RunConflict, RunNotFound, SQLAlchemyError):
            return False

    def _settings_reader(self) -> PersistentToolApprovalMode | None:
        from agent_hub.api.routers.admin import PersistentToolApprovalMode

        reader = self.policy._tool_approval_mode
        if (self.policy._run_repository is self.repository and type(reader) is PersistentToolApprovalMode
                and reader.uses_session_factory(self.repository._session_factory)):
            return reader
        return None

    def _binding(self, row: RunRow, *, phase: str) -> dict[str, object]:
        req = self.request
        authority = get_verification_authority(tenant_id=req.tenant_id, run_id=req.run_id, user_id=req.user_id,
                                              role=self.role, current_record=RunRepository._record(row))
        route = row.routing_decision or {}
        state = route.get('project_input_preparation')
        permissions = route.get('requested_permissions')
        if (authority.mode not in ORIGINAL_PYTHON_OFFLINE_MODES or authority.actor != 'main_agent'
                or req.tool_origin != 'builtin' or req.agent_id != authority.actor
                or req.idempotency_key != 'original-project-admission-' + req.run_id.hex
                or not self.inputs.matches_approval_request(req)
                or row.worker_id != authority.worker_id or row.worker_lease_token != authority.execution_token
                or row.worker_lease_expires_at is None or row.worker_lease_expires_at <= datetime.now(UTC)
                or route.get('permission_mode') != 'auto_review'
                or not isinstance(permissions, (list, tuple))
                or not all(type(permission) is str for permission in permissions)
                or not {'workspace.read', 'workspace.write', 'command.run'}.issubset(permissions)
                or 'generated_project_contract_version' in route or 'generated_project_preparation' in route
                or not isinstance(state, Mapping) or state.get('phase') != phase
                or (phase == 'captured' and not _pre_model_original_capture(route))
                or state.get('reference') != self.inputs.original_reference.to_payload()
                or (phase == 'executing' and state.get('execution_id') != self.execution_id)
                or _python_project_approval_binding(row, capability_approval_scope(req)) != req.arguments):
            raise RunConflict(_ERROR)
        # The verification scope digest includes the preparation phase, which
        # changes at reservation. The reference, recipe and exact phase/version
        # are checked separately. get_verification_authority checks stable routing.
        worker = authority.to_payload()
        del worker['scope_sha256']
        return {
            'schema_version': 1,
            'execution_id': self.execution_id,
            'worker_binding_sha256': sha256(_canonical_json_bytes(_freeze_object(
                worker, name='original offline worker binding'))).hexdigest(),
            'request_sha256': fingerprint_capability_request(req),
            'original_reference': self.inputs.original_reference.to_payload(),
            'recipe_sha256': self.inputs.sha256,
            'environment_input_sha256': self.inputs.environment_input_sha256,
        }

    async def _locked_policy_allows(self, session: AsyncSession, row: RunRow) -> bool:
        reader = self._settings_reader()
        if reader is None or not self.policy._original_python_offline_action_allowed(
            self.request, role=self.role, permission_mode=(row.routing_decision or {}).get('permission_mode'),
        ):
            return False
        # All three paths lock in the same order: run, user, system setting, receipt.
        user = await session.scalar(select(UserRow).where(
            UserRow.tenant_id == row.tenant_id, UserRow.id == row.actor_id).with_for_update())
        if (user is None or user.tenant_id != row.tenant_id or user.id != row.actor_id
                or user.disabled is not False or user.role != row.actor_role):
            return False
        return await reader.locked_auto_review(
            session, factory=self.repository._session_factory, tenant_id=row.tenant_id,
        )

    async def reserve(self, session: AsyncSession, row: RunRow) -> None:
        """Called under the captured-to-executing CAS row lock."""
        binding = self._binding(row, phase='captured')
        if (not await self._locked_policy_allows(session, row)
                or await self._has_history(session)
                or await session.scalar(select(RunCheckpointRow.id).where(
                    RunCheckpointRow.tenant_id == row.tenant_id, RunCheckpointRow.run_id == row.id).limit(1))
                is not None
                or await self.repository._recovery_blocked_after_checkpoint(
                    session, run_id=row.id, pre_model_original_capture=True)
                or self._binding(row, phase='captured') != binding):
            raise RunConflict(_ERROR)
        session.add(RunApprovalRow(tenant_id=row.tenant_id, run_id=row.id, approval_id=_ID,
            action=_ACTION, status='reserved', payload={**binding, 'run_version': row.version + 1}))
        await session.flush()
        if self._binding(row, phase='captured') != binding:
            raise RunConflict(_ERROR)

    async def verify(self, repository: object, inputs: PythonProjectInputs, *, actor: str,
                     execution_id: str, consume: bool = False) -> None:
        if (repository is not self.repository or inputs != self.inputs or actor != self.request.agent_id
                or execution_id != self.execution_id):
            raise RunConflict(_ERROR)
        async with await self.repository.run_transaction() as session, session.begin():
            row = await self.repository.get_for_update(session, self.request.run_id)
            binding = self._binding(row, phase='executing')
            if not await self._locked_policy_allows(session, row):
                raise RunConflict(_ERROR)
            receipt = await session.scalar(self._rows().where(RunApprovalRow.approval_id == _ID).with_for_update())
            if (receipt is None or (receipt.tenant_id, receipt.run_id, receipt.approval_id, receipt.action)
                    != (row.tenant_id, row.id, _ID, _ACTION)
                    or receipt.payload != {**binding, 'run_version': row.version}
                    or receipt.status != ('reserved' if consume else 'consumed')
                    or self._binding(row, phase='executing') != binding):
                raise RunConflict(_ERROR)
            if consume:
                receipt.status = 'consumed'
                await session.flush()
                if self._binding(row, phase='executing') != binding:
                    raise RunConflict(_ERROR)
        # Returning from verify is the dispatch fence. The settings lock and
        # consumed receipt share the commit, before any native call.
