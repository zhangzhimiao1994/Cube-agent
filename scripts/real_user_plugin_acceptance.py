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
    UrllibAcceptanceClient,
    _acceptance_credentials_from_env,
)
from agent_hub.plugins.package_builder import build_signed_plugin_archive

_ADMIN_RUN_PREFIX = "/api/v1/admin/runs"
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


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
    nonce: str
    input_text: str
    archive_path: Path
    public_key: str


def _safe_suffix(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (normalized or uuid4().hex)[:24].rstrip("-")


def build_acceptance_plugin(package_dir: Path, *, execution_id: str) -> AcceptancePluginPackage:
    """Build a unique signed package whose output schema proves the exact adapter result."""

    suffix = _safe_suffix(execution_id)
    plugin_id = f"plugin-uat-{suffix}"
    capability_id = f"acceptance.stats_{suffix.replace('-', '_')}"
    key_id = f"plugin-uat-key-{suffix}"
    nonce = f"plugin-uat-result-{suffix}-{uuid4().hex[:12]}"
    input_text = f"真实插件验收 {suffix}"
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


def verify_public_plugin_invocation(
    *,
    events: object,
    audits: object,
    run_id: str,
    user_id: str,
    plugin_id: str,
    capability_id: str,
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
    requested = any(item.get("kind") == "tool.requested" for item in matching_events)
    completed = any(item.get("kind") == "tool.completed" for item in matching_events)
    failed = any(item.get("kind") == "tool.failed" for item in matching_events)
    if not requested or not completed or failed:
        raise RuntimeError("public run did not complete the expected plugin capability")
    audit_items = _list(audits, "plugin invocation audit")
    correlated = False
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
            correlated = True
            break
    if not correlated:
        raise RuntimeError("correlated plugin invocation audit was not found")
    return {
        "public_tool_requested": True,
        "public_tool_completed": True,
        "correlated_runtime_audit": True,
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
    client: PluginAcceptanceClient,
    *,
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
    try:
        principal = _mapping(client.request_json("GET", "/api/v1/auth/me"), "principal")
        user_id = principal.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise RuntimeError("authenticated principal did not expose user_id")
        phases.append("authenticated")
        settings = _mapping(
            client.request_json("GET", "/api/v1/admin/settings"),
            "system settings",
        )
        registration = settings.get("plugin_package_subprocess_registration_status")
        if registration != "ready":
            raise RuntimeError(f"plugin subprocess runtime is not ready: {registration}")
        phases.append("runtime_ready")
        client.request_json(
            "POST",
            "/api/v1/admin/plugins/signing-keys",
            body={
                "key_id": package.key_id,
                "algorithm": "ed25519",
                "public_key": package.public_key,
            },
        )
        key_registered = True
        phases.append("signing_key_registered")
        installed_plugin = _plugin_from_install(
            client.request_archive(
                "POST",
                "/api/v1/admin/plugins/install",
                archive=package.archive_path.read_bytes(),
                filename=package.archive_path.name,
            )
        )
        installed = True
        if installed_plugin.get("id") != package.plugin_id:
            raise RuntimeError("installed plugin id does not match the signed package")
        if _activation_state(installed_plugin) != "blocked_pending_approval":
            raise RuntimeError("plugin package did not enter pending approval")
        phases.append("package_installed")
        plugin_path = f"/api/v1/admin/plugins/{quote(package.plugin_id, safe='')}"
        approved = _mapping(
            client.request_json(
                "POST",
                f"{plugin_path}/package/approve",
                body={"reason": "real-user production acceptance"},
            ),
            "plugin approval",
        )
        if _activation_state(approved) != "eligible":
            raise RuntimeError("approved plugin package is not eligible")
        phases.append("package_approved")
        started = _mapping(client.request_json("POST", f"{plugin_path}/start", body={}), "start")
        enabled = _mapping(client.request_json("POST", f"{plugin_path}/enable", body={}), "enable")
        if started.get("status") != "running" or enabled.get("enabled") is not True:
            raise RuntimeError("plugin did not become running and enabled")
        phases.append("plugin_enabled")
        capability = _manifest_capability(
            client.request_json("GET", "/api/v1/admin/capabilities/manifest"),
            package.capability_id,
        )
        if capability is None or capability.get("available") is not True:
            raise RuntimeError("plugin capability is not available in the runtime manifest")
        phases.append("capability_available")
        submitted = _mapping(
            client.request_json(
                "POST",
                "/api/v1/runs",
                body={
                    "mode": "dispatch",
                    "skip_evolution_proposal": True,
                    "message": (
                        f"请自动选择并实际调用 {package.capability_id}，只调用一次。"
                        f"参数必须是 {{\"text\": {json.dumps(package.input_text, ensure_ascii=False)}}}。"
                        "完成后简要返回工具结果。"
                    ),
                },
                idempotency_key=f"plugin-uat-{_safe_suffix(execution_id)}",
            ),
            "public run submission",
        )
        raw_run_id = submitted.get("id")
        if not isinstance(raw_run_id, str) or not raw_run_id:
            raise RuntimeError("public run submission did not return an id")
        run_id = raw_run_id
        details = _poll_run(
            client,
            run_id,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if details.get("status") != "completed":
            raise RuntimeError(f"public plugin run ended with status {details.get('status')}")
        phases.append("public_run_completed")
        events = client.request_json(
            "GET", f"/api/v1/runs/{quote(run_id, safe='')}/events"
        )
        audits = client.request_json(
            "GET", "/api/v1/admin/audit?action=plugin.invoke.succeeded"
        )
        evidence.update(
            verify_public_plugin_invocation(
                events=events,
                audits=audits,
                run_id=run_id,
                user_id=user_id,
                plugin_id=package.plugin_id,
                capability_id=package.capability_id,
            )
        )
        evidence["runtime_output_schema_nonce"] = package.nonce
        phases.append("unique_result_validated")
    except Exception as error:  # noqa: BLE001 - always clean up and return machine-readable evidence.
        errors.append(str(error))
    finally:
        cleanup_completed, cleanup_errors = _cleanup(
            client,
            package,
            installed=installed,
            key_registered=key_registered,
        )
        errors.extend(cleanup_errors)
    passed = not errors and "verify_removed" in cleanup_completed
    return {
        "schema_version": 1,
        "kind": "real_user_plugin_acceptance",
        "status": "passed" if passed else "failed",
        "acceptance_complete": passed,
        "execution_id": execution_id,
        "plugin_id": package.plugin_id,
        "capability_id": package.capability_id,
        "run_id": run_id or None,
        "phases": phases,
        "evidence": evidence,
        "cleanup": {"completed": cleanup_completed, "errors": cleanup_errors},
        "errors": errors,
        "success_basis": {
            "logged_in_user_public_run": True,
            "admin_internal_run_data": False,
            "runtime_output_schema_nonce": True,
        },
    }


def _write_report(path: str | None, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


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
    args = parser.parse_args(argv)
    username, password, tenant_id = _acceptance_credentials_from_env()
    if not username or not password:
        parser.error("AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required")
    client = UrllibPluginAcceptanceClient(
        base_url=args.base_url,
        timeout=args.timeout,
        username=username,
        password=password,
        tenant_id=tenant_id,
    )
    with TemporaryDirectory(prefix="agent-hub-plugin-uat-") as temporary:
        report = run_real_user_plugin_acceptance(
            client,
            execution_id=args.execution_id,
            package_dir=Path(temporary),
            wait_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_interval,
        )
    report["base_url"] = args.base_url.rstrip("/")
    report["request_log"] = client.request_log
    _write_report(args.output, report)
    return 0 if report["acceptance_complete"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
