from agent_hub.harness.project_scale import (
    PROJECT_SCALE_FLOW_KINDS,
    PROJECT_SCALE_TIERS,
    ProjectScaleMatrix,
    build_project_scale_run_plan,
    build_project_scale_run_request,
)


def test_project_scale_matrix_covers_every_scale_and_flow() -> None:
    matrix = ProjectScaleMatrix.default()

    assert matrix.scale_tiers == PROJECT_SCALE_TIERS
    assert matrix.flow_kinds == PROJECT_SCALE_FLOW_KINDS
    assert matrix.case_count == len(PROJECT_SCALE_TIERS) * len(PROJECT_SCALE_FLOW_KINDS)

    cases = {(case.scale, case.flow) for case in matrix.cases}
    for scale in PROJECT_SCALE_TIERS:
        for flow in PROJECT_SCALE_FLOW_KINDS:
            assert (scale, flow) in cases


def test_project_scale_matrix_requires_isolation_evidence_and_cleanup() -> None:
    matrix = ProjectScaleMatrix.default()

    assert matrix.requires_isolated_workspace is True
    assert "workspace_bundle" in matrix.required_evidence
    assert "run_events" in matrix.required_evidence
    assert "final_artifacts" in matrix.required_evidence
    assert "project_preflight_approval" in matrix.required_evidence
    assert "self_repair_trace" in matrix.required_evidence
    assert "release_health" in matrix.required_evidence
    assert "delete_workspace" in matrix.cleanup_actions
    assert "cancel_or_archive_probe_runs" in matrix.cleanup_actions
    assert "remove_release_packages" in matrix.cleanup_actions


def test_project_scale_matrix_marks_large_profiles_as_explicit_server_runs() -> None:
    matrix = ProjectScaleMatrix.default()

    ultra_cases = [case for case in matrix.cases if case.scale == "ultra"]
    assert ultra_cases
    assert all(case.requires_bearer_token for case in ultra_cases)
    assert all(case.requires_explicit_server_profile for case in ultra_cases)
    assert any(case.expected_preflight for case in ultra_cases)
    assert any("self_repair" in case.validation_focus for case in ultra_cases)


def test_project_scale_run_requests_are_safe_workspace_write_fixtures() -> None:
    matrix = ProjectScaleMatrix.default()

    for case in matrix.cases:
        request = build_project_scale_run_request(case)
        body = request.body
        assert request.case_id == case.id
        assert body["project_id"] == "project-scale-acceptance"
        assert body["workspace_session_id"] == f"project-scale-{case.scale}-{case.flow}"
        assert body["sandbox_profile"] == "workspace_write"
        assert body["requested_permissions"] == [
            "workspace.read",
            "workspace.write",
            "command.run",
        ]
        assert body["skip_evolution_proposal"] is True
        message = body["message"]
        assert isinstance(message, str)
        assert case.scale in message
        assert case.flow in message


def test_project_scale_run_requests_map_flows_to_execution_modes() -> None:
    cases = {case.id: case for case in ProjectScaleMatrix.default().cases}

    assert build_project_scale_run_request(cases["small:direct"]).body["mode"] == "direct"
    assert build_project_scale_run_request(cases["small:dispatch"]).body["mode"] == "dispatch"
    assert build_project_scale_run_request(cases["small:hybrid"]).body["mode"] == "hybrid"
    assert build_project_scale_run_request(cases["small:multi_agent"]).body["mode"] == "dispatch"
    assert build_project_scale_run_request(cases["small:plugin"]).body["mode"] == "dispatch"
    assert build_project_scale_run_request(cases["small:model_failure"]).body["mode"] == "hybrid"
    assert build_project_scale_run_request(cases["small:self_repair"]).body["mode"] == "hybrid"
    assert (
        build_project_scale_run_request(cases["small:artifact_production"]).body["mode"]
        == "hybrid"
    )


def test_project_scale_run_plan_defaults_to_safe_dry_run_for_all_cases() -> None:
    plan = build_project_scale_run_plan()

    assert plan.dry_run is True
    assert plan.execute is False
    assert plan.case_count == 32
    assert plan.requires_bearer_token is True
    assert plan.required_evidence == (
        "run_details",
        "run_events",
        "workspace_bundle",
        "final_artifacts",
        "project_preflight_approval",
        "self_repair_trace",
        "release_health",
    )
    assert plan.cleanup_actions == (
        "cancel_or_archive_probe_runs",
        "delete_workspace",
        "remove_release_packages",
    )
    assert plan.requests[0].body["workspace_session_id"] == "project-scale-small-direct"


def test_project_scale_run_plan_filters_scale_and_flow() -> None:
    plan = build_project_scale_run_plan(scales=("ultra",), flows=("self_repair", "plugin"))

    assert plan.case_count == 2
    assert [request.case_id for request in plan.requests] == [
        "ultra:plugin",
        "ultra:self_repair",
    ]
    assert all(request.body["project_id"] == "project-scale-acceptance" for request in plan.requests)


def test_project_scale_run_plan_rejects_unknown_filters() -> None:
    try:
        build_project_scale_run_plan(scales=("tiny",))
    except ValueError as error:
        assert "unknown project scale" in str(error)
    else:
        raise AssertionError("unknown scale was accepted")

    try:
        build_project_scale_run_plan(flows=("manual",))
    except ValueError as error:
        assert "unknown project scale flow" in str(error)
    else:
        raise AssertionError("unknown flow was accepted")
