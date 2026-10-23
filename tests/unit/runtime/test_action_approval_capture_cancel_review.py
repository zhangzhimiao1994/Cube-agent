"""Cancellation must retain the issuer task after nested preparation capture."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import JsonValue, RunEvent, TaskContext
from tests.unit.runtime.test_action_approval_capture import IssuerRepository
from tests.unit.runtime.test_action_direct import Backend
from tests.unit.runtime.test_direct_project_verification import Model, batch, context


async def test_cancel_drains_issuer_after_nested_frame_read(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    store = InMemoryApprovalStore()

    class DelayedIssuer(IssuerRepository):
        async def begin_capability_approval(self, tenant_id: UUID, run_id: UUID, **kwargs: Any) -> None:
            entered.set()
            await release.wait()
            await super().begin_capability_approval(tenant_id, run_id, **kwargs)

    class FrameBackend(Backend):
        async def action_preparation_snapshot(self, current: TaskContext) -> dict[str, JsonValue]:
            # Transport double only; this test checks cancellation, not Root authorization.
            return {'schema_version': 1, 'scope': {}, 'contracts': {}, 'workspace_sha256': 'a' * 64}

    ctx = context()
    ctx = ctx.model_copy(update={'routing_decision': {**ctx.routing_decision,
        'requested_permissions': ('workspace.read', 'workspace.write', 'command.run')}})
    repository = DelayedIssuer(store)
    policy = CapabilityPolicy((CapabilityRule(ctx.tenant_id, Role.OPERATOR, 'main_agent',
        'file', 'write', 'workspace/current', PolicyEffect.REQUIRE_APPROVAL),))
    gateway = CapabilityGateway(policy, ApprovalService(store), repository)
    artifacts = InMemoryArtifactRepository()
    backend = FrameBackend(tmp_path, ctx, artifacts)
    runtime = ActionApprovedDirectRuntime(Model(batch({'README.md': 'one'})), logical_model='main',
        capability_gateway=backend, artifact_repository=artifacts,
        harness_tool_gateway=HarnessToolGateway(backend, policy_gateway=gateway, require_actor_identity=True))

    async def consume() -> list[RunEvent]:
        return [event async for event in runtime.run(ctx)]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        # The read is done; only the approval issuer remains blocked. Cancellation
        # must cancel it without waiting for its external DB gate to be released.
        await asyncio.wait_for(runtime.cancel(), timeout=0.5)
        await asyncio.gather(consumer, return_exceptions=True)
        assert repository.begins == 0
        assert await store.list_pending() == ()
    finally:
        release.set()
        await asyncio.gather(consumer, return_exceptions=True)
