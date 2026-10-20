"""Service control fixtures use dummy bytes and fake v4 transport, not native Python."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
    validated_original_observation,
)
from agent_hub.capabilities.python_project_input_store import _capture
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import project_source_fingerprint
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectBrokerResponse,
    PythonProjectInputsObservation,
    base_python_invocation,
)
from tests.unit.capabilities.test_project_baselines import source_tree
from tests.unit.capabilities.test_python_project_input_store import (
    interpreter,
    python_controls,
    selected,
)
from tests.unit.capabilities.test_python_project_reports import fail, fixture
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, StoredRun, stored_run


@dataclass(frozen=True)
class ServiceRecord(StoredRun):
    actor_role: Role = Role.OPERATOR


class Repository(FakeRunRepository):
    def __init__(self, record: Any) -> None:
        super().__init__(record)
        self.record = record
        self.approvals: set[str] = set()

    async def get(self, tenant_id: UUID, run_id: UUID) -> Any:
        assert (tenant_id, run_id) == (self.record.tenant_id, self.record.id)
        return self.record

    async def is_capability_approval_scope_approved(
        self, tenant: Any, run: Any, scope: str
    ) -> bool:
        return scope in self.approvals


class PythonExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[Any] = []
        self.probes: list[Any] = []
        self.inspections: list[Any] = []
        self.failure_index: int | None = 2
        self.origin: str | None = None
        self.mutate: Path | None = None
        self.changed_environment = False

    def inputs(self, invocation: Any) -> Any:
        from agent_hub.capabilities.project_baselines import ProjectBaselineReference
        from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation

        stored = ProjectBaselineStore(self.root).load(
            base_python_invocation(invocation),
            ProjectBaselineReference.from_payload(invocation.original_reference.model_dump()),
        )
        return _capture(
            stored,
            interpreter=interpreter(),
            wheels=selected(),
            max_control_bytes=256 * 1024,
            max_requirements=128,
        )

    async def probe(self, invocation: Any) -> PythonProjectInputsObservation:
        self.probes.append(invocation)
        inputs = self.inputs(invocation)
        return PythonProjectInputsObservation.model_validate(
            {
                "scope": {
                    key: getattr(invocation, key)
                    for key in (
                        "tenant_id",
                        "run_id",
                        "project_id",
                        "session_id",
                        "original_reference",
                    )
                },
                "declaration": inputs.declaration.to_payload(),
                "interpreter": inputs.interpreter.to_payload(),
                "wheels": tuple(wheel.to_payload() for wheel in inputs.wheels),
            },
            strict=True,
        )

    async def run(self, invocation: Any) -> PythonProjectBrokerResponse:
        self.calls.append(invocation)
        inputs = self.inputs(invocation)
        reused = self.origin is not None
        self.origin = self.origin or invocation.execution_id
        _, _, _, raw = fixture(reused=reused)
        raw.update(
            execution_id=invocation.execution_id,
            source_sha256=await project_source_fingerprint(
                self.root, base_python_invocation(invocation)
            ),
            recipe_sha256=inputs.sha256,
            environment_input_sha256=inputs.environment_input_sha256,
            original_check_contract=inputs.contract.protected_sha256,
            runtime=inputs.interpreter.to_payload(),
        )
        raw["dependency_install"]["origin_execution_id"] = self.origin
        reference: PythonEnvironmentReference | None = PythonEnvironmentReference.model_validate(
            {
                "scope": {
                    key: getattr(invocation, key)
                    for key in (
                        "tenant_id",
                        "run_id",
                        "project_id",
                        "session_id",
                        "original_reference",
                    )
                },
                "recipe_sha256": inputs.sha256,
                "environment_input_sha256": inputs.environment_input_sha256,
                "environment_sha256": "e" * 64,
                "security_manifest_sha256": "f" * 64,
                "origin_execution_id": self.origin,
            },
            strict=True,
        )
        if self.failure_index is not None:
            text = "AssertionError: expected 12\ntoken=secret\n"
            fail(raw, self.failure_index, stderr=text, stderr_observed_bytes=len(text.encode()))
            if self.failure_index == 0:
                reference = None
                self.origin = None
        if self.mutate is not None:
            self.mutate.write_bytes(b"changed during execution")
        return PythonProjectBrokerResponse(
            action="run_python_project",
            execution_id=invocation.execution_id,
            ok=True,
            environment_reference=reference,
            result=SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(raw), stderr=""),
        )

    async def inspect_environment(self, invocation: Any, *, expected_environment: Any) -> Any:
        if invocation.execution_id in {
            call.execution_id for call in (*self.calls, *self.inspections)
        }:
            raise OSError("python_project_request_invalid")
        self.inspections.append(invocation)
        if self.changed_environment:
            return expected_environment.model_copy(update={"environment_sha256": "9" * 64})
        return expected_environment


async def configured(tmp_path: Path) -> tuple[Any, Any, Any, Any, Path]:
    root = tmp_path / "workspaces"
    invocation, source = source_tree(root)
    python_controls(source)
    reference = await ProjectBaselineStore(root).capture_once(
        invocation, ManagedProjectInput(("src",))
    )
    record: Any = stored_run(tenant=invocation.tenant_id, run=invocation.run_id)
    record = ServiceRecord(record.tenant_id, record.id, record.routing_decision, UUID(int=456))
    record = replace(
        record,
        routing_decision={
            **(record.routing_decision or {}),
            "requested_permissions": [
                "workspace.read",
                "workspace.write",
                "command.run",
                "network.read",
            ],
            "project_input_contract_version": 1,
            "project_input": ManagedProjectInput(("src",)).to_payload(),
            "project_input_preparation": {
                "schema_version": 1,
                "phase": "captured",
                "reference": reference.to_payload(),
            },
        },
    )
    repository = Repository(record)
    executor = PythonExecutor(root)
    verifier = ProjectVerificationService(
        root=root, repository=repository, executor=None, python_executor=executor
    )
    inputs = await verifier.prepare_python_original(invocation=invocation, reference=reference)
    assert inputs is not None
    record.routing_decision["project_input_preparation"].update(
        schema_version=2,
        python_recipe={
            "schema_version": 1,
            "recipe_sha256": inputs.sha256,
            "environment_input_sha256": inputs.environment_input_sha256,
        },
    )
    return verifier, repository, executor, invocation, source


def request(repository: Any, invocation: Any) -> CapabilityRequest:
    return CapabilityRequest(
        tenant_id=invocation.tenant_id,
        user_id=repository.record.actor_id,
        run_id=invocation.run_id,
        agent_id="engineer",
        capability="command",
        operation="run",
        resource="workspace/current/project-verification",
        arguments={},
        idempotency_key="verify-once",
    )


async def observed(verifier: Any, repository: Any, invocation: Any) -> Mapping[str, Any]:
    state = repository.record.routing_decision["project_input_preparation"]
    original = state["reference"]
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference

    repository.record.routing_decision["project_input_preparation"] = {
        **state,
        "phase": "executing",
        "execution_id": "original_once",
    }
    result = await verifier.observe_original(
        tenant_id=invocation.tenant_id,
        run_id=invocation.run_id,
        actor="engineer",
        reference=ProjectBaselineReference.from_payload(original),
        execution_id="original_once",
        timeout_seconds=30,
    )
    digest = hashlib.sha256(
        json.dumps(
            result, default=dict, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    repository.record.routing_decision["project_input_preparation"] = {
        **state,
        "phase": "observed",
        "execution_id": "original_once",
        "observation": result,
        "observation_sha256": digest,
    }
    return cast(Mapping[str, Any], result)


async def test_authorization_rewrites_only_resource_and_exact_five_arguments(
    tmp_path: Path,
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    original = request(repository, invocation)
    approved = await verifier.authorization_request(original)
    assert approved is not None
    assert set(approved.arguments) == {
        "schema_version",
        "recipe_sha256",
        "environment_input_sha256",
        "project_id",
        "workspace_session_id",
    }
    assert (
        approved.resource
        == "workspace/current/project-verification/python/" + approved.arguments["recipe_sha256"]
    )
    assert original.model_dump(exclude={"resource", "arguments"}) == approved.model_dump(
        exclude={"resource", "arguments"}
    )
    assert not executor.calls and len(executor.probes) == 1


async def test_network_or_old_generic_grant_never_authorizes_python(tmp_path: Path) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    repository.approvals.add(capability_approval_scope(request(repository, invocation)))
    with pytest.raises(ProjectVerificationError, match="approval"):
        await observed(verifier, repository, invocation)
    assert not executor.calls


async def test_original_fail_then_same_environment_repair_and_live_receipt(tmp_path: Path) -> None:
    verifier, repository, executor, invocation, source = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    original = await observed(verifier, repository, invocation)
    assert original["schema_version"] == 2 and original["status"] == "failed"
    assert (
        original["verification"]["test_count"] is None
        and original["verification"]["wheel"] is not None
    )
    assert original["environment_reference"] is not None
    assert "AssertionError" in original["execution_diagnostic"]["stderr"]
    assert "secret" not in repr(original)
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)
    executor.failure_index = None
    (source / "src/billing/__init__.py").write_bytes(b"def total(): return 12\n")
    result = await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
    )
    assert result["verification"]["schema_version"] == 2
    assert result["verification"]["dependency_install"]["mode"] == "reused"
    assert (
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)
        == result["verification"]["source_sha256"]
    )
    assert len(executor.inspections) == 1
    executor.changed_environment = True
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)
    executor.changed_environment = False
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)


async def test_candidate_failure_uses_python_dto_and_actual_preview(tmp_path: Path) -> None:
    verifier, repository, _, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    with pytest.raises(ProjectVerificationError) as error:
        await verifier.verify(
            tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
        )
    assert error.value.project_failure is None
    assert error.value.python_project_failure is not None
    assert error.value.diagnostic is error.value.python_project_failure.diagnostic


async def test_original_long_error_tail_survives_persisted_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    original_run = executor.run
    text = 'building dependency\n' * 300 + 'AssertionError: final business check failed\n'
    async def long_error(actual: Any) -> PythonProjectBrokerResponse:
        response = await original_run(actual)
        raw = json.loads(response.result.stdout)
        fail(raw, 2, stderr=text, stderr_observed_bytes=len(text.encode()))
        return response.model_copy(update={'result': SkillResult(
            exit_code=0, timed_out=False, stdout=json.dumps(raw), stderr='',
        )})
    monkeypatch.setattr(executor, 'run', long_error)
    original = await observed(verifier, repository, invocation)
    diagnostic = original['execution_diagnostic']
    assert diagnostic['stderr_metadata']['truncated'] is True
    assert diagnostic['stderr'].endswith('AssertionError: final business check failed\n')
    # Node's default validator must still reject a tail-formatted Python preview.
    from agent_hub.capabilities.project_verification import _validate_original_diagnostic
    with pytest.raises(ValueError):
        _validate_original_diagnostic(diagnostic)
    context = await verifier.python_original_context(invocation.tenant_id, invocation.run_id)
    assert context.observation == original


@pytest.mark.parametrize(
    "change", ["scope", "permissions", "actor", "grant", "source", "checks", "observation"]
)
async def test_current_receipt_revalidates_every_delivery_boundary(
    tmp_path: Path, change: str
) -> None:
    verifier, repository, executor, invocation, source = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    executor.failure_index = None
    await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
    )
    route = repository.record.routing_decision
    if change == "scope":
        route["workspace_session_id"] = "other-session"
    elif change == "permissions":
        route["requested_permissions"] = ["workspace.read"]
    elif change == "actor":
        from uuid import UUID

        repository.record = replace(repository.record, actor_id=UUID(int=999))
    elif change == "grant":
        repository.approvals.clear()
    elif change == "source":
        (source / "src/billing/__init__.py").write_bytes(b"changed")
    elif change == "checks":
        (source / "tests/test_billing.py").write_bytes(b"weakened")
    else:
        route["project_input_preparation"]["observation_sha256"] = "0" * 64
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)


async def test_source_drift_does_not_create_failure_evidence_or_receipt(tmp_path: Path) -> None:
    verifier, repository, executor, invocation, source = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    executor.mutate = source / "src/billing/__init__.py"
    with pytest.raises(ProjectVerificationError) as error:
        await verifier.verify(
            tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
        )
    assert error.value.python_project_failure is None and error.value.diagnostic is None


async def test_durable_original_context_survives_service_restart_without_execution_or_grant(
    tmp_path: Path,
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    original = await observed(verifier, repository, invocation)
    repository.approvals.clear()
    repository.record.routing_decision["requested_permissions"] = ["workspace.read"]
    restarted = ProjectVerificationService(
        root=verifier.root, repository=repository, executor=None, python_executor=executor
    )
    context = await restarted.python_original_context(invocation.tenant_id, invocation.run_id)
    assert context is not None
    assert context.inputs.sha256 == original["python_recipe"]["recipe_sha256"]
    assert context.invocation.execution_id == "original_once"
    assert context.observation == original
    assert context.environment_reference is not None
    assert (
        context.environment_reference.model_dump(mode="json") == original["environment_reference"]
    )
    assert context.environment_references == (context.environment_reference,)
    assert len(executor.calls) == 1 and len(executor.probes) == 1
    with pytest.raises(ProjectVerificationError):
        await restarted.require_current_receipt(invocation.tenant_id, invocation.run_id)


async def test_durable_refs_include_candidate_environment_when_original_install_failed(
    tmp_path: Path,
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    executor.failure_index = 0
    original = await observed(verifier, repository, invocation)
    assert original["environment_reference"] is None
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
    )
    restarted = ProjectVerificationService(
        root=verifier.root, repository=repository, executor=None, python_executor=executor
    )
    context = await restarted.python_original_context(invocation.tenant_id, invocation.run_id)
    assert context is not None and context.environment_reference is None
    assert len(context.environment_references) == 1
    reference = context.environment_references[0]
    assert reference.environment_sha256 == result["verification"]["environment_sha256"]
    assert reference.origin_execution_id == executor.calls[-1].execution_id
    assert result["python_environment_reference"] == reference.model_dump(mode="json")


async def test_fresh_root_sibling_survives_persisted_history_but_never_grants_delivery(
    tmp_path: Path,
) -> None:
    from agent_hub.runtime.python_project_history import python_success_source
    from tests.unit.runtime.test_python_project_history import history_case

    verifier, repository, executor, original, context = await history_case(
        tmp_path, failure_index=0
    )
    assert original.environment_reference is None
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    trusted = await verifier.python_original_context(context.tenant_id, context.run_id)
    assert trusted is not None and len(trusted.environment_references) == 1
    reference = trusted.environment_references[0]
    assert result["python_environment_reference"] == reference.model_dump(mode="json")
    assert reference.security_manifest_sha256 != reference.environment_sha256
    source = result["verification"]["source_sha256"]
    assert python_success_source(context, result) == source
    persisted = json.loads(json.dumps(result, default=dict))
    assert python_success_source(context, persisted) == source
    assert (
        python_success_source(
            context,
            {
                key: value
                for key, value in persisted.items()
                if key != "python_environment_reference"
            },
        )
        is None
    )
    repository.approvals.clear()
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(context.tenant_id, context.run_id)
    restarted = ProjectVerificationService(
        root=verifier.root, repository=repository, executor=None, python_executor=executor
    )
    with pytest.raises(ProjectVerificationError):
        await restarted.require_current_receipt(context.tenant_id, context.run_id)
    assert not executor.inspections


@pytest.mark.parametrize(
    "defect", ["scope", "recipe", "inputs", "environment", "origin", "schema", "extra"]
)
async def test_fresh_sibling_history_rejects_mismatched_or_self_declared_reference(
    tmp_path: Path, defect: str
) -> None:
    from agent_hub.runtime.python_project_history import python_success_source
    from tests.unit.runtime.test_python_project_history import history_case

    verifier, _, executor, _, context = await history_case(tmp_path, failure_index=0)
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    persisted = json.loads(json.dumps(result, default=dict))
    sibling = persisted["python_environment_reference"]
    if defect == "scope":
        sibling["scope"]["run_id"] = str(UUID(int=999))
    elif defect == "schema":
        sibling["schema_version"] = True
    elif defect == "extra":
        sibling["install_allowed"] = True
    elif defect == "origin":
        sibling["origin_execution_id"] = "fake_origin"
    else:
        field = {
            "recipe": "recipe_sha256",
            "inputs": "environment_input_sha256",
            "environment": "environment_sha256",
        }[defect]
        sibling[field] = "0" * 64
    assert python_success_source(context, persisted) is None


@pytest.mark.parametrize(
    "defect",
    ["scope", "recipe", "inputs", "environment", "origin", "security", "id", "action", "missing"],
)
async def test_native_reply_must_bind_before_persisting_or_returning_root_sibling(
    tmp_path: Path, defect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    executor.failure_index = 0
    await observed(verifier, repository, invocation)
    executor.failure_index = None
    run = executor.run

    async def malformed(actual: Any) -> PythonProjectBrokerResponse:
        response: PythonProjectBrokerResponse = await run(actual)
        reference = response.environment_reference
        assert reference is not None
        if defect == "id":
            return response.model_copy(update={"execution_id": "wrong_execution"})
        if defect == "action":
            return response.model_copy(update={"action": "inspect_python_project_environment"})
        if defect == "missing":
            return response.model_copy(update={"environment_reference": None})
        changes: dict[str, Any]
        if defect == "scope":
            changes = {"scope": reference.scope.model_copy(update={"run_id": UUID(int=999)})}
        elif defect == "origin":
            changes = {"origin_execution_id": "fake_origin"}
        elif defect == "security":
            changes = {"security_manifest_sha256": "not_a_digest"}
        else:
            field = {
                "recipe": "recipe_sha256",
                "inputs": "environment_input_sha256",
                "environment": "environment_sha256",
            }[defect]
            changes = {field: "0" * 64}
        return response.model_copy(
            update={"environment_reference": reference.model_copy(update=changes)}
        )

    monkeypatch.setattr(executor, "run", malformed)
    with pytest.raises(ProjectVerificationError) as error:
        await verifier.verify(
            tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
        )
    assert str(tmp_path) not in str(error.value)
    assert error.value.python_project_failure is None
    context = await verifier.python_original_context(invocation.tenant_id, invocation.run_id)
    assert context is not None and context.environment_references == ()
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)


async def test_missing_original_state_is_unknown_not_node_authorization_fallback(
    tmp_path: Path,
) -> None:
    verifier, repository, _, invocation, _ = await configured(tmp_path)
    repository.record.routing_decision.pop("project_input_preparation")
    with pytest.raises(ProjectVerificationError):
        await verifier.authorization_request(request(repository, invocation))


@pytest.mark.parametrize("change", ["user", "resource", "arguments"])
async def test_authorization_request_never_takes_scope_from_model_arguments(
    tmp_path: Path, change: str
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    original = request(repository, invocation)
    changed = (
        {"user_id": UUID(int=999)}
        if change == "user"
        else (
            {"resource": "workspace/current/command"}
            if change == "resource"
            else {"arguments": {"recipe_sha256": "a" * 64}}
        )
    )
    with pytest.raises(ProjectVerificationError):
        await verifier.authorization_request(original.model_copy(update=changed))
    assert not executor.calls


async def test_partial_durable_reference_is_not_deleted_or_trusted(tmp_path: Path) -> None:
    verifier, repository, _, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    control = (
        verifier.root / ".project-contracts" / str(invocation.tenant_id) / str(invocation.run_id)
    )
    path = control / "python-environment-unknown.json"
    path.write_bytes(b"{partial")
    path.chmod(0o600)
    with pytest.raises(ProjectVerificationError):
        await verifier.python_original_context(invocation.tenant_id, invocation.run_id)
    assert path.read_bytes() == b"{partial"


@pytest.mark.parametrize("failure", [None, 0, 1, 2])
async def test_original_python_observation_is_strict_history_not_delivery_authority(
    tmp_path: Path,
    failure: int | None,
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    executor.failure_index = failure
    original = await observed(verifier, repository, invocation)
    assert original["schema_version"] == 2 and original["verification"]["schema_version"] == 2
    assert original["status"] == ("passed" if failure is None else "failed")
    assert ("execution_diagnostic" in original) == (failure is not None)
    context = await verifier.python_original_context(invocation.tenant_id, invocation.run_id)
    assert context is not None
    assert (
        validated_original_observation(
            original,
            expected_source=context.stored.reference.input_source_sha256,
            expected_contract=context.stored.contract.protected_sha256,
            expected_python_inputs=context.inputs,
            expected_python_invocation=context.invocation,
        )
        == original
    )
    with pytest.raises(ProjectVerificationError):
        validated_original_observation(
            original,
            expected_source=context.stored.reference.input_source_sha256,
            expected_contract=context.stored.contract.protected_sha256,
        )
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)


@pytest.mark.parametrize("field", ["python_recipe", "original_check_contract"])
async def test_python_original_wrapper_rejects_boolean_nested_schema(
    tmp_path: Path, field: str
) -> None:
    verifier, repository, _, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    original = await observed(verifier, repository, invocation)
    context = await verifier.python_original_context(invocation.tenant_id, invocation.run_id)
    assert context is not None
    changed = {**original, field: {**original[field], "schema_version": True}}
    with pytest.raises(ProjectVerificationError):
        validated_original_observation(
            changed,
            expected_source=context.stored.reference.input_source_sha256,
            expected_contract=context.stored.contract.protected_sha256,
            expected_python_inputs=context.inputs,
            expected_python_invocation=context.invocation,
        )


async def test_live_inspection_uses_fresh_no_install_typed_ids_not_completed_run_ids(
    tmp_path: Path,
) -> None:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer"
    )
    source = result["verification"]["source_sha256"]
    assert await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id) == source
    assert await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id) == source
    checks = executor.inspections
    assert len(checks) == 2 and checks[0].execution_id != checks[1].execution_id
    assert all(
        check.execution_id.startswith("python_inspect_") and check.install_allowed is False
        for check in checks
    )
    run = executor.calls[-1]
    for check in checks:
        assert check.model_dump(exclude={"execution_id", "install_allowed"}) == run.model_dump(
            exclude={"execution_id", "install_allowed"}
        )


async def test_cleanup_unknown_survives_real_runtime_tool_bridge_and_blocks_ledger_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autogen_core import CancellationToken

    from agent_hub.capabilities.gateway import CapabilityStatus
    from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest
    from agent_hub.runtime.autogen.adapter import (
        GatewayCapabilityTool,
        RuntimeExecutionError,
        ToolOutcomeUncertain,
        _DiscussionDurability,
    )
    from agent_hub.runtime.contracts import Artifact
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
    from tests.unit.capabilities.test_project_verification import Executor
    from tests.unit.harness.test_tool_gateway import FakePolicyGateway

    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    await observed(verifier, repository, invocation)
    unknown = ProcessCleanupIncomplete("python_project_cleanup_incomplete")

    async def uncertain_run(inv: Any) -> Any:
        executor.calls.append(inv)
        raise unknown

    monkeypatch.setattr(executor, "run", uncertain_run)
    backend = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        project_workspace_dir=verifier.root,
        run_repository=repository,
        project_executor=Executor(),
    )
    backend._project_verification = verifier
    with pytest.raises(ProcessCleanupIncomplete) as error:
        await backend.execute(
            tenant_id=invocation.tenant_id,
            run_id=invocation.run_id,
            actor="engineer",
            name="project.verify",
            arguments={},
            idempotency_key="unknown-runtime",
        )
    assert error.value is unknown and not isinstance(error.value, RuntimeCapabilityError)
    gateway = HarnessToolGateway(
        backend,
        policy_gateway=FakePolicyGateway(CapabilityStatus.ALLOWED),
        raise_backend_errors=True,
    )
    with pytest.raises(ProcessCleanupIncomplete) as error:
        await gateway.invoke(
            invocation.tenant_id,
            HarnessToolCallRequest(
                run_id=invocation.run_id,
                actor="engineer",
                tool_name="project.verify",
                arguments={},
                approval_required=False,
                sandbox="workspace_write",
                idempotency_key="unknown-tool",
            ),
            user_id=repository.record.actor_id,
            role=Role.OPERATOR,
        )
    assert error.value is unknown

    async def publish(artifacts: Any, models: Any, tools: Any) -> None:
        pass

    async def store(artifact: Artifact) -> UUID:
        pytest.fail("UNKNOWN must not publish a completed tool artifact")

    async def abort(write_id: UUID) -> bool:
        return True

    def finalize(write_id: UUID) -> None:
        pytest.fail("UNKNOWN must not finalize a tool artifact")

    durable = _DiscussionDurability(publish, store, abort, finalize, run_id=invocation.run_id)
    records: list[Any] = []
    tool = GatewayCapabilityTool(
        backend,
        tenant_id=invocation.tenant_id,
        run_id=invocation.run_id,
        actor="engineer",
        name="project.verify",
        records=records,
        durability=durable,
        harness_tool_gateway=gateway,
        user_id=repository.record.actor_id,
        role=Role.OPERATOR,
        sandbox_profile="workspace_write",
    )
    with pytest.raises(RuntimeExecutionError, match="outcome is uncertain"):
        await tool.run_json({}, CancellationToken(), call_id="unknown-ledger")
    assert len(durable.tool_entries) == 1 and durable.tool_entries[0]["status"] == "running"
    assert not durable.artifacts
    calls = len(executor.calls)
    with pytest.raises(ToolOutcomeUncertain):
        await tool.run_json({}, CancellationToken(), call_id="unknown-resume")
    assert len(executor.calls) == calls
    assert durable.failure_reason == "tool_outcome_uncertain"
    assert not backend._project_delivery_receipts
