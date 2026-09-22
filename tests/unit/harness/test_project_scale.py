from agent_hub.harness.project_scale import build_project_scale_run_plan


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
    assert "tenant-aware CRM-lite" in messages["medium:direct"]
    assert "must not require pre-created tenant records" in messages["medium:direct"]
    assert "contacts must accept {account_id,name,email}" in messages["medium:direct"]
    assert "opportunities must accept {account_id,name,amount,stage}" in messages["medium:direct"]
    assert "opportunity stages must accept open, won, and lost" in messages["medium:direct"]
    assert "POST create endpoints must return the created object directly with a top-level id" in messages["medium:direct"]
    assert "large project" in messages["large:direct"]
    assert "multi-service order operations platform" in messages["large:direct"]
    assert "ultra-large project" in messages["ultra:direct"]
    assert "enterprise project portfolio" in messages["ultra:direct"]
    assert len(set(messages.values())) == 4
