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
    assert "deliverable_quality" in matrix.required_evidence
    assert "agent_standard_verification" in matrix.required_evidence
    assert "discussion_trace" in matrix.required_evidence
    assert "project_preflight_approval" in matrix.required_evidence
    assert "self_repair_trace" in matrix.required_evidence
    assert "plugin_contract" in matrix.required_evidence
    assert "release_health" not in matrix.required_evidence
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
    assert all("deliverable_quality" in case.validation_focus for case in matrix.cases)
    assert all("agent_standard_verification" in case.validation_focus for case in matrix.cases)


def test_project_scale_matrix_has_explicit_capability_validation_flow() -> None:
    matrix = ProjectScaleMatrix.default()
    capability_cases = [case for case in matrix.cases if case.flow == "capability_validation"]

    assert len(capability_cases) == len(PROJECT_SCALE_TIERS)
    for case in capability_cases:
        assert "capability_matrix" in case.validation_focus
        assert "mode_control" in case.validation_focus
        assert "no_silent_downgrade" in case.validation_focus


def test_project_scale_matrix_has_explicit_plugin_contract_flow() -> None:
    matrix = ProjectScaleMatrix.default()
    plugin_cases = [case for case in matrix.cases if case.flow == "plugin"]

    assert len(plugin_cases) == len(PROJECT_SCALE_TIERS)
    for case in plugin_cases:
        assert "plugin_contract" in case.validation_focus
        assert "capability_matrix" in case.validation_focus
        assert "sandbox_policy" in case.validation_focus
        assert "failure_recovery" in case.validation_focus


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
        assert "satisfy the requested requirements" in message
        assert "avoid placeholder or stub-only output" in message
        assert "deliverable_quality" in message
        assert "agent_standard_verification" in message
        if case.flow == "plugin":
            assert "plugin_contract" in message
            assert "manifest discovery" in message
            assert "sandbox and policy boundaries" in message
        assert "Codex/Claude Code verification standards" in message
        assert "plan before implementation" in message
        assert "repair root causes" in message


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
    assert (
        build_project_scale_run_request(cases["small:capability_validation"]).body["mode"]
        == "hybrid"
    )


def test_project_scale_artifact_production_requests_extended_runtime_budget() -> None:
    cases = {case.id: case for case in ProjectScaleMatrix.default().cases}

    body = build_project_scale_run_request(cases["small:artifact_production"]).body

    assert body["runtime_timeout_seconds"] == 900


def test_project_scale_run_plan_defaults_to_safe_dry_run_for_all_cases() -> None:
    plan = build_project_scale_run_plan()

    assert plan.dry_run is True
    assert plan.execute is False
    assert plan.case_count == 36
    assert plan.requires_bearer_token is True
    assert plan.required_evidence == (
        "run_details",
        "run_events",
        "workspace_bundle",
        "final_artifacts",
        "deliverable_quality",
        "agent_standard_verification",
        "discussion_trace",
        "project_preflight_approval",
        "self_repair_trace",
        "plugin_contract",
    )
    assert plan.cleanup_actions == (
        "cancel_or_archive_probe_runs",
        "delete_workspace",
        "remove_release_packages",
    )
    assert plan.requests[0].body["workspace_session_id"] == "project-scale-small-direct"


def test_project_scale_run_plan_payload_includes_validation_focus() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("capability_validation",))

    payload = plan.to_payload()

    assert payload["requests"] == [
        {
            "case_id": "small:capability_validation",
            "validation_focus": [
                "interaction_stability",
                "final_result",
                "deliverable_quality",
                "agent_standard_verification",
                "capability_matrix",
                "mode_control",
                "no_silent_downgrade",
            ],
            "body": {
                "message": (
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=capability_validation. Read constraints first, keep interaction "
                    "stable, use the approved workspace, produce final artifacts, satisfy "
                    "the requested requirements, verify build/test/interaction behavior, avoid "
                    "placeholder or stub-only output, record deliverable_quality and "
                    "agent_standard_verification evidence, include a lightweight implementation "
                    "plan and verification note in the workspace, and follow Codex/Claude Code "
                    "verification standards: read constraints, plan before implementation, verify "
                    "with reproducible evidence, and repair root causes instead of silently "
                    "degrading."
                ),
                "mode": "hybrid",
                "project_id": "project-scale-acceptance",
                "workspace_session_id": "project-scale-small-capability_validation",
                "sandbox_profile": "workspace_write",
                "requested_permissions": [
                    "workspace.read",
                    "workspace.write",
                    "command.run",
                ],
                "skip_evolution_proposal": True,
                "runtime_timeout_seconds": 900,
            },
        }
    ]


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
