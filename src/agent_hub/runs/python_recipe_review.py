"""Exact offline candidate verification under persisted task consent, never supply authority."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import select

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunOutboxRow,
    RunRow,
    UserRow,
)
from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import (
    RunConflict,
    RunRepository,
    _database_uuid,
    _python_project_approval_binding,
)
from agent_hub.runtime.contracts import JsonValue, RuntimeCheckpoint
from agent_hub.skills.sandbox.project import project_source_fingerprint, project_source_manifest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=dict, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class PythonRecipeReview:
    """A rules decision with original provenance; no model/human approval is invented."""

    def __init__(self, policy: DefaultRuntimeCapabilityPolicyGateway) -> None:
        self.policy = policy

    def _binding(self, row: RunRow, request: CapabilityRequest, role: Role) -> dict[str, object]:
        from agent_hub.capabilities.defaults import (
            _explicit_policy_effect,
            default_capability_policy,
        )

        route = row.routing_decision or {}
        state = route.get('project_input_preparation')
        scope = capability_approval_scope(request)
        binding = _python_project_approval_binding(row, scope)
        permissions = cast(Iterable[object], route.get('requested_permissions', ()))
        rules = self.policy._extra_policy_rules(request)
        if (self.policy._approval_reviewer is not None or request.tool_origin != 'builtin'
                or (_database_uuid(row.tenant_id), _database_uuid(row.id), row.actor_id, row.actor_role)
                != (request.tenant_id, request.run_id, request.user_id, role.value)
                or (request.capability, request.operation) != ('command', 'run')
                or binding is None or binding != request.arguments
                or route.get('permission_mode') != 'auto_review'
                or not {'workspace.read', 'workspace.write', 'command.run'}.issubset(permissions)
                or 'generated_project_preparation' in route or 'generated_project_contract_version' in route
                or not isinstance(state, dict) or state.get('phase') != 'observed'
                or _explicit_policy_effect(rules, request, role) in {PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL}
                or default_capability_policy(request.tenant_id).evaluate(request, role).effect is not PolicyEffect.ALLOW):
            raise RunConflict('python_recipe_review_unavailable')
        return {'python_binding': binding, 'approval_scope': scope,
                'original_reference': state['reference'], 'observation_sha256': state['observation_sha256'],
                'policy_sha256': default_capability_policy(request.tenant_id, extra_rules=rules).policy_sha256()}

    async def _source(self, request: CapabilityRequest) -> dict[str, object]:
        from agent_hub.capabilities.project_verification import ProjectVerificationService

        getter = self.policy._python_recipe_verifier
        verifier = getter() if getter is not None else None
        if type(verifier) is not ProjectVerificationService or verifier.repository is not self.policy._run_repository:
            raise RunConflict('python_recipe_review_unavailable')
        assert verifier is not None
        context = await verifier._python.select(request.tenant_id, request.run_id, request.agent_id,
                                                phases=('observed',), readonly=True)
        if context is None or request != context.inputs.approval_request(
                user_id=context.user_id, agent_id=request.agent_id, idempotency_key=request.idempotency_key):
            raise RunConflict('python_recipe_review_unavailable')
        context.stored.contract.require_current(await project_source_manifest(verifier.root, context.invocation))
        return {'source_sha256': await project_source_fingerprint(verifier.root, context.invocation),
                'original_reference': context.stored.reference.to_payload(),
                'observation_sha256': context.observation_sha256}

    async def _locked(self, session: AsyncSession, row: RunRow, request: CapabilityRequest,
                      role: Role) -> dict[str, object]:
        from agent_hub.api.routers.admin import PersistentToolApprovalMode

        repo = self.policy._run_repository
        reader = self.policy._tool_approval_mode
        binding = self._binding(row, request, role)
        if (not isinstance(repo, RunRepository) or type(reader) is not PersistentToolApprovalMode
                or not reader.uses_session_factory(repo._session_factory)):
            raise RunConflict('python_recipe_review_unavailable')
        assert isinstance(reader, PersistentToolApprovalMode)
        user = await session.scalar(select(UserRow).where(
            UserRow.tenant_id == row.tenant_id, UserRow.id == row.actor_id).with_for_update())
        if (user is None or user.tenant_id != row.tenant_id or user.id != row.actor_id
                or user.disabled is not False or user.role != row.actor_role
                or not await reader.locked_auto_review(session, factory=repo._session_factory, tenant_id=row.tenant_id)):
            raise RunConflict('python_recipe_review_unavailable')
        return binding

    async def review(self, request: CapabilityRequest, role: Role, *, create: bool = False) -> bool:
        """Recheck live policy/source; only create=True may append an automatic audit."""
        repo = self.policy._run_repository
        if (not isinstance(repo, RunRepository) or request.tool_origin != 'builtin'
                or (request.capability, request.operation) != ('command', 'run')
                or re.fullmatch(r'workspace/current/project-verification/python/[a-f0-9]{64}', request.resource) is None):
            return False
        try:
            request = CapabilityRequest.model_validate_json(request.model_dump_json())
            source = await self._source(request)
            async with repo._session_factory() as session, session.begin():
                row = await session.scalar(repo._run_select(request.tenant_id, request.run_id).with_for_update())
                if (row is None or row.status != RunStatus.RUNNING.value or row.worker_id is None
                        or row.worker_lease_token is None or row.worker_lease_expires_at is None
                        or row.worker_lease_expires_at <= datetime.now(UTC)):
                    return False
                from agent_hub.capabilities.execution_authority import get_verification_authority

                get_verification_authority(tenant_id=request.tenant_id, run_id=request.run_id,
                    user_id=request.user_id, role=role, current_record=repo._record(row))
                binding = await self._locked(session, row, request, role)
                if any(binding.get(k) != source[k] for k in ('original_reference', 'observation_sha256')):
                    return False
                binding['source_sha256'] = source['source_sha256']
                rows = (await session.scalars(select(RunApprovalRow).where(
                    RunApprovalRow.tenant_id == row.tenant_id, RunApprovalRow.run_id == row.id,
                    RunApprovalRow.action == 'capability_tool.auto_review', RunApprovalRow.status == 'approved',
                ).with_for_update())).all()
                if any(r.payload.get('python_recipe_review') == binding for r in rows):
                    return True
                if not create:
                    return False
                await self._audit(session, row, request, binding)
            return True
        except Exception:  # noqa: BLE001 - unavailable proof never grants an action.
            return False

    async def _audit(self, session: AsyncSession, row: RunRow, request: CapabilityRequest,
                     binding: dict[str, object]) -> str:
        from uuid import uuid4

        fingerprint = fingerprint_capability_request(request)
        approval_id = 'python_recipe_' + _digest({'fingerprint': fingerprint, 'binding': binding})[:32]
        payload = {
            'approval_id': approval_id, 'approval_fingerprint': fingerprint,
            'approval_scope': binding['approval_scope'], 'python_binding': binding['python_binding'],
            'python_recipe_review': binding, 'reviewer': 'python_recipe_task_policy',
            'reason': 'authorized offline workspace recipe verification', 'status': 'approved'}
        existing = await session.scalar(select(RunApprovalRow).where(
            RunApprovalRow.tenant_id == row.tenant_id, RunApprovalRow.run_id == row.id,
            RunApprovalRow.approval_id == approval_id).with_for_update())
        if existing is not None:
            if ((existing.tenant_id, existing.run_id, existing.approval_id, existing.action, existing.status)
                    != (row.tenant_id, row.id, approval_id, 'capability_tool.auto_review', 'approved')
                    or existing.payload != payload):
                raise RunConflict('python_recipe_review_audit_conflict')
            return approval_id
        session.add(RunApprovalRow(id=uuid4(), tenant_id=row.tenant_id, run_id=row.id,
            approval_id=approval_id, action='capability_tool.auto_review', status='approved', payload=payload))
        await session.flush()
        return approval_id

    async def reevaluate_pending(self, limit: int) -> int:
        """Same-run pending CAS. This never acquires a worker or executes a tool."""
        repo = self.policy._run_repository
        if not isinstance(repo, RunRepository) or limit <= 0:
            return 0
        async with repo._session_factory() as session:
            rows = (await session.scalars(select(RunRow).where(
                RunRow.status == RunStatus.WAITING_APPROVAL.value,
                RunRow.routing_decision['approval_kind'].astext == 'capability_tool',
                RunRow.routing_decision['approval_scope'].astext.contains(
                    ':resource=workspace/current/project-verification/python/', autoescape=True),
            ).order_by(RunRow.created_at, RunRow.id).limit(limit))).all()
            candidates = [(r.tenant_id, r.id, r.version) for r in rows]
        count = 0
        for tenant, run, version in candidates:
            try:
                count += int(await self._pending(_database_uuid(tenant), _database_uuid(run), version))
            except Exception:  # noqa: BLE001, S112 - failed rechecks retain pending; no raw error export.
                continue
        return count

    async def _pending(self, tenant: UUID, run: UUID, version: int) -> bool:
        repo = self.policy._run_repository
        assert isinstance(repo, RunRepository)
        # Read the durable waiting receipt, without pretending a live worker exists.
        async with repo._session_factory() as session:
            row = await session.scalar(repo._run_select(tenant, run))
            if row is None or row.version != version or row.status != RunStatus.WAITING_APPROVAL.value:
                return False
            route = row.routing_decision or {}
            approval_id = route.get('approval_id')
            checkpoint = await repo.latest_checkpoint(session, tenant_id=tenant, run_id=run)
            from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval

            if (not isinstance(approval_id, str) or not checkpoint_waits_for_approval(checkpoint, approval_id)
                    or cast(RuntimeCheckpoint, checkpoint).runtime_type != 'autogen'):
                return False
            assert checkpoint is not None
            tools = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state['tool_ledger'])
            receipt = tools[-1]
            models = checkpoint.state.get('model_ledger')
            if (not isinstance(models, tuple)
                    or any(not isinstance(m, Mapping) or m.get('status') not in {'succeeded', 'failed'} for m in models)
                    or any(t.get('status') != 'succeeded' for t in tools[:-1])
                    or receipt['name'] != 'project.verify' or receipt['arguments_hash'] != _digest({})):
                return False
            binding = _python_project_approval_binding(row, route.get('approval_scope'))
            if binding is None:
                return False
            request = CapabilityRequest(tenant_id=tenant, run_id=run, user_id=_database_uuid(cast(UUID, row.actor_id)),
                agent_id=cast(str, receipt['actor']), capability='command', operation='run',
                resource='workspace/current/project-verification/python/' + cast(str, binding['recipe_sha256']),
                arguments=binding, idempotency_key=cast(str, receipt['idempotency_key']))
            expected = (route.get('approval_scope'), route.get('approval_fingerprint'), checkpoint.state_sha256)
            role = Role(cast(str, row.actor_role))
        source = await self._source(request)
        async with repo._session_factory() as session, session.begin():
            row = await session.scalar(repo._run_select(tenant, run).with_for_update())
            if (row is None or row.status != RunStatus.WAITING_APPROVAL.value or row.version != version
                    or any(v is not None for v in (row.worker_id, row.worker_lease_token,
                                                   row.worker_lease_expires_at, row.worker_heartbeat_at))):
                return False
            route = dict(row.routing_decision or {})
            if (route.get('approval_id') != approval_id or route.get('approval_kind') != 'capability_tool'
                    or (route.get('approval_scope'), route.get('approval_fingerprint')) != expected[:2]
                    or fingerprint_capability_request(request) != expected[1]):
                return False
            # Never create, reopen or refill a pool for a post-model pending action.
            profile = route.get('approval_review_profile')
            pool = await session.scalar(select(RunAdmissionPoolRow).where(
                RunAdmissionPoolRow.tenant_id == tenant, RunAdmissionPoolRow.run_id == run).with_for_update())
            if (not isinstance(profile, Mapping) or pool is None or pool.closed is not False
                    or (pool.tenant_id, pool.run_id, pool.actor_id, pool.actor_role, pool.config_sha256)
                    != (tenant, run, row.actor_id, row.actor_role, profile.get('config_sha256'))
                    or pool.original_deadline <= datetime.now(UTC)
                    or type(pool.original_token_grant) is not int or type(pool.token_charged) is not int
                    or not 0 <= pool.token_charged < pool.original_token_grant <= 10_000_000):
                return False
            unresolved = await session.scalar(select(RunAdmissionAttemptRow.attempt_id).where(
                RunAdmissionAttemptRow.tenant_id == tenant, RunAdmissionAttemptRow.run_id == run,
                RunAdmissionAttemptRow.state.in_(['prepared', 'issued', 'unknown', 'invalid'])).limit(1))
            if unresolved is not None:
                return False
            binding = await self._locked(session, row, request, role)
            if any(binding.get(k) != source[k] for k in ('original_reference', 'observation_sha256')):
                return False
            binding['source_sha256'] = source['source_sha256']
            approval = await session.scalar(select(RunApprovalRow).where(
                RunApprovalRow.tenant_id == tenant, RunApprovalRow.run_id == run,
                RunApprovalRow.approval_id == approval_id).with_for_update())
            if (approval is None or approval.action != 'capability_tool' or approval.status != 'pending'
                    or approval.payload.get('python_binding') != request.arguments
                    or approval.payload.get('approval_scope') != expected[0]
                    or approval.payload.get('approval_fingerprint') != expected[1]):
                return False
            await repo._require_capability_approval_checkpoint(session, row, route, approval_id=approval_id)
            current = await repo.latest_checkpoint(session, tenant_id=tenant, run_id=run)
            if current is None or current.state_sha256 != expected[2]:
                return False
            audit_id = await self._audit(session, row, request, binding)
            approval.status = 'approved'
            approval.payload = {**approval.payload, 'status': 'approved', 'automatic_review_id': audit_id,
                'public_resolution': {'kind': 'approval.resolved', 'run_id': str(run),
                    'approval_id': approval_id, 'sequence': version + 1,
                    'payload': {'reviewer': 'python_recipe_task_policy'}}}
            row.routing_decision = {k: v for k, v in route.items() if k not in {
                'approval_kind', 'approval_id', 'approval_fingerprint', 'approval_scope',
                'approval_checkpoint_required', 'approval_checkpoint_worker_token'}}
            row.status, row.version = RunStatus.QUEUED.value, version + 1
            from uuid import uuid4

            session.add(RunOutboxRow(id=uuid4(), tenant_id=tenant, run_id=run,
                task_name='agent_hub.runs.execute', idempotency_key=f'{tenant}:{run}:capability-approval:{version}',
                payload={'run_id': str(run)}))
            await session.flush()
        return True
