"""Approval and crash recovery with fake v4 measurement, never environment execution."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runs.repository import RunRepository
from tests.unit.capabilities.test_python_project_input_store import python_controls
from tests.unit.capabilities.test_python_project_service import PythonExecutor
from tests.unit.runs.test_original_project_preparation import setup


class InstallPolicy:
    def __init__(self, owner: Authorizer) -> None:
        self.owner = owner

    async def invoke(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        return await self.owner.install(request, role=role)


class Authorizer:
    def __init__(self, verifier: Any, repository: Any, root: Path, invocation: Any) -> None:
        self.verifier, self.repository, self.root, self.invocation = (
            verifier,
            repository,
            root,
            invocation,
        )
        self.allowed = False
        self.scopes: set[str] = set()
        self.calls = 0
        self._policy_gateway = InstallPolicy(self)

    async def install(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
        tenant_id = request.tenant_id
        self.calls += 1
        state = self.repository.row.routing_decision["project_input_preparation"]
        assert state["schema_version"] == 2 and state["phase"] == "captured"
        assert set(state["python_recipe"]) == {
            "schema_version",
            "recipe_sha256",
            "environment_input_sha256",
        }
        path = (
            self.root
            / ".project-contracts"
            / str(tenant_id)
            / str(request.run_id)
            / "python-recipe.json"
        )
        assert path.exists()
        exact = await self.verifier.authorization_request(
            CapabilityRequest(
                tenant_id=tenant_id,
                user_id=request.user_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                capability="command",
                operation="run",
                resource="workspace/current/project-verification",
                arguments={},
                idempotency_key=request.idempotency_key,
            )
        )
        assert exact is not None
        assert exact == request and role is Role.OPERATOR
        if self.allowed:
            scope = capability_approval_scope(exact)
            assert scope is not None
            self.scopes.add(scope)
        return CapabilityResult(
            CapabilityStatus.ALLOWED if self.allowed else CapabilityStatus.DENIED,
            request.run_id, reason=None if self.allowed else "denied",
        )

    async def authorize(self, tenant_id: UUID, request: HarnessToolCallRequest, *,
                        user_id: UUID | None = None, role: Role | None = None) -> HarnessToolCallResult:
        # Static discovery grants nothing. Only InstallPolicy models the
        # concrete IO boundary and publishes the test repository's receipt.
        assert (tenant_id, request.run_id, user_id, role) == (
            self.invocation.tenant_id, self.invocation.run_id,
            self.repository.row.actor_id, Role.OPERATOR,
        )
        return HarnessToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="succeeded", payload={"authorized": True},
        )


async def configured(tmp_path: Path) -> tuple[Any, ...]:
    preparer, repository, context, _, _, root, invocation = await setup(tmp_path)
    source = (
        root
        / str(invocation.tenant_id)
        / "projects"
        / invocation.project_id
        / "sessions"
        / invocation.session_id
    )
    python_controls(source)

    async def current_record(tenant: Any, run: Any) -> Any:
        assert (tenant, run) == (repository.row.tenant_id, repository.row.id)
        return RunRepository._record(repository.row)

    repository.get = current_record
    executor = PythonExecutor(root)
    verifier = ProjectVerificationService(
        root=root, repository=repository, executor=None, python_executor=executor
    )
    authorizer = Authorizer(verifier, repository, root, invocation)

    async def approved(tenant: Any, run: Any, scope: str) -> bool:
        return scope in authorizer.scopes

    repository.is_capability_approval_scope_approved = approved
    preparer.verifier = verifier
    preparer.authorizer = cast(HarnessToolGateway, authorizer)
    return preparer, repository, context, executor, authorizer, root, invocation


async def test_python_recipe_precedes_approval_and_resume_never_recaptures_or_replays(
    tmp_path: Path,
) -> None:
    preparer, repository, context, executor, authorizer, root, invocation = await configured(
        tmp_path
    )
    with pytest.raises(RuntimeError, match="authorization"):
        await preparer(context)
    state = repository.row.routing_decision["project_input_preparation"]
    assert state["schema_version"] == 2 and state["phase"] == "captured"
    controls = root / ".project-contracts" / str(invocation.tenant_id) / str(invocation.run_id)
    before = {
        name: (controls / name).read_bytes() for name in ("baseline.json", "python-recipe.json")
    }
    assert len(executor.probes) == 1 and not executor.calls
    authorizer.allowed = True
    prepared = await preparer(
        context.model_copy(update={"routing_decision": repository.row.routing_decision})
    )
    assert prepared.routing_decision["project_input_preparation"]["schema_version"] == 2
    assert prepared.artifacts[0].content["observation"]["schema_version"] == 2
    assert len(executor.probes) == 1 and len(executor.calls) == 1
    await preparer(context.model_copy(update={"routing_decision": repository.row.routing_decision}))
    assert authorizer.calls == 2 and len(executor.calls) == 1
    assert before == {name: (controls / name).read_bytes() for name in before}


async def test_recipe_write_before_marker_crash_recovers_only_by_fresh_probe_and_matching_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, repository, context, executor, authorizer, root, invocation = await configured(
        tmp_path
    )
    original_state = preparer._state

    async def crash(context: Any, **kwargs: Any) -> Any:
        replacement = kwargs.get("replace")
        if replacement is not None and replacement.get("schema_version") == 2:
            raise RuntimeError("marker crash")
        return await original_state(context, **kwargs)

    monkeypatch.setattr(preparer, "_state", crash)
    with pytest.raises(RuntimeError, match="marker crash"):
        await preparer(context)
    assert repository.row.routing_decision["project_input_preparation"]["schema_version"] == 1
    path = (
        root
        / ".project-contracts"
        / str(invocation.tenant_id)
        / str(invocation.run_id)
        / "python-recipe.json"
    )
    before = path.read_bytes()
    assert len(executor.probes) == 1 and not executor.calls
    monkeypatch.setattr(preparer, "_state", original_state)
    authorizer.allowed = True
    await preparer(context.model_copy(update={"routing_decision": repository.row.routing_decision}))
    assert len(executor.probes) == 2 and len(executor.calls) == 1
    assert len({probe.execution_id for probe in executor.probes}) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("phase", ["reserved", "executing", "unknown"])
async def test_python_uncertain_original_is_never_replayed(tmp_path: Path, phase: str) -> None:
    preparer, repository, context, executor, _, _, _ = await configured(tmp_path)
    repository.row.routing_decision["project_input_preparation"] = {
        "schema_version": 2,
        "phase": phase,
    }
    with pytest.raises(RuntimeError):
        await preparer(
            context.model_copy(update={"routing_decision": repository.row.routing_decision})
        )
    assert not executor.calls and not executor.probes


async def test_preparer_projects_independent_python_history_metadata_without_changing_node_artifact(
    tmp_path: Path,
) -> None:
    preparer, repository, context, executor, authorizer, _, invocation = await configured(tmp_path)
    authorizer.allowed = True
    prepared = await preparer(context)
    trusted = await preparer.verifier.python_original_context(
        invocation.tenant_id, invocation.run_id
    )
    assert trusted is not None and trusted.environment_reference is not None
    metadata = next(
        item for item in prepared.artifacts if item.type == "original_python_project_context"
    )
    assert metadata.producer == "original_project_preparer"
    assert metadata.source_ids == (str(prepared.artifacts[0].id),)
    value = metadata.content
    assert value["schema_version"] == 1 and value["kind"] == "python_original_context"
    assert value["expected_source_sha256"] == trusted.stored.reference.input_source_sha256
    assert value["expected_contract_sha256"] == trusted.stored.contract.protected_sha256
    assert value["expected_recipe_sha256"] == trusted.inputs.sha256
    assert value["expected_environment_input_sha256"] == trusted.inputs.environment_input_sha256
    assert value["expected_environment_sha256"] == trusted.environment_reference.environment_sha256
    assert value["expected_execution_id"] == trusted.invocation.execution_id
    assert value["environment_reference"] == trusted.environment_reference.model_dump(mode="json")
    assert "verification" not in value and "observation" not in value
    assert prepared.artifacts[0].type == "original_project_observation"
    assert prepared.artifacts[0].content["schema_version"] == 1
    assert "AssertionError" not in repr(metadata)
    assert str(trusted.stored.snapshot) not in repr(metadata)
    assert len(executor.probes) == 1 and len(executor.calls) == 1
    resumed = await preparer(
        context.model_copy(update={"routing_decision": repository.row.routing_decision})
    )
    assert next(item for item in resumed.artifacts if item.type == metadata.type) == metadata


async def test_pure_python_history_projection_rejects_mismatched_or_untyped_context(
    tmp_path: Path,
) -> None:
    preparer, _, context, _, authorizer, _, invocation = await configured(tmp_path)
    authorizer.allowed = True
    await preparer(context)
    trusted = await preparer.verifier.python_original_context(
        invocation.tenant_id, invocation.run_id
    )
    assert trusted is not None
    project = importlib.import_module("agent_hub.runs.project_inputs").python_original_context_metadata
    for bad in (
        None,
        {"observation": trusted.observation},
        replace(
            trusted, invocation=trusted.invocation.model_copy(update={"recipe_sha256": "a" * 64})
        ),
    ):
        with pytest.raises(RuntimeError, match="original project preparation"):
            project(bad)
    metadata = project(trusted)
    with pytest.raises(TypeError):
        metadata["expected_source_sha256"] = "a" * 64


async def test_preparer_probe_never_consumes_original_run_execution_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer, _, context, executor, authorizer, _, _ = await configured(tmp_path)
    authorizer.allowed = True
    run = executor.run

    async def reject_probe_id_replay(invocation: Any) -> Any:
        if invocation.execution_id in {probe.execution_id for probe in executor.probes}:
            raise OSError("python_project_request_invalid")
        return await run(invocation)

    monkeypatch.setattr(executor, "run", reject_probe_id_replay)
    prepared = await preparer(context)
    assert len(executor.calls) == 1 and len(executor.probes) == 1
    assert executor.probes[0].execution_id.startswith("python_probe_")
    assert executor.probes[0].install_allowed is False
    assert executor.calls[0].execution_id == "original_" + context.run_id.hex
    assert (
        prepared.routing_decision["project_input_preparation"]["execution_id"]
        == executor.calls[0].execution_id
    )


async def test_cleanup_unknown_keeps_once_fence_and_never_creates_history_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    preparer, repository, context, executor, authorizer, _, _ = await configured(tmp_path)
    authorizer.allowed = True
    unknown = ProcessCleanupIncomplete("python_project_cleanup_incomplete")

    async def uncertain_run(invocation: Any) -> Any:
        executor.calls.append(invocation)
        raise unknown

    monkeypatch.setattr(executor, "run", uncertain_run)
    with pytest.raises(ProcessCleanupIncomplete) as error:
        await preparer(context)
    assert error.value is unknown
    state = repository.row.routing_decision["project_input_preparation"]
    assert state["schema_version"] == 2 and state["phase"] == "executing"
    assert "observation" not in state
    calls, probes = len(executor.calls), len(executor.probes)
    with pytest.raises(RuntimeError, match="original project preparation"):
        await preparer(
            context.model_copy(update={"routing_decision": repository.row.routing_decision})
        )
    assert (len(executor.calls), len(executor.probes)) == (calls, probes)


async def test_metadata_without_root_reference_never_uses_native_self_reported_environment(
    tmp_path: Path,
) -> None:
    preparer, _, context, executor, authorizer, _, _ = await configured(tmp_path)
    authorizer.allowed = True
    executor.failure_index = 0
    prepared = await preparer(context)
    metadata = next(
        item for item in prepared.artifacts if item.type == "original_python_project_context"
    )
    assert metadata.content["environment_reference"] is None
    assert metadata.content["expected_environment_sha256"] is None
