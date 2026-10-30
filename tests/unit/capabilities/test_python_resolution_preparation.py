"""Concrete action authority must precede Root calls; no descriptor grants."""

import importlib

import pytest

from tests.unit.skills import test_python_registry_read as registry
from tests.unit.skills.test_python_resolution_store import claim

offline_network = registry.offline_network


def api():
    return importlib.import_module("agent_hub.capabilities.python_resolution_preparation")


def test_resolution_action_is_specific_and_not_install_or_generic_command():
    p = api()
    original = claim()
    request = p.resolution_action(
        original, (registry.api()[0].DependencyNeed("demo>=1", "a" * 64),), registry.environment()
    )
    assert request.resource.startswith("workspace/current/project-resolution/python/")
    assert request.arguments["purpose"] == "python_dependency_resolution"
    assert request.arguments["install_allowed"] is False
    assert request.arguments["registry"] == "https://pypi.org"
    assert request.arguments["requirements"] == ["demo>=1"]


async def test_no_bound_worker_means_no_root_call():
    p = api()
    calls = []

    async def current():
        calls.append("read")

    async def transport(request):
        calls.append("root")
        raise AssertionError("must not dispatch")

    preparation = p.PythonResolutionPreparation(current=current, approved=None, transport=transport)
    with pytest.raises(ValueError):
        await preparation.resolve(
            needs=(registry.api()[0].DependencyNeed("demo", "a" * 64),),
            environment=registry.environment(),
            planning=claim().planning,
        )
    assert "root" not in calls


@pytest.fixture
def live():
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from agent_hub.auth.models import Role
    from agent_hub.capabilities.python_metadata_preparation import metadata_scope
    from agent_hub.domain.runs import RunStatus, TaskMode
    from agent_hub.runs.repository import RunRecord
    from agent_hub.runtime.contracts import TaskContext

    original = claim()
    route = {
        "generated_project_contract_version": 1,
        "python_dependency_preflight_version": 1,
        "python_dependency_preflight_submission_version": 1,
        "project_id": "project",
        "workspace_session_id": "session",
        "sandbox_profile": "workspace_write",
        "execution_backend": "systemd",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
        "runtime_timeout_seconds": 30,
    }
    row = RunRecord(
        id=original.planning.scope.run_id,
        tenant_id=original.planning.scope.tenant_id,
        actor_id=original.planning.scope.user_id,
        request="create a Python tool that needs demo",
        mode=TaskMode.DIRECT,
        status=RunStatus.RUNNING,
        version=1,
        created_at=datetime.now(UTC),
        routing_decision=route,
        actor_role=Role.OPERATOR,
        worker_id="worker",
        worker_lease_token=original.lease_token,
        worker_lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    context = TaskContext(
        run_id=row.id,
        tenant_id=row.tenant_id,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
        execution_token=row.worker_lease_token,
        mode=row.mode,
        request=row.request,
        routing_decision=route,
        timeout_seconds=30,
    )
    planning = original.planning.model_copy(
        update={"scope": metadata_scope(row, actor="implementer", live=True)}
    )
    return SimpleNamespace(row=row, context=context, planning=planning, events=[], revoked=False)


@pytest.mark.parametrize(
    "change",
    ["none", "token", "actor", "tenant", "role", "grant", "request", "permissions", "lease_expiry"],
)
async def test_fresh_original_worker_and_concrete_grant_fences(live, change):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from agent_hub.auth.models import Role
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    p = api()
    c = live
    reads = 0
    grants = 0

    async def current():
        nonlocal reads
        reads += 1
        row = c.row
        if change != "none" and reads >= 2:
            if change == "token":
                return replace(row, worker_lease_token=UUID(int=999))
            if change == "actor":
                return replace(row, actor_id=UUID(int=999))
            if change == "tenant":
                return replace(row, tenant_id=UUID(int=999))
            if change == "role":
                return replace(row, actor_role=Role.ADMIN)
            if change == "request":
                return replace(row, request="different request")
            if change == "permissions":
                return replace(
                    row,
                    routing_decision={
                        **row.routing_decision,
                        "requested_permissions": ["workspace.read"],
                    },
                )
            if change == "lease_expiry":
                return replace(
                    row, worker_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
        return row

    async def approved(request):
        nonlocal grants
        grants += 1
        assert request.resource.startswith(p.PREFIX)
        return not (change == "grant" and grants >= 2)

    async def transport(request):
        c.events.append(request)
        assert request.claim.lease_token == c.context.execution_token
        return ResolutionStatus(state="unknown", key=request.key)

    preparation = p.PythonResolutionPreparation(
        current=current, approved=approved, transport=transport
    )
    with bind_execution_authority(c.context, worker_id="worker", actor="implementer"):
        if change == "none":
            result = await preparation.resolve(
                needs=(registry.api()[0].DependencyNeed("demo", "a" * 64),),
                environment=registry.environment(),
                planning=c.planning,
            )
            assert result.state == "unknown" and len(c.events) == 1
        else:
            with pytest.raises((ValueError, RuntimeError)):
                await preparation.resolve(
                    needs=(registry.api()[0].DependencyNeed("demo", "a" * 64),),
                    environment=registry.environment(),
                    planning=c.planning,
                )
            assert c.events == []


def test_public_approval_arguments_never_expose_private_worker_lease():
    p = api()
    original = claim()
    request = p.resolution_action(
        original, (registry.api()[0].DependencyNeed("demo", "a" * 64),), registry.environment()
    )
    public = request.model_dump_json()
    assert str(original.lease_token) not in public
    assert "lease_token" not in request.arguments
    import hashlib

    assert (
        request.arguments["lease_sha256"]
        == hashlib.sha256(str(original.lease_token).encode("ascii")).hexdigest()
    )
