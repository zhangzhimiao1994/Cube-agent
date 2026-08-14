import asyncio
import io
import json
import sys
import tarfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
    AgentResourceRequest,
    InMemoryAdminResourceService,
    MainAgentConfigRequest,
    MainAgentModelConfig,
    McpServerRequest,
    ModelDeploymentRequest,
    PersistentAdminResourceService,
    RunArtifactResponse,
    RunDetailResponse,
    RunEventResponse,
    SecretCreateRequest,
    SystemSettingsResponse,
    _admin_run_artifact,
    _admin_run_event,
    _mode_error_log_from_run,
    _model_check_failure_details,
    _routing_details,
    _run_debug_from_detail,
)
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.registry import NoCapableDeployment
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from agent_hub.multimodal.generation import (
    MultimediaDailyLimitExceeded,
    MultimediaGenerationExecutor,
)
from agent_hub.multimodal.video_providers import VideoProviderGenerationError
from agent_hub.runs.repository import RunRecord
from agent_hub.scheduler.service import SchedulerService
from agent_hub.scheduler.types import TaskRequest
from agent_hub.security.secrets import SecretReference


class FakeConfigService:
    def __init__(self) -> None:
        self.current: ConfigRevision | None = None
        self.drafts: list[dict[str, object]] = []

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
        assert tenant_id == TENANT_ID
        return self.current

    async def create_draft(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        document: object,
    ) -> ConfigRevision:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        assert isinstance(document, dict)
        self.drafts.append(document)
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=len(self.drafts),
            status=ConfigStatus.DRAFT,
            document=document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )

    async def publish(
        self,
        tenant_id: UUID,
        version: int,
        actor_id: UUID,
    ) -> ConfigRevision:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        document = self.drafts[version - 1]
        self.current = ConfigRevision(
            id=uuid4(),
            tenant_id=tenant_id,
            version=version,
            status=ConfigStatus.PUBLISHED,
            document=document,
            created_by=actor_id,
            created_at=datetime.now(UTC),
        )
        return self.current


class FakeSecretService:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.resolved: list[tuple[UUID, str]] = []

    async def create_or_get(
        self,
        tenant_id: UUID,
        actor_id: UUID,
        plaintext: str,
    ) -> SecretReference:
        assert tenant_id == TENANT_ID
        assert actor_id == ACTOR_ID
        self.values.append(plaintext)
        return SecretReference(tenant_id=tenant_id, secret_id=SECRET_ID)

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        assert tenant_id == TENANT_ID
        assert isinstance(reference, str)
        self.resolved.append((tenant_id, reference))
        return "sk-live"


class FakeModelTransport:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Deployment, ModelRequest, str]] = []

    async def complete(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> ModelResponse:
        self.calls.append((deployment, request, api_key))
        if self.error is not None:
            raise self.error
        return ModelResponse(
            text="agent-hub-model-check-ok",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


class FakeGenerationGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GatewayCompletion(
            response=ModelResponse(text="artifact://generated-media"),
            deployment_id="media_primary_1",
            logical_model=request.logical_model,
            provider_id="minimax",
            provider_model="minimax/MiniMax-Hailuo-02",
        )


def test_system_settings_default_openclaw_is_disabled() -> None:
    settings = SystemSettingsResponse()

    assert settings.vibe_coding_enabled is False
    assert settings.openclaw_enabled is False
    assert settings.openclaw_mode == "ask"
    assert settings.openclaw_allowed_commands == []
    assert settings.model_dump()["vibe_coding_enabled"] is False
    assert settings.model_dump()["openclaw_enabled"] is False
    assert settings.model_dump()["openclaw_mode"] == "ask"
    assert settings.model_dump()["openclaw_allowed_commands"] == []


def test_openclaw_operation_requires_feature_switch() -> None:
    response = client().post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "smoke test OpenClaw approval path",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_disabled"


def test_openclaw_adapters_expose_multisystem_execution_boundary() -> None:
    response = client().get("/api/v1/admin/openclaw/adapters", headers=headers())

    assert response.status_code == 200
    adapters = {
        (adapter["platform"], adapter["kind"]): adapter
        for adapter in response.json()
    }
    assert adapters[("linux", "server_command")] == {
        "platform": "linux",
        "kind": "server_command",
        "target_type": "server",
        "status": "available",
        "execution_host": "agent-hub-server",
        "requires_user_approval": True,
        "supports_read_only": False,
        "description": "Runs exact allowlisted argv commands on the 魔方agent Linux server after approval.",
    }
    assert adapters[("windows", "server_command")]["status"] == "adapter_unavailable"
    assert adapters[("windows", "server_command")]["execution_host"] == "remote-windows-host"
    assert adapters[("macos", "desktop_action")]["status"] == "adapter_unavailable"
    assert adapters[("linux", "screen_read")]["supports_read_only"] is True
    assert adapters[("windows", "file_read")]["requires_user_approval"] is True


def test_openclaw_operation_creates_approval_request_when_enabled() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    response = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "smoke test OpenClaw approval path",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_user_approval"
    assert body["platform"] == "linux"
    assert body["kind"] == "server_command"
    assert body["approval_id"].startswith("openclaw_")
    assert body["requires_user_approval"] is True
    assert body["operation"]["argv"] == ["python", "--version"]
    assert "agent-hub-server" in body["approval_summary"]

    fetched = api.get(f"/api/v1/admin/openclaw/operations/{body['id']}", headers=headers())
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]
    assert fetched.json()["status"] == "waiting_user_approval"

    approved = api.patch(
        f"/api/v1/admin/openclaw/operations/{body['id']}",
        headers=headers(),
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["requires_user_approval"] is False

    repeated = api.patch(
        f"/api/v1/admin/openclaw/operations/{body['id']}",
        headers=headers(),
        json={"decision": "reject"},
    )
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "openclaw_already_resolved"


def test_openclaw_read_only_mode_rejects_write_operations() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "read_only"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    response = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "read-only mode should block command execution plans",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_read_only"


def test_openclaw_execute_requires_approved_operation() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": [sys.executable, "-c", "print('openclaw-api-exec-ok')"],
            "risk_level": "low",
            "reason": "approved execution should be required",
        },
    )
    assert created.status_code == 202

    response = api.post(
        f"/api/v1/admin/openclaw/operations/{created.json()['id']}/execute",
        headers=headers(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_not_approved"


def test_openclaw_auto_review_approves_allowlisted_low_risk_linux_command() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-auto-review-ok')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "auto_review"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "auto review should approve only an allowlisted low-risk probe",
        },
    )

    assert created.status_code == 202
    operation = created.json()
    assert operation["status"] == "approved"
    assert operation["requires_user_approval"] is False

    executed = api.post(f"/api/v1/admin/openclaw/operations/{operation['id']}/execute", headers=headers())
    assert executed.status_code == 200
    assert executed.json()["stdout"].strip() == "openclaw-auto-review-ok"


def test_openclaw_auto_review_keeps_unlisted_command_waiting_for_user_approval() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-auto-review-denied')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "auto_review"
    payload["openclaw_allowed_commands"] = []
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "unlisted command still needs a human approval",
        },
    )

    assert created.status_code == 202
    operation = created.json()
    assert operation["status"] == "waiting_user_approval"
    assert operation["requires_user_approval"] is True

    executed = api.post(f"/api/v1/admin/openclaw/operations/{operation['id']}/execute", headers=headers())
    assert executed.status_code == 409
    assert executed.json()["error"]["code"] == "openclaw_not_approved"


def test_openclaw_execute_denies_approved_unlisted_command() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = []
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": [sys.executable, "-c", "print('openclaw-api-exec-ok')"],
            "risk_level": "low",
            "reason": "approved command should still require an allowlist match",
        },
    )
    operation_id = created.json()["id"]
    assert api.patch(
        f"/api/v1/admin/openclaw/operations/{operation_id}",
        headers=headers(),
        json={"decision": "approve"},
    ).status_code == 200

    response = api.post(f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_command_denied"


def test_openclaw_execute_runs_allowlisted_linux_command() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-api-exec-ok')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "run a bounded smoke command after approval",
        },
    )
    operation_id = created.json()["id"]
    assert api.patch(
        f"/api/v1/admin/openclaw/operations/{operation_id}",
        headers=headers(),
        json={"decision": "approve"},
    ).status_code == 200

    response = api.post(f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert body["operation"]["status"] == "executed"
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "openclaw-api-exec-ok"
    assert body["stderr"] == ""
    assert body["truncated"] is False

    fetched = api.get(f"/api/v1/admin/openclaw/operations/{operation_id}", headers=headers())
    assert fetched.json()["status"] == "executed"
    assert fetched.json()["execution"]["exit_code"] == 0


def test_openclaw_execute_denies_shell_even_when_allowlisted() -> None:
    api = client()
    command = ["bash", "-c", "echo unsafe"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "shell execution must stay blocked",
        },
    )
    operation_id = created.json()["id"]
    assert api.patch(
        f"/api/v1/admin/openclaw/operations/{operation_id}",
        headers=headers(),
        json={"decision": "approve"},
    ).status_code == 200

    response = api.post(f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_command_denied"


def test_openclaw_execute_returns_adapter_unavailable_for_windows_command() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [["cmd", "/c", "echo", "ok"]]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "windows",
            "kind": "server_command",
            "target": "desktop",
            "argv": ["cmd", "/c", "echo", "ok"],
            "risk_level": "low",
            "reason": "windows adapter must not be treated as linux execution",
        },
    )
    operation_id = created.json()["id"]
    assert api.patch(
        f"/api/v1/admin/openclaw/operations/{operation_id}",
        headers=headers(),
        json={"decision": "approve"},
    ).status_code == 200

    response = api.post(f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_adapter_unavailable"


def test_openclaw_execute_uses_configured_remote_windows_adapter() -> None:
    adapter_calls: list[dict[str, object]] = []

    class AdapterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/openclaw/health":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps({
                "status": "ok",
                "platform": "windows",
                "capabilities": ["server_command"],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            adapter_calls.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            payload = json.dumps(
                {
                    "exit_code": 0,
                    "stdout": "windows-adapter-ok\n",
                    "stderr": "",
                    "truncated": False,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), AdapterHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = client()
        secret = api.post(
            "/api/v1/admin/secrets",
            headers=headers(),
            json={"label": "openclaw-windows-adapter", "value": "sk-live"},
        )
        assert secret.status_code == 200
        payload = api.get("/api/v1/admin/settings", headers=headers()).json()
        payload["openclaw_enabled"] = True
        payload["openclaw_mode"] = "ask"
        payload["openclaw_allowed_commands"] = [["whoami"]]
        payload["openclaw_remote_adapters"] = [
            {
                "platform": "windows",
                "target_type": "server",
                "target": "desktop",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "credential_ref": secret.json()["ref"],
            }
        ]
        assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

        adapters = api.get("/api/v1/admin/openclaw/adapters", headers=headers()).json()
        windows_command = next(
            item for item in adapters if item["platform"] == "windows" and item["kind"] == "server_command"
        )
        assert windows_command["status"] == "available"

        session = api.post(
            "/api/v1/admin/openclaw/sessions",
            headers=headers(),
            json={
                "platform": "windows",
                "target_type": "server",
                "target": "desktop",
                "purpose": "keep the configured Windows adapter bounded to this host",
            },
        )
        assert session.status_code == 201
        assert session.json()["status"] == "active"

        created = api.post(
            "/api/v1/admin/openclaw/operations",
            headers=headers(),
            json={
                "platform": "windows",
                "kind": "server_command",
                "target": "desktop",
                "argv": ["whoami"],
                "risk_level": "low",
                "reason": "execute through the configured Windows adapter",
                "session_id": session.json()["id"],
            },
        )
        assert created.status_code == 202
        operation_id = created.json()["id"]
        assert api.patch(
            f"/api/v1/admin/openclaw/operations/{operation_id}",
            headers=headers(),
            json={"decision": "approve"},
        ).status_code == 200

        executed = api.post(f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers())

        assert executed.status_code == 200
        assert executed.json()["stdout"] == "windows-adapter-ok\n"
        assert adapter_calls == [
            {
                "path": "/v1/openclaw/execute",
                "authorization": "Bearer sk-live",
                "body": {
                    "operation_id": operation_id,
                    "platform": "windows",
                    "kind": "server_command",
                    "target": "desktop",
                    "argv": ["whoami"],
                    "risk_level": "low",
                    "reason": "execute through the configured Windows adapter",
                    "session_id": session.json()["id"],
                },
            }
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_openclaw_operation_can_bind_to_active_session() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created_session = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "keep server operations inside an approved control session",
        },
    )
    assert created_session.status_code == 201
    session_id = created_session.json()["id"]

    created_operation = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "bind this command to the active OpenClaw session",
            "session_id": session_id,
        },
    )

    assert created_operation.status_code == 202
    operation = created_operation.json()
    assert operation["operation"]["session_id"] == session_id

    sessions = api.get("/api/v1/admin/openclaw/sessions", headers=headers())
    assert sessions.status_code == 200
    stored = next(item for item in sessions.json() if item["id"] == session_id)
    assert stored["operation_ids"] == [operation["id"]]


def test_openclaw_operation_rejects_inactive_session_binding() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created_session = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "pause this session before operation binding",
        },
    )
    session_id = created_session.json()["id"]
    assert api.patch(
        f"/api/v1/admin/openclaw/sessions/{session_id}",
        headers=headers(),
        json={"action": "pause"},
    ).status_code == 200

    created_operation = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": ["python", "--version"],
            "risk_level": "low",
            "reason": "paused sessions cannot accept new operations",
            "session_id": session_id,
        },
    )

    assert created_operation.status_code == 409
    assert created_operation.json()["error"]["code"] == "openclaw_session_not_active"

def test_openclaw_execute_rechecks_bound_session_is_active() -> None:
    api = client()
    command = [sys.executable, "-c", "print('openclaw-paused-session-should-not-run')"]
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    payload["openclaw_allowed_commands"] = [command]
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created_session = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "pause this control session before executing a bound operation",
        },
    )
    assert created_session.status_code == 201
    session_id = created_session.json()["id"]

    created_operation = api.post(
        "/api/v1/admin/openclaw/operations",
        headers=headers(),
        json={
            "platform": "linux",
            "kind": "server_command",
            "target": "agent-hub-server",
            "argv": command,
            "risk_level": "low",
            "reason": "bound operation must respect session pause at execute time",
            "session_id": session_id,
        },
    )
    assert created_operation.status_code == 202
    operation_id = created_operation.json()["id"]
    assert api.patch(
        f"/api/v1/admin/openclaw/operations/{operation_id}",
        headers=headers(),
        json={"decision": "approve"},
    ).status_code == 200
    assert api.patch(
        f"/api/v1/admin/openclaw/sessions/{session_id}",
        headers=headers(),
        json={"action": "pause"},
    ).status_code == 200

    response = api.post(f"/api/v1/admin/openclaw/operations/{operation_id}/execute", headers=headers())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_session_not_active"

def test_openclaw_session_requires_feature_switch() -> None:
    response = client().post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "keep a bounded OpenClaw control session for server maintenance",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_disabled"


def test_openclaw_session_lifecycle_tracks_pause_resume_and_stop() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "linux",
            "target_type": "server",
            "target": "agent-hub-server",
            "purpose": "keep a bounded OpenClaw control session for server maintenance",
        },
    )
    assert created.status_code == 201
    session = created.json()
    assert session["status"] == "active"
    assert session["adapter_status"] == "available"
    assert session["mode"] == "ask"
    assert session["platform"] == "linux"
    assert session["target_type"] == "server"
    assert session["operation_ids"] == []

    listed = api.get("/api/v1/admin/openclaw/sessions", headers=headers())
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [session["id"]]

    paused = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "pause"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "resume"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"

    stopped = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "stop"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"

    repeated = api.patch(
        f"/api/v1/admin/openclaw/sessions/{session['id']}",
        headers=headers(),
        json={"action": "resume"},
    )
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "openclaw_session_closed"


def test_openclaw_windows_session_is_managed_but_adapter_unavailable() -> None:
    api = client()
    payload = api.get("/api/v1/admin/settings", headers=headers()).json()
    payload["openclaw_enabled"] = True
    payload["openclaw_mode"] = "ask"
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200

    created = api.post(
        "/api/v1/admin/openclaw/sessions",
        headers=headers(),
        json={
            "platform": "windows",
            "target_type": "computer",
            "target": "office-windows-pc",
            "purpose": "prepare a future local Windows OpenClaw adapter session",
        },
    )

    assert created.status_code == 201
    session = created.json()
    assert session["status"] == "adapter_unavailable"
    assert session["adapter_status"] == "adapter_unavailable"
    assert session["execution_host"] == "remote-windows-host"


def test_qwen_dashscope_unauthorized_model_check_returns_provider_specific_hint() -> None:
    deployment = Deployment(
        id="qwen_1",
        logical_model="qwen",
        provider_model="qwen/qwen-max",
        request_model="qwen-max",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    details = _model_check_failure_details(
        deployment,
        "provider returned status=401",
        status_code="401",
    )

    assert details["provider"] == "qwen"
    assert details["upstream_model"] == "qwen-max"
    assert "DashScope" in details["hint"]
    assert "AccessKey" in details["hint"]
    assert "Bearer" in details["hint"]


def test_run_detail_response_can_expose_mode_decision_token() -> None:
    token = "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234"

    response = RunDetailResponse(
        id=uuid4(),
        status="waiting_user_mode",
        mode="auto",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="ambiguous task",
        events=[],
        artifacts=[],
        explicit_details={"version": "1"},
        decision_token=token,
    )

    assert response.decision_token == token


def test_admin_run_event_exposes_safe_process_details_without_secrets() -> None:
    response = _admin_run_event(
        {
            "sequence": 7,
            "kind": "tool.completed",
            "message": "read repository summary",
            "actor": "reviewer",
            "participants": ["reviewer", "security-reviewer"],
            "tool_name": "github_reader",
            "step_id": "collect-context",
            "action": "inspect",
            "payload": {
                "command": "git diff --stat",
                "api_key": "sk-should-not-leak",
                "nested": {"token": "secret", "result": "ok"},
            },
        }
    )

    assert response.actor == "reviewer"
    assert response.participants == ["reviewer", "security-reviewer"]
    assert response.tool_name == "github_reader"
    assert response.step_id == "collect-context"
    assert response.action == "inspect"
    assert response.payload["command"] == "git diff --stat"
    assert response.payload["api_key"] == "[redacted]"
    assert response.payload["nested"] == {"token": "[redacted]", "result": "ok"}


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("direct", "model gateway failed"),
        ("dispatch", "step execution failed"),
        ("discuss", "discussion_failed"),
        ("hybrid", "hybrid dispatch failed: model gateway failed"),
    ],
)
def test_mode_error_log_includes_runtime_failed_reason_from_events(
    mode: str,
    reason: str,
) -> None:
    run_id = uuid4()
    response = RunDetailResponse(
        id=run_id,
        status="failed",
        mode=mode,
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="model.started",
                message="model request started",
                created_at=datetime.now(UTC),
            ),
            RunEventResponse(
                sequence=2,
                kind="runtime.failed",
                message=reason,
                created_at=datetime.now(UTC),
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert log.message == f"{mode} run failed: {reason}"
    assert log.details["reason"] == reason


def test_mode_error_log_explains_missing_legacy_failure_reason() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="direct",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert log.message == "direct run failed: failure reason was not recorded"
    assert log.details["reason"] == "failure reason was not recorded"
    assert "older runs" in log.details["diagnosis"]


def test_mode_error_log_explains_legacy_generic_gateway_reason() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="direct",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=1,
                kind="runtime.failed",
                message="model gateway failed",
                created_at=datetime.now(UTC),
            )
        ],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert log.message == "direct run failed: model gateway failed"
    assert log.details["reason"] == "model gateway failed"
    assert "rerun" in log.details["diagnosis"].lower()


def test_mode_error_log_prefers_specific_step_reason_over_generic_terminal() -> None:
    response = RunDetailResponse(
        id=uuid4(),
        status="failed",
        mode="dispatch",
        queue_wait_ms=0,
        capacity_wait_ms=0,
        cost_usd="0",
        request="hello",
        events=[
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="dispatch execution failed",
                created_at=datetime.now(UTC),
            ),
            RunEventResponse(
                sequence=2,
                kind="step.failed",
                message="CrewAI step execution failed: agent identifier must be a safe identifier",
                created_at=datetime.now(UTC),
            ),
        ],
        artifacts=[],
        explicit_details={},
    )

    log = _mode_error_log_from_run(response)

    assert (
        log.message
        == "dispatch run failed: CrewAI step execution failed: agent identifier must be a safe identifier"
    )
    assert (
        log.details["reason"]
        == "CrewAI step execution failed: agent identifier must be a safe identifier"
    )
    assert "diagnosis" not in log.details


def test_run_debug_snapshot_preserves_partial_output_and_failure_context() -> None:
    run_id = uuid4()
    response = RunDetailResponse(
        id=run_id,
        status="failed",
        mode="hybrid",
        queue_wait_ms=12,
        capacity_wait_ms=3,
        cost_usd="0.042",
        request="生成代码审查报告",
        events=[
            RunEventResponse(
                sequence=1,
                kind="model.started",
                message="主 Agent 开始规划",
                created_at=datetime.now(UTC),
                actor="main_agent",
                payload={"credential_ref": "secret://main"},
            ),
            RunEventResponse(
                sequence=2,
                kind="artifact.created",
                message="已生成安全审查摘要",
                created_at=datetime.now(UTC),
                actor="security_reviewer",
            ),
            RunEventResponse(
                sequence=3,
                kind="runtime.failed",
                message="hybrid discuss failed: model gateway failed: model transport failed",
                created_at=datetime.now(UTC),
            ),
        ],
        artifacts=[
            RunArtifactResponse(
                id="artifact_1",
                kind="text",
                title="安全审查摘要",
                text="已经完成静态审查，发现 2 个高风险问题。",
            )
        ],
        explicit_details={"routing_reason": "cross_domain_task"},
    )

    debug = _run_debug_from_detail(response)

    assert debug.run_id == run_id
    assert debug.failed_stage == "runtime.failed"
    assert debug.failure_reason == "hybrid discuss failed: model gateway failed: model transport failed"
    assert debug.partial_output_available is True
    assert debug.artifacts[0].text_preview == "已经完成静态审查，发现 2 个高风险问题。"
    assert debug.events[0].payload["credential_ref"] == "[redacted]"
    assert "模型服务" in debug.recommendation


def test_run_debug_endpoint_exposes_safe_failure_snapshot() -> None:
    api = client()
    run_id = "22222222-2222-4222-8222-222222222222"

    response = api.get(f"/api/v1/admin/runs/{run_id}/debug", headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["events"][0]["kind"] == "queued"
    assert body["partial_output_available"] is False
    assert body["artifacts"][0]["title"] == "Readiness report"
    assert body["artifacts"][0]["has_text"] is False


def test_routing_details_exposes_channel_directive_context() -> None:
    details = _routing_details(
        {
            "requested_channel_features": "vibe_coding",
            "requested_skills": "deep-research",
            "requested_mcp_servers": "filesystem",
            "requested_plugins": "github",
        }
    )

    assert details["requested_channel_features"] == "vibe_coding"
    assert details["requested_skills"] == "deep-research"
    assert details["requested_mcp_servers"] == "filesystem"
    assert details["requested_plugins"] == "github"


TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
SECRET_ID = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class StubAuthService:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(USER_ID, TENANT_ID, Role.SUPER_ADMIN)


def client() -> TestClient:
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = InMemoryAdminResourceService()
    return TestClient(app)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


@dataclass(frozen=True, slots=True)
class SubmittedScheduleRun:
    id: UUID


class RecordingScheduleSubmitter:
    def __init__(self) -> None:
        self.calls: list[TaskRequest] = []

    async def submit(self, request: TaskRequest) -> SubmittedScheduleRun:
        self.calls.append(request)
        return SubmittedScheduleRun(uuid4())


class PersistentScheduleResourceService(InMemoryAdminResourceService):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: dict[tuple[str, str], dict[str, object]] = {}

    async def _list_admin_payloads(self, kind: str) -> list[dict[str, object]]:
        return [
            payload
            for (stored_kind, _resource_id), payload in sorted(self.payloads.items())
            if stored_kind == kind
        ]

    async def _upsert_admin_payload(
        self, kind: str, resource_id: str, payload: dict[str, object]
    ) -> bool:
        self.payloads[(kind, resource_id)] = payload
        return True

    async def _delete_admin_payload(self, kind: str, resource_id: str) -> bool:
        return self.payloads.pop((kind, resource_id), None) is not None


def scheduler_client(
    submitter: RecordingScheduleSubmitter,
    *,
    resource_service: InMemoryAdminResourceService | None = None,
) -> TestClient:
    app = create_app(
        auth_service=StubAuthService(),
        rate_limiter=object(),
    )
    app.state.admin_resource_service = resource_service or InMemoryAdminResourceService()
    app.state.schedule_service = SchedulerService(submitter.submit)
    return TestClient(app)


def model_payload() -> dict[str, object]:
    return {
        "provider": "deepseek",
        "api_base": "https://api.deepseek.example/v1",
        "upstream_model": "deepseek-chat",
        "logical_model": "planner",
        "capabilities": ["text", "tool_calling"],
        "credential_ref": "secret_1",
        "quota_scope": "deepseek_account_1",
        "max_concurrency": 1,
        "target_utilization": 0.8,
        "reserved_capacity": 0,
        "rpm": 60,
        "tpm": 100000,
        "queue_timeout_seconds": 60,
        "fallback": "planner_backup",
        "weight": 100,
    }


def test_schedule_api_creates_lists_and_ticks_user_visible_tasks() -> None:
    submitter = RecordingScheduleSubmitter()
    api = scheduler_client(submitter)
    run_at = "2026-08-13T09:00:00+08:00"

    created = api.post(
        "/api/v1/admin/schedules",
        headers=headers(),
        json={
            "name": "daily-report-fill",
            "message": "Open the report system and fill today's report",
            "mode": "dispatch",
            "workflow_id": "daily_report",
            "kind": "one_time",
            "run_at": run_at,
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once",
            "budget": 4096,
            "metadata": {"openclaw": "windows_desktop_report"},
        },
    )

    assert created.status_code == 201
    schedule = created.json()
    assert schedule["name"] == "daily-report-fill"
    assert schedule["status"] == "active"
    assert schedule["kind"] == "one_time"
    assert schedule["next_fire_at"] == "2026-08-13T01:00:00Z"

    listed = api.get("/api/v1/admin/schedules", headers=headers())
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [schedule["id"]]

    ticked = api.post(
        "/api/v1/admin/schedules/tick",
        headers=headers(),
        json={"now": run_at},
    )

    assert ticked.status_code == 200
    assert ticked.json() == {"fired": [schedule["id"]]}
    assert len(submitter.calls) == 1
    request = submitter.calls[0]
    assert request.tenant_id == TENANT_ID
    assert request.actor_id == USER_ID
    assert request.message == "Open the report system and fill today's report"
    assert request.mode.value == "dispatch"
    assert request.workflow == "daily_report"
    assert request.budget == 4096
    assert request.metadata["schedule_id"] == schedule["id"]
    assert request.metadata["openclaw"] == "windows_desktop_report"


def test_schedule_api_persists_restores_and_deletes_tasks() -> None:
    resource_service = PersistentScheduleResourceService()
    api = scheduler_client(RecordingScheduleSubmitter(), resource_service=resource_service)
    run_at = "2026-08-14T09:00:00+08:00"

    created = api.post(
        "/api/v1/admin/schedules",
        headers=headers(),
        json={
            "name": "restart-safe-report-fill",
            "message": "Open the report system after restart",
            "mode": "dispatch",
            "workflow_id": "daily_report",
            "kind": "one_time",
            "run_at": run_at,
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once",
            "budget": 4096,
            "metadata": {"openclaw": "windows_desktop_report"},
        },
    )

    assert created.status_code == 201
    schedule_id = created.json()["id"]
    assert ("schedule", schedule_id) in resource_service.payloads

    restarted_api = scheduler_client(
        RecordingScheduleSubmitter(),
        resource_service=resource_service,
    )
    restored = restarted_api.get("/api/v1/admin/schedules", headers=headers())
    assert restored.status_code == 200
    assert [item["id"] for item in restored.json()] == [schedule_id]

    deleted = restarted_api.delete(f"/api/v1/admin/schedules/{schedule_id}", headers=headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"id": schedule_id, "deleted": True}
    assert ("schedule", schedule_id) not in resource_service.payloads

    listed_after_delete = restarted_api.get("/api/v1/admin/schedules", headers=headers())
    assert listed_after_delete.status_code == 200
    assert listed_after_delete.json() == []


def skill_archive() -> bytes:
    manifest = (
        "name: safe_skill\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3.12\n"
        "declared_tools:\n"
        "  - filesystem.read\n"
        "dependency_lock_hash: "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.yaml", manifest)
        archive.writestr("main.py", "print('ok')\n")
    return buffer.getvalue()


def skill_tar_archive() -> bytes:
    manifest = (
        "name: safe_tar_skill\n"
        "version: 1.0.0\n"
        "entry_point: main.py\n"
        "compatible_runtime: python3.12\n"
        "declared_tools: []\n"
        "dependency_lock_hash: "
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in {
            "skill.yaml": manifest.encode("utf-8"),
            "main.py": b"print('ok')\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def skill_bundle_archive() -> bytes:
    buffer = io.BytesIO()
    skill_manifests = {
        "writer": (
            "name: writer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools:\n"
            "  - filesystem.read\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
        "reviewer": (
            "name: reviewer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools: []\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
    }
    with zipfile.ZipFile(buffer, "w") as archive:
        for folder, manifest in skill_manifests.items():
            archive.writestr(f"{folder}/skill.yaml", manifest)
            archive.writestr(f"{folder}/main.py", "print('ok')\n")
    return buffer.getvalue()


def wrapped_skill_tar_bundle_archive() -> bytes:
    skill_manifests = {
        "writer": (
            "name: wrapped_writer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools:\n"
            "  - filesystem.read\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
        "reviewer": (
            "name: wrapped_reviewer_skill\n"
            "version: 1.0.0\n"
            "entry_point: main.py\n"
            "compatible_runtime: python3.12\n"
            "declared_tools: []\n"
            "dependency_lock_hash: "
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
        ),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder, manifest in skill_manifests.items():
            for name, content in {
                f"all-skills/{folder}/skill.yaml": manifest.encode("utf-8"),
                f"all-skills/{folder}/main.py": b"print('ok')\n",
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def instruction_skill_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: codex-writer\ndescription: Draft structured research notes.\n---\n\nWrite concise notes.\n",
        )
    return buffer.getvalue()


def instruction_skill_bundle_archive() -> bytes:
    skill_docs = {
        "research": "---\nname: research-writer\ndescription: Research writing.\n---\n\nWrite research notes.\n",
        "reviewer": "---\nname: reviewer-checklist\ndescription: Review checklist.\n---\n\nReview outputs.\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for folder, content in skill_docs.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"all-skills/{folder}/SKILL.md")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def large_nested_instruction_skill_bundle_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(99):
            skill_name = f"nested-instruction-skill-{index:03d}"
            content = (
                "---\n"
                f"name: {skill_name}\n"
                "description: Nested bundle regression.\n"
                "---\n\n"
                "Use this instruction skill from a wrapped all-skills archive.\n"
            ).encode()
            info = tarfile.TarInfo(f"all-skills_1/skills/{skill_name}/SKILL.md")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def large_flat_instruction_skill_bundle_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(99):
            skill_name = f"flat-instruction-skill-{index:03d}"
            archive.writestr(
                f"{skill_name}/SKILL.md",
                "---\n"
                f"name: {skill_name}\n"
                "description: Flat bundle regression.\n"
                "---\n\n"
                "Use this instruction skill from a flat skills.zip archive.\n",
            )
    return buffer.getvalue()


def instruction_bundle_with_rich_skill_directory_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "rich-skill/SKILL.md",
            "---\nname: rich-skill\ndescription: Skill with reference files.\n---\n\nUse this skill.\n",
        )
        for index in range(80):
            archive.writestr(
                f"rich-skill/references/note-{index:03d}.md",
                f"Reference note {index}.\n",
            )
        archive.writestr(
            "compact-skill/SKILL.md",
            "---\nname: compact-skill\ndescription: Compact bundled skill.\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def instruction_bundle_with_non_slug_frontmatter_name_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "bianzheng-pingheng/SKILL.md",
            "---\nname: 辩证平衡\ndescription: 中文名称的 Skill。\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()


def instruction_bundle_with_hidden_nested_skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "aibiandao/SKILL.md",
            "---\nname: aibiandao\ndescription: Parent skill.\n---\n\nUse this skill.\n",
        )
        archive.writestr(
            "aibiandao/.worktrees/draft/SKILL.md",
            "---\nname: should-not-install\ndescription: Hidden worktree.\n---\n\nIgnore this worktree.\n",
        )
        archive.writestr("aibiandao/.worktrees/draft/notes.md", "temporary worktree note\n")
        archive.writestr("aibiandao/__pycache__/cached.cpython-314.pyc", b"cached")
        archive.writestr(
            "other-skill/SKILL.md",
            "---\nname: other-skill\ndescription: Other skill.\n---\n\nUse this skill.\n",
        )
    return buffer.getvalue()

def partially_invalid_instruction_skill_bundle_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "valid-skill/SKILL.md",
            "---\nname: valid-bundle-skill\ndescription: Valid bundled skill.\n---\n\nUse this skill.\n",
        )
        archive.writestr(
            "invalid-skill/SKILL.md",
            "---\nname: invalid-bundle-skill\ndescription: Invalid bundled skill.\n---\n\nUse this skill.\n",
        )
        archive.writestr("invalid-skill/nested.zip", b"PK\x03\x04")
    return buffer.getvalue()


def test_model_pool_reports_serial_slot_and_queue_policy() -> None:
    response = client().post("/api/v1/admin/models", headers=headers(), json=model_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["upstream_model"] == "deepseek-chat"
    assert body["effective_slots"] == 1
    assert body["saturation_policy"] == "queue_first_then_fallback"


def test_model_create_auto_infers_known_video_generation_capability() -> None:
    payload = {
        **model_payload(),
        "provider": "minimax",
        "upstream_model": "MiniMax-Hailuo-02",
        "logical_model": "video_primary",
        "capabilities": ["text"],
    }

    response = client().post("/api/v1/admin/models", headers=headers(), json=payload)

    assert response.status_code == 200
    assert response.json()["capabilities"] == ["text", "video_generation"]


def test_model_create_accepts_input_understanding_capabilities() -> None:
    payload = {
        **model_payload(),
        "capabilities": ["text", "vision", "audio", "tool_calling"],
    }

    response = client().post("/api/v1/admin/models", headers=headers(), json=payload)

    assert response.status_code == 200
    assert response.json()["capabilities"] == ["audio", "text", "tool_calling", "vision"]


def test_multimedia_generation_requires_feature_switch() -> None:
    response = client().post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "multimedia_generation_disabled"


def test_multimedia_video_generation_requires_video_capable_model() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    gateway = FakeGenerationGateway(
        error=NoCapableDeployment(
            "no capable deployment for logical model 'video_primary': video_generation"
        )
    )
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(gateway)

    response = api.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "model_capability_unavailable"
    assert gateway.requests[0].required_capabilities == frozenset({
        ModelCapability.VIDEO_GENERATION
    })


def test_multimedia_generation_daily_limit_returns_429() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    gateway = FakeGenerationGateway(
        error=MultimediaDailyLimitExceeded("daily multimedia generation limit exceeded")
    )
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(gateway)

    response = api.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "multimedia_daily_limit_exceeded"


def test_multimedia_generation_provider_failure_returns_502() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    gateway = FakeGenerationGateway(
        error=VideoProviderGenerationError("MiniMax video submit failed: invalid api key", provider_code="2049")
    )
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(gateway)

    response = api.post(
        "/api/v1/admin/multimedia/generate",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "multimedia_provider_failed"
    assert response.json()["error"]["details"] == {
        "provider_code": "2049",
        "reason": "MiniMax video submit failed: invalid api key",
    }


def test_multimedia_generation_job_can_be_run_by_executor_agent_and_read_by_main_agent() -> None:
    api = client()
    settings_response = api.get("/api/v1/admin/settings", headers=headers())
    payload = settings_response.json()
    payload["multimedia_generation_enabled"] = True
    assert api.put("/api/v1/admin/settings", headers=headers(), json=payload).status_code == 200
    cast(Any, api.app).state.multimedia_generation_executor = MultimediaGenerationExecutor(FakeGenerationGateway())

    submitted = api.post(
        "/api/v1/admin/multimedia/jobs",
        headers=headers(),
        json={
            "kind": "video",
            "logical_model": "video_primary",
            "prompt": "make a 5 second product video",
        },
    )

    assert submitted.status_code == 202
    queued = submitted.json()
    assert queued["id"].startswith("media_")
    assert queued["status"] == "queued"
    assert queued["executor_id"] is None
    assert queued["artifacts"] == []

    completed = api.post(
        f"/api/v1/admin/multimedia/jobs/{queued['id']}/run",
        headers=headers(),
        json={"executor_id": "multimedia_generator"},
    )

    assert completed.status_code == 202
    body = completed.json()
    assert body["status"] == "succeeded"
    assert body["executor_id"] == "multimedia_generator"
    assert body["artifacts"] == [
        {
            "kind": "video",
            "uri": "artifact://generated-media",
            "text": "artifact://generated-media",
        }
    ]

    readable = api.get(f"/api/v1/admin/multimedia/jobs/{queued['id']}", headers=headers())

    assert readable.status_code == 200
    assert readable.json() == body


def test_main_agent_config_saves_dedicated_model_api_and_control_policy() -> None:
    api = client()

    updated = api.put(
        "/api/v1/admin/main-agent",
        headers=headers(),
        json={
            "model": {
                "provider": "openai-compatible",
                "api_base": "https://gsykj.com",
                "api_protocol": "openai_compatible",
                "upstream_model": "deepseek-chat",
                "credential_ref": "secret://main-agent",
                "capabilities": ["text", "tool_calling"],
            },
            "control_mode": "supervisor",
            "decision_policy": "choose mode first, then roles; main agent makes the final decision",
            "hermes_policy": "confirm_before_apply",
            "max_review_rounds": 3,
        },
    )
    fetched = api.get("/api/v1/admin/main-agent", headers=headers())

    assert updated.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["model"]["provider"] == "openai-compatible"
    assert fetched.json()["model"]["api_base"] == "https://gsykj.com/v1"
    assert fetched.json()["model"]["api_protocol"] == "openai_compatible"
    assert fetched.json()["control_mode"] == "supervisor"
    assert fetched.json()["hermes_policy"] == "confirm_before_apply"


def test_main_agent_config_rejects_missing_dedicated_model_key() -> None:
    response = client().put(
        "/api/v1/admin/main-agent",
        headers=headers(),
        json={
            "model": {
                "provider": "openai-compatible",
                "api_base": "https://gsykj.com",
                "api_protocol": "openai_compatible",
                "upstream_model": "deepseek-chat",
                "credential_ref": "",
                "capabilities": ["text"],
            },
            "control_mode": "supervisor",
            "decision_policy": "use a dedicated main agent model",
            "hermes_policy": "observe",
            "max_review_rounds": 2,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation"


def test_secret_create_and_get_never_return_value_or_fingerprint() -> None:
    api = client()

    created = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "deepseek", "value": "sk-secret-value"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["last_four"] == "alue"
    assert "sk-secret-value" not in created.text
    assert "fingerprint" not in body

    fetched = api.get(f"/api/v1/admin/secrets/{body['ref']}", headers=headers())
    assert fetched.status_code == 200
    assert "sk-secret-value" not in fetched.text
    assert "fingerprint" not in fetched.json()


def test_duplicate_secret_is_rejected_by_fingerprint_without_disclosure() -> None:
    api = client()

    first = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "one", "value": "same-secret"},
    )
    second = api.post(
        "/api/v1/admin/secrets",
        headers=headers(),
        json={"label": "two", "value": "same-secret"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "same-secret" not in second.text


def test_probe_returns_non_saturating_recommendation() -> None:
    response = client().post(
        "/api/v1/admin/models/probe",
        headers=headers(),
        json={"quota_scope": "deepseek_account_1", "desired_concurrency": 32},
    )

    assert response.status_code == 200
    assert response.json()["recommended_concurrency"] == 8
    assert "explicitly" in response.json()["warning"]


def test_draft_diff_publish_conflict_and_rollback() -> None:
    api = client()

    draft = api.put(
        "/api/v1/admin/config/draft",
        headers=headers(),
        json={"yaml": "models:\n  - planner\n"},
    )
    diff = api.post(
        "/api/v1/admin/config/diff",
        headers=headers(),
        json={"yaml": "models:\n  - planner\n"},
    )
    publish = api.post(
        "/api/v1/admin/config/publish",
        headers=headers(),
        json={"expected_version": 0},
    )
    conflict = api.post(
        "/api/v1/admin/config/publish",
        headers=headers(),
        json={"expected_version": 0},
    )
    rollback = api.post("/api/v1/admin/config/rollback/0", headers=headers())

    assert draft.json() == {"version": 0, "status": "draft"}
    assert diff.json()["changed"] == ["configuration"]
    assert publish.json() == {"version": 1, "status": "published"}
    assert conflict.status_code == 409
    assert rollback.json() == {"version": 0, "status": "rolled_back"}


def test_agent_and_workflow_crud() -> None:
    api = client()

    agent = api.post(
        "/api/v1/admin/agents",
        headers=headers(),
        json={"id": "planner", "name": "Planner", "enabled": True},
    )
    workflow = api.post(
        "/api/v1/admin/workflows",
        headers=headers(),
        json={"id": "dispatch", "name": "Dispatch", "enabled": True},
    )

    assert agent.status_code == 200
    assert workflow.status_code == 200
    assert api.get("/api/v1/admin/agents", headers=headers()).json()[0]["id"] == "planner"
    assert (
        api.get("/api/v1/admin/workflows", headers=headers()).json()[0]["id"] == "dispatch"
    )


def test_channel_status_exposes_feishu_setup_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_live")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret-live")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "verify-live")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "encrypt-live")
    monkeypatch.setenv("FEISHU_TRANSPORT", "webhook")
    monkeypatch.setenv("AGENT_HUB_PUBLIC_URL", "https://agent.example.com")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    by_id = {item["id"]: item for item in payload}
    assert by_id["feishu"]["status"] == "configured"
    assert (
        by_id["feishu"]["public_webhook_url"]
        == "https://agent.example.com/channels/feishu/events"
    )
    assert by_id["feishu"]["missing"] == []
    assert {
        "feishu",
        "dingtalk",
        "wecom_bot",
        "wecom_app",
        "wechat_official",
        "wechat_customer_service",
        "telegram",
        "slack",
        "qq",
        "custom_webhook",
    }.issubset(by_id)
    assert by_id["wechat_official"]["status"] == "missing_config"
    assert by_id["custom_webhook"]["status"] == "missing_config"
    assert by_id["wecom_app"]["name"] == "企业微信 Agent"
    serialized = response.text
    assert "自建应用" not in serialized
    assert "secret-live" not in serialized
    assert "verify-live" not in serialized
    assert "encrypt-live" not in serialized


def test_channel_config_can_be_saved_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DINGTALK_APP_KEY", raising=False)
    monkeypatch.delenv("DINGTALK_APP_SECRET", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_TOKEN", raising=False)

    api = client()
    response = api.post(
        "/api/v1/admin/channels/dingtalk/config",
        headers=headers(),
        json={
            "values": {
                "DINGTALK_APP_KEY": "ding-app-key",
                "DINGTALK_APP_SECRET": "ding-secret",
                "DINGTALK_WEBHOOK_TOKEN": "ding-token",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["saved"] == [
        "DINGTALK_APP_KEY",
        "DINGTALK_APP_SECRET",
        "DINGTALK_WEBHOOK_TOKEN",
    ]
    assert response.json()["status"]["status"] == "configured"
    assert response.json()["status"]["missing"] == []
    assert "ding-secret" not in response.text
    channels = api.get("/api/v1/admin/channels", headers=headers())
    by_id = {item["id"]: item for item in channels.json()}
    assert by_id["dingtalk"]["status"] == "configured"
    assert by_id["dingtalk"]["missing"] == []


def test_all_channel_statuses_are_configured_when_required_env_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_HUB_PUBLIC_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_ENCRYPT_KEY",
        "DINGTALK_APP_KEY",
        "DINGTALK_APP_SECRET",
        "DINGTALK_WEBHOOK_TOKEN",
        "WECOM_BOT_WEBHOOK_KEY",
        "WECOM_BOT_WEBHOOK_TOKEN",
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_SECRET",
        "WECOM_TOKEN",
        "WECHATMP_APP_ID",
        "WECHATMP_APP_SECRET",
        "WECHATMP_TOKEN",
        "WECHAT_KF_CORP_ID",
        "WECHAT_KF_SECRET",
        "WECHAT_KF_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "QQ_BOT_APP_ID",
        "QQ_BOT_TOKEN",
        "QQ_WEBHOOK_TOKEN",
        "CUSTOM_WEBHOOK_TOKEN",
    ):
        monkeypatch.setenv(name, "configured")

    response = client().get("/api/v1/admin/channels", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert {item["status"] for item in payload} == {"configured"}
    assert all(item["missing"] == [] for item in payload)


def test_operational_run_listing_details_and_controls() -> None:
    api = client()

    runs = api.get("/api/v1/admin/runs", headers=headers())
    assert runs.status_code == 200
    run_id = runs.json()[0]["id"]
    assert runs.json()[0]["queue_wait_ms"] >= 0
    assert runs.json()[0]["capacity_wait_ms"] >= 0
    assert runs.json()[0]["cost_usd"] == "0.0132"

    detail = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())
    pause = api.post(f"/api/v1/admin/runs/{run_id}/pause", headers=headers())
    resume = api.post(f"/api/v1/admin/runs/{run_id}/resume", headers=headers())
    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())

    assert detail.status_code == 200
    assert detail.json()["mode"] == "dispatch"
    assert detail.json()["events"][0]["kind"] == "queued"
    assert detail.json()["artifacts"][0]["title"] == "Readiness report"
    assert pause.json()["status"] == "paused"
    assert resume.json()["status"] == "running"
    assert cancel.json()["status"] == "cancelled"


def test_operational_run_delete_removes_cancelled_conversation() -> None:
    api = client()
    run_id = api.get("/api/v1/admin/runs", headers=headers()).json()[0]["id"]

    active_delete = api.delete(f"/api/v1/admin/runs/{run_id}", headers=headers())
    assert active_delete.status_code == 409
    assert active_delete.json()["error"]["code"] == "run_conflict"

    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())
    assert cancel.status_code == 200

    deleted = api.delete(f"/api/v1/admin/runs/{run_id}", headers=headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"id": run_id, "deleted": True}

    missing_detail = api.get(f"/api/v1/admin/runs/{run_id}", headers=headers())
    assert missing_detail.status_code == 404
    remaining = api.get("/api/v1/admin/runs", headers=headers())
    assert all(item["id"] != run_id for item in remaining.json())


def test_operational_run_bulk_delete_uses_existing_delete_rules() -> None:
    api = client()
    run_id = api.get("/api/v1/admin/runs", headers=headers()).json()[0]["id"]

    blocked = api.post(
        "/api/v1/admin/runs/bulk-delete",
        headers=headers(),
        json={"ids": [run_id]},
    )
    assert blocked.status_code == 200
    assert blocked.json()["deleted"] == []
    assert blocked.json()["failed"][0]["id"] == run_id
    assert blocked.json()["failed"][0]["code"] == "run_conflict"

    cancel = api.post(f"/api/v1/admin/runs/{run_id}/cancel", headers=headers())
    assert cancel.status_code == 200
    deleted = api.post(
        "/api/v1/admin/runs/bulk-delete",
        headers=headers(),
        json={"ids": [run_id]},
    )

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == [{"id": run_id, "deleted": True}]
    assert deleted.json()["failed"] == []


def test_operational_run_bulk_delete_accepts_large_selection() -> None:
    api = client()
    ids = [str(uuid4()) for _ in range(101)]

    response = api.post(
        "/api/v1/admin/runs/bulk-delete",
        headers=headers(),
        json={"ids": ids},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == []
    assert [item["id"] for item in response.json()["failed"]] == ids
    assert {item["code"] for item in response.json()["failed"]} == {"not_found"}


def test_admin_run_artifact_exposes_safe_text_for_chat_reply() -> None:
    artifact = _admin_run_artifact(
        {
            "id": "artifact-1",
            "type": "text",
            "producer": "final_synthesizer",
            "content": {"text": "这是可以直接显示在对话里的最终回答。"},
        }
    )

    assert artifact.kind == "text"
    assert artifact.title == "final_synthesizer"
    assert artifact.text == "这是可以直接显示在对话里的最终回答。"


def test_admin_run_artifact_does_not_expose_sensitive_text() -> None:
    artifact = _admin_run_artifact(
        {
            "id": "artifact-2",
            "type": "text",
            "producer": "final_synthesizer",
            "content": {"text": "api_key=sk-secret-value"},
        }
    )

    assert artifact.text is None


def test_conversation_can_be_loaded_by_session_id() -> None:
    api = client()

    response = api.get("/api/v1/admin/conversations/conv-readiness", headers=headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation_id"] == "conv-readiness"
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["request"] == "Summarize current deployment readiness."


@pytest.mark.asyncio
async def test_persistent_admin_conversation_keeps_chronological_messages() -> None:
    first_id = UUID("33333333-3333-4333-8333-333333333331")
    second_id = UUID("33333333-3333-4333-8333-333333333332")

    class FakeRunRepository:
        async def list_recent(self, tenant_id: UUID, *, limit: int = 100) -> tuple[RunRecord, ...]:
            assert tenant_id == TENANT_ID
            assert limit == 200
            return (
                RunRecord(
                    id=second_id,
                    tenant_id=TENANT_ID,
                    actor_id=ACTOR_ID,
                    request="第二轮：继续细化方案",
                    mode=TaskMode.DISPATCH,
                    status=RunStatus.COMPLETED,
                    version=1,
                    routing_decision={"conversation_id": "conv-multi-turn"},
                ),
                RunRecord(
                    id=first_id,
                    tenant_id=TENANT_ID,
                    actor_id=ACTOR_ID,
                    request="第一轮：先做方案",
                    mode=TaskMode.DISPATCH,
                    status=RunStatus.COMPLETED,
                    version=1,
                    routing_decision={"conversation_id": "conv-multi-turn"},
                ),
            )

        async def usage_cost(self, tenant_id: UUID, run_id: UUID) -> str:
            assert tenant_id == TENANT_ID
            assert run_id in {first_id, second_id}
            return "0"

        async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
            assert tenant_id == TENANT_ID
            assert run_id in {first_id, second_id}
            return ()

        async def artifacts(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
            assert tenant_id == TENANT_ID
            assert run_id in {first_id, second_id}
            return ()

    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        run_repository=FakeRunRepository(),  # type: ignore[arg-type]
    )

    conversation = await service.get_conversation("conv-multi-turn")

    assert [run.request for run in conversation.runs] == [
        "第一轮：先做方案",
        "第二轮：继续细化方案",
    ]


def test_skill_upload_approve_mcp_memory_and_audit_are_safe() -> None:
    api = client()

    created_agent = api.post(
        "/api/v1/admin/agents",
        headers=headers(),
        json={
            "id": "smoke-agent",
            "name": "Smoke Agent",
            "role": "reviewer",
            "prompt": "Review safely.",
            "model": "planner",
            "skills": [],
        },
    )
    deleted_agent = api.delete("/api/v1/admin/agents/smoke-agent", headers=headers())
    agents = api.get("/api/v1/admin/agents", headers=headers())
    created_workflow = api.post(
        "/api/v1/admin/workflows",
        headers=headers(),
        json={
            "id": "smoke-workflow",
            "name": "Smoke Workflow",
            "mode": "dispatch",
            "agent_ids": ["planner"],
            "objective": "Smoke test workflow.",
        },
    )
    deleted_workflow = api.delete("/api/v1/admin/workflows/smoke-workflow", headers=headers())
    workflows = api.get("/api/v1/admin/workflows", headers=headers())
    uploaded = api.post(
        "/api/v1/admin/skills",
        headers=headers(),
        json={"filename": "safe-skill.zip"},
    )
    approved = api.post(
        f"/api/v1/admin/skills/{uploaded.json()['id']}/approve",
        headers=headers(),
    )
    deleted_skill = api.delete(
        f"/api/v1/admin/skills/{uploaded.json()['id']}",
        headers=headers(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())
    created_mcp = api.post(
        "/api/v1/admin/mcp",
        headers=headers(),
        json={"id": "browser", "name": "Browser MCP", "allowed_tools": ["open_page"]},
    )
    deleted_mcp = api.delete("/api/v1/admin/mcp/browser", headers=headers())
    mcp = api.get("/api/v1/admin/mcp", headers=headers())
    created_memory = api.post(
        "/api/v1/admin/memory",
        headers=headers(),
        json={
            "id": "logging-policy",
            "scope": "tenant",
            "value": "Default production log collection level is warning.",
        },
    )
    memory = api.get("/api/v1/admin/memory", headers=headers())
    updated_memory = api.patch(
        f"/api/v1/admin/memory/{memory.json()[0]['id']}",
        headers=headers(),
        json={"value": "Updated non-dangerous operation policy."},
    )
    audit = api.get("/api/v1/admin/audit?action=config.publish", headers=headers())

    assert created_agent.status_code == 200
    assert deleted_agent.json()["status"] == "deleted"
    assert all(item["id"] != "smoke-agent" for item in agents.json())
    assert created_workflow.status_code == 200
    assert deleted_workflow.json()["status"] == "deleted"
    assert all(item["id"] != "smoke-workflow" for item in workflows.json())
    assert uploaded.json()["status"] == "quarantined"
    assert approved.json()["status"] == "enabled"
    assert deleted_skill.json()["status"] == "deleted"
    assert all(item["id"] != uploaded.json()["id"] for item in skills.json())
    assert created_mcp.json()["health"] == "configured"
    assert deleted_mcp.json()["status"] == "deleted"
    assert all(item["id"] != "browser" for item in mcp.json())
    assert created_memory.json()["id"] == "logging-policy"
    assert any(item["id"] == "logging-policy" for item in memory.json())
    assert updated_memory.json()["value"] == "Updated non-dangerous operation policy."
    assert audit.json()[0]["action"] == "config.publish"
    serialized = uploaded.text + approved.text + skills.text + mcp.text + memory.text + audit.text
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


def test_unified_logs_include_audit_model_mode_and_feature_errors() -> None:
    api = client()
    app = cast(Any, api.app)
    service = cast(
        InMemoryAdminResourceService,
        app.state.admin_resource_service,
    )

    service.logs.extend(
        [
            service.make_log(
                category="model_error",
                level="error",
                title="模型可用性测试失败",
                message="provider returned status=401",
                source="models.create",
                details={"provider": "deepseek", "status_code": "401"},
            ),
            service.make_log(
                category="mode_error",
                level="error",
                title="模式运行失败",
                message="dispatch runtime failed",
                source="runs.execute",
                details={"mode": "dispatch"},
            ),
            service.make_log(
                category="feature_error",
                level="warning",
                title="主要功能运行错误",
                message="skill package is invalid",
                source="skills.upload",
                details={"feature": "skills"},
            ),
            service.make_log(
                category="agent_error",
                level="warning",
                title="Agent 角色配置错误",
                message="agent model is required",
                source="agents.upsert",
                details={"agent_id": "director", "reason": "missing_model"},
            ),
        ]
    )
    asyncio.run(
        service.record_log(
            category="feature_error",
            level="info",
            title="正常运行流水",
            message="this normal trace must not be collected",
            source="feature.normal",
        )
    )

    all_logs = api.get("/api/v1/admin/logs", headers=headers())
    model_logs = api.get("/api/v1/admin/logs?category=model_error", headers=headers())
    channel_logs = api.get("/api/v1/admin/logs?category=channel_error", headers=headers())

    assert all_logs.status_code == 200
    categories = {item["category"] for item in all_logs.json()}
    assert {
        "audit",
        "model_error",
        "mode_error",
        "feature_error",
        "agent_error",
        "channel_error",
    } <= categories
    assert model_logs.status_code == 200
    assert [item["category"] for item in model_logs.json()] == ["model_error"]
    assert channel_logs.status_code == 200
    assert channel_logs.json()
    assert all(item["level"] == "warning" for item in channel_logs.json())
    serialized = all_logs.text
    assert "this normal trace must not be collected" not in serialized
    for forbidden in ("api_key", "fingerprint", "hidden_reasoning", "chain_of_thought"):
        assert forbidden not in serialized.lower()


def test_skill_archive_upload_scans_real_zip_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-skill.zip"},
        content=skill_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is False
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["name"] == "safe_skill"
    assert item["status"] == "scanned"
    assert item["requested_permissions"] == ["tool:filesystem.read"]
    assert any("content sha256" in entry for entry in item["scan_diff"])
    assert skills.json()[0]["id"] == item["id"]

def test_skill_archive_upload_accepts_percent_encoded_filename_header() -> None:
    api = client()
    filename = "技能包.zip"

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={
            **headers(),
            "X-Agent-Hub-Skill-Filename": quote(filename, safe=""),
            "X-Agent-Hub-Skill-Filename-Encoding": "percent",
        },
        content=skill_archive(),
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["items"][0]["name"] == "safe_skill"


def test_skill_archive_upload_accepts_real_tar_gz_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "safe-tar-skill.tar.gz"},
        content=skill_tar_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is False
    assert body["items"][0]["name"] == "safe_tar_skill"
    assert body["items"][0]["status"] == "scanned"
    assert any(item["id"] == body["items"][0]["id"] for item in skills.json())


def test_skill_archive_upload_scans_bundle_with_multiple_skill_directories() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.zip"},
        content=skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["writer_skill", "reviewer_skill"]
    assert body["items"][0]["requested_permissions"] == ["tool:filesystem.read"]
    assert {item["name"] for item in skills.json()} == {"writer_skill", "reviewer_skill"}


def test_skill_archive_upload_scans_wrapped_tar_gz_bundle_with_multiple_skill_directories() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.tar.gz"},
        content=wrapped_skill_tar_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["wrapped_writer_skill", "wrapped_reviewer_skill"]
    assert body["items"][0]["requested_permissions"] == ["tool:filesystem.read"]
    assert {item["name"] for item in skills.json()} == {"wrapped_writer_skill", "wrapped_reviewer_skill"}


def test_skill_archive_upload_accepts_instruction_only_skill_package() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "codex-writer-skill.zip"},
        content=instruction_skill_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is False
    assert body["items"][0]["name"] == "codex-writer"
    assert body["items"][0]["requested_permissions"] == []
    assert "SKILL.md detected" in body["items"][0]["scan_diff"]
    assert any(item["name"] == "codex-writer" for item in skills.json())


def test_skill_archive_upload_accepts_instruction_skill_tar_gz_bundle() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills.tar.gz"},
        content=instruction_skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["research-writer", "reviewer-checklist"]
    assert all("SKILL.md detected" in item["scan_diff"] for item in body["items"])
    assert {item["name"] for item in skills.json()} == {"research-writer", "reviewer-checklist"}


def test_skill_archive_upload_accepts_large_flat_instruction_bundle_zip() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "skills.zip"},
        content=large_flat_instruction_skill_bundle_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert len(body["items"]) == 99
    assert body["items"][0]["name"] == "flat-instruction-skill-000"
    assert body["items"][-1]["name"] == "flat-instruction-skill-098"
    assert len(skills.json()) == 99


def test_skill_archive_upload_accepts_large_nested_instruction_bundle_tar_gz() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "all-skills_1.tar.gz"},
        content=large_nested_instruction_skill_bundle_archive(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert len(body["items"]) == 99
    assert body["items"][0]["name"] == "nested-instruction-skill-000"
    assert body["items"][-1]["name"] == "nested-instruction-skill-098"
    assert len(skills.json()) == 99


def test_skill_archive_upload_accepts_rich_instruction_skill_directory() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "rich-skills.zip"},
        content=instruction_bundle_with_rich_skill_directory_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["rich-skill", "compact-skill"]
    assert {item["name"] for item in skills.json()} == {"rich-skill", "compact-skill"}


def test_skill_archive_upload_uses_directory_slug_when_frontmatter_name_has_no_slug() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "skills.zip"},
        content=instruction_bundle_with_non_slug_frontmatter_name_zip(),
    )

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["items"][0]["name"] == "bianzheng-pingheng"


def test_skill_archive_upload_ignores_hidden_nested_skill_directories() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "hidden-worktree-skills.zip"},
        content=instruction_bundle_with_hidden_nested_skill_zip(),
    )

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert [item["name"] for item in body["items"]] == ["aibiandao", "other-skill"]

def test_skill_archive_upload_keeps_valid_bundle_items_when_one_item_is_invalid() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "mixed-skills.zip"},
        content=partially_invalid_instruction_skill_bundle_zip(),
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["bundle"] is True
    assert [item["name"] for item in body["items"]] == ["valid-bundle-skill"]
    assert body["skipped"] == [
        {
            "path": "invalid-skill",
            "reason": "instruction skill contains nested archives",
        }
    ]
    assert [item["name"] for item in skills.json()] == ["valid-bundle-skill"]


def test_skill_archive_upload_rejects_invalid_zip_without_saving_metadata() -> None:
    api = client()

    uploaded = api.post(
        "/api/v1/admin/skills/upload",
        headers={**headers(), "X-Agent-Hub-Skill-Filename": "broken.zip"},
        content=b"not-a-zip",
    )
    skills = api.get("/api/v1/admin/skills", headers=headers())

    assert uploaded.status_code == 422
    assert uploaded.json()["error"]["code"] == "invalid_skill_package"
    assert skills.json() == []


def test_memory_forget_removes_record() -> None:
    api = client()
    memory_id = api.get("/api/v1/admin/memory", headers=headers()).json()[0]["id"]

    forgotten = api.delete(f"/api/v1/admin/memory/{memory_id}", headers=headers())
    remaining = api.get("/api/v1/admin/memory", headers=headers())

    assert forgotten.status_code == 200
    assert forgotten.json() == {"status": "forgotten"}
    assert remaining.json() == []


def test_hermes_records_feedback_and_recommends_from_prior_lessons() -> None:
    api = client()

    feedback = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Use group chat when debate review is required.",
            "conversation_id": "conv-architecture-1",
            "tags": ["debate", "review"],
            "weight": 5,
        },
    )
    recommendation = api.post(
        "/api/v1/admin/hermes/recommend",
        headers=headers(),
        json={
            "task": "Run a debate review for this architecture.",
            "mode_candidates": ["dispatch", "group_chat"],
            "model_candidates": ["deepseek-chat", "gpt-4o"],
            "skill_candidates": ["architecture-review", "safe-shell"],
        },
    )
    insights = api.get("/api/v1/admin/hermes", headers=headers())

    assert feedback.status_code == 200
    insight_id = feedback.json()["id"]
    assert feedback.json()["conversation_id"] == "conv-architecture-1"
    assert feedback.json()["confirmed_at"] is None
    assert feedback.json()["summary"] == (
        "Learned success pattern: Use group chat when debate review is required. "
        "Tags: debate, review. Weight: 5."
    )
    detail = api.get(f"/api/v1/admin/hermes/{insight_id}", headers=headers())
    confirmed = api.post(f"/api/v1/admin/hermes/{insight_id}/confirm", headers=headers())
    assert recommendation.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["id"] == insight_id
    assert detail.json()["conversation_id"] == "conv-architecture-1"
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_at"] is not None
    assert recommendation.json()["recommended_mode"] == "group_chat"
    assert recommendation.json()["recommended_model"] == "deepseek-chat"
    assert recommendation.json()["confidence"] > 0.45
    assert any("Hermes lesson" in reason for reason in recommendation.json()["reasons"])
    assert any(
        insight["lesson"] == "Use group chat when debate review is required."
        and insight["summary"].startswith("Learned success pattern:")
        for insight in insights.json()
    )


def test_hermes_bulk_confirm_confirms_multiple_learning_records() -> None:
    api = client()
    first = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Use discussion mode for conflicting design opinions.",
            "conversation_id": "conv-bulk-1",
            "tags": ["discussion"],
            "weight": 4,
        },
    ).json()
    second = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "failure",
            "lesson": "Ask before adding temporary engineering agents.",
            "conversation_id": "conv-bulk-2",
            "tags": ["approval"],
            "weight": 5,
        },
    ).json()

    response = api.post(
        "/api/v1/admin/hermes/bulk-confirm",
        headers=headers(),
        json={"ids": [first["id"], second["id"]]},
    )

    assert response.status_code == 200
    confirmed = response.json()["confirmed"]
    assert [item["id"] for item in confirmed] == [first["id"], second["id"]]
    assert all(item["confirmed_at"] is not None for item in confirmed)
    assert response.json()["failed"] == []


def test_hermes_bulk_delete_removes_multiple_learning_records() -> None:
    api = client()
    first = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "success",
            "lesson": "Delete old Hermes lessons in batches during cleanup.",
            "conversation_id": "conv-bulk-delete-1",
            "tags": ["cleanup"],
            "weight": 3,
        },
    ).json()
    second = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "neutral",
            "lesson": "Remove obsolete confirmed Hermes guidance as a batch.",
            "conversation_id": "conv-bulk-delete-2",
            "tags": ["cleanup"],
            "weight": 2,
        },
    ).json()
    api.post(f"/api/v1/admin/hermes/{second['id']}/confirm", headers=headers())

    response = api.post(
        "/api/v1/admin/hermes/bulk-delete",
        headers=headers(),
        json={"ids": [first["id"], second["id"], "hermes_deadbeef"]},
    )
    remaining = api.get("/api/v1/admin/hermes", headers=headers())

    assert response.status_code == 200
    assert response.json()["deleted"] == [first["id"], second["id"]]
    assert response.json()["failed"] == [
        {
            "id": "hermes_deadbeef",
            "code": "hermes_not_found",
            "message": "Hermes learning record was not found",
        }
    ]
    assert all(item["id"] not in {first["id"], second["id"]} for item in remaining.json())


def test_hermes_bulk_actions_accept_large_mobile_selection() -> None:
    api = client()
    created_ids: list[str] = []
    for index in range(289):
        response = api.post(
            "/api/v1/admin/hermes/feedback",
            headers=headers(),
            json={
                "outcome": "neutral",
                "lesson": f"Large mobile bulk selection regression lesson {index}.",
                "conversation_id": f"conv-large-bulk-{index}",
                "tags": ["bulk"],
                "weight": 1,
            },
        )
        assert response.status_code == 200
        created_ids.append(response.json()["id"])

    confirm = api.post(
        "/api/v1/admin/hermes/bulk-confirm",
        headers=headers(),
        json={"ids": created_ids},
    )
    delete = api.post(
        "/api/v1/admin/hermes/bulk-delete",
        headers=headers(),
        json={"ids": created_ids},
    )

    assert confirm.status_code == 200
    assert [item["id"] for item in confirm.json()["confirmed"]] == created_ids
    assert confirm.json()["failed"] == []
    assert delete.status_code == 200
    assert delete.json() == {"deleted": created_ids, "failed": []}

def test_hermes_bulk_actions_accept_runtime_learning_ids() -> None:
    api = client()
    runtime_id = "hermes_run_deadbeef0123456789abcdef01234567"

    confirm = api.post(
        "/api/v1/admin/hermes/bulk-confirm",
        headers=headers(),
        json={"ids": [runtime_id]},
    )
    delete = api.post(
        "/api/v1/admin/hermes/bulk-delete",
        headers=headers(),
        json={"ids": [runtime_id]},
    )

    assert confirm.status_code == 200
    assert confirm.json() == {
        "confirmed": [],
        "failed": [
            {
                "id": runtime_id,
                "code": "hermes_not_found",
                "message": "Hermes learning record was not found",
            }
        ],
    }
    assert delete.status_code == 200
    assert delete.json() == {
        "deleted": [],
        "failed": [
            {
                "id": runtime_id,
                "code": "hermes_not_found",
                "message": "Hermes learning record was not found",
            }
        ],
    }


def test_hermes_delete_removes_learning_record() -> None:
    api = client()
    insight = api.post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "neutral",
            "lesson": "Delete stale Hermes lessons when they are no longer useful.",
            "conversation_id": "conv-delete-1",
            "tags": ["cleanup"],
            "weight": 2,
        },
    ).json()

    deleted = api.delete(f"/api/v1/admin/hermes/{insight['id']}", headers=headers())
    remaining = api.get("/api/v1/admin/hermes", headers=headers())
    missing = api.get(f"/api/v1/admin/hermes/{insight['id']}", headers=headers())

    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}
    assert all(item["id"] != insight["id"] for item in remaining.json())
    assert missing.status_code == 404


def test_hermes_feedback_rejects_sensitive_content_without_echoing_it() -> None:
    response = client().post(
        "/api/v1/admin/hermes/feedback",
        headers=headers(),
        json={
            "outcome": "failure",
            "lesson": "Do not store api_key sk-secret-value in memory.",
            "tags": ["security"],
            "weight": 10,
        },
    )

    assert response.status_code == 422
    assert "sk-secret-value" not in response.text


@pytest.mark.asyncio
async def test_persistent_admin_models_write_to_published_config() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=4,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.logical_model == "main"
    assert created.upstream_model == "deepseek-chat"
    assert configs.current is not None
    deployment, request, api_key = transport.calls[0]
    assert deployment.provider_model == "deepseek/deepseek-chat"
    assert deployment.request_model == "deepseek-chat"
    assert request.logical_model == "main"
    assert api_key == "sk-live"
    assert secrets.resolved == [
        (TENANT_ID, f"secret://{SECRET_ID}"),
    ]
    assert configs.current.document == {
        "models": {
            "main": {
                "deployments": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_base": "https://api.deepseek.com/v1",
                        "credential_ref": f"secret://{SECRET_ID}",
                        "quota_scope_id": "deepseek-account",
                        "max_concurrency": 4,
                        "target_utilization": 0.8,
                        "reserved_slots": 0,
                        "rpm": 60,
                        "tpm": 100000,
                        "capabilities": ["text", "tool_calling"],
                    }
                ]
            },
        },
        "agents": [],
    }


@pytest.mark.asyncio
async def test_persistent_admin_mcp_preserves_transport_connection_fields() -> None:
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    created = await service.upsert_mcp_server(
        McpServerRequest(
            id="local_mcp",
            name="Local MCP",
            transport="stdio",
            command="/usr/bin/python3",
            args=["/opt/mcp/server.py"],
            executable_allowlist=["/usr/bin/python3"],
            allowed_tools=["echo"],
            timeout_seconds=5,
        )
    )
    listed = await service.list_mcp_servers()

    assert created.transport == "stdio"
    assert created.command == "/usr/bin/python3"
    assert created.args == ["/opt/mcp/server.py"]
    assert created.executable_allowlist == ["/usr/bin/python3"]
    assert created.timeout_seconds == 5
    assert listed[0].transport == "stdio"


@pytest.mark.asyncio
async def test_persistent_admin_normalizes_openai_compatible_root_api_base() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="openai-compatible",
            api_base="https://gsykj.com",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="relay-account",
            max_concurrency=2,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://gsykj.com/v1"
    assert transport.calls[0][0].api_base == "https://gsykj.com/v1"
    assert configs.current is not None
    deployment = cast(
        dict[str, object],
        cast(dict[str, object], configs.current.document["models"])["main"],
    )["deployments"]
    assert cast(list[dict[str, object]], deployment)[0]["api_base"] == "https://gsykj.com/v1"


@pytest.mark.asyncio
async def test_persistent_admin_updates_existing_model_and_rechecks_availability() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=1,
            target_utilization=0.8,
        )
    )

    updated = await service.update_model(
        created.id,
        ModelDeploymentRequest(
            provider="openai-compatible",
            api_base="https://gsykj.com",
            upstream_model="deepseek-v4-flash",
            logical_model="planner",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="relay-account",
            max_concurrency=4,
            target_utilization=0.8,
            rpm=120,
            tpm=200000,
        ),
    )

    assert updated.logical_model == "planner"
    assert updated.provider == "openai-compatible"
    assert updated.api_base == "https://gsykj.com/v1"
    assert updated.max_concurrency == 4
    assert len(transport.calls) == 2
    assert configs.current is not None
    assert configs.current.document["models"] == {
        "planner": {
            "deployments": [
                {
                    "provider": "openai-compatible",
                    "model": "deepseek-v4-flash",
                    "api_base": "https://gsykj.com/v1",
                    "credential_ref": f"secret://{SECRET_ID}",
                    "quota_scope_id": "relay-account",
                    "max_concurrency": 4,
                    "target_utilization": 0.8,
                    "reserved_slots": 0,
                    "rpm": 120,
                    "tpm": 200000,
                    "capabilities": ["text", "tool_calling"],
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_persistent_admin_normalizes_anthropic_messages_api_base() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="claude-code-compatible",
            api_base="https://toapis.com/v1",
            api_protocol="anthropic_messages",
            upstream_model="claude-sonnet-4-6",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="anthropic-account",
            max_concurrency=2,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://toapis.com/v1/messages"
    assert created.api_protocol == "anthropic_messages"
    assert transport.calls[0][0].api_base == "https://toapis.com/v1/messages"


@pytest.mark.asyncio
async def test_persistent_admin_verifies_dedicated_main_agent_model() -> None:
    configs = FakeConfigService()
    secrets = FakeSecretService()
    transport = FakeModelTransport()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    response = await service.update_main_agent_config(
        MainAgentConfigRequest(
            model=MainAgentModelConfig(
                provider="claude-code-relay",
                api_base="https://toapis.com/v1",
                api_protocol="anthropic_messages",
                upstream_model="claude-sonnet-4-6",
                credential_ref=f"secret://{SECRET_ID}",
                capabilities=["text", "tool_calling"],
            ),
            control_mode="supervisor",
            hermes_policy="confirm_before_apply",
            decision_policy="choose mode and role pool; ask before workflow changes",
            operating_style="control the room and ask before changing a chosen workflow",
            direct_answerer="main_agent",
            max_review_rounds=2,
        )
    )

    assert response.model is not None
    assert response.model.api_base == "https://toapis.com/v1/messages"
    assert transport.calls[0][0].logical_model == "main_agent"
    assert transport.calls[0][0].api_base == "https://toapis.com/v1/messages"
    assert transport.calls[0][2] == "sk-live"


@pytest.mark.asyncio
async def test_persistent_admin_logs_dedicated_main_agent_model_failures() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(RuntimeError("provider returned status=401")),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.update_main_agent_config(
            MainAgentConfigRequest(
                model=MainAgentModelConfig(
                    provider="claude-code-relay",
                    api_base="https://bad-relay.example/v1",
                    api_protocol="anthropic_messages",
                    upstream_model="claude-sonnet-4-6",
                    credential_ref=f"secret://{SECRET_ID}",
                    capabilities=["text", "tool_calling"],
                ),
                control_mode="supervisor",
                hermes_policy="confirm_before_apply",
                decision_policy="choose mode and role pool; ask before workflow changes",
                operating_style="control the room and ask before changing a chosen workflow",
                direct_answerer="main_agent",
                max_review_rounds=2,
            )
        )

    assert error.value.code == "model_unavailable"
    assert error.value.details is not None
    assert error.value.details["provider"] == "claude-code-relay"
    assert error.value.details["logical_model"] == "main_agent"
    assert error.value.details["api_base"] == "https://bad-relay.example/v1/messages"
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].source == "main_agent.update"
    assert model_logs[0].message == "provider returned status=401"
    assert model_logs[0].details["provider"] == "claude-code-relay"
    assert model_logs[0].details["logical_model"] == "main_agent"
    assert model_logs[0].details["api_base"] == "https://bad-relay.example/v1/messages"
    serialized = model_logs[0].model_dump_json()
    assert "credential_ref" not in serialized
    assert "secret://" not in serialized


@pytest.mark.asyncio
async def test_persistent_admin_agents_write_to_published_config() -> None:
    configs = FakeConfigService()
    configs.current = ConfigRevision(
        id=uuid4(),
        tenant_id=TENANT_ID,
        version=1,
        status=ConfigStatus.PUBLISHED,
        document={
            "models": {
                "main": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "credential_ref": f"secret://{SECRET_ID}",
                            "quota_scope_id": "deepseek-account",
                        }
                    ]
                }
            },
            "agents": [],
        },
        created_by=ACTOR_ID,
        created_at=datetime.now(UTC),
    )
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    created = await service.upsert_agent(
        AgentResourceRequest(
            id="director",
            name="导演",
            enabled=True,
            role="短视频导演",
            prompt="负责拆解选题、镜头语言和成片节奏。",
            model="main",
            skills=["script_review"],
        )
    )
    listed = await service.list_agents()

    assert created.id == "director"
    assert created.role == "短视频导演"
    assert listed == (created,)
    assert configs.current is not None
    assert configs.current.version == 1
    assert configs.current.document["agents"] == [
        {
            "id": "director",
            "role": "短视频导演",
            "prompt": "负责拆解选题、镜头语言和成片节奏。",
            "model": "main",
            "skills": ["script_review"],
        }
    ]


@pytest.mark.asyncio
async def test_persistent_admin_model_is_not_published_when_availability_check_fails() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(RuntimeError("provider returned status=401")),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.create_model(
            ModelDeploymentRequest(
                provider="deepseek",
                api_base="https://api.deepseek.com/v1",
                upstream_model="deepseek-chat",
                logical_model="main",
                capabilities=["text"],
                credential_ref=f"secret://{SECRET_ID}",
                quota_scope="deepseek-account",
                max_concurrency=4,
                target_utilization=0.8,
                reserved_capacity=0,
                rpm=60,
                tpm=100000,
                queue_timeout_seconds=60,
                fallback=None,
                weight=100,
            )
        )

    assert error.value.code == "model_unavailable"
    assert "status=401" in error.value.public_message
    assert error.value.details == {
        "stage": "model_availability_check",
        "provider": "deepseek",
        "api_base": "https://api.deepseek.com/v1",
        "logical_model": "main",
        "upstream_model": "deepseek-chat",
        "status_code": "401",
        "reason": "provider returned status=401",
        "hint": "检查 API Key 是否有效、API Base 是否可从服务器访问、模型名是否属于该服务商账号。",
    }
    assert "sk-live" not in error.value.public_message
    assert "credential_ref" not in error.value.details
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].message == "provider returned status=401"
    assert model_logs[0].details["provider"] == "deepseek"
    assert model_logs[0].details["status_code"] == "401"
    assert configs.drafts == []
    assert configs.current is None


@pytest.mark.asyncio
async def test_persistent_admin_saves_multimedia_video_model_without_chat_probe() -> None:
    configs = FakeConfigService()
    transport = FakeModelTransport(RuntimeError("chat probe should not run"))
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="minimax",
            api_base="https://api.minimax.io",
            upstream_model="MiniMax-Hailuo-02",
            logical_model="video_primary",
            capabilities=["video_generation"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="minimax-video-account",
            max_concurrency=1,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=3,
            tpm=None,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://api.minimax.io/v1"
    assert created.capabilities == ["video_generation"]
    assert transport.calls == []
    assert configs.current is not None
    document = cast(dict[str, Any], configs.current.document)
    models = cast(dict[str, Any], document["models"])
    video_primary = cast(dict[str, Any], models["video_primary"])
    deployments = cast(list[dict[str, Any]], video_primary["deployments"])
    assert deployments[0]["model"] == "MiniMax-Hailuo-02"


@pytest.mark.asyncio
async def test_persistent_admin_saves_multimedia_audio_model_without_chat_probe() -> None:
    configs = FakeConfigService()
    transport = FakeModelTransport(RuntimeError("chat probe should not run"))
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="minimax",
            api_base="https://api.minimax.io",
            upstream_model="speech-2.8-turbo",
            logical_model="audio_primary",
            capabilities=["audio_generation"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="minimax-audio-account",
            max_concurrency=2,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=30,
            tpm=None,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    assert created.api_base == "https://api.minimax.io/v1"
    assert created.capabilities == ["audio_generation"]
    assert transport.calls == []
    assert configs.current is not None
    document = cast(dict[str, Any], configs.current.document)
    models = cast(dict[str, Any], document["models"])
    audio_primary = cast(dict[str, Any], models["audio_primary"])
    deployments = cast(list[dict[str, Any]], audio_primary["deployments"])
    assert deployments[0]["model"] == "speech-2.8-turbo"


@pytest.mark.asyncio
async def test_persistent_admin_deletes_model_deployment_and_publishes_config() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(),
    )

    created = await service.create_model(
        ModelDeploymentRequest(
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            upstream_model="deepseek-chat",
            logical_model="main",
            capabilities=["text", "tool_calling"],
            credential_ref=f"secret://{SECRET_ID}",
            quota_scope="deepseek-account",
            max_concurrency=4,
            target_utilization=0.8,
            reserved_capacity=0,
            rpm=60,
            tpm=100000,
            queue_timeout_seconds=60,
            fallback=None,
            weight=100,
        )
    )

    await service.delete_model(created.id)

    assert await service.list_models() == ()
    assert configs.current is not None
    assert configs.current.document["models"] == {}
    model_logs = await service.list_logs("model_error")
    serialized = "".join(item.model_dump_json() for item in model_logs)
    assert "secret://" not in serialized


@pytest.mark.asyncio
async def test_persistent_admin_model_logs_preflight_availability_failures() -> None:
    configs = FakeConfigService()
    service = PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=FakeModelTransport(),
    )

    with pytest.raises(PublicAPIError) as error:
        await service.create_model(
            ModelDeploymentRequest(
                provider="minimax",
                api_base="https://api.minimax.chat/v1",
                upstream_model="abab6.5s-chat",
                logical_model="vision_only",
                capabilities=["vision"],
                credential_ref=f"secret://{SECRET_ID}",
                quota_scope="minimax-account",
                max_concurrency=4,
                target_utilization=0.8,
                reserved_capacity=0,
                rpm=60,
                tpm=100000,
                queue_timeout_seconds=60,
                fallback=None,
                weight=100,
            )
        )

    assert error.value.code == "model_unavailable"
    model_logs = await service.list_logs("model_error")
    assert len(model_logs) == 1
    assert model_logs[0].message == "model availability check requires text capability"
    assert model_logs[0].details["provider"] == "minimax"
    assert model_logs[0].details["logical_model"] == "vision_only"
    serialized = model_logs[0].model_dump_json()
    assert "credential_ref" not in serialized
    assert "secret://" not in serialized
    assert configs.drafts == []
    assert configs.current is None


@pytest.mark.asyncio
async def test_persistent_admin_secret_uses_sealed_secret_service() -> None:
    secrets = FakeSecretService()
    service = PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=secrets,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
    )

    reference = await service.create_secret(
        SecretCreateRequest(label="deepseek", value=SecretStr("sk-live-1234"))
    )

    assert reference.ref == f"secret://{SECRET_ID}"
    assert reference.last_four == "1234"
    assert secrets.values == ["sk-live-1234"]

def test_evolution_run_records_skill_optimization_rounds_and_audit() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "skill_optimization",
            "title": "优化 darwin-skill",
            "objective": "对 darwin-skill 做标准三轮优化，保留有测试收益的版本。",
            "mode": "hybrid",
            "source_skill_ids": ["darwin-skill"],
            "target_artifact_type": "skill",
            "baseline_agent_id": "agent-main-m3",
            "candidate_agent_ids": ["agent-coder", "agent-reviewer"],
            "evaluator_agent_id": "agent-evaluator",
            "approval_policy": "ask",
            "iteration_policy": "score_gated",
            "memory_policy": "summarize_between_rounds",
            "max_rounds": 3,
            "min_delta": 2.0,
            "rubric": ["结构评分", "实测表现", "反例黑名单"],
        },
    )

    assert created.status_code == 200
    run = created.json()
    assert run["id"].startswith("evolution_")
    assert run["status"] == "waiting_approval"
    assert run["kind"] == "skill_optimization"
    assert run["baseline_agent_id"] == "agent-main-m3"
    assert run["candidate_agent_ids"] == ["agent-coder", "agent-reviewer"]
    assert run["evaluator_agent_id"] == "agent-evaluator"
    assert run["approval_status"] == "pending"
    assert run["next_action"] == "request_approval"

    blocked = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "未审批测试",
            "candidate_summary": "未审批前不应记录候选版本。",
            "score_before": 70.0,
            "score_after": 71.0,
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "evolution_run_requires_approval"

    approved = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/approve",
        headers=headers(),
        json={"approved": True, "note": "人工确认基准 agent 和评测口径。"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "running"
    assert approved.json()["approval_status"] == "approved"
    assert approved.json()["next_action"] == "run_next_round"

    recorded = api.post(
        f"/api/v1/admin/evolution-runs/{run['id']}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "实测表现",
            "candidate_summary": "补充 test-prompts 并减少自评偏差。",
            "score_before": 72.0,
            "score_after": 76.5,
            "tests_passed": True,
            "regression_detected": False,
            "judge_summary": "两个测试 prompt 均优于基线。",
            "artifact_refs": ["artifact://generated-skill/darwin-v2"],
            "tokens_used": 12000,
            "elapsed_seconds": 180,
        },
    )

    assert recorded.status_code == 200
    body = recorded.json()
    assert body["status"] == "running"
    assert body["next_action"] == "run_next_round"
    assert body["rounds"][0]["delta"] == 4.5
    assert body["rounds"][0]["accepted"] is True
    assert body["rounds"][0]["recommendation"] == "continue"

    listed = api.get("/api/v1/admin/evolution-runs", headers=headers())
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == run["id"]

    audit = api.get("/api/v1/admin/audit?action=evolution.round_recorded", headers=headers())
    assert audit.status_code == 200
    event = audit.json()[0]
    assert event["resource"] == f"evolution:{run['id']}"
    assert event["details"]["recommendation"] == "continue"
    assert event["details"]["next_action"] == "run_next_round"

    approval_audit = api.get("/api/v1/admin/audit?action=evolution.approve", headers=headers())
    assert approval_audit.status_code == 200
    assert approval_audit.json()[0]["details"]["approval_status"] == "approved"


def test_evolution_run_stops_after_two_low_delta_rounds() -> None:
    api = client()
    created = api.post(
        "/api/v1/admin/evolution-runs",
        headers=headers(),
        json={
            "kind": "academic_research",
            "title": "论文创新点发现",
            "objective": "迭代发现论文创新点并用反例筛选。",
            "mode": "discuss",
            "target_artifact_type": "research_gap",
            "approval_policy": "auto",
            "max_rounds": 5,
            "min_delta": 2.0,
        },
    )
    run_id = created.json()["id"]

    first = api.post(
        f"/api/v1/admin/evolution-runs/{run_id}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "可发表性",
            "candidate_summary": "提出一个小幅改进的研究 gap。",
            "score_before": 80.0,
            "score_after": 81.0,
            "tests_passed": True,
            "regression_detected": False,
        },
    )
    assert first.status_code == 200
    assert first.json()["status"] == "running"
    assert first.json()["rounds"][0]["recommendation"] == "observe_one_more_round"

    second = api.post(
        f"/api/v1/admin/evolution-runs/{run_id}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "反例验证",
            "candidate_summary": "反例检查后只有小幅提升。",
            "score_before": 81.0,
            "score_after": 81.7,
            "tests_passed": True,
            "regression_detected": False,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "stopped"
    assert body["rounds"][1]["recommendation"] == "stop"
    assert body["stop_reason"] == "two consecutive rounds below minimum delta"

    rejected = api.post(
        f"/api/v1/admin/evolution-runs/{run_id}/rounds",
        headers=headers(),
        json={
            "changed_dimension": "继续迭代",
            "candidate_summary": "不应继续执行。",
            "score_before": 81.7,
            "score_after": 82.0,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "evolution_run_closed"
