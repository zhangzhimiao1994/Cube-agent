#!/usr/bin/env python3
"""Run the four project scales through the same HTTP path as a logged-in user."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from agent_hub.harness.project_scale import (
    PROJECT_SCALE_TIERS,
    ProjectScaleRunPlan,
    ProjectScaleRunRequest,
    build_project_scale_run_plan,
)
from agent_hub.harness.project_scale_runner import (
    AcceptanceClient,
    ProjectScaleCaseResult,
    UrllibAcceptanceClient,
    _acceptance_credentials_from_env,
    _effective_execute_wait_seconds,
    _safe_workspace_session_token,
    _safe_zip_member_path,
    execute_project_scale_plan,
)

_FLOW = "artifact_production"
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_ID_RE = re.compile(r"[^a-z0-9-]+")
_ADMIN_RUN_PREFIX = "/api/v1/admin/runs"
_PREVIEW_PENDING_REASON = (
    "dynamic website preview is not part of the current project_scale_runner contract; "
    "a reachable preview URL and browser interaction still require separate acceptance"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RealUserAcceptanceClient:
    """Audit HTTP use and make the runner's admin-run fallback impossible."""

    def __init__(self, delegate: AcceptanceClient) -> None:
        self._delegate = delegate
        self.request_log: list[str] = []
        self.blocked_admin_run_requests: list[str] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        self._record(method, path)
        if _is_admin_run_path(path):
            blocked = f"{method.upper()} {path}"
            self.blocked_admin_run_requests.append(blocked)
            raise RuntimeError(f"{blocked} is forbidden as acceptance evidence")
        return cast(
            dict[str, object] | list[object],
            self._delegate.request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            ),
        )

    def request_bytes(self, method: str, path: str) -> bytes:
        self._record(method, path)
        if _is_admin_run_path(path):
            blocked = f"{method.upper()} {path}"
            self.blocked_admin_run_requests.append(blocked)
            raise RuntimeError(f"{blocked} is forbidden as acceptance evidence")
        return cast(bytes, self._delegate.request_bytes(method, path))

    def _record(self, method: str, path: str) -> None:
        self.request_log.append(f"{method.upper()} {path}")


def _is_admin_run_path(path: str) -> bool:
    normalized = urlsplit(path).path.rstrip("/")
    return normalized == _ADMIN_RUN_PREFIX or normalized.startswith(f"{_ADMIN_RUN_PREFIX}/")


def build_real_user_scale_plan(
    *,
    scale: str,
    project_id: str,
    project_label: str,
    conversation_id: str,
    workspace_session_id: str,
) -> ProjectScaleRunPlan:
    """Reuse the capability runner, replacing only user/session scope and auto mode."""

    base = build_project_scale_run_plan(
        scales=(scale,),
        flows=(_FLOW,),
        execute=True,
        benchmark_kind="capability",
    )
    request = base.requests[0]
    body = dict(request.body)
    body.update(
        {
            "mode": "auto",
            "project_id": project_id,
            "project_label": project_label,
            "conversation_id": conversation_id,
            "workspace_session_id": workspace_session_id,
        }
    )
    scoped_request = ProjectScaleRunRequest(
        case_id=f"{scale}:real_user",
        body=body,
        validation_focus=request.validation_focus,
    )
    return replace(base, requests=(scoped_request,))


def verify_public_workspace_artifacts(
    client: AcceptanceClient,
    *,
    project_id: str,
    workspace_session_id: str,
) -> dict[str, object]:
    """Cross-check the public file API, every public download, and the public ZIP."""

    encoded_project = quote(project_id, safe="")
    encoded_session = quote(workspace_session_id, safe="")
    root = f"/api/v1/workspaces/projects/{encoded_project}/sessions/{encoded_session}"
    files_path = f"{root}/files"
    bundle_path = f"{root}/bundle/download"
    errors: list[str] = []
    downloaded_file_count = 0
    zip_sha256: str | None = None
    zip_crc_ok = False
    metadata_matches_zip = False
    unsafe_member_count = 0
    file_items: list[Mapping[str, object]] = []
    bundle = b""

    try:
        listing = client.request_json("GET", files_path)
        if not isinstance(listing, dict):
            raise TypeError("public workspace file list returned non-object JSON")
        raw_items = listing.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise RuntimeError("public workspace file list is empty")
        if listing.get("bundle_download_url") != bundle_path:
            errors.append("public bundle_download_url does not match the requested workspace")
        file_items = [cast(Mapping[str, object], item) for item in raw_items if isinstance(item, Mapping)]
        if len(file_items) != len(raw_items):
            errors.append("public workspace file list contains invalid items")
        bundle = client.request_bytes("GET", bundle_path)
    except Exception as error:  # noqa: BLE001 - report every scale instead of aborting the matrix.
        errors.append(f"public workspace API: {error}")

    zip_files: dict[str, bytes] = {}
    if bundle:
        zip_sha256 = hashlib.sha256(bundle).hexdigest()
        try:
            with zipfile.ZipFile(BytesIO(bundle)) as archive:
                seen: set[str] = set()
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    try:
                        safe_path = str(_safe_zip_member_path(info.filename))
                    except RuntimeError:
                        unsafe_member_count += 1
                        continue
                    if safe_path in seen:
                        errors.append(f"public ZIP contains duplicate member: {safe_path}")
                        continue
                    seen.add(safe_path)
                    zip_files[safe_path] = archive.read(info)
                bad_member = archive.testzip()
                zip_crc_ok = bad_member is None
                if bad_member is not None:
                    errors.append(f"public ZIP CRC failed: {bad_member}")
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            errors.append(f"public ZIP invalid: {error}")
    else:
        errors.append("public ZIP download is empty")

    listed_paths: set[str] = set()
    for item in file_items:
        raw_path = item.get("path")
        raw_size = item.get("size_bytes")
        raw_sha256 = item.get("sha256")
        raw_download_url = item.get("download_url")
        if not isinstance(raw_path, str):
            errors.append("public file metadata is missing path")
            continue
        try:
            safe_path = str(_safe_zip_member_path(raw_path))
        except RuntimeError as error:
            errors.append(str(error))
            continue
        listed_paths.add(safe_path)
        if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
            errors.append(f"public file has invalid size: {safe_path}")
            continue
        if not isinstance(raw_sha256, str) or _SHA256_RE.fullmatch(raw_sha256) is None:
            errors.append(f"public file has invalid sha256: {safe_path}")
            continue
        if not isinstance(raw_download_url, str) or not _valid_public_file_url(
            raw_download_url,
            expected_path=f"{root}/files/download",
            expected_file=safe_path,
        ):
            errors.append(f"public file has invalid download_url: {safe_path}")
            continue
        try:
            downloaded = client.request_bytes("GET", raw_download_url)
        except Exception as error:  # noqa: BLE001 - preserve the complete evidence report.
            errors.append(f"public file download failed {safe_path}: {error}")
            continue
        downloaded_file_count += 1
        if len(downloaded) != raw_size:
            errors.append(f"public file size mismatch: {safe_path}")
        if hashlib.sha256(downloaded).hexdigest() != raw_sha256:
            errors.append(f"public file sha256 mismatch: {safe_path}")
        zipped = zip_files.get(safe_path)
        if zipped is None:
            errors.append(f"public ZIP is missing listed file: {safe_path}")
        elif zipped != downloaded:
            errors.append(f"public ZIP content mismatch: {safe_path}")

    if listed_paths != set(zip_files):
        errors.append("public file list and ZIP member set differ")
    elif listed_paths:
        metadata_matches_zip = True
    if unsafe_member_count:
        errors.append(f"public ZIP contains {unsafe_member_count} unsafe members")

    return {
        "ok": not errors,
        "source": "public_workspace_api",
        "admin_internal_run_data_used": False,
        "files_endpoint": files_path,
        "bundle_endpoint": bundle_path,
        "file_count": len(listed_paths),
        "downloaded_file_count": downloaded_file_count,
        "zip_member_count": len(zip_files),
        "zip_size_bytes": len(bundle),
        "zip_sha256": zip_sha256,
        "zip_crc_ok": zip_crc_ok,
        "metadata_matches_zip": metadata_matches_zip,
        "unsafe_member_count": unsafe_member_count,
        "errors": errors,
    }


def _valid_public_file_url(value: str, *, expected_path: str, expected_file: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != expected_path:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return query.get("path") == [expected_file]


def _dynamic_preview_pending() -> dict[str, object]:
    return {
        "status": "pending",
        "counted_as_passed": False,
        "reason": _PREVIEW_PENDING_REASON,
        "required_evidence": [
            "reachable_preview_url",
            "desktop_browser_interaction",
            "mobile_browser_interaction",
            "preview_process_lifecycle_cleanup",
        ],
    }


def build_case_report(
    *,
    scale: str,
    project: Mapping[str, object],
    conversation: Mapping[str, object],
    result: ProjectScaleCaseResult,
    public_artifacts: Mapping[str, object],
) -> dict[str, object]:
    generated_project_ok = result.evidence.get("generated_project_validation") is True
    requirements_ok = result.evidence.get("requirements_validation") is True
    public_artifacts_ok = public_artifacts.get("ok") is True
    core_ok = result.ok and generated_project_ok and requirements_ok and public_artifacts_ok
    preview = _dynamic_preview_pending()
    return {
        "scale": scale,
        "status": "pending" if core_ok else "failed",
        "core_acceptance_ok": core_ok,
        "acceptance_complete": False,
        "project": dict(project),
        "conversation": dict(conversation),
        "run": result.to_payload(),
        "build_and_test": {
            "status": "passed" if generated_project_ok and requirements_ok else "failed",
            "source": "project_scale_runner.generated_project_validation",
            "generated_project_validation": generated_project_ok,
            "requirements_validation": requirements_ok,
        },
        "public_artifacts": dict(public_artifacts),
        "dynamic_web_preview": preview,
        "success_basis": {
            "logged_in_user_http_api": True,
            "public_run_api": True,
            "public_workspace_file_api": public_artifacts_ok,
            "public_workspace_zip": public_artifacts_ok,
            "admin_internal_run_data": False,
        },
    }


def run_real_user_four_scale_acceptance(
    client: RealUserAcceptanceClient,
    *,
    username: str,
    base_url: str,
    execution_id: str,
    wait_seconds: float,
    poll_interval_seconds: float,
    artifact_build_timeout_seconds: float,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    started_at = _utc_now()
    principal = client.request_json("GET", "/api/v1/auth/me")
    if not isinstance(principal, dict):
        raise TypeError("GET /api/v1/auth/me returned non-object JSON")
    cases: list[dict[str, object]] = []
    safe_execution_id = _safe_identifier(execution_id)

    for scale in PROJECT_SCALE_TIERS:
        project_id = _bounded_identifier(f"uat-{safe_execution_id}-{scale}", 128)
        conversation_id = _bounded_identifier(f"conv-{safe_execution_id}-{scale}", 128)
        project_label = f"真实用户 {scale} 验收"
        runner_execution_id = f"{safe_execution_id}-{scale}"
        workspace_session_id = _safe_workspace_session_token(
            conversation_id,
            runner_execution_id,
        )
        if progress is not None:
            progress(f"{scale}: creating project and conversation")
        try:
            project = client.request_json(
                "POST",
                "/api/v1/admin/project-workspaces",
                body={
                    "project_id": project_id,
                    "label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{safe_execution_id}-{scale}-project",
            )
            if not isinstance(project, dict) or project.get("project_id") != project_id:
                raise RuntimeError("project workspace creation returned the wrong project")
            conversation = client.request_json(
                "POST",
                "/api/v1/admin/conversations",
                body={
                    "conversation_id": conversation_id,
                    "title": f"{scale} 真实用户项目验收",
                    "project_id": project_id,
                    "project_label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{safe_execution_id}-{scale}-conversation",
            )
            if not isinstance(conversation, dict) or conversation.get(
                "conversation_id"
            ) != conversation_id:
                raise RuntimeError("conversation creation returned the wrong conversation")

            plan = build_real_user_scale_plan(
                scale=scale,
                project_id=project_id,
                project_label=project_label,
                conversation_id=conversation_id,
                workspace_session_id=conversation_id,
            )
            effective_wait = _effective_execute_wait_seconds(
                plan,
                wait_seconds,
                generated_project_timeout_seconds=artifact_build_timeout_seconds,
            )
            if progress is not None:
                progress(f"{scale}: executing capability run with wait budget {effective_wait:.0f}s")
            runner_report = execute_project_scale_plan(
                plan,
                client,
                wait_seconds=effective_wait,
                poll_interval_seconds=poll_interval_seconds,
                execution_id=runner_execution_id,
                validate_generated_project=True,
                generated_project_timeout_seconds=artifact_build_timeout_seconds,
                progress=(
                    (lambda message, scale=scale: progress(f"{scale}: {message}"))
                    if progress is not None
                    else None
                ),
            )
            result = runner_report.results[0]
            public_artifacts = verify_public_workspace_artifacts(
                client,
                project_id=project_id,
                workspace_session_id=workspace_session_id,
            )
            cases.append(
                build_case_report(
                    scale=scale,
                    project=project,
                    conversation=conversation,
                    result=result,
                    public_artifacts=public_artifacts,
                )
            )
        except Exception as error:  # noqa: BLE001 - all four scales must be attempted.
            cases.append(
                {
                    "scale": scale,
                    "status": "failed",
                    "core_acceptance_ok": False,
                    "acceptance_complete": False,
                    "project": {"project_id": project_id},
                    "conversation": {"conversation_id": conversation_id},
                    "errors": [str(error)],
                    "dynamic_web_preview": _dynamic_preview_pending(),
                    "success_basis": {
                        "logged_in_user_http_api": True,
                        "admin_internal_run_data": False,
                    },
                }
            )

    core_ok = len(cases) == len(PROJECT_SCALE_TIERS) and all(
        case.get("core_acceptance_ok") is True for case in cases
    )
    status = "pending" if core_ok else "failed"
    return {
        "schema_version": 1,
        "kind": "real_user_four_scale_acceptance",
        "status": status,
        "core_acceptance_ok": core_ok,
        "acceptance_complete": False,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "base_url": base_url.rstrip("/"),
        "execution_id": execution_id,
        "benchmark_kind": "capability",
        "run_mode": "auto",
        "scales": list(PROJECT_SCALE_TIERS),
        "actor": {
            "username_from_environment": username,
            "authenticated_via_password_environment": True,
            "principal": principal,
        },
        "case_count": len(cases),
        "core_passed_case_count": sum(
            1 for case in cases if case.get("core_acceptance_ok") is True
        ),
        "failed_case_count": sum(1 for case in cases if case.get("status") == "failed"),
        "pending_case_count": sum(1 for case in cases if case.get("status") == "pending"),
        "dynamic_web_preview": _dynamic_preview_pending(),
        "success_policy": {
            "admin_internal_run_data_allowed": False,
            "fixture_results_allowed": False,
            "public_files_and_zip_required": True,
            "generated_project_build_and_tests_required": True,
            "pending_preview_counts_as_complete": False,
        },
        "blocked_admin_run_requests": list(client.blocked_admin_run_requests),
        "cases": cases,
    }


def _safe_identifier(value: str) -> str:
    normalized = _SAFE_ID_RE.sub("-", value.strip().casefold()).strip("-")
    return normalized or uuid4().hex[:12]


def _bounded_identifier(value: str, limit: int) -> str:
    return value[:limit].rstrip("-")


def _default_execution_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"


def _write_report(path: str | None, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run small/medium/large/ultra capability acceptance as one logged-in user. "
            "Exit 0 means complete, 1 means failed, and 2 means core passed with pending preview."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS", "20")),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_WAIT_SECONDS", "0")),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_POLL_INTERVAL_SECONDS", "2")),
    )
    parser.add_argument(
        "--artifact-build-timeout",
        type=float,
        default=float(
            os.environ.get("AGENT_HUB_PROJECT_SCALE_ARTIFACT_BUILD_TIMEOUT_SECONDS", "120")
        ),
    )
    parser.add_argument(
        "--execution-id",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID") or _default_execution_id(),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_REPORT_PATH"),
    )
    args = parser.parse_args(argv)

    username, password, tenant_id = _acceptance_credentials_from_env()
    if not username or not password:
        parser.error(
            "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required "
            "(AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME/PASSWORD is also accepted)"
        )

    delegate = UrllibAcceptanceClient(
        base_url=args.base_url,
        timeout=args.timeout,
        username=username,
        password=password,
        tenant_id=tenant_id,
    )
    client = RealUserAcceptanceClient(delegate)
    try:
        payload = run_real_user_four_scale_acceptance(
            client,
            username=username,
            base_url=args.base_url,
            execution_id=args.execution_id,
            wait_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_interval,
            artifact_build_timeout_seconds=args.artifact_build_timeout,
            progress=lambda message: print(
                f"real-user-four-scale progress: {message}",
                file=sys.stderr,
                flush=True,
            ),
        )
    except Exception as error:  # noqa: BLE001 - emit a machine-readable authentication/setup failure.
        payload = {
            "schema_version": 1,
            "kind": "real_user_four_scale_acceptance",
            "status": "failed",
            "core_acceptance_ok": False,
            "acceptance_complete": False,
            "base_url": args.base_url.rstrip("/"),
            "execution_id": args.execution_id,
            "errors": [str(error)],
            "dynamic_web_preview": _dynamic_preview_pending(),
        }
    _write_report(args.output, payload)
    if payload.get("status") == "failed":
        return 1
    if payload.get("acceptance_complete") is not True:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
