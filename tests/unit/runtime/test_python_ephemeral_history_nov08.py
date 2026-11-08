"""Historical ephemeral receipts remain scoped observations, never cache grants."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.python_project_reports import (
    PythonProjectFailure,
    decode_python_project_report,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import generated_project_history, python_project_history
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object
from tests.unit.capabilities.test_python_ephemeral_reports_nov08 import context, result
from tests.unit.capabilities.test_python_ephemeral_services_nov08 import (
    configure_ephemeral_executor,
)


@pytest.fixture
def temporary_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_ephemeral_executor(monkeypatch)


@pytest.mark.parametrize("failed", [False, True])
def test_original_report_history_accepts_removed_environment_with_exact_baseline_scope(failed: bool) -> None:
    invocation, inputs, _, evidence, raw = context()
    if failed:
        from tests.unit.capabilities.test_python_project_reports import fail

        fail(raw, 2)
    receipt = decode_python_project_report(
        invocation=invocation, inputs=inputs, expected_source_sha256=evidence.source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    ).to_payload()
    metadata: dict[str, JsonValue] = {
        "scope": {"tenant_id": str(invocation.tenant_id), "run_id": str(invocation.run_id),
                  "project_id": invocation.project_id, "workspace_session_id": invocation.session_id},
        "reference": _freeze_object(inputs.original_reference.to_payload(), name="baseline reference"),
        "recipe": {"schema_version": 1, "recipe_sha256": inputs.sha256,
                   "environment_input_sha256": inputs.environment_input_sha256},
        "runtime": _freeze_object(inputs.interpreter.to_payload(), name="runtime"), "environment_references": (),
        "protected_sha256": inputs.contract.protected_sha256,
    }
    actual = python_project_history._report(
        receipt, metadata, passed=not failed, source_label=evidence.source_sha256,
        ephemeral_sibling=evidence.model_dump(mode="json"),
    )
    assert actual == receipt
    for key, value in (("session_id", "another"), ("project_id", "another")):
        scope = evidence.scope.model_copy(update={key: value})
        forged = evidence.model_copy(update={"scope": scope})
        with pytest.raises(ValueError):
            python_project_history._report(
                receipt, metadata, passed=not failed, source_label=evidence.source_sha256,
                ephemeral_sibling=forged.model_dump(mode="json"),
            )


@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
def test_generated_history_validates_current_execution_without_persistent_reference(mode: TaskMode) -> None:
    invocation, inputs, _, evidence, raw = context()
    receipt = decode_python_project_report(
        invocation=invocation, inputs=inputs, expected_source_sha256=evidence.source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    ).to_payload()
    task = TaskContext(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor_id=UUID(int=9),
        actor_role=Role.OPERATOR, request="build requested project", mode=mode,
        routing_decision={"generated_project_contract_version": 1,
            "project_id": invocation.project_id, "workspace_session_id": invocation.session_id},
    )
    core: dict[str, JsonValue] = {"schema_version": 1, "manifest_sha256": "a" * 64,
        "count": 5, "names": tuple(f"test_billing.BillingTests.test_{i}" for i in range(5))}
    proof: dict[str, JsonValue] = {**core, "test_evidence_sha256": hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    binding: dict[str, JsonValue] = {
        "schema_version": 1, "kind": "generated_project_acceptance", "capture_phase": "post_model",
        "tenant_id": str(task.tenant_id), "run_id": str(task.run_id), "user_id": str(task.actor_id),
        "project_id": invocation.project_id, "session_id": invocation.session_id,
        "request_sha256": hashlib.sha256(task.request.encode()).hexdigest(),
        "acceptance_sha256": "b" * 64, "manifest_sha256": "c" * 64,
        "manifest_raw_sha256": "a" * 64, "source_sha256": evidence.source_sha256,
        "stack": "python", "test_execution": proof,
    }
    payload: dict[str, JsonValue] = {
        "verification": {**receipt, "generated_acceptance_sha256": "b" * 64},
        "generated_project_acceptance": binding,
        "python_recipe": {"schema_version": 1, "recipe_sha256": inputs.sha256,
                          "environment_input_sha256": inputs.environment_input_sha256},
        "python_ephemeral_environment": evidence.model_dump(mode="json"),
    }
    assert generated_project_history.generated_success_source(task, payload) == evidence.source_sha256
    assert generated_project_history.generated_success_source(
        task, {**payload, "python_environment_reference": None}) is None
    for field, value in (("source_sha256", "f" * 64), ("execution_id", "another"),
                         ("interpreter_sha256", "f" * 64), ("environment_removed", False)):
        forged = evidence.model_copy(update={field: value})
        assert generated_project_history.generated_success_source(task, {
            **payload, "python_ephemeral_environment": forged.model_dump(mode="json"),
        }) is None
@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
def test_original_failure_forwarding_preserves_prevalidated_temporary_observation(
    monkeypatch: pytest.MonkeyPatch, mode: TaskMode,
) -> None:
    """Exercise real diagnosis forwarding; metadata provenance is a separate gate."""
    invocation, inputs, _, evidence, raw = context()
    from tests.unit.capabilities.test_python_project_reports import fail

    fail(raw, 2)
    failure = decode_python_project_report(
        invocation=invocation, inputs=inputs, expected_source_sha256=evidence.source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    )
    assert isinstance(failure, PythonProjectFailure) and failure.diagnostic is not None
    binding: dict[str, JsonValue] = {"schema_version": 1,
        "contract_sha256": inputs.contract.protected_sha256,
        "protected_sha256": inputs.contract.protected_sha256}
    metadata: dict[str, JsonValue] = {
        "scope": {"tenant_id": str(invocation.tenant_id), "run_id": str(invocation.run_id),
                  "project_id": invocation.project_id, "workspace_session_id": invocation.session_id},
        "reference": _freeze_object(inputs.original_reference.to_payload(), name="baseline"),
        "recipe": {"schema_version": 1, "recipe_sha256": inputs.sha256,
                   "environment_input_sha256": inputs.environment_input_sha256},
        "runtime": _freeze_object(inputs.interpreter.to_payload(), name="runtime"),
        "environment_references": (), "protected_sha256": inputs.contract.protected_sha256,
        "original_execution_id": invocation.execution_id,
    }
    observation: dict[str, JsonValue] = {"verification": failure.to_payload(),
        "ephemeral_environment": evidence.model_dump(mode="json"),
        "execution_diagnostic": failure.diagnostic.to_payload(),
        "original_check_contract": binding}
    task = TaskContext(tenant_id=invocation.tenant_id, run_id=invocation.run_id, mode=mode,
        request="repair", routing_decision={"project_input_preparation": {"observation": observation}})
    monkeypatch.setattr(python_project_history, "_history", lambda _: metadata)
    assert python_project_history.python_original_failed(task)
    damaged = {**observation, "ephemeral_environment": evidence.model_copy(
        update={"execution_id": "other_execution"}).model_dump(mode="json")}
    assert not python_project_history.python_original_failed(task.model_copy(update={
        "routing_decision": {"project_input_preparation": {"observation": damaged}}}))


@pytest.mark.parametrize("mode", [mode for mode in TaskMode if mode is not TaskMode.AUTO])
async def test_full_original_history_recognizes_temporary_test_failure_for_repair(
    tmp_path: Path, temporary_executor: None, mode: TaskMode,
) -> None:
    from agent_hub.runtime.original_project_progress import original_failure_observed
    from tests.unit.runtime.test_python_project_history import history_case

    _, _, _, original, task = await history_case(tmp_path)
    assert original.observation["schema_version"] == 3
    assert original.environment_reference is None and not original.environment_references
    task = task.model_copy(update={"mode": mode})
    assert python_project_history.python_original_failed(task)
    assert original_failure_observed(task)


