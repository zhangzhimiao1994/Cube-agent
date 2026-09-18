import json
import subprocess
import sys
import zipfile
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Protocol, Self, cast
from urllib.error import HTTPError

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.project_scale import build_project_scale_run_plan
from agent_hub.harness.project_scale_runner import (
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
    UrllibAcceptanceClient,
    _bundle_has_build_test_execution_evidence,
    _deliverable_repair_body,
    _discussion_trace_payload_passes,
    _has_deliverable_repair_trace,
    _has_self_repair_trace,
    _plugin_contract_payload_passes,
    execute_project_scale_plan,
    format_project_scale_result_line,
)
from agent_hub.runtime.role_planner import RolePlanningRequest


def run_project_scale_runner(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_hub.harness.project_scale_runner", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_project_scale_runner_prints_dry_run_plan_json() -> None:
    result = run_project_scale_runner("--scale", "ultra", "--flow", "self_repair", "--json")

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


def test_project_scale_runner_prints_dry_run_plan_focus_in_text() -> None:
    result = run_project_scale_runner("--scale", "small", "--flow", "capability_validation")

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
    )

    assert result.returncode == 0
    stdout_payload = json.loads(result.stdout)
    file_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert file_payload == stdout_payload
    assert file_payload["requests"][0]["case_id"] == "small:direct"


def test_project_scale_runner_rejects_execute_without_token() -> None:
    result = run_project_scale_runner("--execute", "--scale", "small", "--flow", "direct")

    assert result.returncode == 2
    assert "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or" in result.stderr


def test_project_scale_runner_rejects_unknown_filters() -> None:
    result = run_project_scale_runner("--scale", "tiny", "--json")

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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("plugin",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("plugin",), execute=True)
    body = dict(plan.requests[0].body)
    body["message"] = f"{body['message']}\n" + ("original context " * 220)

    repair_body = _deliverable_repair_body(
        body,
        "small:plugin",
        failed_reasons=(
            "plugin_contract: missing or incomplete plugin capability contract evidence",
            "discussion_trace: missing hybrid/discussion process evidence",
        ),
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


def test_execute_project_scale_plan_fails_plugin_flow_without_contract_after_repair() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("plugin",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="completed", artifacts=[{"id": "artifact-1"}])

    report = execute_project_scale_plan(plan, client, execution_id="acceptance-20260914")

    assert report.ok is True
    assert client.calls[0] == (
        "POST",
        "/api/v1/runs",
        "project-scale-small-direct-0-acceptance-20260914",
    )


def test_execute_project_scale_plan_repairs_failed_deliverable_quality() -> None:
    plan = build_project_scale_run_plan(scales=("medium",), flows=("artifact_production",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("medium",), flows=("artifact_production",), execute=True)
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
    assert "root_cause_repair" in repair_message


def test_execute_project_scale_plan_repairs_missing_build_test_execution_evidence() -> None:
    plan = build_project_scale_run_plan(scales=("medium",), flows=("artifact_production",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("hybrid",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("medium",), flows=("artifact_production",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("medium",), flows=("artifact_production",), execute=True)
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
        "missing=deliverable_quality,agent_standard_verification errors=7 repair=failed"
    )


def test_execute_project_scale_plan_rejects_scope_mismatch_from_replayed_run() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="completed", artifacts=[{"id": "artifact-1"}], events=[])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].evidence["run_events"] is False
    assert report.results[0].errors == ("run_events: empty event stream",)


def test_execute_project_scale_plan_rejects_event_scope_mismatch_when_present() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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


def test_direct_deliverable_repair_prompt_requires_embedded_bundle() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
    embedded_bundle = {
        "workspace_bundle": {
            "files": {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
                "VERIFICATION.md": (
                    "- npm run build: passed\n"
                    "- npm test: passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": "export const status = 'ready';\n",
                "tests/main.test.js": "import { status } from '../src/main.js';\n",
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
                "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
                "VERIFICATION_REPORT.md": (
                    "- npm run build: passed\n"
                    "- npm test: passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": "export const status = 'ready';\n",
                "tests/main.test.js": "import { status } from '../src/main.js';\n",
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
- Read constraints
- Build project
```

### `VERIFICATION.md`

```markdown
- npm run build: passed
- npm test: passed
- interaction smoke: passed
```

### `package.json`

```json
{"scripts":{"build":"node --check src/main.js","test":"node --test"}}
```

### `src/main.js`

```js
export const status = 'ready';
```

### `tests/main.test.js`

```js
import { status } from '../src/main.js';
```
""".strip()
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


def test_execute_project_scale_plan_reads_quality_from_markdown_metadata_file() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
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
- Read constraints
- Build project
```

### `docs/verification-report.md`

```markdown
## Reproducible build evidence
npm run build
{{"build": "ok"}}

## Reproducible test evidence
npm test
Expected deterministic result: all cases pass.

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
def ready():
    return True
```

### `tests/test_core.py`

```python
from direct_ledger.core import ready
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


def test_execute_project_scale_plan_rejects_failed_terminal_status() -> None:
    plan = build_project_scale_run_plan(scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="failed", artifacts=[{"id": "artifact-1"}])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    result = report.results[0]
    assert result.status == "failed"
    assert result.evidence["terminal_status"] is True
    assert result.errors == ("terminal_status: failed",)


def test_execute_project_scale_plan_approves_large_project_preflight() -> None:
    plan = build_project_scale_run_plan(scales=("large",), flows=("direct",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("artifact_production",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("self_repair",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("dispatch",), execute=True)
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
    plan = build_project_scale_run_plan(scales=("small",), flows=("dispatch",), execute=True)
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
    ) -> None:
        self.fail_bundle = fail_bundle
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
        if self.fail_bundle:
            raise RuntimeError("workspace bundle unavailable")
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
                    "src/main.ts": "export const status = 'ready';\n",
                    "tests/app.test.ts": "import { status } from '../src/main';\n",
                }
            )
        verification = (
            "- npm run build: passed\n- npm test: passed\n- interaction smoke: passed\n"
            if self.current_execution_evidence
            else "- npm run build\n- npm test\n- interaction smoke planned\n"
        )
        return _project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
                "VERIFICATION.md": verification,
                "package.json": json.dumps(
                    {"scripts": {"build": "vite build", "test": "vitest run"}},
                    sort_keys=True,
                ),
                "src/main.ts": "export const status = 'ready';\n",
                "tests/app.test.ts": "import { status } from '../src/main';\n",
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
