"""Real service stores with fake native transport; not an installation proof."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_verification import ProjectVerificationError, _strict_object
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_verification_recovery import restore_python_verification
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEphemeralEnvironmentEvidence,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)
from tests.unit.capabilities import test_python_generated_service as generated
from tests.unit.capabilities import test_python_project_service as original


def configure_ephemeral_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    run = original.PythonExecutor.run

    async def execute(
        self: original.PythonExecutor, invocation: PythonProjectInvocation,
    ) -> PythonProjectBrokerResponse:
        self.origin = None  # Each temporary environment is built for this invocation.
        response = await run(self, invocation)
        reference = response.environment_reference
        assert reference is not None
        inputs = PythonProjectInputs.from_payload(self.inputs(invocation).to_payload())
        assert response.result is not None
        raw: object = json.loads(response.result.stdout, object_pairs_hook=_strict_object)
        assert isinstance(raw, dict) and isinstance(raw["source_sha256"], str)
        evidence = PythonEphemeralEnvironmentEvidence(
            scope=reference.scope, execution_id=invocation.execution_id,
            recipe_sha256=inputs.sha256,
            environment_input_sha256=inputs.environment_input_sha256,
            environment_sha256=reference.environment_sha256,
            source_sha256=raw["source_sha256"],
            interpreter_sha256=inputs.interpreter.binary_sha256,
            environment_bytes=1024, unit_quiet=True, environment_removed=True,
            cache_written=False,
        )
        return response.model_copy(update={
            "environment_reference": None, "ephemeral_environment": evidence,
        })

    monkeypatch.setattr(original.PythonExecutor, "run", execute)


@pytest.fixture
def temporary_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_ephemeral_executor(monkeypatch)


async def test_original_observe_edit_verify_and_current_receipt_without_cache(
    tmp_path: Path, temporary_executor: None,
) -> None:
    verifier, repository, executor, invocation, source = await original.configured(tmp_path)
    executor.failure_index = 2
    exact = await verifier.authorization_request(original.request(repository, invocation))
    assert exact is not None
    repository.approvals.add(capability_approval_scope(exact))
    observation = await original.observed(verifier, repository, invocation)
    assert observation["schema_version"] == 3
    assert observation["status"] == "failed"
    assert observation["environment_reference"] is None
    assert observation["ephemeral_environment"]["execution_id"] == "original_once"
    executor.failure_index = None
    (source / "src/billing/__init__.py").write_bytes(b"def total(): return 12\n")
    response = await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="engineer",
    )
    assert "python_environment_reference" not in response
    assert response["python_ephemeral_environment"]["execution_id"] != "original_once"
    assert await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id) == response["verification"]["source_sha256"]
    assert not executor.inspections
    await restore_python_verification(
        verifier, kind="python_original", tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor="engineer", payload=response, source_sha256=response["verification"]["source_sha256"],
        execution_id=response["verification"]["execution_id"],
    )
    assert not executor.inspections and len(executor.calls) == 2
    repository.approvals.clear()
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)


async def test_generated_verify_and_delivery_receipt_without_cache(
    tmp_path: Path, temporary_executor: None,
) -> None:
    api = importlib.import_module("agent_hub.capabilities.python_generated_service")
    service, _, repository, executor, _, context = await generated.configured(api, tmp_path)
    exact = await service.authorization_request(generated.request(), context)
    generated.approve(repository, exact)
    response = await service.verify(context, "engineer")
    assert "python_environment_reference" not in response
    assert response["python_ephemeral_environment"]["environment_removed"] is True
    invocation = context.invocation
    assert await service.require_current(invocation.tenant_id, invocation.run_id) == response["verification"]["source_sha256"]
    assert not executor.inspections
    await restore_python_verification(
        service.owner, kind="python_generated", tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor="engineer", payload=response, source_sha256=response["verification"]["source_sha256"],
        execution_id=response["verification"]["execution_id"],
    )
    assert not executor.inspections and len(executor.calls) == 1
    repository.approvals.clear()
    with pytest.raises(ProjectVerificationError):
        await service.require_current(invocation.tenant_id, invocation.run_id)
