import json
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Protocol, Self, cast
from urllib.error import HTTPError

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness import project_scale_runner as project_scale_runner_module
from agent_hub.harness.project_scale import (
    PROJECT_SCALE_FLOW_KINDS,
    PROJECT_SCALE_TIERS,
    ProjectScaleBenchmarkKind,
    ProjectScaleRunPlan,
    build_project_scale_run_plan,
)
from agent_hub.harness.project_scale_runner import (
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
    UrllibAcceptanceClient,
    _acceptance_credentials_from_env,
    _bundle_has_build_test_execution_evidence,
    _deliverable_repair_body,
    _discussion_trace_payload_passes,
    _drop_recovered_workspace_bundle_errors,
    _evaluate_agent_standard_verification,
    _has_agent_standard_verification,
    _has_deliverable_repair_trace,
    _has_self_repair_trace,
    _plugin_contract_payload_passes,
    _safe_zip_member_path,
    _should_attempt_deliverable_repair,
    _workspace_bundle_agent_standard_reasons,
    execute_project_scale_plan,
    format_project_scale_result_line,
)
from agent_hub.runtime.role_planner import RolePlanningRequest

_AGENT_STANDARD_IMPLEMENTATION_PLAN = (
    "- Read before implementation: AGENTS.md workspace rules, HANDOFF current-state index, "
    "and PROJECT_REQUIREMENTS.md.\n"
    "- Skill/rule sources checked before implementation: AGENTS.md workspace rules, "
    "applicable SKILL.md inventory, and no project-specific SKILL.md required for this fixture.\n"
    "- Build project\n"
)


def run_project_scale_runner(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_hub.harness.project_scale_runner", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_project_scale_runner_prints_dry_run_plan_json() -> None:
    result = run_project_scale_runner(
        "--scale", "ultra", "--flow", "self_repair", "--json", "--benchmark-kind", "fixture"
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["execute"] is False
    assert payload["case_count"] == 1
    assert payload["requests"][0]["case_id"] == "ultra:self_repair"
    assert payload["requests"][0]["body"]["workspace_session_id"] == (
        "project-scale-ultra-self_repair"
    )
    assert "run_events" in payload["required_evidence"]
    assert "agent_standard_verification" in payload["required_evidence"]
    assert "discussion_trace" in payload["required_evidence"]
    assert "plugin_contract" in payload["required_evidence"]
    assert "delete_workspace" in payload["cleanup_actions"]


def test_project_scale_runner_defaults_to_full_matrix_plan_json() -> None:
    result = run_project_scale_runner("--json", "--benchmark-kind", "fixture")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    expected_case_ids = [
        f"{scale}:{flow}" for scale in PROJECT_SCALE_TIERS for flow in PROJECT_SCALE_FLOW_KINDS
    ]
    actual_case_ids = [request["case_id"] for request in payload["requests"]]

    assert payload["dry_run"] is True
    assert payload["execute"] is False
    assert payload["case_count"] == len(expected_case_ids)
    assert actual_case_ids == expected_case_ids
    assert "small:direct" in actual_case_ids
    assert "medium:hybrid" in actual_case_ids
    assert "large:plugin" in actual_case_ids
    assert "ultra:capability_validation" in actual_case_ids


def test_fixture_execution_report_does_not_claim_real_capability() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    report = execute_project_scale_plan(plan, FakeAcceptanceClient())

    payload = report.to_payload()
    assert payload["benchmark_kind"] == "fixture"
    assert payload["capability_verified"] is False
    assert "synthetic" in str(payload["verification_scope"])


def test_capability_plan_cli_does_not_trigger_preseed_runtime() -> None:
    result = run_project_scale_runner(
        "--benchmark-kind", "capability", "--scale", "small", "--flow", "direct", "--json"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["benchmark_kind"] == "capability"
    assert payload["capability_verified"] is False
    message = payload["requests"][0]["body"]["message"]
    lowered = message.lower()
    assert "project-scale acceptance fixture" not in lowered
    assert "task management API" in message
    assert "constraints_reading_evidence.json" in message
    assert "AGENTS.md workspace rules" in message
    assert "HANDOFF current-state index" in message
    assert "PROJECT_REQUIREMENTS.md" in message
    assert "applicable SKILL.md" in message


def test_capability_benchmark_can_be_selected_by_acceptance_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HUB_PROJECT_SCALE_BENCHMARK_KIND", "capability")
    result = run_project_scale_runner("--scale", "small", "--flow", "direct", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["benchmark_kind"] == "capability"


def test_capability_repair_preserves_business_request_without_claiming_success() -> None:
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), benchmark_kind="capability"
    )
    body = plan.requests[0].body
    repaired = _deliverable_repair_body(
        body, "small:direct", failed_reasons=("requirements: GET /tasks returns 404",),
        benchmark_kind="capability",
    )
    message = str(repaired["message"])
    assert str(body["message"]) in message
    assert "GET /tasks returns 404" in message
    assert "all true" not in message
    assert len(message) <= 2_000
    RolePlanningRequest(task=message, mode=TaskMode.DIRECT)


def test_capability_execution_enforces_generated_project_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations: list[bytes | None] = []

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        validations.append(bundle)
        return project_scale_runner_module._EvidenceCheck(passed=True, reasons=())

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), execute=True, benchmark_kind="capability"
    )
    report = execute_project_scale_plan(plan, FakeAcceptanceClient())

    assert validations
    assert report.results[0].evidence["generated_project_validation"] is True
    assert report.to_payload()["benchmark_kind"] == "capability"
    assert report.to_payload()["capability_verified"] is False


def test_capability_build_success_cannot_replace_independent_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project_scale_runner_module, "validate_small_task_api",
        lambda root, timeout_seconds: ("requirements: task API missing",),
        raising=False,
    )
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=10,
        requirements_case_id="small:direct",
    )
    assert result.passed is False
    assert "requirements: task API missing" in result.reasons


def test_capability_medium_uses_independent_crm_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project_scale_runner_module,
        "validate_medium_crm_api",
        lambda root, timeout_seconds: ("requirements: tenant isolation missing",),
        raising=False,
    )
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=10,
        requirements_case_id="medium:direct",
    )
    assert result.passed is False
    assert "requirements: tenant isolation missing" in result.reasons


def test_capability_quality_uses_executed_checks_instead_of_claimed_pass_records() -> None:
    bundle = _project_bundle({
        "README.md": "# Task API",
        "src/main.js": _functional_js_source(),
        "tests/main.test.js": _functional_js_test() + "\n// input example: hello world\n",
        "VERIFICATION.md": "Not executed by the author; run independent verification.",
    })
    passed = project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
    failed = project_scale_runner_module._EvidenceCheck(
        passed=False, reasons=("requirements: persistence lost",)
    )
    assert project_scale_runner_module._executed_capability_quality(bundle, passed).passed
    assert not project_scale_runner_module._executed_capability_quality(bundle, failed).passed


@pytest.mark.parametrize("claimed_standard", (False, True))
@pytest.mark.parametrize("validation_failure", (None, "build failed", "requirements failed"))
def test_capability_standard_stays_unverified_after_delivery_validation_and_repair(
    monkeypatch: pytest.MonkeyPatch,
    claimed_standard: bool,
    validation_failure: str | None,
) -> None:
    outcomes = [False, True] if validation_failure else [True, True]

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        passed = outcomes.pop(0)
        return project_scale_runner_module._EvidenceCheck(
            passed=passed, reasons=() if passed else (str(validation_failure),)
        )

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), execute=True, benchmark_kind="capability"
    )
    client = FakeAcceptanceClient(
        status="completed", artifacts=[{"id": "artifact-1"}], agent_standard=claimed_standard
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert len(client.submitted_bodies) == 2
    assert result.evidence["agent_standard_verification"] is False
    assert result.evidence["generated_project_validation"] is True
    assert result.evidence["requirements_validation"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.missing_evidence == ("agent_standard_verification",)
    assert (
        "agent_standard_verification: trusted runtime context/plan evidence unavailable"
        in result.errors
    )
    assert report.ok is False
    assert report.to_payload()["capability_verified"] is False
    assert result.repair_attempted is True
    assert result.run_id == client.repair_run_id
    assert outcomes == []
    if validation_failure:
        assert validation_failure in str(client.submitted_bodies[1]["message"])


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
@pytest.mark.parametrize(
    "failure", (None, "workspace_bundle", "deliverable_quality", "generated_project_validation")
)
def test_process_evidence_only_triggers_fixture_repair(
    benchmark_kind: ProjectScaleBenchmarkKind, failure: str | None
) -> None:
    evidence = {
        "final_artifacts": True,
        "workspace_bundle": True,
        "deliverable_quality": True,
        "generated_project_validation": True,
        "agent_standard_verification": False,
    }
    if failure:
        evidence[failure] = False

    assert _should_attempt_deliverable_repair(
        status="completed", evidence=evidence, case_id="small:direct", benchmark_kind=benchmark_kind
    ) is True


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
@pytest.mark.parametrize(
    "source", ("details", "model_text", "event_flags", "zip_flags", "zip_reading", "zip_plan")
)
def test_agent_standard_self_reports_are_fixture_only(
    benchmark_kind: ProjectScaleBenchmarkKind, source: str
) -> None:
    claim: dict[str, object] = {"agent_standard_verification": {
        "constraints_read": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }}
    details: dict[str, object] | None = None
    events: list[object] | None = None
    files: dict[str, str] = {}
    if source == "details":
        details = claim
    elif source == "model_text":
        details = {"artifact": {"content": {"text": json.dumps(claim)}}}
    elif source == "event_flags":
        events = [{"kind": "tool.completed", "tool_name": "project.generate_zip", "payload": claim}]
    elif source == "zip_flags":
        files["verification.json"] = json.dumps(claim)
    else:
        files["IMPLEMENTATION_PLAN.md"] = _AGENT_STANDARD_IMPLEMENTATION_PLAN if (
            source == "zip_plan"
        ) else "Implement the API."
        files["VERIFICATION.md"] = "All checks passed."
        if source == "zip_reading":
            files["constraints_reading_evidence.json"] = json.dumps({
                "read_before_implementation": True,
                "constraints": ["AGENTS.md", "HANDOFF.md", "PROJECT_REQUIREMENTS.md"],
                "skills": ["SKILL.md"],
            })
    bundle = _project_bundle(files) if files else None

    check = _evaluate_agent_standard_verification(
        details, events, bundle, benchmark_kind=benchmark_kind
    )

    assert check.passed is (benchmark_kind == "fixture")
    assert _has_agent_standard_verification(
        details, events, bundle, benchmark_kind=benchmark_kind
    ) is check.passed
    if benchmark_kind == "capability":
        assert check.reasons
        if source == "event_flags":
            assert "workspace_bundle: missing project bundle" in check.reasons
        else:
            assert (
                "agent_standard_verification: trusted runtime context/plan evidence unavailable"
                in check.reasons
            )


@pytest.mark.parametrize("events", (
    None,
    [],
    [{"kind": "tool.completed", "tool_name": "workspace.read", "payload": {
        "status": "succeeded", "workspace_files": [{"path": "AGENTS.md", "sha256": "a" * 64}],
    }}],
    [{"kind": "checkpoint.saved", "checkpoint": {"state": {"plan_digest": "a" * 64}}}],
))
def test_capability_standard_requires_runtime_contract(events: list[object] | None) -> None:
    check = _evaluate_agent_standard_verification(
        None, events, None, benchmark_kind="capability"
    )

    assert check.passed is False
    assert check.reasons


def test_capability_standard_accepts_public_event_with_workspace_plan_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Capability Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    events: list[object] = [
        {
            "kind": "artifact.created",
            "payload": {
                "agent_standard_verification": {
                    "constraints_read": True,
                    "plan_before_implementation": True,
                    "reproducible_verification": True,
                    "root_cause_repair": True,
                },
            },
        }
    ]

    check = _evaluate_agent_standard_verification(None, events, bundle, benchmark_kind="capability")

    assert check.passed is True
    assert check.reasons == ()


def test_capability_standard_rejects_details_only_self_report() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Capability Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": "- npm run build: passed exit 0; vite build completed\n",
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    details: dict[str, object] = {
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
    }

    check = _evaluate_agent_standard_verification(
        details, None, bundle, benchmark_kind="capability"
    )

    assert check.passed is False
    assert check.reasons == (
        "agent_standard_verification: trusted runtime context/plan evidence unavailable",
    )


def test_execute_project_scale_plan_capability_accepts_public_event_and_workspace_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def validate(bundle: bytes | None, **kwargs: object) -> object:
        return project_scale_runner_module._EvidenceCheck(passed=True, reasons=())

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), execute=True, benchmark_kind="capability"
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    },
                },
            }
        ],
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
    )

    assert report.ok is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert report.to_payload()["capability_verified"] is True


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
def test_execution_report_describes_benchmark_verification_scope(
    benchmark_kind: ProjectScaleBenchmarkKind,
) -> None:
    payload = ProjectScaleExecutionReport(results=(), benchmark_kind=benchmark_kind).to_payload()

    assert payload["capability_verified"] is False
    if benchmark_kind == "capability":
        assert payload["verification_scope"] == (
            "actual build/test and per-case independent business checks; "
            "runtime process evidence unverified"
        )
    else:
        assert payload["verification_scope"] == (
            "synthetic fixture regression; not real project capability or recovery proof"
        )


def test_execution_report_marks_capability_verified_when_all_capability_cases_pass() -> None:
    result = ProjectScaleCaseResult(
        case_id="small:direct",
        run_id="run-1",
        status="completed",
        evidence={
            "agent_standard_verification": True,
            "cleanup_cancel": True,
            "deliverable_quality": True,
            "final_artifacts": True,
            "generated_project_validation": True,
            "requirements_validation": True,
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "workspace_bundle": True,
        },
    )

    payload = ProjectScaleExecutionReport(
        results=(result,), benchmark_kind="capability"
    ).to_payload()

    assert payload["ok"] is True
    assert payload["capability_verified"] is True


def test_plain_execution_output_uses_report_capability_verified(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = ProjectScaleCaseResult(
        case_id="small:direct",
        run_id="run-1",
        status="completed",
        evidence={
            "agent_standard_verification": True,
            "cleanup_cancel": True,
            "deliverable_quality": True,
            "final_artifacts": True,
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "workspace_bundle": True,
        },
    )

    def execute(*args: object, **kwargs: object) -> ProjectScaleExecutionReport:
        return ProjectScaleExecutionReport(results=(result,), benchmark_kind="capability")

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "token")
    monkeypatch.setattr(project_scale_runner_module, "execute_project_scale_plan", execute)

    exit_code = project_scale_runner_module.main(
        ["--benchmark-kind", "capability", "--scale", "small", "--flow", "direct", "--execute"]
    )

    assert exit_code == 0
    assert "benchmark_kind=capability capability_verified=true" in capsys.readouterr().out


def test_project_scale_runner_execute_defaults_to_full_matrix_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_execute_project_scale_plan(
        *args: object, **kwargs: object
    ) -> ProjectScaleExecutionReport:
        captured["plan"] = args[0]
        captured["kwargs"] = kwargs
        return ProjectScaleExecutionReport(results=(), benchmark_kind="fixture")

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(
        project_scale_runner_module,
        "execute_project_scale_plan",
        fake_execute_project_scale_plan,
    )

    exit_code = project_scale_runner_module.main(
        ["--execute", "--json", "--benchmark-kind", "fixture"]
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    plan = captured["plan"]
    assert exit_code == 0
    assert payload["execute"] is True
    assert payload["dry_run"] is False
    assert isinstance(plan, ProjectScaleRunPlan)
    assert plan.execute is True
    assert plan.dry_run is False
    assert plan.case_count == len(PROJECT_SCALE_TIERS) * len(PROJECT_SCALE_FLOW_KINDS)
    assert {request.case_id for request in plan.requests} == {
        f"{scale}:{flow}" for scale in PROJECT_SCALE_TIERS for flow in PROJECT_SCALE_FLOW_KINDS
    }
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["validate_generated_project"] is False


def test_project_scale_runner_execute_can_enable_generated_project_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_execute_project_scale_plan(
        *args: object, **kwargs: object
    ) -> ProjectScaleExecutionReport:
        captured["plan"] = args[0]
        captured["kwargs"] = kwargs
        return ProjectScaleExecutionReport(results=(), benchmark_kind="fixture")

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_HUB_PROJECT_SCALE_VERIFY_ARTIFACT_BUILD", "1")
    monkeypatch.setattr(
        project_scale_runner_module,
        "execute_project_scale_plan",
        fake_execute_project_scale_plan,
    )

    exit_code = project_scale_runner_module.main(
        ["--execute", "--artifact-build-timeout", "9", "--json", "--benchmark-kind", "fixture"]
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 0
    assert payload["execute"] is True
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["validate_generated_project"] is True
    assert kwargs["generated_project_timeout_seconds"] == 9


def test_project_scale_runner_prints_dry_run_plan_focus_in_text() -> None:
    result = run_project_scale_runner(
        "--scale", "small", "--flow", "capability_validation", "--benchmark-kind", "fixture"
    )

    assert result.returncode == 0
    assert (
        "small:capability_validation "
        "focus=interaction_stability,final_result,deliverable_quality,"
        "agent_standard_verification,capability_matrix,mode_control,no_silent_downgrade"
    ) in result.stdout


def test_discussion_trace_rejects_empty_disagreement_evidence() -> None:
    assert (
        _discussion_trace_payload_passes(
            {
                "participants": ["architect", "reviewer"],
                "member_statements": [
                    {"member": "architect", "position": "Plan first."},
                    {"member": "reviewer", "position": "Verify before release."},
                ],
                "disagreements": [],
                "verification_steps": ["Run the acceptance suite."],
                "final_decision": "Proceed after verification.",
            }
        )
        is False
    )


@pytest.mark.parametrize(
    "override",
    [
        {"participants": [""]},
        {"member_statements": [{}]},
        {"member_statements": [{"member": [""], "position": "Plan first."}]},
        {"disagreements": [" "]},
        {"disagreements": [{}]},
        {"verification_steps": [""]},
    ],
)
def test_discussion_trace_rejects_empty_coordination_details(
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "participants": ["architect", "reviewer"],
        "member_statements": [
            {"member": "architect", "position": "Plan first."},
            {"member": "reviewer", "position": "Verify before release."},
        ],
        "disagreements": ["Scope risk needs verification."],
        "verification_steps": ["Run the acceptance suite."],
        "final_decision": "Proceed after verification.",
    }
    payload.update(override)

    assert _discussion_trace_payload_passes(payload) is False


def test_self_repair_trace_rejects_generic_repair_text() -> None:
    assert (
        _has_self_repair_trace(
            [
                {
                    "kind": "message.created",
                    "message": "The plan mentions repair readiness but no repair event occurred.",
                }
            ]
        )
        is False
    )


def test_self_repair_trace_rejects_note_style_marker() -> None:
    assert _has_self_repair_trace([{"kind": "message.self_repair_note"}]) is False


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "repair.classified"},
        {"kind": "runtime.self_repair.completed"},
    ],
)
def test_self_repair_trace_accepts_explicit_repair_events(event: dict[str, object]) -> None:
    assert _has_self_repair_trace([event]) is True


def test_deliverable_repair_trace_rejects_generic_keyword_text() -> None:
    assert (
        _has_deliverable_repair_trace(
            [
                {
                    "kind": "message.created",
                    "message": "Operator asked for deliverable.repair evidence in the prompt.",
                }
            ]
        )
        is False
    )


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "deliverable.repair.completed", "run_id": "repair-run"},
        {"payload": {"event": "deliverable.repair.started"}},
    ],
)
def test_deliverable_repair_trace_accepts_explicit_event_markers(event: object) -> None:
    assert _has_deliverable_repair_trace([event]) is True


def test_plugin_contract_payload_rejects_boolean_only_shell() -> None:
    assert (
        _plugin_contract_payload_passes(
            {
                "manifest_discovered": True,
                "adapter_contract_checked": True,
                "policy_boundary_checked": True,
                "sandbox_profile_checked": True,
                "failure_recovery_checked": True,
            }
        )
        is False
    )


def test_plugin_contract_payload_accepts_auditable_contract_details() -> None:
    assert (
        _plugin_contract_payload_passes(
            {
                "manifest_discovered": True,
                "adapter_contract_checked": True,
                "policy_boundary_checked": True,
                "sandbox_profile_checked": True,
                "failure_recovery_checked": True,
                "manifest_ref": "project-scale-plugin-manifest",
                "adapter_ref": "project.generate_zip",
                "policy_ref": "fail-closed plugin policy",
                "sandbox_ref": "workspace_write",
                "recovery_ref": "install/start failure recovery",
            }
        )
        is True
    )


def test_project_scale_runner_writes_json_report_to_output_path(tmp_path: Path) -> None:
    output_path = tmp_path / "project-scale-report.json"

    result = run_project_scale_runner(
        "--scale",
        "small",
        "--flow",
        "direct",
        "--json",
        "--output",
        str(output_path),
        "--benchmark-kind",
        "fixture",
    )

    assert result.returncode == 0
    stdout_payload = json.loads(result.stdout)
    file_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert file_payload == stdout_payload
    assert file_payload["requests"][0]["case_id"] == "small:direct"


def test_project_scale_runner_rejects_execute_without_token() -> None:
    result = run_project_scale_runner(
        "--execute", "--scale", "small", "--flow", "direct", "--benchmark-kind", "fixture"
    )

    assert result.returncode == 2
    assert (
        "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or "
        "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required for --execute"
    ) in result.stderr
    assert "AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME/PASSWORD" in result.stderr


def test_project_scale_runner_accepts_harness_login_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_TENANT_ID", raising=False)
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME", "admin")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD", "valid-password")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID", "tenant-1")

    assert _acceptance_credentials_from_env() == ("admin", "valid-password", "tenant-1")


def test_project_scale_runner_rejects_unknown_filters() -> None:
    result = run_project_scale_runner("--scale", "tiny", "--json", "--benchmark-kind", "fixture")

    assert result.returncode == 2
    assert "unknown project scale: tiny" in result.stderr


def test_urllib_acceptance_client_reauthenticates_once_on_expired_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, bytes | None]] = []

    class UrlopenRequest(Protocol):
        full_url: str
        data: bytes | None

        def get_header(self, header_name: str) -> str | None: ...

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        del timeout
        request = cast(UrlopenRequest, request)
        url = request.full_url
        auth = request.get_header("Authorization")
        data = request.data
        calls.append((url, auth, data))
        if len(calls) == 1:
            raise HTTPError(
                url,
                401,
                "Unauthorized",
                hdrs=Message(),
                fp=BytesIO(
                    b'{"error":{"code":"invalid_token","message":"invalid access token"}}'
                ),
            )
        if url.endswith("/api/v1/auth/login"):
            assert auth is None
            return Response(
                b'{"access_token":"fresh-token","token_type":"bearer",'
                b'"principal":{"user_id":"11111111-1111-4111-8111-111111111111",'
                b'"tenant_id":"22222222-2222-4222-8222-222222222222",'
                b'"role":"super_admin"}}'
            )
        return Response(b'{"ok":true}')

    monkeypatch.setattr("agent_hub.harness.project_scale_runner.urlopen", fake_urlopen)
    client = UrllibAcceptanceClient(
        base_url="http://agent-hub.local",
        bearer_token="expired-token",
        username="test",
        password="valid password",
    )

    result = client.request_json("GET", "/api/v1/runs/run-1/details")

    assert result == {"ok": True}
    assert [call[1] for call in calls] == [
        "Bearer expired-token",
        None,
        "Bearer fresh-token",
    ]


def test_urllib_acceptance_client_retries_busy_acceptance_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, bytes | None]] = []
    sleeps: list[float] = []

    class UrlopenRequest(Protocol):
        full_url: str
        data: bytes | None

        def get_header(self, header_name: str) -> str | None: ...

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        del timeout
        request = cast(UrlopenRequest, request)
        url = request.full_url
        auth = request.get_header("Authorization")
        data = request.data
        calls.append((url, auth, data))
        if url.endswith("/api/v1/auth/login") and len(calls) == 1:
            raise HTTPError(
                url,
                429,
                "Too Many Requests",
                hdrs=Message(),
                fp=BytesIO(
                    b'{"error":{"code":"authentication_busy","message":"authentication busy"}}'
                ),
            )
        if url.endswith("/api/v1/auth/login"):
            assert auth is None
            return Response(
                b'{"access_token":"fresh-token","token_type":"bearer",'
                b'"principal":{"user_id":"11111111-1111-4111-8111-111111111111",'
                b'"tenant_id":"22222222-2222-4222-8222-222222222222",'
                b'"role":"super_admin"}}'
            )
        return Response(b'{"ok":true}')

    monkeypatch.setattr("agent_hub.harness.project_scale_runner.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "agent_hub.harness.project_scale_runner.time.sleep",
        lambda delay: sleeps.append(delay),
    )
    client = UrllibAcceptanceClient(
        base_url="http://agent-hub.local",
        username="test",
        password="valid password",
    )

    result = client.request_json("GET", "/api/v1/runs/run-1/details")

    assert result == {"ok": True}
    assert [call[0].removeprefix("http://agent-hub.local") for call in calls] == [
        "/api/v1/auth/login",
        "/api/v1/auth/login",
        "/api/v1/runs/run-1/details",
    ]
    assert sleeps == [1.0]


def test_execute_project_scale_plan_submits_run_and_collects_evidence() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="completed", artifacts=[{"id": "artifact-1"}])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.case_count == 1
    result = report.results[0]
    assert result.case_id == "small:direct"
    assert result.run_id == "run-small-direct"
    assert result.evidence == {
        "run_details": True,
        "run_events": True,
        "terminal_status": True,
        "final_artifacts": True,
        "deliverable_quality": True,
        "agent_standard_verification": True,
        "discussion_trace": False,
        "plugin_contract": False,
        "deliverable_repair_trace": False,
        "self_repair_trace": False,
        "project_preflight_approval": False,
        "workspace_bundle": True,
        "cleanup_cancel": True,
    }
    workspace_bundle_path = (
        "/api/v1/workspaces/projects/project-scale-acceptance/"
        "sessions/project-scale-small-direct/bundle/download"
    )
    assert client.calls == [
        ("POST", "/api/v1/runs", "project-scale-small-direct-0"),
        ("GET", "/api/v1/runs/run-small-direct/details", None),
        ("GET", "/api/v1/runs/run-small-direct/events", None),
        ("GET", workspace_bundle_path, None),
    ]


def test_execute_project_scale_plan_accepts_production_events_envelope_and_artifact_ids() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[],
        artifact_ids=["artifact-1"],
        events_envelope=True,
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["run_events"] is True
    assert result.evidence["final_artifacts"] is True


def test_execute_project_scale_plan_reports_case_validation_focus() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("capability_validation",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-capability-validation",
        session_id="project-scale-small-capability_validation",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    results = cast(list[dict[str, object]], report.to_payload()["results"])

    assert results[0]["validation_focus"] == [
        "interaction_stability",
        "final_result",
        "deliverable_quality",
        "agent_standard_verification",
        "capability_matrix",
        "mode_control",
        "no_silent_downgrade",
    ]


def test_execute_project_scale_plan_requires_plugin_contract_evidence() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("plugin",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-plugin",
        session_id="project-scale-small-plugin",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        plugin_contract=False,
        plugin_contract_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-small-plugin-repair"
    assert result.evidence["plugin_contract"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "plugin_contract: missing or incomplete plugin capability contract evidence" in repair_message
    assert "adapter contracts" in repair_message
    assert "sandbox and policy boundaries" in repair_message


def test_deliverable_repair_body_keeps_dispatch_task_bounded_for_plugin_flow() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("plugin",), execute=True)
    body = dict(plan.requests[0].body)
    body["message"] = f"{body['message']}\n" + ("original context " * 220)

    repair_body = _deliverable_repair_body(
        body,
        "small:plugin",
        failed_reasons=(
            "plugin_contract: missing or incomplete plugin capability contract evidence",
            "discussion_trace: missing hybrid/discussion process evidence",
        ),
        benchmark_kind="fixture",
    )

    message = repair_body["message"]
    assert isinstance(message, str)
    assert message == message.strip()
    assert len(message) <= 2_000
    RolePlanningRequest(task=message, mode=TaskMode.DISPATCH)


def test_python_verification_report_counts_split_build_and_unittest_success() -> None:
    verification_text = """
    ## Reproducible build
    bash scripts/build.sh
    build: ok
    The script byte-compiles src, tests, and scripts with python -m compileall -q.

    ## Reproducible tests
    bash scripts/test.sh
    Ran 15 tests
    OK
    """

    assert _bundle_has_build_test_execution_evidence(verification_text) is True


def test_planned_interaction_smoke_does_not_count_as_execution_success() -> None:
    verification_text = """
    - npm run build
    - npm test
    - interaction smoke planned
    """

    assert _bundle_has_build_test_execution_evidence(verification_text) is False


def test_simple_passed_lines_do_not_count_as_reproducible_execution_evidence() -> None:
    verification_text = """
    - npm run build: passed
    - npm test: passed
    """

    assert _bundle_has_build_test_execution_evidence(verification_text) is False


def test_execute_project_scale_plan_fails_plugin_flow_without_contract_after_repair() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("plugin",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-plugin",
        session_id="project-scale-small-plugin",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        plugin_contract=False,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is False
    assert result.evidence["plugin_contract"] is False
    assert "plugin_contract: missing or incomplete plugin capability contract evidence" in result.errors
    assert "plugin_contract" in result.missing_evidence


def test_execute_project_scale_plan_rejects_silent_mode_downgrade() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("capability_validation",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-capability-validation",
        session_id="project-scale-small-capability_validation",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        actual_mode="direct",
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].errors == ("mode_control: requested hybrid got direct",)


def test_project_scale_execution_report_summarizes_failed_evidence_and_focus() -> None:
    report = ProjectScaleExecutionReport(
        benchmark_kind="fixture",
        results=(
            ProjectScaleCaseResult(
                case_id="medium:artifact_production",
                run_id="run-medium-artifact",
                status="completed",
                evidence={
                    "run_details": True,
                    "run_events": True,
                    "terminal_status": True,
                    "final_artifacts": False,
                    "deliverable_quality": False,
                    "agent_standard_verification": False,
                    "workspace_bundle": True,
                    "cleanup_cancel": True,
                },
                validation_focus=("interaction_stability", "final_result", "artifact_integrity"),
            ),
            ProjectScaleCaseResult(
                case_id="ultra:self_repair",
                run_id="run-ultra-self-repair",
                status="failed",
                evidence={
                    "run_details": True,
                    "run_events": True,
                    "terminal_status": True,
                    "project_preflight_approval": True,
                    "workspace_bundle": False,
                    "deliverable_quality": False,
                    "agent_standard_verification": False,
                    "cleanup_cancel": True,
                },
                validation_focus=(
                    "interaction_stability",
                    "final_result",
                    "long_running_control",
                    "project_preflight",
                    "fault_injection",
                    "self_repair",
                ),
                errors=("terminal_status: failed",),
            ),
        )
    )

    payload = report.to_payload()

    assert payload["failed_case_count"] == 2
    assert payload["failed_cases"] == ["medium:artifact_production", "ultra:self_repair"]
    assert payload["missing_evidence_summary"] == {
        "final_artifacts": 2,
        "deliverable_quality": 2,
        "agent_standard_verification": 2,
        "discussion_trace": 2,
        "workspace_bundle": 1,
        "self_repair_trace": 1,
    }
    assert payload["failed_validation_focus"] == [
        "interaction_stability",
        "final_result",
        "artifact_integrity",
        "long_running_control",
        "project_preflight",
        "fault_injection",
        "self_repair",
    ]


def test_execute_project_scale_plan_can_scope_idempotency_to_execution_id() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        session_id="project-scale-small-direct-acceptance-20260914",
    )

    report = execute_project_scale_plan(plan, client, execution_id="acceptance-20260914")

    assert report.ok is True
    assert client.submitted_bodies[0]["workspace_session_id"] == (
        "project-scale-small-direct-acceptance-20260914"
    )
    assert client.calls[0] == (
        "POST",
        "/api/v1/runs",
        "project-scale-small-direct-0-acceptance-20260914",
    )


def test_project_scale_repair_attempted_counts_self_repair_trace() -> None:
    result = ProjectScaleCaseResult(
        case_id="small:self_repair",
        run_id="run-small-self-repair",
        status="completed",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "final_artifacts": True,
            "deliverable_quality": True,
            "agent_standard_verification": True,
            "discussion_trace": True,
            "project_preflight_approval": False,
            "self_repair_trace": True,
            "plugin_contract": False,
            "workspace_bundle": True,
            "cleanup_cancel": True,
            "deliverable_repair_trace": False,
        },
        validation_focus=("fault_injection", "self_repair"),
    )

    assert result.repair_attempted is True
    assert result.to_payload()["repair_attempted"] is True


def test_execute_project_scale_plan_repairs_failed_deliverable_quality() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-medium-artifact-repair"
    assert report.results[0].evidence["final_artifacts"] is True
    assert report.results[0].evidence["workspace_bundle"] is True
    assert report.results[0].evidence["deliverable_quality"] is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert report.results[0].missing_evidence == ()
    assert (
        "POST",
        "/api/v1/runs",
        "project-scale-medium-artifact-production-0-deliverable-repair",
    ) in client.calls


def test_execute_project_scale_plan_repairs_missing_agent_standard_verification() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        agent_standard_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-medium-artifact-repair"
    assert report.results[0].evidence["deliverable_quality"] is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert report.results[0].missing_evidence == ()
    assert len(client.submitted_bodies) == 2
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "agent_standard_verification" in repair_message
    assert "plan_before_implementation" in repair_message
    assert "constraints_reading_evidence.json" in repair_message
    assert "root_cause_repair" in repair_message


def test_execute_project_scale_plan_repairs_missing_build_test_execution_evidence() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        execution_evidence_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-medium-artifact-repair"
    assert report.results[0].evidence["deliverable_quality"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "workspace_bundle: missing build/test execution evidence" in repair_message
    assert "rerun build/test/interaction checks" in repair_message


def test_execute_project_scale_plan_repairs_missing_hybrid_discussion_trace() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("hybrid",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-hybrid",
        session_id="project-scale-small-hybrid",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        discussion_trace_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-small-hybrid-repair"
    assert report.results[0].evidence["discussion_trace"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "discussion_trace: missing hybrid/discussion process evidence" in repair_message
    assert "record discussion_trace" in repair_message


def test_execute_project_scale_plan_explains_quality_and_standard_repair_reasons() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, True),
        agent_standard_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "Previous failed evidence:" in repair_message
    assert "deliverable_quality: missing or incomplete structured quality flags" in repair_message
    assert "workspace_bundle: contains placeholder or stub markers" in repair_message
    assert "agent_standard_verification: missing or incomplete Codex/Claude standard flags" in repair_message
    assert "workspace_bundle: missing implementation plan artifact" in repair_message
    assert "workspace_bundle: missing verification report artifact" in repair_message


def test_execute_project_scale_plan_reports_failed_deliverable_repair_outcome() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, False),
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    payload = result.to_payload()
    assert report.ok is False
    assert result.run_id == "run-medium-artifact-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert payload["repair_attempted"] is True
    assert payload["repair_outcome"] == "failed"
    assert "deliverable_quality: missing or incomplete structured quality flags" in result.errors
    assert format_project_scale_result_line(result) == (
        "medium:artifact_production run_id=run-medium-artifact-repair ok=false "
        "focus=interaction_stability,final_result,deliverable_quality,"
        "agent_standard_verification,artifact_integrity "
        "missing=deliverable_quality errors=7 repair=failed"
    )


def test_execute_project_scale_plan_rejects_scope_mismatch_from_replayed_run() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        response_project_id="project-scale-acceptance",
        response_session_id="project-scale-stale-direct",
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].errors == (
        (
            "run scope mismatch: workspace_session_id expected project-scale-small-direct "
            "got project-scale-stale-direct"
        ),
    )
    assert ("POST", "/api/v1/runs/run-small-direct/cancel", None) in client.calls


def test_execute_project_scale_plan_rejects_detail_scope_mismatch() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        details_run_id="run-stale-direct",
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].errors == (
        "run details scope mismatch: id expected run-small-direct got run-stale-direct",
    )


def test_execute_project_scale_plan_requires_non_empty_event_stream() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="completed", artifacts=[{"id": "artifact-1"}], events=[])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].evidence["run_events"] is False
    assert report.results[0].errors == ("run_events: empty event stream",)


def test_execute_project_scale_plan_rejects_event_scope_mismatch_when_present() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "run.created", "run_id": "run-stale-direct"}],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].errors == (
        "run events scope mismatch: run_id expected run-small-direct got run-stale-direct",
    )


def test_execute_project_scale_plan_records_case_failure_and_continues_cleanup() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(fail_bundle=True)

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].evidence["run_details"] is True
    assert report.results[0].evidence["run_events"] is True
    assert report.results[0].evidence["terminal_status"] is False
    assert report.results[0].evidence["workspace_bundle"] is False
    assert report.results[0].evidence["cleanup_cancel"] is True
    assert report.results[0].errors == ("workspace_bundle: workspace bundle unavailable",)


def test_execute_project_scale_plan_attempts_repair_when_workspace_bundle_is_missing() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert len(client.submitted_bodies) == 2
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert "workspace_bundle: workspace bundle unavailable" in report.results[0].errors


def test_execute_project_scale_plan_drops_stale_workspace_bundle_error_after_repair() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        fail_bundle_once=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-small-direct-repair"
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.errors == ()


def test_drop_recovered_workspace_bundle_errors_keeps_quality_failures() -> None:
    errors = [
        "workspace_bundle: GET /api/v1/workspaces/projects/p/sessions/s/bundle/download failed status=404",
        "workspace_bundle: workspace bundle unavailable",
        "workspace_bundle: missing source files",
        "generated_project_validation: command failed exit=1 command=npm test",
    ]

    _drop_recovered_workspace_bundle_errors(errors)

    assert errors == [
        "workspace_bundle: missing source files",
        "generated_project_validation: command failed exit=1 command=npm test",
    ]


def test_direct_deliverable_repair_prompt_requires_embedded_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    execute_project_scale_plan(plan, client)

    repair_message = str(client.submitted_bodies[1]["message"])
    assert "do not call tools" in repair_message
    assert "workspace_bundle.files" in repair_message
    assert "Markdown file blocks" in repair_message
    assert "credential-like terms" in repair_message
    assert "sk-" in repair_message


def test_execute_project_scale_plan_uses_embedded_workspace_bundle_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    embedded_bundle = {
        "workspace_bundle": {
            "files": {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; vite build completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        }
    }
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": json.dumps(embedded_bundle)}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_reads_quality_flags_from_json_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    embedded_bundle = {
        "deliverable_quality": {
            "requirements_satisfied": True,
            "build_passed": True,
            "tests_passed": True,
            "interactive_checks_passed": True,
            "no_placeholders": True,
            "artifact_integrity": True,
        },
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
        "workspace_bundle": {
            "files": {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION_REPORT.md": (
                    "- npm run build: passed exit 0; vite build completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        },
    }
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality=False,
        agent_standard=False,
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": json.dumps(embedded_bundle)}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_uses_markdown_file_bundle_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    markdown_bundle = """
# Direct Deliverable

### `README.md`

```markdown
# Acceptance Fixture

Implements the requested project scope.
```

### `PROJECT_REQUIREMENTS.md`

```markdown
- Requirement satisfied
- Interaction verified
```

### `IMPLEMENTATION_PLAN.md`

```markdown
__IMPLEMENTATION_PLAN__
```

### `VERIFICATION.md`

```markdown
- npm run build: passed exit 0; vite build completed
- npm test: passed exit 0; 1 test passed
- interaction smoke: passed
```

### `package.json`

```json
{"scripts":{"build":"node --check src/main.js","test":"node --test"}}
```

### `src/main.js`

```js
export function formatGreeting(name) {
  const value = String(name || '').trim();
  if (!value) return 'Hello, guest';
  return `Hello, ${value}`;
}
```

### `tests/main.test.js`

```js
import assert from 'node:assert/strict';
import { formatGreeting } from '../src/main.js';

assert.equal(formatGreeting(' Ada '), 'Hello, Ada');
assert.equal(formatGreeting(''), 'Hello, guest');
```
""".replace("__IMPLEMENTATION_PLAN__", _AGENT_STANDARD_IMPLEMENTATION_PLAN).strip()
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": markdown_bundle}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_recovers_workspace_bundle_from_downloaded_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    downloaded_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "payload": {
                    "artifact_id": "artifact-1",
                    "output": "### `README.md`\n\n```text\n# Acceptance Fixture\n...",
                },
            }
        ],
        artifact_downloads={"artifact-1": downloaded_bundle},
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()
    assert (
        "GET",
        "/api/v1/runs/run-small-direct/artifacts/artifact-1/download",
        None,
    ) in client.calls
    assert [call for call in client.calls if call[0] == "POST" and call[1] == "/api/v1/runs"] == [
        ("POST", "/api/v1/runs", "project-scale-small-direct-0")
    ]


def test_execute_project_scale_plan_reads_quality_from_markdown_metadata_file() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    metadata = {
        "deliverable_quality": {
            "requirements_satisfied": True,
            "build_passed": True,
            "tests_passed": True,
            "interactive_checks_passed": True,
            "no_placeholders": True,
            "artifact_integrity": True,
        },
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
    }
    markdown_bundle = f"""
### `deliverable_metadata.json`

```json
{json.dumps(metadata, sort_keys=True)}
```

### `README.md`

```markdown
# Acceptance Fixture

Implements the requested project scope.
```

### `docs/implementation-plan.md`

```markdown
{_AGENT_STANDARD_IMPLEMENTATION_PLAN}
```

### `docs/verification-report.md`

```markdown
## Reproducible build evidence
npm run build
{{"build": "ok", "exit_code": 0, "tool": "compileall"}}

## Reproducible test evidence
npm test
Ran 1 test
OK
exit 0

npm run test:interaction
- build_passed: true
- tests_passed: true
- interactive_checks_passed: true
```

### `requirements.txt`

```text
# Standard library only.
```

### `direct_ledger/core.py`

```python
def format_greeting(name):
    value = str(name or "").strip()
    if not value:
        return "Hello, guest"
    return f"Hello, {{value}}"
```

### `tests/test_core.py`

```python
from direct_ledger.core import format_greeting

def test_format_greeting():
    assert format_greeting(" Ada ") == "Hello, Ada"
    assert format_greeting("") == "Hello, guest"
```

### `scripts/build.sh`

```bash
python -m compileall direct_ledger tests
```
""".strip()
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality=False,
        agent_standard=False,
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": markdown_bundle}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_requires_interaction_evidence_when_claimed() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    metadata = {
        "deliverable_quality": {
            "requirements_satisfied": True,
            "build_passed": True,
            "tests_passed": True,
            "interactive_checks_passed": True,
            "no_placeholders": True,
            "artifact_integrity": True,
        },
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
    }
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
            ),
            "deliverable_metadata.json": json.dumps(metadata, sort_keys=True),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality=False,
        agent_standard=False,
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing interaction execution evidence" in result.errors


def test_execute_project_scale_plan_rejects_todo_dummy_project_markers() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": (
                "# Acceptance Fixture\n\n"
                "TODO: replace with real implementation after the demo.\n"
            ),
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": "export function runApp() { return 'dummy implementation'; }\n",
            "tests/main.test.js": (
                "import assert from 'node:assert/strict';\n"
                "import { runApp } from '../src/main.js';\n"
                "assert.equal(runApp(), 'ready');\n"
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: contains placeholder or stub markers" in result.errors


def test_execute_project_scale_plan_rejects_constant_only_source_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": "export const status = 'ready';\n",
            "tests/main.test.js": (
                "import assert from 'node:assert/strict';\n"
                "import { status } from '../src/main.js';\n"
                "assert.equal(status, 'ready');\n"
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing meaningful source implementation" in result.errors


def test_execute_project_scale_plan_accepts_small_functional_source_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    functional_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 2 tests passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": (
                "export function formatGreeting(name) {\n"
                "  const value = String(name || '').trim();\n"
                "  if (!value) return 'Hello, guest';\n"
                "  return `Hello, ${value}`;\n"
                "}\n"
            ),
            "tests/main.test.js": (
                "import assert from 'node:assert/strict';\n"
                "import { formatGreeting } from '../src/main.js';\n"
                "assert.equal(formatGreeting(' Ada '), 'Hello, Ada');\n"
                "assert.equal(formatGreeting(''), 'Hello, guest');\n"
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=functional_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True


def test_execute_project_scale_plan_can_validate_generated_project_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; assert Path('package.json').exists(); "
                    "assert Path('src/main.js').exists()"
                ),
            ),
        ),
    )

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_fails_when_generated_project_validation_fails() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=((sys.executable, "-c", "raise SystemExit(7)"),),
    )

    assert report.ok is False
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is False
    assert result.missing_evidence == ("generated_project_validation",)
    assert result.errors == (
        (
            "generated_project_validation: command failed exit=7 command="
            f"{sys.executable} -c raise SystemExit(7)"
        ),
    )


def test_execute_project_scale_plan_repairs_generated_project_validation_failure(
    tmp_path: Path,
) -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    marker = tmp_path / "validation-repaired"
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact-validation",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    f"p=Path({str(marker)!r}); "
                    "sys.exit(0) if p.exists() else (p.write_text('seen'), sys.exit(7))"
                ),
            ),
        ),
    )

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-medium-artifact-validation-repair"
    assert result.evidence["generated_project_validation"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert len(client.submitted_bodies) == 2
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "generated_project_validation: command failed exit=7" in repair_message
    assert "rerun build/test/interaction checks" in repair_message


def test_execute_project_scale_plan_rejects_unsafe_generated_project_zip_paths() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
                "nested/../evil.js": "throw new Error('unsafe');\n",
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
    )

    assert report.ok is False
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is False
    assert result.errors == (
        "generated_project_validation: workspace bundle has unsafe path: nested/../evil.js",
    )


def test_generated_project_zip_path_validation_rejects_backslashes() -> None:
    with pytest.raises(RuntimeError, match=r"unsafe path"):
        _safe_zip_member_path(r"nested\evil.js")


def test_workspace_bundle_agent_standard_requires_constraints_and_skill_rule_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_rejects_generic_reading_claims() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": (
                "- Read before implementation: requirements and rules were reviewed.\n"
                "- Skills checked before implementation: applicable rules reviewed.\n"
                "- Build project\n"
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_rejects_partial_plan_reading_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": (
                "- Read before implementation: HANDOFF current-state index and "
                "PROJECT_REQUIREMENTS.md.\n"
                "- Rules checked before implementation: applicable runtime rules.\n"
                "- Build project\n"
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_rejects_generic_json_reading_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "sources": ["requirements"],
                    "rules": ["general rules"],
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_agent_standard_verification_accepts_public_tool_event_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    event = {
        "kind": "tool.completed",
        "tool_name": "project.generate_zip",
        "payload": {
            "agent_standard_verification": {
                "constraints_read": True,
                "plan_before_implementation": True,
                "reproducible_verification": True,
                "root_cause_repair": True,
            },
        },
    }

    check = _evaluate_agent_standard_verification(None, [event], bundle, benchmark_kind="fixture")

    assert check.passed is True
    assert check.reasons == ()


def test_execute_project_scale_plan_rejects_package_only_shell_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "echo build passed", "test": "echo tests passed"}},
                sort_keys=True,
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing source files" in result.errors


def test_execute_project_scale_plan_rejects_source_bundle_without_test_files() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "echo build passed", "test": "echo tests passed"}},
                sort_keys=True,
            ),
            "src/main.js": "export const status = 'ready';\n",
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing test or verification file path" in result.errors


def test_execute_project_scale_plan_rejects_import_only_test_files() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": "export const status = 'ready';\n",
            "tests/main.test.js": "import { status } from '../src/main.js';\n",
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing meaningful test assertions" in result.errors


def test_execute_project_scale_plan_rejects_failed_terminal_status() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="failed", artifacts=[{"id": "artifact-1"}])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    result = report.results[0]
    assert result.status == "failed"
    assert result.evidence["terminal_status"] is True
    assert result.errors == ("terminal_status: failed",)


def test_execute_project_scale_plan_approves_large_project_preflight() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("large",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-large-direct",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large",
        decision_version=4,
        statuses=("queued", "completed"),
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["project_preflight_approval"] is True
    assert ("POST", "/api/v1/runs/run-large-direct/approve-project-preflight", None) in client.calls


def test_execute_project_scale_plan_approves_waiting_capability_tool() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-artifact",
        session_id="project-scale-small-artifact_production",
        statuses=("waiting_approval", "completed"),
        artifacts=[{"id": "artifact-1"}],
        capability_approval_id="approval_project_zip",
        capability_approval_version=3,
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    assert report.results[0].status == "completed"
    assert (
        "POST",
        "/api/v1/runs/run-small-artifact/approve-capability",
        None,
    ) in client.calls


def test_execute_project_scale_plan_can_wait_for_terminal_status() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("self_repair",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-self-repair",
        session_id="project-scale-small-self_repair",
        statuses=("queued", "completed"),
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "runtime.self_repair.completed"}],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    assert report.results[0].status == "completed"
    assert report.results[0].evidence["terminal_status"] is True
    assert report.results[0].evidence["final_artifacts"] is True
    assert report.results[0].evidence["self_repair_trace"] is True
    assert client.calls.count(("GET", "/api/v1/runs/run-small-self-repair/details", None)) == 2


def test_execute_project_scale_plan_accepts_self_repair_proposal() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("dispatch",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-dispatch",
        session_id="project-scale-small-dispatch",
        statuses=("failed", "completed"),
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "repair.classified", "run_id": "run-small-dispatch"}],
        self_repair_decision_token="repair-token-12345678901234567890",
        self_repair_decision_version=7,
        public_self_repair_proposal=False,
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-small-dispatch-repair"
    assert result.evidence["self_repair_trace"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.evidence["discussion_trace"] is True
    assert (
        "POST",
        "/api/v1/runs/run-small-dispatch/accept-repair",
        None,
    ) in client.calls
    assert result.errors == ()


def test_execute_project_scale_plan_repairs_failed_run_with_artifacts() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("dispatch",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-dispatch",
        session_id="project-scale-small-dispatch",
        statuses=("failed", "completed"),
        artifacts=[{"id": "artifact-1"}],
        discussion_trace_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-small-dispatch-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["discussion_trace"] is True
    assert not any(error.startswith("terminal_status: failed") for error in result.errors)


def test_project_scale_execution_payload_lists_missing_evidence() -> None:
    result = ProjectScaleCaseResult(
        case_id="ultra:self_repair",
        run_id="run-ultra-self-repair",
        status="completed",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "final_artifacts": False,
            "deliverable_quality": False,
            "agent_standard_verification": False,
            "self_repair_trace": False,
            "project_preflight_approval": True,
            "workspace_bundle": True,
            "cleanup_cancel": True,
        },
    )

    payload = result.to_payload()

    assert payload["ok"] is False
    assert payload["required_evidence"] == [
        "run_details",
        "run_events",
        "terminal_status",
        "final_artifacts",
        "deliverable_quality",
        "agent_standard_verification",
        "project_preflight_approval",
        "workspace_bundle",
        "cleanup_cancel",
        "discussion_trace",
        "self_repair_trace",
    ]
    assert payload["missing_evidence"] == [
        "final_artifacts",
        "deliverable_quality",
        "agent_standard_verification",
        "discussion_trace",
        "self_repair_trace",
    ]


def test_project_scale_execution_text_line_lists_failed_case_diagnostics() -> None:
    result = ProjectScaleCaseResult(
        case_id="ultra:self_repair",
        run_id="run-ultra-self-repair",
        status="failed",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "project_preflight_approval": True,
            "workspace_bundle": True,
            "deliverable_quality": True,
            "agent_standard_verification": True,
            "cleanup_cancel": True,
        },
        validation_focus=(
            "interaction_stability",
            "final_result",
            "self_repair",
            "project_preflight_approval",
        ),
        errors=("terminal_status: failed",),
    )

    line = format_project_scale_result_line(result)

    assert line == (
        "ultra:self_repair run_id=run-ultra-self-repair ok=false "
        "focus=interaction_stability,final_result,self_repair,project_preflight_approval "
        "missing=final_artifacts,discussion_trace,self_repair_trace errors=1"
    )


class FakeAcceptanceClient:
    def __init__(
        self,
        *,
        fail_bundle: bool = False,
        fail_bundle_once: bool = False,
        run_id: str = "run-small-direct",
        session_id: str = "project-scale-small-direct",
        create_status: str | None = None,
        decision_token: str | None = None,
        decision_version: int | None = None,
        status: str = "queued",
        statuses: tuple[str, ...] | None = None,
        artifacts: list[dict[str, object]] | None = None,
        events: list[dict[str, object]] | None = None,
        events_envelope: bool = False,
        artifact_ids: list[str] | None = None,
        response_project_id: str | None = None,
        response_session_id: str | None = None,
        details_run_id: str | None = None,
        deliverable_quality: bool = True,
        deliverable_quality_sequence: tuple[bool, ...] | None = None,
        agent_standard: bool = True,
        agent_standard_sequence: tuple[bool, ...] | None = None,
        discussion_trace: bool = True,
        discussion_trace_sequence: tuple[bool, ...] | None = None,
        plugin_contract: bool = True,
        plugin_contract_sequence: tuple[bool, ...] | None = None,
        execution_evidence: bool = True,
        execution_evidence_sequence: tuple[bool, ...] | None = None,
        actual_mode: str | None = None,
        capability_approval_id: str | None = None,
        capability_approval_version: int | None = None,
        self_repair_decision_token: str | None = None,
        self_repair_decision_version: int | None = None,
        public_self_repair_proposal: bool = True,
        workspace_bundle: bytes | None = None,
        artifact_downloads: Mapping[str, bytes] | None = None,
    ) -> None:
        self.fail_bundle = fail_bundle
        self.fail_bundle_once = fail_bundle_once
        self.run_id = run_id
        self.session_id = session_id
        self.create_status = create_status
        self.decision_token = decision_token
        self.decision_version = decision_version
        self.statuses = list(statuses or (status,))
        self.artifacts = artifacts or []
        self.events = [{"kind": "run.created"}] if events is None else events
        self.events_envelope = events_envelope
        self.artifact_ids = artifact_ids or []
        self.response_project_id = response_project_id
        self.response_session_id = response_session_id
        self.details_run_id = details_run_id
        self.deliverable_quality = deliverable_quality
        self.deliverable_quality_sequence = list(deliverable_quality_sequence or ())
        self.current_deliverable_quality = deliverable_quality
        self.agent_standard = agent_standard
        self.agent_standard_sequence = list(agent_standard_sequence or ())
        self.current_agent_standard = agent_standard
        self.discussion_trace = discussion_trace
        self.discussion_trace_sequence = list(discussion_trace_sequence or ())
        self.current_discussion_trace = discussion_trace
        self.plugin_contract = plugin_contract
        self.plugin_contract_sequence = list(plugin_contract_sequence or ())
        self.current_plugin_contract = plugin_contract
        self.execution_evidence = execution_evidence
        self.execution_evidence_sequence = list(execution_evidence_sequence or ())
        self.current_execution_evidence = execution_evidence
        self.actual_mode = actual_mode
        self.capability_approval_id = capability_approval_id
        self.capability_approval_version = capability_approval_version
        self.self_repair_decision_token = self_repair_decision_token
        self.self_repair_decision_version = self_repair_decision_version
        self.public_self_repair_proposal = public_self_repair_proposal
        self.workspace_bundle = workspace_bundle
        self.artifact_downloads = dict(artifact_downloads or {})
        self.repair_run_id = f"{run_id}-repair"
        self.calls: list[tuple[str, str, str | None]] = []
        self.submitted_bodies: list[dict[str, object]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        self.calls.append((method, path, idempotency_key))
        if method == "POST" and path == "/api/v1/runs":
            assert body is not None
            self.submitted_bodies.append(dict(body))
            assert body["workspace_session_id"] == self.session_id
            is_repair = "deliverable-repair" in (idempotency_key or "")
            run_id = self.repair_run_id if is_repair else self.run_id
            response: dict[str, object] = {
                "id": run_id,
                "status": self.create_status or self.statuses[0],
                "project_id": self.response_project_id or body["project_id"],
                "workspace_session_id": self.response_session_id or body["workspace_session_id"],
                "mode": self.actual_mode or body["mode"],
            }
            if self.decision_token is not None:
                response["decision_token"] = self.decision_token
            if self.decision_version is not None:
                response["version"] = self.decision_version
            return response
        if path == f"/api/v1/runs/{self.run_id}/approve-project-preflight":
            assert body == {
                "decision_token": self.decision_token,
                "version": self.decision_version,
            }
            return {"id": self.run_id, "status": self.statuses[0]}
        if path == f"/api/v1/runs/{self.run_id}/approve-capability":
            assert body == {
                "approval_id": self.capability_approval_id,
                "version": self.capability_approval_version,
            }
            return {"id": self.run_id, "status": "queued", "version": self.capability_approval_version}
        if path == f"/api/v1/runs/{self.run_id}/accept-repair":
            assert body == {
                "decision_token": self.self_repair_decision_token,
                "version": self.self_repair_decision_version,
            }
            return {
                "id": self.repair_run_id,
                "status": "queued",
                "project_id": self.submitted_bodies[-1]["project_id"],
                "workspace_session_id": self.submitted_bodies[-1]["workspace_session_id"],
                "mode": self.actual_mode or self.submitted_bodies[-1]["mode"],
            }
        if path == f"/api/v1/admin/runs/{self.run_id}":
            admin_response: dict[str, object] = {
                "id": self.run_id,
                "status": self.statuses[0],
                "version": self.capability_approval_version,
                "explicit_details": {
                    "approval_id": self.capability_approval_id,
                    "version": str(self.capability_approval_version),
                },
            }
            if self.self_repair_decision_token is not None:
                admin_response["decision_token"] = self.self_repair_decision_token
                admin_response["version"] = self.self_repair_decision_version or 1
                admin_response["repair_proposal"] = _self_repair_proposal_fixture()
            return admin_response
        if path in {
            f"/api/v1/runs/{self.run_id}/details",
            f"/api/v1/runs/{self.repair_run_id}/details",
        }:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if path == f"/api/v1/runs/{self.repair_run_id}/details":
                details_run_id = self.repair_run_id
            else:
                details_run_id = self.details_run_id or self.run_id
            deliverable_quality = self._next_deliverable_quality()
            agent_standard = self._next_agent_standard()
            discussion_trace = self._next_discussion_trace()
            plugin_contract = self._next_plugin_contract()
            details_response: dict[str, object] = {
                "id": details_run_id,
                "status": status,
                "artifacts": self.artifacts,
                "artifact_ids": self.artifact_ids,
                "mode": self.actual_mode or self.submitted_bodies[-1]["mode"],
            }
            if deliverable_quality:
                details_response["deliverable_quality"] = {
                    "requirements_satisfied": True,
                    "build_passed": True,
                    "tests_passed": True,
                    "interactive_checks_passed": True,
                    "no_placeholders": True,
                    "artifact_integrity": True,
                }
            if agent_standard:
                details_response["agent_standard_verification"] = {
                    "constraints_read": True,
                    "plan_before_implementation": True,
                    "reproducible_verification": True,
                    "root_cause_repair": True,
                }
            if discussion_trace:
                details_response["discussion_trace"] = {
                    "participants": ["planner", "reviewer"],
                    "member_statements": [
                        {
                            "agent": "planner",
                            "summary": "proposed the implementation path and acceptance gates",
                        },
                        {
                            "agent": "reviewer",
                            "summary": "challenged missing verification evidence before approval",
                        },
                    ],
                    "disagreements": [
                        {
                            "topic": "verification depth",
                            "resolution": "run build, unit, and interaction checks before finalizing",
                        }
                    ],
                    "verification_steps": ["compare requirements", "inspect artifacts", "review tests"],
                    "final_decision": (
                        "planner and reviewer selected the implementation path after evidence review"
                    ),
                }
            if plugin_contract:
                details_response["plugin_contract"] = {
                    "manifest_discovered": True,
                    "adapter_contract_checked": True,
                    "policy_boundary_checked": True,
                    "sandbox_profile_checked": True,
                    "failure_recovery_checked": True,
                    "manifest_ref": "project-scale-plugin-manifest",
                    "adapter_ref": "project.generate_zip",
                    "policy_ref": "fail-closed plugin policy",
                    "sandbox_ref": "workspace_write",
                    "recovery_ref": "install/start failure recovery",
                }
            if (
                path == f"/api/v1/runs/{self.run_id}/details"
                and self.public_self_repair_proposal
            ):
                if self.self_repair_decision_token is not None:
                    details_response["decision_token"] = self.self_repair_decision_token
                if self.self_repair_decision_version is not None:
                    details_response["version"] = self.self_repair_decision_version
                if self.self_repair_decision_token is not None:
                    details_response["repair_proposal"] = _self_repair_proposal_fixture()
            return details_response
        if path in {
            f"/api/v1/runs/{self.run_id}/events",
            f"/api/v1/runs/{self.repair_run_id}/events",
        }:
            events = list(self.events)
            if path == f"/api/v1/runs/{self.repair_run_id}/events":
                events = []
                events.append({"kind": "deliverable.repair.completed", "run_id": self.repair_run_id})
            if self.events_envelope:
                return {"items": events}
            return events
        if path in {
            f"/api/v1/runs/{self.run_id}/cancel",
            f"/api/v1/runs/{self.repair_run_id}/cancel",
        }:
            return {"id": self.run_id, "status": "cancelled"}
        raise AssertionError(f"unexpected JSON request {method} {path}")

    def request_bytes(self, method: str, path: str) -> bytes:
        self.calls.append((method, path, None))
        artifact_prefix = f"/api/v1/runs/{self.run_id}/artifacts/"
        if method == "GET" and path.startswith(artifact_prefix) and path.endswith("/download"):
            artifact_id = path[len(artifact_prefix) : -len("/download")]
            if artifact_id in self.artifact_downloads:
                return self.artifact_downloads[artifact_id]
            raise RuntimeError("artifact download unavailable")
        if self.fail_bundle:
            raise RuntimeError("workspace bundle unavailable")
        if self.fail_bundle_once:
            self.fail_bundle_once = False
            raise RuntimeError("workspace bundle unavailable")
        if self.workspace_bundle is not None:
            return self.workspace_bundle
        self._next_execution_evidence()
        if not self.current_deliverable_quality:
            return _project_bundle({"README.md": "placeholder project"})
        if not self.current_agent_standard:
            return _project_bundle(
                {
                    "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                    "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                    "package.json": json.dumps(
                        {"scripts": {"build": "vite build", "test": "vitest run"}},
                        sort_keys=True,
                    ),
                    "src/main.ts": _functional_ts_source(),
                    "tests/app.test.ts": _functional_ts_test(),
                }
            )
        verification = (
            "- npm run build: passed exit 0; vite build completed\n"
            "- npm test: passed exit 0; 1 test passed\n"
            "- interaction smoke: passed\n"
            if self.current_execution_evidence
            else "- npm run build\n- npm test\n- interaction smoke planned\n"
        )
        return _project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": verification,
                "package.json": json.dumps(
                    {"scripts": {"build": "vite build", "test": "vitest run"}},
                    sort_keys=True,
                ),
                "src/main.ts": _functional_ts_source(),
                "tests/app.test.ts": _functional_ts_test(),
            }
        )

    def _next_deliverable_quality(self) -> bool:
        if self.deliverable_quality_sequence:
            self.current_deliverable_quality = self.deliverable_quality_sequence.pop(0)
        else:
            self.current_deliverable_quality = self.deliverable_quality
        return self.current_deliverable_quality

    def _next_agent_standard(self) -> bool:
        if self.agent_standard_sequence:
            self.current_agent_standard = self.agent_standard_sequence.pop(0)
        else:
            self.current_agent_standard = self.agent_standard
        return self.current_agent_standard

    def _next_discussion_trace(self) -> bool:
        if self.discussion_trace_sequence:
            self.current_discussion_trace = self.discussion_trace_sequence.pop(0)
        else:
            self.current_discussion_trace = self.discussion_trace
        return self.current_discussion_trace

    def _next_plugin_contract(self) -> bool:
        if self.plugin_contract_sequence:
            self.current_plugin_contract = self.plugin_contract_sequence.pop(0)
        else:
            self.current_plugin_contract = self.plugin_contract
        return self.current_plugin_contract

    def _next_execution_evidence(self) -> bool:
        if self.execution_evidence_sequence:
            self.current_execution_evidence = self.execution_evidence_sequence.pop(0)
        else:
            self.current_execution_evidence = self.execution_evidence
        return self.current_execution_evidence


def _project_bundle(files: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _functional_js_source() -> str:
    return (
        "export function formatGreeting(name) {\n"
        "  const value = String(name || '').trim();\n"
        "  if (!value) return 'Hello, guest';\n"
        "  return `Hello, ${value}`;\n"
        "}\n"
    )


def _functional_js_test() -> str:
    return (
        "import assert from 'node:assert/strict';\n"
        "import { formatGreeting } from '../src/main.js';\n"
        "assert.equal(formatGreeting(' Ada '), 'Hello, Ada');\n"
        "assert.equal(formatGreeting(''), 'Hello, guest');\n"
    )


def _functional_ts_source() -> str:
    return (
        "export function formatGreeting(name: string | undefined): string {\n"
        "  const value = String(name || '').trim();\n"
        "  if (!value) return 'Hello, guest';\n"
        "  return `Hello, ${value}`;\n"
        "}\n"
    )


def _functional_ts_test() -> str:
    return (
        "import { formatGreeting } from '../src/main';\n"
        "expect(formatGreeting(' Ada ')).toBe('Hello, Ada');\n"
        "expect(formatGreeting('')).toBe('Hello, guest');\n"
    )


def _self_repair_proposal_fixture() -> dict[str, object]:
    return {
        "kind": "self_repair",
        "failure_kind": "runtime_failure",
        "repair_action": "draft_repair_proposal",
        "requires_approval": True,
        "automatic_execution": False,
        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
        "orchestration_recovery_hint": "retry_blocked_contract_chain",
    }
