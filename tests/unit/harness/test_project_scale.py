import pytest

from agent_hub.harness.project_scale import build_project_scale_run_plan


def test_ultra_contract_declares_fixed_pagination_workload() -> None:
    from agent_hub.harness.project_scale import PROJECT_ULTRA_LOAD_GUIDANCE
    from agent_hub.harness.project_scale_runner import _deliverable_repair_body

    plan = build_project_scale_run_plan(
        scales=("ultra",), flows=("direct",), benchmark_kind="capability",
    )
    body = plan.requests[0].body
    repair = _deliverable_repair_body(dict(body), "ultra:direct", benchmark_kind="capability")
    for message in (str(body["message"]), str(repair["message"])):
        assert PROJECT_ULTRA_LOAD_GUIDANCE in message
        for required in (
            "offset", "1..100", "1000", "17", "four concurrent", "String(project_id)",
            "DATA_DIR", "distinct empty", "new absolute paths", "inaccessible",
            "without rebuilding or reinstalling",
        ):
            assert required in message


@pytest.mark.parametrize("scale, required", [
    ("large", (
        "reservations atomically consume stock", "rejected reservations leave stock unchanged",
        "orders.total", "orders.authorized_count", "inventory.reserved_units",
        "fulfillment.total", "fulfillment.cancelled_count", "nonnegative integers",
        "entity_id", "order.created", "payment.authorized", "survive process restart",
        "POST /inventory/stock adds quantity to the existing SKU balance",
    )),
    ("ultra", (
        "GET /projects/:id/milestones", "GET /projects/:id/budgets",
        "GET /projects/:id/staffing", "GET /projects/:id/risks", "GET /dependencies/:id",
        "project_id,program_id,name", "budget_total", "staffing_allocation",
        "risk_count", "milestone_count", "survive process restart",
        "filter by program_id", "CSV quoting",
    )),
])
def test_large_business_contract_declares_independent_readback_checks(
    scale: str, required: tuple[str, ...],
) -> None:
    plan = build_project_scale_run_plan(
        scales=(scale,), flows=("direct",), benchmark_kind="capability",
    )
    message = str(plan.requests[0].body["message"])
    for phrase in required:
        assert phrase in message


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
def test_capability_request_requires_canonical_verification_report(scale: str) -> None:
    plan = build_project_scale_run_plan(
        scales=(scale,), flows=("direct",), benchmark_kind="capability"
    )

    message = str(plan.requests[0].body["message"])

    assert "root-level VERIFICATION.md" in message
    assert "build, test, and interaction commands" in message


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
def test_capability_request_requires_honest_unexecuted_checks(scale: str) -> None:
    plan = build_project_scale_run_plan(
        scales=(scale,), flows=("direct",), benchmark_kind="capability"
    )

    message = str(plan.requests[0].body["message"])

    assert "mark checks not executed when they were not run" in message
    assert "Do not prefill pass records or fabricate execution" in message


def test_project_scale_plan_defaults_to_fixture_benchmark_payload() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",))

    payload = plan.to_payload()

    assert payload["benchmark_kind"] == "fixture"
    assert payload["capability_verified"] is False
    assert plan.benchmark_kind == "fixture"
    assert "Project-scale acceptance fixture" in str(plan.requests[0].body["message"])


def test_capability_benchmark_uses_real_scale_requirements_without_fixture_marker() -> None:
    plan = build_project_scale_run_plan(
        scales=("small", "medium", "large", "ultra"),
        flows=("direct",),
        benchmark_kind="capability",
    )

    payload = plan.to_payload()

    assert payload["benchmark_kind"] == "capability"
    assert payload["capability_verified"] is False
    messages = {
        request.case_id: str(request.body["message"])
        for request in plan.requests
    }
    assert set(messages) == {
        "small:direct",
        "medium:direct",
        "large:direct",
        "ultra:direct",
    }
    for message in messages.values():
        assert "Project-scale acceptance fixture" not in message
        assert "deliverable_quality" not in message
        assert "agent_standard_verification" not in message
        assert "workspace_bundle.files" in message
        assert "file blocks headed ### `path/to/file`" in message
        assert "Acceptance conditions:" in message
        assert "Do not prefill pass records" in message
        assert "reproducible verification evidence" in message
    assert "persistent task management API" in messages["small:direct"]
    assert "POST /tasks" in messages["small:direct"]
    assert "GET /tasks" in messages["small:direct"]
    assert "PATCH /tasks/:id" in messages["small:direct"]
    assert "DELETE /tasks/:id" in messages["small:direct"]
    assert "POST /tasks/:id/restore" in messages["small:direct"]
    assert "PORT environment variable" in messages["small:direct"]
    assert "DATA_DIR" in messages["small:direct"]
    assert "CRM" not in messages["medium:direct"]
    assert "tenant-aware customer account service" in messages["medium:direct"]
    assert "must not require pre-created tenant records" in messages["medium:direct"]
    assert "contacts must accept {account_id,name,email}" in messages["medium:direct"]
    assert "GET /tenants/:tenant_id/opportunities" in messages["medium:direct"]
    assert "opportunities list endpoint is required" in messages["medium:direct"]
    assert "opportunities must accept {account_id,name,amount,stage}" in messages["medium:direct"]
    assert "resolve account_id/contact_id inside the URL tenant before validating" in messages["medium:direct"]
    assert "foreign or missing reference must return 404 NOT_FOUND" in messages["medium:direct"]
    assert "opportunity stages must accept open, won, and lost" in messages["medium:direct"]
    assert "POST create endpoints must return the created object directly with a top-level id" in messages["medium:direct"]
    assert "large project" in messages["large:direct"]
    assert "multi-service order operations platform" in messages["large:direct"]
    assert "all POST create endpoints must return the created object directly with a top-level id" in messages["large:direct"]
    assert "POST /inventory/reservations accepts {sku,quantity,reason}" in messages["large:direct"]
    assert "POST /orders accepts {customer_id,client_request_id,lines:[{sku,quantity}]}" in messages["large:direct"]
    assert "POST /orders/:id/payment accepts {state,amount}" in messages["large:direct"]
    assert "POST /fulfillment/jobs accepts {order_id,warehouse}" in messages["large:direct"]
    assert "cancelled and completed" in messages["large:direct"]
    assert "summary keys orders, inventory, and fulfillment" in messages["large:direct"]
    assert "test runner, test API imports, and test configuration must agree" in messages["large:direct"]
    assert "Project-local build and test executables referenced by npm scripts" in messages["large:direct"]
    assert "ultra-large project" in messages["ultra:direct"]
    assert "enterprise project portfolio" in messages["ultra:direct"]
    assert len(set(messages.values())) == 4


def test_multi_agent_capability_request_requires_role_artifact_dependencies() -> None:
    plan = build_project_scale_run_plan(
        scales=("small",),
        flows=("multi_agent",),
        benchmark_kind="capability",
    )

    message = str(plan.requests[0].body["message"])

    assert "Architecture Agent" in message
    assert "Implementation Agent" in message
    assert "Test Agent" in message
    assert "Synthesis Agent" in message
    assert "must depend on the architecture contract" in message
    assert "must run after implementation artifacts exist" in message
    assert "must depend on the architecture, implementation, and test artifacts" in message
