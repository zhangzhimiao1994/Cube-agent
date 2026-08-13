import asyncio
import io
import sys
import tarfile
import zipfile
from datetime import UTC, datetime
from typing import Any, cast
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
    _run_debug_from_detail,
)
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import RunRecord
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
    assert body["name"] == "safe_skill"
    assert body["status"] == "scanned"
    assert body["requested_permissions"] == ["tool:filesystem.read"]
    assert any("content sha256" in item for item in body["scan_diff"])
    assert skills.json()[0]["id"] == body["id"]


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
    assert body["name"] == "safe_tar_skill"
    assert body["status"] == "scanned"
    assert any(item["id"] == body["id"] for item in skills.json())


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
