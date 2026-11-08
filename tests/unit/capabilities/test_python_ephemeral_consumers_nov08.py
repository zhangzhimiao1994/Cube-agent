"""Current-execution evidence crosses consumers without becoming a reuse grant."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.python_product_delivery import consume_response
from agent_hub.capabilities.python_project_reports import (
    PythonProjectFailure,
    decode_python_project_report,
)
from agent_hub.capabilities.python_project_service import (
    _encoded,
    _validate_expired_original_observation,
    python_recipe_marker,
    validate_python_original_observation,
)
from agent_hub.capabilities.python_verification_recovery import _passed
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.python_project_client import PythonProjectSystemdSandbox
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
)
from tests.unit.capabilities.test_python_ephemeral_reports_nov08 import context, result


def test_product_consumer_accepts_negotiated_ephemeral_evidence() -> None:
    invocation, _, _, evidence, raw = context()
    response = PythonProjectBrokerResponse(
        action="run_python_project", execution_id=invocation.execution_id, ok=True,
        result=result(raw), ephemeral_environment=evidence,
    )
    with consume_response(response, invocation, evidence.source_sha256) as (actual, stream):
        assert actual.ephemeral_environment == evidence
        assert actual.environment_reference is None and stream is None


@pytest.mark.parametrize("install_allowed", [False, True])
async def test_client_negotiates_ephemeral_only_for_authorized_install(
    monkeypatch: pytest.MonkeyPatch, install_allowed: bool,
) -> None:
    invocation, _, _, _, raw = context()
    invocation = invocation.model_copy(update={"install_allowed": install_allowed})
    requests: list[PythonProjectBrokerRequest] = []

    async def exchange(
        self: PythonProjectSystemdSandbox, request: PythonProjectBrokerRequest,
    ) -> PythonProjectBrokerResponse:
        requests.append(request)
        return PythonProjectBrokerResponse(
            action="run_python_project", execution_id=invocation.execution_id,
            ok=True, result=result(raw),
        )

    monkeypatch.setattr(PythonProjectSystemdSandbox, "_exchange", exchange)
    await PythonProjectSystemdSandbox().run(invocation)
    assert len(requests) == 1
    assert requests[0].ephemeral_environment_version == (1 if install_allowed else None)


@pytest.mark.parametrize("failed", [False, True])
def test_original_observation_has_explicit_versioned_ephemeral_variant(failed: bool) -> None:
    invocation, inputs, _, evidence, raw = context()
    if failed:
        from tests.unit.capabilities.test_python_project_reports import fail

        fail(raw, 2)
    decoded = decode_python_project_report(
        invocation=invocation, inputs=inputs, expected_source_sha256=evidence.source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    )
    observation: dict[str, JsonValue] = {
        "schema_version": 3, "status": "failed" if failed else "passed", "verification": decoded.to_payload(),
        "original_check_contract": {
            "schema_version": 1, "contract_sha256": inputs.contract.protected_sha256,
            "protected_sha256": inputs.contract.protected_sha256,
        },
        "python_recipe": python_recipe_marker(inputs),
        "execution_id": invocation.execution_id, "environment_reference": None,
        "ephemeral_environment": evidence.model_dump(mode="json"),
    }
    if isinstance(decoded, PythonProjectFailure):
        assert decoded.diagnostic is not None
        observation["execution_diagnostic"] = decoded.diagnostic.to_payload()
    actual = validate_python_original_observation(
        observation, inputs=inputs, invocation=invocation,
        expected_source=evidence.source_sha256, expected_contract=inputs.contract.protected_sha256,
    )
    assert actual == observation
    assert isinstance(actual["ephemeral_environment"], Mapping)
    for changed in (
        {"schema_version": 2}, {"status": "passed" if failed else "failed"}, {"ephemeral_environment": None},
        {"environment_reference": {"fake": True}},
    ):
        with pytest.raises(ProjectVerificationError):
            validate_python_original_observation(
                observation | changed, inputs=inputs, invocation=invocation,
                expected_source=evidence.source_sha256,
                expected_contract=inputs.contract.protected_sha256,
            )


@pytest.mark.parametrize("failed", [False, True])
def test_ephemeral_observation_remains_valid_history_for_expired_owner_selection(failed: bool) -> None:
    import hashlib

    invocation, inputs, _, evidence, raw = context()
    if failed:
        from tests.unit.capabilities.test_python_project_reports import fail

        fail(raw, 2)
    decoded = decode_python_project_report(
        invocation=invocation, inputs=inputs, expected_source_sha256=evidence.source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    )
    observation: dict[str, JsonValue] = {
        "schema_version": 3, "status": "failed" if failed else "passed", "verification": decoded.to_payload(),
        "original_check_contract": {"schema_version": 1,
            "contract_sha256": inputs.contract.protected_sha256,
            "protected_sha256": inputs.contract.protected_sha256},
        "python_recipe": python_recipe_marker(inputs), "execution_id": invocation.execution_id,
        "environment_reference": None, "ephemeral_environment": evidence.model_dump(mode="json"),
    }
    if isinstance(decoded, PythonProjectFailure):
        assert decoded.diagnostic is not None
        observation["execution_diagnostic"] = decoded.diagnostic.to_payload()
    state: dict[str, JsonValue] = {"schema_version": 2, "phase": "observed",
        "reference": invocation.original_reference.model_dump(mode="json"),
        "python_recipe": python_recipe_marker(inputs), "execution_id": invocation.execution_id,
        "observation": observation,
        "observation_sha256": hashlib.sha256(_encoded(observation)).hexdigest()}
    _validate_expired_original_observation(
        state, invocation.model_copy(update={"execution_id": "expiry_selector", "install_allowed": False}),
    )


def test_private_recovery_accepts_same_execution_observation_without_cache_inspection() -> None:
    invocation, inputs, _, evidence, raw = context()
    decoded = decode_python_project_report(
        invocation=invocation, inputs=inputs, expected_source_sha256=evidence.source_sha256,
        environment_reference=None, ephemeral_environment=evidence, result=result(raw),
    )
    payload: dict[str, JsonValue] = {"verification": decoded.to_payload(),
                                  "python_recipe": python_recipe_marker(inputs)}
    restored = _passed(payload, inputs=inputs, invocation=invocation,
                       reference=evidence, source=evidence.source_sha256)
    assert restored.to_payload() == decoded.to_payload()
    with pytest.raises((ProjectVerificationError, ValueError)):
        _passed(payload, inputs=inputs,
                invocation=invocation.model_copy(update={"execution_id": "other_execution"}),
                reference=evidence, source=evidence.source_sha256)
