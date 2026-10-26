"""Durable pre-capture metadata state/approval bindings, without native IO."""

from __future__ import annotations

import importlib
import importlib.util
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from tests.unit.capabilities.test_generated_project_service import Repository
from tests.unit.skills.test_python_metadata_plan_protocol import plan_fields

MODULE = "agent_hub.capabilities.python_metadata_preparation"


def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "metadata preparation state binding missing"
    return importlib.import_module(MODULE)


def setup() -> tuple[Any, Any, Any]:
    m = api()
    row = Repository().row
    row.routing_decision.update(python_dependency_preflight_version=1,
        python_dependency_preflight_submission_version=1)
    scope = m.metadata_scope(row, actor="engineer")
    from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlan

    plan = PythonMetadataPlan.model_validate(plan_fields() | {"scope": scope.model_dump()})
    state = {"schema_version": 1, "phase": "planned", "plan": plan.model_dump(mode="json"),
        "plan_sha256": plan.sha256}
    row.routing_decision[m.METADATA_KEY] = state
    return m, row, plan


def test_metadata_binding_exists() -> None:
    assert callable(api().metadata_preparation_binding)


def test_concrete_plan_is_a_distinct_purpose_not_supply_or_recipe() -> None:
    m, row, plan = setup()
    req = m.metadata_preparation_binding(row, planned=True, live=True)
    assert req.user_id == row.actor_id and req.run_id == row.id
    assert req.agent_id == "engineer"
    assert req.resource == "workspace/current/project-metadata/python/" + plan.sha256
    assert req.arguments["purpose"] == "python_dependency_metadata"
    assert req.arguments["plan_sha256"] == plan.sha256
    assert "original_reference" not in req.arguments and "recipe_sha256" not in req.arguments
    assert req.arguments["download_bytes"] == plan.missing_bytes


@pytest.mark.parametrize("field", ["actor_id", "request", "actor_role"])
def test_changed_actor_or_request_rejects_stored_plan(field: str) -> None:
    m, row, _ = setup()
    setattr(row, field, UUID(int=99) if field == "actor_id" else "different" if field == "request" else Role.ADMIN)
    with pytest.raises(ValueError):
        m.metadata_preparation_binding(row)


@pytest.mark.parametrize("field,value", [
    ("requested_permissions", ["workspace.read"]), ("sandbox_profile", "restricted"),
    ("execution_backend", "docker"), ("project_id", "foreign"),
    ("workspace_session_id", "foreign"), ("generated_project_preparation", {}),
    ("python_dependency_preflight_version", True),
    ("python_dependency_preflight_submission_version", 0),
])
def test_bound_route_drift_or_legacy_context_never_gains_authority(field: str, value: Any) -> None:
    m, row, _ = setup()
    row.routing_decision[field] = value
    with pytest.raises(ValueError):
        m.metadata_preparation_binding(row)


def test_live_binding_requires_current_worker_lease() -> None:
    m, row, _ = setup()
    row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError):
        m.metadata_preparation_binding(row, live=True)
    assert m.metadata_preparation_binding(row).user_id == row.actor_id


@pytest.mark.parametrize("change", ["extra", "bool_version", "wrong_hash", "ready", "unknown", "wrong_id"])
def test_malformed_or_uncertain_state_is_not_planned(change: str) -> None:
    m, row, plan = setup()
    state = deepcopy(row.routing_decision[m.METADATA_KEY])
    if change == "extra": state["recipe"] = {}
    if change == "bool_version": state["schema_version"] = True
    if change == "wrong_hash": state["plan_sha256"] = "a" * 64
    if change == "ready":
        from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlan
        ready = PythonMetadataPlan.model_validate(plan_fields("ready") | {"scope": plan.scope.model_dump()})
        state.update(plan=ready.model_dump(mode="json"), plan_sha256=ready.sha256)
    if change == "unknown": state["phase"] = "unknown"
    if change == "wrong_id": state.update(phase="executing", execution_id="original_run")
    with pytest.raises(ValueError):
        m.metadata_state_plan(state)


def test_transition_reserves_execution_once_and_rejects_replay() -> None:
    m, row, _ = setup()
    planned = row.routing_decision[m.METADATA_KEY]
    assert m.validate_metadata_transition(None, planned)["phase"] == "planned"
    executing = {**planned, "phase": "executing", "execution_id": "metadata_prepare_" + "1" * 32}
    assert m.validate_metadata_transition(planned, executing)["phase"] == "executing"
    for before, after in ((None, executing), (executing, planned),
                          (executing, {**executing, "execution_id": "metadata_prepare_" + "2" * 32})):
        with pytest.raises(ValueError):
            m.validate_metadata_transition(before, after)


def test_approval_scope_is_plan_specific_and_never_a_conversation_grant() -> None:
    from agent_hub.capabilities.approvals import capability_approval_scope
    from agent_hub.capabilities.conversation_approvals import conversation_approval_binding

    m, row, plan = setup()
    req = m.metadata_preparation_binding(row)
    scope = capability_approval_scope(req)
    assert scope is not None and scope.endswith("resource=" + req.resource)
    assert plan.sha256 in scope
    assert conversation_approval_binding(row, req) is None
