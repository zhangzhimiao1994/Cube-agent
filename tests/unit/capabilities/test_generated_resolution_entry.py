"""Full generated-service entry diagnosis; all external transports are offline."""

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.generated_project_service import (
    PythonMetadataPreparationApprovalRequired,
)
from agent_hub.capabilities.python_input_preparation import dynamic_python_needs
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectRecipeError,
    parse_python_project_declaration,
)
from agent_hub.runs.repository import RunRecord
from agent_hub.skills.sandbox import python_resolution_worker as wire
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionRequest,
    ResolutionStatus,
)
from tests.unit.capabilities.dynamic_resolution_typed_fixture import present
from tests.unit.capabilities.test_dynamic_python_resolution_flow import generated_dynamic_case
from tests.unit.capabilities.test_python_catalog_gateway import configured_profile
from tests.unit.capabilities.test_python_resolution_preparation import generated_planning
from tests.unit.capabilities.test_scoped_read import project_path


async def test_main_agent_invalid_lock_three_unavailable_needs_reaches_specific_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, context, _ = generated_dynamic_case(tmp_path, monkeypatch)
    assert isinstance(repo.row, RunRecord)
    work = service.root / project_path()
    # Reproduce the measured shape (three needs, zero satisfied), without
    # exporting the real user's source or pretending these are its exact bytes.
    manifest = (work / "pyproject.toml").read_bytes().replace(
        b"setuptools==80.9.0", b"setuptools>=999"
    ).replace(b"wheel==0.45.1", b"wheel>=999")
    lock = b"demo-dep==1.0 --hash=sha256:not-valid-digest \n"
    assert len(lock) == 46
    (work / "pyproject.toml").write_bytes(manifest)
    (work / "requirements.lock").write_bytes(lock)
    needs = dynamic_python_needs(manifest)
    assert len(needs) == 3
    catalog = configured_profile(large=False).catalog
    assert sum(
        any(
            wheel.name == canonicalize_name(Requirement(need.requirement).name)
            and Version(wheel.version) in Requirement(need.requirement).specifier
            for wheel in catalog
        )
        for need in needs
    ) == 0
    with pytest.raises(PythonProjectRecipeError, match="python_project_lock_invalid"):
        parse_python_project_declaration(
            manifest, lock, max_control_bytes=256 * 1024, max_requirements=128,
        )

    actions: list[str] = []
    approvals: list[str] = []
    inspected: list[ResolutionRequest] = []

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        actions.append(request.action)
        assert request.action == "inspect_python_resolution", "resolution effects forbidden"
        assert request.claim.lease_token == context.execution_token
        assert request.claim.planning.scope.actor == "main_agent"
        assert request.needs == needs
        inspected.append(request)
        return ResolutionStatus(state="absent", key=request.key)

    async def approved(tenant: UUID, run: UUID, scope: str) -> bool:
        assert (tenant, run) == (context.tenant_id, context.run_id)
        approvals.append(scope)
        return False

    repo.is_capability_approval_scope_approved = approved
    monkeypatch.setattr(wire, "request_resolution", transport)
    with (
        bind_execution_authority(
            context, worker_id=present(repo.row.worker_id), actor="main_agent",
        ),
        pytest.raises(PythonMetadataPreparationApprovalRequired) as failure,
    ):
        await service.prepare(
            context.tenant_id, context.run_id, "main_agent", user_id=present(context.actor_id),
        )
    assert failure.value.approval_request.arguments["purpose"] == "python_dependency_resolution"
    assert failure.value.approval_request.arguments["install_allowed"] is False
    assert actions == ["inspect_python_resolution"]
    assert len(approvals) == 1
    assert repo.updates == 0
    assert "generated_project_preparation" not in present(repo.row.routing_decision)
    journal = present(service._resolution_journal)
    checkpoints = await journal.read_artifacts(
        context.tenant_id, context.run_id,
        artifact_type="python_resolution_checkpoint", producer="python_resolution",
    )
    # The existing approved callback durably registers data before asking for
    # the grant. This is not a Root resolution intent or a captured project.
    assert len(checkpoints) == 1
    assert checkpoints[0].content["resolution_key"] == inspected[0].key
    assert checkpoints[0].content["pending_plan_sha256"] is None
    assert generated_planning(inspected[0].claim).draft.lock_sha256 == hashlib.sha256(lock).hexdigest()
    assert await journal.read_artifacts(
        context.tenant_id, context.run_id,
        artifact_type="python_resolution_data", producer="python_resolution",
    ) == ()
