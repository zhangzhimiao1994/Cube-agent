from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import zipfile
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib.parse import quote, unquote
from uuid import uuid4

import pytest

from agent_hub.harness.project_scale_runner import (
    AcceptanceHTTPError,
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
    _idempotency_key,
    execute_project_scale_plan,
)
from tests.unit.harness.browser_evidence_fixture import build_device_bundle

_BROWSER_EVIDENCE_ROOT: ContextVar[Path] = ContextVar("browser_evidence_root")


@pytest.fixture(autouse=True)
def browser_evidence_root(tmp_path: Path) -> Iterator[None]:
    token = _BROWSER_EVIDENCE_ROOT.set(tmp_path)
    try:
        yield
    finally:
        _BROWSER_EVIDENCE_ROOT.reset(token)


def _evidence_root() -> Path:
    return _BROWSER_EVIDENCE_ROOT.get()


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


def validated_workspace_manifest() -> dict[str, tuple[int, str]]:
    with zipfile.ZipFile(io.BytesIO(workspace_zip())) as archive:
        return {
            item.filename: (item.file_size, hashlib.sha256(archive.read(item)).hexdigest())
            for item in archive.infolist()
        }


def scale_validation_evidence(case_id: str, run_id: str) -> dict[str, object] | None:
    from agent_hub.harness.project_validation_result import scale_validation_manifest_sha256

    scale = case_id.split(":", 1)[0]
    if scale not in {"large", "ultra"}:
        return None
    # Synthetic complete evidence for integration gates, never native acceptance credit.
    filename = "large_module_result.json" if scale == "large" else "ultra_storage_result.json"
    fixture = Path(__file__).resolve().parents[1] / "fixtures/project_business" / filename
    return {
        "case_id": case_id, "run_id": run_id,
        "manifest_sha256": scale_validation_manifest_sha256(validated_workspace_manifest()),
        "result": json.loads(fixture.read_text(encoding="utf-8")),
    }


def public_artifact_evidence() -> dict[str, object]:
    return {
        "ok": True,
        "source": "public_workspace_api",
        "validated_bundle_matches": True,
        "workspace_manifest": {
            path: list(item) for path, item in validated_workspace_manifest().items()
        },
        "file_count": 2,
        "downloaded_file_count": 2,
        "zip_member_count": 2,
    }


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
        validated_workspace_manifest=validated_workspace_manifest(),
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
        validated_workspace_manifest=validated_workspace_manifest(),
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
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
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
        validated_workspace_manifest=validated_workspace_manifest(),
        scale_validation=scale_validation_evidence("large:direct", "run-large"),
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
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
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
        validated_workspace_manifest=validated_workspace_manifest(),
        scale_validation=scale_validation_evidence("large:auto", "run-large-repair"),
        case_id="large:auto",
        run_id="run-large-repair",
        status="completed",
        observed_mode="hybrid",
        final_observed_mode="direct",
        requested_mode="direct",
        effective_scale="large",
        final_effective_scale="large",
        artifact_origin="incremental_workspace_delivery",
        workspace_bundle_source="public_workspace_api",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
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
        validated_workspace_manifest=validated_workspace_manifest(),
        case_id="large:auto",
        run_id="run-large-repair",
        status="completed",
        observed_mode="direct",
        final_observed_mode="hybrid",
        requested_mode="hybrid",
        effective_scale="large",
        final_effective_scale="large",
        artifact_origin="incremental_workspace_delivery",
        workspace_bundle_source="public_workspace_api",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["core_acceptance_ok"] is False
    assert payload["route_policy_ok"] is False
    assert payload["route_observed_mode"] == "direct"


def test_report_keeps_explicit_mode_coverage_bound_to_final_run() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        validated_workspace_manifest=validated_workspace_manifest(),
        case_id="large:hybrid",
        run_id="run-large-repair",
        status="completed",
        observed_mode="hybrid",
        final_observed_mode="direct",
        requested_mode="direct",
        effective_scale="large",
        final_effective_scale="large",
        artifact_origin="incremental_workspace_delivery",
        workspace_bundle_source="public_workspace_api",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )

    payload = module.build_case_report(
        scale="large",
        project={"project_id": "project-large"},
        conversation={"conversation_id": "conv-large"},
        result=case,
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["core_acceptance_ok"] is False
    assert payload["route_policy_ok"] is False
    assert payload["route_observed_mode"] == "direct"


def test_report_rejects_missing_effective_scale_instead_of_using_expected_scale() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        validated_workspace_manifest=validated_workspace_manifest(),
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
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["effective_scale"] is None
    assert payload["scale_fidelity_ok"] is False
    assert payload["core_acceptance_ok"] is False


@pytest.mark.parametrize("expected, final", [
    ("large", "small"), ("ultra", "large"), ("medium", "ultra"),
    ("large", ""), ("ultra", "unknown"), ("large", None),
])
def test_report_rejects_changed_or_unknown_completion_scale_after_repair(
    expected: str, final: str | None,
) -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        validated_workspace_manifest=validated_workspace_manifest(),
        case_id=f"{expected}:hybrid", run_id="run-repaired", status="completed",
        observed_mode="hybrid", final_observed_mode="hybrid", requested_mode="hybrid",
        effective_scale=expected, final_effective_scale=final,
        artifact_origin="model_workspace_bundle", workspace_bundle_source="embedded_bundle",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )
    payload = module.build_case_report(
        scale=expected, project={"project_id": "project-scale"},
        conversation={"conversation_id": "conv-scale"}, result=case,
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )
    assert payload["scale_fidelity_ok"] is False
    assert payload["core_acceptance_ok"] is False
    assert payload["initial_effective_scale"] == expected
    assert payload["final_effective_scale"] == final


@pytest.mark.parametrize("scale", ["small", "medium", "large", "ultra"])
def test_report_preserves_matching_completion_scale_after_repair(scale: str) -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        validated_workspace_manifest=validated_workspace_manifest(),
        case_id=f"{scale}:hybrid", run_id="run-repaired", status="completed",
        scale_validation=scale_validation_evidence(f"{scale}:hybrid", "run-repaired"),
        observed_mode="hybrid", final_observed_mode="hybrid", requested_mode="hybrid",
        effective_scale=scale, final_effective_scale=scale,
        artifact_origin="model_workspace_bundle", workspace_bundle_source="embedded_bundle",
        evidence={**_passing_evidence(), "deliverable_repair_trace": True},
    )
    payload = module.build_case_report(
        scale=scale, project={"project_id": "project-scale"},
        conversation={"conversation_id": "conv-scale"}, result=case,
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )
    assert payload["scale_fidelity_ok"] is True
    assert payload["core_acceptance_ok"] is True
    assert payload["initial_effective_scale"] == payload["final_effective_scale"] == scale


def test_report_rejects_unverified_mode_change_and_fixture_artifact_origin() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        validated_workspace_manifest=validated_workspace_manifest(),
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
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert payload["route_policy_ok"] is False
    assert payload["artifact_origin_ok"] is False
    assert payload["core_acceptance_ok"] is False


def test_report_rejects_medium_case_when_effective_scale_drifts_to_large() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        validated_workspace_manifest=validated_workspace_manifest(),
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
        model_scope_evidence=_scope_evidence(str(case.run_id), "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
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


def test_ultra_report_rejects_missing_independent_load_evidence() -> None:
    module = load_script()
    result = ProjectScaleCaseResult(
        case_id="ultra:direct", run_id="run-ultra", status="completed",
        evidence=_passing_evidence(), observed_mode="direct", final_observed_mode="direct",
        requested_mode="direct", effective_scale="ultra", final_effective_scale="ultra",
        artifact_origin="tool_workspace_write", workspace_bundle_source="public_workspace_api",
        validated_workspace_manifest=validated_workspace_manifest(),
    )

    report = module.build_case_report(
        scale="ultra", project={"project_id": "project-ultra"},
        conversation={"conversation_id": "conv-ultra"}, result=result,
        model_scope_evidence=_scope_evidence("run-ultra", "deepseek-backup"),
        public_artifacts=public_artifact_evidence(),
        dynamic_web_preview={"counted_as_passed": True},
    )

    assert report["core_acceptance_ok"] is False
    assert report["scale_specific_evidence_ok"] is False


_FINALIZER_CASE_IDS = tuple(
    f"{scale}:{route}"
    for scale in ("small", "medium", "large", "ultra")
    for route in ("auto", "direct", "dispatch", "hybrid", "multi_agent")
)


def _model_event(run_id: str, logical_model: str = "deepseek-backup") -> dict[str, Any]:
    from uuid import NAMESPACE_URL, uuid5

    from agent_hub.runs.repository import _public_event_payload
    from agent_hub.runtime.contracts import Artifact, GatewayProvenance

    artifact = Artifact(
        id=uuid5(NAMESPACE_URL, f"operator-test:{run_id}:{logical_model}"),
        type="model_response",
        producer="architect",
        provenance=GatewayProvenance(
            logical_model=logical_model, deployment_id="fixture-deployment",
            provider_id="fixture-provider", provider_model="fixture-provider/fixture-model",
        ),
        content={"attempted_logical_models": (logical_model,), "text": "Fixed test response"},
    )
    event = _public_event_payload(
        {
            "kind": "artifact.created",
            "run_id": run_id,
            "sequence": 1,
            "actor": "architect",
            "payload": {
                "artifact_id": str(artifact.id),
                "logical_model": logical_model,
                "attempted_logical_models": [logical_model],
            },
            "artifact": artifact.to_payload(),
        }
    )
    public_artifact = cast(dict[str, Any], event["artifact"])
    event["model_artifact"] = {
        "source": "public_event_artifact",
        **{key: copy.deepcopy(public_artifact[key]) for key in (
            "id", "type", "producer", "version", "source_ids", "content_sha256",
            "public_content_sha256", "content_redacted", "provenance",
        )},
        "hash_verified": True,
        "hash_verification_scope": "public_projection",
    }
    return event


def _scope_evidence(run_id: str, logical_model: str) -> dict[str, Any]:
    event = _model_event(run_id, logical_model)
    event.pop("artifact")
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
                "model_events": [event],
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
            validated_workspace_manifest=validated_workspace_manifest(),
            case_id=case_id,
            run_id=f"run-{case_id}",
            scale_validation=scale_validation_evidence(case_id, f"run-{case_id}"),
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
                logical_model=logical_model,
                model_scope_evidence=_scope_evidence(
                    str(result.run_id), logical_model or "deepseek-backup",
                ),
                public_artifacts={
                    **public_artifact_evidence(),
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
        "actor": {"principal": {"user_id": "00000000-0000-4000-8000-000000000002", "tenant_id": "00000000-0000-4000-8000-000000000001"}},
        "execution_identity": {
            "execution_id": "matrix-123",
            "base_url": "http://example.test",
            "user_id": "00000000-0000-4000-8000-000000000002",
            "tenant_id": "00000000-0000-4000-8000-000000000001",
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
    journal = module.SubmissionJournal(report["execution_identity"])
    for case in cases:
        scale, route = case["case_id"].split(":")
        case_key = f"auto-{scale}" if route == "auto" else f"mode-{scale}-{route.replace('_', '-')}"
        scope = f"matrix-123-{case_key}"
        context = {
            "case_id": case["case_id"], "attempt": 1,
            "project_id": case["project"]["project_id"],
            "conversation_id": case["conversation"]["conversation_id"],
            "workspace_session_id": case["conversation"]["workspace_path"],
        }
        plan = module.build_real_user_scale_plan(
            scale=scale, route_intent=route, project_id=context["project_id"],
            project_label=(
                f"真实用户 {scale} AUTO 规模验收" if route == "auto"
                else f"真实用户 {route} 模式能力验收"
            ), conversation_id=context["conversation_id"],
            workspace_session_id=context["workspace_session_id"], logical_model=logical_model,
        )
        index, _ = journal.prepare(
            path="/api/v1/runs", body=json.loads(json.dumps(plan.requests[0].body)), context=context,
            idempotency_key=_idempotency_key(case["case_id"], 0, execution_id=scope),
            persist=lambda: None,
        )
        journal.confirm(index, {
            "id": case["run"]["run_id"], "tenant_id": "00000000-0000-4000-8000-000000000001", "status": "completed",
            "project_id": context["project_id"], "conversation_id": context["conversation_id"],
            "workspace_session_id": context["workspace_session_id"], "version": 1,
        }, lambda: None)
    report["submission_journal"] = journal.snapshot()
    return report


def _safe_upgrade_cases(module: Any, report: dict[str, Any]) -> None:
    for case in report["cases"]:
        if case["case_id"] not in {"large:direct", "ultra:direct"}:
            continue
        run = case["run"]
        result = ProjectScaleCaseResult(
            case_id=case["case_id"], run_id=run["run_id"], status="completed",
            evidence=copy.deepcopy(run["evidence"]), observed_mode="hybrid",
            final_observed_mode="hybrid", requested_mode="direct",
            route_reason="project_scale_mode_upgrade", mode_source="project_scale_assessment",
            effective_scale=case["scale"], final_effective_scale=case["scale"],
            artifact_origin=run["artifact_origin"],
            workspace_bundle_source=run["workspace_bundle_source"],
            validated_workspace_manifest=validated_workspace_manifest(),
            scale_validation=copy.deepcopy(run["scale_validation"]),
        )
        rebuilt = module.build_case_report(
            scale=case["scale"], project=case["project"], conversation=case["conversation"],
            result=result, public_artifacts=case["public_artifacts"],
            dynamic_web_preview=case["dynamic_web_preview"],
            model_scope_evidence=case["model_scope_evidence"],
        )
        for key in ("status", "acceptance_complete", "real_device_acceptance_complete"):
            rebuilt[key] = case[key]
        case.update(rebuilt)


def _mode_matrix_report(module: Any, saved: dict[str, Any]) -> dict[str, Any]:
    client = module.RealUserAcceptanceClient(None)
    client.configure_submission_journal(
        module.SubmissionJournal(saved["execution_identity"], saved["submission_journal"]),
        lambda: None,
    )
    return cast(dict[str, Any], module._matrix_report(
        client=client, cases=saved["cases"], attempt_history=saved.get("attempt_history", []),
        username="test", principal=saved["actor"]["principal"], base_url=saved["base_url"],
        execution_id=saved["execution_id"], identity=saved["execution_identity"],
        started_at="2026-10-05T00:00:00Z", authentication_method="bearer_token", finished=True,
    ))


@pytest.mark.parametrize("upgrades", [False, True])
@pytest.mark.parametrize("reordered", [False, True])
def test_mode_coverage_counts_only_current_canonical_core_cases(
    upgrades: bool, reordered: bool,
) -> None:
    module = load_script()
    saved = _pending_automated_report()
    if upgrades:
        _safe_upgrade_cases(module, saved)
    if reordered:
        saved["cases"].reverse()
    before = copy.deepcopy(saved)
    coverage = module._matrix_mode_coverage(saved["cases"], execution_id="matrix-123")
    assert coverage == {
        "auto_scale_case_count": 4,
        "mode_capability_case_count": 16,
        "core_passed_case_count": 20,
        "auto_scale_passed_case_count": 4,
        "exact_mode_passed_case_count": 14 if upgrades else 16,
        "safe_upgrade_case_count": 2 if upgrades else 0,
        "missing_auto_scale_case_ids": [],
        "missing_exact_mode_case_ids": ["large:direct", "ultra:direct"] if upgrades else [],
        "exact_mode_coverage_complete": not upgrades,
    }
    assert saved == before


@pytest.mark.parametrize("count,auto,exact", [(0, 0, 0), (1, 1, 0), (4, 4, 0), (19, 4, 15)])
def test_mode_coverage_partial_checkpoints_keep_full_denominators(
    count: int, auto: int, exact: int,
) -> None:
    module = load_script()
    canonical_ids = (
        "small:auto", "medium:auto", "large:auto", "ultra:auto",
        *(f"{scale}:{mode}" for scale in ("small", "medium", "large", "ultra")
          for mode in ("direct", "dispatch", "hybrid", "multi_agent")),
    )
    by_id = {case["case_id"]: case for case in _pending_automated_report()["cases"]}
    cases = [by_id[case_id] for case_id in canonical_ids[:count]]
    coverage = module._matrix_mode_coverage(cases, execution_id="matrix-123")
    assert coverage["core_passed_case_count"] == count
    assert coverage["auto_scale_case_count"] == 4
    assert coverage["mode_capability_case_count"] == 16
    assert coverage["auto_scale_passed_case_count"] == auto
    assert coverage["exact_mode_passed_case_count"] == exact
    assert coverage["safe_upgrade_case_count"] == 0
    assert coverage["missing_auto_scale_case_ids"] == list(canonical_ids[auto:4])
    assert coverage["missing_exact_mode_case_ids"] == list(canonical_ids[4 + exact:])
    assert coverage["exact_mode_coverage_complete"] is False


@pytest.mark.parametrize("consumer", ["coverage", "matrix", "resume", "finalize"])
@pytest.mark.parametrize("change", [
    "duplicate", "unknown", "missing_id", "empty_id", "null_id", "integer_id", "list_id",
    "whitespace_id", "scale", "route", "run_case", "run_missing_id", "null_run", "non_object",
])
def test_mode_coverage_rejects_bad_identity_before_credit_or_resume(
    consumer: str, change: str,
) -> None:
    module = load_script()
    saved = _pending_automated_report()
    case = saved["cases"][0]
    if change == "duplicate":
        saved["cases"][-1] = copy.deepcopy(case)
    elif change == "unknown":
        case["case_id"] = "unknown:auto"
    elif change == "missing_id":
        del case["case_id"]
    elif change in {"empty_id", "null_id", "integer_id", "list_id", "whitespace_id"}:
        case["case_id"] = {
            "empty_id": "", "null_id": None, "integer_id": 1, "list_id": ["small:auto"],
            "whitespace_id": " small:auto ",
        }[change]
    elif change == "scale":
        case["scale"] = "large"
    elif change == "route":
        case["route_intent"] = "direct"
    elif change == "run_case":
        case["run"]["case_id"] = "small:direct"
    elif change == "run_missing_id":
        del case["run"]["case_id"]
    elif change == "null_run":
        case["run"] = None
    elif change == "non_object":
        saved["cases"][0] = None
    before = copy.deepcopy(saved)
    with pytest.raises((ValueError, TypeError)):
        if consumer == "coverage":
            module._matrix_mode_coverage(saved["cases"], execution_id="matrix-123")
        elif consumer == "matrix":
            _mode_matrix_report(module, saved)
        elif consumer == "resume":
            module._resume_cases(saved, saved["execution_identity"], [])
        else:
            module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert saved == before


@pytest.mark.parametrize("mutation", [
    "failed_without_run", "missing_participation", "forged_credit", "forged_core",
])
def test_mode_coverage_revalidates_bottom_level_evidence_and_ignores_history(mutation: str) -> None:
    module = load_script()
    saved = _pending_automated_report()
    _safe_upgrade_cases(module, saved)
    case = next(case for case in saved["cases"] if case["case_id"] == "small:multi_agent")
    saved["attempt_history"] = copy.deepcopy(_pending_automated_report()["cases"])
    if mutation == "failed_without_run":
        del case["run"]
        case.update(status="failed", core_acceptance_ok=False, automated_acceptance_complete=False)
    elif mutation == "missing_participation":
        case["run"]["participant_agent_ids"] = []
    elif mutation == "forged_credit":
        case["coverage_credit"] = "safe_upgrade"
    else:
        case["run"]["evidence"]["generated_project_validation"] = False
    payload = _mode_matrix_report(module, saved)
    assert payload["core_passed_case_count"] == 19
    assert payload["auto_scale_passed_case_count"] == 4
    assert payload["exact_mode_passed_case_count"] == 13
    assert payload["safe_upgrade_case_count"] == 2
    assert payload["missing_exact_mode_case_ids"] == [
        "small:multi_agent", "large:direct", "ultra:direct",
    ]
    assert payload["core_acceptance_ok"] is False
    assert payload["automated_acceptance_complete"] is False
    assert payload["status"] == "failed"


def test_mode_coverage_missing_auto_blocks_completion_even_with_all_explicit_modes() -> None:
    module = load_script()
    saved = _pending_automated_report()
    saved["cases"][0]["run"]["evidence"]["generated_project_validation"] = False
    coverage = module._matrix_mode_coverage(saved["cases"], execution_id="matrix-123")
    assert coverage["core_passed_case_count"] == 19
    assert coverage["auto_scale_passed_case_count"] == 3
    assert coverage["exact_mode_passed_case_count"] == 16
    assert coverage["missing_auto_scale_case_ids"] == ["small:auto"]
    assert coverage["missing_exact_mode_case_ids"] == []
    assert coverage["exact_mode_coverage_complete"] is False


@pytest.mark.parametrize("forged_summary", [False, True])
def test_mode_coverage_finalizer_rejects_consistent_safe_upgrades(forged_summary: bool) -> None:
    module = load_script()
    saved = _pending_automated_report()
    _safe_upgrade_cases(module, saved)
    if forged_summary:
        saved.update(
            auto_scale_passed_case_count=4, exact_mode_passed_case_count=16,
            safe_upgrade_case_count=0, missing_auto_scale_case_ids=[],
            missing_exact_mode_case_ids=[], exact_mode_coverage_complete=True,
        )
    before = copy.deepcopy(saved)
    with pytest.raises(ValueError, match="mode coverage"):
        module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert saved == before


@pytest.mark.parametrize("field,value", [
    ("core_passed_case_count", 19), ("auto_scale_case_count", 3),
    ("mode_capability_case_count", 15), ("auto_scale_passed_case_count", 3),
    ("exact_mode_passed_case_count", 14), ("safe_upgrade_case_count", 2),
    ("missing_auto_scale_case_ids", ["small:auto"]),
    ("missing_exact_mode_case_ids", ["large:direct"]),
    ("exact_mode_coverage_complete", False), ("exact_mode_coverage_complete", 1),
    ("safe_upgrade_case_count", False), ("exact_mode_passed_case_count", 16.0),
])
def test_mode_coverage_finalizer_rejects_inconsistent_supplied_summaries(
    field: str, value: object,
) -> None:
    module = load_script()
    saved = _pending_automated_report()
    saved[field] = value
    with pytest.raises(ValueError, match="coverage"):
        module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())


def test_mode_coverage_finalizer_recomputes_legacy_pending_report() -> None:
    module = load_script()
    saved = _pending_automated_report()
    before = copy.deepcopy(saved)
    completed = module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert completed["core_passed_case_count"] == 20
    assert completed["auto_scale_case_count"] == 4
    assert completed["mode_capability_case_count"] == 16
    assert completed["auto_scale_passed_case_count"] == 4
    assert completed["exact_mode_passed_case_count"] == 16
    assert completed["safe_upgrade_case_count"] == 0
    assert completed["missing_auto_scale_case_ids"] == []
    assert completed["missing_exact_mode_case_ids"] == []
    assert completed["exact_mode_coverage_complete"] is True
    assert completed["acceptance_complete"] is True
    assert saved == before


def test_mode_coverage_resume_keeps_completed_upgrades_without_new_plans_or_attempts(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    _safe_upgrade_cases(module, saved)
    saved["attempt_history"] = []
    before = copy.deepcopy(saved)
    output = tmp_path / "pending.json"
    resumed = run_matrix(module, delegate, resume_report=saved, output_path=str(output))
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []
    assert {case["case_id"]: case for case in resumed["cases"]} == {
        case["case_id"]: case for case in before["cases"]
    }
    assert resumed["attempt_history"] == []
    assert resumed["submission_journal"] == before["submission_journal"]
    assert resumed["core_acceptance_ok"] is True
    assert resumed["automated_acceptance_complete"] is False
    assert resumed["status"] == "pending_mode_coverage"
    assert resumed["core_passed_case_count"] == 20
    assert resumed["exact_mode_passed_case_count"] == 14
    assert resumed["safe_upgrade_case_count"] == 2
    assert resumed["exact_mode_coverage_complete"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == resumed
    assert saved == before


def test_mode_coverage_cli_returns_pending_and_preserves_finalization_source(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path, monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    _safe_upgrade_cases(module, saved)
    output, device = tmp_path / "pending.json", tmp_path / "device.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    device.write_text(json.dumps(_real_device_evidence("matrix-123")), encoding="utf-8")
    before = output.read_bytes()
    assert module.main([
        "--finalize-report", str(output), "--real-device-evidence", str(device),
        "--output", str(output),
    ]) == 1
    assert output.read_bytes() == before
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-token")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    assert module.main([
        "--base-url", "http://example.test", "--evidence-root", str(_evidence_root()), "--resume-report", str(output),
        "--output", str(output),
    ]) == 2
    pending = json.loads(output.read_text(encoding="utf-8"))
    assert pending["status"] == "pending_mode_coverage"
    assert {case["case_id"]: case for case in pending["cases"]} == {
        case["case_id"]: case for case in saved["cases"]
    }
    assert pending["attempt_history"] == []
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []


@pytest.mark.parametrize("mutation", ["safe_upgrades", "missing_summary", "forged_summary"])
def test_mode_coverage_finalized_cli_resume_fails_closed_without_writes_or_posts(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, mutation: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = module.finalize_real_device_acceptance(
        _pending_automated_report(), _real_device_evidence("matrix-123"),
        evidence_root=_evidence_root(),
    )
    saved["attempt_history"] = [{"case_id": "small:auto", "attempt": 1, "status": "failed"}]
    if mutation == "safe_upgrades":
        _safe_upgrade_cases(module, saved)
    elif mutation == "missing_summary":
        for key in (
            "auto_scale_passed_case_count", "exact_mode_passed_case_count", "safe_upgrade_case_count",
            "missing_auto_scale_case_ids", "missing_exact_mode_case_ids", "exact_mode_coverage_complete",
        ):
            saved.pop(key, None)
    else:
        saved["exact_mode_passed_case_count"] = 14
    output = tmp_path / "finalized.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    before = output.read_bytes()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-token")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    assert module.main([
        "--base-url", "http://example.test", "--evidence-root", str(_evidence_root()), "--resume-report", str(output),
        "--output", str(output),
    ]) == 1
    assert output.read_bytes() == before
    assert json.loads(output.read_text(encoding="utf-8")) == saved
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []


@pytest.mark.parametrize("logical_model", [None, "deepseek-backup"])
def test_browser_collection_scope_allows_partial_matrix_without_promotion(
    logical_model: str | None,
) -> None:
    module = load_script()
    report = _pending_automated_report(logical_model)
    report["cases"] = report["cases"][:1]
    report["submission_journal"]["records"] = report["submission_journal"]["records"][:1]
    report.update(status="in_progress", core_acceptance_ok=False,
                  automated_acceptance_complete=False, case_count=1)
    before = copy.deepcopy(report)
    collected = module.case_browser_collection_scope(report, "small:auto")
    case = report["cases"][0]
    assert collected == {
        "scope": {
            "execution_identity": report["execution_identity"],
            "case_id": "small:auto",
            "project_id": case["project"]["project_id"],
            "conversation_id": case["conversation"]["conversation_id"],
            "run_id": case["run"]["run_id"],
            "workspace_session_id": case["conversation"]["workspace_path"],
        },
        "validated_manifest": {
            path: list(item) for path, item in validated_workspace_manifest().items()
        },
    }
    collected["scope"]["execution_identity"]["base_url"] = "https://foreign.invalid"
    assert report == before


@pytest.mark.parametrize("mutation", [
    "case_missing", "duplicate", "identity", "profile", "journal", "unresolved",
    "request_digest", "model", "public", "scale", "workspace", "nonterminal",
])
def test_browser_collection_scope_revalidates_existing_case_evidence(mutation: str) -> None:
    module = load_script()
    report = _pending_automated_report("deepseek-backup")
    case = report["cases"][-1]
    case_id = case["case_id"]
    if mutation == "case_missing":
        report["cases"].pop()
    elif mutation == "duplicate":
        report["cases"].append(copy.deepcopy(case))
    elif mutation == "identity":
        report["execution_identity"]["tenant_id"] = "foreign"
    elif mutation == "profile":
        case["model_profile"] = {"direct_model": "foreign", "allowed_models": ["foreign"]}
    elif mutation == "journal":
        report.pop("submission_journal")
    elif mutation == "unresolved":
        report["submission_journal"]["records"][-1]["state"] = "unresolved"
        report["submission_journal"]["records"][-1]["response"] = None
    elif mutation == "request_digest":
        report["submission_journal"]["records"][-1]["request_sha256"] = "0" * 64
    elif mutation == "model":
        case["model_scope_evidence"]["runs"][0]["model_events"] = []
    elif mutation == "public":
        case["public_artifacts"]["workspace_manifest"] = {}
    elif mutation == "scale":
        case["run"]["scale_validation"] = None
    elif mutation == "workspace":
        case["conversation"]["workspace_path"] = "foreign"
    else:
        case["run"]["status"] = "running"
    before = copy.deepcopy(report)
    with pytest.raises((TypeError, ValueError)):
        module.case_browser_collection_scope(report, case_id)
    assert report == before


def test_finalizer_rejects_legacy_flags_only_browser_evidence() -> None:
    module = load_script()
    report = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    evidence["schema_version"] = 1
    for case in evidence["cases"].values():
        for device in ("desktop", "mobile"):
            case[device].pop("bundle_file", None)
            case[device].pop("schema_version", None)
    before_report, before_evidence = copy.deepcopy(report), copy.deepcopy(evidence)
    with pytest.raises(ValueError, match="schema_version"):
        module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
    assert report == before_report
    assert evidence == before_evidence


def _real_device_evidence(
    execution_id: str, logical_model: str | None = None, *, evidence_root: Path | None = None,
) -> dict[str, Any]:
    root = _evidence_root() if evidence_root is None else evidence_root
    report = _pending_automated_report(logical_model)
    identity = copy.deepcopy(report["execution_identity"])
    identity["execution_id"] = execution_id
    checks = {
        "login": True,
        "project_navigation": True,
        "preview_rendered": True,
        "preview_interaction": True,
        "preview_revoked": True,
    }
    evidence: dict[str, Any] = {
        "schema_version": 2,
        "execution_id": execution_id,
        "execution_identity": identity,
        "desktop_browser_interaction": {
            "passed": True,
            "observed_at": "2026-10-05T12:00:00+00:00",
            "viewport": {"width": 1440, "height": 960},
            "checks": checks,
        },
        "mobile_browser_interaction": {
            "passed": True,
            "observed_at": "2026-10-05T12:05:00+00:00",
            "viewport": {"width": 390, "height": 844},
            "checks": checks,
        },
        "cases": {},
    }
    batch = uuid4().hex
    for case in report["cases"]:
        case_id = case["case_id"]
        scope = {
            "execution_identity": identity,
            "case_id": case_id,
            "project_id": case["project"]["project_id"],
            "conversation_id": case["conversation"]["conversation_id"],
            "run_id": case["run"]["run_id"],
            "workspace_session_id": case["conversation"]["workspace_path"],
        }
        evidence["cases"][case_id] = {
            key: scope[key] for key in ("project_id", "conversation_id", "run_id")
        }
        for device in ("desktop", "mobile"):
            evidence["cases"][case_id][device] = build_device_bundle(
                root, scope=scope,
                validated_manifest=validated_workspace_manifest(), device=device,
                stem=f"{batch}-{case_id.replace(':', '-')}",
            )
    if logical_model is not None:
        evidence["model_profile"] = copy.deepcopy(report["model_profile"])
    return evidence


@pytest.mark.parametrize("root", [None, "untrusted"])
def test_file_backed_finalizer_requires_caller_selected_trusted_root(root: object) -> None:
    module = load_script()
    report = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    evidence["evidence_root"] = str(_evidence_root())
    before = copy.deepcopy(report)
    with pytest.raises((TypeError, ValueError), match="evidence_root"):
        module.finalize_real_device_acceptance(report, evidence, evidence_root=root)
    assert report == before


@pytest.mark.parametrize("mutation", ["schema1", "missing_bundle", "foreign_case", "wrong_device"])
def test_finalizer_rejects_flags_only_or_replayed_device_bundle(mutation: str) -> None:
    module = load_script()
    report = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    case = evidence["cases"]["small:auto"]
    if mutation == "schema1":
        case["mobile"]["schema_version"] = 1
    elif mutation == "missing_bundle":
        case["mobile"].pop("bundle_file")
    elif mutation == "foreign_case":
        case["mobile"] = copy.deepcopy(evidence["cases"]["medium:auto"]["mobile"])
    else:
        case["mobile"] = copy.deepcopy(case["desktop"])
    before_report, before_evidence = copy.deepcopy(report), copy.deepcopy(evidence)
    with pytest.raises((TypeError, ValueError)):
        module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
    assert report == before_report
    assert evidence == before_evidence


def test_finalize_cli_explicit_root_overrides_only_evidence_file_parent(
    tmp_path: Path,
) -> None:
    module = load_script()
    automated = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    evidence["evidence_root"] = str(_evidence_root())
    reports = tmp_path / "reports"
    reports.mkdir()
    source, proof, output = (reports / name for name in ("source.json", "proof.json", "out.json"))
    source.write_text(json.dumps(automated), encoding="utf-8")
    proof.write_text(json.dumps(evidence), encoding="utf-8")
    before = source.read_bytes()
    args = ["--finalize-report", str(source), "--real-device-evidence", str(proof),
            "--output", str(output)]
    assert module.main(args) == 1
    assert source.read_bytes() == before
    assert json.loads(output.read_text(encoding="utf-8"))["acceptance_complete"] is False
    assert module.main([*args, "--evidence-root", str(_evidence_root())]) == 0
    completed = json.loads(output.read_text(encoding="utf-8"))
    assert completed["acceptance_complete"] is True
    assert completed["real_device_acceptance"]["schema_version"] == 2
    assert source.read_bytes() == before
    assert "evidence_root" not in completed
    assert "evidence_root" not in completed["real_device_acceptance"]


@pytest.mark.parametrize("device", ["desktop", "mobile"])
@pytest.mark.parametrize("role", ["bundle", "viewport_png", "render", "business", "provenance", "cleanup"])
@pytest.mark.parametrize("damage", ["deleted", "corrupt"])
def test_finalized_resume_rereads_each_device_file_without_writes_or_tasks(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    device: str, role: str, damage: str, monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = module.finalize_real_device_acceptance(
        _pending_automated_report(), _real_device_evidence("matrix-123"),
        evidence_root=_evidence_root(),
    )
    descriptor = saved["real_device_acceptance"]["cases"]["small:auto"][device]
    bundle_path = _evidence_root() / descriptor["bundle_file"]["path"]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    target = bundle_path if role == "bundle" else _evidence_root() / bundle["files"][role]["path"]
    if damage == "deleted":
        target.unlink()
    else:
        target.write_bytes(b"corrupted operator observation")
    output = tmp_path / "finalized.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    before = output.read_bytes()
    evidence_files = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def forbidden_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("finalized resume must not save or promote evidence")

    monkeypatch.setattr(module, "_save_report", forbidden_write)
    with pytest.raises((TypeError, ValueError)):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)
    assert output.read_bytes() == before
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == evidence_files
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []


@pytest.mark.parametrize("damage", ["missing_root", "wrong_root", "deleted", "corrupt"])
def test_finalized_cli_requires_explicit_root_and_revalidates_files(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    damage: str, monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = module.finalize_real_device_acceptance(
        _pending_automated_report(), _real_device_evidence("matrix-123"),
        evidence_root=_evidence_root(),
    )
    saved["evidence_root"] = str(_evidence_root())
    if damage in {"deleted", "corrupt"}:
        descriptor = saved["real_device_acceptance"]["cases"]["ultra:multi_agent"]["mobile"]
        target = _evidence_root() / descriptor["bundle_file"]["path"]
        if damage == "deleted":
            target.unlink()
        else:
            target.write_bytes(b"corrupt bundle")
    output = tmp_path / "finalized.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    before = output.read_bytes()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-token")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    args = ["--base-url", "http://example.test", "--resume-report", str(output)]
    if damage != "missing_root":
        root = tmp_path / "wrong" if damage == "wrong_root" else _evidence_root()
        args += ["--evidence-root", str(root)]
    assert module.main(args) == 1
    assert output.read_bytes() == before
    assert json.loads(before) == saved
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []


def test_rejected_finalized_cli_resume_never_writes_alternate_output(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path, monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness
    report = _pending_automated_report()
    report.update(status="passed", acceptance_complete=True)
    source, output = tmp_path / "source.json", tmp_path / "alternate.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    before = source.read_bytes()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-token")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    assert module.main([
        "--base-url", "http://example.test", "--resume-report", str(source),
        "--evidence-root", str(_evidence_root()), "--output", str(output),
    ]) == 1
    assert source.read_bytes() == before
    assert not output.exists()
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert plans == []


def _corrupt_device_review_evidence(evidence: dict[str, Any], mutation: str) -> None:
    if mutation == "missing_identity":
        evidence.pop("execution_identity", None)
    elif mutation == "null_identity":
        evidence["execution_identity"] = None
    elif mutation == "missing_user":
        evidence["execution_identity"].pop("user_id", None)
    elif mutation.startswith("identity_"):
        evidence["execution_identity"][mutation.removeprefix("identity_")] = "foreign"
    elif mutation == "base_url":
        evidence["base_url"] = "https://other.invalid"
    elif mutation == "null_base_url":
        evidence["base_url"] = None
    elif mutation == "actor":
        evidence["actor"] = {"principal": {"user_id": "foreign", "tenant_id": "00000000-0000-4000-8000-000000000001"}}
    elif mutation == "null_actor":
        evidence["actor"] = None
    elif mutation == "missing_principal":
        evidence["actor"] = {}
    elif mutation == "null_principal":
        evidence["actor"] = {"principal": None}
    elif mutation == "missing_tenant":
        evidence["actor"] = {"principal": {"user_id": "00000000-0000-4000-8000-000000000002"}}
    elif mutation == "schema_missing":
        evidence.pop("schema_version", None)
    elif mutation.startswith("schema_"):
        evidence["schema_version"] = {
            "schema_bool": True, "schema_float": 1.0, "schema_unknown": 99,
            "schema_null": None, "schema_string": "1",
        }[mutation]
    elif mutation == "viewport":
        evidence["mobile_browser_interaction"]["viewport"] = {"width": True, "height": True}
    else:
        raise AssertionError(f"unknown device review mutation: {mutation}")


@pytest.mark.parametrize("finalized", [False, True])
@pytest.mark.parametrize("mutation", [
    "missing_identity", "null_identity", "missing_user", "identity_execution_id",
    "identity_base_url", "identity_user_id", "identity_tenant_id", "identity_extra",
    "base_url", "null_base_url", "actor", "null_actor", "missing_principal",
    "null_principal", "missing_tenant", "schema_missing", "schema_bool", "schema_float",
    "schema_unknown", "schema_null", "schema_string",
])
def test_device_review_fix_rejects_unbound_scope_and_unsupported_schema(
    finalized: bool, mutation: str,
) -> None:
    module = load_script()
    report = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    if finalized:
        report = module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
        assert report["real_device_acceptance"].get("execution_identity") == (
            evidence["execution_identity"]
        )
        evidence = report["real_device_acceptance"]
    _corrupt_device_review_evidence(evidence, mutation)
    before_report, before_evidence = copy.deepcopy(report), copy.deepcopy(evidence)
    with pytest.raises((TypeError, ValueError)):
        if finalized:
            module._validated_finalized_report(report, evidence_root=_evidence_root())
        else:
            module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
    assert report == before_report
    assert evidence == before_evidence


@pytest.mark.parametrize("finalized", [False, True])
@pytest.mark.parametrize("profile", [None, {}, {
    "direct_model": "foreign", "allowed_models": ["foreign"],
}])
def test_device_review_fix_binds_model_profile_inside_device_identity(
    finalized: bool, profile: object,
) -> None:
    module = load_script()
    report = _pending_automated_report("deepseek-backup")
    evidence = _real_device_evidence("matrix-123", "deepseek-backup")
    if finalized:
        report = module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
        assert report["real_device_acceptance"].get("execution_identity") == (
            evidence["execution_identity"]
        )
        evidence = report["real_device_acceptance"]
    if profile is None:
        evidence["execution_identity"].pop("model_profile")
    else:
        evidence["execution_identity"]["model_profile"] = profile
    with pytest.raises((TypeError, ValueError), match="identity"):
        if finalized:
            module._validated_finalized_report(report, evidence_root=_evidence_root())
        else:
            module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())


@pytest.mark.parametrize("stage", ["helper", "finalize", "resume"])
@pytest.mark.parametrize("device,dimension,value", [
    ("mobile", "width", True), ("mobile", "height", True), ("desktop", "height", True),
    ("desktop", "width", 1440.0), ("mobile", "width", 390.0),
    ("desktop", "height", None), ("mobile", "height", None),
    ("desktop", "height", 0), ("mobile", "width", 0), ("mobile", "width", False),
])
def test_device_review_fix_viewports_require_exact_positive_integers(
    stage: str, device: str, dimension: str, value: object,
) -> None:
    module = load_script()
    report = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    if stage == "resume":
        report = module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
        evidence = report["real_device_acceptance"]
    key = f"{device}_browser_interaction"
    evidence[key]["viewport"][dimension] = value
    with pytest.raises((TypeError, ValueError), match="viewport"):
        if stage == "helper":
            module._validated_device_result(evidence, key)
        elif stage == "resume":
            module._validated_finalized_report(report, evidence_root=_evidence_root())
        else:
            module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())


@pytest.mark.parametrize("logical_model", [None, "deepseek-backup"])
@pytest.mark.parametrize("redundant_scope", [False, True])
def test_device_review_fix_persists_proven_scope_version_and_final_counts(
    logical_model: str | None, redundant_scope: bool,
) -> None:
    module = load_script()
    report = _mode_matrix_report(module, _pending_automated_report(logical_model))
    evidence = _real_device_evidence("matrix-123", logical_model)
    if redundant_scope:
        evidence["base_url"] = "http://example.test"
        evidence["actor"] = {
            "principal": {"user_id": "00000000-0000-4000-8000-000000000002", "tenant_id": "00000000-0000-4000-8000-000000000001", "role": "operator"},
        }
    evidence["desktop_browser_interaction"]["viewport"] = {"width": 1024, "height": 1}
    evidence["mobile_browser_interaction"]["viewport"] = {"width": 600, "height": 1}
    before_report, before_evidence = copy.deepcopy(report), copy.deepcopy(evidence)
    assert report["pending_case_count"] == 20
    completed = module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
    device = completed["real_device_acceptance"]
    assert type(device.get("schema_version")) is int and device["schema_version"] == 2
    assert device["execution_identity"] == evidence["execution_identity"]
    assert device["execution_identity"] is not evidence["execution_identity"]
    assert (completed["case_count"], completed["failed_case_count"], completed["pending_case_count"]) == (
        20, 0, 0,
    )
    assert completed["exact_mode_coverage_complete"] is True
    assert module._validated_finalized_report(completed, evidence_root=_evidence_root()) == completed
    assert module.finalize_real_device_acceptance(completed, evidence, evidence_root=_evidence_root()) == completed
    assert report == before_report
    assert evidence == before_evidence


@pytest.mark.parametrize("counts", [
    {"case_count": 1, "failed_case_count": 20, "pending_case_count": 20},
    {"case_count": True, "failed_case_count": "20", "pending_case_count": None},
])
def test_device_review_fix_rebuilds_stale_counts_but_rejects_immutable_finalized_counts(
    counts: dict[str, object],
) -> None:
    module = load_script()
    report = _pending_automated_report()
    report.update(counts)
    evidence = _real_device_evidence("matrix-123")
    completed = module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
    assert (completed["case_count"], completed["failed_case_count"], completed["pending_case_count"]) == (
        20, 0, 0,
    )
    assert all(type(completed[key]) is int for key in counts)
    completed.update(counts)
    before = copy.deepcopy(completed)
    with pytest.raises(ValueError, match="contradicts"):
        module._validated_finalized_report(completed, evidence_root=_evidence_root())
    assert completed == before


@pytest.mark.parametrize("finalized", [False, True])
@pytest.mark.parametrize("mutation", [
    "missing_identity", "identity_tenant_id", "base_url", "actor", "schema_missing",
    "schema_bool", "schema_unknown", "viewport",
])
def test_device_review_fix_cli_rejection_preserves_bytes_and_attempts_without_posts(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, finalized: bool, mutation: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    report = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    if finalized:
        report = module.finalize_real_device_acceptance(report, evidence, evidence_root=_evidence_root())
        assert report["real_device_acceptance"].get("execution_identity") == (
            evidence["execution_identity"]
        )
        evidence = report["real_device_acceptance"]
    report["attempt_history"] = [{"case_id": "small:auto", "attempt": 1, "status": "failed"}]
    _corrupt_device_review_evidence(evidence, mutation)
    output, device = tmp_path / "report.json", tmp_path / "device.json"
    output.write_text(json.dumps(report), encoding="utf-8")
    device.write_text(json.dumps(evidence), encoding="utf-8")
    before_report, before_device = output.read_bytes(), device.read_bytes()
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "synthetic-token")
    monkeypatch.delenv("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID", raising=False)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    args = ["--output", str(output)]
    if finalized:
        args += ["--base-url", "http://example.test", "--evidence-root", str(_evidence_root()), "--resume-report", str(output)]
    else:
        args += ["--finalize-report", str(output), "--real-device-evidence", str(device)]
    assert module.main(args) == 1
    assert output.read_bytes() == before_report
    assert device.read_bytes() == before_device
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert delegate.requests == ([("GET", "/api/v1/auth/me")] if finalized else [])
    assert plans == []


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
        evidence_root=_evidence_root(),
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
            evidence_root=_evidence_root(),
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
            evidence_root=_evidence_root(),
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
        ("run", "final_effective_scale", "ultra"),
        ("run", "final_effective_scale", None),
        ("run", "final_effective_scale", ""),
        ("run", "final_effective_scale", True),
        (None, "final_effective_scale", "ultra"),
        (None, "initial_effective_scale", "ultra"),
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
        module.finalize_real_device_acceptance(pending, evidence, evidence_root=_evidence_root())
    assert pending == before


@pytest.mark.parametrize("mutation", (
    "missing", "run", "case", "manifest", "profile", "count", "boolean_count",
    "unknown", "extra", "nan", "top_flag", "legacy_load", "missing_isolation", "old_paths",
))
def test_finalizer_rejects_unbound_or_incomplete_ultra_load(mutation: str) -> None:
    module = load_script()
    pending = _pending_automated_report()
    case = next(row for row in pending["cases"] if row["case_id"] == "ultra:auto")
    bound = case["run"]["scale_validation"]
    if mutation == "missing":
        case["run"].pop("scale_validation")
    elif mutation == "run":
        bound["run_id"] = "other-run"
    elif mutation == "case":
        bound["case_id"] = "ultra:direct"
    elif mutation == "manifest":
        bound["manifest_sha256"] = "0" * 64
    elif mutation == "profile":
        bound["result"]["profile"] = "legacy"
    elif mutation == "count":
        bound["result"]["checks"]["load"]["measurements"]["target_projects"] = 999
    elif mutation == "boolean_count":
        bound["result"]["checks"]["load"]["measurements"]["restart_traversals"] = True
    elif mutation == "unknown":
        bound["result"].update(status="unknown", reasons=["not executed"])
    elif mutation == "extra":
        bound["trusted"] = True
    elif mutation == "nan":
        bound["result"]["checks"]["load"]["measurements"]["elapsed_seconds"] = float("inf")
    elif mutation == "legacy_load":
        bound["result"] = bound["result"]["checks"]["load"]
    elif mutation == "missing_isolation":
        del bound["result"]["checks"]["data_dir_isolation"]
    elif mutation == "old_paths":
        bound["result"]["checks"]["same_version_relocation"]["measurements"]["old_paths_unavailable"] = False
    else:
        case["scale_specific_evidence_ok"] = False
    before = copy.deepcopy(pending)
    with pytest.raises(ValueError, match="core"):
        module.finalize_real_device_acceptance(pending, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert pending == before


@pytest.mark.parametrize("mutation", ["missing", "old", "wrong_profile", "run", "manifest",
                                       "count", "isolation", "cleanup", "npm_binding"])
@pytest.mark.parametrize("finalized", [False, True])
def test_large_module_evidence_is_revalidated_at_finalize_and_finalized_resume(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    mutation: str, finalized: bool,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    if finalized:
        saved = module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    case = next(row for row in saved["cases"] if row["case_id"] == "large:auto")
    bound = case["run"]["scale_validation"]
    if mutation == "missing":
        case["run"].pop("scale_validation")
    elif mutation == "old":
        bound["result"] = {"status": "passed"}
    elif mutation == "wrong_profile":
        ultra = next(row for row in saved["cases"] if row["case_id"] == "ultra:auto")
        bound["result"] = copy.deepcopy(ultra["run"]["scale_validation"]["result"])
    elif mutation == "run":
        bound["run_id"] = "old-run"
    elif mutation == "manifest":
        bound["manifest_sha256"] = "0" * 64
    elif mutation == "count":
        bound["result"]["measurements"]["composition_markers"] = 4
    elif mutation == "isolation":
        bound["result"]["isolation_verified"] = False
    elif mutation == "cleanup":
        bound["result"]["cleanup_ok"] = False
    else:
        bound["result"]["npm_start_module_binding"] = "passed"
    before = copy.deepcopy(saved)
    output = tmp_path / "large-report.json"
    module._write_report(str(output), saved)
    original_bytes = output.read_bytes()
    with pytest.raises(ValueError):
        if finalized:
            run_matrix(module, delegate, output_path=str(output), resume_report=saved)
        else:
            module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert output.read_bytes() == original_bytes and saved == before
    assert not plans
    assert delegate.requests == ([("GET", "/api/v1/auth/me")] if finalized else [])


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
        module.finalize_real_device_acceptance(pending, evidence, evidence_root=_evidence_root())
    assert pending == before


@pytest.mark.parametrize("errors", [["setup failed"], None, "setup failed", False])
def test_finalize_rejects_top_level_errors(errors: object) -> None:
    module = load_script()
    pending = _pending_automated_report()
    pending["errors"] = errors
    before = copy.deepcopy(pending)
    with pytest.raises(ValueError, match="errors"):
        module.finalize_real_device_acceptance(pending, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert pending == before


@pytest.mark.parametrize("case_id", _FINALIZER_CASE_IDS)
def test_finalize_rejects_any_missing_canonical_case_despite_success_flags(case_id: str) -> None:
    module = load_script()
    pending = _pending_automated_report()
    evidence = _real_device_evidence("matrix-123")
    pending["cases"] = [case for case in pending["cases"] if case["case_id"] != case_id]
    del evidence["cases"][case_id]

    with pytest.raises(ValueError):
        module.finalize_real_device_acceptance(pending, evidence, evidence_root=_evidence_root())


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
        module.finalize_real_device_acceptance(pending, evidence, evidence_root=_evidence_root())
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
        module.finalize_real_device_acceptance(_pending_automated_report(), evidence, evidence_root=_evidence_root())


@pytest.mark.parametrize("field", ["project_id", "conversation_id", "run_id"])
@pytest.mark.parametrize("value", [None, "", "wrong-scope", 1])
def test_finalize_rejects_case_evidence_scope_mismatch(field: str, value: object) -> None:
    module = load_script()
    evidence = _real_device_evidence("matrix-123")
    evidence["cases"]["ultra:multi_agent"][field] = value

    with pytest.raises(ValueError, match="does not match"):
        module.finalize_real_device_acceptance(_pending_automated_report(), evidence, evidence_root=_evidence_root())


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
        module.finalize_real_device_acceptance(pending, evidence, evidence_root=_evidence_root())


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
                return {"user_id": "00000000-0000-4000-8000-000000000002", "tenant_id": "00000000-0000-4000-8000-000000000001", "role": "operator"}
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
                        "tenant_id": "00000000-0000-4000-8000-000000000001",
                        "version": 1,
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
                run_id = unquote(path.split("/")[4])
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
            validated_workspace_manifest=validated_workspace_manifest(),
            case_id=plan.requests[0].case_id,
            run_id=str(observed["id"]),
            scale_validation=scale_validation_evidence(plan.requests[0].case_id, str(observed["id"])),
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
    payload = run_matrix(module, delegate)

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
    if "evidence_root" not in options:
        options["evidence_root"] = _evidence_root()
    def execute() -> dict[str, Any]:
        return cast(
            dict[str, Any],
            module.run_real_user_four_scale_acceptance(
                module.RealUserAcceptanceClient(delegate), **options
            ),
        )

    if "output_path" in options:
        return execute()
    with TemporaryDirectory(prefix="matrix-checkpoint-") as temporary:
        options["output_path"] = str(Path(temporary) / "report.json")
        return execute()


def test_defer_preview_separates_twenty_deliveries_without_preview_requests(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], monkeypatch: Any,
) -> None:
    module, delegate, plans, _ = matrix_harness

    def forbidden_preview(*args: Any, **kwargs: Any) -> None:
        pytest.fail("deferred delivery must not start or inspect a preview")

    monkeypatch.setattr(module, "verify_dynamic_web_preview", forbidden_preview)
    payload = run_matrix(module, delegate, defer_preview=True)
    assert len(plans) == len(delegate.submissions) == 20
    assert payload["defer_preview"] is True
    assert payload["status"] == "pending_preview"
    assert payload["delivery_acceptance_ok"] is True
    assert payload["delivery_passed_case_count"] == 20
    assert payload["delivery_failed_case_count"] == 0
    assert payload["delivery_exact_mode_passed_case_count"] == 16
    assert payload["delivery_auto_scale_passed_case_count"] == 4
    assert payload["core_passed_case_count"] == payload["exact_mode_passed_case_count"] == 0
    assert payload["case_count"] == payload["pending_case_count"] == 20
    for item in [payload, *payload["cases"]]:
        assert item["status"] == "pending_preview"
        assert item["delivery_acceptance_ok"] is True
        assert item["dynamic_web_preview"]["status"] == "deferred"
        assert item["dynamic_web_preview"]["counted_as_passed"] is False
        for key in ("core_acceptance_ok", "automated_acceptance_complete",
                    "real_device_acceptance_complete", "acceptance_complete"):
            assert item[key] is False
    assert all("web-previews" not in path for _, path in delegate.requests)
    assert payload["success_policy"]["public_preview_lifecycle_required"] is True


@pytest.mark.parametrize("failure", [
    "status", "run_id", "ok", "build", "business", "public_artifacts", "bundle",
    "mode", "scale", "scale_specific", "origin", "multi_agent", "model",
])
def test_defer_preview_stops_at_first_invalid_delivery(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], monkeypatch: Any,
    tmp_path: Path, failure: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    if failure == "multi_agent":
        monkeypatch.setattr(module, "_ACCEPTANCE_CASES", tuple(
            case for case in module._ACCEPTANCE_CASES if case[2] == "multi_agent"
        ))
    elif failure == "scale_specific":
        monkeypatch.setattr(module, "_ACCEPTANCE_CASES", tuple(
            case for case in module._ACCEPTANCE_CASES if case[1] == "large"
        ))
    execute = module.execute_project_scale_plan

    def invalid_delivery(*args: Any, **kwargs: Any) -> ProjectScaleExecutionReport:
        report = cast(ProjectScaleExecutionReport, execute(*args, **kwargs))
        result = report.results[0]
        changes: dict[str, Any] = {}
        if failure == "status":
            changes["status"] = "failed"
        elif failure == "run_id":
            changes["run_id"] = None
        elif failure == "ok":
            changes["errors"] = ("delivery failed",)
        elif failure in {"build", "business"}:
            key = "generated_project_validation" if failure == "build" else "requirements_validation"
            changes["evidence"] = {**result.evidence, key: False}
        elif failure == "mode":
            changes.update(observed_mode="unknown", final_observed_mode="unknown")
        elif failure == "scale":
            changes["effective_scale"] = "ultra"
        elif failure == "scale_specific":
            changes["scale_validation"] = {}
        elif failure == "origin":
            changes["artifact_origin"] = "fixture"
        elif failure == "multi_agent":
            changes["participant_agent_ids"] = ()
        elif failure == "model":
            delegate.model_events[result.run_id] = []
        return replace(report, results=(replace(result, **changes),))

    monkeypatch.setattr(module, "execute_project_scale_plan", invalid_delivery)
    if failure in {"public_artifacts", "bundle"}:
        verify = module.verify_public_workspace_artifacts

        def invalid_artifacts(*args: Any, **kwargs: Any) -> dict[str, Any]:
            artifacts = cast(dict[str, Any], verify(*args, **kwargs))
            if failure == "public_artifacts":
                artifacts["ok"] = False
            else:
                artifacts["workspace_manifest"] = {}
            return artifacts

        monkeypatch.setattr(module, "verify_public_workspace_artifacts", invalid_artifacts)
    output = tmp_path / "delivery.json"
    payload = run_matrix(module, delegate, defer_preview=True, stop_on_failure=True,
                         output_path=str(output))
    assert len(plans) == len(delegate.submissions) == 1
    assert payload["status"] == payload["cases"][0]["status"] == "failed"
    assert payload["delivery_acceptance_ok"] is False
    assert payload["cases"][0]["delivery_acceptance_ok"] is False
    assert payload["delivery_failed_case_count"] == 1
    assert payload["delivery_passed_case_count"] == 0
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert payload["submission_journal"]["records"][0]["state"] == "confirmed"
    assert all("web-previews" not in path for _, path in delegate.requests)


def test_defer_preview_unknown_submission_keeps_intent_and_stops(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], monkeypatch: Any, tmp_path: Path,
) -> None:
    module, delegate, plans, _ = matrix_harness
    request = delegate.request_json

    def uncertain(method: str, path: str, **kwargs: Any) -> Any:
        response = request(method, path, **kwargs)
        if method == "POST" and path == "/api/v1/runs":
            raise TimeoutError("response lost")
        return response

    monkeypatch.setattr(delegate, "request_json", uncertain)
    output = tmp_path / "unknown.json"
    with pytest.raises(ValueError, match="unresolved submission"):
        run_matrix(module, delegate, defer_preview=True, output_path=str(output))
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert len(plans) == len(delegate.submissions) == 1
    assert saved["defer_preview"] is True
    assert saved["cases"] == []
    assert saved["submission_journal"]["records"][0]["state"] == "unresolved"
    assert all("web-previews" not in path for _, path in delegate.requests)


@pytest.mark.parametrize("defer_preview", [False, True])
def test_deferred_reports_cannot_resume_or_finalize_even_with_forged_complete_flags(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    defer_preview: bool,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    saved["defer_preview"] = True
    output = tmp_path / "deferred.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    original = output.read_bytes()
    with pytest.raises(ValueError, match="deferred preview"):
        run_matrix(module, delegate, defer_preview=defer_preview,
                   resume_report=saved, output_path=str(output))
    with pytest.raises(ValueError, match="deferred preview"):
        module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"))
    assert plans == delegate.requests == []
    assert output.read_bytes() == original


def test_defer_preview_refuses_any_resume_before_requests(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    module, delegate, _, _ = matrix_harness
    with pytest.raises(ValueError, match="deferred preview"):
        run_matrix(module, delegate, defer_preview=True, resume_report=_pending_automated_report())
    assert delegate.requests == []


def test_defer_preview_cli_runs_delivery_only_and_returns_pending(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], monkeypatch: Any, tmp_path: Path,
) -> None:
    module, delegate, plans, _ = matrix_harness
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "offline-test-token")
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    output = tmp_path / "cli-delivery.json"
    assert module.main(["--defer-preview", "--execution-id", "fresh-offline",
                        "--output", str(output)]) == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["delivery_acceptance_ok"] is True
    assert payload["status"] == "pending_preview"
    assert len(plans) == 20
    assert all("web-previews" not in path for _, path in delegate.requests)


@pytest.mark.parametrize("defer_preview", [False, True])
@pytest.mark.parametrize("state", ["deferred", "unresolved"])
def test_defer_preview_does_not_overwrite_existing_checkpoint_or_replay(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    defer_preview: bool, state: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    output = tmp_path / "existing.json"
    saved = {"defer_preview": state == "deferred", "status": "in_progress",
             "submission_journal": {"records": [{"state": "unresolved", "response": None}]}}
    output.write_text(json.dumps(saved), encoding="utf-8")
    original = output.read_bytes()
    with pytest.raises(ValueError, match="existing checkpoint"):
        run_matrix(module, delegate, defer_preview=defer_preview, output_path=str(output))
    assert plans == delegate.requests == []
    assert output.read_bytes() == original


@pytest.mark.parametrize("action", ["resume", "finalize"])
def test_deferred_report_cli_rejects_before_authentication_and_preserves_bytes(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, action: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    saved["defer_preview"] = True
    source = tmp_path / "deferred.json"
    device = tmp_path / "device.json"
    source.write_text(json.dumps(saved), encoding="utf-8")
    device.write_text(json.dumps(_real_device_evidence("matrix-123")), encoding="utf-8")
    original = source.read_bytes()

    def forbidden_client(**kwargs: Any) -> None:
        pytest.fail("deferred report rejection must precede authentication")

    monkeypatch.setattr(module, "UrllibAcceptanceClient", forbidden_client)
    args = ["--output", str(source)]
    if action == "resume":
        args += ["--resume-report", str(source)]
    else:
        args += ["--finalize-report", str(source), "--real-device-evidence", str(device)]
    with pytest.raises(SystemExit) as rejected:
        module.main(args)
    assert rejected.value.code == 2
    assert source.read_bytes() == original
    assert plans == delegate.requests == []


@pytest.mark.parametrize("stop_on_failure", [None, False, True])
def test_defer_preview_records_prior_success_before_later_delivery_failure(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], monkeypatch: Any,
    stop_on_failure: bool | None,
) -> None:
    module, delegate, plans, _ = matrix_harness
    execute = module.execute_project_scale_plan

    def fail_second(*args: Any, **kwargs: Any) -> ProjectScaleExecutionReport:
        report = cast(ProjectScaleExecutionReport, execute(*args, **kwargs))
        if len(plans) == 2:
            result = replace(report.results[0], errors=("business failure",))
            return replace(report, results=(result,))
        return report

    monkeypatch.setattr(module, "execute_project_scale_plan", fail_second)
    options = {} if stop_on_failure is None else {"stop_on_failure": stop_on_failure}
    payload = run_matrix(module, delegate, defer_preview=True, **options)
    assert len(plans) == len(delegate.submissions) == (2 if stop_on_failure else 20)
    assert payload["status"] == "failed"
    assert payload["delivery_acceptance_ok"] is False
    assert payload["delivery_passed_case_count"] == (1 if stop_on_failure else 19)
    assert payload["delivery_failed_case_count"] == 1
    assert payload["delivery_unsubmitted_case_count"] == (18 if stop_on_failure else 0)
    assert [case["status"] for case in payload["cases"][:2]] == ["pending_preview", "failed"]
    assert all("web-previews" not in path for _, path in delegate.requests)


@pytest.mark.parametrize("marker_path,value", [
    pytest.param(("dynamic_web_preview", "status"), "deferred", id="root-preview-deferred"),
    pytest.param(("dynamic_web_preview", "status"), "pending_preview", id="root-preview-pending"),
    pytest.param(("cases", 0, "dynamic_web_preview", "status"), "deferred",
                 id="case-preview-deferred"),
    pytest.param(("cases", 0, "dynamic_web_preview", "status"), "pending_preview",
                 id="case-preview-pending"),
    pytest.param(("cases", 0, "dynamic_web_preview", "browser_interaction"), "pending_preview",
                 id="case-browser-pending"),
    pytest.param(("cases", 0, "status"), "pending_preview", id="case-status-pending"),
    pytest.param(("attempt_history", 0, "defer_preview"), True, id="history-defer-flag"),
    pytest.param(("attempt_history", 0, "status"), "pending_preview", id="history-status-pending"),
    pytest.param(("attempt_history", 0, "dynamic_web_preview", "status"), "deferred",
                 id="history-preview-deferred"),
    pytest.param(("attempt_history", 0, "dynamic_web_preview", "status"), "pending_preview",
                 id="history-preview-pending"),
    pytest.param(("attempt_history", 0, "dynamic_web_preview", "browser_interaction"),
                 "pending_preview", id="history-browser-pending"),
])
@pytest.mark.parametrize("consumer", ["resume_api", "finalize_api", "resume_cli", "finalize_cli"])
def test_deferred_report_marker_rejects_before_auth_credentials_or_evidence(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, capsys: Any, marker_path: tuple[str | int, ...],
    value: object, consumer: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    saved.pop("defer_preview", None)
    saved["attempt_history"] = []
    if marker_path[0] == "attempt_history":
        saved["attempt_history"] = [copy.deepcopy(saved["cases"][0])]
    # Keep complete-looking flags; only one surviving marker must trigger the early guard.
    target: object = saved
    for key in marker_path[:-1]:
        if isinstance(key, int):
            assert isinstance(target, list)
            target = target[key]
        else:
            assert isinstance(target, dict)
            target = target[key]
    leaf = marker_path[-1]
    assert isinstance(target, dict)
    assert isinstance(leaf, str)
    target[leaf] = value
    source = tmp_path / "marker-checkpoint.json"
    device = tmp_path / "unused-device.json"
    source.write_text(json.dumps(saved), encoding="utf-8")
    device.write_text("{}", encoding="utf-8")
    original = source.read_bytes()

    def forbidden_auth(*args: Any, **kwargs: Any) -> None:
        pytest.fail("deferred marker must reject before auth or credential access")

    def forbidden_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("deferred marker must reject before checkpoint writes")

    read = module._read_json_mapping

    def guarded_read(path: str) -> Any:
        if path == str(device):
            pytest.fail("deferred marker must reject before reading device evidence")
        return read(path)

    monkeypatch.setattr(delegate, "request_json", forbidden_auth)
    monkeypatch.setattr(module, "_acceptance_credentials_from_env", forbidden_auth)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", forbidden_auth)
    monkeypatch.setattr(module, "_save_report", forbidden_write)
    monkeypatch.setattr(module, "_write_report", forbidden_write)
    monkeypatch.setattr(module, "_read_json_mapping", guarded_read)
    try:
        if consumer == "resume_api":
            with pytest.raises(ValueError, match="deferred preview"):
                run_matrix(module, delegate, resume_report=saved, output_path=str(source))
        elif consumer == "finalize_api":
            with pytest.raises(ValueError, match="deferred preview"):
                module.finalize_real_device_acceptance(saved, {})
        else:
            args = ["--output", str(source)]
            if consumer == "resume_cli":
                args += ["--resume-report", str(source)]
            else:
                args += ["--finalize-report", str(source), "--real-device-evidence", str(device)]
            with pytest.raises(SystemExit) as rejected:
                module.main(args)
            assert rejected.value.code == 2
            assert "deferred preview" in capsys.readouterr().err
    finally:
        assert source.read_bytes() == original
        assert plans == delegate.requests == []


@pytest.mark.parametrize("defer_preview", [False, True])
@pytest.mark.parametrize("state", ["deferred", "unresolved"])
def test_fresh_cli_rejects_existing_checkpoint_before_credentials_or_http(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, defer_preview: bool, state: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    output = tmp_path / "owned-checkpoint.json"
    output.write_text(json.dumps({
        "defer_preview": state == "deferred", "status": "in_progress",
        "submission_journal": {"records": [{"state": "unresolved", "response": None}]},
    }), encoding="utf-8")
    original = output.read_bytes()

    def forbidden_credentials() -> None:
        pytest.fail("fresh checkpoint rejection must precede credential access")

    monkeypatch.setattr(module, "_acceptance_credentials_from_env", forbidden_credentials)
    args = ["--execution-id", "fresh-offline", "--output", str(output)]
    if defer_preview:
        args.append("--defer-preview")
    with pytest.raises(SystemExit) as rejected:
        module.main(args)
    assert rejected.value.code == 2
    assert output.read_bytes() == original
    assert plans == delegate.requests == []


@pytest.mark.parametrize("stop_on_failure", [False, True])
def test_unknown_journal_cannot_be_replaced_by_default_fresh_execution(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, stop_on_failure: bool,
) -> None:
    module, delegate, plans, _ = matrix_harness
    request = delegate.request_json

    def lost_response(method: str, path: str, **kwargs: Any) -> Any:
        response = request(method, path, **kwargs)
        if method == "POST" and path == "/api/v1/runs":
            raise TimeoutError("committed response lost")
        return response

    monkeypatch.setattr(delegate, "request_json", lost_response)
    output = tmp_path / "actual-intent.json"
    with pytest.raises(ValueError, match="unresolved submission"):
        run_matrix(module, delegate, defer_preview=True, stop_on_failure=stop_on_failure,
                   output_path=str(output))
    original = output.read_bytes()
    saved = json.loads(original)
    assert saved["submission_journal"]["records"][0]["state"] == "unresolved"
    assert len(plans) == len(delegate.submissions) == 1
    delegate.requests.clear()
    with pytest.raises(ValueError, match="existing checkpoint"):
        run_matrix(module, delegate, output_path=str(output))
    assert output.read_bytes() == original
    assert delegate.requests == []
    assert len(plans) == len(delegate.submissions) == 1


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_stop_on_failure_requires_explicit_boolean_before_any_request(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], value: object,
) -> None:
    module, delegate, plans, _ = matrix_harness
    with pytest.raises(TypeError, match="^stop_on_failure must be a boolean$"):
        run_matrix(module, delegate, stop_on_failure=value)
    assert plans == delegate.requests == []


@pytest.mark.parametrize("stop_on_failure", [False, True])
def test_native_operator_stop_on_failure_cli_controls_known_delivery_failure(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path, monkeypatch: Any,
    stop_on_failure: bool,
) -> None:
    module, delegate, plans, _ = matrix_harness
    execute = module.execute_project_scale_plan

    def failed_delivery(*args: Any, **kwargs: Any) -> ProjectScaleExecutionReport:
        report = cast(ProjectScaleExecutionReport, execute(*args, **kwargs))
        return replace(report, results=(replace(report.results[0], errors=("delivery failed",)),))

    monkeypatch.setattr(module, "execute_project_scale_plan", failed_delivery)
    monkeypatch.setattr(module, "UrllibAcceptanceClient", lambda **kwargs: delegate)
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "offline-test-token")
    output = tmp_path / "operator.json"
    args = ["--defer-preview", "--execution-id", "fresh-offline", "--output", str(output)]
    if stop_on_failure:
        args.append("--stop-on-failure")
    assert module.main(args) == 1
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["delivery_acceptance_ok"] is False
    assert saved["delivery_failed_case_count"] == (1 if stop_on_failure else 20)
    assert saved["delivery_unsubmitted_case_count"] == (19 if stop_on_failure else 0)
    assert saved["submission_journal"]["records"][0]["state"] == "confirmed"
    assert len(plans) == len(delegate.submissions) == (1 if stop_on_failure else 20)
    assert all("web-previews" not in path for _, path in delegate.requests)


@pytest.mark.parametrize("stop_on_failure", [False, True])
@pytest.mark.parametrize("failure_write", [2, 3])
def test_checkpoint_write_failure_always_stops_independent_of_operator_control(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, stop_on_failure: bool, failure_write: int,
) -> None:
    module, delegate, plans, _ = matrix_harness
    replace_file = module.os.replace
    writes = 0

    def failed_checkpoint(source: object, target: object) -> None:
        nonlocal writes
        writes += 1
        if writes == failure_write:
            raise OSError("checkpoint unavailable")
        replace_file(source, target)

    monkeypatch.setattr(module.os, "replace", failed_checkpoint)
    output = tmp_path / "checkpoint.json"
    with pytest.raises(ValueError, match="unresolved submission"):
        run_matrix(module, delegate, defer_preview=True, stop_on_failure=stop_on_failure,
                   output_path=str(output))
    assert len(plans) == 1
    assert len(delegate.submissions) == failure_write - 2
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["cases"] == []
    records = saved["submission_journal"]["records"]
    assert records == [] if failure_write == 2 else records[0]["state"] == "unresolved"
    assert all("web-previews" not in path for _, path in delegate.requests)


@pytest.mark.parametrize("failure", ["timeout", "json", "utf8", "disconnect", "list", "id", "scope"])
def test_unknown_submissions_stop_matrix_and_restart_without_post_or_checkpoint_changes(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, failure: str,
) -> None:
    module, delegate, plans, _ = matrix_harness
    request = delegate.request_json
    output = tmp_path / "journal.json"

    def uncertain(method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and path == "/api/v1/runs":
            intent = json.loads(output.read_text(encoding="utf-8"))
            assert intent["submission_journal"]["records"][0]["state"] == "unresolved"
            assert intent["cases"] == []
            response = request(method, path, **kwargs)
            if failure == "timeout":
                raise TimeoutError("response lost")
            if failure == "json":
                raise json.JSONDecodeError("invalid response", "{", 1)
            if failure == "utf8":
                raise UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid")
            if failure == "disconnect":
                raise ConnectionResetError("disconnected")
            if failure == "list":
                return []
            if failure == "id":
                response.pop("id")
            if failure == "scope":
                response["workspace_session_id"] = "other-workspace"
            return response
        return request(method, path, **kwargs)

    monkeypatch.setattr(delegate, "request_json", uncertain)
    with pytest.raises(ValueError, match="unresolved submission"):
        run_matrix(module, delegate, output_path=str(output))
    assert len(plans) == len(delegate.submissions) == 1
    saved_bytes = output.read_bytes()
    saved = json.loads(saved_bytes)
    assert saved["cases"] == saved["attempt_history"] == []
    record = saved["submission_journal"]["records"][0]
    assert record["state"] == "unresolved" and record["response"] is None
    assert record["context"]["attempt"] == 1
    assert "message" not in record and "body" not in record
    delegate.requests.clear()
    with pytest.raises(ValueError, match="unresolved submission"):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert len(delegate.submissions) == 1
    assert output.read_bytes() == saved_bytes


@pytest.mark.parametrize("failure_write", [2, 3])
def test_submission_persistence_failures_do_not_advance_paid_matrix(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, failure_write: int,
) -> None:
    module, delegate, _, _ = matrix_harness
    output = tmp_path / "journal.json"
    replace_file = module.os.replace
    writes = 0

    def fail_replace(source: object, target: object) -> None:
        nonlocal writes
        writes += 1
        if writes == failure_write:
            raise OSError("checkpoint unavailable")
        replace_file(source, target)

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(ValueError, match="unresolved submission"):
        run_matrix(module, delegate, output_path=str(output))
    assert len(delegate.submissions) == failure_write - 2
    saved_bytes = output.read_bytes()
    saved = json.loads(saved_bytes)
    assert saved["cases"] == []
    records = saved["submission_journal"]["records"]
    if failure_write == 2:
        assert records == []
    else:
        assert records[0]["state"] == "unresolved"
        with pytest.raises(ValueError, match="unresolved submission"):
            run_matrix(module, delegate, output_path=str(output), resume_report=saved)
        assert len(delegate.submissions) == 1
        assert output.read_bytes() == saved_bytes
    assert list(tmp_path.iterdir()) == [output]


def test_paid_matrix_requires_durable_output_before_any_request(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    module, delegate, _, _ = matrix_harness
    with pytest.raises(ValueError, match="durable output_path"):
        run_matrix(module, delegate, output_path=None)
    assert delegate.requests == []


def test_legacy_report_without_journal_cannot_resume_or_finalize(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
) -> None:
    module, delegate, _, _ = matrix_harness
    saved = _pending_automated_report()
    saved.pop("submission_journal")
    output = tmp_path / "legacy.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    original = output.read_bytes()
    with pytest.raises(ValueError, match="journal is missing"):
        run_matrix(module, delegate, resume_report=saved, output_path=str(output))
    with pytest.raises(ValueError, match="journal is missing"):
        module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert output.read_bytes() == original


def test_submission_digest_conflict_stops_other_cases_instead_of_classifying_retryable_failure(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
) -> None:
    module, delegate, _, _ = matrix_harness
    saved = run_matrix(module, delegate)
    saved["cases"] = []
    saved["submission_journal"]["records"] = saved["submission_journal"]["records"][:1]
    saved["submission_journal"]["records"][0]["request_sha256"] = "0" * 64
    original_posts = len(delegate.submissions)
    with pytest.raises(ValueError, match="submission"):
        run_matrix(module, delegate, resume_report=saved, output_path=str(tmp_path / "report.json"))
    assert len(delegate.submissions) == original_posts


def test_finalize_cli_does_not_overwrite_unresolved_source_report(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
) -> None:
    module, _, _, _ = matrix_harness
    saved = _pending_automated_report()
    saved["submission_journal"]["records"][0].update(state="unresolved", response=None)
    output, device = tmp_path / "report.json", tmp_path / "device.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    device.write_text(json.dumps(_real_device_evidence("matrix-123")), encoding="utf-8")
    original = output.read_bytes()
    assert module.main([
        "--finalize-report", str(output), "--real-device-evidence", str(device),
        "--output", str(output),
    ]) == 1
    assert output.read_bytes() == original


def test_completed_report_still_rejects_original_request_digest_mismatch(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    saved["submission_journal"]["records"][0]["request_sha256"] = "0" * 64
    output = tmp_path / "report.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    original = output.read_bytes()
    with pytest.raises(ValueError, match="digest"):
        run_matrix(module, delegate, resume_report=saved, output_path=str(output))
    with pytest.raises(ValueError, match="digest"):
        module.finalize_real_device_acceptance(saved, _real_device_evidence("matrix-123"), evidence_root=_evidence_root())
    assert plans == []
    assert delegate.requests == [("GET", "/api/v1/auth/me")]
    assert output.read_bytes() == original


@pytest.mark.parametrize("status", ["running", "queued", "waiting_approval"])
def test_confirmed_nonterminal_run_is_not_replaced_by_new_attempt_on_resume(
    matrix_harness: tuple[Any, Any, list[Any], list[str]], tmp_path: Path,
    monkeypatch: Any, status: str,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    saved = run_matrix(module, delegate)
    saved["cases"][0]["run"]["errors"] = ["poll lost connection"]
    next(iter(delegate.runs.values()))["status"] = status
    output = tmp_path / "journal.json"
    output.write_text(json.dumps(saved), encoding="utf-8")
    original = output.read_bytes()
    delegate.requests.clear()
    with pytest.raises(ValueError, match="nonterminal"):
        run_matrix(module, delegate, resume_report=saved, output_path=str(output))
    assert len(delegate.submissions) == 1
    assert all(method == "GET" for method, _ in delegate.requests)
    assert output.read_bytes() == original


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
            saved, _real_device_evidence("matrix-123", saved_model),
            evidence_root=_evidence_root(),
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
            saved, _real_device_evidence("matrix-123", "deepseek-backup"),
            evidence_root=_evidence_root(),
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
        module.finalize_real_device_acceptance(saved, evidence, evidence_root=_evidence_root())


def test_scoped_finalizer_preserves_profile_and_does_not_invent_model_evidence() -> None:
    module = load_script()
    saved = _pending_automated_report("deepseek-backup")
    evidence = _real_device_evidence("matrix-123", "deepseek-backup")
    before = copy.deepcopy(saved)
    completed = module.finalize_real_device_acceptance(saved, evidence, evidence_root=_evidence_root())
    assert completed["model_profile"] == saved["model_profile"]
    assert completed["real_device_acceptance"]["model_profile"] == saved["model_profile"]
    assert saved == before
    assert [case["run"] for case in completed["cases"]] == [case["run"] for case in saved["cases"]]


def test_unscoped_resume_keeps_default_requests_and_report_identity(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
) -> None:
    module, delegate, plans, _ = matrix_harness
    saved = _pending_automated_report()
    saved["cases"] = []
    saved["submission_journal"]["records"] = []
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
        args += ["--evidence-root", str(_evidence_root()), "--resume-report", str(report)]
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


@pytest.fixture
async def direct_public_model_events() -> list[dict[str, Any]]:
    from uuid import uuid4

    from agent_hub.config.service import ConfigService
    from agent_hub.domain.runs import TaskMode
    from agent_hub.runs.repository import _public_event_payload
    from agent_hub.runtime.contracts import TaskContext
    from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
    from agent_hub.security.secrets import SecretService
    from tests.unit.runtime.test_configured_runtime import (
        TENANT_ID,
        FakeConfigService,
        FakeSecretService,
        FakeTransport,
        _immediate_capacity,
    )
    from tests.unit.runtime.test_run_model_scope import _document

    runtime = ConfigBackedDirectRuntime(
        config_service=cast(ConfigService, FakeConfigService(_document())),
        secret_service=cast(SecretService, FakeSecretService()),
        capacity_factory=_immediate_capacity,
        transport=FakeTransport(),
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DIRECT,
        request="Answer briefly in plain text.",
        routing_decision={"direct_model": "deepseek", "allowed_models": ("deepseek",)},
    )
    return [_public_event_payload(event.to_payload()) async for event in runtime.run(context)]


@pytest.fixture
async def crew_public_model_events() -> list[dict[str, Any]]:
    from agent_hub.models.gateway import GatewayCompletion
    from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
    from agent_hub.runs.repository import _public_event_payload
    from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        FastFactory,
        _context,
        _one_step_plan,
    )

    class Gateway:
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            return GatewayCompletion(
                response=ModelResponse(text="actual model answer", usage=TokenUsage(10, 2, 12)),
                deployment_id="deepseek-deployment",
                logical_model=request.logical_model,
                provider_id="deepseek",
                provider_model="deepseek/deepseek-chat",
                attempted_logical_models=(request.logical_model,),
            )

    plan = _one_step_plan()
    plan = plan.model_copy(update={
        "agents": (plan.agents[0].model_copy(update={"logical_model": "deepseek"}),),
    })
    runtime = CrewDispatchRuntime(Gateway(), plan, crew_factory=FastFactory())
    return [_public_event_payload(event.to_payload()) async for event in runtime.run(_context())]


class PublicModelEvidenceClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.run_id = events[0]["run_id"]
        self.requests: list[tuple[str, str]] = []

    def request_json(
        self, method: str, path: str, *, body: object = None, idempotency_key: object = None,
    ) -> dict[str, Any]:
        assert method == "GET" and body is None and idempotency_key is None
        self.requests.append((method, path))
        root = f"/api/v1/runs/{self.run_id}"
        if path == root:
            return {"id": self.run_id, "status": "completed"}
        assert path == f"{root}/events"
        return {"items": copy.deepcopy(self.events)}


def collect_direct_model_evidence(module: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    client = PublicModelEvidenceClient(events)
    evidence = module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(client),
        logical_model="deepseek",
        submitted_run_ids=[client.run_id],
        accepted_repair_run_ids=[],
        result_run_id=client.run_id,
    )
    assert client.requests == [
        ("GET", f"/api/v1/runs/{client.run_id}"),
        ("GET", f"/api/v1/runs/{client.run_id}/events"),
    ]
    return cast(dict[str, Any], evidence)


@pytest.mark.parametrize("failed_status", ["failed", "cancelled", "running"])
@pytest.mark.parametrize("failed_identity", ["repair", "original", "result"])
@pytest.mark.parametrize("scope_variant", [
    "valid", "hash", "unknown_field", "history", "foreign_model", "count",
    "wrong_run", "duplicate_call", "unknown_call", "missing_start", "missing_receipt",
    "wrong_actor", "wrong_link", "out_of_order", "payload_history", "redacted", "extra_receipt",
])
async def test_recovered_failure_scope_is_not_a_model_completion(
    direct_public_model_events: list[dict[str, Any]], failed_status: str,
    failed_identity: str, scope_variant: str,
) -> None:
    from uuid import uuid4

    from agent_hub.runtime.contracts import Artifact, GatewayProvenance

    module = load_script()
    original_id, result_id, repair_id = (str(uuid4()) for _ in range(3))
    provenance = GatewayProvenance(
        logical_model="deepseek", deployment_id="deepseek-deployment",
        provider_id="deepseek", provider_model="deepseek/deepseek-chat",
    )
    receipt: dict[str, Any] = {
        "schema_version": 1, "source": "model_gateway", "call_id": str(uuid4()),
        "requested_logical_model": "deepseek", "allow_fallback": False,
        "disposition": "failed", "history_complete": True,
        "attempted_logical_models": ["deepseek"],
        "attempts": [{"ordinal": 1, "provenance": provenance.to_payload(),
                      "transport_state": "entered", "outcome": "empty_response",
                      "status_code": 200, "usage_status": "known",
                      "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}],
    }
    failed_id = {"repair": repair_id, "original": original_id, "result": result_id}[failed_identity]
    artifact = Artifact(
        id=uuid4(), type="model_attempt", producer="main_agent", provenance=provenance,
        content={"schema_version": 1, "source": "direct_runtime", "run_id": failed_id,
                 "tenant_id": str(uuid4()), "requested_logical_model": "deepseek",
                 "scope_id": str(uuid4()), "part_index": 1, "part_count": 1, "call_offset": 0,
                 "history_complete": True, "call_count": 1,
                 "calls": ({"outcome": "failed", "receipt": receipt},)},
    )
    linkage = {"artifact_id": str(artifact.id), "logical_model": "deepseek",
               "requested_logical_model": "deepseek", "attempted_logical_models": ["deepseek"],
               "deployment": provenance.deployment_id, "provider": provenance.provider_id,
               "upstream_model": provenance.provider_model}
    failure_events: list[dict[str, Any]] = [
        {"run_id": failed_id, "kind": "model.started", "sequence": 1,
         "actor": "main_agent", "payload": {"logical_model": "deepseek"}},
        {"run_id": failed_id, "kind": "artifact.created", "sequence": 2,
         "actor": "main_agent", "payload": linkage, "artifact": artifact.to_payload()},
        {"run_id": failed_id, "kind": "model.failure_receipt", "sequence": 3,
         "actor": None, "payload": {**linkage, "actor": "main_agent"}},
    ]
    envelope = artifact.to_payload()
    if scope_variant in {"unknown_field", "history", "foreign_model", "count", "wrong_run",
                         "duplicate_call", "unknown_call"}:
        content = cast(dict[str, Any], envelope["content"])
        if scope_variant == "unknown_field":
            content["calls"][0]["receipt"]["response"] = "private body"
        elif scope_variant == "history":
            content["calls"][0]["receipt"]["history_complete"] = False
        elif scope_variant == "foreign_model":
            content["calls"][0]["receipt"]["attempted_logical_models"] = ["foreign", "deepseek"]
        elif scope_variant == "count":
            content["call_count"] = 2
        elif scope_variant == "wrong_run":
            content["run_id"] = str(uuid4())
        elif scope_variant == "duplicate_call":
            content["calls"].append(copy.deepcopy(content["calls"][0]))
            content["call_count"] = 2
        else:
            content["calls"][0]["outcome"] = "unknown"
        envelope.pop("content_sha256")
        failure_events[1]["artifact"] = Artifact.from_payload(envelope).to_payload()
    elif scope_variant == "hash":
        failure_events[1]["artifact"]["content_sha256"] = "0" * 64
    elif scope_variant == "redacted":
        failure_events[1]["artifact"].update(
            content_sha256="0" * 64, public_content_sha256=artifact.content_sha256,
            content_redacted=True,
        )
    elif scope_variant == "missing_start":
        failure_events.pop(0)
    elif scope_variant == "missing_receipt":
        failure_events.pop()
    elif scope_variant == "wrong_actor":
        failure_events[-1]["payload"]["actor"] = "other_agent"
    elif scope_variant == "wrong_link":
        failure_events[-1]["payload"] = {**failure_events[-1]["payload"], "artifact_id": str(uuid4())}
    elif scope_variant == "out_of_order":
        failure_events[-1]["sequence"] = 1
    elif scope_variant == "payload_history":
        failure_events[-1]["payload"] = {**failure_events[-1]["payload"],
                                       "attempted_logical_models": ["foreign", "deepseek"]}
    elif scope_variant == "extra_receipt":
        extra = copy.deepcopy(failure_events[-1])
        extra["sequence"] = 4
        extra["payload"]["upstream_model"] = "deepseek/other"
        failure_events.append(extra)
    by_run: dict[str, Any] = {}
    for run_id in (original_id, result_id, repair_id):
        events = copy.deepcopy(direct_public_model_events)
        for event in events:
            event["run_id"] = run_id
        by_run[run_id] = {"status": "completed", "events": events}
    by_run[failed_id] = {"status": failed_status, "events": failure_events}

    class Client:
        def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            assert method == "GET" and all(value is None for value in kwargs.values())
            run_id = path.split("/")[4]
            if path.endswith("/events"):
                return {"items": copy.deepcopy(by_run[run_id]["events"])}
            return {"id": run_id, "status": by_run[run_id]["status"]}

    evidence = module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(Client()), logical_model="deepseek",
        submitted_run_ids=[original_id, repair_id], accepted_repair_run_ids=[],
        result_run_id=result_id,
    )
    permitted = (failed_status == "failed" and failed_identity == "repair"
                 and scope_variant == "valid")
    assert evidence["ok"] is permitted, evidence["errors"]
    assert module._has_direct_model_completion(failure_events) is False
    assert "private body" not in json.dumps(evidence)
    if permitted:
        retained = evidence["runs"][1]["model_events"]
        assert module._has_failed_attempt_scope(retained)
        assert not module._has_direct_model_completion(retained)
        retained[1]["model_artifact"]["scope_content"]["call_count"] = 2
        assert not module._has_failed_attempt_scope(retained)
        assert module._model_scope_errors(evidence, logical_model="deepseek", result_run_id=result_id)


async def test_cancelled_repair_with_partial_model_completion_is_rejected(
    direct_public_model_events: list[dict[str, Any]],
) -> None:
    module = load_script()
    evidence = collect_direct_model_evidence(module, direct_public_model_events)
    assert evidence["ok"]
    run = copy.deepcopy(evidence["runs"][0])
    original = run["run_id"]
    from uuid import uuid4

    run["run_id"] = str(uuid4())
    run["status"] = "cancelled"
    run["events_endpoint"] = f"/api/v1/runs/{run['run_id']}/events"
    for event in run["model_events"]:
        event["run_id"] = run["run_id"]
    evidence["runs"].append(run)
    evidence["accepted_repair_run_ids"] = [run["run_id"]]
    assert module._model_scope_errors(evidence, logical_model="deepseek", result_run_id=original)


async def test_scoped_collector_accepts_real_configured_direct_public_completion_metadata(
    direct_public_model_events: list[dict[str, Any]],
) -> None:
    module = load_script()
    assert [event["kind"] for event in direct_public_model_events] == [
        "model.started", "artifact.created", "checkpoint.saved", "runtime.completed",
    ]
    evidence = collect_direct_model_evidence(module, direct_public_model_events)
    assert evidence["ok"] is True, evidence["errors"]
    retained = evidence["runs"][0]["model_events"]
    assert [event["kind"] for event in retained] == [
        "model.started", "artifact.created", "runtime.completed",
    ]
    assert retained[1]["payload"]["deployment"]
    assert retained[1]["payload"]["upstream_model"] == "deepseek/deepseek-chat"
    assert retained[1]["payload"]["attempted_logical_models"] == ["deepseek"]
    assert all(
        set(event) <= {"kind", "run_id", "sequence", "actor", "payload", "model_artifact"}
        for event in retained
    )
    assert all(
        set(event["payload"]) <= {
            "logical_model", "requested_logical_model", "attempted_logical_models",
            "artifact_id", "deployment", "provider", "upstream_model", "artifact_origin",
        }
        for event in retained
    )


async def test_scoped_collector_accepts_real_crew_typed_completion_artifact(
    crew_public_model_events: list[dict[str, Any]],
) -> None:
    module = load_script()
    assert all(event["kind"] != "model.completed" for event in crew_public_model_events)
    artifact_event = next(
        event for event in crew_public_model_events
        if event["kind"] == "artifact.created" and event["artifact"]["type"] == "model_response"
    )
    assert artifact_event["payload"] == {"agent_id": "writer"}
    evidence = collect_direct_model_evidence(module, crew_public_model_events)
    assert evidence["ok"] is True, evidence["errors"]
    proof = next(event for event in evidence["runs"][0]["model_events"] if "model_artifact" in event)
    assert proof["kind"] == "artifact.created"
    assert proof["payload"]["logical_model"] == "deepseek"
    assert proof["payload"]["attempted_logical_models"] == ["deepseek"]
    assert proof["model_artifact"]["content_sha256"] == artifact_event["artifact"]["content_sha256"]
    assert proof["model_artifact"]["hash_verified"] is True
    assert proof["model_artifact"]["type"] == "model_response"
    assert "actual model answer" not in json.dumps(evidence)
    assert "content" not in proof["model_artifact"]


@pytest.mark.parametrize("endpoint", ["run", "events"])
def test_scope_collector_never_echoes_http_or_client_exception_body(endpoint: str) -> None:
    module = load_script()
    secret = "Authorization: Bearer private-token password=private-password"

    class FailingClient:
        def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
            if endpoint == "run" or path.endswith("/events"):
                raise RuntimeError(secret)
            return {"id": "run-1", "status": "completed"}

    evidence = module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(FailingClient()), logical_model="deepseek",
        submitted_run_ids=["run-1"], accepted_repair_run_ids=[], result_run_id="run-1",
    )
    assert evidence["ok"] is False
    assert "run-1: public_model_scope_read_failed (RuntimeError)" in evidence["errors"]
    assert "private-token" not in json.dumps(evidence)
    assert "private-password" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "invalid", ["foreign_actual", "foreign_attempt", "empty_attempts", "missing_attempts",
                "hash_mismatch", "missing_provenance", "empty_deployment", "empty_upstream",
                "wrong_producer", "step_only", "text_only", "missing_hash"],
)
async def test_crew_completion_rejects_requested_role_model_and_invalid_actual_artifact(
    crew_public_model_events: list[dict[str, Any]], invalid: str,
) -> None:
    from agent_hub.runtime.contracts import Artifact

    module = load_script()
    events = copy.deepcopy(crew_public_model_events)
    artifact_event = next(
        event for event in events
        if event["kind"] == "artifact.created" and event["artifact"]["type"] == "model_response"
    )
    artifact = artifact_event["artifact"]
    if invalid in {"step_only", "text_only"}:
        events = [event for event in events if event is not artifact_event]
        if invalid == "step_only":
            events = [event for event in events if event["kind"] == "step.completed"]
    elif invalid == "hash_mismatch":
        artifact["content_sha256"] = "0" * 64
    elif invalid == "missing_hash":
        artifact.pop("content_sha256")
    else:
        if invalid == "foreign_actual":
            artifact["provenance"]["logical_model"] = "other-model"
        elif invalid == "foreign_attempt":
            artifact["content"]["attempted_logical_models"] = ["other-model", "deepseek"]
        elif invalid == "empty_attempts":
            artifact["content"]["attempted_logical_models"] = []
        elif invalid == "missing_attempts":
            artifact["content"].pop("attempted_logical_models")
        elif invalid == "missing_provenance":
            artifact["provenance"] = None
        elif invalid == "empty_deployment":
            artifact["provenance"]["deployment_id"] = ""
        elif invalid == "empty_upstream":
            artifact["provenance"]["provider_model"] = ""
        elif invalid == "wrong_producer":
            artifact["producer"] = "another-agent"
        if invalid not in {"empty_deployment", "empty_upstream"}:
            artifact["content_sha256"] = ""
            artifact_event["artifact"] = Artifact.from_payload({
                key: value for key, value in artifact.items()
                if key not in {"public_content_sha256", "content_redacted"}
            }).to_payload()
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert evidence["errors"]
    assert "actual model answer" not in json.dumps(evidence)


def projected_model_events(events: list[dict[str, Any]], redacted: bool) -> list[dict[str, Any]]:
    from agent_hub.runtime.contracts import Artifact

    result = copy.deepcopy(events)
    for event in result:
        artifact = event.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("provenance") is None:
            continue
        original_digest = artifact["content_sha256"]
        artifact.pop("public_content_sha256", None)
        artifact.pop("content_redacted", None)
        if redacted:
            artifact["content"]["text"] = "[redacted]"
        artifact["content_sha256"] = ""
        public_digest = Artifact.from_payload(artifact).content_sha256
        artifact.update(
            content_sha256=original_digest,
            public_content_sha256=public_digest,
            content_redacted=redacted,
        )
    return result


@pytest.mark.parametrize("runtime", ["direct", "crew"])
@pytest.mark.parametrize("redacted", [False, True])
async def test_scoped_collector_validates_public_projection_envelope_without_retaining_content(
    direct_public_model_events: list[dict[str, Any]],
    crew_public_model_events: list[dict[str, Any]], runtime: str, redacted: bool,
) -> None:
    module = load_script()
    original = direct_public_model_events if runtime == "direct" else crew_public_model_events
    events = projected_model_events(original, redacted)
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is True, evidence["errors"]
    proofs = [event["model_artifact"] for event in evidence["runs"][0]["model_events"]
              if "model_artifact" in event]
    assert proofs
    for proof in proofs:
        artifact = next(event["artifact"] for event in events
                        if isinstance(event.get("artifact"), dict)
                        and event["artifact"]["id"] == proof["id"])
        assert proof["content_sha256"] == artifact["content_sha256"]
        assert proof["public_content_sha256"] == artifact["public_content_sha256"]
        assert proof["content_redacted"] is redacted
        assert proof["hash_verification_scope"] == "public_projection"
        assert (proof["content_sha256"] != proof["public_content_sha256"]) is redacted
        assert "content" not in proof
    assert "actual model answer" not in json.dumps(evidence)
    assert "[redacted]" not in json.dumps(evidence)


@pytest.mark.parametrize("runtime", ["direct", "crew"])
async def test_scoped_collector_keeps_legacy_original_hash_validation(
    direct_public_model_events: list[dict[str, Any]],
    crew_public_model_events: list[dict[str, Any]], runtime: str,
) -> None:
    module = load_script()
    original = direct_public_model_events if runtime == "direct" else crew_public_model_events
    events = copy.deepcopy(original)
    for event in events:
        artifact = event.get("artifact")
        if isinstance(artifact, dict):
            artifact.pop("public_content_sha256", None)
            artifact.pop("content_redacted", None)
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is True, evidence["errors"]
    assert all("public_content_sha256" not in event.get("model_artifact", {})
               for event in evidence["runs"][0]["model_events"])
    assert all(event["model_artifact"]["hash_verification_scope"] == "original"
               for event in evidence["runs"][0]["model_events"] if "model_artifact" in event)


@pytest.mark.parametrize(
    "invalid", ["missing_public_hash", "missing_flag", "bad_flag", "integer_flag",
                "bad_public_hash", "bad_original_hash", "equal_redacted_hashes",
                "different_unredacted_hashes", "tampered_content", "tampered_provenance"],
)
async def test_scoped_collector_rejects_incomplete_or_contradictory_projection_metadata(
    crew_public_model_events: list[dict[str, Any]], invalid: str,
) -> None:
    module = load_script()
    events = projected_model_events(crew_public_model_events, True)
    artifact = next(event["artifact"] for event in events
                    if isinstance(event.get("artifact"), dict)
                    and event["artifact"]["type"] == "model_response")
    if invalid == "missing_public_hash":
        artifact.pop("public_content_sha256")
    elif invalid == "missing_flag":
        artifact.pop("content_redacted")
    elif invalid == "bad_flag":
        artifact["content_redacted"] = "true"
    elif invalid == "integer_flag":
        artifact["content_redacted"] = 1
    elif invalid == "bad_public_hash":
        artifact["public_content_sha256"] = "not-a-hash"
    elif invalid == "bad_original_hash":
        artifact["content_sha256"] = "not-a-hash"
    elif invalid == "equal_redacted_hashes":
        artifact["content_sha256"] = artifact["public_content_sha256"]
    elif invalid == "different_unredacted_hashes":
        artifact["content_redacted"] = False
    elif invalid == "tampered_content":
        artifact["content"]["text"] = "private output must not appear in errors"
    elif invalid == "tampered_provenance":
        artifact["provenance"]["logical_model"] = "other-model"
    events.append(_model_event(events[0]["run_id"], "deepseek"))
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert any("public_model_scope_read_failed" in error for error in evidence["errors"])
    assert "private output" not in json.dumps(evidence)


@pytest.mark.parametrize("projection_metadata", [False, True])
async def test_actual_public_security_redaction_requires_verified_projection_hash(
    crew_public_model_events: list[dict[str, Any]], projection_metadata: bool,
) -> None:
    from agent_hub.runs.repository import _public_event_payload
    from agent_hub.runtime.contracts import Artifact

    module = load_script()
    events = copy.deepcopy(crew_public_model_events)
    event = next(event for event in events if isinstance(event.get("artifact"), dict)
                 and event["artifact"]["type"] == "model_response")
    raw: dict[str, Any] = {key: value for key, value in event["artifact"].items()
           if key not in {"public_content_sha256", "content_redacted"}}
    raw["content"]["text"] = "Security review: password policy is covered."
    raw["content_sha256"] = ""
    event["artifact"] = Artifact.from_payload(raw).to_payload()
    public = _public_event_payload(event)
    projected = cast(dict[str, Any], public["artifact"])
    assert projected["content"]["text"] == "[redacted]"
    if projection_metadata:
        assert projected["content_redacted"] is True
        assert projected["public_content_sha256"] != projected["content_sha256"]
    else:
        projected.pop("public_content_sha256", None)
        projected.pop("content_redacted", None)
    events[events.index(event)] = public
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is projection_metadata, evidence["errors"]
    assert "password policy" not in json.dumps(evidence)
    assert "[redacted]" not in json.dumps(evidence)


@pytest.mark.parametrize("invalid_index", [0, 1, 2])
@pytest.mark.parametrize("invalid", [None, "foreign_attempt", "hash_mismatch"])
async def test_scoped_collector_checks_every_typed_completion_not_just_final_model(
    crew_public_model_events: list[dict[str, Any]], invalid_index: int, invalid: str | None,
) -> None:
    from uuid import uuid4

    from agent_hub.runtime.contracts import Artifact

    module = load_script()
    original = next(
        event for event in crew_public_model_events
        if event["kind"] == "artifact.created" and event["artifact"]["type"] == "model_response"
    )
    completions = []
    for index in range(3):
        event = copy.deepcopy(original)
        event["sequence"] = index + 1
        event["artifact"]["id"] = str(uuid4())
        if index == invalid_index and invalid == "foreign_attempt":
            event["artifact"]["content"]["attempted_logical_models"] = ["other-model", "deepseek"]
            event["artifact"]["content_sha256"] = ""
            event["artifact"] = Artifact.from_payload({
                key: value for key, value in event["artifact"].items()
                if key not in {"public_content_sha256", "content_redacted"}
            }).to_payload()
        elif index == invalid_index and invalid == "hash_mismatch":
            event["artifact"]["content_sha256"] = "0" * 64
        completions.append(event)
    # An otherwise valid completion must not hide any mismatching model artifact.
    events = [*completions, {
        "kind": "model.completed", "run_id": original["run_id"],
        "payload": {"logical_model": "deepseek", "attempted_logical_models": ["deepseek"]},
    }]
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is (invalid is None)
    assert bool(evidence["errors"]) is (invalid is not None)
    if invalid is None:
        retained = evidence["runs"][0]["model_events"]
        assert len([event for event in retained if "model_artifact" in event]) == 3
    if invalid == "hash_mismatch":
        assert any("public_model_scope_read_failed" in error for error in evidence["errors"])
    assert "actual model answer" not in json.dumps(evidence)


@pytest.mark.parametrize("tamper", [None, "hash_flag", "provenance", "empty_attempts", "actor", "builtin_fixture",
                                   "half_projection", "contradictory_projection"])
async def test_crew_completion_proof_is_revalidated_on_resume_and_finalization(
    crew_public_model_events: list[dict[str, Any]], tamper: str | None,
) -> None:
    module = load_script()
    evidence = collect_direct_model_evidence(module, crew_public_model_events)
    assert evidence["ok"] is True
    proof = next(event for event in evidence["runs"][0]["model_events"] if "model_artifact" in event)
    if tamper == "hash_flag":
        proof["model_artifact"]["hash_verified"] = 1
    elif tamper == "provenance":
        proof["model_artifact"]["provenance"]["logical_model"] = "other-model"
    elif tamper == "empty_attempts":
        proof["payload"]["attempted_logical_models"] = []
    elif tamper == "actor":
        proof["actor"] = "another-agent"
    elif tamper == "builtin_fixture":
        proof["payload"]["artifact_origin"] = "builtin_fixture"
    elif tamper == "half_projection":
        proof["model_artifact"]["public_content_sha256"] = proof["model_artifact"]["content_sha256"]
        proof["model_artifact"].pop("content_redacted", None)
    elif tamper == "contradictory_projection":
        proof["model_artifact"]["public_content_sha256"] = proof["model_artifact"]["content_sha256"]
        proof["model_artifact"]["content_redacted"] = True
    report = _pending_automated_report("deepseek")
    case = report["cases"][0]
    run_id = case["run"]["run_id"]
    evidence.update(original_run_id=run_id, result_run_id=run_id)
    run = evidence["runs"][0]
    run.update(run_id=run_id, events_endpoint=f"/api/v1/runs/{quote(run_id, safe='')}/events")
    for event in run["model_events"]:
        event["run_id"] = run_id
    case["model_scope_evidence"] = evidence
    assert module._has_complete_core_evidence(
        case, safe_execution_id="matrix-123", case_key="auto-small"
    ) is (tamper is None)
    if tamper is None:
        finalized = module.finalize_real_device_acceptance(
            report, _real_device_evidence("matrix-123", "deepseek"),
            evidence_root=_evidence_root(),
        )
        assert finalized["acceptance_complete"] is True
    else:
        with pytest.raises(ValueError):
            module.finalize_real_device_acceptance(
                report, _real_device_evidence("matrix-123", "deepseek"),
                evidence_root=_evidence_root(),
            )


@pytest.mark.parametrize("attempts", [["other-model", "deepseek"], [], ["other-model"]])
async def test_crew_event_attempts_cannot_be_overwritten_by_artifact_history(
    crew_public_model_events: list[dict[str, Any]], attempts: list[str],
) -> None:
    module = load_script()
    events = copy.deepcopy(crew_public_model_events)
    event = next(event for event in events if isinstance(event.get("artifact"), dict)
                 and event["artifact"]["type"] == "model_response")
    event["payload"]["attempted_logical_models"] = attempts
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert evidence["errors"]


@pytest.mark.parametrize("envelope", [None, "not-an-envelope", {}, "missing"])
async def test_extra_malformed_model_envelope_cannot_hide_behind_valid_completion(
    direct_public_model_events: list[dict[str, Any]], envelope: object,
) -> None:
    module = load_script()
    events = copy.deepcopy(direct_public_model_events)
    malformed = copy.deepcopy(events[1])
    malformed["sequence"] = events[-1]["sequence"] + 1
    if envelope == "missing":
        malformed.pop("artifact")
    else:
        malformed["artifact"] = envelope
    events.append(malformed)
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert evidence["errors"]


async def test_crew_fixture_model_response_is_not_real_completion(
    crew_public_model_events: list[dict[str, Any]],
) -> None:
    module = load_script()
    events = copy.deepcopy(crew_public_model_events)
    event = next(event for event in events if isinstance(event.get("artifact"), dict)
                 and event["artifact"]["type"] == "model_response")
    event["payload"]["artifact_origin"] = "builtin_fixture"
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert evidence["errors"]


@pytest.mark.parametrize(
    "invalid",
    [
        "started_only", "request_only", "artifact_only", "runtime_only", "no_start",
        "no_artifact", "no_runtime", "runtime_without_metadata", "no_artifact_attempts",
        "no_runtime_attempts", "empty_artifact_attempts", "empty_runtime_attempts",
        "foreign_artifact_attempt", "foreign_runtime_attempt", "foreign_start",
        "foreign_artifact_model", "foreign_runtime_model", "foreign_requested_model",
        "artifact_id_mismatch", "actor_mismatch", "sequence_mismatch", "boolean_sequence",
        "no_deployment", "empty_upstream", "empty_provider", "builtin_fixture",
        "wrong_artifact_run", "wrong_runtime_run", "direct_hash_mismatch",
    ],
)
async def test_scoped_direct_completion_requires_linked_actual_gateway_metadata(
    direct_public_model_events: list[dict[str, Any]],
    invalid: str,
) -> None:
    module = load_script()
    events = copy.deepcopy(direct_public_model_events)
    start, artifact, _, completed = events
    if invalid.endswith("_only"):
        if invalid == "request_only":
            start["kind"] = "request.created"
        selected = artifact if invalid == "artifact_only" else (
            completed if invalid == "runtime_only" else start
        )
        events = [selected]
    elif invalid in {"no_start", "no_artifact", "no_runtime"}:
        kind = {"no_start": "model.started", "no_artifact": "artifact.created",
                "no_runtime": "runtime.completed"}[invalid]
        events = [event for event in events if event["kind"] != kind]
    elif invalid == "runtime_without_metadata":
        completed["payload"] = {}
    elif invalid in {"no_artifact_attempts", "no_runtime_attempts"}:
        event = artifact if invalid == "no_artifact_attempts" else completed
        event["payload"].pop("attempted_logical_models")
    elif invalid in {"empty_artifact_attempts", "empty_runtime_attempts"}:
        event = artifact if invalid == "empty_artifact_attempts" else completed
        event["payload"]["attempted_logical_models"] = []
    elif invalid in {"foreign_artifact_attempt", "foreign_runtime_attempt"}:
        event = artifact if invalid == "foreign_artifact_attempt" else completed
        event["payload"]["attempted_logical_models"] = ["other-model", "deepseek"]
    elif invalid in {"foreign_start", "foreign_artifact_model", "foreign_runtime_model"}:
        event = start if invalid == "foreign_start" else (
            artifact if invalid == "foreign_artifact_model" else completed
        )
        event["payload"]["logical_model"] = "other-model"
    elif invalid == "foreign_requested_model":
        artifact["payload"]["requested_logical_model"] = "other-model"
    elif invalid == "artifact_id_mismatch":
        completed["payload"]["artifact_id"] = "another-artifact"
    elif invalid == "actor_mismatch":
        completed["actor"] = "another-agent"
    elif invalid == "sequence_mismatch":
        completed["sequence"] = start["sequence"]
    elif invalid == "boolean_sequence":
        start["sequence"] = True
    elif invalid == "no_deployment":
        artifact["payload"].pop("deployment")
    elif invalid == "empty_upstream":
        artifact["payload"]["upstream_model"] = ""
    elif invalid == "empty_provider":
        artifact["payload"]["provider"] = ""
    elif invalid == "builtin_fixture":
        artifact["payload"]["artifact_origin"] = "builtin_fixture"
    elif invalid == "wrong_artifact_run":
        artifact["run_id"] = "another-run"
    elif invalid == "wrong_runtime_run":
        completed["run_id"] = "another-run"
    elif invalid == "direct_hash_mismatch":
        artifact["artifact"]["content_sha256"] = "0" * 64
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert evidence["errors"]


@pytest.mark.parametrize("attempts", ["empty", "missing"])
async def test_another_completion_cannot_hide_direct_artifact_without_attempt_proof(
    direct_public_model_events: list[dict[str, Any]], attempts: str,
) -> None:
    module = load_script()
    events = copy.deepcopy(direct_public_model_events)
    artifact = events[1]
    if attempts == "empty":
        artifact["payload"]["attempted_logical_models"] = []
    else:
        artifact["payload"].pop("attempted_logical_models")
    events.append(_model_event(events[0]["run_id"], "deepseek"))
    evidence = collect_direct_model_evidence(module, events)
    assert evidence["ok"] is False
    assert evidence["errors"]


@pytest.mark.parametrize("tamper", [None, "empty_attempts", "missing_start", "missing_deployment"])
async def test_direct_completion_proof_is_revalidated_during_finalization(
    direct_public_model_events: list[dict[str, Any]],
    tamper: str | None,
) -> None:
    module = load_script()
    evidence = collect_direct_model_evidence(module, direct_public_model_events)
    report = _pending_automated_report("deepseek")
    case = report["cases"][0]
    run_id = case["run"]["run_id"]
    for key in ("original_run_id", "result_run_id"):
        evidence[key] = run_id
    run = evidence["runs"][0]
    run["run_id"] = run_id
    run["events_endpoint"] = f"/api/v1/runs/{quote(run_id, safe='')}/events"
    for event in run["model_events"]:
        event["run_id"] = run_id
    if tamper == "empty_attempts":
        run["model_events"][1]["payload"]["attempted_logical_models"] = []
    elif tamper == "missing_start":
        run["model_events"].pop(0)
    elif tamper == "missing_deployment":
        run["model_events"][1]["payload"].pop("deployment", None)
    case["model_scope_evidence"] = evidence
    if tamper is None:
        finalized = module.finalize_real_device_acceptance(
            report, _real_device_evidence("matrix-123", "deepseek"),
            evidence_root=_evidence_root(),
        )
        assert finalized["acceptance_complete"] is True
    else:
        with pytest.raises(ValueError):
            module.finalize_real_device_acceptance(
                report, _real_device_evidence("matrix-123", "deepseek"),
                evidence_root=_evidence_root(),
            )


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
        events = [{"kind": "model.completed", "run_id": "run-1", **event["payload"]}]
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


@pytest.mark.parametrize("attempts", ["absent", "empty", "present"])
def test_scoped_completion_rejects_unproven_adapter_completion(
    matrix_harness: tuple[Any, Any, list[Any], list[str]],
    monkeypatch: Any,
    attempts: str,
) -> None:
    module, delegate, _, _ = matrix_harness
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    event: dict[str, Any] = {
        "kind": "model.completed", "run_id": "run-1", "sequence": 1,
        "payload": {"logical_model": "deepseek-backup"},
    }
    if attempts == "absent":
        pass
    elif attempts == "empty":
        event["payload"]["attempted_logical_models"] = []
    else:
        event["payload"]["attempted_logical_models"] = ["deepseek-backup"]
    delegate.model_events["run-1"] = [event]
    payload = run_matrix(module, delegate, logical_model="deepseek-backup")
    assert payload["core_acceptance_ok"] is False
    assert payload["cases"][0]["model_scope_evidence"]["ok"] is False


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
            saved, _real_device_evidence("matrix-123", "deepseek-backup"),
            evidence_root=_evidence_root(),
        )
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    saved["cases"] = [case]
    saved["submission_journal"]["records"] = saved["submission_journal"]["records"][:1]
    if tamper in {"missing_original", "missing_repair"}:
        with pytest.raises(ValueError, match="submission journal"):
            run_matrix(module, delegate, logical_model="deepseek-backup", resume_report=saved)
        assert plans == []
        return
    receipt = saved["submission_journal"]["records"][0]["response"]
    delegate.runs["original"] = copy.deepcopy(receipt)
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
    with pytest.raises(ValueError, match="unresolved submission"):
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
        evidence_root=_evidence_root(),
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
        _pending_automated_report(), _real_device_evidence("matrix-123"),
        evidence_root=_evidence_root(),
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
        evidence_root=_evidence_root(),
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
            "--evidence-root", str(_evidence_root()), "--resume-report",
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
            "--evidence-root", str(_evidence_root()), "--resume-report",
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
            "--evidence-root", str(_evidence_root()), "--resume-report",
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


def test_resume_confirmed_active_case_uses_reads_without_reposting(
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
    assert delegate.submissions.count(
        ("project-scale-small-auto-0-matrix-123-auto-small", "run-1")
    ) == 1
    assert len(delegate.submissions) == 20
    assert len({key for key, _ in delegate.submissions}) == 20
    assert set(delegate.observed_runs[:2]) == {"run-1"}
    assert resumed["cases"][0]["attempt"] == 1
    assert resumed["core_passed_case_count"] == 20


@pytest.mark.parametrize(
    "interrupt_path",
    ["/api/v1/admin/project-workspaces", "/api/v1/admin/conversations"],
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


def test_real_runner_never_reposts_unknown_commit_after_interruption(
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
    before = output.read_bytes()
    request_count = len(delegate.requests)
    with pytest.raises(ValueError, match="unresolved"):
        run_matrix(module, delegate, output_path=str(output), resume_report=saved)

    assert len(delegate.runs) == 1
    assert delegate.submissions == [
        ("project-scale-small-auto-0-matrix-123-auto-small", "run-1"),
    ]
    assert delegate.requests[request_count:] == [("GET", "/api/v1/auth/me")]
    assert output.read_bytes() == before
    checkpoint = json.loads(output.read_text(encoding="utf-8"))
    assert checkpoint["cases"] == []
    assert checkpoint["core_acceptance_ok"] is False
