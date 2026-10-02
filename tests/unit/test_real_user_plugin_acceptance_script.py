from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT_PATH = Path("scripts/real_user_plugin_acceptance.py")
USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"


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
        run_status: str = "completed",
        existing_resource: bool = False,
        install_response_lost: bool = False,
        residual_signing_key: bool = False,
    ) -> None:
        self.audit_matches = audit_matches
        self.capability_available = capability_available
        self.availability_reason = availability_reason
        self.run_status = run_status
        self.existing_resource = existing_resource
        self.install_response_lost = install_response_lost
        self.residual_signing_key = residual_signing_key
        self.requests: list[tuple[str, str]] = []
        self.plugin_id = ""
        self.capability_id = ""
        self.nonce = ""
        self.run_idempotency_keys: list[str] = []
        self.signing_keys = (
            {"plugin-uat-key-flow-123"} if existing_resource else set()
        )

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
        if self.install_response_lost:
            raise ConnectionError("install response was lost")
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
        self.requests.append((method, path))
        if path == "/api/v1/auth/me":
            return {"user_id": USER_ID, "tenant_id": "tenant-1", "role": "admin"}
        if path == "/api/v1/admin/settings":
            return {"plugin_package_subprocess_registration_status": "ready"}
        if path == "/api/v1/admin/plugins/signing-keys" and method == "GET":
            return [{"key_id": key_id} for key_id in sorted(self.signing_keys)]
        if path == "/api/v1/admin/plugins/signing-keys" and method == "POST":
            key_id = body["key_id"] if body else ""
            assert isinstance(key_id, str)
            self.signing_keys.add(key_id)
            return {"key_id": key_id}
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
            assert isinstance(idempotency_key, str)
            self.run_idempotency_keys.append(idempotency_key)
            return {"id": RUN_ID, "status": "queued", "version": 1}
        if path == f"/api/v1/runs/{RUN_ID}/details":
            return {"id": RUN_ID, "status": self.run_status, "version": 2}
        if path == f"/api/v1/runs/{RUN_ID}/cancel" and method == "POST":
            return {"id": RUN_ID, "status": "cancelled", "version": 3}
        if path == f"/api/v1/runs/{RUN_ID}/events":
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
        parsed = urlsplit(path)
        if parsed.path == "/api/v1/admin/audit":
            query = parse_qs(parsed.query)
            assert query == {
                "action": ["plugin.invoke.succeeded"],
                "run_id": [RUN_ID],
                "user_id": [USER_ID],
                "resource": [f"plugin:{self.plugin_id}:{self.capability_id}"],
                "limit": ["1"],
            }
            if not self.audit_matches:
                return []
            return [
                {
                    "action": "plugin.invoke.succeeded",
                    "details": {
                        "run_id": RUN_ID,
                        "user_id": USER_ID,
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
            key_id = path.rsplit("/", 1)[-1]
            if not self.residual_signing_key:
                self.signing_keys.discard(key_id)
            return {"status": "deleted"}
        if path == "/api/v1/admin/plugins" and method == "GET":
            if self.existing_resource:
                return [{"id": "plugin-uat-flow-123"}]
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


def test_build_acceptance_plugin_uses_fresh_runtime_nonce_for_same_execution_id(
    tmp_path: Path,
) -> None:
    module = _load_script()

    first = module.build_acceptance_plugin(tmp_path / "first", execution_id="repeat-run")
    second = module.build_acceptance_plugin(tmp_path / "second", execution_id="repeat-run")

    assert first.runtime_nonce != second.runtime_nonce
    assert first.plugin_id != second.plugin_id
    assert first.capability_id != second.capability_id
    assert first.key_id != second.key_id
    assert first.runtime_nonce in first.plugin_id
    assert first.runtime_nonce in first.capability_id


def test_build_acceptance_plugin_avoids_long_execution_id_prefix_collisions(
    tmp_path: Path,
) -> None:
    module = _load_script()
    shared_prefix = "same-prefix-that-is-longer-than-twenty-four-characters"

    first = module.build_acceptance_plugin(
        tmp_path / "first",
        execution_id=f"{shared_prefix}-first",
    )
    second = module.build_acceptance_plugin(
        tmp_path / "second",
        execution_id=f"{shared_prefix}-second",
    )

    assert first.plugin_id != second.plugin_id
    assert first.capability_id != second.capability_id
    assert first.key_id != second.key_id


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
        runtime_nonce="case",
    )

    assert evidence["public_tool_completed"] is True
    assert evidence["correlated_runtime_audit"] is True
    assert evidence["runtime_output_schema_nonce"] == "case"
    with pytest.raises(RuntimeError, match="correlated plugin invocation audit"):
        module.verify_public_plugin_invocation(
            events=events,
            audits=[],
            run_id="run-1",
            user_id="user-1",
            plugin_id="plugin-case",
            capability_id="acceptance.stats_case",
            runtime_nonce="case",
        )


def test_verify_public_invocation_rejects_multiple_calls() -> None:
    module = _load_script()
    events = {
        "items": [
            {"kind": "tool.requested", "tool_name": "acceptance.stats_case"},
            {"kind": "tool.completed", "tool_name": "acceptance.stats_case"},
            {"kind": "tool.requested", "tool_name": "acceptance.stats_case"},
            {"kind": "tool.completed", "tool_name": "acceptance.stats_case"},
        ]
    }
    audits = [
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

    with pytest.raises(RuntimeError, match="exactly once"):
        module.verify_public_plugin_invocation(
            events=events,
            audits=audits,
            run_id="run-1",
            user_id="user-1",
            plugin_id="plugin-case",
            capability_id="acceptance.stats_case",
            runtime_nonce="case",
        )


def test_verify_public_invocation_rejects_old_run_evidence_for_fresh_nonce(
    tmp_path: Path,
) -> None:
    module = _load_script()
    old = module.build_acceptance_plugin(tmp_path / "old", execution_id="same-execution")
    fresh = module.build_acceptance_plugin(tmp_path / "fresh", execution_id="same-execution")
    old_events = {
        "items": [
            {"kind": "tool.requested", "tool_name": old.capability_id},
            {"kind": "tool.completed", "tool_name": old.capability_id},
        ]
    }
    old_audits = [
        {
            "action": "plugin.invoke.succeeded",
            "details": {
                "run_id": "run-old",
                "user_id": "user-1",
                "plugin_id": old.plugin_id,
                "capability_id": old.capability_id,
            },
        }
    ]

    with pytest.raises(RuntimeError):
        module.verify_public_plugin_invocation(
            events=old_events,
            audits=old_audits,
            run_id="run-old",
            user_id="user-1",
            plugin_id=fresh.plugin_id,
            capability_id=fresh.capability_id,
            runtime_nonce=fresh.runtime_nonce,
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


def test_real_user_plugin_acceptance_reuses_execution_id_with_fresh_run_nonce(
    tmp_path: Path,
) -> None:
    module = _load_script()
    first_client = FakePluginAcceptanceClient()
    second_client = FakePluginAcceptanceClient()

    first = module.run_real_user_plugin_acceptance(
        first_client,
        execution_id="same-execution",
        package_dir=tmp_path / "first",
        wait_seconds=1,
        poll_interval_seconds=0,
    )
    second = module.run_real_user_plugin_acceptance(
        second_client,
        execution_id="same-execution",
        package_dir=tmp_path / "second",
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert first["status"] == "passed"
    assert second["status"] == "passed"
    assert first_client.run_idempotency_keys != second_client.run_idempotency_keys


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
    assert report["success_basis"] == {
        "logged_in_user_public_run": True,
        "admin_internal_run_data": False,
        "runtime_output_schema_nonce": False,
    }
    assert any(path.endswith("/disable") for _, path in client.requests)
    assert any(path.endswith("/stop") for _, path in client.requests)
    assert any(path.endswith("/uninstall") for _, path in client.requests)
    assert any(path.startswith("/api/v1/admin/plugins/signing-keys/") for _, path in client.requests)


def test_real_user_plugin_acceptance_cancels_nonterminal_run_before_cleanup(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(run_status="queued")

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="flow-timeout",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert ("POST", f"/api/v1/runs/{RUN_ID}/cancel") in client.requests
    assert "cancel_run" in report["cleanup"]["completed"]
    cancel_index = client.requests.index(("POST", f"/api/v1/runs/{RUN_ID}/cancel"))
    uninstall_index = next(
        index for index, request in enumerate(client.requests) if request[1].endswith("/uninstall")
    )
    assert cancel_index < uninstall_index


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


def test_real_user_plugin_acceptance_refuses_existing_plugin_or_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    package = module.build_acceptance_plugin(tmp_path, execution_id="flow-123")
    client = FakePluginAcceptanceClient()
    client.signing_keys.add(package.key_id)
    monkeypatch.setattr(module, "build_acceptance_plugin", lambda *_args, **_kwargs: package)

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="flow-123",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["errors"] == ["temporary plugin or signing key already exists"]
    assert ("POST", "/api/v1/admin/plugins/signing-keys") not in client.requests
    assert ("POST", "/api/v1/admin/plugins/install") not in client.requests


def test_real_user_plugin_acceptance_cleans_up_after_lost_install_response(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(install_response_lost=True)

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="ambiguous-install",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert any(path.endswith("/uninstall") for _, path in client.requests)
    assert any(path.startswith("/api/v1/admin/plugins/signing-keys/") for _, path in client.requests)


def test_real_user_plugin_acceptance_fails_when_signing_key_remains(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(residual_signing_key=True)

    report = module.run_real_user_plugin_acceptance(
        client,
        execution_id="residual-key",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["acceptance_complete"] is False
    assert any("signing key remains registered" in error for error in report["errors"])
