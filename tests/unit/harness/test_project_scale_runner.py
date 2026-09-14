import json
import subprocess
import sys

from agent_hub.harness.project_scale import build_project_scale_run_plan
from agent_hub.harness.project_scale_runner import execute_project_scale_plan


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
        "self_repair_trace": False,
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


class FakeAcceptanceClient:
    def __init__(
        self,
        *,
        fail_bundle: bool = False,
        run_id: str = "run-small-direct",
        session_id: str = "project-scale-small-direct",
        status: str = "queued",
        statuses: tuple[str, ...] | None = None,
        artifacts: list[dict[str, object]] | None = None,
        events: list[dict[str, object]] | None = None,
    ) -> None:
        self.fail_bundle = fail_bundle
        self.run_id = run_id
        self.session_id = session_id
        self.statuses = list(statuses or (status,))
        self.artifacts = artifacts or []
        self.events = events or [{"kind": "run.created"}]
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
            return {"id": self.run_id, "status": self.statuses[0]}
        if path == f"/api/v1/runs/{self.run_id}/details":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"id": self.run_id, "status": status, "artifacts": self.artifacts}
        if path == f"/api/v1/runs/{self.run_id}/events":
            return list(self.events)
        if path == f"/api/v1/runs/{self.run_id}/cancel":
            return {"id": self.run_id, "status": "cancelled"}
        raise AssertionError(f"unexpected JSON request {method} {path}")

    def request_bytes(self, method: str, path: str) -> bytes:
        self.calls.append((method, path, None))
        if self.fail_bundle:
            raise RuntimeError("workspace bundle unavailable")
        return b"PK\x03\x04"
