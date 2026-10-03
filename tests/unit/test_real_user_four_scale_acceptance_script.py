from __future__ import annotations

import copy
import importlib.util
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest

from agent_hub.harness.project_scale_runner import (
    AcceptanceHTTPError,
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
    _idempotency_key,
    execute_project_scale_plan,
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


@pytest.mark.parametrize(
    "raw",
    (
        '{"cases":{"small:auto":{"passed":false},"small:auto":{"passed":true}}}',
        '{"execution_id":"old","execution_id":"new"}',
        '{"cases":{"small:auto":{"desktop":{"passed":false,"passed":true}}}}',
    ),
)
def test_evidence_json_rejects_duplicate_keys(tmp_path: Path, raw: str) -> None:
    module = load_script()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        module._read_json_mapping(str(evidence))


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
                    "/api/v1/workspaces/projects/project-small/sessions/conv-small/bundle/download"
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
    assert {plan.requests[0].case_id for plan in plans.values()} == {
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
                    "preview_url": ("/api/v1/web-previews/preview-small/content/"),
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "GET" and path == "/api/v1/web-previews/conversations/conv-small":
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": ("/api/v1/web-previews/preview-small/content/"),
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "POST" and path == "/api/v1/web-previews/preview-small/renew":
                return {
                    "id": "preview-small",
                    "status": "ready",
                    "preview_url": ("/api/v1/web-previews/preview-small/content/"),
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
                b'<!doctype html><title>real preview</title><script src="assets/app.js"></script>'
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


def test_report_uses_initial_auto_route_when_deliverable_repair_runs_direct() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="large:auto",
        run_id="run-large-repair",
        status="completed",
        observed_mode="hybrid",
        final_observed_mode="direct",
        requested_mode="direct",
        effective_scale="large",
        artifact_origin="incremental_workspace_delivery",
        workspace_bundle_source="public_workspace_api",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
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
    assert payload["exact_mode_coverage_ok"] is True
    assert payload["route_observed_mode"] == "hybrid"
    assert payload["initial_observed_mode"] == "hybrid"
    assert payload["final_observed_mode"] == "direct"


def test_report_does_not_let_auto_repair_mode_hide_wrong_initial_route() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="large:auto",
        run_id="run-large-repair",
        status="completed",
        observed_mode="direct",
        final_observed_mode="hybrid",
        requested_mode="hybrid",
        effective_scale="large",
        artifact_origin="incremental_workspace_delivery",
        workspace_bundle_source="public_workspace_api",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["core_acceptance_ok"] is False
    assert payload["route_policy_ok"] is False
    assert payload["route_observed_mode"] == "direct"


def test_report_keeps_explicit_mode_coverage_bound_to_final_run() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="large:hybrid",
        run_id="run-large-repair",
        status="completed",
        observed_mode="hybrid",
        final_observed_mode="direct",
        requested_mode="direct",
        effective_scale="large",
        artifact_origin="incremental_workspace_delivery",
        workspace_bundle_source="public_workspace_api",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["core_acceptance_ok"] is False
    assert payload["route_policy_ok"] is False
    assert payload["route_observed_mode"] == "direct"


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


_FINALIZER_CASE_IDS = tuple(
    f"{scale}:{route}"
    for scale in ("small", "medium", "large", "ultra")
    for route in ("auto", "direct", "dispatch", "hybrid", "multi_agent")
)


def _model_event(run_id: str, logical_model: str = "deepseek-backup") -> dict[str, Any]:
    from agent_hub.runs.repository import _public_event_payload

    return _public_event_payload(
        {
            "kind": "model.completed",
            "run_id": run_id,
            "sequence": 1,
            "payload": {
                "logical_model": logical_model,
                "attempted_logical_models": [logical_model],
            },
        }
    )


def _scope_evidence(run_id: str, logical_model: str) -> dict[str, Any]:
    return {
        "source": "public_run_events",
        "original_run_id": run_id,
        "result_run_id": run_id,
        "accepted_repair_run_ids": [],
        "ok": True,
        "errors": [],
        "runs": [
            {
                "run_id": run_id,
                "status": "completed",
                "events_endpoint": f"/api/v1/runs/{quote(run_id, safe='')}/events",
                "model_events": [_model_event(run_id, logical_model)],
            }
        ],
    }


def _pending_automated_report(logical_model: str | None = None) -> dict[str, Any]:
    module = load_script()
    cases = []
    for case_id in _FINALIZER_CASE_IDS:
        scale, route = case_id.split(":")
        case_key = f"auto-{scale}" if route == "auto" else f"mode-{scale}-{route.replace('_', '-')}"
        scope = f"matrix-123-{case_key}"
        project_id, conversation_id = f"uat-{scope}", f"conv-{scope}"
        root = f"/api/v1/workspaces/projects/{project_id}/sessions/{conversation_id}"
        mode = (
            "hybrid"
            if route == "auto" and scale in {"large", "ultra"}
            else "direct"
            if route == "auto"
            else "dispatch"
            if route == "multi_agent"
            else route
        )
        result = ProjectScaleCaseResult(
            case_id=case_id,
            run_id=f"run-{case_id}",
            status="completed",
            observed_mode=mode,
            final_observed_mode=mode,
            requested_mode="dispatch" if route == "multi_agent" else route,
            effective_scale=scale,
            artifact_origin="tool_workspace_write",
            workspace_bundle_source="public_workspace_api",
            participant_agent_ids=("architect", "implementer", "tester", "synthesizer")
            if route == "multi_agent"
            else (),
            participant_event_kinds=("step.started", "step.completed") * 4
            if route == "multi_agent"
            else (),
            participant_event_count=8 if route == "multi_agent" else 0,
            evidence={**_passing_evidence(), "multi_agent_participation": route == "multi_agent"},
        )
        cases.append(
            module.build_case_report(
                scale=scale,
                project={"project_id": project_id, "workspace_path": conversation_id},
                conversation={
                    "conversation_id": conversation_id,
                    "project_id": project_id,
                    "workspace_path": conversation_id,
                },
                result=result,
                public_artifacts={
                    "ok": True,
                    "source": "public_workspace_api",
                    "admin_internal_run_data_used": False,
                    "files_endpoint": f"{root}/files",
                    "bundle_endpoint": f"{root}/bundle/download",
                    "file_count": 2,
                    "downloaded_file_count": 2,
                    "zip_member_count": 2,
                    "zip_size_bytes": 200,
                    "zip_crc_ok": True,
                    "metadata_matches_zip": True,
                    "unsafe_member_count": 0,
                    "zip_sha256": "a" * 64,
                    "errors": [],
                },
                dynamic_web_preview={
                    "status": "passed",
                    "counted_as_passed": True,
                    "reachable_preview_url": True,
                    "current_preview_matches": True,
                    "renewed": True,
                    "lease_extended": True,
                    "stopped": True,
                    "revoked_after_stop": True,
                    "referenced_asset_count": 1,
                    "referenced_assets_loaded": 1,
                    "content_size_bytes": 100,
                    "capability_token_retained": False,
                    "browser_interaction": "pending_real_device",
                    "errors": [],
                },
            )
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "real_user_four_scale_acceptance",
        "execution_id": "matrix-123",
        "base_url": "http://example.test",
        "benchmark_kind": "capability",
        "actor": {"principal": {"user_id": "user-test", "tenant_id": "tenant-test"}},
        "execution_identity": {
            "execution_id": "matrix-123",
            "base_url": "http://example.test",
            "user_id": "user-test",
            "tenant_id": "tenant-test",
        },
        "status": "pending_real_device",
        "core_acceptance_ok": True,
        "automated_acceptance_complete": True,
        "real_device_acceptance_complete": False,
        "acceptance_complete": False,
        "case_count": 20,
        "core_passed_case_count": 20,
        "failed_case_count": 0,
        "dynamic_web_preview": {"status": "pending_real_device"},
        "cases": cases,
    }
    if logical_model is not None:
        profile = {"direct_model": logical_model, "allowed_models": [logical_model]}
        report["model_profile"] = copy.deepcopy(profile)
        report["execution_identity"]["model_profile"] = copy.deepcopy(profile)
        for case in cases:
            case["model_profile"] = copy.deepcopy(profile)
            case["model_scope_evidence"] = _scope_evidence(case["run"]["run_id"], logical_model)
            case["success_basis"]["model_scope"] = True
    return report


def _real_device_evidence(execution_id: str, logical_model: str | None = None) -> dict[str, Any]:
    scopes = {case["case_id"]: case for case in _pending_automated_report()["cases"]}
    checks = {
        "login": True,
        "project_navigation": True,
        "preview_rendered": True,
        "preview_interaction": True,
        "preview_revoked": True,
    }
    evidence = {
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
            case_id: {
                "project_id": scopes[case_id]["project"]["project_id"],
                "conversation_id": scopes[case_id]["conversation"]["conversation_id"],
                "run_id": f"run-{case_id}",
                "desktop": {
                    "passed": True,
                    "observed_at": "2026-09-28T12:01:00+00:00",
                    "evidence_ref": f"desktop-trace-{case_id}",
                    "checks": {
                        "preview_rendered": True,
                        "preview_interaction": True,
                        "preview_revoked": True,
                    },
                },
                "mobile": {
                    "passed": True,
                    "observed_at": "2026-09-28T12:06:00+00:00",
                    "evidence_ref": f"mobile-trace-{case_id}",
                    "checks": {
                        "preview_rendered": True,
                        "preview_interaction": True,
                        "preview_revoked": True,
                    },
                },
            }
            for case_id in _FINALIZER_CASE_IDS
        },
    }
    if logical_model is not None:
        evidence["model_profile"] = {
            "direct_model": logical_model,
            "allowed_models": [logical_model],
        }
    return evidence


def test_finalize_real_device_acceptance_requires_and_merges_both_viewports() -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    pending["cases"].reverse()
    pending_before = copy.deepcopy(pending)
    evidence_before = copy.deepcopy(evidence)

    completed = module.finalize_real_device_acceptance(
        pending,
        evidence,
    )

    assert completed["status"] == "passed"
    assert completed["real_device_acceptance_complete"] is True
    assert completed["acceptance_complete"] is True
    assert completed["real_device_acceptance"]["counted_as_complete"] is True
    assert len(completed["cases"]) == 20
    assert set(completed["real_device_acceptance"]["cases"]) == set(_FINALIZER_CASE_IDS)
    for case in completed["cases"]:
        assert case["status"] == "passed"
        assert case["acceptance_complete"] is True
        assert case["real_device_acceptance_complete"] is True
        assert case["real_device_evidence"] == evidence["cases"][case["case_id"]]
        assert case["dynamic_web_preview"]["browser_interaction"] == (
            "verified_by_deployed_real_device_acceptance"
        )
    assert pending == pending_before
    assert evidence == evidence_before


def test_finalize_real_device_acceptance_rejects_mismatched_execution() -> None:
    module = load_script()
    pending = _pending_automated_report()

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
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    del evidence["cases"]["medium:hybrid"]

    with pytest.raises(ValueError, match="case evidence"):
        module.finalize_real_device_acceptance(
            pending,
            evidence,
        )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "errors", ["case failed"]),
        (None, "automated_acceptance_complete", False),
        (None, "route_policy_ok", False),
        (None, "coverage_credit", "safe_upgrade"),
        ("run", "status", "failed"),
        ("run", "ok", False),
        ("run", "errors", ["provider unavailable"]),
        ("run", "missing_evidence", ["run_events"]),
        ("run", "evidence", {}),
        ("run", "effective_scale", "ultra"),
        ("run", "final_observed_mode", "unknown"),
        ("run", "artifact_origin", "builtin_fixture"),
        ("build_and_test", "status", "failed"),
        ("build_and_test", "requirements_validation", False),
        ("public_artifacts", "zip_crc_ok", False),
        ("dynamic_web_preview", "revoked_after_stop", False),
        ("success_basis", "admin_internal_run_data", True),
        ("conversation", "workspace_path", "wrong-session"),
    ],
)
def test_finalize_rejects_contradictory_core_evidence(
    section: str | None,
    field: str,
    value: object,
) -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    case = pending["cases"][0]
    (case if section is None else case[section])[field] = value
    before = copy.deepcopy(pending)
    with pytest.raises(ValueError, match="core"):
        module.finalize_real_device_acceptance(pending, evidence)
    assert pending == before


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "boolean_version",
        "kind",
        "benchmark",
        "execution",
        "attempt",
        "identity",
        "principal",
        "scale",
    ],
)
def test_finalize_rejects_wrong_version_or_execution_scope(change: str) -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    if change == "version":
        pending["schema_version"] = 2
    elif change == "boolean_version":
        pending["schema_version"] = True
    elif change == "kind":
        pending["kind"] = "other_acceptance"
    elif change == "benchmark":
        pending["benchmark_kind"] = "other"
    elif change == "execution":
        pending["execution_id"] = "other-execution"
        evidence["execution_id"] = "other-execution"
    elif change == "attempt":
        pending["cases"][0]["attempt"] = 2
    elif change == "identity":
        pending["execution_identity"]["tenant_id"] = "other-tenant"
    elif change == "principal":
        pending["actor"]["principal"]["user_id"] = "other-user"
    elif change == "scale":
        case = pending["cases"][0]
        case["scale"] = "medium"
        case["effective_scale"] = "medium"
        case["run"]["effective_scale"] = "medium"
        case["run"]["final_effective_scale"] = "medium"
    before = copy.deepcopy(pending)
    with pytest.raises(ValueError):
        module.finalize_real_device_acceptance(pending, evidence)
    assert pending == before


@pytest.mark.parametrize("errors", [["setup failed"], None, "setup failed", False])
def test_finalize_rejects_top_level_errors(errors: object) -> None:
    module = load_script()
    pending = _pending_automated_report()
    pending["errors"] = errors
    before = copy.deepcopy(pending)
    with pytest.raises(ValueError, match="errors"):
        module.finalize_real_device_acceptance(pending, _real_device_evidence("matrix-123"))
    assert pending == before


@pytest.mark.parametrize("case_id", _FINALIZER_CASE_IDS)
def test_finalize_rejects_any_missing_canonical_case_despite_success_flags(case_id: str) -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    pending["cases"] = [case for case in pending["cases"] if case["case_id"] != case_id]
    del evidence["cases"][case_id]

    with pytest.raises(ValueError):
        module.finalize_real_device_acceptance(pending, evidence)


@pytest.mark.parametrize(
    "change",
    [
        "one_case",
        "empty",
        "missing_cases",
        "cases_object",
        "cases_string",
        "duplicate",
        "duplicate_replaces_case",
        "unknown",
        "unknown_replaces_case",
        "non_object",
        "missing_id",
        "empty_id",
        "non_string_id",
        "list_id",
        "failed",
        "failed_status",
        "missing_core",
        "integer_core",
        "string_core",
    ],
)
def test_finalize_rejects_invalid_automated_cases(change: str) -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    first = pending["cases"][0]
    if change == "one_case":
        pending["cases"] = [first]
        evidence["cases"] = {first["case_id"]: evidence["cases"][first["case_id"]]}
    elif change == "empty":
        pending["cases"] = []
    elif change == "missing_cases":
        del pending["cases"]
    elif change == "cases_object":
        pending["cases"] = {first["case_id"]: first}
    elif change == "cases_string":
        pending["cases"] = "small:auto"
    elif change == "duplicate":
        pending["cases"].append(copy.deepcopy(first))
    elif change == "duplicate_replaces_case":
        pending["cases"][-1] = copy.deepcopy(first)
    elif change in {"unknown", "unknown_replaces_case"}:
        unknown = {**first, "case_id": "small:unknown"}
        evidence["cases"]["small:unknown"] = copy.deepcopy(evidence["cases"][first["case_id"]])
        if change == "unknown":
            pending["cases"].append(unknown)
        else:
            evidence["cases"].pop(pending["cases"][-1]["case_id"])
            pending["cases"][-1] = unknown
    elif change == "non_object":
        pending["cases"][0] = None
    elif change == "missing_id":
        del first["case_id"]
    elif change == "empty_id":
        first["case_id"] = ""
    elif change == "non_string_id":
        first["case_id"] = 1
    elif change == "list_id":
        first["case_id"] = ["small:auto"]
    elif change == "failed":
        first["core_acceptance_ok"] = False
        first["status"] = "failed"
    elif change == "failed_status":
        first["status"] = "failed"
    elif change == "missing_core":
        del first["core_acceptance_ok"]
    elif change == "integer_core":
        first["core_acceptance_ok"] = 1
    elif change == "string_core":
        first["core_acceptance_ok"] = "true"
    pending_before = copy.deepcopy(pending)
    evidence_before = copy.deepcopy(evidence)

    with pytest.raises((TypeError, ValueError)):
        module.finalize_real_device_acceptance(pending, evidence)
    assert pending == pending_before
    assert evidence == evidence_before


@pytest.mark.parametrize("change", ["extra", "missing", "missing_cases", "non_object", "bad_item"])
def test_finalize_rejects_invalid_evidence_case_set(change: str) -> None:
    module = load_script()
    evidence = _real_device_evidence("matrix-123")
    if change == "extra":
        evidence["cases"]["unknown:direct"] = copy.deepcopy(evidence["cases"]["small:auto"])
    elif change == "missing":
        del evidence["cases"]["ultra:multi_agent"]
    elif change == "missing_cases":
        del evidence["cases"]
    elif change == "non_object":
        evidence["cases"] = []
    elif change == "bad_item":
        evidence["cases"]["small:auto"] = []

    with pytest.raises((TypeError, ValueError)):
        module.finalize_real_device_acceptance(_pending_automated_report(), evidence)


@pytest.mark.parametrize("field", ["project_id", "conversation_id", "run_id"])
@pytest.mark.parametrize("value", [None, "", "wrong-scope", 1])
def test_finalize_rejects_case_evidence_scope_mismatch(field: str, value: object) -> None:
    module = load_script()
    evidence = _real_device_evidence("matrix-123")
    evidence["cases"]["ultra:multi_agent"][field] = value

    with pytest.raises(ValueError, match="does not match"):
        module.finalize_real_device_acceptance(_pending_automated_report(), evidence)


@pytest.mark.parametrize(
    ("section", "field"),
    [("project", "project_id"), ("conversation", "conversation_id"), ("run", "run_id")],
)
@pytest.mark.parametrize("value", [None, "", "   ", 1])
def test_finalize_rejects_malformed_automated_scope(
    section: str,
    field: str,
    value: object,
) -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    pending["cases"][-1][section][field] = value
    evidence["cases"]["ultra:multi_agent"][field] = value

    with pytest.raises((TypeError, ValueError)):
        module.finalize_real_device_acceptance(pending, evidence)


def test_finalize_cli_writes_complete_report_without_logging_in(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = load_script()
    report_path = tmp_path / "automated.json"
    evidence_path = tmp_path / "real-device.json"
    output_path = tmp_path / "complete.json"
    report_path.write_text(
        json.dumps(_pending_automated_report()),
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


def test_main_accepts_bearer_token_without_password(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = load_script()
    output_path = tmp_path / "bearer-report.json"
    captured: dict[str, object] = {}

    class BearerClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    def run_acceptance(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        captured["run_kwargs"] = kwargs
        return {
            "status": "pending_real_device",
            "acceptance_complete": False,
        }

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "short-lived-token")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_USERNAME", "ignored-user")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_PASSWORD", "ignored-password")
    monkeypatch.setattr(module, "UrllibAcceptanceClient", BearerClient)
    monkeypatch.setattr(module, "run_real_user_four_scale_acceptance", run_acceptance)

    exit_code = module.main(["--output", str(output_path)])

    assert exit_code == 2
    assert captured["bearer_token"] == "short-lived-token"
    assert captured["username"] is None
    assert captured["password"] is None
    run_kwargs = captured["run_kwargs"]
    assert isinstance(run_kwargs, dict)
    assert run_kwargs["authentication_method"] == "bearer_token"


@pytest.fixture
def matrix_harness(
    monkeypatch: Any,
) -> tuple[Any, Any, list[Any], list[str]]:
    module = load_script()
    bundle = workspace_zip()

    class MatrixDelegate:
        def __init__(self) -> None:
            self.created_projects: list[str] = []
            self.created_conversations: list[str] = []
            self.project_workspace_paths: list[str] = []
            self.conversation_workspace_paths: list[str] = []
            self.stopped_previews: set[str] = set()
            self.projects: dict[str, dict[str, object]] = {}
            self.conversations: dict[str, dict[str, object]] = {}
            self.runs: dict[str, dict[str, object]] = {}
            self.run_bodies: dict[str, dict[str, object]] = {}
            self.submissions: list[tuple[str, str]] = []
            self.observed_runs: list[str] = []
            self.requests: list[tuple[str, str]] = []
            self.interrupt_after_commit: str | None = None
            self.model_events: dict[str, object] = {}

        def _interrupt(self, path: str) -> None:
            if self.interrupt_after_commit == path:
                self.interrupt_after_commit = None
                raise KeyboardInterrupt

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            self.requests.append((method, path))
            if method == "GET" and path == "/api/v1/auth/me":
                return {"user_id": "user-test", "tenant_id": "tenant-test", "role": "operator"}
            if method == "POST" and path == "/api/v1/admin/project-workspaces":
                assert body is not None
                project_id = str(body["project_id"])
                if project_id in self.projects:
                    raise AcceptanceHTTPError(
                        method=method,
                        path=path,
                        status_code=409,
                        response_body='{"error":{"code":"project_workspace_conflict"}}',
                    )
                self.created_projects.append(project_id)
                self.project_workspace_paths.append(str(body["workspace_path"]))
                self.projects[project_id] = {**body, "legacy_workspace_count": 1}
                self._interrupt(path)
                return copy.deepcopy(self.projects[project_id])
            if method == "GET" and path == "/api/v1/admin/project-workspaces":
                return cast(list[object], copy.deepcopy(list(self.projects.values())))
            if method == "POST" and path == "/api/v1/admin/conversations":
                assert body is not None
                conversation_id = str(body["conversation_id"])
                if conversation_id in self.conversations:
                    raise AcceptanceHTTPError(
                        method=method,
                        path=path,
                        status_code=409,
                        response_body='{"error":{"code":"conversation_conflict"}}',
                    )
                if body["project_id"] not in self.projects:
                    raise AcceptanceHTTPError(
                        method=method,
                        path=path,
                        status_code=409,
                        response_body='{"error":{"code":"project_workspace_required"}}',
                    )
                self.created_conversations.append(conversation_id)
                self.conversation_workspace_paths.append(str(body["workspace_path"]))
                self.conversations[conversation_id] = {**body, "archived_at": None, "runs": []}
                self._interrupt(path)
                return copy.deepcopy(self.conversations[conversation_id])
            if method == "GET" and path.startswith("/api/v1/admin/conversations/"):
                return copy.deepcopy(self.conversations[path.rsplit("/", 1)[-1]])
            if method == "POST" and path == "/api/v1/runs":
                assert body is not None and idempotency_key is not None
                if idempotency_key not in self.runs:
                    self.run_bodies[idempotency_key] = copy.deepcopy(body)
                    self.runs[idempotency_key] = {
                        **body,
                        "id": f"run-{len(self.runs) + 1}",
                        "status": "completed",
                        "mode": "direct" if body["mode"] == "auto" else body["mode"],
                        "requested_mode": body["mode"],
                        "effective_scale": "small",
                    }
                assert self.run_bodies[idempotency_key] == body
                run = self.runs[idempotency_key]
                self.submissions.append((idempotency_key, str(run["id"])))
                self._interrupt(path)
                return copy.deepcopy(run)
            if method == "GET" and path.startswith("/api/v1/runs/"):
                if path.endswith("/events"):
                    run_id = path.split("/")[4]
                    return {"items": self.model_events.get(run_id, [_model_event(run_id)])}
                if path.endswith("/artifacts"):
                    return []
                run_id = path.split("/")[4]
                self.observed_runs.append(run_id)
                return copy.deepcopy(next(run for run in self.runs.values() if run["id"] == run_id))
            if method == "POST" and path == "/api/v1/web-previews/start":
                assert body is not None
                conversation_id = str(body["conversation_id"])
                return {
                    "id": f"preview-{conversation_id}",
                    "status": "ready",
                    "preview_url": (f"/api/v1/web-previews/preview-{conversation_id}/content/"),
                    "lease_expires_at": "2026-09-28T08:30:00Z",
                }
            if method == "GET" and path.startswith("/api/v1/web-previews/conversations/"):
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
                                "1285d7ebaa1def54aa12adb818c4ee1cb1782bf16237ffb86e768002b16e55f9"
                            ),
                            "download_url": f"{root}/files/download?path=README.md",
                        },
                        {
                            "path": "src/app.py",
                            "filename": "app.py",
                            "mime_type": "text/x-python",
                            "size_bytes": 12,
                            "sha256": (
                                "ad64355106bb158b020ecf9702be48f7730fc091dd4bb6a2f092b40393495b3d"
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
                return b'<!doctype html><title>preview</title><script src="assets/app.js"></script>'
            raise AssertionError(f"unexpected bytes request: {method} {path}")

    plans: list[Any] = []
    scoped_workspace_paths: list[str] = []

    def execute(plan: Any, client: Any, **kwargs: object) -> ProjectScaleExecutionReport:
        assert kwargs["auto_approve_capability_requests"] is True
        plans.append(plan)
        submitted = client.request_json(
            "POST",
            "/api/v1/runs",
            body=dict(plan.requests[0].body),
            idempotency_key=_idempotency_key(
                plan.requests[0].case_id,
                0,
                execution_id=str(kwargs["execution_id"]),
            ),
        )
        observed = client.request_json("GET", f"/api/v1/runs/{submitted['id']}")
        scale, route_intent = plan.requests[0].case_id.split(":", 1)
        scoped_workspace_paths.append(
            module._safe_workspace_session_token(
                str(plan.requests[0].body["workspace_session_id"]),
                str(kwargs["execution_id"]),
            )
        )
        result = ProjectScaleCaseResult(
            case_id=plan.requests[0].case_id,
            run_id=str(observed["id"]),
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
    return module, delegate, plans, scoped_workspace_paths


def test_real_user_acceptance_runs_four_auto_scales_and_every_mode_at_every_scale(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    module, delegate, plans, scoped_workspace_paths = matrix_harness
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
        (case["case_kind"], case["scale"], case["route_intent"]) for case in payload["cases"]
    ] == [("auto_scale", scale, "auto") for scale in ("small", "medium", "large", "ultra")] + [
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


def run_matrix(module: Any, delegate: Any, **kwargs: Any) -> dict[str, Any]:
    options = {
        "username": "test",
        "base_url": "http://example.test",
        "execution_id": "matrix-123",
        "wait_seconds": 1,
        "poll_interval_seconds": 0,
        "artifact_build_timeout_seconds": 1,
        **kwargs,
    }
    return cast(
        dict[str, Any],
        module.run_real_user_four_scale_acceptance(
            module.RealUserAcceptanceClient(delegate), **options
        ),
    )


def test_logical_model_scopes_all_twenty_public_requests_without_changing_modes_or_scales(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    from agent_hub.api.routers.runs import CreateRunRequest

    module, delegate, plans, _ = matrix_harness
    payload = run_matrix(module, delegate, logical_model="deepseek-backup")
    profile = {"direct_model": "deepseek-backup", "allowed_models": ["deepseek-backup"]}
    assert len(delegate.run_bodies) == len(plans) == 20
    assert payload["model_profile"] == profile
    assert payload["execution_identity"]["model_profile"] == profile
    assert payload["core_acceptance_ok"] is True
    for plan, case, body in zip(plans, payload["cases"], delegate.run_bodies.values(), strict=True):
        assert body["direct_model"] == "deepseek-backup"
        assert body["allowed_models"] == ("deepseek-backup",)
        public_request = CreateRunRequest.model_validate(body)
        assert public_request.allowed_models == ("deepseek-backup",)
        baseline = module.build_real_user_scale_plan(
            scale=case["scale"],
            route_intent=case["route_intent"],
            project_id=body["project_id"],
            project_label=body["project_label"],
            conversation_id=body["conversation_id"],
            workspace_session_id=body["workspace_session_id"],
        )
        assert {k: v for k, v in body.items() if k not in profile} == baseline.requests[0].body
        assert plan.requests[0].validation_focus == baseline.requests[0].validation_focus
        assert case["model_profile"] == profile
        assert case["model_scope_evidence"]["ok"] is True
        assert case["success_basis"]["model_scope"] is True
        assert ("GET", f"/api/v1/runs/{case['run']['run_id']}/events") in delegate.requests
        assert "participant_models" not in case["run"]


@pytest.mark.parametrize(
    "logical_model",
    ["", " backup", "backup ", "Backup", "a/b", "a.b", "a:b", "-a", "a\n", "a" * 129, 123],
)
def test_logical_model_rejects_unsafe_ids_before_http_or_writes(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    logical_model: object,
) -> None:
    module, delegate, plans, _ = matrix_harness
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="safe logical model"):
        run_matrix(module, delegate, logical_model=logical_model, output_path=str(output))
    assert delegate.requests == []
    assert plans == []
    assert not output.exists()


@pytest.mark.parametrize(
    ("saved_model", "requested_model"),
    [(None, "deepseek-backup"), ("deepseek-backup", None), ("deepseek-backup", "other-model")],
)
@pytest.mark.parametrize("finalized", [False, True])
def test_model_profile_resume_mismatch_rejects_before_side_effects(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    saved_model: str | None,
    requested_model: str | None,
    finalized: bool,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report(saved_model)
    if finalized:
        saved = module.finalize_real_device_acceptance(
            saved, _real_device_evidence("matrix-123", saved_model)
        )
    before = copy.deepcopy(saved)
    output = tmp_path / "report.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    original_bytes = output.read_bytes()
    with pytest.raises(ValueError, match="model profile"):
        run_matrix(
            module,
            delegate,
            logical_model=requested_model,
            resume_report=saved,
            output_path=str(output),
        )
    assert saved == before
    assert output.read_bytes() == original_bytes
    assert delegate.requests == []
    assert plans == []


@pytest.mark.parametrize("section", ["execution_identity", "cases", "attempt_history"])
def test_model_profile_cannot_relabel_existing_run_or_attempt_evidence(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    section: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report("deepseek-backup")
    saved["attempt_history"] = [copy.deepcopy(saved["cases"][0])]
    target = saved[section] if section == "execution_identity" else saved[section][0]
    target.pop("model_profile")
    before = copy.deepcopy(saved)
    with pytest.raises(ValueError, match="model profile"):
        run_matrix(module, delegate, logical_model="deepseek-backup", resume_report=saved)
    with pytest.raises(ValueError, match="model profile"):
        module.finalize_real_device_acceptance(
            saved, _real_device_evidence("matrix-123", "deepseek-backup")
        )
    assert saved == before
    assert delegate.requests == []
    assert plans == []


@pytest.mark.parametrize(
    "profile",
    [
        None,
        {},
        {"direct_model": "deepseek-backup", "allowed_models": []},
        {"direct_model": "deepseek-backup", "allowed_models": ["other-model"]},
        {"direct_model": "a/b", "allowed_models": ["a/b"]},
    ],
)
def test_finalizer_requires_exact_model_profile_in_device_evidence(profile: object) -> None:
    module = load_script()
    saved = _pending_automated_report("deepseek-backup")
    evidence = _real_device_evidence("matrix-123")
    if profile is not None:
        evidence["model_profile"] = profile
    with pytest.raises((TypeError, ValueError), match="model profile"):
        module.finalize_real_device_acceptance(saved, evidence)


def test_scoped_finalizer_preserves_profile_and_does_not_invent_model_evidence() -> None:
    module = load_script()
    saved = _pending_automated_report("deepseek-backup")
    evidence = _real_device_evidence("matrix-123", "deepseek-backup")
    before = copy.deepcopy(saved)
    completed = module.finalize_real_device_acceptance(saved, evidence)
    assert completed["model_profile"] == saved["model_profile"]
    assert completed["real_device_acceptance"]["model_profile"] == saved["model_profile"]
    assert saved == before
    assert [case["run"] for case in completed["cases"]] == [case["run"] for case in saved["cases"]]


def test_legacy_unscoped_resume_keeps_default_requests_and_report_identity(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    saved["cases"] = []
    resumed = run_matrix(module, delegate, resume_report=saved)
    assert resumed["core_acceptance_ok"] is True
    assert resumed["execution_identity"] == saved["execution_identity"]
    assert "model_profile" not in resumed
    for plan, case in zip(plans, resumed["cases"], strict=True):
        assert "direct_model" not in plan.requests[0].body
        assert "allowed_models" not in plan.requests[0].body
        assert "model_profile" not in case


def test_failed_case_retry_and_existing_runner_repairs_keep_request_model_scope(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
) -> None:
    from agent_hub.harness.project_scale_runner import _deliverable_repair_body

    module, delegate, plans, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    execute = module.execute_project_scale_plan

    def failed(plan: Any, client: Any, **kwargs: Any) -> Any:
        report = execute(plan, client, **kwargs)
        return replace(report, results=(replace(report.results[0], errors=("failed repair",)),))

    monkeypatch.setattr(module, "execute_project_scale_plan", failed)
    saved = run_matrix(module, delegate, logical_model="deepseek-backup")
    assert saved["failed_case_count"] == 1
    monkeypatch.setattr(module, "execute_project_scale_plan", execute)
    resumed = run_matrix(module, delegate, logical_model="deepseek-backup", resume_report=saved)
    assert resumed["core_acceptance_ok"] is True
    assert resumed["cases"][0]["attempt"] == 2
    assert resumed["attempt_history"] == saved["cases"]
    for plan in plans:
        body = dict(plan.requests[0].body)
        original = copy.deepcopy(body)
        for mode in ("direct", "dispatch", "hybrid"):
            body = _deliverable_repair_body(
                body,
                plan.requests[0].case_id,
                benchmark_kind="capability",
                effective_mode=mode,
                failed_reasons=("generated_project_validation: failed",),
            )
            assert body["direct_model"] == "deepseek-backup"
            assert body["allowed_models"] == ("deepseek-backup",)
        assert plan.requests[0].body == original


def test_logical_model_cli_scopes_requests(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-token")
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    output = tmp_path / "report.json"
    assert (
        module.main(
            [
                "--base-url",
                "http://example.test",
                "--execution-id",
                "matrix-123",
                "--output",
                str(output),
                "--logical-model",
                "deepseek-backup",
            ]
        )
        == 2
    )
    assert len(delegate.run_bodies) == 20
    assert json.loads(output.read_text(encoding="utf-8"))["model_profile"] == {
        "direct_model": "deepseek-backup",
        "allowed_models": ["deepseek-backup"],
    }


@pytest.mark.parametrize("action", ["resume", "finalize"])
def test_model_profile_cli_mismatch_preserves_existing_report_before_client_creation(
    tmp_path: Path,
    monkeypatch: Any,
    action: str,
) -> None:
    module = load_script()
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_pending_automated_report()), encoding="utf-8")
    before = report.read_bytes()
    args = ["--logical-model", "deepseek-backup", "--output", str(report)]
    if action == "resume":
        args += ["--resume-report", str(report)]
    else:
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps(_real_device_evidence("matrix-123")), encoding="utf-8")
        args += ["--finalize-report", str(report), "--real-device-evidence", str(evidence)]

    def forbidden_client(**kwargs: Any) -> Any:
        pytest.fail("profile mismatch must stop before client creation")

    monkeypatch.setattr(module, "UrllibAcceptanceClient", forbidden_client)
    with pytest.raises(SystemExit) as error:
        module.main(args)
    assert error.value.code == 2
    assert report.read_bytes() == before


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "foreign",
        "attempted",
        "null_attempts",
        "bad_attempts",
        "wrong_run",
        "flat_payload",
        "noncompletion",
        "other_event_foreign",
        "malformed_event",
    ],
)
def test_scoped_core_requires_actual_public_completions_and_no_foreign_attempts(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    invalid: str,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    event = _model_event("run-1")
    events: list[Any] = [event]
    if invalid == "missing":
        events = []
    elif invalid == "foreign":
        event["payload"]["logical_model"] = "other-model"
    elif invalid == "attempted":
        event["payload"]["attempted_logical_models"] = ["other-model", "deepseek-backup"]
    elif invalid == "null_attempts":
        event["payload"]["attempted_logical_models"] = None
    elif invalid == "bad_attempts":
        event["payload"]["attempted_logical_models"] = "deepseek-backup"
    elif invalid == "wrong_run":
        event["run_id"] = "another-run"
    elif invalid == "flat_payload":
        event.update(event.pop("payload"))
    elif invalid == "noncompletion":
        event["kind"] = "model.started"
    elif invalid == "other_event_foreign":
        events.append(
            {
                "kind": "model.failed",
                "run_id": "run-1",
                "payload": {"attempted_logical_models": ["other-model"]},
            }
        )
    elif invalid == "malformed_event":
        events.append("malformed")
    delegate.model_events["run-1"] = events
    payload = run_matrix(module, delegate, logical_model="deepseek-backup")
    case = payload["cases"][0]
    assert payload["core_acceptance_ok"] is False
    assert case["core_acceptance_ok"] is False
    assert case["model_scope_evidence"]["ok"] is False
    assert case["model_scope_evidence"]["errors"]


@pytest.mark.parametrize("attempts", ["absent", "empty"])
def test_scoped_completion_allows_old_adapters_without_attempt_history(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    attempts: str,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    event = _model_event("run-1")
    if attempts == "absent":
        event["payload"].pop("attempted_logical_models")
    else:
        event["payload"]["attempted_logical_models"] = []
    delegate.model_events["run-1"] = [event]
    payload = run_matrix(module, delegate, logical_model="deepseek-backup")
    assert payload["core_acceptance_ok"] is True
    assert payload["cases"][0]["model_scope_evidence"]["runs"][0]["model_events"] == [event]


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "foreign",
        "attempted",
        "missing_original",
        "missing_repair",
        "nonterminal",
        "flag",
    ],
)
def test_scope_evidence_revalidated_on_resume_and_finalization(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    tamper: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report("deepseek-backup")
    case = saved["cases"][0]
    scope = case["model_scope_evidence"]
    if tamper == "missing":
        case.pop("model_scope_evidence")
    elif tamper == "foreign":
        scope["runs"][0]["model_events"][0]["payload"]["logical_model"] = "other-model"
    elif tamper == "attempted":
        scope["runs"][0]["model_events"][0]["payload"]["attempted_logical_models"] = ["other-model"]
    elif tamper == "missing_original":
        scope["original_run_id"] = "missing-original"
    elif tamper == "missing_repair":
        scope["accepted_repair_run_ids"] = ["missing-repair"]
    elif tamper == "nonterminal":
        scope["runs"][0]["status"] = "running"
    elif tamper == "flag":
        scope["ok"] = 1
    with pytest.raises(ValueError):
        module.finalize_real_device_acceptance(
            saved, _real_device_evidence("matrix-123", "deepseek-backup")
        )
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    saved["cases"] = [case]
    resumed = run_matrix(module, delegate, logical_model="deepseek-backup", resume_report=saved)
    assert len(plans) == 1
    assert resumed["cases"][0]["attempt"] == 2
    assert resumed["attempt_history"] == [case]


@pytest.mark.parametrize("repair_path", ["deliverable", "accepted"])
@pytest.mark.parametrize("bad_run", [None, "run-1", "repair-1", "run-2"])
def test_scoped_core_checks_original_result_and_every_accepted_repair_run(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    repair_path: str,
    bad_run: str | None,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    execute = module.execute_project_scale_plan
    request = delegate.request_json

    def accepted(method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and path == "/api/v1/runs/run-1/accept-repair":
            delegate.runs["accepted"] = {
                **delegate.runs[next(iter(delegate.runs))],
                "id": "repair-1",
            }
            return copy.deepcopy(delegate.runs["accepted"])
        return request(method, path, **kwargs)

    monkeypatch.setattr(delegate, "request_json", accepted)

    def repaired(plan: Any, client: Any, **kwargs: Any) -> Any:
        report = execute(plan, client, **kwargs)
        if repair_path == "accepted":
            client.request_json(
                "POST",
                "/api/v1/runs/run-1/accept-repair",
                body={"decision_token": "token", "version": 1},
            )
        else:
            client.request_json(
                "POST", "/api/v1/runs", body=dict(plan.requests[0].body), idempotency_key="repair-1"
            )
        # A distinct final run also needs proof, even if it was not returned by submission.
        delegate.runs["result"] = {**next(iter(delegate.runs.values())), "id": "result-run"}
        return replace(report, results=(replace(report.results[0], run_id="result-run"),))

    monkeypatch.setattr(module, "execute_project_scale_plan", repaired)
    repair_id = "repair-1" if repair_path == "accepted" else "run-2"
    if bad_run is not None:
        invalid_id = "result-run" if bad_run not in {"run-1", repair_id} else bad_run
        delegate.model_events[invalid_id] = []
    payload = run_matrix(module, delegate, logical_model="deepseek-backup")
    scope = payload["cases"][0]["model_scope_evidence"]
    assert scope["original_run_id"] == "run-1"
    assert scope["accepted_repair_run_ids"] == [repair_id]
    assert {item["run_id"] for item in scope["runs"]} == {"run-1", repair_id, "result-run"}
    assert payload["core_acceptance_ok"] is (bad_run is None)


def test_retry_preserves_flat_attempt_history_and_does_not_mutate_saved_evidence(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    execute = module.execute_project_scale_plan
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])

    def fail(plan: Any, client: Any, **kwargs: Any) -> ProjectScaleExecutionReport:
        report = cast(ProjectScaleExecutionReport, execute(plan, client, **kwargs))
        return replace(report, results=(replace(report.results[0], errors=("provider blocked",)),))

    monkeypatch.setattr(module, "execute_project_scale_plan", fail)
    output = tmp_path / "history.json"
    first = run_matrix(module, delegate, output_path=str(output))
    before = copy.deepcopy(first)
    second = run_matrix(module, delegate, output_path=str(output), resume_report=first)
    assert second["attempt_history"] == [first["cases"][0]]
    assert first == before
    monkeypatch.setattr(module, "execute_project_scale_plan", execute)
    third = run_matrix(module, delegate, output_path=str(output), resume_report=second)
    assert third["attempt_history"] == [first["cases"][0], second["cases"][0]]
    assert third["cases"][0]["attempt"] == 3
    assert third["cases"][0]["core_acceptance_ok"] is True
    assert third["failed_case_count"] == 0
    assert json.loads(output.read_text(encoding="utf-8")) == third
    third["cases"][0]["run"]["errors"].append("changed current evidence")
    assert third["attempt_history"][0]["run"]["errors"] == ["provider blocked"]
    third["attempt_history"][0]["run"]["errors"].clear()
    assert second["attempt_history"][0]["run"]["errors"] == ["provider blocked"]


def test_retry_checkpoint_retains_original_evidence_before_interrupt(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    saved = run_matrix(module, delegate)
    saved["cases"][0]["run"]["errors"] = ["original failure"]
    original = copy.deepcopy(saved["cases"][0])
    output = tmp_path / "history.json"

    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "execute_project_scale_plan", interrupt)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            run_matrix(module, delegate, output_path=str(output), resume_report=saved)
        saved = json.loads(output.read_text(encoding="utf-8"))
        assert saved["attempt_history"] == [original]
        assert saved["core_passed_case_count"] == 19


def test_failed_retry_checkpoint_stops_before_next_case_and_preserves_disk_history(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = run_matrix(module, delegate)
    for case in saved["cases"][:2]:
        case["run"]["errors"] = ["original failure"]
    output = tmp_path / "history.json"
    module._write_report(str(output), saved)
    plans.clear()
    replace_file = module.os.replace
    writes = 0

    def fail_after_initial_checkpoint(source: object, target: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("cannot save retry")
        replace_file(source, target)

    monkeypatch.setattr(module.os, "replace", fail_after_initial_checkpoint)
    with pytest.raises(OSError, match="cannot save retry"):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)
    assert len(plans) == 1
    durable = json.loads(output.read_text(encoding="utf-8"))
    assert durable["attempt_history"] == saved["cases"][:2]
    assert durable["cases"][0]["attempt"] == 1
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("logical_model", [None, "deepseek-backup"])
def test_resume_finalized_report_returns_exact_evidence_without_tasks_or_writes(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
    logical_model: str | None,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = module.finalize_real_device_acceptance(
        _pending_automated_report(logical_model),
        _real_device_evidence("matrix-123", logical_model),
    )
    saved["cases"].reverse()
    saved["finished_at"] = "2026-10-02T13:00:00Z"
    saved["attempt_history"] = [{"case_id": "small:auto", "attempt": 1, "status": "failed"}]
    if logical_model is not None:
        saved["attempt_history"][0]["model_profile"] = copy.deepcopy(saved["model_profile"])
    before = copy.deepcopy(saved)
    output = tmp_path / "finalized.json"
    module._write_report(str(output), saved)
    original_bytes = output.read_bytes()

    def forbidden_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("finalized resume must not save a snapshot")

    monkeypatch.setattr(module, "_save_report", forbidden_write)
    resumed = run_matrix(
        module, delegate, output_path=str(output), resume_report=saved, logical_model=logical_model
    )
    assert resumed == before
    assert saved == before
    assert output.read_bytes() == original_bytes
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []
    resumed["cases"][0]["real_device_evidence"]["mobile"]["passed"] = False
    assert saved == before


@pytest.mark.parametrize(
    "change",
    [
        "run_failed",
        "case_errors",
        "route",
        "missing_case",
        "identity",
        "desktop",
        "mobile",
        "device_scope",
        "case_device",
        "case_complete",
        "case_browser",
        "preview",
        "aggregate",
        "device_complete",
        "integer_complete",
        "integer_device_complete",
        "integer_case_complete",
    ],
)
def test_resume_rejects_invalid_finalized_report_before_tasks_or_writes(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    change: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = module.finalize_real_device_acceptance(
        _pending_automated_report(), _real_device_evidence("matrix-123")
    )
    case = saved["cases"][0]
    device = saved["real_device_acceptance"]
    if change == "run_failed":
        case["run"]["status"] = "failed"
    elif change == "case_errors":
        case["errors"] = ["failure"]
    elif change == "route":
        case["route_policy_ok"] = False
    elif change == "missing_case":
        saved["cases"].pop()
    elif change == "identity":
        saved["execution_identity"]["tenant_id"] = "another-tenant"
    elif change in {"desktop", "mobile"}:
        device[f"{change}_browser_interaction"]["checks"]["preview_interaction"] = False
    elif change == "device_scope":
        device["cases"][case["case_id"]]["run_id"] = "other-run"
    elif change == "case_device":
        case["real_device_evidence"]["mobile"]["passed"] = False
    elif change == "case_complete":
        case["acceptance_complete"] = False
    elif change == "case_browser":
        case["dynamic_web_preview"]["browser_interaction"] = "pending_real_device"
    elif change == "preview":
        saved["dynamic_web_preview"]["counted_as_passed"] = False
    elif change == "aggregate":
        saved["status"] = "pending_real_device"
    elif change == "device_complete":
        device["counted_as_complete"] = False
    elif change == "integer_complete":
        saved["acceptance_complete"] = 1
    elif change == "integer_device_complete":
        device["counted_as_complete"] = 1
    elif change == "integer_case_complete":
        case["real_device_acceptance_complete"] = 1
    before = copy.deepcopy(saved)
    output = tmp_path / "finalized.json"
    module._write_report(str(output), saved)
    original_bytes = output.read_bytes()
    with pytest.raises((TypeError, ValueError)):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)
    assert output.read_bytes() == original_bytes
    assert saved == before
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("logical_model", [None, "deepseek-backup"])
def test_resume_finalized_cli_preserves_file_and_uses_only_authenticated_identity(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
    invalid: bool,
    logical_model: str | None,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = module.finalize_real_device_acceptance(
        _pending_automated_report(logical_model),
        _real_device_evidence("matrix-123", logical_model),
    )
    if invalid:
        saved["real_device_acceptance"]["mobile_browser_interaction"]["passed"] = False
    output = tmp_path / "finalized.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    before = output.read_bytes()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-secret-token")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_USERNAME", "synthetic-user")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_PASSWORD", "synthetic-secret-password")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_REPORT_PATH", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)

    def forbidden_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("finalized CLI resume must preserve file bytes")

    monkeypatch.setattr(module, "_save_report", forbidden_write)
    exit_code = module.main(
        [
            "--base-url",
            "http://example.test",
            "--resume-report",
            str(output),
        ]
        + (["--logical-model", logical_model] if logical_model is not None else [])
    )
    assert exit_code == (1 if invalid else 0)
    assert output.read_bytes() == before
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []
    assert "synthetic-secret-token" not in before.decode("utf-8")
    assert "synthetic-secret-password" not in before.decode("utf-8")


def test_checkpoint_survives_interrupt_and_resume_keeps_completed_case(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    output = tmp_path / "checkpoint.json"
    execute = module.execute_project_scale_plan

    def interrupt(plan: Any, client: Any, **kwargs: Any) -> Any:
        if plans:
            saved = json.loads(output.read_text(encoding="utf-8"))
            assert saved["case_count"] == 1
            assert saved["core_passed_case_count"] == 1
            assert saved["status"] == "in_progress"
            assert saved["core_acceptance_ok"] is False
            assert saved["automated_acceptance_complete"] is False
            assert saved["acceptance_complete"] is False
            assert saved["finished_at"] is None
            raise KeyboardInterrupt
        return execute(plan, client, **kwargs)

    monkeypatch.setattr(module, "execute_project_scale_plan", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    first = saved["cases"][0]
    monkeypatch.setattr(module, "execute_project_scale_plan", execute)
    resumed = run_matrix(module, delegate, output_path=str(output), resume_report=saved)

    assert len(plans) == 20
    assert resumed["cases"][0] == first
    assert resumed["started_at"] == saved["started_at"]
    assert resumed["case_count"] == 20
    assert resumed["core_passed_case_count"] == 20
    assert resumed["status"] == "pending_real_device"
    assert resumed["acceptance_complete"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == resumed


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "status", "failed"),
        (None, "automated_acceptance_complete", False),
        ("run", "status", "running"),
        ("run", "run_id", None),
        ("run", "errors", ["failed"]),
        ("run", "evidence", {}),
        ("run", "effective_scale", "ultra"),
        ("run", "artifact_origin", "fixture"),
        ("run", "final_observed_mode", "unknown"),
        ("build_and_test", "requirements_validation", False),
        ("public_artifacts", "ok", 1),
        ("public_artifacts", "downloaded_file_count", 0),
        ("public_artifacts", "zip_crc_ok", False),
        ("public_artifacts", "zip_sha256", None),
        ("public_artifacts", "unsafe_member_count", False),
        ("public_artifacts", "files_endpoint", "/another/workspace/files"),
        ("dynamic_web_preview", "revoked_after_stop", False),
        ("dynamic_web_preview", "referenced_assets_loaded", 0),
        ("dynamic_web_preview", "counted_as_passed", "true"),
        ("success_basis", "admin_internal_run_data", True),
        ("project", "project_id", "another-project"),
    ],
)
def test_resume_retests_incomplete_or_inconsistent_core_evidence(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    section: str | None,
    key: str,
    value: object,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = run_matrix(module, delegate)
    case = saved["cases"][0]
    (case if section is None else case[section])[key] = value
    original_project = delegate.created_projects[0]
    plans.clear()
    resumed = run_matrix(module, delegate, resume_report=saved)

    assert len(plans) == 1
    assert plans[0].requests[0].case_id == "small:auto"
    assert delegate.created_projects[-1] != original_project
    assert resumed["core_passed_case_count"] == 20
    assert len({item["case_id"] for item in resumed["cases"]}) == 20
    assert resumed["cases"][1:] == saved["cases"][1:]


@pytest.mark.parametrize("field", ["execution_id", "base_url", "user_id", "tenant_id"])
def test_resume_rejects_different_execution_identity_before_case_requests(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    field: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = run_matrix(module, delegate)
    if field in {"execution_id", "base_url"}:
        saved[field] = "different"
    else:
        saved["actor"]["principal"][field] = "different"
    plans.clear()
    with pytest.raises(ValueError, match="identity"):
        run_matrix(module, delegate, resume_report=saved)
    assert plans == []
    assert len(delegate.created_projects) == 20


def test_resume_retests_exception_failure_with_new_attempt_scope(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    execute = module.execute_project_scale_plan

    def fail(plan: Any, client: Any, **kwargs: Any) -> Any:
        if plan.requests[0].case_id == "small:auto":
            raise RuntimeError("deployment unavailable")
        return execute(plan, client, **kwargs)

    monkeypatch.setattr(module, "execute_project_scale_plan", fail)
    saved = run_matrix(module, delegate)
    assert saved["cases"][0]["case_id"] == "small:auto"
    assert saved["failed_case_count"] == 1
    monkeypatch.setattr(module, "execute_project_scale_plan", execute)
    plans.clear()
    resumed = run_matrix(module, delegate, resume_report=saved)
    assert len(plans) == 1
    assert resumed["cases"][0]["attempt"] == 2
    assert resumed["cases"][0]["project"] != saved["cases"][0]["project"]
    assert resumed["cases"][0]["conversation"] != saved["cases"][0]["conversation"]
    assert resumed["failed_case_count"] == 0


def test_atomic_report_replace_failure_preserves_previous_checkpoint(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = load_script()
    output = tmp_path / "report.json"
    module._write_report(str(output), {"cases": ["completed"]})
    before = output.read_bytes()

    def fail_replace(source: object, target: object) -> None:
        assert Path(str(source)).parent == output.parent
        assert Path(str(target)) == output
        assert json.loads(Path(str(source)).read_text(encoding="utf-8"))["cases"] == []
        raise OSError("replace interrupted")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace interrupted"):
        module._write_report(str(output), {"cases": []})
    assert output.read_bytes() == before
    assert list(tmp_path.iterdir()) == [output]


def test_resume_cli_uses_saved_execution_and_checkpoints_in_place(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = run_matrix(module, delegate)
    output = tmp_path / "resume.json"
    module._write_report(str(output), saved)
    plans.clear()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "new-token-same-user")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    exit_code = module.main(
        [
            "--base-url",
            "http://example.test/",
            "--resume-report",
            str(output),
        ]
    )
    assert exit_code == 2
    assert plans == []
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["execution_id"] == "matrix-123"
    assert report["cases"] == saved["cases"]
    assert report["acceptance_complete"] is False


def test_resume_rejects_duplicate_cases(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = run_matrix(module, delegate)
    saved["cases"].append(copy.deepcopy(saved["cases"][0]))
    plans.clear()
    with pytest.raises(ValueError, match="duplicate"):
        run_matrix(module, delegate, resume_report=saved)
    assert plans == []


def test_repeated_failed_attempts_use_distinct_workspaces_with_long_execution_id(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    execute = module.execute_project_scale_plan

    def fail(plan: Any, client: Any, **kwargs: Any) -> Any:
        if plan.requests[0].case_id == "small:multi_agent":
            raise RuntimeError("provider unavailable")
        return execute(plan, client, **kwargs)

    monkeypatch.setattr(module, "execute_project_scale_plan", fail)
    saved = run_matrix(module, delegate, execution_id="matrix-" + "a" * 33)
    retry = run_matrix(
        module,
        delegate,
        execution_id="matrix-" + "a" * 33,
        resume_report=saved,
    )
    monkeypatch.setattr(module, "execute_project_scale_plan", execute)
    resumed = run_matrix(
        module,
        delegate,
        execution_id="matrix-" + "a" * 33,
        resume_report=retry,
    )
    assert delegate.project_workspace_paths[-2] != delegate.project_workspace_paths[-1]
    case = next(item for item in resumed["cases"] if item["case_id"] == "small:multi_agent")
    assert case["attempt"] == 3
    assert case["core_acceptance_ok"] is True


@pytest.mark.parametrize("failure", ["identity", "checkpoint"])
def test_resume_cli_failure_preserves_report(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
    failure: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = run_matrix(module, delegate)
    output = tmp_path / "resume.json"
    module._write_report(str(output), saved)
    before = output.read_bytes()
    plans.clear()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "token")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    if failure == "checkpoint":

        def fail_replace(*args: Any) -> None:
            raise OSError("cannot save checkpoint")

        monkeypatch.setattr(module.os, "replace", fail_replace)
    exit_code = module.main(
        [
            "--base-url",
            "http://different.test" if failure == "identity" else "http://example.test",
            "--resume-report",
            str(output),
        ]
    )
    assert exit_code == 1
    assert output.read_bytes() == before
    assert plans == []


def test_resuming_partial_report_preserves_later_passes_during_retry_interrupt(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    saved = run_matrix(module, delegate)
    saved["cases"][0]["run"]["evidence"] = {}
    output = tmp_path / "resume.json"

    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "execute_project_scale_plan", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["cases"][1:] == saved["cases"][1:]
    assert report["cases"][0]["core_acceptance_ok"] is False
    assert report["core_passed_case_count"] == 19
    assert report["status"] == "in_progress"
    assert report["automated_acceptance_complete"] is False


@pytest.mark.parametrize("change", [{"case_id": "medium:auto"}, {"status": "running"}])
def test_checkpoint_does_not_count_wrong_case_or_nonterminal_runner_result(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    change: dict[str, Any],
) -> None:
    module, delegate, _, _ = matrix_harness
    execute = module.execute_project_scale_plan

    def wrong_result(plan: Any, client: Any, **kwargs: Any) -> ProjectScaleExecutionReport:
        report = cast(ProjectScaleExecutionReport, execute(plan, client, **kwargs))
        if plan.requests[0].case_id == "small:auto":
            return replace(report, results=(replace(report.results[0], **change),))
        return report

    monkeypatch.setattr(module, "execute_project_scale_plan", wrong_result)
    payload = run_matrix(module, delegate)
    assert payload["core_acceptance_ok"] is False
    assert payload["cases"][0]["case_id"] == "small:auto"
    assert payload["cases"][0]["core_acceptance_ok"] is False
    assert payload["failed_case_count"] == 1


def test_resume_interrupted_active_case_reuses_run_idempotency_scope(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    execute = module.execute_project_scale_plan
    submissions: list[tuple[str, object]] = []
    output = tmp_path / "active.json"

    def interrupt_after_submit(plan: Any, client: Any, **kwargs: Any) -> Any:
        report = execute(plan, client, **kwargs)
        if plan.requests[0].case_id == "small:auto":
            submissions.append(
                (
                    _idempotency_key("small:auto", 0, execution_id=str(kwargs["execution_id"])),
                    plan.requests[0].body["workspace_session_id"],
                )
            )
            if len(submissions) == 1:
                raise KeyboardInterrupt
        return report

    monkeypatch.setattr(module, "execute_project_scale_plan", interrupt_after_submit)
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["cases"] == []
    assert saved["status"] == "in_progress"
    resumed = run_matrix(module, delegate, output_path=str(output), resume_report=saved)
    assert submissions == [
        ("project-scale-small-auto-0-matrix-123-auto-small", "conv-matrix-123-auto-small"),
        ("project-scale-small-auto-0-matrix-123-auto-small", "conv-matrix-123-auto-small"),
    ]
    assert delegate.created_projects.count("uat-matrix-123-auto-small") == 1
    assert delegate.created_conversations.count("conv-matrix-123-auto-small") == 1
    assert delegate.submissions[0] == delegate.submissions[1]
    assert delegate.observed_runs[0] == delegate.observed_runs[1]
    assert resumed["cases"][0]["attempt"] == 1
    assert resumed["core_passed_case_count"] == 20


@pytest.mark.parametrize(
    "interrupt_path",
    ["/api/v1/admin/project-workspaces", "/api/v1/admin/conversations", "/api/v1/runs"],
)
def test_resume_recovers_resources_committed_before_response_was_saved(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
    interrupt_path: str,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    output = tmp_path / "active.json"
    delegate.interrupt_after_commit = interrupt_path
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["cases"] == []

    resumed = run_matrix(module, delegate, output_path=str(output), resume_report=saved)

    assert resumed["core_passed_case_count"] == 1
    assert resumed["cases"][0]["attempt"] == 1
    assert delegate.created_projects == ["uat-matrix-123-auto-small"]
    assert delegate.created_conversations == ["conv-matrix-123-auto-small"]
    assert ("GET", "/api/v1/admin/project-workspaces") in delegate.requests
    if interrupt_path != "/api/v1/admin/project-workspaces":
        assert (
            "GET",
            "/api/v1/admin/conversations/conv-matrix-123-auto-small",
        ) in delegate.requests
    assert len(delegate.runs) == 1
    assert resumed["cases"][0]["run"]["run_id"] == "run-1"
    if interrupt_path == "/api/v1/runs":
        assert delegate.submissions == [
            ("project-scale-small-auto-0-matrix-123-auto-small", "run-1"),
            ("project-scale-small-auto-0-matrix-123-auto-small", "run-1"),
        ]
        assert delegate.observed_runs == ["run-1"]


@pytest.mark.parametrize(
    ("resource", "field", "value"),
    [
        ("projects", "project_id", "other-project"),
        ("projects", "label", "another acceptance"),
        ("projects", "workspace_path", "other-workspace"),
        ("conversations", "conversation_id", "other-conversation"),
        ("conversations", "project_id", "other-project"),
        ("conversations", "project_label", "another acceptance"),
        ("conversations", "workspace_path", "other-workspace"),
        ("conversations", "title", "another acceptance"),
        ("conversations", "archived_at", "2026-10-02T01:00:00Z"),
    ],
)
def test_resume_rejects_conflicting_existing_resource_scope(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
    resource: str,
    field: str,
    value: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    output = tmp_path / "active.json"
    delegate.interrupt_after_commit = (
        "/api/v1/admin/project-workspaces"
        if resource == "projects"
        else "/api/v1/admin/conversations"
    )
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    resources = getattr(delegate, resource)
    next(iter(resources.values()))[field] = value
    resumed = run_matrix(module, delegate, resume_report=saved)

    assert resumed["failed_case_count"] == 1
    assert resumed["cases"][0]["core_acceptance_ok"] is False
    assert "scope" in resumed["cases"][0]["errors"][0]
    assert plans == []
    assert delegate.runs == {}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "invalid_token"),
        (403, "forbidden"),
        (500, "internal_error"),
        (409, "project_workspace_required"),
        (409, "unrelated_conflict"),
    ],
)
def test_resource_recovery_does_not_swallow_other_http_errors(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    status: int,
    code: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    request = delegate.request_json

    def fail(method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and path == "/api/v1/admin/conversations":
            raise AcceptanceHTTPError(
                method=method,
                path=path,
                status_code=status,
                response_body=json.dumps({"error": {"code": code}}),
            )
        return request(method, path, **kwargs)

    monkeypatch.setattr(delegate, "request_json", fail)
    report = run_matrix(module, delegate)

    assert report["failed_case_count"] == 1
    assert f"status={status}" in report["cases"][0]["errors"][0]
    assert code in report["cases"][0]["errors"][0]
    assert not any(
        method == "GET" and "/admin/conversations/" in path for method, path in delegate.requests
    )
    assert plans == []


@pytest.mark.parametrize("visibility", ["missing", "forbidden"])
def test_resource_conflict_requires_authenticated_lookup_before_reuse(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
    visibility: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    output = tmp_path / "active.json"
    delegate.interrupt_after_commit = "/api/v1/admin/project-workspaces"
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    request = delegate.request_json

    def invisible(method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET" and path == "/api/v1/admin/project-workspaces":
            if visibility == "missing":
                return []
            raise AcceptanceHTTPError(
                method=method,
                path=path,
                status_code=403,
                response_body='{"error":{"code":"forbidden"}}',
            )
        return request(method, path, **kwargs)

    monkeypatch.setattr(delegate, "request_json", invisible)
    resumed = run_matrix(module, delegate, resume_report=saved)
    assert resumed["failed_case_count"] == 1
    assert resumed["cases"][0]["core_acceptance_ok"] is False
    assert plans == []
    assert delegate.created_conversations == []
    assert delegate.runs == {}


def test_real_runner_reobserves_run_committed_before_interruption(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])

    monkeypatch.setattr(module, "execute_project_scale_plan", execute_project_scale_plan)
    output = tmp_path / "active.json"
    delegate.interrupt_after_commit = "/api/v1/runs"
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    request = delegate.request_json

    def stop_after_observation(method: str, path: str, **kwargs: Any) -> Any:
        response = request(method, path, **kwargs)
        if method == "GET" and path == "/api/v1/runs/run-1/details":
            assert response["id"] == "run-1"
            # Stop before unrelated deliverable validation and repair side effects.
            raise KeyboardInterrupt
        return response

    monkeypatch.setattr(delegate, "request_json", stop_after_observation)
    with pytest.raises(KeyboardInterrupt):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)

    assert len(delegate.runs) == 1
    assert delegate.submissions == [
        ("project-scale-small-auto-0-matrix-123-auto-small", "run-1"),
        ("project-scale-small-auto-0-matrix-123-auto-small", "run-1"),
    ]
    assert "run-1" in delegate.observed_runs
    checkpoint = json.loads(output.read_text(encoding="utf-8"))
    assert checkpoint["cases"] == []
    assert checkpoint["core_acceptance_ok"] is False
