#!/usr/bin/env python3
"""Run the four project scales through the same HTTP path as a logged-in user."""

from __future__ import annotations

import argparse
import copy
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
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urljoin, urlsplit
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

_MODE_CAPABILITIES = ("direct", "dispatch", "hybrid", "multi_agent")
_ROUTE_INTENTS = ("auto", *_MODE_CAPABILITIES)
_ACCEPTANCE_CASES = (
    *(("auto_scale", scale, "auto", f"auto-{scale}") for scale in PROJECT_SCALE_TIERS),
    *(
        (
            "mode_capability",
            scale,
            mode,
            f"mode-{scale}-{mode.replace('_', '-')}",
        )
        for scale in PROJECT_SCALE_TIERS
        for mode in _MODE_CAPABILITIES
    ),
)
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_ID_RE = re.compile(r"[^a-z0-9-]+")
_ADMIN_RUN_PREFIX = "/api/v1/admin/runs"
_MAX_PREVIEW_ASSETS = 24
_WEBSITE_DELIVERABLE_REQUIREMENT = (
    " Also include a complete interactive website for the project. Put a self-contained "
    "preview.html or index.html entrypoint in the workspace with usable navigation and at least "
    "one real interaction backed by the generated project. Do not return a placeholder preview."
)


class _PreviewAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {name.casefold(): value for name, value in attrs}
        reference: str | None = None
        if tag.casefold() in {"script", "img", "source", "video", "audio"}:
            reference = values.get("src")
        elif tag.casefold() == "link":
            rel = (values.get("rel") or "").casefold().split()
            if any(value in {"stylesheet", "icon", "preload", "modulepreload"} for value in rel):
                reference = values.get("href")
        if reference:
            self.references.append(reference)


def _preview_asset_paths(html: bytes, preview_url: str) -> tuple[str, ...]:
    try:
        decoded = html.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    parser = _PreviewAssetParser()
    parser.feed(decoded)
    prefix = preview_url if preview_url.endswith("/") else f"{preview_url}/"
    paths: list[str] = []
    for reference in parser.references:
        if reference.startswith(("data:", "blob:", "mailto:", "javascript:", "#", "//")):
            continue
        resolved = urljoin(prefix, reference)
        parsed = urlsplit(resolved)
        if parsed.scheme or parsed.netloc or not resolved.startswith(prefix):
            continue
        if resolved not in paths:
            paths.append(resolved)
        if len(paths) >= _MAX_PREVIEW_ASSETS:
            break
    return tuple(paths)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


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
        return self._delegate.request_json(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
        )

    def request_bytes(self, method: str, path: str) -> bytes:
        self._record(method, path)
        if _is_admin_run_path(path):
            blocked = f"{method.upper()} {path}"
            self.blocked_admin_run_requests.append(blocked)
            raise RuntimeError(f"{blocked} is forbidden as acceptance evidence")
        return self._delegate.request_bytes(method, path)

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
    route_intent: str,
) -> ProjectScaleRunPlan:
    """Build one natural AUTO scale case or one explicit mode capability case."""

    if route_intent not in _ROUTE_INTENTS:
        raise ValueError(f"unknown real-user route intent: {route_intent}")

    base_flow = "artifact_production" if route_intent == "auto" else route_intent
    base = build_project_scale_run_plan(
        scales=(scale,),
        flows=(base_flow,),
        execute=True,
        benchmark_kind="capability",
    )
    request = base.requests[0]
    body = dict(request.body)
    body.update(
        {
            "mode": "auto" if route_intent == "auto" else body["mode"],
            "project_id": project_id,
            "project_label": project_label,
            "conversation_id": conversation_id,
            "workspace_session_id": workspace_session_id,
            "message": f"{body['message']}{_WEBSITE_DELIVERABLE_REQUIREMENT}",
        }
    )
    scoped_request = ProjectScaleRunRequest(
        case_id=f"{scale}:{route_intent}",
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
        "status": "pending_real_device",
        "counted_as_passed": False,
        "reason": "desktop and mobile browser interaction require deployed real-device acceptance",
        "required_evidence": [
            "desktop_browser_interaction",
            "mobile_browser_interaction",
        ],
    }


def verify_dynamic_web_preview(
    client: AcceptanceClient,
    *,
    project_id: str,
    conversation_id: str,
    workspace_session_id: str,
) -> dict[str, object]:
    """Exercise the public preview lifecycle without retaining its capability token."""

    preview_id: str | None = None
    preview_url: str | None = None
    reachable = False
    current_preview_matches = False
    renewed = False
    lease_extended = False
    stopped = False
    revoked_after_stop = False
    content_size_bytes = 0
    referenced_asset_count = 0
    referenced_assets_loaded = 0
    errors: list[str] = []
    try:
        started = client.request_json(
            "POST",
            "/api/v1/web-previews/start",
            body={
                "project_id": project_id,
                "conversation_id": conversation_id,
                "workspace_session_id": workspace_session_id,
            },
        )
        if not isinstance(started, dict):
            raise TypeError("preview start returned non-object JSON")
        raw_id = started.get("id")
        raw_url = started.get("preview_url")
        if started.get("status") != "ready" or not isinstance(raw_id, str):
            raise RuntimeError("preview did not become ready")
        if not isinstance(raw_url, str) or not raw_url.startswith(
            f"/api/v1/web-previews/{quote(raw_id, safe='')}/content/"
        ):
            raise RuntimeError("preview returned an invalid public URL")
        preview_id = raw_id
        preview_url = raw_url
        current = client.request_json(
            "GET",
            f"/api/v1/web-previews/conversations/{quote(conversation_id, safe='')}",
        )
        current_preview_matches = (
            isinstance(current, dict)
            and current.get("id") == preview_id
            and current.get("status") == "ready"
            and current.get("preview_url") == preview_url
        )
        if not current_preview_matches:
            errors.append("current preview lookup did not match the started preview")
        content = client.request_bytes("GET", preview_url)
        content_size_bytes = len(content)
        lowered = content[:4096].lower()
        if not content or b"<html" not in lowered and b"<!doctype html" not in lowered:
            raise RuntimeError("preview root did not return HTML")
        reachable = True
        asset_paths = _preview_asset_paths(content, preview_url)
        referenced_asset_count = len(asset_paths)
        for asset_path in asset_paths:
            asset = client.request_bytes("GET", asset_path)
            if not asset:
                raise RuntimeError(f"preview asset returned an empty response: {asset_path}")
            referenced_assets_loaded += 1
        renewed_payload = client.request_json(
            "POST",
            f"/api/v1/web-previews/{quote(preview_id, safe='')}/renew",
        )
        if not isinstance(renewed_payload, dict):
            raise TypeError("preview renew returned non-object JSON")
        renewed = (
            renewed_payload.get("id") == preview_id
            and renewed_payload.get("status") == "ready"
        )
        initial_expiry = _timestamp(started.get("lease_expires_at"))
        renewed_expiry = _timestamp(renewed_payload.get("lease_expires_at"))
        lease_extended = (
            initial_expiry is not None
            and renewed_expiry is not None
            and renewed_expiry > initial_expiry
        )
        if not renewed:
            errors.append("preview renew did not preserve the ready preview")
        if not lease_extended:
            errors.append("preview renew did not extend the lease")
    except Exception as error:  # noqa: BLE001 - preserve complete lifecycle evidence.
        errors.append(f"preview lifecycle: {error}")
    finally:
        if preview_id is not None:
            try:
                stopped_payload = client.request_json(
                    "DELETE",
                    f"/api/v1/web-previews/{quote(preview_id, safe='')}",
                )
                stopped = (
                    isinstance(stopped_payload, dict)
                    and stopped_payload.get("id") == preview_id
                    and stopped_payload.get("status") == "stopped"
                    and stopped_payload.get("preview_url") is None
                )
                if not stopped:
                    errors.append("preview stop did not return a stopped state")
            except Exception as error:  # noqa: BLE001 - report cleanup failures.
                errors.append(f"preview stop: {error}")
        if preview_url is not None and stopped:
            try:
                client.request_bytes("GET", preview_url)
            except Exception as error:  # noqa: BLE001 - record exact public revocation evidence.
                if _is_explicit_preview_not_found(error):
                    revoked_after_stop = True
                else:
                    errors.append(f"preview revocation check failed: {error}")
            else:
                errors.append("preview capability URL remained readable after stop")

    passed = (
        reachable
        and current_preview_matches
        and renewed
        and lease_extended
        and referenced_assets_loaded == referenced_asset_count
        and stopped
        and revoked_after_stop
        and not errors
    )
    return {
        "status": "passed" if passed else "failed",
        "counted_as_passed": passed,
        "reachable_preview_url": reachable,
        "current_preview_matches": current_preview_matches,
        "renewed": renewed,
        "lease_extended": lease_extended,
        "referenced_asset_count": referenced_asset_count,
        "referenced_assets_loaded": referenced_assets_loaded,
        "stopped": stopped,
        "revoked_after_stop": revoked_after_stop,
        "content_size_bytes": content_size_bytes,
        "browser_interaction": "pending_real_device",
        "capability_token_retained": False,
        "errors": errors,
    }


def build_case_report(
    *,
    scale: str,
    project: Mapping[str, object],
    conversation: Mapping[str, object],
    result: ProjectScaleCaseResult,
    public_artifacts: Mapping[str, object],
    dynamic_web_preview: Mapping[str, object],
) -> dict[str, object]:
    generated_project_ok = result.evidence.get("generated_project_validation") is True
    requirements_ok = result.evidence.get("requirements_validation") is True
    public_artifacts_ok = public_artifacts.get("ok") is True
    preview_ok = dynamic_web_preview.get("counted_as_passed") is True
    route_intent = result.case_id.split(":", 1)[1]
    case_kind = "auto_scale" if route_intent == "auto" else "mode_capability"
    expected_modes = {
        "auto": (
            frozenset({"hybrid"})
            if scale in {"large", "ultra"}
            else frozenset({"direct", "dispatch", "hybrid"})
        ),
        "direct": frozenset({"direct"}),
        "dispatch": frozenset({"dispatch"}),
        "hybrid": frozenset({"hybrid"}),
        "multi_agent": frozenset({"dispatch"}),
    }
    allowed_modes = expected_modes.get(route_intent)
    observed_route_ok = allowed_modes is None or result.observed_mode in allowed_modes
    multi_agent_evidence_ok = (
        route_intent != "multi_agent"
        or (
            result.evidence.get("multi_agent_participation") is True
            and len(result.participant_agent_ids) >= 2
            and result.participant_event_count >= 2
        )
    )
    core_ok = (
        result.ok
        and generated_project_ok
        and requirements_ok
        and public_artifacts_ok
        and preview_ok
        and observed_route_ok
        and multi_agent_evidence_ok
    )
    return {
        "case_id": result.case_id,
        "case_kind": case_kind,
        "scale": scale,
        "route_intent": route_intent,
        "observed_mode": result.observed_mode,
        "initial_observed_mode": result.observed_mode,
        "final_observed_mode": result.final_observed_mode or result.observed_mode,
        "observed_route_ok": observed_route_ok,
        "autonomous_mode_selected": case_kind == "auto_scale" and observed_route_ok,
        "multi_agent_evidence_ok": multi_agent_evidence_ok,
        "status": "pending_real_device" if core_ok else "failed",
        "core_acceptance_ok": core_ok,
        "automated_acceptance_complete": core_ok,
        "real_device_acceptance_complete": False,
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
        "dynamic_web_preview": dict(dynamic_web_preview),
        "success_basis": {
            "logged_in_user_http_api": True,
            "public_run_api": True,
            "public_workspace_file_api": public_artifacts_ok,
            "public_workspace_zip": public_artifacts_ok,
            "public_preview_lifecycle": preview_ok,
            "observed_route": observed_route_ok,
            "multi_agent_participation": multi_agent_evidence_ok,
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

    for case_kind, scale, route_intent, case_key in _ACCEPTANCE_CASES:
        project_id = _bounded_identifier(f"uat-{safe_execution_id}-{case_key}", 128)
        conversation_id = _bounded_identifier(
            f"conv-{safe_execution_id}-{case_key}",
            128,
        )
        project_label = (
            f"真实用户 {scale} AUTO 规模验收"
            if case_kind == "auto_scale"
            else f"真实用户 {route_intent} 模式能力验收"
        )
        runner_execution_id = f"{safe_execution_id}-{case_key}"
        workspace_session_id = _safe_workspace_session_token(
            conversation_id,
            runner_execution_id,
        )
        if progress is not None:
            progress(f"{case_kind}/{scale}/{route_intent}: creating project and conversation")
        try:
            project = client.request_json(
                "POST",
                "/api/v1/admin/project-workspaces",
                body={
                    "project_id": project_id,
                    "label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{safe_execution_id}-{case_key}-project",
            )
            if not isinstance(project, dict) or project.get("project_id") != project_id:
                raise RuntimeError("project workspace creation returned the wrong project")
            conversation = client.request_json(
                "POST",
                "/api/v1/admin/conversations",
                body={
                    "conversation_id": conversation_id,
                    "title": f"{case_kind} {scale} {route_intent} 真实用户项目验收",
                    "project_id": project_id,
                    "project_label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{safe_execution_id}-{case_key}-conversation",
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
                workspace_session_id=workspace_session_id,
                route_intent=route_intent,
            )
            effective_wait = _effective_execute_wait_seconds(
                plan,
                wait_seconds,
                generated_project_timeout_seconds=artifact_build_timeout_seconds,
            )
            if progress is not None:
                progress(
                    f"{case_kind}/{scale}/{route_intent}: executing capability run "
                    f"with wait budget {effective_wait:.0f}s"
                )
            case_progress: Callable[[str], None] | None = None
            if progress is not None:
                progress_callback = progress
                progress_prefix = f"{case_kind}/{scale}/{route_intent}"

                def emit_case_progress(
                    message: str,
                    callback: Callable[[str], None] = progress_callback,
                    prefix: str = progress_prefix,
                ) -> None:
                    callback(f"{prefix}: {message}")

                case_progress = emit_case_progress
            runner_report = execute_project_scale_plan(
                plan,
                client,
                wait_seconds=effective_wait,
                poll_interval_seconds=poll_interval_seconds,
                execution_id=runner_execution_id,
                validate_generated_project=True,
                generated_project_timeout_seconds=artifact_build_timeout_seconds,
                progress=case_progress,
            )
            result = runner_report.results[0]
            public_artifacts = verify_public_workspace_artifacts(
                client,
                project_id=project_id,
                workspace_session_id=workspace_session_id,
            )
            dynamic_web_preview = verify_dynamic_web_preview(
                client,
                project_id=project_id,
                conversation_id=conversation_id,
                workspace_session_id=workspace_session_id,
            )
            cases.append(
                build_case_report(
                    scale=scale,
                    project=project,
                    conversation=conversation,
                    result=result,
                    public_artifacts=public_artifacts,
                    dynamic_web_preview=dynamic_web_preview,
                )
            )
        except Exception as error:  # noqa: BLE001 - every matrix case must be attempted.
            cases.append(
                {
                    "scale": scale,
                    "case_kind": case_kind,
                    "route_intent": route_intent,
                    "status": "failed",
                    "core_acceptance_ok": False,
                    "automated_acceptance_complete": False,
                    "real_device_acceptance_complete": False,
                    "acceptance_complete": False,
                    "project": {"project_id": project_id},
                    "conversation": {"conversation_id": conversation_id},
                    "errors": [str(error)],
                    "dynamic_web_preview": {
                        "status": "failed",
                        "counted_as_passed": False,
                        "browser_interaction": "pending_real_device",
                    },
                    "success_basis": {
                        "logged_in_user_http_api": True,
                        "admin_internal_run_data": False,
                    },
                }
            )

    expected_case_count = len(_ACCEPTANCE_CASES)
    core_ok = len(cases) == expected_case_count and all(
        case.get("core_acceptance_ok") is True for case in cases
    )
    status = "pending_real_device" if core_ok else "failed"
    return {
        "schema_version": 1,
        "kind": "real_user_four_scale_acceptance",
        "status": status,
        "core_acceptance_ok": core_ok,
        "automated_acceptance_complete": core_ok,
        "real_device_acceptance_complete": False,
        "acceptance_complete": False,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "base_url": base_url.rstrip("/"),
        "execution_id": execution_id,
        "benchmark_kind": "capability",
        "run_mode": "mixed",
        "auto_scale_run_mode": "auto",
        "mode_capabilities": list(_MODE_CAPABILITIES),
        "scales": list(PROJECT_SCALE_TIERS),
        "route_intents": list(_MODE_CAPABILITIES),
        "auto_scale_case_count": len(PROJECT_SCALE_TIERS),
        "mode_capability_case_count": len(PROJECT_SCALE_TIERS) * len(_MODE_CAPABILITIES),
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
        "pending_case_count": sum(
            1 for case in cases if case.get("status") == "pending_real_device"
        ),
        "dynamic_web_preview": _dynamic_preview_pending(),
        "real_device_acceptance": {
            "status": "pending_real_device",
            "counted_as_complete": False,
            "required_evidence": [
                "desktop_browser_interaction",
                "mobile_browser_interaction",
            ],
        },
        "success_policy": {
            "admin_internal_run_data_allowed": False,
            "fixture_results_allowed": False,
            "public_files_and_zip_required": True,
            "public_preview_lifecycle_required": True,
            "generated_project_build_and_tests_required": True,
            "pending_preview_counts_as_complete": False,
        },
        "blocked_admin_run_requests": list(client.blocked_admin_run_requests),
        "cases": cases,
    }


def _safe_identifier(value: str) -> str:
    normalized = _SAFE_ID_RE.sub("-", value.strip().casefold()).strip("-")
    return normalized or uuid4().hex[:12]


def _is_explicit_preview_not_found(error: Exception) -> bool:
    message = str(error).casefold()
    return "status=404" in message or "http 404" in message


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


def _validated_device_result(
    evidence: Mapping[str, object],
    key: str,
) -> dict[str, object]:
    result = evidence.get(key)
    if not isinstance(result, Mapping) or result.get("passed") is not True:
        raise ValueError(f"{key} must be a passed evidence object")
    if _timestamp(result.get("observed_at")) is None:
        raise ValueError(f"{key}.observed_at must be timezone-aware")
    viewport = result.get("viewport")
    if not isinstance(viewport, Mapping):
        raise TypeError(f"{key}.viewport is required")
    width = viewport.get("width")
    height = viewport.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or height <= 0:
        raise ValueError(f"{key}.viewport must contain positive integer dimensions")
    if key.startswith("desktop") and width < 1024:
        raise ValueError("desktop_browser_interaction viewport must be at least 1024px wide")
    if key.startswith("mobile") and (width <= 0 or width > 600):
        raise ValueError("mobile_browser_interaction viewport must be at most 600px wide")
    checks = result.get("checks")
    required_checks = (
        "login",
        "project_navigation",
        "preview_rendered",
        "preview_interaction",
        "preview_revoked",
    )
    if not isinstance(checks, Mapping) or any(checks.get(name) is not True for name in required_checks):
        raise ValueError(f"{key}.checks must pass every required real-user interaction")
    return copy.deepcopy(dict(result))


def _case_scope_value(
    case: Mapping[str, object],
    section: str,
    key: str,
) -> str:
    value = case.get(section)
    if not isinstance(value, Mapping):
        raise TypeError(f"case evidence scope is missing {section}.{key}")
    scoped = value.get(key)
    if not isinstance(scoped, str) or not scoped:
        raise ValueError(f"case evidence scope is missing {section}.{key}")
    return scoped


def _validated_case_device_result(
    result: object,
    *,
    case_id: str,
    device: str,
) -> dict[str, object]:
    label = f"case evidence {case_id}.{device}"
    if not isinstance(result, Mapping) or result.get("passed") is not True:
        raise ValueError(f"{label} must be a passed evidence object")
    if _timestamp(result.get("observed_at")) is None:
        raise ValueError(f"{label}.observed_at must be timezone-aware")
    evidence_ref = result.get("evidence_ref")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError(f"{label}.evidence_ref is required")
    checks = result.get("checks")
    required_checks = ("preview_rendered", "preview_interaction", "preview_revoked")
    if not isinstance(checks, Mapping) or any(checks.get(name) is not True for name in required_checks):
        raise ValueError(f"{label}.checks must pass every required preview interaction")
    return copy.deepcopy(dict(result))


def _validated_case_evidence(
    automated_report: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    raw_cases = automated_report.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("automated report must contain case evidence scopes")
    raw_evidence = evidence.get("cases")
    if raw_evidence is None:
        raise ValueError("real-device case evidence is required")
    if not isinstance(raw_evidence, Mapping):
        raise TypeError("real-device case evidence must be an object")
    validated: dict[str, dict[str, object]] = {}
    for case in raw_cases:
        if not isinstance(case, Mapping) or case.get("core_acceptance_ok") is not True:
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("automated report case evidence is missing case_id")
        item = raw_evidence.get(case_id)
        if item is None:
            raise ValueError(f"case evidence is missing for {case_id}")
        if not isinstance(item, Mapping):
            raise TypeError(f"case evidence must be an object for {case_id}")
        expected_scope = {
            "project_id": _case_scope_value(case, "project", "project_id"),
            "conversation_id": _case_scope_value(case, "conversation", "conversation_id"),
            "run_id": _case_scope_value(case, "run", "run_id"),
        }
        for key, expected in expected_scope.items():
            if item.get(key) != expected:
                raise ValueError(f"case evidence {case_id}.{key} does not match the report")
        validated[case_id] = {
            **expected_scope,
            "desktop": _validated_case_device_result(
                item.get("desktop"),
                case_id=case_id,
                device="desktop",
            ),
            "mobile": _validated_case_device_result(
                item.get("mobile"),
                case_id=case_id,
                device="mobile",
            ),
        }
    if not validated:
        raise ValueError("real-device case evidence is empty")
    return validated


def finalize_real_device_acceptance(
    automated_report: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, object]:
    """Merge deployed desktop/mobile evidence into a completed acceptance report."""

    if automated_report.get("kind") != "real_user_four_scale_acceptance":
        raise ValueError("automated report kind is invalid")
    if (
        automated_report.get("core_acceptance_ok") is not True
        or automated_report.get("automated_acceptance_complete") is not True
    ):
        raise ValueError("automated acceptance must pass before real-device finalization")
    execution_id = automated_report.get("execution_id")
    if not isinstance(execution_id, str) or evidence.get("execution_id") != execution_id:
        raise ValueError("real-device evidence execution_id does not match the automated report")
    desktop = _validated_device_result(evidence, "desktop_browser_interaction")
    mobile = _validated_device_result(evidence, "mobile_browser_interaction")
    case_evidence = _validated_case_evidence(automated_report, evidence)

    completed = copy.deepcopy(dict(automated_report))
    completed["status"] = "passed"
    completed["real_device_acceptance_complete"] = True
    completed["acceptance_complete"] = True
    completed["real_device_acceptance"] = {
        "status": "passed",
        "counted_as_complete": True,
        "desktop_browser_interaction": desktop,
        "mobile_browser_interaction": mobile,
        "cases": case_evidence,
    }
    dynamic_preview = completed.get("dynamic_web_preview")
    if isinstance(dynamic_preview, dict):
        dynamic_preview["status"] = "passed"
        dynamic_preview["counted_as_passed"] = True
    cases = completed.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict) or case.get("core_acceptance_ok") is not True:
                continue
            case["status"] = "passed"
            case["real_device_acceptance_complete"] = True
            case["acceptance_complete"] = True
            case_id = case.get("case_id")
            if isinstance(case_id, str):
                case["real_device_evidence"] = case_evidence[case_id]
            preview = case.get("dynamic_web_preview")
            if isinstance(preview, dict):
                preview["browser_interaction"] = (
                    "verified_by_deployed_real_device_acceptance"
                )
    return completed


def _read_json_mapping(path: str) -> dict[str, object]:
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError(f"JSON file must contain an object: {path}")
    return cast(dict[str, object], parsed)


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
    parser.add_argument("--finalize-report")
    parser.add_argument("--real-device-evidence")
    args = parser.parse_args(argv)

    if args.finalize_report or args.real_device_evidence:
        if not args.finalize_report or not args.real_device_evidence:
            parser.error("--finalize-report and --real-device-evidence must be used together")
        try:
            payload = finalize_real_device_acceptance(
                _read_json_mapping(args.finalize_report),
                _read_json_mapping(args.real_device_evidence),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            payload = {
                "schema_version": 1,
                "kind": "real_user_four_scale_acceptance",
                "status": "failed",
                "acceptance_complete": False,
                "errors": [str(error)],
            }
        _write_report(args.output, payload)
        return 0 if payload.get("acceptance_complete") is True else 1

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
            "automated_acceptance_complete": False,
            "real_device_acceptance_complete": False,
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
