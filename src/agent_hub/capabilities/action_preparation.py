"""Read Root-validated preparation for private action continuation, never a tool grant."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_hub.capabilities.execution_authority import get_verification_authority
from agent_hub.capabilities.generated_project_service import generated_selected
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_manifest

if TYPE_CHECKING:
    from agent_hub.capabilities.project_verification import ProjectVerificationService
    from agent_hub.runs.project_verification_receipts import VerificationAuthority
    from agent_hub.runs.repository import RunRecord

CONTRACT_ROUTE_KEYS = (
    'project_input_contract_version', 'project_input', 'project_input_preparation',
    'generated_project_contract_version', 'generated_project_preparation',
    'python_project_history', 'python_input_supply', 'toolchain_supply', 'go_project_recipe',
)


def _authority(record: RunRecord, context: TaskContext) -> VerificationAuthority:
    if context.actor_id is None or context.actor_role is None:
        raise ValueError
    authority = get_verification_authority(tenant_id=context.tenant_id, run_id=context.run_id,
        user_id=context.actor_id, role=context.actor_role, current_record=record)
    expiry = record.worker_lease_expires_at
    if (record.worker_id != authority.worker_id or record.worker_lease_token != authority.execution_token
            or expiry is None or expiry.tzinfo is None or expiry <= datetime.now(UTC)):
        raise ValueError
    return authority


def _contracts(record: RunRecord) -> Mapping[str, JsonValue]:
    route = record.routing_decision or {}
    return _freeze_object({key: route[key] for key in CONTRACT_ROUTE_KEYS if key in route},
                          name='action preparation contracts')


async def capture_action_preparation(
    verifier: ProjectVerificationService, context: TaskContext,
) -> Mapping[str, JsonValue]:
    """Internal worker use only; read, validate and re-read without prepare/supply/run.

    The stable worker binding is mandatory. A root-readable valid contract is
    continuity metadata, not approval or successful verification evidence.
    """
    from agent_hub.capabilities.project_verification import ProjectVerificationError

    try:
        getter = getattr(verifier.repository, 'get', None)
        if not callable(getter):
            raise TypeError
        record = await getter(context.tenant_id, context.run_id)
        authority = _authority(record, context)
        contracts = _contracts(record)
        project, session = authorized_workspace_scope(record, tenant_id=context.tenant_id, run_id=context.run_id)
        async def workspace_digest() -> str:
            files = await asyncio.to_thread(ProjectWorkspaceStore(verifier.root).list_files,
                context.tenant_id, project, session)
            return hashlib.sha256(json.dumps([(item.path, item.sha256, item.size_bytes) for item in files],
                ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
        source = await workspace_digest()
        if 'toolchain_supply' in contracts or 'go_project_recipe' in contracts:
            raise ValueError
        if generated_selected(record):
            generated = await verifier.generated.load(context.tenant_id, context.run_id, 'main_agent', readonly=True)
            if generated is not None:
                generated.envelope.require_current(await project_source_manifest(verifier.root, generated.invocation))
                if generated.state.get('phase') == 'python_ready':
                    await verifier._python_generated._inputs(generated)
            elif 'generated_project_preparation' in contracts:
                raise ValueError
        elif 'project_input_contract_version' in contracts or 'project_input' in contracts:
            python = await verifier._python.select(context.tenant_id, context.run_id, 'main_agent')
            if python is not None:
                python.stored.contract.require_current(await project_source_manifest(verifier.root, python.invocation))
            else:
                invocation = ProjectInvocation(execution_id='action_preparation_read',
                    tenant_id=context.tenant_id, run_id=context.run_id, project_id=project,
                    session_id=session, sandbox_profile='restricted', install_allowed=False, timeout_seconds=15)
                original = await verifier._original_scope(invocation)
                if original is None:
                    raise ValueError
                original.stored.contract.require_current(await project_source_manifest(verifier.root, invocation))
        elif contracts:
            raise ValueError
        current = await getter(context.tenant_id, context.run_id)
        if (_authority(current, context) != authority or _contracts(current) != contracts
                or await workspace_digest() != source):
            raise ValueError
        return _freeze_object({'schema_version':1, 'scope':authority.stable_scope(),
                               'contracts':contracts, 'workspace_sha256':source},
                              name='action preparation snapshot')
    except ProjectVerificationError:
        raise
    except Exception:  # noqa: BLE001 - current scope and Root metadata never become public diagnostics.
        raise ProjectVerificationError('action preparation scope is unavailable or changed') from None
