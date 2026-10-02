#!/usr/bin/env python3
"""Exercise a signed executable plugin through a logged-in public run."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast
from urllib.parse import quote, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ed25519

from agent_hub.harness.project_scale_runner import (
    AcceptanceHTTPError,
    UrllibAcceptanceClient,
    _acceptance_credentials_from_env,
)
from agent_hub.plugins.package_builder import build_signed_plugin_archive

_ADMIN_RUN_PREFIX = "/api/v1/admin/runs"
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_SIDE_EFFECT_FREE_RUN_SUBMISSION_REJECTIONS = frozenset(
    {
        (401, "invalid_token"),
        (403, "permission_denied"),
        (409, "vibe_coding_disabled"),
        (409, "execution_backend_unavailable"),
        (409, "vibe_coding_unavailable"),
        (409, "conversation_archived"),
        (422, "request_validation"),
    }
)


class PluginAcceptanceClient(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]: ...

    def request_archive(
        self,
        method: str,
        path: str,
        *,
        archive: bytes,
        filename: str,
    ) -> dict[str, object]: ...


class UrllibPluginAcceptanceClient(UrllibAcceptanceClient):
    """Add bounded archive upload while forbidding admin-run success evidence."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str = "",
        timeout: float = 20.0,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            bearer_token=bearer_token,
            timeout=timeout,
            username=username,
            password=password,
            tenant_id=tenant_id,
        )
        self.request_log: list[str] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        self._record(method, path)
        normalized = urlsplit(path).path.rstrip("/")
        if method.upper() == "DELETE" and normalized.startswith("/api/v1/users/"):
            raw = self._request(
                method,
                path,
                headers={"Accept": "application/json"},
                data=None,
            )
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict | list):
                raise TypeError(f"{method} {path} returned non-object JSON")
            return parsed
        return super().request_json(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
        )

    def request_archive(
        self,
        method: str,
        path: str,
        *,
        archive: bytes,
        filename: str,
    ) -> dict[str, object]:
        self._record(method, path)
        raw = self._request(
            method,
            path,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/zip",
                "X-Agent-Hub-Plugin-Filename": filename,
            },
            data=archive,
        )
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise TypeError(f"{method} {path} returned non-object JSON")
        return cast(dict[str, object], parsed)

    def _record(self, method: str, path: str) -> None:
        normalized = urlsplit(path).path.rstrip("/")
        if normalized == _ADMIN_RUN_PREFIX or normalized.startswith(f"{_ADMIN_RUN_PREFIX}/"):
            raise RuntimeError(f"{method.upper()} {path} is forbidden as acceptance evidence")
        self.request_log.append(f"{method.upper()} {path}")


@dataclass(frozen=True, slots=True)
class AcceptancePluginPackage:
    plugin_id: str
    capability_id: str
    key_id: str
    runtime_nonce: str
    nonce: str
    input_text: str
    archive_path: Path
    public_key: str


def _safe_suffix(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (normalized or uuid4().hex)[:24].rstrip("-")


def build_acceptance_plugin(package_dir: Path, *, execution_id: str) -> AcceptancePluginPackage:
    """Build a unique signed package whose output schema proves the exact adapter result."""

    execution_suffix = _safe_suffix(execution_id)
    runtime_nonce = uuid4().hex
    suffix = f"{execution_suffix}-{runtime_nonce}"
    plugin_id = f"plugin-uat-{suffix}"
    capability_id = f"acceptance.stats_{suffix.replace('-', '_')}"
    key_id = f"plugin-uat-key-{suffix}"
    nonce = runtime_nonce
    input_text = f"真实插件验收 {execution_suffix}"
    source_dir = package_dir / f"source-{suffix}"
    adapter_dir = source_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": f"真实插件验收 {suffix}",
        "description": "由生产闭环验收临时生成并在结束后卸载。",
        "version": "1.0.0",
        "package": {
            "schema_version": 1,
            "kind": "adapter_package",
            "package_version": "1.0.0",
            "adapter_id": "python_subprocess_v1",
            "sdk_api_version": "1.0",
            "signature": {
                "algorithm": "ed25519",
                "key_id": key_id,
                "value": "A" * 86,
            },
            "runtime": "python",
            "entrypoint": "adapter/main.py",
            "isolation": "local_process",
            "install_mode": "runtime_registered",
            "dependencies": [],
        },
        "capabilities": [
            {
                "id": capability_id,
                "adapter": "python_subprocess_v1",
                "permission_class": "plugin.use",
                "sandbox_profile": "local_process",
                "policy_effect": "allow",
                "replay_safe": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "maxLength": 4096}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean", "const": True},
                        "character_count": {"type": "integer", "minimum": 0},
                        "acceptance_nonce": {"type": "string", "const": nonce},
                    },
                    "required": ["ok", "character_count", "acceptance_nonce"],
                    "additionalProperties": False,
                },
            }
        ],
    }
    (source_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    adapter_source = f'''from __future__ import annotations

import json
import sys

CAPABILITY_ID = {capability_id!r}
ACCEPTANCE_NONCE = {nonce!r}


def main() -> int:
    request = json.load(sys.stdin)
    if request.get("capability_id") != CAPABILITY_ID:
        raise ValueError("unsupported capability")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {{"text"}}:
        raise ValueError("invalid arguments")
    text = arguments.get("text")
    if not isinstance(text, str):
        raise ValueError("invalid text")
    json.dump(
        {{"ok": True, "character_count": len(text), "acceptance_nonce": ACCEPTANCE_NONCE}},
        sys.stdout,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    (adapter_dir / "main.py").write_text(adapter_source, encoding="utf-8")
    private_key = ed25519.Ed25519PrivateKey.generate()
    archive_path = package_dir / f"{plugin_id}.zip"
    built = build_signed_plugin_archive(
        source_dir=source_dir,
        output_path=archive_path,
        private_key=private_key,
        key_id=key_id,
    )
    public_key = base64.urlsafe_b64encode(built.public_key).rstrip(b"=").decode("ascii")
    return AcceptancePluginPackage(
        plugin_id=plugin_id,
        capability_id=capability_id,
        key_id=key_id,
        runtime_nonce=runtime_nonce,
        nonce=nonce,
        input_text=input_text,
        archive_path=archive_path,
        public_key=public_key,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} returned non-object JSON")
    return cast(Mapping[str, object], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} returned non-list JSON")
    return value


def _plugin_from_install(value: object) -> Mapping[str, object]:
    payload = _mapping(value, "plugin install")
    return _mapping(payload.get("plugin"), "plugin install resource")


def _activation_state(plugin: Mapping[str, object]) -> str:
    package = _mapping(plugin.get("package_metadata"), "plugin package metadata")
    value = package.get("activation_state")
    return value if isinstance(value, str) else ""


def _manifest_capability(
    manifest: object,
    capability_id: str,
) -> Mapping[str, object] | None:
    payload = _mapping(manifest, "capability manifest")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list | tuple):
        raise TypeError("capability manifest capabilities are invalid")
    return next(
        (
            cast(Mapping[str, object], item)
            for item in capabilities
            if isinstance(item, Mapping) and item.get("id") == capability_id
        ),
        None,
    )


def _runtime_nonce_from_identifier(identifier: object, separator: str) -> str:
    if not isinstance(identifier, str):
        raise TypeError("runtime evidence identifier is missing")
    prefix, found, runtime_nonce = identifier.rpartition(separator)
    if not found or not prefix or not runtime_nonce:
        raise RuntimeError("runtime evidence identifier does not contain a nonce")
    return runtime_nonce


def verify_public_plugin_invocation(
    *,
    events: object,
    audits: object,
    run_id: str,
    user_id: str,
    plugin_id: str,
    capability_id: str,
    runtime_nonce: str,
) -> dict[str, object]:
    """Require public tool completion and the exact runtime invocation audit."""

    event_payload = _mapping(events, "public run events")
    items = event_payload.get("items")
    if not isinstance(items, list | tuple):
        raise TypeError("public run events items are invalid")
    matching_events = [
        item
        for item in items
        if isinstance(item, Mapping) and item.get("tool_name") == capability_id
    ]
    requested_count = sum(item.get("kind") == "tool.requested" for item in matching_events)
    completed_count = sum(item.get("kind") == "tool.completed" for item in matching_events)
    failed = any(item.get("kind") == "tool.failed" for item in matching_events)
    if requested_count != 1 or completed_count != 1:
        raise RuntimeError("public run did not invoke the expected plugin capability exactly once")
    if failed:
        raise RuntimeError("public run did not complete the expected plugin capability")
    audit_items = _list(audits, "plugin invocation audit")
    correlated_details: Mapping[str, object] | None = None
    for item in audit_items:
        if not isinstance(item, Mapping) or item.get("action") != "plugin.invoke.succeeded":
            continue
        details = item.get("details")
        if not isinstance(details, Mapping):
            continue
        if all(
            details.get(key) == value
            for key, value in {
                "run_id": run_id,
                "user_id": user_id,
                "plugin_id": plugin_id,
                "capability_id": capability_id,
            }.items()
        ):
            correlated_details = details
            break
    if correlated_details is None:
        raise RuntimeError("correlated plugin invocation audit was not found")
    completed_event = next(
        item for item in matching_events if item.get("kind") == "tool.completed"
    )
    observed_nonces = {
        _runtime_nonce_from_identifier(completed_event.get("tool_name"), "_"),
        _runtime_nonce_from_identifier(correlated_details.get("capability_id"), "_"),
        _runtime_nonce_from_identifier(correlated_details.get("plugin_id"), "-"),
    }
    if observed_nonces != {runtime_nonce}:
        raise RuntimeError("runtime nonce evidence does not match this acceptance invocation")
    return {
        "public_tool_requested": True,
        "public_tool_completed": True,
        "correlated_runtime_audit": True,
        "runtime_output_schema_nonce": observed_nonces.pop(),
    }


def _poll_run(
    client: PluginAcceptanceClient,
    run_id: str,
    *,
    wait_seconds: float,
    poll_interval_seconds: float,
) -> Mapping[str, object]:
    deadline = time.monotonic() + max(wait_seconds, 0.1)
    while True:
        details = _mapping(
            client.request_json("GET", f"/api/v1/runs/{quote(run_id, safe='')}/details"),
            "public run details",
        )
        status = details.get("status")
        if status in _TERMINAL_RUN_STATUSES:
            return details
        if status == "waiting_approval":
            approval_id = details.get("approval_id")
            version = details.get("version")
            if isinstance(approval_id, str) and isinstance(version, int):
                client.request_json(
                    "POST",
                    f"/api/v1/runs/{quote(run_id, safe='')}/approve-capability",
                    body={"approval_id": approval_id, "version": version},
                )
        if time.monotonic() >= deadline:
            raise RuntimeError("public plugin run did not reach a terminal status")
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)


def _wait_for_run_quiescence(
    client: PluginAcceptanceClient,
    run_id: str,
    *,
    wait_seconds: float,
    poll_interval_seconds: float,
) -> str:
    deadline = time.monotonic() + max(wait_seconds, 0)
    attempts = 0
    last_status: object = None
    last_quiescent: object = None
    last_lease_expiry: object = None
    while True:
        attempts += 1
        details = _mapping(
            client.request_json(
                "GET",
                f"/api/v1/runs/{quote(run_id, safe='')}/details",
            ),
            "cancelled public run details",
        )
        last_status = details.get("status")
        last_quiescent = details.get("execution_quiescent")
        last_lease_expiry = details.get("execution_lease_expires_at")
        if last_status in _TERMINAL_RUN_STATUSES and last_quiescent is True:
            return "quiescent"
        if time.monotonic() >= deadline and attempts >= 3:
            raise RuntimeError(
                "cancelled public run is not execution-quiescent: "
                f"status={last_status!r} execution_quiescent={last_quiescent!r} "
                f"execution_lease_expires_at={last_lease_expiry!r}"
            )
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)


def _cleanup_recovery(
    package: AcceptancePluginPackage,
    *,
    run_id: str,
    retry_after_seconds: float,
) -> dict[str, object]:
    plugin_path = f"/api/v1/admin/plugins/{quote(package.plugin_id, safe='')}"
    return {
        "required": True,
        "reason": "run_execution_not_quiescent",
        "run_id": run_id,
        "plugin_id": package.plugin_id,
        "key_id": package.key_id,
        "retry_after_seconds": max(1, int(retry_after_seconds or 1)),
        "steps": [
            {
                "method": "GET",
                "path": f"/api/v1/runs/{quote(run_id, safe='')}/details",
                "require": {"execution_quiescent": True},
            },
            *_resource_cleanup_recovery_steps(package, plugin_path=plugin_path),
        ],
    }


def _unknown_run_cleanup_recovery(
    package: AcceptancePluginPackage,
    *,
    run_request: dict[str, object],
    run_idempotency_key: str,
    retry_after_seconds: float,
) -> dict[str, object]:
    plugin_path = f"/api/v1/admin/plugins/{quote(package.plugin_id, safe='')}"
    return {
        "required": True,
        "reason": "run_submission_result_unknown",
        "run_id": None,
        "plugin_id": package.plugin_id,
        "key_id": package.key_id,
        "retry_after_seconds": max(1, int(retry_after_seconds or 1)),
        "steps": [
            {
                "method": "POST",
                "path": "/api/v1/runs",
                "body": run_request,
                "idempotency_key": run_idempotency_key,
                "capture": {"run_id": "id"},
            },
            {
                "method": "GET",
                "path": "/api/v1/runs/{run_id}/details",
                "require": {"execution_quiescent": True},
            },
            *_resource_cleanup_recovery_steps(package, plugin_path=plugin_path),
        ],
    }


def _is_side_effect_free_run_submission_rejection(error: AcceptanceHTTPError) -> bool:
    try:
        payload = json.loads(error.response_body)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    error_payload = payload.get("error")
    if not isinstance(error_payload, Mapping):
        return False
    error_code = error_payload.get("code")
    return isinstance(error_code, str) and (
        error.status_code,
        error_code,
    ) in _SIDE_EFFECT_FREE_RUN_SUBMISSION_REJECTIONS


def _resource_cleanup_recovery_steps(
    package: AcceptancePluginPackage,
    *,
    plugin_path: str,
) -> list[dict[str, object]]:
    return [
        {"method": "POST", "path": f"{plugin_path}/disable", "body": {}},
        {"method": "POST", "path": f"{plugin_path}/stop", "body": {}},
        {"method": "POST", "path": f"{plugin_path}/uninstall", "body": {}},
        {
            "method": "DELETE",
            "path": (
                "/api/v1/admin/plugins/signing-keys/"
                f"{quote(package.key_id, safe='')}"
            ),
        },
        {
            "method": "GET",
            "path": "/api/v1/admin/plugins",
            "require_absent": {"id": package.plugin_id},
        },
        {
            "method": "GET",
            "path": "/api/v1/admin/capabilities/manifest",
            "require_absent": {"capability_id": package.capability_id},
        },
        {
            "method": "GET",
            "path": "/api/v1/admin/plugins/signing-keys",
            "require_absent": {"key_id": package.key_id},
        },
    ]


def _cleanup(
    client: PluginAcceptanceClient,
    package: AcceptancePluginPackage,
    *,
    installed: bool,
    key_registered: bool,
) -> tuple[list[str], list[str]]:
    completed: list[str] = []
    errors: list[str] = []
    plugin_path = f"/api/v1/admin/plugins/{quote(package.plugin_id, safe='')}"
    if installed:
        for action in ("disable", "stop", "uninstall"):
            try:
                client.request_json("POST", f"{plugin_path}/{action}", body={})
                completed.append(action)
            except Exception as error:  # noqa: BLE001 - cleanup must continue through every phase.
                errors.append(f"{action}: {error}")
    if key_registered:
        try:
            client.request_json(
                "DELETE",
                f"/api/v1/admin/plugins/signing-keys/{quote(package.key_id, safe='')}",
            )
            completed.append("delete_signing_key")
        except Exception as error:  # noqa: BLE001 - report cleanup failures without hiding the run.
            errors.append(f"delete_signing_key: {error}")
        try:
            signing_keys = _list(
                client.request_json("GET", "/api/v1/admin/plugins/signing-keys"),
                "plugin signing key list",
            )
            if any(
                isinstance(item, Mapping) and item.get("key_id") == package.key_id
                for item in signing_keys
            ):
                raise RuntimeError("temporary signing key remains registered")
            completed.append("verify_signing_key_removed")
        except Exception as error:  # noqa: BLE001 - cleanup verification is acceptance evidence.
            errors.append(f"verify_signing_key_removed: {error}")
    if installed:
        try:
            plugins = _list(client.request_json("GET", "/api/v1/admin/plugins"), "plugin list")
            if any(isinstance(item, Mapping) and item.get("id") == package.plugin_id for item in plugins):
                raise RuntimeError("temporary plugin remains installed")
            manifest = client.request_json("GET", "/api/v1/admin/capabilities/manifest")
            if _manifest_capability(manifest, package.capability_id) is not None:
                raise RuntimeError("temporary plugin capability remains available")
            completed.append("verify_removed")
        except Exception as error:  # noqa: BLE001 - cleanup verification is acceptance evidence.
            errors.append(f"verify_removed: {error}")
    return completed, errors


def run_real_user_plugin_acceptance(
    admin_client: PluginAcceptanceClient,
    *,
    operator_client: PluginAcceptanceClient,
    execution_id: str,
    package_dir: Path,
    wait_seconds: float = 180,
    poll_interval_seconds: float = 2,
) -> dict[str, object]:
    """Run installation, public invocation, evidence checks, and unconditional cleanup."""

    package = build_acceptance_plugin(package_dir, execution_id=execution_id)
    errors: list[str] = []
    phases: list[str] = []
    evidence: dict[str, object] = {}
    key_registered = False
    installed = False
    run_id = ""
    run_terminal = False
    run_cleanup_blocked = False
    run_submission_attempted = False
    run_submission_rejected = False
    run_submission_unknown = False
    run_request: dict[str, object] | None = None
    run_idempotency_key: str | None = None
    recovery: dict[str, object] = {"required": False}
    try:
        admin_principal = _mapping(
            admin_client.request_json("GET", "/api/v1/auth/me"),
            "admin principal",
        )
        operator_principal = _mapping(
            operator_client.request_json("GET", "/api/v1/auth/me"),
            "operator principal",
        )
        admin_user_id = admin_principal.get("user_id")
        operator_user_id = operator_principal.get("user_id")
        admin_tenant_id = admin_principal.get("tenant_id")
        operator_tenant_id = operator_principal.get("tenant_id")
        if not isinstance(admin_user_id, str) or not admin_user_id:
            raise RuntimeError("authenticated admin principal did not expose user_id")
        if not isinstance(operator_user_id, str) or not operator_user_id:
            raise RuntimeError("authenticated operator principal did not expose user_id")
        if admin_principal.get("role") not in {"admin", "super_admin"}:
            raise RuntimeError("plugin installation principal is not an administrator")
        if operator_principal.get("role") != "operator":
            raise RuntimeError("public invocation principal is not an operator")
        if admin_user_id == operator_user_id:
            raise RuntimeError("admin and operator principals must be distinct users")
        if (
            not isinstance(admin_tenant_id, str)
            or not admin_tenant_id
            or admin_tenant_id != operator_tenant_id
        ):
            raise RuntimeError("admin and operator principals must belong to the same tenant")
        phases.append("authenticated")
        settings = _mapping(
            admin_client.request_json("GET", "/api/v1/admin/settings"),
            "system settings",
        )
        registration = settings.get("plugin_package_subprocess_registration_status")
        if registration != "ready":
            raise RuntimeError(f"plugin subprocess runtime is not ready: {registration}")
        phases.append("runtime_ready")
        plugins = _list(
            admin_client.request_json("GET", "/api/v1/admin/plugins"),
            "plugin list",
        )
        signing_keys = _list(
            admin_client.request_json("GET", "/api/v1/admin/plugins/signing-keys"),
            "plugin signing key list",
        )
        if any(
            isinstance(item, Mapping) and item.get("id") == package.plugin_id
            for item in plugins
        ) or any(
            isinstance(item, Mapping) and item.get("key_id") == package.key_id
            for item in signing_keys
        ):
            raise RuntimeError("temporary plugin or signing key already exists")
        phases.append("resource_names_available")
        key_registered = True
        admin_client.request_json(
            "POST",
            "/api/v1/admin/plugins/signing-keys",
            body={
                "key_id": package.key_id,
                "algorithm": "ed25519",
                "public_key": package.public_key,
            },
        )
        phases.append("signing_key_registered")
        installed = True
        installed_plugin = _plugin_from_install(
            admin_client.request_archive(
                "POST",
                "/api/v1/admin/plugins/install",
                archive=package.archive_path.read_bytes(),
                filename=package.archive_path.name,
            )
        )
        if installed_plugin.get("id") != package.plugin_id:
            raise RuntimeError("installed plugin id does not match the signed package")
        if _activation_state(installed_plugin) != "blocked_pending_approval":
            raise RuntimeError("plugin package did not enter pending approval")
        phases.append("package_installed")
        plugin_path = f"/api/v1/admin/plugins/{quote(package.plugin_id, safe='')}"
        approved = _mapping(
            admin_client.request_json(
                "POST",
                f"{plugin_path}/package/approve",
                body={"reason": "real-user production acceptance"},
            ),
            "plugin approval",
        )
        if _activation_state(approved) != "eligible":
            raise RuntimeError("approved plugin package is not eligible")
        phases.append("package_approved")
        enabled = _mapping(
            admin_client.request_json("POST", f"{plugin_path}/enable", body={}),
            "enable",
        )
        started = _mapping(
            admin_client.request_json("POST", f"{plugin_path}/start", body={}),
            "start",
        )
        if started.get("status") != "running" or enabled.get("enabled") is not True:
            raise RuntimeError("plugin did not become running and enabled")
        phases.append("plugin_enabled")
        capability = _manifest_capability(
            admin_client.request_json("GET", "/api/v1/admin/capabilities/manifest"),
            package.capability_id,
        )
        if capability is None:
            raise RuntimeError("plugin capability is missing from the runtime manifest")
        if capability.get("available") is not True:
            reason = capability.get("availability_reason")
            safe_reason = reason if isinstance(reason, str) and reason else "unknown_reason"
            raise RuntimeError(f"plugin capability is unavailable: {safe_reason}")
        phases.append("capability_available")
        run_request = {
            "mode": "dispatch",
            "skip_evolution_proposal": True,
            "message": (
                f"请自动选择并实际调用 {package.capability_id}，只调用一次。"
                f"参数必须是 {{\"text\": {json.dumps(package.input_text, ensure_ascii=False)}}}。"
                "完成后简要返回工具结果。"
            ),
        }
        run_idempotency_key = f"plugin-uat-{package.runtime_nonce}"
        run_submission_attempted = True
        try:
            submitted_payload = operator_client.request_json(
                "POST",
                "/api/v1/runs",
                body=run_request,
                idempotency_key=run_idempotency_key,
            )
        except AcceptanceHTTPError as error:
            if _is_side_effect_free_run_submission_rejection(error):
                run_submission_rejected = True
            raise
        submitted = _mapping(submitted_payload, "public run submission")
        raw_run_id = submitted.get("id")
        if not isinstance(raw_run_id, str) or not raw_run_id:
            raise RuntimeError("public run submission did not return an id")
        run_id = raw_run_id
        details = _poll_run(
            operator_client,
            run_id,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        run_status = details.get("status")
        run_terminal = run_status in _TERMINAL_RUN_STATUSES
        if run_status != "completed":
            raise RuntimeError(f"public plugin run ended with status {run_status}")
        phases.append("public_run_completed")
        events = operator_client.request_json(
            "GET", f"/api/v1/runs/{quote(run_id, safe='')}/events"
        )
        audit_resource = f"plugin:{package.plugin_id}:{package.capability_id}"
        audits = admin_client.request_json(
            "GET",
            (
                "/api/v1/admin/audit?action=plugin.invoke.succeeded"
                f"&run_id={quote(run_id, safe='')}"
                f"&user_id={quote(operator_user_id, safe='')}"
                f"&resource={quote(audit_resource, safe='')}"
                "&limit=1"
            ),
        )
        evidence.update(
            verify_public_plugin_invocation(
                events=events,
                audits=audits,
                run_id=run_id,
                user_id=operator_user_id,
                plugin_id=package.plugin_id,
                capability_id=package.capability_id,
                runtime_nonce=package.runtime_nonce,
            )
        )
        phases.append("unique_result_validated")
    except Exception as error:  # noqa: BLE001 - always clean up and return machine-readable evidence.
        errors.append(str(error))
    finally:
        run_cleanup_completed: list[str] = []
        run_cleanup_errors: list[str] = []
        cleanup_completed: list[str]
        cleanup_errors: list[str]
        if run_id:
            if not run_terminal:
                try:
                    operator_client.request_json(
                        "POST",
                        f"/api/v1/runs/{quote(run_id, safe='')}/cancel",
                    )
                    run_cleanup_completed.append("cancel_run")
                except Exception as error:  # noqa: BLE001 - quiescence check still follows.
                    run_cleanup_errors.append(f"cancel_run: {error}")
            try:
                quiescence = _wait_for_run_quiescence(
                    operator_client,
                    run_id,
                    wait_seconds=wait_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
                run_terminal = True
                run_cleanup_completed.append(f"verify_run_{quiescence}")
            except Exception as error:  # noqa: BLE001 - unsafe cleanup must remain deferred.
                run_cleanup_errors.append(f"cancel_run: {error}")
                run_cleanup_blocked = True
                recovery = _cleanup_recovery(
                    package,
                    run_id=run_id,
                    retry_after_seconds=max(poll_interval_seconds, 1),
                )
        elif run_submission_attempted and not run_submission_rejected:
            run_submission_unknown = True
            run_cleanup_blocked = True
            assert run_request is not None
            assert run_idempotency_key is not None
            recovery = _unknown_run_cleanup_recovery(
                package,
                run_request=run_request,
                run_idempotency_key=run_idempotency_key,
                retry_after_seconds=max(poll_interval_seconds, 1),
            )
        if run_cleanup_blocked:
            cleanup_completed = []
            cleanup_errors = [
                "plugin cleanup deferred because the public run submission result is unknown"
                if run_submission_unknown
                else "plugin cleanup deferred because the public run is not confirmed execution-quiescent"
            ]
        else:
            cleanup_completed, cleanup_errors = _cleanup(
                admin_client,
                package,
                installed=installed,
                key_registered=key_registered,
            )
        cleanup_completed = [*run_cleanup_completed, *cleanup_completed]
        cleanup_errors = [*run_cleanup_errors, *cleanup_errors]
        errors.extend(cleanup_errors)
    passed = (
        not errors
        and "verify_removed" in cleanup_completed
        and "verify_signing_key_removed" in cleanup_completed
    )
    cleanup_state = (
        "deferred_waiting_for_run_identity"
        if run_submission_unknown
        else "deferred_waiting_for_execution_quiescence"
        if run_cleanup_blocked
        else "failed"
        if cleanup_errors
        else "complete"
    )
    return {
        "schema_version": 1,
        "kind": "real_user_plugin_acceptance",
        "status": "passed" if passed else "failed",
        "acceptance_complete": passed,
        "execution_id": execution_id,
        "plugin_id": package.plugin_id,
        "capability_id": package.capability_id,
        "run_id": run_id or None,
        "run_cleanup_blocked": run_cleanup_blocked,
        "cleanup_state": cleanup_state,
        "recovery": recovery,
        "operator_user_id": operator_user_id if "operator_user_id" in locals() else None,
        "phases": phases,
        "evidence": evidence,
        "cleanup": {"completed": cleanup_completed, "errors": cleanup_errors},
        "errors": errors,
        "success_basis": {
            "logged_in_user_public_run": "public_run_completed" in phases,
            "admin_internal_run_data": False,
            "runtime_output_schema_nonce": "unique_result_validated" in phases,
        },
    }


def _write_report(path: str | None, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


def _validate_temporary_operator(
    value: object,
    *,
    username: str,
    tenant_id: str,
    require_id: bool,
) -> str | None:
    operator = _mapping(value, "temporary operator")
    if operator.get("username") != username:
        raise RuntimeError("temporary operator username does not match the requested identity")
    if operator.get("role") != "operator":
        raise RuntimeError("temporary operator role is not operator")
    observed_tenant = operator.get("tenant_id")
    if observed_tenant is not None and observed_tenant != tenant_id:
        raise RuntimeError("temporary operator tenant does not match the admin tenant")
    user_id = operator.get("id")
    if require_id and (not isinstance(user_id, str) or not user_id):
        raise RuntimeError("temporary operator creation did not return an id")
    return user_id if isinstance(user_id, str) and user_id else None


def _find_temporary_operator(
    admin_client: PluginAcceptanceClient,
    *,
    username: str,
    tenant_id: str,
    expected_user_id: str | None,
) -> str | None:
    principal = _mapping(
        admin_client.request_json("GET", "/api/v1/auth/me"),
        "admin principal before temporary operator cleanup",
    )
    if principal.get("tenant_id") != tenant_id:
        raise RuntimeError("admin tenant changed before temporary operator cleanup")
    if principal.get("role") not in {"admin", "super_admin"}:
        raise RuntimeError("temporary operator cleanup principal is not an administrator")
    users = _list(admin_client.request_json("GET", "/api/v1/users"), "user list")
    matches = [
        item
        for item in users
        if isinstance(item, Mapping) and item.get("username") == username
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("temporary operator username did not resolve to one user")
    resolved_id = _validate_temporary_operator(
        matches[0],
        username=username,
        tenant_id=tenant_id,
        require_id=True,
    )
    if expected_user_id is not None and resolved_id != expected_user_id:
        raise RuntimeError("temporary operator id changed before cleanup")
    return resolved_id


def _remove_temporary_operator(
    admin_client: PluginAcceptanceClient,
    *,
    username: str,
    tenant_id: str,
    expected_user_id: str | None,
) -> tuple[list[str], list[str]]:
    completed: list[str] = []
    errors: list[str] = []
    try:
        user_id = _find_temporary_operator(
            admin_client,
            username=username,
            tenant_id=tenant_id,
            expected_user_id=expected_user_id,
        )
        if user_id is None:
            completed.append("verify_user_absent")
            return completed, errors
        admin_client.request_json(
            "DELETE",
            f"/api/v1/users/{quote(user_id, safe='')}",
        )
        completed.append("delete_user")
    except Exception as error:  # noqa: BLE001 - verification still runs after deletion failure.
        errors.append(f"delete_user: {error}")
    try:
        users = _list(admin_client.request_json("GET", "/api/v1/users"), "user list")
        if any(
            isinstance(item, Mapping)
            and (item.get("id") == expected_user_id or item.get("username") == username)
            for item in users
        ):
            raise RuntimeError("temporary operator remains registered")
        completed.append("verify_user_removed")
    except Exception as error:  # noqa: BLE001 - cleanup verification is acceptance evidence.
        errors.append(f"verify_user_removed: {error}")
    return completed, errors


def _record_temporary_operator_cleanup(
    report: dict[str, object],
    *,
    completed: list[str],
    errors: list[str],
) -> None:
    report["temporary_operator_cleanup"] = {
        "completed": completed,
        "errors": errors,
    }
    if not errors:
        return
    existing_errors = report.get("errors")
    report["errors"] = [
        *(existing_errors if isinstance(existing_errors, list) else []),
        *(f"temporary_operator_cleanup: {error}" for error in errors),
    ]
    report["status"] = "failed"
    report["acceptance_complete"] = False
    if report.get("cleanup_state") not in {
        "deferred_waiting_for_execution_quiescence",
        "deferred_waiting_for_run_identity",
    }:
        report["cleanup_state"] = "failed"


def _setup_failure_report(execution_id: str, errors: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "real_user_plugin_acceptance",
        "status": "failed",
        "acceptance_complete": False,
        "execution_id": execution_id,
        "plugin_id": None,
        "capability_id": None,
        "run_id": None,
        "run_cleanup_blocked": False,
        "cleanup_state": "not_started",
        "recovery": {"required": False},
        "operator_user_id": None,
        "phases": [],
        "evidence": {},
        "cleanup": {"completed": [], "errors": []},
        "errors": errors,
        "success_basis": {
            "logged_in_user_public_run": False,
            "admin_internal_run_data": False,
            "runtime_output_schema_nonce": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a real signed plugin through a logged-in public run, then remove it."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--wait-seconds", type=float, default=180)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--execution-id", default=f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
    parser.add_argument("--output")
    parser.add_argument(
        "--operator-username",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME"),
    )
    parser.add_argument(
        "--operator-password",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD"),
    )
    parser.add_argument(
        "--operator-tenant-id",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_OPERATOR_TENANT_ID"),
    )
    args = parser.parse_args(argv)
    username, password, tenant_id = _acceptance_credentials_from_env()
    if not username or not password:
        parser.error("AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required")
    if bool(args.operator_username) != bool(args.operator_password):
        parser.error("operator username and password must be provided together")
    admin_client = UrllibPluginAcceptanceClient(
        base_url=args.base_url,
        timeout=args.timeout,
        username=username,
        password=password,
        tenant_id=tenant_id,
    )
    operator_source = "provided"
    temporary_operator_id: str | None = None
    temporary_operator_tenant: str | None = None
    operator_client: PluginAcceptanceClient | None = None
    setup_errors: list[str] = []
    cleanup_completed: list[str]
    cleanup_errors: list[str]
    report = _setup_failure_report(args.execution_id, setup_errors)
    if args.operator_username and args.operator_password:
        operator_username = args.operator_username
        operator_password = args.operator_password
    else:
        operator_source = "temporary"
        operator_username = f"plugin-uat-operator-{uuid4().hex[:16]}"
        operator_password = f"Uat-{uuid4().hex}-9aA!"
    try:
        if operator_source == "temporary":
            try:
                admin_principal = _mapping(
                    admin_client.request_json("GET", "/api/v1/auth/me"),
                    "admin principal before temporary operator creation",
                )
                raw_tenant_id = admin_principal.get("tenant_id")
                if not isinstance(raw_tenant_id, str) or not raw_tenant_id:
                    raise RuntimeError("admin principal did not expose tenant_id")
                if admin_principal.get("role") not in {"admin", "super_admin"}:
                    raise RuntimeError("temporary operator creator is not an administrator")
                if args.operator_tenant_id and args.operator_tenant_id != raw_tenant_id:
                    raise RuntimeError("temporary operator tenant differs from the admin tenant")
                temporary_operator_tenant = raw_tenant_id
                existing_id = _find_temporary_operator(
                    admin_client,
                    username=operator_username,
                    tenant_id=temporary_operator_tenant,
                    expected_user_id=None,
                )
                if existing_id is not None:
                    raise RuntimeError("temporary operator username already exists")
            except Exception as error:  # noqa: BLE001 - setup failure is reported and cleaned.
                setup_errors.append(f"temporary operator preflight: {error}")
            if not setup_errors and temporary_operator_tenant is not None:
                try:
                    created_operator = admin_client.request_json(
                        "POST",
                        "/api/v1/users",
                        body={
                            "username": operator_username,
                            "password": operator_password,
                            "role": "operator",
                        },
                    )
                    temporary_operator_id = _validate_temporary_operator(
                        created_operator,
                        username=operator_username,
                        tenant_id=temporary_operator_tenant,
                        require_id=True,
                    )
                except Exception as error:  # noqa: BLE001 - ambiguous creation still needs cleanup.
                    setup_errors.append(f"temporary operator creation: {error}")
        operator_client = UrllibPluginAcceptanceClient(
            base_url=args.base_url,
            timeout=args.timeout,
            username=operator_username,
            password=operator_password,
            tenant_id=args.operator_tenant_id or temporary_operator_tenant or tenant_id,
        )
        if operator_source == "temporary" and temporary_operator_tenant is not None:
            try:
                operator_principal = _mapping(
                    operator_client.request_json("GET", "/api/v1/auth/me"),
                    "temporary operator principal",
                )
                operator_user_id = operator_principal.get("user_id")
                if not isinstance(operator_user_id, str) or not operator_user_id:
                    raise RuntimeError("temporary operator principal did not expose user_id")
                if operator_principal.get("role") != "operator":
                    raise RuntimeError("temporary operator login role is not operator")
                if operator_principal.get("tenant_id") != temporary_operator_tenant:
                    raise RuntimeError("temporary operator login tenant does not match")
                if (
                    temporary_operator_id is not None
                    and temporary_operator_id != operator_user_id
                ):
                    raise RuntimeError(
                        "temporary operator user id differs from the creation response"
                    )
                if temporary_operator_id is None:
                    temporary_operator_id = operator_user_id
            except Exception as error:  # noqa: BLE001 - identity failure is reported and cleaned.
                setup_errors.append(f"temporary operator login: {error}")
        if setup_errors:
            report = _setup_failure_report(args.execution_id, setup_errors)
        else:
            with TemporaryDirectory(prefix="agent-hub-plugin-uat-") as temporary:
                report = run_real_user_plugin_acceptance(
                    admin_client,
                    operator_client=operator_client,
                    execution_id=args.execution_id,
                    package_dir=Path(temporary),
                    wait_seconds=args.wait_seconds,
                    poll_interval_seconds=args.poll_interval,
                )
    finally:
        if operator_source == "temporary" and report.get("run_cleanup_blocked") is True:
            cleanup_completed = []
            cleanup_errors = [
                "temporary operator cleanup deferred because the public run identity is unknown"
                if report.get("cleanup_state") == "deferred_waiting_for_run_identity"
                else "temporary operator cleanup deferred because its run is not execution-quiescent"
            ]
            recovery = report.get("recovery")
            if isinstance(recovery, dict):
                raw_steps = recovery.get("steps")
                if isinstance(raw_steps, list):
                    raw_steps.extend(
                        [
                            {
                                "method": "GET",
                                "path": "/api/v1/users",
                                "require": {
                                    "username": operator_username,
                                    "id": temporary_operator_id,
                                    "role": "operator",
                                    "tenant_id": temporary_operator_tenant,
                                },
                            },
                            {
                                "method": "DELETE",
                                "path": (
                                    f"/api/v1/users/{quote(temporary_operator_id, safe='')}"
                                    if temporary_operator_id
                                    else "/api/v1/users/{id-resolved-by-validated-username}"
                                ),
                            },
                            {
                                "method": "GET",
                                "path": "/api/v1/users",
                                "require_absent": {"username": operator_username},
                            },
                        ]
                    )
        elif operator_source == "temporary" and temporary_operator_tenant is not None:
            cleanup_completed, cleanup_errors = _remove_temporary_operator(
                admin_client,
                username=operator_username,
                tenant_id=temporary_operator_tenant,
                expected_user_id=temporary_operator_id,
            )
        else:
            cleanup_completed, cleanup_errors = [], []
    report["operator_source"] = operator_source
    _record_temporary_operator_cleanup(
        report,
        completed=cleanup_completed,
        errors=cleanup_errors,
    )
    report["base_url"] = args.base_url.rstrip("/")
    report["admin_request_log"] = getattr(admin_client, "request_log", [])
    report["operator_request_log"] = getattr(operator_client, "request_log", [])
    _write_report(args.output, report)
    return 0 if report["acceptance_complete"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
