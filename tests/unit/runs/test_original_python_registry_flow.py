"""Exercise the actual worker flow and journal with controlled Root transport."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import fingerprint_capability_request
from agent_hub.capabilities.defaults import _project_resolution_preparation_effect
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.python_input_preparation import InspectOnlyError
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs import original_python_resolution
from agent_hub.runs.project_inputs import OriginalProjectApprovalPending
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.skills.sandbox import python_resolution_worker
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionDescriptor
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
)
from agent_hub.skills.sandbox.python_resolution_store import freeze_plan
from tests.unit.capabilities.test_protected_original_resolution_authorization import (
    original as running_record,
)
from tests.unit.capabilities.test_standard_python_private_recipe import ENVIRONMENT, _wheel
from tests.unit.skills.sandbox.test_protected_original_resolution import _original


@pytest.mark.parametrize("publication_lost", [False, True])
async def test_registry_requires_exact_approval_and_never_replays_unknown_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication_lost: bool,
) -> None:
    baseline, request = _original(tmp_path)
    captured: dict[str, JsonValue] = {
        "schema_version": 1, "phase": "captured",
        "reference": cast(dict[str, JsonValue], baseline.reference.to_payload()),
    }
    record = running_record()
    route = dict(record.routing_decision or {})
    route.update(project_id="project", workspace_session_id="session", project_input_preparation=captured,
                 project_input={"schema_version": 1, "kind": "managed_workspace",
                                "editable_paths": ["src/expense_cli/cli.py"]},
                 runtime_timeout_source="project_scale_soft_budget", runtime_timeout_seconds=30,
                 runtime_timeout_absolute_seconds=180.25)
    # Earlier queue time has exhausted the initial active budget in wall time,
    # but does not consume the live worker's 25-second monotonic budget.
    record = replace(record, created_at=datetime.now(UTC) - timedelta(seconds=45))
    cutoff = record.created_at + timedelta(seconds=180)
    record = replace(record, routing_decision=route)
    grants: set[str] = set()
    submissions: list[CapabilityRequest] = []

    async def current(_tenant: UUID, _run: UUID) -> RunRecord:
        return record

    async def approved(_tenant: UUID, _run: UUID, fingerprint: str, *, tool_origin: str) -> bool:
        assert tool_origin == "builtin"
        return fingerprint in grants

    async def authorize(action: CapabilityRequest, **identity: object) -> CapabilityResult:
        assert identity == {"tenant_id": record.tenant_id, "run_id": record.id,
                            "user_id": record.actor_id, "role": Role.OPERATOR}
        assert await _project_resolution_preparation_effect(repository, action, role=Role.OPERATOR) is (
            PolicyEffect.REQUIRE_APPROVAL)
        submissions.append(action)
        return CapabilityResult(CapabilityStatus.WAITING_APPROVAL, record.id, approval_id="approval_registry")

    repository = cast(RunRepository, SimpleNamespace(get=current, is_capability_approval_approved=approved))
    authorizer = cast(HarnessToolGateway, SimpleNamespace(authorize_original_python_resolution=authorize))
    profiles: list[str | None] = []

    class Profiles:
        async def inspect(self, invocation: object, *, expected_source_sha256: str | None = None) -> object:
            profiles.append(expected_source_sha256)
            return SimpleNamespace(ok=True, profile=SimpleNamespace(
                root_config_sha256="4" * 64, catalog_sha256="5" * 64,
                target_environment=None if expected_source_sha256 is None else ENVIRONMENT,
            ))

    actions: list[str] = []
    plan: ResolutionPlan | None = None
    original_request: ResolutionRequest | None = None
    descriptor: ResolutionDescriptor | None = None
    publication = "frozen"

    async def transport(call: ResolutionRequest) -> ResolutionStatus:
        nonlocal plan, original_request, descriptor, publication
        assert call.claim.original_deadline == cutoff
        assert 0 < call.claim.deadline - time.monotonic() <= 25
        actions.append(call.action)
        if call.action == "inspect_python_resolution":
            if call.expected_plan_sha256 is not None:
                assert plan is not None and call.expected_plan_sha256 == plan.sha256
                if publication == "unknown":
                    return ResolutionStatus(state="unknown", key=call.key)
                if publication == "published":
                    return ResolutionStatus(state="published", key=call.key)
                return ResolutionStatus(state="frozen", key=call.key)
            if descriptor is None:
                return ResolutionStatus(state="absent", key=call.key)
            return ResolutionStatus(state="resolved", key=call.key, descriptor=descriptor.to_payload())
        if call.action == "resolve_python_inputs":
            assert descriptor is None
            original_request = call
            descriptor = ResolutionDescriptor(call.claim.resolution_scope(), call.needs,
                tuple(sorted(call.environment.items())), (
                    _wheel("click", "8.1.8"), _wheel("setuptools", "80.9.0"), _wheel("wheel", "0.45.1"),
                ), ())
            return ResolutionStatus(state="resolved", key=call.key, descriptor=descriptor.to_payload())
        assert descriptor is not None and original_request is not None
        if call.action == "freeze_python_resolution":
            frozen = freeze_plan(descriptor, original=original_request.claim, current=call.claim,
                                 overlay_sha256="9" * 64)
            if plan is not None:
                assert plan.sha256 == frozen.sha256
            else:
                plan = frozen
            return ResolutionStatus(state="frozen", key=call.key, plan=plan)
        assert call.action == "publish_python_resolution" and plan is not None and publication == "frozen"
        assert call.expected_plan_sha256 == plan.sha256
        if publication_lost:
            publication = "unknown"
            raise ConnectionResetError
        publication = "published"
        return ResolutionStatus(state="published", key=call.key, plan=plan)

    monkeypatch.setattr(original_python_resolution, "PythonProjectProfileSandbox", Profiles)
    monkeypatch.setattr(python_resolution_worker, "request_resolution", transport)
    artifacts = InMemoryArtifactRepository()

    async def run() -> None:
        context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, request=record.request,
            mode=TaskMode.DISCUSS, actor_id=record.actor_id, actor_role=record.actor_role,
            execution_token=record.worker_lease_token, routing_decision=cast(dict[str, JsonValue], route),
            timeout_seconds=25.0)
        assert record.worker_id is not None
        with bind_execution_authority(context, worker_id=record.worker_id):
            await original_python_resolution.resolve_missing_original_python_inputs(
                context=context, stored=baseline, preparation=captured, invocation=request.invocation,
                repository=repository, authorizer=authorizer, artifacts=artifacts,
                deadline=asyncio.get_running_loop().time() + 25,
            )

    with pytest.raises(OriginalProjectApprovalPending):
        await run()
    assert actions == ["inspect_python_resolution"]
    assert len(submissions) == 1 and submissions[0].arguments["install_allowed"] is False
    assert submissions[0].arguments["original_deadline"] == cutoff.isoformat()
    grants.add(fingerprint_capability_request(submissions[0]))
    # A later worker must retain the sealed cutoff even if policy now allows longer.
    route = {**route, "runtime_timeout_absolute_seconds": 240}
    record = replace(record, worker_id="new-worker", worker_lease_token=UUID(int=43), routing_decision=route)
    with pytest.raises(OriginalProjectApprovalPending):
        await run()
    assert "publish_python_resolution" not in actions
    assert len(submissions) == 2 and submissions[1].resource != submissions[0].resource
    assert submissions[1].arguments["original_deadline"] == cutoff.isoformat()
    grants.add(fingerprint_capability_request(submissions[1]))
    record = replace(record, worker_id="publication-worker", worker_lease_token=UUID(int=44))
    if publication_lost:
        with pytest.raises(ConnectionResetError):
            await run()
        effects = tuple(action for action in actions if action != "inspect_python_resolution")
        with pytest.raises(InspectOnlyError):
            await run()
        assert tuple(action for action in actions if action != "inspect_python_resolution") == effects
    else:
        await run()
    assert actions.count("resolve_python_inputs") == actions.count("publish_python_resolution") == 1
    assert len(submissions) == 2
    assert profiles.count(baseline.reference.input_source_sha256) == 1
