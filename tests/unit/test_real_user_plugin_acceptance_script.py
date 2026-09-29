from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path("scripts/real_user_plugin_acceptance.py")


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("real_user_plugin_acceptance", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakePluginAcceptanceClient:
    def __init__(
        self,
        *,
        audit_matches: bool = True,
        capability_available: bool = True,
        availability_reason: str | None = None,
    ) -> None:
        self.audit_matches = audit_matches
        self.capability_available = capability_available
        self.availability_reason = availability_reason
        self.requests: list[tuple[str, str]] = []
        self.plugin_id = ""
        self.capability_id = ""
        self.nonce = ""

    def request_archive(
        self,
        method: str,
        path: str,
        *,
        archive: bytes,
        filename: str,
    ) -> dict[str, object]:
        self.requests.append((method, path))
        assert method == "POST"
        assert path == "/api/v1/admin/plugins/install"
        assert filename.endswith(".zip")
        with zipfile.ZipFile(__import__("io").BytesIO(archive)) as package:
            manifest = json.loads(package.read("plugin.json"))
        self.plugin_id = manifest["id"]
        capability = manifest["capabilities"][0]
        self.capability_id = capability["id"]
        self.nonce = capability["output_schema"]["properties"]["acceptance_nonce"]["const"]
        return {
            "plugin": {
                "id": self.plugin_id,
                "status": "stopped",
                "enabled": False,
                "package_metadata": {"activation_state": "blocked_pending_approval"},
            }
        }

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        del idempotency_key
        self.requests.append((method, path))
        if path == "/api/v1/auth/me":
            return {"user_id": "user-real-1", "tenant_id": "tenant-1", "role": "admin"}
        if path == "/api/v1/admin/settings":
            return {"plugin_package_subprocess_registration_status": "ready"}
        if path == "/api/v1/admin/plugins/signing-keys" and method == "POST":
            return {"key_id": body["key_id"] if body else ""}
        if path.endswith("/package/approve"):
            return {
                "id": self.plugin_id,
                "package_metadata": {"activation_state": "eligible"},
            }
        if path.endswith("/start"):
            return {"id": self.plugin_id, "status": "running", "health": "healthy"}
        if path.endswith("/enable"):
            return {
                "id": self.plugin_id,
                "status": "stopped",
                "health": "stopped",
                "enabled": True,
            }
        if path == "/api/v1/admin/capabilities/manifest":
            active = not any(request_path.endswith("/uninstall") for _, request_path in self.requests)
            return {
                "schema_version": 1,
                "capabilities": (
                    [
                        {
                            "id": self.capability_id,
                            "available": self.capability_available,
                            "availability_reason": self.availability_reason,
                        }
                    ]
                    if active
                    else []
                ),
            }
        if path == "/api/v1/runs" and method == "POST":
            assert body is not None
            assert body["mode"] == "dispatch"
            assert self.capability_id in str(body["message"])
            return {"id": "run-public-1", "status": "queued", "version": 1}
        if path == "/api/v1/runs/run-public-1/details":
            return {"id": "run-public-1", "status": "completed", "version": 2}
        if path == "/api/v1/runs/run-public-1/events":
            return {
                "items": [
                    {"kind": "tool.requested", "tool_name": self.capability_id},
                    {
                        "kind": "tool.completed",
                        "tool_name": self.capability_id,
                        "payload": {"status": "succeeded", "result_bytes": 128},
                    },
                ]
            }
        if path == "/api/v1/admin/audit?action=plugin.invoke.succeeded":
            if not self.audit_matches:
                return []
            return [
                {
                    "action": "plugin.invoke.succeeded",
                    "details": {
                        "run_id": "run-public-1",
                        "user_id": "user-real-1",
                        "plugin_id": self.plugin_id,
                        "capability_id": self.capability_id,
                    },
                }
            ]
        if path.endswith("/disable"):
            return {"id": self.plugin_id, "enabled": False}
        if path.endswith("/stop"):
            return {"id": self.plugin_id, "status": "stopped", "health": "stopped"}
        if path.endswith("/uninstall"):
            return {"status": "uninstalled"}
        if path.startswith("/api/v1/admin/plugins/signing-keys/") and method == "DELETE":
            return {"status": "deleted"}
        if path == "/api/v1/admin/plugins" and method == "GET":
            return []
        raise AssertionError(f"unexpected request: {method} {path} {body}")


def test_build_acceptance_plugin_is_signed_executable_and_nonce_bound(tmp_path: Path) -> None:
    module = _load_script()

    package = module.build_acceptance_plugin(tmp_path, execution_id="case-123")

    assert package.archive_path.is_file()
    with zipfile.ZipFile(package.archive_path) as archive:
        manifest = json.loads(archive.read("plugin.json"))
        adapter = archive.read("adapter/main.py")
    capability = manifest["capabilities"][0]
    assert manifest["id"] == package.plugin_id
    assert capability["id"] == package.capability_id
    assert capability["output_schema"]["properties"]["acceptance_nonce"] == {
        "type": "string",
        "const": package.nonce,
    }
    completed = subprocess.run(
        [sys.executable, "-c", adapter.decode("utf-8")],
        input=json.dumps(
            {
                "capability_id": package.capability_id,
                "arguments": {"text": package.input_text},
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result["acceptance_nonce"] == package.nonce
    assert result["character_count"] == len(package.input_text)


def test_verify_public_invocation_correlates_public_tool_event_and_user_audit() -> None:
    module = _load_script()
    events = {
        "items": [
            {"kind": "tool.requested", "tool_name": "acceptance.stats_case"},
            {"kind": "tool.completed", "tool_name": "acceptance.stats_case"},
        ]
    }
    audit = [
        {
            "action": "plugin.invoke.succeeded",
            "details": {
                "run_id": "run-1",
                "user_id": "user-1",
                "plugin_id": "plugin-case",
                "capability_id": "acceptance.stats_case",
            },
        }
    ]

    evidence = module.verify_public_plugin_invocation(
        events=events,
        audits=audit,
        run_id="run-1",
        user_id="user-1",
        plugin_id="plugin-case",
        capability_id="acceptance.stats_case",
    )

    assert evidence["public_tool_completed"] is True
    assert evidence["correlated_runtime_audit"] is True
    with pytest.raises(RuntimeError, match="correlated plugin invocation audit"):
        module.verify_public_plugin_invocation(
            events=events,
            audits=[],
            run_id="run-1",
            user_id="user-1",
            plugin_id="plugin-case",
            capability_id="acceptance.stats_case",
        )


def test_real_user_plugin_acceptance_runs_public_flow_and_cleans_up(tmp_path: Path) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient()

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="flow-123",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "passed"
    assert report["acceptance_complete"] is True
    assert report["success_basis"] == {
        "logged_in_user_public_run": True,
        "admin_internal_run_data": False,
        "runtime_output_schema_nonce": True,
    }
    assert ("POST", "/api/v1/runs") in client.requests
    enable_index = next(
        index for index, request in enumerate(client.requests) if request[1].endswith("/enable")
    )
    start_index = next(
        index for index, request in enumerate(client.requests) if request[1].endswith("/start")
    )
    assert enable_index < start_index
    assert not any(path.startswith("/api/v1/admin/runs") for _, path in client.requests)
    assert any(path.endswith("/disable") for _, path in client.requests)
    assert any(path.endswith("/stop") for _, path in client.requests)
    assert any(path.endswith("/uninstall") for _, path in client.requests)
    assert ("GET", "/api/v1/admin/plugins") in client.requests


def test_real_user_plugin_acceptance_cleans_up_after_evidence_failure(tmp_path: Path) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(audit_matches=False)

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="flow-failure",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["acceptance_complete"] is False
    assert any(path.endswith("/disable") for _, path in client.requests)
    assert any(path.endswith("/stop") for _, path in client.requests)
    assert any(path.endswith("/uninstall") for _, path in client.requests)
    assert any(path.startswith("/api/v1/admin/plugins/signing-keys/") for _, path in client.requests)


def test_real_user_plugin_acceptance_reports_manifest_unavailable_reason(tmp_path: Path) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(
        capability_available=False,
        availability_reason="plugin_package_adapter_unavailable",
    )

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="flow-unavailable",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["errors"] == [
        "plugin capability is unavailable: plugin_package_adapter_unavailable"
    ]
