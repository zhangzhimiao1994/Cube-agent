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
import tempfile
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
    AcceptanceHTTPError,
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
        file_items = [
            cast(Mapping[str, object], item) for item in raw_items if isinstance(item, Mapping)
        ]
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
            renewed_payload.get("id") == preview_id and renewed_payload.get("status") == "ready"
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
    final_mode = result.final_observed_mode or result.observed_mode
    route_observed_mode = result.observed_mode if route_intent == "auto" else final_mode
    exact_mode_coverage_ok = allowed_modes is None or route_observed_mode in allowed_modes
    safe_upgrade = (
        route_intent == "direct"
        and result.requested_mode == "direct"
        and final_mode == "hybrid"
        and result.route_reason == "project_scale_mode_upgrade"
        and result.mode_source == "project_scale_assessment"
        and result.effective_scale in {"large", "ultra"}
    )
    route_policy_ok = exact_mode_coverage_ok or safe_upgrade
    effective_scale = result.effective_scale
    scale_fidelity_ok = effective_scale == scale
    artifact_origin_ok = result.artifact_origin in {
        "model_workspace_bundle",
        "tool_workspace_write",
        "incremental_workspace_delivery",
    }
    required_multi_agent_ids = {"architect", "implementer", "tester", "synthesizer"}
    multi_agent_evidence_ok = route_intent != "multi_agent" or (
        result.evidence.get("multi_agent_participation") is True
        and required_multi_agent_ids <= set(result.participant_agent_ids)
        and result.participant_event_count >= 8
    )
    core_ok = (
        result.status == "completed"
        and bool(result.run_id)
        and result.ok
        and generated_project_ok
        and requirements_ok
        and public_artifacts_ok
        and preview_ok
        and route_policy_ok
        and scale_fidelity_ok
        and artifact_origin_ok
        and multi_agent_evidence_ok
    )
    return {
        "case_id": result.case_id,
        "case_kind": case_kind,
        "scale": scale,
        "route_intent": route_intent,
        "observed_mode": result.observed_mode,
        "initial_observed_mode": result.observed_mode,
        "final_observed_mode": final_mode,
        "route_observed_mode": route_observed_mode,
        "requested_mode": result.requested_mode,
        "route_reason": result.route_reason,
        "mode_source": result.mode_source,
        "effective_scale": effective_scale,
        "scale_fidelity_ok": scale_fidelity_ok,
        "route_policy_ok": route_policy_ok,
        "observed_route_ok": route_policy_ok,
        "exact_mode_coverage_ok": exact_mode_coverage_ok,
        "coverage_credit": (
            "exact_mode" if exact_mode_coverage_ok else "safe_upgrade" if safe_upgrade else "none"
        ),
        "artifact_origin_ok": artifact_origin_ok,
        "autonomous_mode_selected": case_kind == "auto_scale" and route_policy_ok,
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
        "artifact_provenance": {
            "artifact_origin": result.artifact_origin,
            "embedded_bundle_available": result.workspace_bundle_source == "embedded_bundle",
            "public_materialized": public_artifacts_ok,
            "preview_available": preview_ok,
            "fixture_origin_allowed": False,
        },
        "success_basis": {
            "logged_in_user_http_api": True,
            "public_run_api": True,
            "public_workspace_file_api": public_artifacts_ok,
            "public_workspace_zip": public_artifacts_ok,
            "public_preview_lifecycle": preview_ok,
            "observed_route": route_policy_ok,
            "exact_mode_coverage": exact_mode_coverage_ok,
            "scale_fidelity": scale_fidelity_ok,
            "artifact_origin": artifact_origin_ok,
            "multi_agent_participation": multi_agent_evidence_ok,
            "admin_internal_run_data": False,
        },
    }


def _create_or_recover_case_resource(
    client: RealUserAcceptanceClient,
    *,
    path: str,
    body: dict[str, object],
    idempotency_key: str,
) -> dict[str, object]:
    is_project = path == "/api/v1/admin/project-workspaces"
    expected_code = "project_workspace_conflict" if is_project else "conversation_conflict"
    try:
        resource = client.request_json("POST", path, body=body, idempotency_key=idempotency_key)
    except AcceptanceHTTPError as error:
        if error.status_code != 409 or error.method != "POST" or error.path != path:
            raise
        try:
            payload = json.loads(error.response_body)
        except json.JSONDecodeError:
            raise error from None
        details = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(details, dict) or details.get("code") != expected_code:
            raise
        # These authenticated reads enforce tenant ownership; the API exposes no user owner field.
        if is_project:
            existing = client.request_json("GET", path)
            if not isinstance(existing, list):
                raise TypeError("project resource scope lookup returned non-list JSON") from error
            matches = [
                item
                for item in existing
                if isinstance(item, dict) and item.get("project_id") == body["project_id"]
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "project resource scope is not uniquely visible to this actor"
                ) from error
            resource = matches[0]
        else:
            resource = client.request_json(
                "GET",
                f"{path}/{quote(str(body['conversation_id']), safe='')}",
            )
    if not isinstance(resource, dict):
        raise TypeError("case resource scope lookup returned non-object JSON")
    for key, expected in body.items():
        if resource.get(key) != expected:
            raise RuntimeError(f"case resource scope mismatch: {key}")
    if not is_project and resource.get("archived_at") is not None:
        raise RuntimeError("case resource scope mismatch: conversation is archived")
    return resource


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
    authentication_method: str = "password",
    output_path: str | None = None,
    resume_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    started_at = _utc_now()
    principal = client.request_json("GET", "/api/v1/auth/me")
    if not isinstance(principal, dict):
        raise TypeError("GET /api/v1/auth/me returned non-object JSON")
    cases: list[dict[str, object]] = []
    attempt_history: list[dict[str, object]] = []
    safe_execution_id = _safe_identifier(execution_id)
    identity = _execution_identity(execution_id, base_url, principal)
    if resume_report is not None:
        cases = _resume_cases(resume_report, identity, attempt_history)
        device = resume_report.get("real_device_acceptance")
        if (
            resume_report.get("status") == "passed"
            or resume_report.get("acceptance_complete") is True
            or resume_report.get("real_device_acceptance_complete") is True
            or isinstance(device, Mapping)
            and (device.get("status") == "passed" or device.get("counted_as_complete") is True)
        ):
            return _validated_finalized_report(resume_report)
        previous_start = resume_report.get("started_at")
        if isinstance(previous_start, str) and _timestamp(previous_start) is not None:
            started_at = previous_start

    def snapshot(*, finished: bool = False) -> dict[str, object]:
        return _matrix_report(
            client=client,
            cases=cases,
            attempt_history=attempt_history,
            username=username,
            principal=principal,
            base_url=base_url,
            execution_id=execution_id,
            identity=identity,
            started_at=started_at,
            authentication_method=authentication_method,
            finished=finished,
        )

    if output_path:
        _save_report(output_path, snapshot())

    for case_kind, scale, route_intent, case_key in _ACCEPTANCE_CASES:
        case_id = f"{scale}:{route_intent}"
        previous = next((case for case in cases if case.get("case_id") == case_id), None)
        if previous is not None and _has_complete_core_evidence(
            previous, safe_execution_id=safe_execution_id, case_key=case_key
        ):
            if progress is not None:
                progress(f"{case_kind}/{scale}/{route_intent}: resuming completed core evidence")
            continue
        attempt = _case_attempt(previous) + 1 if previous is not None else 1
        scope_token = _case_execution_token(safe_execution_id, case_key, attempt)
        project_id = _bounded_identifier(f"uat-{scope_token}", 128)
        conversation_id = _bounded_identifier(
            f"conv-{scope_token}",
            128,
        )
        project_label = (
            f"真实用户 {scale} AUTO 规模验收"
            if case_kind == "auto_scale"
            else f"真实用户 {route_intent} 模式能力验收"
        )
        runner_execution_id = scope_token
        workspace_session_id = _safe_workspace_session_token(
            conversation_id,
            runner_execution_id,
        )
        if progress is not None:
            progress(f"{case_kind}/{scale}/{route_intent}: creating project and conversation")
        try:
            project = _create_or_recover_case_resource(
                client,
                path="/api/v1/admin/project-workspaces",
                body={
                    "project_id": project_id,
                    "label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{scope_token}-project",
            )
            if not isinstance(project, dict) or project.get("project_id") != project_id:
                raise RuntimeError("project workspace creation returned the wrong project")
            conversation = _create_or_recover_case_resource(
                client,
                path="/api/v1/admin/conversations",
                body={
                    "conversation_id": conversation_id,
                    "title": f"{case_kind} {scale} {route_intent} 真实用户项目验收",
                    "project_id": project_id,
                    "project_label": project_label,
                    "workspace_path": workspace_session_id,
                },
                idempotency_key=f"{scope_token}-conversation",
            )
            if (
                not isinstance(conversation, dict)
                or conversation.get("conversation_id") != conversation_id
            ):
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
                auto_approve_capability_requests=True,
            )
            result = runner_report.results[0]
            if result.case_id != case_id:
                raise RuntimeError("capability runner returned evidence for the wrong case")
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
            completed_case = build_case_report(
                scale=scale,
                project=project,
                conversation=conversation,
                result=result,
                public_artifacts=public_artifacts,
                dynamic_web_preview=dynamic_web_preview,
            )
        except Exception as error:  # noqa: BLE001 - every matrix case must be attempted.
            completed_case = {
                "case_id": case_id,
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
        completed_case["attempt"] = attempt
        if previous is not None:
            cases[cases.index(previous)] = completed_case
        else:
            cases.append(completed_case)
        # Saving is outside the case exception handler: a failed checkpoint must stop execution.
        if output_path:
            _save_report(output_path, snapshot())

    payload = snapshot(finished=True)
    if output_path:
        _save_report(output_path, payload)
    return payload


def _matrix_report(
    *,
    client: RealUserAcceptanceClient,
    cases: list[dict[str, object]],
    attempt_history: list[dict[str, object]],
    username: str,
    principal: Mapping[str, object],
    base_url: str,
    execution_id: str,
    identity: Mapping[str, object],
    started_at: str,
    authentication_method: str,
    finished: bool,
) -> dict[str, object]:

    expected_case_count = len(_ACCEPTANCE_CASES)
    core_ok = (
        finished
        and len(cases) == expected_case_count
        and all(case.get("core_acceptance_ok") is True for case in cases)
    )
    status = "pending_real_device" if core_ok else "failed" if finished else "in_progress"
    return {
        "schema_version": 1,
        "kind": "real_user_four_scale_acceptance",
        "status": status,
        "core_acceptance_ok": core_ok,
        "automated_acceptance_complete": core_ok,
        "real_device_acceptance_complete": False,
        "acceptance_complete": False,
        "started_at": started_at,
        "finished_at": _utc_now() if finished else None,
        "base_url": base_url.rstrip("/"),
        "execution_id": execution_id,
        "execution_identity": dict(identity),
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
            "authentication_method": authentication_method,
            "authenticated_via_password_environment": authentication_method == "password",
            "authenticated_via_bearer_environment": authentication_method == "bearer_token",
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
        "cases": copy.deepcopy(cases),
        "attempt_history": copy.deepcopy(attempt_history),
    }


def _execution_identity(
    execution_id: str, base_url: str, principal: Mapping[str, object]
) -> dict[str, object]:
    identity: dict[str, object] = {
        "execution_id": execution_id,
        "base_url": base_url.rstrip("/"),
    }
    for key in ("user_id", "tenant_id"):
        value = principal.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"execution identity requires authenticated {key}")
        identity[key] = value
    if not execution_id.strip() or not _SAFE_ID_RE.sub("-", execution_id.casefold()).strip("-"):
        raise ValueError("execution identity requires a usable execution_id")
    return identity


def _resume_cases(
    report: Mapping[str, object],
    identity: Mapping[str, object],
    attempt_history: list[dict[str, object]],
) -> list[dict[str, object]]:
    if (
        report.get("kind") != "real_user_four_scale_acceptance"
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("benchmark_kind") != "capability"
    ):
        raise ValueError("resume report must be a supported capability acceptance report")
    actor = report.get("actor")
    principal = actor.get("principal") if isinstance(actor, Mapping) else None
    execution_id, base_url = report.get("execution_id"), report.get("base_url")
    if (
        not isinstance(principal, Mapping)
        or not isinstance(execution_id, str)
        or not isinstance(base_url, str)
    ):
        raise TypeError("resume report execution identity is incomplete")
    saved_identity = _execution_identity(execution_id, base_url, principal)
    if saved_identity != identity or (
        "execution_identity" in report and report["execution_identity"] != identity
    ):
        raise ValueError("resume report execution identity does not match this execution")
    raw_cases = report.get("cases")
    if not isinstance(raw_cases, list):
        raise TypeError("resume report cases must be a list")
    by_id: dict[str, dict[str, object]] = {}
    expected_ids = {f"{scale}:{route}" for _, scale, route, _ in _ACCEPTANCE_CASES}
    raw_history = report.get("attempt_history", [])
    if not isinstance(raw_history, list):
        raise TypeError("resume attempt_history must be a list")
    archived_attempts: set[tuple[str, int]] = set()
    for item in raw_history:
        if not isinstance(item, dict) or "attempt_history" in item or "cases" in item:
            raise ValueError("resume attempt_history must contain flat case evidence")
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or case_id not in expected_ids:
            raise ValueError("resume attempt_history contains an unknown case_id")
        key = (case_id, _case_attempt(item))
        if key in archived_attempts:
            raise ValueError("resume attempt_history contains a duplicate attempt")
        archived_attempts.add(key)
        attempt_history.append(copy.deepcopy(item))
    for case in raw_cases:
        if not isinstance(case, dict):
            raise TypeError("resume report contains a non-object case")
        case_id = case.get("case_id")
        # Older exception reports have no case_id; derive only their canonical matrix key.
        if case_id is None:
            case_id = f"{case.get('scale')}:{case.get('route_intent')}"
        if not isinstance(case_id, str) or case_id not in expected_ids:
            raise ValueError("resume report contains an unknown case_id")
        if case_id in by_id:
            raise ValueError(f"resume report contains duplicate case_id: {case_id}")
        by_id[case_id] = copy.deepcopy({**case, "case_id": case_id})
    cases = []
    for _, scale, route, case_key in _ACCEPTANCE_CASES:
        case = by_id.get(f"{scale}:{route}")
        if case is None:
            continue
        if not _has_complete_core_evidence(
            case, safe_execution_id=_safe_identifier(execution_id), case_key=case_key
        ):
            # Archive before normalizing flags or saving the pre-retry checkpoint.
            key = (cast(str, case["case_id"]), _case_attempt(case))
            if key not in archived_attempts:
                attempt_history.append(copy.deepcopy(case))
                archived_attempts.add(key)
            case.update(
                {
                    "status": "failed",
                    "core_acceptance_ok": False,
                    "automated_acceptance_complete": False,
                    "real_device_acceptance_complete": False,
                    "acceptance_complete": False,
                }
            )
        cases.append(case)
    return cases


def _case_attempt(case: Mapping[str, object] | None) -> int:
    attempt = case.get("attempt", 1) if case is not None else 1
    if type(attempt) is not int or attempt < 1:
        raise ValueError("resume case attempt must be a positive integer")
    return attempt


def _case_execution_token(execution_id: str, case_key: str, attempt: int) -> str:
    if attempt == 1:
        return f"{execution_id}-{case_key}"
    # Keep retry identity within the runner's 64-character workspace limit.
    digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]
    return f"{case_key}-retry-{attempt}-{digest}"


def _has_complete_core_evidence(
    case: Mapping[str, object], *, safe_execution_id: str, case_key: str
) -> bool:
    if (
        case.get("core_acceptance_ok") is not True
        or case.get("automated_acceptance_complete") is not True
        or case.get("status") not in ("passed", "pending_real_device")
        or case.get("errors", []) != []
    ):
        return False
    sections = ("run", "project", "conversation", "public_artifacts", "dynamic_web_preview")
    if any(not isinstance(case.get(key), Mapping) for key in sections):
        return False
    run, project, conversation, public, preview = (
        cast(Mapping[str, object], case[key]) for key in sections
    )
    case_id, scale = case.get("case_id"), case.get("scale")
    if (
        not isinstance(case_id, str)
        or not isinstance(scale, str)
        or case_id != f"{scale}:{case.get('route_intent')}"
    ):
        return False
    attempt = _case_attempt(case)
    scope_token = _case_execution_token(safe_execution_id, case_key, attempt)
    project_id = _bounded_identifier(f"uat-{scope_token}", 128)
    conversation_id = _bounded_identifier(f"conv-{scope_token}", 128)
    session_id = _safe_workspace_session_token(conversation_id, scope_token)
    root = (
        f"/api/v1/workspaces/projects/{quote(project_id, safe='')}"
        f"/sessions/{quote(session_id, safe='')}"
    )
    if (
        project.get("project_id") != project_id
        or conversation.get("conversation_id") != conversation_id
        or project.get("workspace_path") != session_id
        or conversation.get("workspace_path") != session_id
        or conversation.get("project_id") != project_id
        or public.get("files_endpoint") != f"{root}/files"
        or public.get("bundle_endpoint") != f"{root}/bundle/download"
        or run.get("case_id") != case_id
        or run.get("status") != "completed"
        or not isinstance(run.get("run_id"), str)
        or not str(run["run_id"]).strip()
        or run.get("ok") is not True
        or run.get("errors") != []
        or run.get("missing_evidence") != []
    ):
        return False
    evidence = run.get("evidence")
    if not isinstance(evidence, dict) or any(
        type(value) is not bool for value in evidence.values()
    ):
        return False
    string_fields = (
        "observed_mode",
        "final_observed_mode",
        "requested_mode",
        "route_reason",
        "mode_source",
        "effective_scale",
        "artifact_origin",
        "workspace_bundle_source",
    )
    if any(run.get(key) is not None and not isinstance(run.get(key), str) for key in string_fields):
        return False
    if run.get("final_observed_mode") not in ("direct", "dispatch", "hybrid"):
        return False
    participants, event_kinds = run.get("participant_agent_ids"), run.get("participant_event_kinds")
    event_count = run.get("participant_event_count")
    if (
        not isinstance(participants, list)
        or any(not isinstance(item, str) for item in participants)
        or not isinstance(event_kinds, list)
        or any(not isinstance(item, str) for item in event_kinds)
        or type(event_count) is not int
        or event_count < 0
    ):
        return False
    result = ProjectScaleCaseResult(
        case_id=case_id,
        run_id=cast(str, run["run_id"]),
        status="completed",
        evidence=cast(dict[str, bool], evidence),
        observed_mode=cast(str | None, run.get("observed_mode")),
        final_observed_mode=cast(str | None, run.get("final_observed_mode")),
        requested_mode=cast(str | None, run.get("requested_mode")),
        route_reason=cast(str | None, run.get("route_reason")),
        mode_source=cast(str | None, run.get("mode_source")),
        effective_scale=cast(str | None, run.get("effective_scale")),
        artifact_origin=cast(str | None, run.get("artifact_origin")),
        workspace_bundle_source=cast(str | None, run.get("workspace_bundle_source")),
        participant_agent_ids=tuple(cast(list[str], participants)),
        participant_event_kinds=tuple(cast(list[str], event_kinds)),
        participant_event_count=event_count,
    )
    if not result.ok or run.get("required_evidence") != list(result.required_evidence):
        return False
    if (
        public.get("ok") is not True
        or public.get("source") != "public_workspace_api"
        or public.get("admin_internal_run_data_used") is not False
        or public.get("zip_crc_ok") is not True
        or public.get("metadata_matches_zip") is not True
        or type(public.get("unsafe_member_count")) is not int
        or public.get("unsafe_member_count") != 0
        or public.get("errors") != []
        or not isinstance(public.get("zip_sha256"), str)
        or _SHA256_RE.fullmatch(cast(str, public["zip_sha256"])) is None
    ):
        return False
    for key in ("file_count", "downloaded_file_count", "zip_member_count", "zip_size_bytes"):
        value = public.get(key)
        if type(value) is not int or value <= 0:
            return False
    if not public["file_count"] == public["downloaded_file_count"] == public["zip_member_count"]:
        return False
    if (
        preview.get("status") != "passed"
        or preview.get("errors") != []
        or any(
            preview.get(key) is not True
            for key in (
                "counted_as_passed",
                "reachable_preview_url",
                "current_preview_matches",
                "renewed",
                "lease_extended",
                "stopped",
                "revoked_after_stop",
            )
        )
        or preview.get("capability_token_retained") is not False
    ):
        return False
    for key in ("referenced_asset_count", "referenced_assets_loaded", "content_size_bytes"):
        value = preview.get(key)
        if type(value) is not int or value < (1 if key == "content_size_bytes" else 0):
            return False
    if preview["referenced_asset_count"] != preview["referenced_assets_loaded"]:
        return False
    rebuilt = build_case_report(
        scale=scale,
        project=project,
        conversation=conversation,
        result=result,
        public_artifacts=public,
        dynamic_web_preview=preview,
    )
    compared_fields = (
        "case_kind",
        "route_intent",
        "scale_fidelity_ok",
        "route_policy_ok",
        "artifact_origin_ok",
        "multi_agent_evidence_ok",
        "build_and_test",
        "success_basis",
        "artifact_provenance",
        "observed_mode",
        "initial_observed_mode",
        "final_observed_mode",
        "route_observed_mode",
        "requested_mode",
        "route_reason",
        "mode_source",
        "effective_scale",
        "observed_route_ok",
        "exact_mode_coverage_ok",
        "coverage_credit",
    )
    return rebuilt["core_acceptance_ok"] is True and json.dumps(
        {key: case.get(key) for key in compared_fields}, sort_keys=True
    ) == json.dumps({key: rebuilt[key] for key in compared_fields}, sort_keys=True)


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


def _save_report(path: str, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_report(path: str | None, payload: Mapping[str, object]) -> None:
    if path:
        _save_report(path, payload)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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
    if not isinstance(checks, Mapping) or any(
        checks.get(name) is not True for name in required_checks
    ):
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
    if not isinstance(scoped, str) or not scoped.strip():
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
    if not isinstance(checks, Mapping) or any(
        checks.get(name) is not True for name in required_checks
    ):
        raise ValueError(f"{label}.checks must pass every required preview interaction")
    return copy.deepcopy(dict(result))


def _validated_case_evidence(
    automated_report: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    raw_cases = automated_report.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("automated report must contain case evidence scopes")
    case_keys = {f"{scale}:{route}": key for _, scale, route, key in _ACCEPTANCE_CASES}
    expected_ids = set(case_keys)
    execution_id = automated_report.get("execution_id")
    if not isinstance(execution_id, str):
        raise TypeError("automated report core evidence requires execution_id")
    case_ids: set[str] = set()
    for case in raw_cases:
        if not isinstance(case, Mapping):
            raise TypeError("automated report case evidence must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id not in expected_ids:
            raise ValueError("automated report case evidence has an invalid canonical case_id")
        if case_id in case_ids:
            raise ValueError(f"automated report case evidence has duplicate case_id: {case_id}")
        if not _has_complete_core_evidence(
            case, safe_execution_id=_safe_identifier(execution_id), case_key=case_keys[case_id]
        ):
            raise ValueError(f"automated report case evidence must pass core acceptance: {case_id}")
        case_ids.add(case_id)
    if case_ids != expected_ids:
        raise ValueError(
            "automated report case evidence must contain every canonical case exactly once"
        )
    raw_evidence = evidence.get("cases")
    if raw_evidence is None:
        raise ValueError("real-device case evidence is required")
    if not isinstance(raw_evidence, Mapping):
        raise TypeError("real-device case evidence must be an object")
    if set(raw_evidence) != expected_ids:
        raise ValueError("real-device case evidence must match the canonical case set exactly")
    validated: dict[str, dict[str, object]] = {}
    for case in raw_cases:
        case_id = cast(str, case["case_id"])
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
    return validated


def finalize_real_device_acceptance(
    automated_report: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, object]:
    """Merge deployed desktop/mobile evidence into a completed acceptance report."""

    if (
        automated_report.get("kind") != "real_user_four_scale_acceptance"
        or type(automated_report.get("schema_version")) is not int
        or automated_report.get("schema_version") != 1
        or automated_report.get("benchmark_kind") != "capability"
    ):
        raise ValueError("automated report must be a supported capability acceptance report")
    if automated_report.get("errors", []) != []:
        raise ValueError("automated report errors must be empty before finalization")
    if (
        automated_report.get("core_acceptance_ok") is not True
        or automated_report.get("automated_acceptance_complete") is not True
    ):
        raise ValueError("automated acceptance must pass before real-device finalization")
    execution_id = automated_report.get("execution_id")
    if not isinstance(execution_id, str) or evidence.get("execution_id") != execution_id:
        raise ValueError("real-device evidence execution_id does not match the automated report")
    actor, base_url = automated_report.get("actor"), automated_report.get("base_url")
    principal = actor.get("principal") if isinstance(actor, Mapping) else None
    if not isinstance(principal, Mapping) or not isinstance(base_url, str):
        raise TypeError("automated report execution identity is incomplete")
    identity = _execution_identity(execution_id, base_url, principal)
    if (
        "execution_identity" in automated_report
        and automated_report["execution_identity"] != identity
    ):
        raise ValueError("automated report execution identity is inconsistent")
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
                preview["browser_interaction"] = "verified_by_deployed_real_device_acceptance"
    return completed


def _validated_finalized_report(report: Mapping[str, object]) -> dict[str, object]:
    device = report.get("real_device_acceptance")
    if not isinstance(device, Mapping) or report.get("errors", []) != []:
        raise ValueError("finalized report requires complete real-device evidence")
    rebuilt = finalize_real_device_acceptance(
        report, {**device, "execution_id": report.get("execution_id")}
    )
    if json.dumps(rebuilt, sort_keys=True) != json.dumps(dict(report), sort_keys=True):
        raise ValueError("finalized report contradicts its complete acceptance evidence")
    return copy.deepcopy(dict(report))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_mapping(path: str) -> dict[str, object]:
    parsed = json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
    )
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
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID"),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_REPORT_PATH"),
    )
    parser.add_argument("--finalize-report")
    parser.add_argument("--real-device-evidence")
    parser.add_argument(
        "--resume-report",
        "--resume",
        dest="resume_report",
        help="Resume matching core evidence and retry failed/incomplete cases from this JSON report.",
    )
    args = parser.parse_args(argv)

    if args.finalize_report or args.real_device_evidence:
        if args.resume_report:
            parser.error("--resume-report cannot be combined with real-device finalization")
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

    resume_report = None
    if args.resume_report:
        try:
            resume_report = _read_json_mapping(args.resume_report)
        except (OSError, TypeError, ValueError) as error:
            parser.error(f"cannot read resume report: {error}")
        saved_execution_id = resume_report.get("execution_id")
        if not isinstance(saved_execution_id, str) or not saved_execution_id.strip():
            parser.error("resume report execution identity requires execution_id")
        if args.execution_id is not None and args.execution_id != saved_execution_id:
            parser.error("resume report execution identity does not match --execution-id")
        args.execution_id = saved_execution_id
        args.output = args.output or args.resume_report
    args.execution_id = args.execution_id or _default_execution_id()

    username, password, tenant_id = _acceptance_credentials_from_env()
    bearer_token = os.environ.get("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "").strip()
    if not bearer_token and (not username or not password):
        parser.error(
            "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or "
            "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required "
            "(AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME/PASSWORD is also accepted)"
        )
    login_username = None if bearer_token else username
    login_password = None if bearer_token else password

    delegate = UrllibAcceptanceClient(
        base_url=args.base_url,
        bearer_token=bearer_token,
        timeout=args.timeout,
        username=login_username,
        password=login_password,
        tenant_id=tenant_id,
    )
    client = RealUserAcceptanceClient(delegate)
    try:
        payload = run_real_user_four_scale_acceptance(
            client,
            username=username or "bearer-token",
            base_url=args.base_url,
            execution_id=args.execution_id,
            wait_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_interval,
            artifact_build_timeout_seconds=args.artifact_build_timeout,
            authentication_method="bearer_token" if bearer_token else "password",
            output_path=args.output,
            resume_report=resume_report,
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
        # Authentication, identity, and save errors must not overwrite durable case evidence.
        if args.output and Path(args.output).exists():
            print(f"preserved checkpoint: {args.output}", file=sys.stderr, flush=True)
            _write_report(None, payload)
            return 1
    finalized_resume = resume_report is not None and payload.get("acceptance_complete") is True
    _write_report(None if finalized_resume else args.output, payload)
    if payload.get("status") == "failed":
        return 1
    if payload.get("acceptance_complete") is not True:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
