from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path
from typing import Any

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


def test_scale_plan_reuses_runner_and_uses_real_conversation_scope() -> None:
    module = load_script()

    plan = module.build_real_user_scale_plan(
        scale="large",
        project_id="uat-large-123",
        project_label="真实用户 large 验收",
        conversation_id="conv-large-123",
        workspace_session_id="conv-large-123",
    )

    assert plan.benchmark_kind == "capability"
    assert plan.execute is True
    assert plan.case_count == 1
    request = plan.requests[0]
    assert request.case_id == "large:real_user"
    assert request.body["mode"] == "auto"
    assert request.body["project_id"] == "uat-large-123"
    assert request.body["project_label"] == "真实用户 large 验收"
    assert request.body["conversation_id"] == "conv-large-123"
    assert request.body["workspace_session_id"] == "conv-large-123"
    assert request.body["runtime_timeout_seconds"] == 1800


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


def test_report_keeps_dynamic_preview_pending_out_of_passed_evidence() -> None:
    module = load_script()
    case = ProjectScaleCaseResult(
        case_id="small:artifact_production",
        run_id="run-small",
        status="completed",
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
    )

    assert payload["core_acceptance_ok"] is True
    assert payload["acceptance_complete"] is False
    assert payload["status"] == "pending"
    assert payload["dynamic_web_preview"]["status"] == "pending"
    assert payload["dynamic_web_preview"]["counted_as_passed"] is False
    assert payload["success_basis"]["admin_internal_run_data"] is False


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


def test_real_user_matrix_creates_unique_projects_and_conversations_for_all_scales(
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
            raise AssertionError(f"unexpected bytes request: {method} {path}")

    plans: list[Any] = []

    def execute(plan: Any, client: Any, **kwargs: object) -> ProjectScaleExecutionReport:
        del client, kwargs
        plans.append(plan)
        scale = plan.requests[0].case_id.split(":", 1)[0]
        result = ProjectScaleCaseResult(
            case_id=plan.requests[0].case_id,
            run_id=f"run-{scale}",
            status="completed",
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

    assert [case["scale"] for case in payload["cases"]] == [
        "small",
        "medium",
        "large",
        "ultra",
    ]
    assert len(set(delegate.created_projects)) == 4
    assert len(set(delegate.created_conversations)) == 4
    expected_workspace_paths = [
        module._safe_workspace_session_token(
            f"conv-matrix-123-{scale}",
            f"matrix-123-{scale}",
        )
        for scale in ("small", "medium", "large", "ultra")
    ]
    assert delegate.project_workspace_paths == expected_workspace_paths
    assert delegate.conversation_workspace_paths == expected_workspace_paths
    assert all(plan.requests[0].body["mode"] == "auto" for plan in plans)
    assert payload["core_acceptance_ok"] is True
    assert payload["status"] == "pending"
    assert payload["acceptance_complete"] is False
    assert payload["blocked_admin_run_requests"] == []
