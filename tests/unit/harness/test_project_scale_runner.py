import json
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import cast

from agent_hub.harness.project_scale import build_project_scale_run_plan
from agent_hub.harness.project_scale_runner import (
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
    execute_project_scale_plan,
    format_project_scale_result_line,
)


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
    assert "delete_workspace" in payload["cleanup_actions"]


def test_project_scale_runner_prints_dry_run_plan_focus_in_text() -> None:
    result = run_project_scale_runner("--scale", "small", "--flow", "capability_validation")

    assert result.returncode == 0
    assert (
        "small:capability_validation "
        "focus=interaction_stability,final_result,deliverable_quality,"
        "agent_standard_verification,capability_matrix,mode_control,no_silent_downgrade"
    ) in result.stdout


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
    assert "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN is required for --execute" in result.stderr


def test_project_scale_runner_rejects_unknown_filters() -> None:
    result = run_project_scale_runner("--scale", "tiny", "--json")

    assert result.returncode == 2
    assert "unknown project scale: tiny" in result.stderr


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
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert report.results[0].missing_evidence == ()
    assert (
        "POST",
        "/api/v1/runs",
        "project-scale-medium-artifact-production-0-deliverable-repair",
    ) in client.calls


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
        "project_preflight_approval",
        "workspace_bundle",
        "cleanup_cancel",
        "self_repair_trace",
    ]
    assert payload["missing_evidence"] == [
        "final_artifacts",
        "deliverable_quality",
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
        "missing=final_artifacts,self_repair_trace errors=1"
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
        response_project_id: str | None = None,
        response_session_id: str | None = None,
        details_run_id: str | None = None,
        deliverable_quality: bool = True,
        deliverable_quality_sequence: tuple[bool, ...] | None = None,
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
        self.response_project_id = response_project_id
        self.response_session_id = response_session_id
        self.details_run_id = details_run_id
        self.deliverable_quality = deliverable_quality
        self.deliverable_quality_sequence = list(deliverable_quality_sequence or ())
        self.current_deliverable_quality = deliverable_quality
        self.repair_run_id = f"{run_id}-repair"
        self.calls: list[tuple[str, str, str | None]] = []

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
            assert body["workspace_session_id"] == self.session_id
            is_repair = "deliverable-repair" in (idempotency_key or "")
            run_id = self.repair_run_id if is_repair else self.run_id
            response: dict[str, object] = {
                "id": run_id,
                "status": self.create_status or self.statuses[0],
                "project_id": self.response_project_id or body["project_id"],
                "workspace_session_id": self.response_session_id or body["workspace_session_id"],
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
            details_response: dict[str, object] = {
                "id": details_run_id,
                "status": status,
                "artifacts": self.artifacts,
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
            return details_response
        if path in {
            f"/api/v1/runs/{self.run_id}/events",
            f"/api/v1/runs/{self.repair_run_id}/events",
        }:
            events = list(self.events)
            if path == f"/api/v1/runs/{self.repair_run_id}/events":
                events.append({"kind": "deliverable.repair.completed", "run_id": self.repair_run_id})
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
        if not self.current_deliverable_quality:
            return _project_bundle({"README.md": "placeholder project"})
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

    def _next_deliverable_quality(self) -> bool:
        if self.deliverable_quality_sequence:
            self.current_deliverable_quality = self.deliverable_quality_sequence.pop(0)
        else:
            self.current_deliverable_quality = self.deliverable_quality
        return self.current_deliverable_quality


def _project_bundle(files: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()
