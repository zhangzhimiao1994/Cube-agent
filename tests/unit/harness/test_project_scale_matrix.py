from agent_hub.harness.project_scale import (
    PROJECT_SCALE_FLOW_KINDS,
    PROJECT_SCALE_TIERS,
    ProjectScaleMatrix,
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
