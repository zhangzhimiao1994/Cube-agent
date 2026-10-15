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
from jsonschema import ValidationError, validate  # type: ignore[import-untyped]

SCRIPT_PATH = Path("scripts/real_user_plugin_acceptance.py")
ADMIN_USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
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
        run_response_lost: bool = False,
        residual_signing_key: bool = False,
        cancel_effective: bool = True,
        cancel_quiescent_after_polls: int = 0,
        residual_plugin: bool = False,
        terminal_quiescent: bool = True,
        omit_quiescence_field: bool = False,
        expected_mode: str = "dispatch",
        expected_direct_model: str | None = None,
    ) -> None:
        self.audit_matches = audit_matches
        self.capability_available = capability_available
        self.availability_reason = availability_reason
        self.run_status = run_status
        self.existing_resource = existing_resource
        self.install_response_lost = install_response_lost
        self.run_response_lost = run_response_lost
        self.residual_signing_key = residual_signing_key
        self.cancel_effective = cancel_effective
        self.cancel_quiescent_after_polls = cancel_quiescent_after_polls
        self.cancel_detail_polls = 0
        self.residual_plugin = residual_plugin
        self.terminal_quiescent = terminal_quiescent
        self.omit_quiescence_field = omit_quiescence_field
        self.expected_mode = expected_mode
        self.expected_direct_model = expected_direct_model
        self.cancel_requested = False
        self.requests: list[tuple[str, str]] = []
        self.plugin_id = ""
        self.capability_id = ""
        self.nonce = ""
        self.run_idempotency_keys: list[str] = []
        self.installed_plugins: dict[str, dict[str, object]] = {}
        if existing_resource:
            self.installed_plugins["plugin-uat-flow-123"] = {
                "id": "plugin-uat-flow-123",
                "status": "stopped",
                "enabled": False,
                "approved": True,
            }
        self.signing_keys = {"plugin-uat-key-flow-123"} if existing_resource else set()

    def capability_is_active(self) -> bool:
        plugin = self.installed_plugins.get(self.plugin_id)
        return bool(plugin and plugin.get("enabled") is True and plugin.get("status") == "running")

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
        self.installed_plugins[self.plugin_id] = {
            "id": self.plugin_id,
            "status": "stopped",
            "enabled": False,
            "approved": False,
        }
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
            return {"user_id": ADMIN_USER_ID, "tenant_id": "tenant-1", "role": "admin"}
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
            self.installed_plugins[self.plugin_id]["approved"] = True
            return {
                "id": self.plugin_id,
                "package_metadata": {"activation_state": "eligible"},
            }
        if path.endswith("/start"):
            assert self.installed_plugins[self.plugin_id]["approved"] is True
            self.installed_plugins[self.plugin_id]["status"] = "running"
            return {"id": self.plugin_id, "status": "running", "health": "healthy"}
        if path.endswith("/enable"):
            assert self.installed_plugins[self.plugin_id]["approved"] is True
            self.installed_plugins[self.plugin_id]["enabled"] = True
            return {
                "id": self.plugin_id,
                "status": "stopped",
                "health": "stopped",
                "enabled": True,
            }
        if path == "/api/v1/admin/capabilities/manifest":
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
                    if self.capability_is_active()
                    else []
                ),
            }
        if path == "/api/v1/runs" and method == "POST":
            assert body is not None
            assert body["mode"] == self.expected_mode
            assert body.get("direct_model") == self.expected_direct_model
            assert self.capability_id in str(body["message"])
            assert isinstance(idempotency_key, str)
            self.run_idempotency_keys.append(idempotency_key)
            if self.run_response_lost:
                raise ConnectionError("run submission response was lost")
            return {"id": RUN_ID, "status": "queued", "version": 1}
        if path == f"/api/v1/runs/{RUN_ID}/details":
            if self.cancel_requested:
                self.cancel_detail_polls += 1
            status = (
                "cancelled" if self.cancel_requested and self.cancel_effective else self.run_status
            )
            execution_quiescent = (
                status in {"completed", "failed"} and self.terminal_quiescent
            ) or (
                status == "cancelled"
                and self.cancel_detail_polls > self.cancel_quiescent_after_polls
            )
            details: dict[str, object] = {
                "id": RUN_ID,
                "status": status,
                "version": 2,
                "execution_lease_expires_at": (
                    None if execution_quiescent else "2026-10-02T12:00:00Z"
                ),
            }
            if not self.omit_quiescence_field:
                details["execution_quiescent"] = execution_quiescent
            return details
        if path == f"/api/v1/runs/{RUN_ID}/cancel" and method == "POST":
            self.cancel_requested = True
            return {"id": RUN_ID, "status": "cancelled", "version": 3}
        if path == f"/api/v1/runs/{RUN_ID}/events":
            assert self.capability_is_active()
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
            if self.plugin_id in self.installed_plugins:
                self.installed_plugins[self.plugin_id]["enabled"] = False
            return {"id": self.plugin_id, "enabled": False}
        if path.endswith("/stop"):
            if self.plugin_id in self.installed_plugins:
                self.installed_plugins[self.plugin_id]["status"] = "stopped"
            return {"id": self.plugin_id, "status": "stopped", "health": "stopped"}
        if path.endswith("/uninstall"):
            if not self.residual_plugin:
                self.installed_plugins.pop(self.plugin_id, None)
            return {"status": "uninstalled"}
        if path.startswith("/api/v1/admin/plugins/signing-keys/") and method == "DELETE":
            key_id = path.rsplit("/", 1)[-1]
            if not self.residual_signing_key:
                self.signing_keys.discard(key_id)
            return {"status": "deleted"}
        if path == "/api/v1/admin/plugins" and method == "GET":
            return list(self.installed_plugins.values())
        raise AssertionError(f"unexpected request: {method} {path} {body}")


class PrincipalClient:
    def __init__(
        self,
        backend: FakePluginAcceptanceClient,
        *,
        user_id: str,
        role: str,
        tenant_id: str = "tenant-1",
    ) -> None:
        self.backend = backend
        self.user_id = user_id
        self.role = role
        self.tenant_id = tenant_id
        self.requests: list[tuple[str, str]] = []

    def request_archive(
        self,
        method: str,
        path: str,
        *,
        archive: bytes,
        filename: str,
    ) -> dict[str, object]:
        self.requests.append((method, path))
        return self.backend.request_archive(
            method,
            path,
            archive=archive,
            filename=filename,
        )

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
            return {
                "user_id": self.user_id,
                "tenant_id": self.tenant_id,
                "role": self.role,
            }
        return self.backend.request_json(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
        )


def _operator(client: FakePluginAcceptanceClient) -> PrincipalClient:
    return PrincipalClient(client, user_id=USER_ID, role="operator")


class ApprovalPluginAcceptanceClient(FakePluginAcceptanceClient):
    def __init__(
        self,
        *,
        detail_changes: dict[str, object] | None = None,
        event_changes: dict[str, object] | None = None,
        payload_changes: dict[str, object] | None = None,
        recheck_changes: dict[str, object] | None = None,
        event_state: str = "pending",
        repeated: bool = False,
        approval_response_lost: bool = False,
        post_approval_details_unavailable: bool = False,
        ready_values: tuple[object, ...] = (True,),
        omit_ready: bool = False,
    ) -> None:
        super().__init__()
        self.plugin_id = "trusted-plugin"
        self.capability_id = "calendar.create_event"
        self.detail_changes = detail_changes or {}
        self.event_changes = event_changes or {}
        self.payload_changes = payload_changes or {}
        self.recheck_changes = recheck_changes or {}
        self.event_state = event_state
        self.repeated = repeated
        self.approval_response_lost = approval_response_lost
        self.post_approval_details_unavailable = post_approval_details_unavailable
        self.ready_values = ready_values
        self.omit_ready = omit_ready
        self.approval_reads = 0
        self.details_reads = 0
        self.approval_bodies: list[dict[str, object] | None] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        waiting = not self.approval_bodies or self.repeated or self.approval_response_lost
        if (
            method == "GET"
            and path == f"/api/v1/runs/{RUN_ID}/details"
            and self.approval_bodies
            and self.post_approval_details_unavailable
        ):
            self.requests.append((method, path))
            raise ConnectionError("post-approval details unavailable")
        if (
            method == "GET"
            and path == f"/api/v1/runs/{RUN_ID}/details"
            and waiting
            and not self.cancel_requested
        ):
            self.requests.append((method, path))
            self.details_reads += 1
            details: dict[str, object] = {
                "id": RUN_ID,
                "status": "waiting_approval",
                "approval_kind": "capability_tool",
                "approval_id": "approval-1",
                "version": 2,
                **self.detail_changes,
            }
            if self.details_reads > 1:
                details.update(self.recheck_changes)
            return details
        if method == "GET" and path == f"/api/v1/runs/{RUN_ID}/capability-approvals" and waiting:
            self.requests.append((method, path))
            self.approval_reads += 1
            if self.event_state == "unavailable":
                raise ConnectionError("approval events unavailable")
            event: dict[str, object] = {
                "kind": "approval.requested",
                "run_id": RUN_ID,
                "approval_id": "approval-1",
                "sequence": 1,
                "actor": "plugin",
                "action": "plugin.invoke",
                "reason": "fixture approval",
                "payload": {
                    "plugin_id": self.plugin_id,
                    "capability_id": self.capability_id,
                    "version": 2,
                    **self.payload_changes,
                },
                **self.event_changes,
            }
            items = [] if self.event_state == "missing" else [event]
            if self.event_state == "duplicate":
                items.append(dict(event))
            if self.event_state == "resolved":
                items.append({**event, "kind": "approval.resolved", "sequence": 2})
            if self.event_state == "other_pending":
                items.append({**event, "approval_id": "approval-other", "sequence": 2})
            response: dict[str, object] = {"items": items}
            if not self.omit_ready:
                response["ready"] = self.ready_values[min(self.approval_reads - 1, len(self.ready_values) - 1)]
            return response
        if method == "POST" and path == f"/api/v1/runs/{RUN_ID}/approve-capability":
            self.requests.append((method, path))
            self.approval_bodies.append(body)
            if self.approval_response_lost:
                raise ConnectionError("approval response was lost")
            return {"id": RUN_ID, "status": "queued", "version": 3}
        return super().request_json(method, path, body=body, idempotency_key=idempotency_key)


class ApprovalPollClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


def test_public_plugin_approval_waits_for_ready_then_posts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    clock = ApprovalPollClock()
    monkeypatch.setattr(module, "time", clock)
    backend = ApprovalPluginAcceptanceClient(ready_values=(False, False, False, True))
    details = module._poll_run(
        backend, RUN_ID, expected_plugin_id=backend.plugin_id,
        expected_capability_id=backend.capability_id, wait_seconds=2, poll_interval_seconds=0.25,
    )
    assert details["status"] == "completed"
    assert clock.sleeps == [0.25, 0.25, 0.25, 0.25]
    assert backend.approval_bodies == [{"approval_id": "approval-1", "version": 2}]
    assert backend.requests == [
        ("GET", f"/api/v1/runs/{RUN_ID}/details"),
        ("GET", f"/api/v1/runs/{RUN_ID}/capability-approvals"),
    ] * 4 + [
        ("GET", f"/api/v1/runs/{RUN_ID}/details"),
        ("POST", f"/api/v1/runs/{RUN_ID}/approve-capability"),
        ("GET", f"/api/v1/runs/{RUN_ID}/details"),
    ]


def test_public_plugin_approval_not_ready_times_out_without_post_or_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    clock = ApprovalPollClock()
    monkeypatch.setattr(module, "time", clock)
    backend = ApprovalPluginAcceptanceClient(ready_values=(False,))
    with pytest.raises(RuntimeError, match="did not reach a terminal status") as error:
        module._poll_run(
            backend, RUN_ID, expected_plugin_id=backend.plugin_id,
            expected_capability_id=backend.capability_id, wait_seconds=0.5, poll_interval_seconds=0.25,
        )
    assert not isinstance(error.value, module.FrozenPluginApproval)
    assert clock.sleeps == [0.25, 0.25]
    assert backend.approval_reads == 3
    assert backend.approval_bodies == []
    assert all(method == "GET" for method, _ in backend.requests)


@pytest.mark.parametrize("ready,omit", [(None, True), (None, False), (0, False), (1, False),
                                        ("true", False), ("false", False), ([], False), ({}, False)])
def test_public_plugin_approval_missing_or_nonbool_ready_freezes(
    ready: object, omit: bool,
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(ready_values=(ready,), omit_ready=omit)
    with pytest.raises(module.FrozenPluginApproval, match="readiness"):
        module._poll_run(
            backend, RUN_ID, expected_plugin_id=backend.plugin_id,
            expected_capability_id=backend.capability_id, wait_seconds=0, poll_interval_seconds=0,
        )
    assert backend.approval_bodies == []
    assert all(method == "GET" for method, _ in backend.requests)


@pytest.mark.parametrize("ready", [False, True])
def test_verify_public_plugin_approval_returns_validated_readiness(ready: bool) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(ready_values=(ready,))
    response = backend.request_json("GET", f"/api/v1/runs/{RUN_ID}/capability-approvals")
    assert module._verify_plugin_approval(
        response, run_id=RUN_ID, approval_id="approval-1", version=2,
        plugin_id=backend.plugin_id, capability_id=backend.capability_id,
    ) is ready


@pytest.mark.parametrize("ready", [False, None])
def test_public_plugin_approval_checks_binding_before_readiness(ready: object) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(
        ready_values=(ready,), payload_changes={"version": 1},
    )
    with pytest.raises(module.FrozenPluginApproval, match="capability binding"):
        module._poll_run(
            backend, RUN_ID, expected_plugin_id=backend.plugin_id,
            expected_capability_id=backend.capability_id, wait_seconds=0, poll_interval_seconds=0,
        )
    assert backend.approval_bodies == []


def test_public_plugin_approval_requires_exact_binding(tmp_path: Path) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient()
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="bound-approval",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )
    assert report["acceptance_complete"] is True
    assert backend.approval_bodies == [{"approval_id": "approval-1", "version": 2}]
    assert backend.details_reads >= 2
    assert report["source_basis"] == "controlled_fixture"
    assert report["generated_source_acceptance"] is False


@pytest.mark.parametrize(
    "kind",
    ["self_repair", "temporary_agent_creation", "evolution_creation", "schedule_creation",
     "openclaw_operation", "project_preflight", None,
     "secret://do-not-export", {"private": "value"}],
)
def test_separate_run_approval_freezes_without_any_approval_or_cleanup(
    tmp_path: Path, kind: object,
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(detail_changes={"approval_kind": kind})
    report = module.run_real_user_plugin_acceptance(
        backend, operator_client=_operator(backend), execution_id="separate-approval",
        package_dir=tmp_path, wait_seconds=0, poll_interval_seconds=0,
    )
    expected_kind = kind if isinstance(kind, str) and not kind.startswith("secret:") else "unknown"
    assert report["acceptance_complete"] is False
    assert report["run_cleanup_blocked"] is True
    assert report["cleanup_state"] == "frozen_waiting_for_separate_approval"
    assert report["recovery"]["reason"] == "non_capability_approval_requires_review"
    assert report["recovery"]["approval_kind"] == expected_kind
    assert report["recovery"]["frozen"] is True
    assert report["recovery"]["resubmission_allowed"] is False
    assert all(step["method"] == "GET" for step in report["recovery"]["steps"])
    assert backend.approval_bodies == []
    assert backend.approval_reads == 0
    assert not any(path.endswith(("/cancel", "/disable", "/stop", "/uninstall"))
                   or method == "DELETE" for method, path in backend.requests)
    serialized = json.dumps(report)
    assert "secret://do-not-export" not in serialized
    assert '"private"' not in serialized


def test_approval_kind_rechecked_before_capability_approval(tmp_path: Path) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(
        detail_changes={"approval_kind": "capability_tool"},
        recheck_changes={"approval_kind": "self_repair", "approval_id": None},
    )
    report = module.run_real_user_plugin_acceptance(
        backend, operator_client=_operator(backend), execution_id="changed-kind",
        package_dir=tmp_path, wait_seconds=0, poll_interval_seconds=0,
    )
    assert report["cleanup_state"] == "frozen_waiting_for_separate_approval"
    assert report["recovery"]["approval_kind"] == "self_repair"
    assert backend.approval_reads == 1
    assert backend.approval_bodies == []


def test_explicit_capability_approval_kind_preserves_bound_approval(tmp_path: Path) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(detail_changes={"approval_kind": "capability_tool"})
    report = module.run_real_user_plugin_acceptance(
        backend, operator_client=_operator(backend), execution_id="explicit-kind",
        package_dir=tmp_path, wait_seconds=0, poll_interval_seconds=0,
    )
    assert report["acceptance_complete"] is True
    assert backend.approval_bodies == [{"approval_id": "approval-1", "version": 2}]


def test_missing_approval_kind_is_not_automatic_approval_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient()
    original = backend.request_json

    def request_json(method: str, path: str, **kwargs: Any) -> dict[str, object] | list[object]:
        value = original(method, path, **kwargs)
        if path.endswith("/details") and isinstance(value, dict):
            value.pop("approval_kind", None)
        return value

    monkeypatch.setattr(backend, "request_json", request_json)
    report = module.run_real_user_plugin_acceptance(
        backend, operator_client=_operator(backend), execution_id="missing-kind",
        package_dir=tmp_path, wait_seconds=0, poll_interval_seconds=0,
    )
    assert report["acceptance_complete"] is False
    assert report["cleanup_state"] == "frozen_waiting_for_separate_approval"
    assert report["recovery"]["approval_kind"] == "unknown"
    assert backend.approval_reads == 0
    assert backend.approval_bodies == []
    assert not any(path.endswith(("/cancel", "/disable", "/stop", "/uninstall"))
                   or method == "DELETE" for method, path in backend.requests)


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("detail", {"id": "foreign-run"}),
        ("detail", {"id": None}),
        ("detail", {"approval_id": None}),
        ("detail", {"approval_id": ""}),
        ("detail", {"approval_id": "   "}),
        ("detail", {"version": None}),
        ("detail", {"version": True}),
        ("detail", {"version": 0}),
        ("detail", {"version": "2"}),
        ("event", {"run_id": "foreign-run"}),
        ("event", {"run_id": None}),
        ("event", {"approval_id": "foreign-approval"}),
        ("event", {"approval_id": None}),
        ("event", {"kind": "tool.requested"}),
        ("payload", {"plugin_id": "foreign-plugin"}),
        ("payload", {"plugin_id": None}),
        ("payload", {"capability_id": "foreign-capability"}),
        ("payload", {"capability_id": None}),
        ("payload", {"version": 1}),
        ("payload", {"version": None}),
        ("payload", {"version": True}),
    ],
)
def test_unbound_public_plugin_approval_freezes_without_writes(
    tmp_path: Path, section: str, changes: dict[str, object]
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(
        detail_changes=changes if section == "detail" else None,
        event_changes=changes if section == "event" else None,
        payload_changes=changes if section == "payload" else None,
    )
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="unbound-approval",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )
    assert report["acceptance_complete"] is False
    assert report["cleanup_state"] == "frozen_waiting_for_approval_binding"
    assert report["run_cleanup_blocked"] is True
    assert report["recovery"]["frozen"] is True
    assert all(step["method"] == "GET" for step in report["recovery"]["steps"])
    assert backend.approval_bodies == []
    assert not backend.cancel_requested
    assert backend.capability_is_active()
    assert backend.signing_keys


@pytest.mark.parametrize(
    "event_state", ["missing", "duplicate", "resolved", "other_pending", "unavailable"]
)
def test_uncertain_public_plugin_approval_events_freeze(tmp_path: Path, event_state: str) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(event_state=event_state)
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="uncertain-approval",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )
    assert report["cleanup_state"] == "frozen_waiting_for_approval_binding"
    assert backend.approval_bodies == []
    assert not backend.cancel_requested
    assert backend.capability_is_active()


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 3},
        {"approval_id": "approval-other"},
        {"id": "foreign-run"},
        {"status": "running"},
    ],
)
def test_public_plugin_approval_rechecks_binding_before_post(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(recheck_changes=changes)
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="changed-approval",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )
    assert report["cleanup_state"] == "frozen_waiting_for_approval_binding"
    assert backend.approval_bodies == []
    assert not backend.cancel_requested


@pytest.mark.parametrize("response_lost", [False, True])
def test_public_plugin_approval_never_replays_or_cleans_up_unknown_approval(
    tmp_path: Path, response_lost: bool
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(
        repeated=not response_lost, approval_response_lost=response_lost
    )
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="once-only-approval",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )
    assert report["acceptance_complete"] is False
    assert report["cleanup_state"] == "frozen_waiting_for_approval_binding"
    assert backend.approval_bodies == [{"approval_id": "approval-1", "version": 2}]
    assert not backend.cancel_requested
    assert backend.capability_is_active()


def test_public_plugin_approval_freezes_when_result_cannot_be_read(tmp_path: Path) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(post_approval_details_unavailable=True)
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="unreadable-approval-result",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )
    assert report["cleanup_state"] == "frozen_waiting_for_approval_binding"
    assert backend.approval_bodies == [{"approval_id": "approval-1", "version": 2}]
    assert not backend.cancel_requested
    assert backend.capability_is_active()


def test_real_user_plugin_acceptance_separates_admin_and_operator_paths(
    tmp_path: Path,
) -> None:
    module = _load_script()
    backend = FakePluginAcceptanceClient()
    admin = PrincipalClient(
        backend,
        user_id=ADMIN_USER_ID,
        role="admin",
    )
    operator = PrincipalClient(backend, user_id=USER_ID, role="operator")

    report = module.run_real_user_plugin_acceptance(
        admin,
        operator_client=operator,
        execution_id="split-identity",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "passed"
    assert report["operator_user_id"] == USER_ID
    assert ("POST", "/api/v1/runs") in operator.requests
    assert ("GET", f"/api/v1/runs/{RUN_ID}/details") in operator.requests
    assert ("GET", f"/api/v1/runs/{RUN_ID}/events") in operator.requests
    assert not any(path.startswith("/api/v1/admin/") for _, path in operator.requests)
    assert not any(path.startswith("/api/v1/runs") for _, path in admin.requests)
    assert any(path.startswith("/api/v1/admin/audit?") for _, path in admin.requests)


def test_real_user_plugin_acceptance_rejects_text_only_mode_before_install(
    tmp_path: Path,
) -> None:
    module = _load_script()
    backend = FakePluginAcceptanceClient(
        expected_mode="direct",
        expected_direct_model="deepseek",
    )

    with pytest.raises(ValueError, match="tool-capable"):
        module.run_real_user_plugin_acceptance(
            PrincipalClient(backend, user_id=ADMIN_USER_ID, role="admin"),
            operator_client=_operator(backend),
            execution_id="direct-model",
            package_dir=tmp_path,
            wait_seconds=1,
            poll_interval_seconds=0,
            run_mode="direct",
            direct_model="deepseek",
        )
    assert not backend.requests


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
    validate({"text": package.input_text}, capability["input_schema"])
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
    validate(result, capability["output_schema"])
    assert result["acceptance_nonce"] == package.nonce
    assert result["character_count"] == len(package.input_text)


@pytest.mark.parametrize("changed_field", ["text", "character_count"])
def test_acceptance_fixture_rejects_changed_text_or_character_count(
    tmp_path: Path, changed_field: str
) -> None:
    module = _load_script()
    package = module.build_acceptance_plugin(tmp_path, execution_id="schema-binding")
    with zipfile.ZipFile(package.archive_path) as archive:
        capability = json.loads(archive.read("plugin.json"))["capabilities"][0]
    if changed_field == "text":
        schema = capability["input_schema"]
        valid = {"text": package.input_text}
        invalid = {"text": package.input_text[:-1] + "!"}
    else:
        schema = capability["output_schema"]
        valid = {
            "ok": True,
            "acceptance_nonce": package.nonce,
            "character_count": len(package.input_text),
        }
        invalid = {**valid, "character_count": len(package.input_text) + 1}
    validate(valid, schema)
    with pytest.raises(ValidationError):
        validate(invalid, schema)


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
        operator_client=_operator(client),
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
    assert client.installed_plugins == {}
    assert client.capability_is_active() is False


def test_real_user_plugin_acceptance_reuses_execution_id_with_fresh_run_nonce(
    tmp_path: Path,
) -> None:
    module = _load_script()
    first_client = FakePluginAcceptanceClient()
    second_client = FakePluginAcceptanceClient()

    first = module.run_real_user_plugin_acceptance(
        first_client,
        operator_client=_operator(first_client),
        execution_id="same-execution",
        package_dir=tmp_path / "first",
        wait_seconds=1,
        poll_interval_seconds=0,
    )
    second = module.run_real_user_plugin_acceptance(
        second_client,
        operator_client=_operator(second_client),
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
        operator_client=_operator(client),
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
    assert any(
        path.startswith("/api/v1/admin/plugins/signing-keys/") for _, path in client.requests
    )


def test_real_user_plugin_acceptance_cancels_nonterminal_run_before_cleanup(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(run_status="queued")

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
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
    assert client.installed_plugins == {}
    assert client.capability_is_active() is False


def test_real_user_plugin_acceptance_waits_for_worker_quiescence_after_cancel(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(
        run_status="queued",
        cancel_quiescent_after_polls=2,
    )

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
        execution_id="cancel-delayed-quiescence",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert client.cancel_detail_polls >= 3
    assert "verify_run_quiescent" in report["cleanup"]["completed"]
    assert client.installed_plugins == {}


def test_real_user_plugin_acceptance_reports_executable_recovery_when_worker_stays_active(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(
        run_status="queued",
        cancel_quiescent_after_polls=99,
    )

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
        execution_id="cancel-active-worker",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["run_cleanup_blocked"] is True
    assert client.installed_plugins
    assert client.capability_is_active() is True
    recovery = report["recovery"]
    assert recovery["required"] is True
    assert recovery["run_id"] == RUN_ID
    assert recovery["retry_after_seconds"] >= 1
    assert recovery["steps"][0] == {
        "method": "GET",
        "path": f"/api/v1/runs/{RUN_ID}/details",
        "require": {"execution_quiescent": True},
    }
    assert any(step["path"].endswith("/uninstall") for step in recovery["steps"])
    assert {
        "method": "GET",
        "path": "/api/v1/admin/plugins",
        "require_absent": {"id": report["plugin_id"]},
    } in recovery["steps"]
    assert {
        "method": "GET",
        "path": "/api/v1/admin/capabilities/manifest",
        "require_absent": {"capability_id": report["capability_id"]},
    } in recovery["steps"]
    assert {
        "method": "GET",
        "path": "/api/v1/admin/plugins/signing-keys",
        "require_absent": {"key_id": recovery["key_id"]},
    } in recovery["steps"]


@pytest.mark.parametrize("status_code", [404, 410])
def test_run_disappearance_never_bypasses_execution_quiescence(status_code: int) -> None:
    module = _load_script()

    class MissingRunError(RuntimeError):
        status_code: int

        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.status_code = status_code

    class MissingRunClient:
        def request_json(self, *_args: object, **_kwargs: object) -> object:
            raise MissingRunError("run details unavailable")

    with pytest.raises(RuntimeError, match="run details unavailable"):
        module._wait_for_run_quiescence(
            MissingRunClient(),
            RUN_ID,
            wait_seconds=0,
            poll_interval_seconds=0,
        )


@pytest.mark.parametrize(
    "cleanup_state",
    [
        "deferred_waiting_for_execution_quiescence",
        "deferred_waiting_for_run_identity",
        "frozen_waiting_for_approval_binding",
    ],
)
def test_temporary_operator_cleanup_error_preserves_deferred_recovery_state(
    cleanup_state: str,
) -> None:
    module = _load_script()
    report = {
        "status": "failed",
        "acceptance_complete": False,
        "cleanup_state": cleanup_state,
        "recovery": {"required": True, "steps": [{"method": "GET", "path": "/details"}]},
        "errors": [],
    }

    module._record_temporary_operator_cleanup(
        report,
        completed=[],
        errors=["delete_user: execution still active"],
    )

    assert report["cleanup_state"] == cleanup_state
    recovery = report["recovery"]
    assert isinstance(recovery, dict)
    assert recovery["required"] is True


@pytest.mark.parametrize("omit_field", [False, True])
def test_real_user_plugin_acceptance_refuses_completed_run_without_quiescence_proof(
    tmp_path: Path,
    omit_field: bool,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(
        run_status="completed",
        terminal_quiescent=False,
        omit_quiescence_field=omit_field,
    )

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
        execution_id="completed-worker-active",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["cleanup_state"] == "deferred_waiting_for_execution_quiescence"
    assert not any(path.endswith("/uninstall") for _, path in client.requests)
    assert client.installed_plugins


def test_real_user_plugin_acceptance_reports_manifest_unavailable_reason(tmp_path: Path) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(
        capability_available=False,
        availability_reason="plugin_package_adapter_unavailable",
    )

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
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
        operator_client=_operator(client),
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
        operator_client=_operator(client),
        execution_id="ambiguous-install",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert any(path.endswith("/uninstall") for _, path in client.requests)
    assert any(
        path.startswith("/api/v1/admin/plugins/signing-keys/") for _, path in client.requests
    )


def test_real_user_plugin_acceptance_fails_when_signing_key_remains(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(residual_signing_key=True)

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
        execution_id="residual-key",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["acceptance_complete"] is False
    assert any("signing key remains registered" in error for error in report["errors"])


def test_real_user_plugin_acceptance_fails_when_plugin_remains_installed(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(residual_plugin=True)

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
        execution_id="residual-plugin",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert client.installed_plugins
    assert any("plugin remains installed" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (401, "invalid_token"),
        (403, "permission_denied"),
        (409, "vibe_coding_disabled"),
        (409, "execution_backend_unavailable"),
        (409, "vibe_coding_unavailable"),
        (409, "conversation_archived"),
        (422, "request_validation"),
    ],
)
def test_explicit_side_effect_free_run_submission_rejection_allows_resource_cleanup(
    tmp_path: Path,
    status_code: int,
    error_code: str,
) -> None:
    module = _load_script()
    backend = FakePluginAcceptanceClient()
    admin = PrincipalClient(backend, user_id=ADMIN_USER_ID, role="admin")

    class RejectedOperator(PrincipalClient):
        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            if method == "POST" and path == "/api/v1/runs":
                raise module.AcceptanceHTTPError(
                    method=method,
                    path=path,
                    status_code=status_code,
                    response_body=json.dumps(
                        {
                            "error": {
                                "code": error_code,
                                "message": "request rejected before run creation",
                            }
                        }
                    ),
                )
            return super().request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            )

    operator = RejectedOperator(backend, user_id=USER_ID, role="operator")

    report = module.run_real_user_plugin_acceptance(
        admin,
        operator_client=operator,
        execution_id="explicit-rejection",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["cleanup_state"] == "complete"
    assert report["run_cleanup_blocked"] is False
    assert report["recovery"] == {"required": False}
    assert backend.installed_plugins == {}
    assert backend.signing_keys == set()


@pytest.mark.parametrize(
    ("status_code", "response_body"),
    [
        (
            408,
            '{"error":{"code":"request_timeout","message":"request timed out"}}',
        ),
        (
            409,
            '{"error":{"code":"idempotency_conflict","message":"conflict"}}',
        ),
        (
            418,
            '{"error":{"code":"unknown_client_error","message":"unknown"}}',
        ),
        (422, '{"error":"request rejected"}'),
    ],
)
def test_ambiguous_run_submission_http_error_defers_cleanup_fail_closed(
    tmp_path: Path,
    status_code: int,
    response_body: str,
) -> None:
    module = _load_script()
    backend = FakePluginAcceptanceClient()
    admin = PrincipalClient(backend, user_id=ADMIN_USER_ID, role="admin")

    class AmbiguousOperator(PrincipalClient):
        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            if method == "POST" and path == "/api/v1/runs":
                raise module.AcceptanceHTTPError(
                    method=method,
                    path=path,
                    status_code=status_code,
                    response_body=response_body,
                )
            return super().request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            )

    operator = AmbiguousOperator(backend, user_id=USER_ID, role="operator")

    report = module.run_real_user_plugin_acceptance(
        admin,
        operator_client=operator,
        execution_id=f"ambiguous-rejection-{status_code}",
        package_dir=tmp_path,
        wait_seconds=1,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert report["cleanup_state"] == "deferred_waiting_for_run_identity"
    assert report["run_cleanup_blocked"] is True
    assert report["recovery"]["reason"] == "run_submission_result_unknown"
    assert report["recovery"]["frozen"] is True
    assert all(step["method"] == "GET" for step in report["recovery"]["steps"])
    assert backend.installed_plugins
    assert backend.signing_keys
    assert not any(path.endswith("/uninstall") for _, path in admin.requests)


@pytest.mark.parametrize("status_code", [404, 410])
def test_wait_for_run_quiescence_rejects_missing_or_gone_run(status_code: int) -> None:
    module = _load_script()

    class RunStatusError(RuntimeError):
        def __init__(self) -> None:
            self.status_code = status_code
            super().__init__(f"run unavailable: {status_code}")

    class GoneRunClient:
        def request_json(self, *_args: object, **_kwargs: object) -> object:
            raise RunStatusError

    with pytest.raises(RuntimeError, match=f"run unavailable: {status_code}"):
        module._wait_for_run_quiescence(
            GoneRunClient(),
            RUN_ID,
            wait_seconds=0,
            poll_interval_seconds=0,
        )


class CliAdminClient(PrincipalClient):
    def __init__(
        self,
        backend: FakePluginAcceptanceClient,
        *,
        create_response_lost: bool = False,
        create_missing_id: bool = False,
        create_response_overrides: dict[str, object] | None = None,
        delete_fails: bool = False,
        delete_residual: bool = False,
    ) -> None:
        super().__init__(backend, user_id=ADMIN_USER_ID, role="admin")
        self.temporary_users: dict[str, dict[str, object]] = {}
        self.create_response_lost = create_response_lost
        self.create_missing_id = create_missing_id
        self.create_response_overrides = create_response_overrides or {}
        self.delete_fails = delete_fails
        self.delete_residual = delete_residual

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        if path == "/api/v1/users" and method == "POST":
            self.requests.append((method, path))
            assert body is not None and body["role"] == "operator"
            created = {
                "id": USER_ID,
                "username": body["username"],
                "role": "operator",
                "tenant_id": "tenant-1",
            }
            self.temporary_users[USER_ID] = created
            if self.create_response_lost:
                raise ConnectionError("create response was lost")
            response = {**created, **self.create_response_overrides}
            if self.create_missing_id:
                response.pop("id")
            return response
        if path == "/api/v1/users" and method == "GET":
            self.requests.append((method, path))
            return list(self.temporary_users.values())
        if path == f"/api/v1/users/{USER_ID}" and method == "DELETE":
            self.requests.append((method, path))
            if self.delete_fails:
                raise ConnectionError("delete failed")
            if not self.delete_residual:
                self.temporary_users.pop(USER_ID, None)
            return {}
        return super().request_json(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
        )


class CliClientFactory:
    def __init__(
        self,
        *,
        create_response_lost: bool = False,
        create_missing_id: bool = False,
        create_response_overrides: dict[str, object] | None = None,
        delete_fails: bool = False,
        delete_residual: bool = False,
        run_response_lost: bool = False,
        operator_user_id: str = USER_ID,
        operator_tenant_id: str = "tenant-1",
        backend: FakePluginAcceptanceClient | None = None,
    ) -> None:
        self.backend = backend or FakePluginAcceptanceClient(run_response_lost=run_response_lost)
        self.admin = CliAdminClient(
            self.backend,
            create_response_lost=create_response_lost,
            create_missing_id=create_missing_id,
            create_response_overrides=create_response_overrides,
            delete_fails=delete_fails,
            delete_residual=delete_residual,
        )
        self.operator = PrincipalClient(
            self.backend,
            user_id=operator_user_id,
            role="operator",
            tenant_id=operator_tenant_id,
        )
        self.credentials: list[tuple[str | None, str | None, str | None]] = []

    def __call__(
        self,
        *,
        base_url: str,
        bearer_token: str = "",
        timeout: float = 20.0,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
    ) -> PrincipalClient:
        del base_url, bearer_token, timeout
        self.credentials.append((username, password, tenant_id))
        return self.admin if len(self.credentials) == 1 else self.operator


def test_cli_uses_independent_operator_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    factory = CliClientFactory()
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)

    exit_code = module.main(
        [
            "--operator-username",
            "operator-user",
            "--operator-password",
            "operator-password",
            "--operator-tenant-id",
            "tenant-1",
            "--execution-id",
            "explicit-operator",
            "--output",
            str(tmp_path / "report.json"),
            "--poll-interval",
            "0",
        ]
    )

    assert exit_code == 0
    assert factory.credentials == [
        ("admin-user", "admin-password", "tenant-1"),
        ("operator-user", "operator-password", "tenant-1"),
    ]
    assert ("POST", "/api/v1/runs") in factory.operator.requests
    assert ("POST", "/api/v1/users") not in factory.admin.requests


def test_cli_creates_and_removes_temporary_operator_when_credentials_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    factory = CliClientFactory()
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_TENANT_ID", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)

    exit_code = module.main(
        [
            "--execution-id",
            "temporary-operator",
            "--output",
            str(tmp_path / "report.json"),
            "--poll-interval",
            "0",
        ]
    )

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert ("POST", "/api/v1/users") in factory.admin.requests
    assert ("DELETE", f"/api/v1/users/{USER_ID}") in factory.admin.requests
    assert factory.admin.temporary_users == {}
    assert report["operator_source"] == "temporary"
    assert report["temporary_operator_cleanup"] == {
        "completed": ["delete_user", "verify_user_removed"],
        "errors": [],
    }
    plugin_path = f"/api/v1/admin/plugins/{report['plugin_id']}"
    cleanup_start = factory.admin.requests.index(("POST", f"{plugin_path}/disable"))
    assert factory.admin.requests[cleanup_start:] == [
        ("POST", f"{plugin_path}/disable"),
        ("POST", f"{plugin_path}/stop"),
        ("POST", f"{plugin_path}/uninstall"),
        ("GET", "/api/v1/admin/plugins"),
        ("GET", "/api/v1/admin/capabilities/manifest"),
        ("DELETE", f"/api/v1/admin/plugins/signing-keys/plugin-uat-key-{report['plugin_id'][11:]}"),
        ("GET", "/api/v1/admin/plugins/signing-keys"),
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/users"),
        ("DELETE", f"/api/v1/users/{USER_ID}"),
        ("GET", "/api/v1/users"),
    ]


@pytest.mark.parametrize(
    ("stage", "outcome"),
    [
        (stage, outcome)
        for stage in ("disable", "stop", "uninstall", "delete_signing_key")
        for outcome in ("error", "response_lost")
    ]
    + [
        (stage, outcome)
        for stage in ("verify_plugin", "verify_capability", "verify_signing_key")
        for outcome in ("error", "residual")
    ],
)
def test_cli_cleanup_stops_after_unproven_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    outcome: str,
) -> None:
    module = _load_script()
    backend = FakePluginAcceptanceClient()
    factory = CliClientFactory(backend=backend)
    original_request = backend.request_json
    cleanup_started = False

    def request_json(
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        nonlocal cleanup_started
        plugin_path = f"/api/v1/admin/plugins/{backend.plugin_id}"
        if (method, path) == ("POST", f"{plugin_path}/disable"):
            cleanup_started = True
        targets = {
            "disable": ("POST", f"{plugin_path}/disable"),
            "stop": ("POST", f"{plugin_path}/stop"),
            "uninstall": ("POST", f"{plugin_path}/uninstall"),
            "verify_plugin": ("GET", "/api/v1/admin/plugins"),
            "verify_capability": ("GET", "/api/v1/admin/capabilities/manifest"),
            "delete_signing_key": (
                "DELETE",
                f"/api/v1/admin/plugins/signing-keys/plugin-uat-key-{backend.plugin_id[11:]}",
            ),
            "verify_signing_key": ("GET", "/api/v1/admin/plugins/signing-keys"),
        }
        fault = cleanup_started and (method, path) == targets[stage]
        if fault and outcome == "error":
            backend.requests.append((method, path))
            raise ConnectionError(f"{stage} failed")
        result = original_request(method, path, body=body, idempotency_key=idempotency_key)
        if fault and outcome == "response_lost":
            raise TimeoutError(f"{stage} response was lost")
        if fault and outcome == "residual":
            if stage == "verify_plugin":
                return [{"id": backend.plugin_id}]
            if stage == "verify_capability":
                return {"capabilities": [{"id": backend.capability_id}]}
            return [{"key_id": f"plugin-uat-key-{backend.plugin_id[11:]}"}]
        return result

    monkeypatch.setattr(backend, "request_json", request_json)
    for name in ("USERNAME", "PASSWORD", "TENANT_ID"):
        monkeypatch.delenv(f"AGENT_HUB_ACCEPTANCE_OPERATOR_{name}", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"

    assert module.main(["--output", str(report_path), "--poll-interval", "0"]) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["acceptance_complete"] is False
    assert report["cleanup_state"] == "failed"
    assert report["cleanup"]["errors"]
    assert report["temporary_operator_cleanup"]["completed"] == []
    assert report["temporary_operator_cleanup"]["errors"]
    assert factory.admin.temporary_users
    assert not any(path.startswith("/api/v1/users/") for _, path in factory.admin.requests)
    cleanup_start = next(
        i for i, (_, path) in enumerate(backend.requests) if path.endswith("/disable")
    )
    cleanup_requests = backend.requests[cleanup_start:]
    assert (
        len(cleanup_requests)
        == {
            "disable": 1,
            "stop": 2,
            "uninstall": 3,
            "verify_plugin": 4,
            "verify_capability": 5,
            "delete_signing_key": 6,
            "verify_signing_key": 7,
        }[stage]
    )
    if stage not in {"delete_signing_key", "verify_signing_key"}:
        assert backend.signing_keys
    assert len(backend.run_idempotency_keys) == 1
    assert report["recovery"]["required"] is True
    assert report["recovery"]["frozen"] is True
    assert report["recovery"]["resubmission_allowed"] is False
    assert all(step["method"] == "GET" for step in report["recovery"]["steps"])


def test_cleanup_recovery_preserves_plugin_verification_before_key_deletion(tmp_path: Path) -> None:
    module = _load_script()
    backend = FakePluginAcceptanceClient(terminal_quiescent=False)
    report = module.run_real_user_plugin_acceptance(
        backend,
        operator_client=_operator(backend),
        execution_id="cleanup-order",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )
    steps = report["recovery"]["steps"]
    assert [(step["method"], step["path"]) for step in steps] == [
        ("GET", f"/api/v1/runs/{RUN_ID}/details"),
        ("POST", f"/api/v1/admin/plugins/{report['plugin_id']}/disable"),
        ("POST", f"/api/v1/admin/plugins/{report['plugin_id']}/stop"),
        ("POST", f"/api/v1/admin/plugins/{report['plugin_id']}/uninstall"),
        ("GET", "/api/v1/admin/plugins"),
        ("GET", "/api/v1/admin/capabilities/manifest"),
        ("DELETE", f"/api/v1/admin/plugins/signing-keys/{report['recovery']['key_id']}"),
        ("GET", "/api/v1/admin/plugins/signing-keys"),
    ]


def test_cli_defers_all_cleanup_when_run_submission_response_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    factory = CliClientFactory(run_response_lost=True)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_TENANT_ID", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"

    exit_code = module.main(
        [
            "--execution-id",
            "ambiguous-run-submission",
            "--output",
            str(report_path),
            "--poll-interval",
            "0",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["run_id"] is None
    assert report["run_cleanup_blocked"] is True
    assert report["cleanup_state"] == "deferred_waiting_for_run_identity"
    assert report["cleanup"]["completed"] == []
    assert factory.backend.capability_is_active() is True
    assert factory.backend.signing_keys
    assert factory.admin.temporary_users
    assert not any(path.endswith("/uninstall") for _, path in factory.admin.requests)
    assert not any(
        method == "DELETE" and path.startswith("/api/v1/admin/plugins/signing-keys/")
        for method, path in factory.admin.requests
    )
    assert not any(
        method == "DELETE" and path.startswith("/api/v1/users/")
        for method, path in factory.admin.requests
    )
    recovery = report["recovery"]
    assert recovery["required"] is True
    assert recovery["reason"] == "run_submission_result_unknown"
    assert recovery["run_id"] is None
    assert recovery["frozen"] is True
    assert recovery["steps"] == []
    lookup = recovery["identity_lookup"]
    assert lookup["read_only"] is True
    assert lookup["available"] is False
    assert lookup["idempotency_key"] == factory.backend.run_idempotency_keys[0]
    assert lookup["run_request"]["mode"] == "dispatch"
    assert report["capability_id"] in lookup["run_request"]["message"]
    assert factory.backend.run_idempotency_keys == [lookup["idempotency_key"]]


@pytest.mark.parametrize("response_lost", [False, True])
def test_cli_preserves_temporary_operator_and_read_only_recovery_on_frozen_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response_lost: bool
) -> None:
    module = _load_script()
    backend = ApprovalPluginAcceptanceClient(
        payload_changes={} if response_lost else {"plugin_id": "foreign-plugin"},
        approval_response_lost=response_lost,
    )
    factory = CliClientFactory(backend=backend)
    for name in ("USERNAME", "PASSWORD", "TENANT_ID"):
        monkeypatch.delenv(f"AGENT_HUB_ACCEPTANCE_OPERATOR_{name}", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"
    assert module.main(["--output", str(report_path), "--poll-interval", "0"]) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["cleanup_state"] == "frozen_waiting_for_approval_binding"
    assert report["recovery"]["frozen"] is True
    assert {step["method"] for step in report["recovery"]["steps"]} == {"GET"}
    assert factory.admin.temporary_users
    assert not any(method == "DELETE" for method, _ in factory.admin.requests)
    assert not backend.cancel_requested
    assert backend.capability_is_active()
    assert len(backend.approval_bodies) == int(response_lost)


def test_cli_removes_temporary_operator_when_operator_client_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    factory = CliClientFactory()

    def client_factory(
        *,
        base_url: str,
        bearer_token: str = "",
        timeout: float = 20.0,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
    ) -> PrincipalClient:
        if factory.credentials:
            raise RuntimeError("operator login client failed")
        return factory(
            base_url=base_url,
            bearer_token=bearer_token,
            timeout=timeout,
            username=username,
            password=password,
            tenant_id=tenant_id,
        )

    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", client_factory)

    with pytest.raises(RuntimeError, match="operator login client failed"):
        module.main(["--execution-id", "operator-client-failure"])

    assert ("DELETE", f"/api/v1/users/{USER_ID}") in factory.admin.requests
    assert factory.admin.temporary_users == {}


@pytest.mark.parametrize("create_missing_id", [False, True])
def test_cli_recovers_and_removes_operator_when_create_result_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_missing_id: bool,
) -> None:
    module = _load_script()
    factory = CliClientFactory(
        create_response_lost=not create_missing_id,
        create_missing_id=create_missing_id,
    )
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"

    exit_code = module.main(
        [
            "--execution-id",
            "ambiguous-operator-create",
            "--output",
            str(report_path),
            "--poll-interval",
            "0",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert factory.admin.temporary_users == {}
    assert ("DELETE", f"/api/v1/users/{USER_ID}") in factory.admin.requests
    assert report["temporary_operator_cleanup"]["errors"] == []
    assert any("temporary operator creation" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("response_override", "expected_error"),
    [
        ({"username": "unexpected-user"}, "username"),
        ({"role": "admin"}, "role"),
        ({"tenant_id": "tenant-2"}, "tenant"),
    ],
)
def test_cli_rejects_mismatched_temporary_operator_creation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_override: dict[str, object],
    expected_error: str,
) -> None:
    module = _load_script()
    factory = CliClientFactory(create_response_overrides=response_override)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"

    exit_code = module.main(
        [
            "--execution-id",
            "mismatched-create-identity",
            "--output",
            str(report_path),
            "--poll-interval",
            "0",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert factory.admin.temporary_users == {}
    assert any(expected_error in error for error in report["errors"])
    assert ("POST", "/api/v1/runs") not in factory.operator.requests


def test_cli_rejects_operator_login_id_that_differs_from_created_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    factory = CliClientFactory(operator_user_id="33333333-3333-4333-8333-333333333333")
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"

    exit_code = module.main(
        [
            "--execution-id",
            "mismatched-login-identity",
            "--output",
            str(report_path),
            "--poll-interval",
            "0",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert factory.admin.temporary_users == {}
    assert any("operator user id" in error for error in report["errors"])
    assert ("POST", "/api/v1/runs") not in factory.operator.requests


@pytest.mark.parametrize("delete_fails", [True, False])
def test_cli_fails_when_temporary_operator_cannot_be_proven_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_fails: bool,
) -> None:
    module = _load_script()
    factory = CliClientFactory(
        delete_fails=delete_fails,
        delete_residual=not delete_fails,
    )
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        module,
        "_acceptance_credentials_from_env",
        lambda: ("admin-user", "admin-password", "tenant-1"),
    )
    monkeypatch.setattr(module, "UrllibPluginAcceptanceClient", factory)
    report_path = tmp_path / "report.json"

    exit_code = module.main(
        [
            "--execution-id",
            "operator-delete-failure",
            "--output",
            str(report_path),
            "--poll-interval",
            "0",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["temporary_operator_cleanup"]["errors"]
    assert factory.admin.temporary_users


def test_real_user_plugin_acceptance_does_not_uninstall_until_cancel_is_terminal(
    tmp_path: Path,
) -> None:
    module = _load_script()
    client = FakePluginAcceptanceClient(run_status="queued", cancel_effective=False)

    report = module.run_real_user_plugin_acceptance(
        client,
        operator_client=_operator(client),
        execution_id="cancel-not-terminal",
        package_dir=tmp_path,
        wait_seconds=0,
        poll_interval_seconds=0,
    )

    assert report["status"] == "failed"
    assert any("cancel" in error and "quiescent" in error for error in report["errors"])
    assert not any(path.endswith("/uninstall") for _, path in client.requests)
    assert client.installed_plugins
