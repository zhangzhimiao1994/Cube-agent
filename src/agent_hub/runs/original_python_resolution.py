"""The missing-package branch for a captured standard Python project."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agent_hub.capabilities.approvals import (
    capability_approval_scope,
    fingerprint_capability_request,
)
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.python_input_preparation import (
    DynamicResolutionJournal,
    InspectOnlyError,
)
from agent_hub.capabilities.python_original_metadata import standard_python_intent
from agent_hub.capabilities.python_original_private_recipe import (
    derive_private_original_recipe,
    original_dependency_needs,
)
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileSandbox
from agent_hub.skills.sandbox.python_registry_protocol import need, target_environment
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ProtectedOriginalResolutionPlanning,
    ProtectedOriginalResolutionSource,
    decode_descriptor,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.project_baselines import StoredProjectBaseline
    from agent_hub.capabilities.types import CapabilityRequest
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.runs.repository import RunRecord, RunRepository
    from agent_hub.runtime.artifacts import ArtifactRepository
    from agent_hub.runtime.contracts import JsonValue, TaskContext


def _approval_window(record: RunRecord) -> int:
    route = record.routing_decision or {}
    value = route.get("runtime_timeout_seconds", 300)
    need(type(value) in {int, float} and math.isfinite(cast(float, value))
         and cast(float, value) >= 1, "resolution_deadline")
    initial = min(3600, math.floor(cast(float, value)))
    if route.get("runtime_timeout_source") != "project_scale_soft_budget":
        return initial
    absolute = route.get("runtime_timeout_absolute_seconds")
    if absolute is None:
        return initial
    need(type(absolute) in {int, float} and cast(float, value) <= cast(float, absolute) <= 3600,
         "resolution_deadline")
    # Waiting for a person is bounded by the existing project envelope. Each
    # admitted worker and broker call still uses its active monotonic budget.
    return math.floor(cast(float, absolute))


async def resolve_missing_original_python_inputs(
    *, context: TaskContext, stored: StoredProjectBaseline, preparation: Mapping[str, JsonValue],
    invocation: ProjectInvocation, repository: RunRepository, authorizer: HarnessToolGateway,
    artifacts: ArtifactRepository, deadline: float,
) -> None:
    """Reuse the Root resolver, its exact effect approvals, and inspect-only recovery."""
    from agent_hub.capabilities.python_resolution_authorization import (
        bind_resolution_original_deadline,
        original_resolution_scope,
        resolution_policy_scope,
    )
    from agent_hub.runs.project_inputs import OriginalProjectApprovalPending

    manifest = await asyncio.to_thread(
        _read_no_links, stored.snapshot, ("pyproject.toml",), max_bytes=256 * 1024,
    )
    intent = standard_python_intent(manifest, paths=stored.source_files.keys(),
        protected=stored.contract.protected_files.keys())
    need(intent is not None and stored.generated_metadata is None, "resolution_source_changed")
    assert intent is not None
    source = ProtectedOriginalResolutionSource.model_validate({
        "original_reference": stored.reference.to_payload(),
        "protected_sha256": stored.contract.protected_sha256,
        "manifest_sha256": intent.manifest_sha256,
    }, strict=True)

    async def current() -> RunRecord:
        return await repository.get(context.tenant_id, context.run_id)

    record = await current()
    scope = original_resolution_scope(record, actor="main_agent", source=source, live=True)
    need((scope.user_id, record.worker_lease_token) == (context.actor_id, context.execution_token),
         "resolution_scope_changed")

    def probe() -> ProjectInvocation:
        remaining = deadline - asyncio.get_running_loop().time()
        need(remaining >= 1, "resolution_deadline")
        return ProjectInvocation.model_validate({
            **invocation.model_dump(), "execution_id": "original_resolution_" + uuid4().hex,
            "timeout_seconds": int(remaining), "sandbox_profile": "restricted", "install_allowed": False,
        }, strict=True)

    profiles = PythonProjectProfileSandbox()
    base = await profiles.inspect(probe())
    need(base.ok and base.profile is not None and base.profile.root_config_sha256 is not None,
         "resolution_data_unavailable")
    assert base.profile is not None and base.profile.root_config_sha256 is not None
    stable_scope: dict[str, object] = {
        **scope.model_dump(mode="json"), "root_sha256": base.profile.root_config_sha256,
        "catalog_sha256": base.profile.catalog_sha256, "protected_original": source.model_dump(mode="json"),
    }
    cutoff = record.created_at + timedelta(seconds=_approval_window(record))
    journal = DynamicResolutionJournal(artifacts)
    rows = await journal.read_artifacts(context.tenant_id, context.run_id,
        artifact_type="python_resolution_checkpoint", producer="python_resolution")
    if rows:
        earlier = rows[0].content.get("scope")
        need(isinstance(earlier, Mapping)
             and {key: value for key, value in earlier.items() if key != "original_deadline"} == stable_scope,
             "resolution_checkpoint_scope_changed")
        assert isinstance(earlier, Mapping)
        raw = earlier.get("original_deadline")
        need(type(raw) is str, "resolution_checkpoint_scope_changed")
        recovered_cutoff = datetime.fromisoformat(cast(str, raw))
        need(recovered_cutoff.tzinfo is not None and record.created_at < recovered_cutoff <= cutoff,
             "resolution_checkpoint_scope_changed")
        cutoff = recovered_cutoff
    need(cutoff > datetime.now(UTC), "resolution_deadline")
    journal_scope = {**stable_scope, "original_deadline": cutoff.isoformat()}
    checkpoint = await journal.load(journal_scope)
    if checkpoint is not None:
        environment = target_environment(cast(Mapping[str, str], checkpoint["environment"]))
    else:
        measured = await profiles.inspect(probe(), expected_source_sha256=source.source_sha256)
        need(measured.ok and measured.profile is not None
             and measured.profile.target_environment is not None
             and measured.profile.catalog_sha256 == base.profile.catalog_sha256
             and measured.profile.root_config_sha256 == base.profile.root_config_sha256,
             "resolution_data_unavailable")
        assert measured.profile is not None and measured.profile.target_environment is not None
        environment = target_environment(measured.profile.target_environment)
    needs = original_dependency_needs(intent, environment)
    if checkpoint is not None:
        need(checkpoint["needs"] == [
            {"requirement": n.requirement, "evidence_sha256": n.evidence_sha256} for n in needs
        ], "resolution_checkpoint_scope_changed")
    planning = ProtectedOriginalResolutionPlanning(
        invocation=probe(), scope=scope, draft=source,
        expected_root_config_sha256=base.profile.root_config_sha256,
        expected_catalog_sha256=base.profile.catalog_sha256,
    )

    def require_current(live: RunRecord, plan: ProtectedOriginalResolutionPlanning) -> int:
        need(plan == planning and live.worker_lease_token == context.execution_token
             and original_resolution_scope(live, actor=scope.actor, source=source, live=True) == scope,
             "resolution_scope_changed")
        return _approval_window(live)

    async def save(key: str, pending: str | None) -> None:
        require_current(await current(), planning)
        await journal.save(journal_scope, needs=needs, environment=environment, resolution_key=key,
                           pending_plan_sha256=pending)
        require_current(await current(), planning)

    async def approved(action: CapabilityRequest) -> bool:
        require_current(await current(), planning)
        if await repository.is_capability_approval_approved(context.tenant_id, context.run_id,
                fingerprint_capability_request(action), tool_origin="builtin"):
            require_current(await current(), planning)
            return True
        await save(cast(str, action.arguments["key"]), cast(str | None, action.arguments.get("plan_sha256")))
        assert context.actor_id is not None and context.actor_role is not None
        with resolution_policy_scope(action):
            bind_resolution_original_deadline(stable_scope, cutoff)
            result = await authorizer.authorize_original_python_resolution(action, tenant_id=context.tenant_id,
                run_id=context.run_id, user_id=context.actor_id, role=context.actor_role)
        if (result.status is CapabilityStatus.WAITING_APPROVAL and result.approval_id is not None
                and result.run_id == context.run_id):
            bound_scope = capability_approval_scope(action)
            need(bound_scope is not None, "resolution_scope_changed")
            raise OriginalProjectApprovalPending(result.approval_id, preparation, resolution_scope=bound_scope)
        return result.status is CapabilityStatus.ALLOWED and result.run_id == context.run_id

    client = PythonResolutionPreparation(current=current, approved=approved, original_deadline=cutoff,
                                         protected_original=require_current)
    previous_plan = None if checkpoint is None else cast(str | None, checkpoint["pending_plan_sha256"])
    previous_key = None if checkpoint is None else checkpoint["resolution_key"]
    status = await client.inspect(needs=needs, environment=environment, planning=planning,
                                  expected_plan_sha256=previous_plan)
    need(previous_key is None or status.key == previous_key, "resolution_checkpoint_scope_changed")
    if previous_plan is not None:
        if status.state == "published":
            return
        if status.state != "frozen":
            raise InspectOnlyError(status.diagnostic)
        status = await client.inspect(needs=needs, environment=environment, planning=planning)
    elif status.state == "absent":
        status = await client.resolve(needs=needs, environment=environment, planning=planning)
    if status.state != "resolved" or status.descriptor is None:
        raise InspectOnlyError(status.diagnostic)
    descriptor = decode_descriptor(status.descriptor)
    need(descriptor.scope.source_sha256 == source.source_sha256
         and descriptor.scope.catalog_sha256 == base.profile.catalog_sha256
         and descriptor.scope.actor == scope.actor and descriptor.scope.user_id == scope.user_id
         and descriptor.scope.run_id == scope.run_id, "resolution_scope_changed")
    frozen = await client.freeze(needs=needs, environment=environment, planning=planning)
    if frozen.state != "frozen" or frozen.plan is None:
        raise InspectOnlyError(frozen.diagnostic)
    plan = frozen.plan
    need(previous_plan is None or plan.sha256 == previous_plan, "resolution_plan_changed")
    derive_private_original_recipe(intent, descriptor, expected_scope=plan.claim.resolution_scope(),
        verified_descriptor_sha256=plan.descriptor_sha256)
    # The approval callback saves this same plan again before any publication.
    await save(frozen.key, plan.sha256)
    result = await client.publish(plan, needs=needs, environment=environment, planning=planning)
    if result.state != "published" or result.plan != plan:
        raise InspectOnlyError(result.diagnostic)
