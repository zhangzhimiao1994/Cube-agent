import copy
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyArtifact,
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation


def invocation():
    return PythonProjectProbeInvocation(
        execution_id="supply_" + "1" * 32,
        tenant_id=UUID(int=1), run_id=UUID(int=2), project_id="project", session_id="session",
        sandbox_profile="workspace_write", timeout_seconds=30,
        original_reference={"schema_version": 1, "input_source_sha256": "1" * 64,
                            "contract_sha256": "2" * 64, "file_count": 6},
    )


def artifact():
    return PythonInputSupplyArtifact(filename="packaging-26.0-py3-none-any.whl", name="packaging",
        version="26.0", sha256="4" * 64, size_bytes=123)


def test_supply_request_is_separate_v5_and_has_no_model_selected_sources():
    value = PythonInputSupplyRequest(action="plan_python_project_inputs", invocation=invocation())
    assert value.version == 5
    assert value.invocation.install_allowed is False
    for change in ({"version": 4}, {"url": "https://example.com/pkg"},
                   {"action": "supply_python_project_inputs"}, {"expected_plan_sha256": "5" * 64}):
        with pytest.raises(ValidationError):
            PythonInputSupplyRequest.model_validate(value.model_dump() | change, strict=True)


def test_supply_and_status_require_exact_plan_and_separate_execution_id():
    data = {"invocation": invocation(), "expected_plan_sha256": "5" * 64}
    value = PythonInputSupplyRequest(action="supply_python_project_inputs", **data)
    assert value.expected_plan_sha256 == "5" * 64
    with pytest.raises(ValidationError):
        PythonInputSupplyRequest(action="inspect_python_project_input_supply", **data)
    status = PythonInputSupplyRequest(action="inspect_python_project_input_supply",
        supply_execution_id="supply_" + "2" * 32, **data)
    assert status.supply_execution_id != status.invocation.execution_id
    with pytest.raises(ValidationError):
        PythonInputSupplyRequest(action="inspect_python_project_input_supply",
            supply_execution_id=invocation().execution_id, **data)


def plan():
    scope = {name: getattr(invocation(), name) for name in (
        "tenant_id", "run_id", "project_id", "session_id", "original_reference")}
    return PythonInputSupplyPlan(scope=scope, declaration_sha256="5" * 64,
        manifest_sha256="6" * 64, lock_sha256="7" * 64, catalog_sha256="8" * 64,
        interpreter={"implementation": "cpython", "version": "3.12.14", "abi": "cp312",
                     "platform": "linux_x86_64", "binary_sha256": "9" * 64,
                     "base_runtime_sha256": "a" * 64}, artifacts=(artifact(),))


def test_plan_digest_binds_scope_catalog_original_and_all_selected_artifacts():
    value = plan()
    assert len(value.sha256) == 64
    assert value.sha256 == PythonInputSupplyPlan.model_validate_json(value.model_dump_json(), strict=True).sha256
    for key in ("catalog_sha256", "lock_sha256", "manifest_sha256", "declaration_sha256"):
        changed = value.model_dump()
        changed[key] = "b" * 64
        assert value.sha256 != PythonInputSupplyPlan.model_validate(changed, strict=True).sha256
    changed = copy.deepcopy(value.model_dump())
    changed["scope"]["run_id"] = UUID(int=3)
    assert value.sha256 != PythonInputSupplyPlan.model_validate(changed, strict=True).sha256
    with pytest.raises(ValidationError):
        PythonInputSupplyPlan.model_validate(value.model_dump() | {"artifacts": (artifact(), artifact())}, strict=True)


def test_artifact_cannot_hide_path_native_wheel_or_boolean_size():
    for change in ({"filename": "../packaging-26.0-py3-none-any.whl"},
                   {"filename": "packaging-26.0-cp312-cp312-linux_x86_64.whl"},
                   {"size_bytes": True}, {"url": "https://example.com"}):
        with pytest.raises(ValidationError):
            PythonInputSupplyArtifact.model_validate(artifact().model_dump() | change, strict=True)


def test_receipt_requires_complete_disjoint_partition_and_strict_version():
    value = PythonInputSupplyReceipt(plan=plan(), supply_execution_id=invocation().execution_id,
        published=(artifact().filename,), reused=())
    for change in ({"published": ()}, {"reused": value.published},
                   {"published": value.published * 2}, {"schema_version": True}):
        with pytest.raises(ValidationError):
            PythonInputSupplyReceipt.model_validate(value.model_dump() | change, strict=True)


def test_response_exclusivity_binds_same_run_and_exact_supply_execution():
    item = PythonInputSupplyRequest(action="supply_python_project_inputs", invocation=invocation(),
                                  expected_plan_sha256=plan().sha256)
    receipt = PythonInputSupplyReceipt(plan=plan(), supply_execution_id=invocation().execution_id,
        published=(artifact().filename,), reused=())
    response = PythonInputSupplyResponse(action=item.action, execution_id=item.invocation.execution_id,
        ok=True, receipt=receipt)
    response.require_for(item)
    for change in ({"error": "input_supply_unknown"}, {"ok": False}, {"plan": plan()},
                   {"missing_wheels": ()}, {"version": True}):
        with pytest.raises(ValidationError):
            PythonInputSupplyResponse.model_validate(response.model_dump() | change, strict=True)
    with pytest.raises(ValueError):
        response.require_for(item.model_copy(update={"expected_plan_sha256": "0" * 64}))
    with pytest.raises(ValueError):
        response.model_copy(update={"execution_id": "other_1"}).require_for(item)


def test_status_receipt_requires_matching_plan_and_original_execution():
    receipt = PythonInputSupplyReceipt(plan=plan(), supply_execution_id=invocation().execution_id,
        published=(artifact().filename,), reused=())
    value = PythonInputSupplyStatus(state="supplied", plan_sha256=plan().sha256,
        supply_execution_id=invocation().execution_id, receipt=receipt)
    for change in ({"state": "unknown"}, {"receipt": None},
                   {"plan_sha256": "0" * 64}, {"supply_execution_id": "other_1"}):
        with pytest.raises(ValidationError):
            PythonInputSupplyStatus.model_validate(value.model_dump() | change, strict=True)
