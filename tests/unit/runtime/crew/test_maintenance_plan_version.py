"""Maintenance contracts are explicit in new steps without legacy digest churn."""

from __future__ import annotations

import pytest

from agent_hub.runtime.crew.plan import DispatchPlan, DispatchStep
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    WorkspaceDeliverySequenceGateway,
    WorkspaceDeliverySequenceHarness,
    _workspace_delivery_sequence_runtime,
)


def step(**extra: object) -> DispatchStep:
    return DispatchStep.model_validate({"id": "repair", "agent": "implementer", "task": "Repair files", **extra})


def test_default_version_is_omitted_from_every_legacy_serialization() -> None:
    default = step()
    assert default.workspace_maintenance_contract_version == 0
    assert "workspace_maintenance_contract_version" not in default.model_dump()
    assert "workspace_maintenance_contract_version" not in default.model_dump_json()
    explicit = step(workspace_maintenance_contract_version=0)
    assert explicit.model_dump_json() == default.model_dump_json()


def test_enabled_version_is_serialized_and_bound_to_plan_digest() -> None:
    runtime = _workspace_delivery_sequence_runtime(WorkspaceDeliverySequenceGateway(()), WorkspaceDeliverySequenceHarness())
    old = runtime._plan
    # Captured from the c582 HEAD class before adding the optional contract field.
    assert old.digest == "f3cc9d0ea86e2b814b0fd8be6527c74d6349dff60de5b28560dd6d827f131738"
    enabled = DispatchStep.model_validate({**old.steps[0].model_dump(), "workspace_maintenance_contract_version": 1})
    plan = old.model_copy(update={"steps": (enabled,)})
    assert enabled.model_dump()["workspace_maintenance_contract_version"] == 1
    assert plan.digest != old.digest
    assert DispatchPlan.model_validate_json(plan.model_dump_json()).digest == plan.digest
    assert DispatchPlan.model_validate_json(old.model_dump_json()).digest == old.digest


@pytest.mark.parametrize("value", (True, False, "1", 1.0, None, -1, 2))
def test_only_exact_supported_integer_versions_are_allowed(value: object) -> None:
    with pytest.raises(ValueError, match="maintenance"):
        step(workspace_maintenance_contract_version=value)
