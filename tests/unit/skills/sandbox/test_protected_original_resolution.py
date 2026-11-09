"""Root must rederive the registry query from the actual protected manifest."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.capabilities.python_original_metadata import standard_python_intent
from agent_hub.capabilities.python_original_private_recipe import original_dependency_needs
from agent_hub.skills.sandbox import python_dependency_inspection
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataScope
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionError, digest
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ProtectedOriginalResolutionPlanning,
    ProtectedOriginalResolutionSource,
    ResolutionClaim,
    ResolutionRequest,
)
from agent_hub.skills.sandbox.python_resolution_worker import _resolution_controls
from tests.unit.capabilities.test_standard_python_original_intent import FILES, MANIFEST, PROTECTED
from tests.unit.capabilities.test_standard_python_private_recipe import ENVIRONMENT
from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload


def _original(tmp_path: Path) -> tuple[StoredProjectBaseline, ResolutionRequest]:
    (tmp_path / "pyproject.toml").write_bytes(MANIFEST)
    files = {name: hashlib.sha256(b"f\0" + (MANIFEST if name == "pyproject.toml" else b"pass\n")).hexdigest()
             for name in FILES}
    invocation = ProjectInvocation(execution_id="original_dependency", tenant_id=UUID(int=1), run_id=UUID(int=2),
        project_id="project", session_id="session", sandbox_profile="restricted", timeout_seconds=30)
    reference = ProjectBaselineReference("1" * 64, digest(files), len(files))
    contract = build_project_check_contract(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, project_id=invocation.project_id,
        session_id=invocation.session_id, input_source_sha256=reference.input_source_sha256,
        editable_paths=("src/expense_cli/cli.py",), source_files=files,
    )
    original = StoredProjectBaseline(reference, contract, tmp_path, files)
    intent = standard_python_intent(MANIFEST, paths=FILES, protected=PROTECTED)
    assert intent is not None
    planning = ProtectedOriginalResolutionPlanning(
        invocation=invocation, scope=PythonMetadataScope(
            tenant_id=invocation.tenant_id, run_id=invocation.run_id, user_id=UUID(int=3), project_id="project",
            session_id="session", actor="main_agent", actor_role="operator", request_sha256="2" * 64,
            permissions_sha256="3" * 64,
        ), draft=ProtectedOriginalResolutionSource.model_validate({
            "original_reference": reference.to_payload(), "protected_sha256": contract.protected_sha256,
            "manifest_sha256": intent.manifest_sha256,
        }, strict=True), expected_root_config_sha256="4" * 64, expected_catalog_sha256="5" * 64,
    )
    now = datetime.now(UTC)
    claim = ResolutionClaim(planning=planning, worker_id="main-worker", lease_token=UUID(int=4),
        lease_expires_at=now + timedelta(seconds=20), original_deadline=now + timedelta(seconds=30),
        monotonic_deadline=time.monotonic() + 30)
    return original, ResolutionRequest(
        action="resolve_python_inputs", claim=claim, environment=ENVIRONMENT,
        requirements=tuple((row.requirement, row.evidence_sha256) for row in original_dependency_needs(intent, ENVIRONMENT)),
    )


def test_protected_source_scope_does_not_allow_rebinding_on_lease_change(tmp_path: Path) -> None:
    _, request = _original(tmp_path)
    roundtrip = ResolutionRequest.model_validate_json(request.model_dump_json(), strict=True)
    assert roundtrip == request and roundtrip.key == request.key
    claim = request.claim
    assert isinstance(claim.planning, ProtectedOriginalResolutionPlanning)
    next_worker = claim.model_copy(update={"worker_id": "new-worker", "lease_token": UUID(int=42)})
    next_worker.require_data_origin(claim)
    with pytest.raises(ResolutionError, match="resolution_scope_changed"):
        next_worker.require_continuity(claim)
    changed = claim.planning.draft.model_copy(update={"protected_sha256": "9" * 64})
    moved = next_worker.model_copy(update={"planning": claim.planning.model_copy(update={"draft": changed})})
    with pytest.raises(ResolutionError, match="resolution_scope_changed"):
        moved.require_data_origin(claim)


def test_root_checks_manifest_requirements_and_target_without_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, request = _original(tmp_path)
    monkeypatch.setattr(python_dependency_inspection, "_target_environment", lambda root: ENVIRONMENT)
    root = RootConfig.from_payload(configuration_payload())
    policy = BrokerPolicy()
    deadline = time.monotonic() + 15
    _resolution_controls(request, policy, root, deadline, original)
    wrong = request.model_copy(update={"requirements": request.requirements[1:]})
    with pytest.raises(ResolutionError, match="resolution_requirements_changed"):
        _resolution_controls(wrong, policy, root, deadline, original)
    changed_env = request.model_copy(update={"environment": {**ENVIRONMENT, "platform_release": "wrong"}})
    with pytest.raises(ResolutionError, match="resolution_environment_changed"):
        _resolution_controls(changed_env, policy, root, deadline, original)
    (tmp_path / "pyproject.toml").write_bytes(MANIFEST.replace(b"click>=8.1,<9", b"click>=9"))
    with pytest.raises(ValueError, match="python_original_metadata_invalid"):
        _resolution_controls(request, policy, root, deadline, original)
