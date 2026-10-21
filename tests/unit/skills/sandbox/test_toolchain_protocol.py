"""v7 data binding tests; valid envelopes are not SDK supply authorization."""

import importlib
import importlib.util
from typing import Any
from uuid import UUID

import pytest

from tests.unit.skills.sandbox.test_toolchain_catalog import catalog, declaration, spaces


def test_separate_toolchain_protocol_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_protocol") is not None


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.toolchain_protocol"
    if importlib.util.find_spec(name) is None:
        pytest.skip("v7 absent")
    return importlib.import_module(name)


def invocation(api: Any) -> Any:
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope

    scope = ToolchainPlanScope(
        tenant_id=UUID(int=1),
        user_id=UUID(int=2),
        run_id=UUID(int=3),
        project_id="demo",
        session_id="session",
        actor_id="main_agent",
        actor_role="operator",
        source_sha256="b" * 64,
        contract_sha256="c" * 64,
    )
    return api.ToolchainInvocation(
        execution_id="sdk_test",
        tenant_id=scope.tenant_id,
        run_id=scope.run_id,
        project_id=scope.project_id,
        session_id=scope.session_id,
        sandbox_profile="workspace_write",
        install_allowed=False,
        timeout_seconds=600,
        scope=scope,
        original_reference={
            "schema_version": 1,
            "input_source_sha256": "b" * 64,
            "contract_sha256": "c" * 64,
            "file_count": 3,
        },
    )


def plan(api: Any) -> Any:
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog, plan_toolchain_supply

    call = invocation(api)
    return plan_toolchain_supply(
        ToolchainCatalog.from_payload(catalog()),
        declaration(),
        call.scope,
        spaces(importlib.import_module("agent_hub.skills.sandbox.toolchain_catalog")),
        execution_bytes=call.workspace_limit_bytes + call.source_limit_bytes,
        source_limit_bytes=call.source_limit_bytes,
        source_entry_limit=10 * call.file_limit,
    )


def reference(api: Any) -> Any:
    value = plan(api)
    return api.ToolchainReference(
        artifact_sha256=value.artifact.artifact_sha256,
        archive_sha256=value.artifact.sha256,
        tree_sha256="1" * 64,
        binary_sha256="2" * 64,
        gofmt_sha256="3" * 64,
        total_bytes=100,
        file_count=3,
        version=value.artifact.version,
        platform="linux_amd64",
    )


def test_v7_plan_supply_inspect_and_profile_have_exclusive_shapes(api: Any) -> None:
    call = invocation(api)
    value = plan(api)
    for action, extra in (
        ("plan_project_toolchain", {}),
        ("supply_project_toolchain", {"plan": value}),
        (
            "inspect_project_toolchain_supply",
            {"plan": value, "supply_execution_id": "other_supply"},
        ),
        ("inspect_project_toolchain", {"plan": value, "expected_environment": reference(api)}),
    ):
        request = api.ToolchainRequest(action=action, invocation=call, **extra)
        assert api.parse_toolchain_request(request.model_dump_json().encode()) == request
        assert request.version == 7
    assert call.install_allowed is False


@pytest.mark.parametrize(
    "change",
    [
        {"install_allowed": True},
        {"install_allowed": 1},
        {"sandbox_profile": "restricted"},
        {"tenant_id": UUID(int=4)},
        {"run_id": UUID(int=4)},
        {"project_id": "different"},
        {"session_id": "different"},
        {
            "original_reference": {
                "schema_version": 1,
                "input_source_sha256": "d" * 64,
                "contract_sha256": "c" * 64,
                "file_count": 3,
            }
        },
    ],
)
def test_invocation_cannot_rebind_scope_or_reuse_old_install_flag(
    api: Any, change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        api.ToolchainInvocation.model_validate(invocation(api).model_dump() | change, strict=True)


@pytest.mark.parametrize(
    "action,extra",
    [
        ("plan_project_toolchain", {"supply_execution_id": "old"}),
        ("supply_project_toolchain", {}),
        ("inspect_project_toolchain_supply", {}),
        ("inspect_project_toolchain", {}),
        ("run_project", {}),
    ],
)
def test_incomplete_or_legacy_actions_are_not_accepted(
    api: Any, action: str, extra: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        api.ToolchainRequest(action=action, invocation=invocation(api), **extra)


def test_cross_scope_plan_and_wrong_capacity_budget_rejected(api: Any) -> None:
    call = invocation(api)
    value = plan(api)
    wrong_scope = type(value.scope).model_validate(value.scope.model_dump() | {"actor_id": "other"})
    for wrong in (
        value.model_copy(update={"scope": wrong_scope}),
        value.model_copy(update={"execution_bytes": value.execution_bytes + 1}),
    ):
        with pytest.raises(ValueError):
            api.ToolchainRequest(action="supply_project_toolchain", invocation=call, plan=wrong)


def test_parser_duplicate_keys_bool_versions_size_and_unknowns_fail(api: Any) -> None:
    raw = (
        api.ToolchainRequest(action="plan_project_toolchain", invocation=invocation(api))
        .model_dump_json()
        .encode()
    )
    for damaged in (
        raw.replace(b'"version":7', b'"version":true'),
        raw.replace(b'"version":7', b'"version":7,"version":7'),
        raw.replace(b'"version":7', b'"version":6'),
        raw[:-1] + b',"grant":true}',
        b" " * 524289,
    ):
        with pytest.raises(ValueError):
            api.parse_toolchain_request(damaged)


def test_reference_binds_actual_artifact_and_receipt_action_scope(api: Any) -> None:
    call = invocation(api)
    value = plan(api)
    ref = reference(api)
    receipt = api.ToolchainSupplyReceipt(
        plan=value, supply_execution_id=call.execution_id, environment=ref, reused=False
    )
    request = api.ToolchainRequest(action="supply_project_toolchain", invocation=call, plan=value)
    response = api.ToolchainResponse(
        action=request.action,
        execution_id=call.execution_id,
        scope=call.scope,
        ok=True,
        receipt=receipt,
    )
    response.require_for(request)
    for change in (
        {"artifact_sha256": "e" * 64},
        {"archive_sha256": "f" * 64},
        {"version": "1.27.4"},
        {"total_bytes": 1001},
        {"file_count": 101},
    ):
        with pytest.raises(ValueError):
            api.ToolchainSupplyReceipt(
                plan=value,
                supply_execution_id=call.execution_id,
                environment=ref.model_copy(update=change),
                reused=False,
            )
    for change in ({"execution_id": "wrong"}, {"action": "plan_project_toolchain"}):
        with pytest.raises(ValueError):
            response.model_copy(update=change).require_for(request)


def test_failure_and_inspection_status_never_fabricate_a_success_receipt(api: Any) -> None:
    call = invocation(api)
    value = plan(api)
    request = api.ToolchainRequest(
        action="inspect_project_toolchain_supply",
        invocation=call,
        plan=value,
        supply_execution_id="old_supply",
    )
    for state in ("absent", "in_progress", "unknown"):
        status = api.ToolchainSupplyStatus(
            state=state, plan_sha256=value.sha256, supply_execution_id="old_supply"
        )
        response = api.ToolchainResponse(
            action=request.action,
            execution_id=call.execution_id,
            scope=call.scope,
            ok=True,
            status=status,
        )
        response.require_for(request)
        assert status.receipt is None
    with pytest.raises(ValueError):
        api.ToolchainSupplyStatus(
            state="supplied", plan_sha256=value.sha256, supply_execution_id="old_supply"
        )
    with pytest.raises(ValueError):
        api.ToolchainResponse(
            action=request.action,
            execution_id=call.execution_id,
            scope=call.scope,
            ok=False,
            error="toolchain_unknown",
            status=status,
        )
