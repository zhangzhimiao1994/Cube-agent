from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from agent_hub.harness.project_scale_runner import (
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
)


def load_script() -> Any:
    module_path = Path("scripts/real_user_four_scale_acceptance.py")
    spec = importlib.util.spec_from_file_location(
        "real_user_four_scale_acceptance",
        module_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicArtifactClient:
    def __init__(self, bundle: bytes) -> None:
        self.bundle = bundle
        self.calls: list[tuple[str, str]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        del body, idempotency_key
        self.calls.append((method, path))
        if path.endswith("/files"):
            return {
                "items": [
                    {
                        "path": "README.md",
                        "filename": "README.md",
                        "mime_type": "text/markdown",
                        "size_bytes": 8,
                        "sha256": "1285d7ebaa1def54aa12adb818c4ee1cb1782bf16237ffb86e768002b16e55f9",
                        "download_url": (
                            "/api/v1/workspaces/projects/project-small/sessions/"
                            "conv-small/files/download?path=README.md"
                        ),
                    },
                    {
                        "path": "src/app.py",
                        "filename": "app.py",
                        "mime_type": "text/x-python",
                        "size_bytes": 12,
                        "sha256": "ad64355106bb158b020ecf9702be48f7730fc091dd4bb6a2f092b40393495b3d",
                        "download_url": (
                            "/api/v1/workspaces/projects/project-small/sessions/"
                            "conv-small/files/download?path=src%2Fapp.py"
                        ),
                    },
                ],
                "bundle_download_url": (
                    "/api/v1/workspaces/projects/project-small/sessions/"
                    "conv-small/bundle/download"
                ),
            }
        raise AssertionError(f"unexpected JSON request: {method} {path}")

    def request_bytes(self, method: str, path: str) -> bytes:
        self.calls.append((method, path))
        if path.endswith("/bundle/download"):
            return self.bundle
        if path.endswith("path=README.md"):
            return b"# Readme"
        if path.endswith("path=src%2Fapp.py"):
            return b"print('ok')\n"
        raise AssertionError(f"unexpected bytes request: {method} {path}")


def workspace_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", "# Readme")
        archive.writestr("src/app.py", "print('ok')\n")
    return buffer.getvalue()


def test_auto_scale_plan_keeps_natural_user_request_in_auto_mode() -> None:
    module = load_script()

    plan = module.build_real_user_scale_plan(
        scale="large",
        project_id="uat-large-123",
        project_label="真实用户 large 验收",
        conversation_id="conv-large-123",
        workspace_session_id="conv-large-123",
        route_intent="auto",
    )

    assert plan.benchmark_kind == "capability"
    assert plan.execute is True
    assert plan.case_count == 1
    request = plan.requests[0]
    assert request.case_id == "large:auto"
    assert request.body["mode"] == "auto"
    assert request.body["project_id"] == "uat-large-123"
    assert request.body["project_label"] == "真实用户 large 验收"
    assert request.body["conversation_id"] == "conv-large-123"
    assert request.body["workspace_session_id"] == "conv-large-123"
    assert request.body["runtime_timeout_seconds"] == 1800
    assert "preview.html" in str(request.body["message"])
    assert "interactive website" in str(request.body["message"])
    assert "Use hybrid" not in str(request.body["message"])
    assert "Use dispatch" not in str(request.body["message"])


def test_explicit_mode_capability_plans_use_the_requested_runtime_mode() -> None:
    module = load_script()

    plans = {
        capability: module.build_real_user_scale_plan(
            scale="small",
            project_id=f"uat-mode-{capability}",
            project_label=f"真实用户 {capability} 能力验收",
            conversation_id=f"conv-mode-{capability}",
            workspace_session_id=f"conv-mode-{capability}",
            route_intent=capability,
        )
        for capability in ("direct", "dispatch", "hybrid", "multi_agent")
    }

    assert plans["direct"].requests[0].body["mode"] == "direct"
    assert plans["dispatch"].requests[0].body["mode"] == "dispatch"
    assert plans["hybrid"].requests[0].body["mode"] == "hybrid"
    assert plans["multi_agent"].requests[0].body["mode"] == "dispatch"
    assert {
        plan.requests[0].case_id for plan in plans.values()
    } == {
        "small:direct",
        "small:dispatch",
        "small:hybrid",
        "small:multi_agent",
    }


def test_dynamic_preview_verification_uses_public_lifecycle_and_revokes_content() -> None:
    module = load_script()

    class PreviewClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object] | None]] = []
            self.stopped = False

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object]:
            del idempotency_key
            self.calls.append((method, path, body))
            if method == "POST" and path == "/api/v1/web-previews/start":
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": (
                        "/api/v1/web-previews/preview-small/content/"
                    ),
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "GET" and path == "/api/v1/web-previews/conversations/conv-small":
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": (
                        "/api/v1/web-previews/preview-small/content/"
                    ),
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "POST" and path == "/api/v1/web-previews/preview-small/renew":
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": (
                        "/api/v1/web-previews/preview-small/content/"
                    ),
                    "lease_expires_at": "2026-09-28T09:00:00Z",
                }
            if method == "DELETE" and path == "/api/v1/web-previews/preview-small":
                self.stopped = True
                return {
                    "id": "preview-small",
                    "status": "stopped",
                    "preview_url": None,
                    "lease_expires_at": "2026-09-28T09:00:00Z",
                }
            raise AssertionError(f"unexpected JSON request: {method} {path}")

        def request_bytes(self, method: str, path: str) -> bytes:
            self.calls.append((method, path, None))
            if self.stopped:
                raise RuntimeError("HTTP 404 preview_not_found")
            if path.endswith("/assets/app.js"):
                return b"document.body.dataset.ready = 'true';"
            return (
                b'<!doctype html><title>real preview</title>'
                b'<script src="assets/app.js"></script>'
            )

    client = PreviewClient()
    result = module.verify_dynamic_web_preview(
        client,
        project_id="project-small",
        conversation_id="conv-small",
        workspace_session_id="session-small",
    )

    assert result["status"] == "passed"
    assert result["counted_as_passed"] is True
    assert result["reachable_preview_url"] is True
    assert result["current_preview_matches"] is True
    assert result["renewed"] is True
    assert result["lease_extended"] is True
    assert result["referenced_asset_count"] == 1
    assert result["referenced_assets_loaded"] == 1
    assert result["stopped"] is True
    assert result["revoked_after_stop"] is True
    assert result["browser_interaction"] == "pending_real_device"
    assert client.calls[0] == (
        "POST",
        "/api/v1/web-previews/start",
        {
            "project_id": "project-small",
            "conversation_id": "conv-small",
            "workspace_session_id": "session-small",
        },
    )


def test_dynamic_preview_revocation_requires_explicit_not_found() -> None:
    module = load_script()

    class PreviewClient:
        def __init__(self) -> None:
            self.stopped = False

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object]:
            del body, idempotency_key
            if method == "POST" and path == "/api/v1/web-previews/start":
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": "/api/v1/web-previews/preview-small/content/",
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "GET" and path.endswith("/conversations/conv-small"):
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": "/api/v1/web-previews/preview-small/content/",
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "POST" and path.endswith("/renew"):
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": "/api/v1/web-previews/preview-small/content/",
                    "lease_expires_at": "2026-09-28T09:00:00Z",
                }
            if method == "DELETE":
                self.stopped = True
                return {
                    "id": "preview-small",
                    "status": "stopped",
                    "preview_url": None,
                    "lease_expires_at": "2026-09-28T09:00:00Z",
                }
            raise AssertionError(f"unexpected JSON request: {method} {path}")

        def request_bytes(self, method: str, path: str) -> bytes:
            del method, path
            if self.stopped:
                raise RuntimeError("connection reset by peer")
            return b"<!doctype html><html></html>"

    result = module.verify_dynamic_web_preview(
        PreviewClient(),
        project_id="project-small",
        conversation_id="conv-small",
        workspace_session_id="session-small",
    )

    assert result["status"] == "failed"
    assert result["revoked_after_stop"] is False
    assert any("revocation check failed" in item for item in result["errors"])


def test_public_workspace_verification_checks_list_files_and_zip() -> None:
    module = load_script()
    client = PublicArtifactClient(workspace_zip())

    result = module.verify_public_workspace_artifacts(
        client,
        project_id="project-small",
        workspace_session_id="conv-small",
    )

    assert result["ok"] is True
    assert result["source"] == "public_workspace_api"
    assert result["file_count"] == 2
    assert result["downloaded_file_count"] == 2
    assert result["zip_crc_ok"] is True
    assert result["metadata_matches_zip"] is True
    assert result["unsafe_member_count"] == 0
    assert all("/api/v1/admin/runs" not in path for _, path in client.calls)


def test_report_counts_preview_api_but_keeps_real_device_browser_pending() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="small:artifact_production",
        run_id="run-small",
        status="completed",
        observed_mode="direct",
        final_observed_mode="direct",
        requested_mode="direct",
        effective_scale="small",
        artifact_origin="model_workspace_bundle",
        workspace_bundle_source="embedded_bundle",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "final_artifacts": True,
            "deliverable_quality": True,
            "agent_standard_verification": True,
            "discussion_trace": True,
            "plugin_contract": False,
            "deliverable_repair_trace": False,
            "self_repair_trace": False,
            "project_preflight_approval": True,
            "workspace_bundle": True,
            "cleanup_cancel": True,
            "generated_project_validation": True,
            "requirements_validation": True,
        },
    )

    payload = module.build_case_report(
        scale="small",
        project={"project_id": "project-small"},
        conversation={"conversation_id": "conv-small"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={
            "status": "passed",
            "counted_as_passed": True,
            "reachable_preview_url": True,
            "renewed": True,
            "stopped": True,
            "revoked_after_stop": True,
            "browser_interaction": "pending_real_device",
        },
    )

    assert payload["core_acceptance_ok"] is True
    assert payload["automated_acceptance_complete"] is True
    assert payload["real_device_acceptance_complete"] is False
    assert payload["acceptance_complete"] is False
    assert payload["status"] == "pending_real_device"
    assert payload["dynamic_web_preview"]["status"] == "passed"
    assert payload["dynamic_web_preview"]["counted_as_passed"] is True
    assert payload["success_basis"]["admin_internal_run_data"] is False
    assert payload["artifact_provenance"] == {
        "artifact_origin": "model_workspace_bundle",
        "embedded_bundle_available": True,
        "public_materialized": True,
        "preview_available": True,
        "fixture_origin_allowed": False,
    }


def test_report_accepts_verified_direct_to_hybrid_upgrade_without_direct_coverage() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="large:direct",
        run_id="run-large",
        status="completed",
        observed_mode="direct",
        final_observed_mode="hybrid",
        requested_mode="direct",
        route_reason="project_scale_mode_upgrade",
        mode_source="project_scale_assessment",
        effective_scale="large",
        artifact_origin="tool_workspace_write",
        workspace_bundle_source="public_workspace_api",
        evidence=_passing_evidence(),
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["core_acceptance_ok"] is True
    assert payload["route_policy_ok"] is True
    assert payload["exact_mode_coverage_ok"] is False
    assert payload["coverage_credit"] == "safe_upgrade"
    assert payload["final_observed_mode"] == "hybrid"


def test_report_rejects_missing_effective_scale_instead_of_using_expected_scale() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="small:direct",
        run_id="run-small",
        status="completed",
        observed_mode="direct",
        final_observed_mode="direct",
        requested_mode="direct",
        effective_scale=None,
        artifact_origin="model_workspace_bundle",
        workspace_bundle_source="embedded_bundle",
        evidence=_passing_evidence(),
    )

    payload = module.build_case_report(
        scale="small",
        project={"project_id": "project-small"},
        conversation={"conversation_id": "conv-small"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["effective_scale"] is None
    assert payload["scale_fidelity_ok"] is False
    assert payload["core_acceptance_ok"] is False


def test_report_rejects_unverified_mode_change_and_fixture_artifact_origin() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="large:direct",
        run_id="run-large",
        status="completed",
        observed_mode="direct",
        final_observed_mode="hybrid",
        requested_mode="direct",
        effective_scale="large",
        artifact_origin="builtin_fixture",
        workspace_bundle_source="embedded_bundle",
        evidence=_passing_evidence(),
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["route_policy_ok"] is False
    assert payload["artifact_origin_ok"] is False
    assert payload["core_acceptance_ok"] is False


def test_report_rejects_medium_case_when_effective_scale_drifts_to_large() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="medium:dispatch",
        run_id="run-medium",
        status="completed",
        observed_mode="dispatch",
        final_observed_mode="dispatch",
        requested_mode="dispatch",
        effective_scale="large",
        artifact_origin="tool_workspace_write",
        workspace_bundle_source="public_workspace_api",
        evidence=_passing_evidence(),
    )

    payload = module.build_case_report(
        scale="medium",
        project={"project_id": "project-medium"},
        conversation={"conversation_id": "conv-medium"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["scale_fidelity_ok"] is False
    assert payload["core_acceptance_ok"] is False


def _passing_evidence() -> dict[str, bool]:
    return {
        "run_details": True,
        "run_events": True,
        "terminal_status": True,
        "final_artifacts": True,
        "deliverable_quality": True,
        "agent_standard_verification": True,
        "discussion_trace": True,
        "plugin_contract": False,
        "deliverable_repair_trace": False,
        "self_repair_trace": False,
        "project_preflight_approval": True,
        "workspace_bundle": True,
        "cleanup_cancel": True,
        "generated_project_validation": True,
        "requirements_validation": True,
    }


def _real_device_evidence(execution_id: str) -> dict[str, object]:
    checks = {
        "login": True,
        "project_navigation": True,
        "preview_rendered": True,
        "preview_interaction": True,
        "preview_revoked": True,
    }
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "desktop_browser_interaction": {
            "passed": True,
            "observed_at": "2026-09-28T12:00:00+00:00",
            "viewport": {"width": 1440, "height": 900},
            "checks": checks,
        },
        "mobile_browser_interaction": {
            "passed": True,
            "observed_at": "2026-09-28T12:05:00+00:00",
            "viewport": {"width": 390, "height": 844},
            "checks": checks,
        },
        "cases": {
            "small:direct": {
                "project_id": "project-small",
                "conversation_id": "conv-small",
                "run_id": "run-small-direct",
                "desktop": {
                    "passed": True,
                    "observed_at": "2026-09-28T12:01:00+00:00",
                    "evidence_ref": "desktop-trace-small-direct",
                    "checks": {
                        "preview_rendered": True,
                        "preview_interaction": True,
                        "preview_revoked": True,
                    },
                },
                "mobile": {
                    "passed": True,
                    "observed_at": "2026-09-28T12:06:00+00:00",
                    "evidence_ref": "mobile-trace-small-direct",
                    "checks": {
                        "preview_rendered": True,
                        "preview_interaction": True,
                        "preview_revoked": True,
                    },
                },
            }
        },
    }


def test_finalize_real_device_acceptance_requires_and_merges_both_viewports() -> None:
    module = load_script()
    pending = {
        "schema_version": 1,
        "kind": "real_user_four_scale_acceptance",
        "execution_id": "matrix-123",
        "status": "pending_real_device",
        "core_acceptance_ok": True,
        "automated_acceptance_complete": True,
        "real_device_acceptance_complete": False,
        "acceptance_complete": False,
        "dynamic_web_preview": {"status": "pending_real_device"},
        "cases": [
            {
                "case_id": "small:direct",
                "status": "pending_real_device",
                "core_acceptance_ok": True,
                "real_device_acceptance_complete": False,
                "acceptance_complete": False,
                "project": {"project_id": "project-small"},
                "conversation": {"conversation_id": "conv-small"},
                "run": {"run_id": "run-small-direct"},
                "dynamic_web_preview": {"browser_interaction": "pending_real_device"},
            }
        ],
    }

    completed = module.finalize_real_device_acceptance(
        pending,
        _real_device_evidence("matrix-123"),
    )

    assert completed["status"] == "passed"
    assert completed["real_device_acceptance_complete"] is True
    assert completed["acceptance_complete"] is True
    assert completed["real_device_acceptance"]["counted_as_complete"] is True
    assert completed["cases"][0]["status"] == "passed"
    assert completed["cases"][0]["dynamic_web_preview"]["browser_interaction"] == (
        "verified_by_deployed_real_device_acceptance"
    )
    assert pending["status"] == "pending_real_device"


def test_finalize_real_device_acceptance_rejects_mismatched_execution() -> None:
    module = load_script()
    pending = {
        "kind": "real_user_four_scale_acceptance",
        "execution_id": "matrix-123",
        "core_acceptance_ok": True,
        "automated_acceptance_complete": True,
        "cases": [],
    }

    try:
        module.finalize_real_device_acceptance(
            pending,
            _real_device_evidence("matrix-other"),
        )
    except ValueError as error:
        assert "execution_id" in str(error)
    else:
        raise AssertionError("mismatched real-device evidence was accepted")


def test_finalize_real_device_acceptance_rejects_missing_case_evidence() -> None:
    module = load_script()
    pending = {
        "kind": "real_user_four_scale_acceptance",
        "execution_id": "matrix-123",
        "core_acceptance_ok": True,
        "automated_acceptance_complete": True,
        "cases": [
            {
                "case_id": "small:direct",
                "core_acceptance_ok": True,
                "project": {"project_id": "project-small"},
                "conversation": {"conversation_id": "conv-small"},
                "run": {"run_id": "run-small-direct"},
            },
            {
                "case_id": "medium:hybrid",
                "core_acceptance_ok": True,
                "project": {"project_id": "project-medium"},
                "conversation": {"conversation_id": "conv-medium"},
                "run": {"run_id": "run-medium-hybrid"},
            },
        ],
    }

    with pytest.raises(ValueError, match="case evidence"):
        module.finalize_real_device_acceptance(
            pending,
            _real_device_evidence("matrix-123"),
        )


def test_finalize_cli_writes_complete_report_without_logging_in(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = load_script()
    report_path = tmp_path / "automated.json"
    evidence_path = tmp_path / "real-device.json"
    output_path = tmp_path / "complete.json"
    report_path.write_text(
        json.dumps(
            {
                "kind": "real_user_four_scale_acceptance",
                "execution_id": "matrix-123",
                "core_acceptance_ok": True,
                "automated_acceptance_complete": True,
                "cases": [
                    {
                        "case_id": "small:direct",
                        "core_acceptance_ok": True,
                        "project": {"project_id": "project-small"},
                        "conversation": {"conversation_id": "conv-small"},
                        "run": {"run_id": "run-small-direct"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_path.write_text(
        json.dumps(_real_device_evidence("matrix-123")),
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_PASSWORD", raising=False)

    exit_code = module.main(
        [
            "--finalize-report",
            str(report_path),
            "--real-device-evidence",
            str(evidence_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["acceptance_complete"] is True


def test_restricted_client_blocks_admin_run_success_data() -> None:
    module = load_script()

    class Delegate:
        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object]:
            del method, path, body, idempotency_key
            return {"unexpected": True}

        def request_bytes(self, method: str, path: str) -> bytes:
            del method, path
            return b"unexpected"

    client = module.RealUserAcceptanceClient(Delegate())

    try:
        client.request_json("GET", "/api/v1/admin/runs/run-1")
    except RuntimeError as error:
        assert "forbidden as acceptance evidence" in str(error)
    else:
        raise AssertionError("admin run data was accepted as real-user evidence")

    assert client.blocked_admin_run_requests == ["GET /api/v1/admin/runs/run-1"]


def test_real_user_acceptance_runs_four_auto_scales_and_every_mode_at_every_scale(
    monkeypatch: Any,
) -> None:
    module = load_script()
    bundle = workspace_zip()

    class MatrixDelegate:
        def __init__(self) -> None:
            self.created_projects: list[str] = []
            self.created_conversations: list[str] = []
            self.project_workspace_paths: list[str] = []
            self.conversation_workspace_paths: list[str] = []
            self.stopped_previews: set[str] = set()

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            del idempotency_key
            if method == "GET" and path == "/api/v1/auth/me":
                return {"user_id": "user-test", "tenant_id": "tenant-test", "role": "operator"}
            if method == "POST" and path == "/api/v1/admin/project-workspaces":
                assert body is not None
                project_id = str(body["project_id"])
                self.created_projects.append(project_id)
                self.project_workspace_paths.append(str(body["workspace_path"]))
                return dict(body)
            if method == "POST" and path == "/api/v1/admin/conversations":
                assert body is not None
                conversation_id = str(body["conversation_id"])
                self.created_conversations.append(conversation_id)
                self.conversation_workspace_paths.append(str(body["workspace_path"]))
                return dict(body)
            if method == "POST" and path == "/api/v1/web-previews/start":
                assert body is not None
                conversation_id = str(body["conversation_id"])
                return {
                    "id": f"preview-{conversation_id}",
                    "status": "ready",
                    "preview_url": (
                        f"/api/v1/web-previews/preview-{conversation_id}/content/"
                    ),
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "GET" and path.startswith(
                "/api/v1/web-previews/conversations/"
            ):
                conversation_id = path.rsplit("/", 1)[-1]
                preview_id = f"preview-{conversation_id}"
                return {
                    "id": preview_id,
                    "status": "ready",
                    "preview_url": f"/api/v1/web-previews/{preview_id}/content/",
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "POST" and path.endswith("/renew"):
                preview_id = path.split("/")[-2]
                return {
                    "id": preview_id,
                    "status": "ready",
                    "preview_url": f"/api/v1/web-previews/{preview_id}/content/",
                    "lease_expires_at": "2026-09-28T09:00:00Z",
                }
            if method == "DELETE" and path.startswith("/api/v1/web-previews/"):
                preview_id = path.rsplit("/", 1)[-1]
                self.stopped_previews.add(preview_id)
                return {
                    "id": preview_id,
                    "status": "stopped",
                    "preview_url": None,
                    "lease_expires_at": "2026-09-28T09:00:00Z",
                }
            if method == "GET" and path.endswith("/files"):
                root = path.removesuffix("/files")
                return {
                    "items": [
                        {
                            "path": "README.md",
                            "filename": "README.md",
                            "mime_type": "text/markdown",
                            "size_bytes": 8,
                            "sha256": (
                                "1285d7ebaa1def54aa12adb818c4ee1cb1782bf16237ffb86"
                                "e768002b16e55f9"
                            ),
                            "download_url": f"{root}/files/download?path=README.md",
                        },
                        {
                            "path": "src/app.py",
                            "filename": "app.py",
                            "mime_type": "text/x-python",
                            "size_bytes": 12,
                            "sha256": (
                                "ad64355106bb158b020ecf9702be48f7730fc091dd4bb6a2f"
                                "092b40393495b3d"
                            ),
                            "download_url": f"{root}/files/download?path=src%2Fapp.py",
                        },
                    ],
                    "bundle_download_url": f"{root}/bundle/download",
                }
            raise AssertionError(f"unexpected JSON request: {method} {path} {body}")

        def request_bytes(self, method: str, path: str) -> bytes:
            assert method == "GET"
            if path.endswith("/bundle/download"):
                return bundle
            if path.endswith("path=README.md"):
                return b"# Readme"
            if path.endswith("path=src%2Fapp.py"):
                return b"print('ok')\n"
            if "/api/v1/web-previews/" in path:
                preview_id = path.split("/")[4]
                if preview_id in self.stopped_previews:
                    raise RuntimeError("HTTP 404 preview_not_found")
                if path.endswith("/assets/app.js"):
                    return b"document.body.dataset.ready = 'true';"
                return (
                    b'<!doctype html><title>preview</title>'
                    b'<script src="assets/app.js"></script>'
                )
            raise AssertionError(f"unexpected bytes request: {method} {path}")

    plans: list[Any] = []
    scoped_workspace_paths: list[str] = []

    def execute(plan: Any, client: Any, **kwargs: object) -> ProjectScaleExecutionReport:
        del client
        assert kwargs["auto_approve_capability_requests"] is True
        plans.append(plan)
        scale, route_intent = plan.requests[0].case_id.split(":", 1)
        scoped_workspace_paths.append(
            module._safe_workspace_session_token(
                str(plan.requests[0].body["workspace_session_id"]),
                str(kwargs["execution_id"]),
            )
        )
        result = ProjectScaleCaseResult(
            case_id=plan.requests[0].case_id,
            run_id=f"run-{scale}-{route_intent}",
            status="completed",
            observed_mode=(
                "hybrid"
                if route_intent == "auto" and scale in {"large", "ultra"}
                else "direct"
                if route_intent == "auto"
                else "dispatch"
                if route_intent == "multi_agent"
                else route_intent
            ),
            final_observed_mode=(
                "hybrid"
                if route_intent == "auto" and scale in {"large", "ultra"}
                else "direct"
                if route_intent == "auto"
                else "dispatch"
                if route_intent == "multi_agent"
                else route_intent
            ),
            requested_mode=str(plan.requests[0].body["mode"]),
            effective_scale=scale,
            artifact_origin="tool_workspace_write",
            workspace_bundle_source="public_workspace_api",
            participant_agent_ids=("architect", "implementer", "synthesizer", "tester")
            if route_intent == "multi_agent"
            else (),
            participant_event_kinds=(
                "step.started",
                "step.completed",
                "step.started",
                "step.completed",
                "step.started",
                "step.completed",
                "step.started",
                "step.completed",
            )
            if route_intent == "multi_agent"
            else (),
            participant_event_count=8 if route_intent == "multi_agent" else 0,
            evidence={
                "run_details": True,
                "run_events": True,
                "terminal_status": True,
                "final_artifacts": True,
                "deliverable_quality": True,
                "agent_standard_verification": True,
                "discussion_trace": True,
                "plugin_contract": False,
                "deliverable_repair_trace": False,
                "self_repair_trace": False,
                "project_preflight_approval": True,
                "workspace_bundle": True,
                "cleanup_cancel": True,
                "generated_project_validation": True,
                "requirements_validation": True,
                "multi_agent_participation": route_intent == "multi_agent",
            },
        )
        return ProjectScaleExecutionReport(results=(result,), benchmark_kind="capability")

    monkeypatch.setattr(module, "execute_project_scale_plan", execute)
    delegate = MatrixDelegate()
    client = module.RealUserAcceptanceClient(delegate)

    payload = module.run_real_user_four_scale_acceptance(
        client,
        username="test",
        base_url="http://example.test",
        execution_id="matrix-123",
        wait_seconds=1,
        poll_interval_seconds=0,
        artifact_build_timeout_seconds=1,
    )

    assert len(payload["cases"]) == 20
    assert [
        (case["case_kind"], case["scale"], case["route_intent"])
        for case in payload["cases"]
    ] == [
        ("auto_scale", scale, "auto")
        for scale in ("small", "medium", "large", "ultra")
    ] + [
        ("mode_capability", scale, mode)
        for scale in ("small", "medium", "large", "ultra")
        for mode in ("direct", "dispatch", "hybrid", "multi_agent")
    ]
    assert len(set(delegate.created_projects)) == 20
    assert len(set(delegate.created_conversations)) == 20
    expected_workspace_paths = [
        module._safe_workspace_session_token(
            f"conv-matrix-123-{case_key}",
            f"matrix-123-{case_key}",
        )
        for case_key in (
            *(f"auto-{scale}" for scale in ("small", "medium", "large", "ultra")),
            *(
                f"mode-{scale}-{mode.replace('_', '-')}"
                for scale in ("small", "medium", "large", "ultra")
                for mode in ("direct", "dispatch", "hybrid", "multi_agent")
            ),
        )
    ]
    assert delegate.project_workspace_paths == expected_workspace_paths
    assert delegate.conversation_workspace_paths == expected_workspace_paths
    assert [plan.requests[0].body["mode"] for plan in plans] == [
        *("auto" for _ in range(4)),
        *(
            "dispatch" if mode == "multi_agent" else mode
            for _scale in ("small", "medium", "large", "ultra")
            for mode in ("direct", "dispatch", "hybrid", "multi_agent")
        ),
    ]
    assert scoped_workspace_paths == expected_workspace_paths
    assert [
        plan.requests[0].body["workspace_session_id"] for plan in plans
    ] == expected_workspace_paths
    assert payload["core_acceptance_ok"] is True
    assert payload["automated_acceptance_complete"] is True
    assert payload["real_device_acceptance_complete"] is False
    assert payload["status"] == "pending_real_device"
    assert payload["acceptance_complete"] is False
    assert payload["auto_scale_case_count"] == 4
    assert payload["mode_capability_case_count"] == 16
    assert payload["run_mode"] == "mixed"
    assert payload["auto_scale_run_mode"] == "auto"
    assert payload["mode_capabilities"] == [
        "direct",
        "dispatch",
        "hybrid",
        "multi_agent",
    ]
    assert payload["real_device_acceptance"]["status"] == "pending_real_device"
    assert payload["real_device_acceptance"]["counted_as_complete"] is False
    assert payload["blocked_admin_run_requests"] == []
