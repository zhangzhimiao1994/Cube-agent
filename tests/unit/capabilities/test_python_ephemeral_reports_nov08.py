"""A removed current environment proves one execution, never cache availability."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_hub.capabilities import python_project_reports as reports
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonEphemeralEnvironmentEvidence,
    PythonProjectInvocation,
)
from tests.unit.capabilities.test_python_project_reports import fixture
from tests.unit.runtime.delivery_report_typed_fixture import mutable


def context() -> tuple[
    PythonProjectInvocation, PythonProjectInputs, PythonEnvironmentReference,
    PythonEphemeralEnvironmentEvidence, dict[str, object],
]:
    invocation, inputs, reference, raw = fixture()
    request = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
    selected = PythonProjectInputs.from_payload(inputs.to_payload())
    cached = PythonEnvironmentReference.model_validate(reference.model_dump(), strict=True)
    evidence = PythonEphemeralEnvironmentEvidence(
        scope=cached.scope, execution_id=request.execution_id,
        recipe_sha256=selected.sha256,
        environment_input_sha256=selected.environment_input_sha256,
        environment_sha256=cached.environment_sha256,
        source_sha256=selected.original_reference.input_source_sha256,
        interpreter_sha256=selected.interpreter.binary_sha256,
        environment_bytes=1024, unit_quiet=True, environment_removed=True,
        cache_written=False,
    )
    return request, selected, cached, evidence, mutable(raw)


def result(raw: dict[str, object]) -> SkillResult:
    return SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(raw), stderr="")


@pytest.mark.parametrize("candidate", [False, True])
def test_removed_environment_accepts_current_passed_receipt_without_cache_reference(
    candidate: bool,
) -> None:
    request, inputs, _, evidence, raw = context()
    source = evidence.source_sha256
    if candidate:
        source = "4" * 64
        request = request.model_copy(update={"source_kind": "candidate"})
        evidence = evidence.model_copy(update={"source_sha256": source})
        raw["source_sha256"] = source
    decoded = reports.decode_python_project_report(
        invocation=request, inputs=inputs, expected_source_sha256=source,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    )
    assert isinstance(decoded, reports.PythonEphemeralProjectReport)
    assert decoded.ephemeral_environment == evidence
    assert decoded.to_payload()["status"] == "passed"
    assert decoded.to_payload()["environment_sha256"] == evidence.environment_sha256
    assert decoded.environment_reference is None
    with pytest.raises((ValueError, ProjectVerificationError)):
        replace(decoded, expected_source_sha256="5" * 64)


@pytest.mark.parametrize("field,value", [
    ("source_sha256", "4" * 64), ("interpreter_sha256", "4" * 64),
    ("execution_id", "another_execution"), ("recipe_sha256", "4" * 64),
    ("environment_input_sha256", "4" * 64), ("environment_sha256", "4" * 64),
    ("unit_quiet", False), ("environment_removed", False), ("cache_written", True),
])
def test_ephemeral_receipt_rejects_substitution_and_unproved_cleanup(
    field: str, value: object,
) -> None:
    request, inputs, _, evidence, raw = context()
    forged = evidence.model_copy(update={field: value})
    with pytest.raises(ProjectVerificationError, match="python_project_report_invalid"):
        reports.decode_python_project_report(
            invocation=request, inputs=inputs,
            expected_source_sha256=inputs.original_reference.input_source_sha256,
            environment_reference=None, ephemeral_environment=forged, result=result(raw),
        )


def test_ephemeral_and_cached_proofs_cannot_be_mixed() -> None:
    request, inputs, reference, evidence, raw = context()
    with pytest.raises(ProjectVerificationError, match="python_project_report_invalid"):
        reports.decode_python_project_report(
            invocation=request, inputs=inputs,
            expected_source_sha256=inputs.original_reference.input_source_sha256,
            environment_reference=reference, ephemeral_environment=evidence, result=result(raw),
        )


def test_natural_test_failure_keeps_removed_environment_observation_without_success_credit() -> None:
    request, inputs, _, evidence, raw = context()
    checks = raw["checks"]
    assert isinstance(checks, list)
    check = mutable(checks[-1])
    check.update(status="failed", exit_code=1, stdout="", stdout_observed_bytes=0)
    raw.update(status="failed", test_count=None)
    decoded = reports.decode_python_project_report(
        invocation=request, inputs=inputs,
        expected_source_sha256=inputs.original_reference.input_source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    )
    assert isinstance(decoded, reports.PythonProjectFailure)
    assert decoded.ephemeral_environment == evidence
    assert decoded.report["status"] == "failed" and decoded.diagnostic is not None
    assert decoded.report["environment_sha256"] == evidence.environment_sha256
    restored = reports.validate_python_failure(
        decoded.to_payload(), invocation=request, inputs=inputs,
        expected_source_sha256=evidence.source_sha256, environment_reference=None,
        ephemeral_environment=evidence,
    )
    assert restored.report == decoded.report and restored.diagnostic is None


def test_install_failure_cannot_claim_a_verified_removed_environment() -> None:
    request, inputs, _, evidence, raw = context()
    from tests.unit.capabilities.test_python_project_reports import fail

    fail(raw, 0)
    with pytest.raises(ProjectVerificationError, match="python_project_report_invalid"):
        reports.decode_python_project_report(
            invocation=request, inputs=inputs, expected_source_sha256=evidence.source_sha256,
            environment_reference=None, ephemeral_environment=evidence, result=result(raw),
        )
