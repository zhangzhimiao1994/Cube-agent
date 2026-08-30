from __future__ import annotations

import io
import json
import tarfile
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.service import RunSummary, SubmittedRun


class StubAuthService:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return self.principal


@dataclass(slots=True)
class StubSettingsService:
    vibe_coding_enabled: bool = False
    audit_events: list[dict[str, object]] = field(default_factory=list)

    async def get_settings(self) -> object:
        return type("Settings", (), {"vibe_coding_enabled": self.vibe_coding_enabled})()

    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object],
    ) -> object:
        self.audit_events.append({"actor": actor, "action": action, "resource": resource, "details": details})
        return object()


@dataclass(slots=True)
class StubRunService:
    submitted: list[
        tuple[
            UUID,
            UUID,
            str,
            TaskMode,
            tuple[str, ...],
            str | None,
            bool,
            str | None,
            str | None,
            tuple[str, ...],
            bool,
        ]
    ]
    enqueue_count: int = 0
    direct_models: list[str | None] | None = None
    vibe_coding_flags: list[bool] | None = None
    actor_roles: list[Role | None] = field(default_factory=list)

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_role: Role | None = None,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...] = (),
        workflow_id: str | None = None,
        allow_workflow_adjustment: bool = False,
        conversation_id: str | None = None,
        reference_conversation_id: str | None = None,
        attachment_ids: tuple[str, ...] = (),
        direct_model: str | None = None,
        vibe_coding: bool = False,
        skip_evolution_proposal: bool = False,
        idempotency_key: str | None = None,
    ) -> SubmittedRun:
        del idempotency_key
        self.actor_roles.append(actor_role)
        if self.direct_models is not None:
            self.direct_models.append(direct_model)
        if self.vibe_coding_flags is not None:
            self.vibe_coding_flags.append(vibe_coding)
        self.submitted.append(
            (
                tenant_id,
                actor_id,
                message,
                mode,
                agent_ids,
                workflow_id,
                allow_workflow_adjustment,
                conversation_id,
                reference_conversation_id,
                attachment_ids,
                skip_evolution_proposal,
            )
        )
        if not skip_evolution_proposal and "进化 darwin-skill" in message:
            return SubmittedRun(
                id=uuid4(),
                tenant_id=tenant_id,
                status=RunStatus.WAITING_APPROVAL,
                mode=TaskMode.HYBRID,
                decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
                version=1,
                clarification_reason="evolution_requires_user_confirmation",
                conversation_id=conversation_id or "conv-test",
                reference_conversation_id=reference_conversation_id,
                evolution_proposal={
                    "kind": "skill_optimization",
                    "title": "Skill 进化任务",
                    "objective": message,
                    "mode": "hybrid",
                    "source_skill_ids": ["darwin-skill"],
                    "source_conversation_id": conversation_id or "conv-test",
                    "source_run_id": None,
                    "target_artifact_type": "skill",
                    "baseline_agent_id": "main-agent",
                    "candidate_agent_ids": ["worker-agent", "reviewer-agent"],
                    "evaluator_agent_id": "evaluator-agent",
                    "approval_policy": "ask",
                    "iteration_policy": "score_gated",
                    "memory_policy": "summarize_between_rounds",
                    "max_rounds": 5,
                    "min_delta": 2.0,
                    "budget_tokens": 200000,
                    "budget_minutes": 120,
                    "rubric": ["实测表现", "反例覆盖", "人工验收"],
                    "summary": "主 Agent 判断这条消息适合进入进化任务。",
                    "metadata": {"source": "chat_evolution_proposal", "requires_user_confirmation": "true"},
                },
            )
        if not skip_evolution_proposal and "生成一个相关的 skill" in message:
            return SubmittedRun(
                id=uuid4(),
                tenant_id=tenant_id,
                status=RunStatus.WAITING_APPROVAL,
                mode=TaskMode.HYBRID,
                decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
                version=1,
                clarification_reason="evolution_requires_user_confirmation",
                conversation_id=conversation_id or "conv-test",
                reference_conversation_id=reference_conversation_id,
                evolution_proposal={
                    "kind": "skill_distillation",
                    "title": "Skill 创建任务",
                    "objective": message,
                    "mode": "hybrid",
                    "source_skill_ids": [],
                    "source_conversation_id": conversation_id or "conv-test",
                    "source_run_id": None,
                    "target_artifact_type": "skill",
                    "baseline_agent_id": "main-agent",
                    "candidate_agent_ids": ["worker-agent", "reviewer-agent"],
                    "evaluator_agent_id": "evaluator-agent",
                    "approval_policy": "ask",
                    "iteration_policy": "score_gated",
                    "memory_policy": "summarize_between_rounds",
                    "max_rounds": 5,
                    "min_delta": 2.0,
                    "budget_tokens": 200000,
                    "budget_minutes": 120,
                    "rubric": ["实测表现", "反例覆盖", "人工验收"],
                    "summary": "主 Agent 判断这条消息是在创建可沉淀的 Skill：先收敛目标和输入资料，再生成 SKILL.md、references/scripts/assets，并用真实任务验收。",
                    "metadata": {"source": "chat_evolution_proposal", "requires_user_confirmation": "true"},
                },
            )
        if "OpenClaw" in message and "execute date" in message:
            return SubmittedRun(
                id=uuid4(),
                tenant_id=tenant_id,
                status=RunStatus.WAITING_APPROVAL,
                mode=TaskMode.DISPATCH,
                decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
                version=1,
                clarification_reason="openclaw_requires_user_confirmation",
                conversation_id=conversation_id or "conv-test",
                reference_conversation_id=reference_conversation_id,
                openclaw_proposal={
                    "kind": "server_command",
                    "platform": "linux",
                    "target_type": "server",
                    "target": "linux-server",
                    "operation_text": message,
                    "source_conversation_id": conversation_id or "conv-test",
                    "summary": "User confirmation is required before creating an OpenClaw operation.",
                    "metadata": {"source": "chat_openclaw_proposal", "requires_user_confirmation": "true"},
                },
            )
        status = RunStatus.WAITING_USER_MODE if mode is TaskMode.AUTO else RunStatus.QUEUED
        if status is RunStatus.QUEUED:
            self.enqueue_count += 1
        return SubmittedRun(
            id=uuid4(),
            tenant_id=tenant_id,
            status=status,
            mode=None if status is RunStatus.WAITING_USER_MODE else mode,
            decision_token="safe-decision-token-abcdefghijklmnopqrstuvwxyz1234"
            if status is RunStatus.WAITING_USER_MODE
            else None,
            version=1,
            clarification_reason="routing_requires_user_choice"
            if status is RunStatus.WAITING_USER_MODE
            else None,
            conversation_id=conversation_id or "conv-test",
            reference_conversation_id=reference_conversation_id,
        )

    async def choose_mode(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        mode: TaskMode,
        decision_token: str,
        version: int,
        operator_note: str | None = None,
    ) -> SubmittedRun:
        del actor_id, decision_token, version, operator_note
        return SubmittedRun(
            id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.QUEUED,
            mode=mode,
            decision_token=None,
            version=2,
            clarification_reason=None,
        )

    async def approve_temporary_agent(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
    ) -> SubmittedRun:
        del actor_id, decision_token, version
        return SubmittedRun(
            id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.QUEUED,
            mode=TaskMode.DISPATCH,
            decision_token=None,
            version=2,
            clarification_reason=None,
            temporary_agent_proposal=None,
        )

    async def revise_temporary_agent(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        decision_token: str,
        version: int,
        feedback: str,
    ) -> SubmittedRun:
        del actor_id, decision_token, version, feedback
        return SubmittedRun(
            id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.QUEUED,
            mode=TaskMode.DISPATCH,
            decision_token=None,
            version=2,
            clarification_reason=None,
            temporary_agent_proposal=None,
        )

    async def get(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        return RunSummary(
            id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.RUNNING,
            mode=TaskMode.DISPATCH,
            request="safe request",
            completed_step_ids=("research",),
            artifact_ids=(uuid4(),),
            usage_cost_usd=Decimal("0.00"),
        )

    async def events(self, tenant_id: UUID, run_id: UUID) -> tuple[dict[str, object], ...]:
        del tenant_id
        return (
            {"sequence": 1, "run_id": str(run_id), "kind": "step.started", "payload": {"ok": True}},
            {
                "sequence": 2,
                "run_id": str(run_id),
                "kind": "custom.progress",
                "payload": {"provider_model": "openai/gpt-4o-mini"},
            },
        )

    async def pause(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        return await self.get(tenant_id, run_id)

    async def resume(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        return await self.get(tenant_id, run_id)

    async def cancel(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        summary = await self.get(tenant_id, run_id)
        return RunSummary(
            id=summary.id,
            tenant_id=summary.tenant_id,
            status=RunStatus.CANCELLED,
            mode=summary.mode,
            request=summary.request,
            completed_step_ids=summary.completed_step_ids,
            artifact_ids=summary.artifact_ids,
            usage_cost_usd=summary.usage_cost_usd,
        )


def _client(
    role: Role = Role.OPERATOR,
    *,
    attachment_store_dir: Path | None = None,
    settings_service: StubSettingsService | None = None,
) -> tuple[TestClient, StubRunService, AuthenticatedPrincipal]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), role)
    service = StubRunService([])
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=settings_service,
        run_service=service,
    )
    if attachment_store_dir is not None:
        app.state.attachment_store_dir = attachment_store_dir
    return TestClient(app), service, principal


def bearer() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def test_low_confidence_submission_returns_202_waiting_user_mode_and_does_not_enqueue_runtime() -> (
    None
):
    client, service, principal = _client()

    response = client.post(
        "/api/v1/runs",
        headers={**bearer(), "Idempotency-Key": "request-1"},
        json={"message": "ambiguous task", "mode": "auto"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_user_mode"
    assert body["mode"] is None
    assert body["decision_token"] is not None
    assert service.submitted == [
        (
            principal.tenant_id,
            principal.user_id,
            "ambiguous task",
            TaskMode.AUTO,
            (),
            None,
            False,
            None,
            None,
            (),
            False,
        )
    ]
    assert service.enqueue_count == 0


def test_run_submission_records_user_conversation_audit_event() -> None:
    settings_service = StubSettingsService()
    client, service, principal = _client(settings_service=settings_service)

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "请分析这个产品方案并给出下一步。",
            "mode": "hybrid",
            "agent_ids": ["researcher"],
            "workflow_id": "market-review",
            "conversation_id": "conv-audit-1",
            "reference_conversation_id": "conv-previous",
            "vibe_coding": False,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert len(settings_service.audit_events) == 1
    event = settings_service.audit_events[0]
    assert event["actor"] == str(principal.user_id)
    assert event["action"] == "run.submit"
    assert event["resource"] == body["id"]
    details = cast(dict[str, object], event["details"])
    assert details["user_id"] == str(principal.user_id)
    assert details["user_role"] == principal.role.value
    assert details["run_id"] == body["id"]
    assert details["conversation_id"] == "conv-audit-1"
    assert details["reference_conversation_id"] == "conv-previous"
    assert details["mode"] == "hybrid"
    assert details["accepted_mode"] == "hybrid"
    assert details["status"] == "queued"
    assert details["agent_ids"] == ["researcher"]
    assert details["workflow_id"] == "market-review"
    assert details["attachment_count"] == 0
    assert details["message_preview"] == "请分析这个产品方案并给出下一步。"
    assert isinstance(details["message_sha256"], str)
    assert service.enqueue_count == 1
    assert service.actor_roles == [principal.role]

def test_direct_submission_forwards_selected_model_without_agent_ids() -> None:
    client, service, principal = _client()
    service.direct_models = []

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={"message": "answer directly", "mode": "direct", "direct_model": "coder"},
    )

    assert response.status_code == 202
    assert service.direct_models == ["coder"]
    assert service.submitted == [
        (
            principal.tenant_id,
            principal.user_id,
            "answer directly",
            TaskMode.DIRECT,
            (),
            None,
            False,
            None,
            None,
            (),
            False,
        )
    ]


def test_run_submission_forwards_skip_evolution_proposal_flag() -> None:
    client, service, principal = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "请进化 darwin-skill，做多轮迭代",
            "mode": "auto",
            "skip_evolution_proposal": True,
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "waiting_user_mode"
    assert response.json().get("evolution_proposal") is None
    assert service.submitted == [
        (
            principal.tenant_id,
            principal.user_id,
            "请进化 darwin-skill，做多轮迭代",
            TaskMode.AUTO,
            (),
            None,
            False,
            None,
            None,
            (),
            True,
        )
    ]


def test_vibe_coding_submission_is_rejected_when_system_switch_is_disabled() -> None:
    client, service, _ = _client(settings_service=StubSettingsService(vibe_coding_enabled=False))
    service.vibe_coding_flags = []

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={"message": "review this repo", "mode": "hybrid", "vibe_coding": True},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vibe_coding_disabled"
    assert service.vibe_coding_flags == []


def test_vibe_coding_submission_is_forwarded_when_system_switch_is_enabled() -> None:
    client, service, principal = _client(settings_service=StubSettingsService(vibe_coding_enabled=True))
    service.vibe_coding_flags = []

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "review this repo",
            "mode": "hybrid",
            "conversation_id": "conv-vibe-coding",
            "vibe_coding": True,
        },
    )

    assert response.status_code == 202
    assert response.json()["conversation_id"] == "conv-vibe-coding"
    assert service.vibe_coding_flags == [True]
    assert service.submitted == [
        (
            principal.tenant_id,
            principal.user_id,
            "review this repo",
            TaskMode.HYBRID,
            (),
            None,
            False,
            "conv-vibe-coding",
            None,
            (),
            False,
        )
    ]


def test_evolution_proposal_is_returned_from_run_submission() -> None:
    client, _, _ = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "请进化 darwin-skill，做多轮迭代",
            "mode": "auto",
            "conversation_id": "conv-evolution-api",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_approval"
    assert body["clarification_reason"] == "evolution_requires_user_confirmation"
    assert body["evolution_proposal"]["kind"] == "skill_optimization"
    assert body["evolution_proposal"]["source_skill_ids"] == ["darwin-skill"]
    assert body["evolution_proposal"]["baseline_agent_id"] == "main-agent"
    assert body["evolution_proposal"]["source_conversation_id"] == "conv-evolution-api"


def test_normal_iterative_plan_submission_does_not_return_evolution_proposal() -> None:
    client, _, _ = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "这个 AI 科研项目需要长期迭代，先帮我给出研究方案和资料检索计划",
            "mode": "auto",
            "conversation_id": "conv-normal-plan-api",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["clarification_reason"] != "evolution_requires_user_confirmation"
    assert body["evolution_proposal"] is None


def test_skill_creation_submission_returns_grounded_evolution_proposal() -> None:
    client, _, _ = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "我想进行 AI 方面的科研，给我生成一个相关的 skill，并用真实资料和测试任务验收",
            "mode": "auto",
            "conversation_id": "conv-skill-create-api",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_approval"
    assert body["clarification_reason"] == "evolution_requires_user_confirmation"
    assert body["evolution_proposal"]["kind"] == "skill_distillation"
    assert body["evolution_proposal"]["title"] == "Skill 创建任务"
    assert body["evolution_proposal"]["target_artifact_type"] == "skill"
    assert "真实任务验收" in body["evolution_proposal"]["summary"]


def test_openclaw_proposal_is_returned_from_run_submission() -> None:
    client, _, _ = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "Use OpenClaw to execute date on the Linux server after approval.",
            "mode": "auto",
            "conversation_id": "conv-openclaw-api",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "waiting_approval"
    assert body["clarification_reason"] == "openclaw_requires_user_confirmation"
    assert body["openclaw_proposal"]["kind"] == "server_command"
    assert body["openclaw_proposal"]["platform"] == "linux"
    assert body["openclaw_proposal"]["target_type"] == "server"
    assert body["openclaw_proposal"]["source_conversation_id"] == "conv-openclaw-api"


def test_submission_forwards_selected_workflow_and_agents() -> None:
    client, service, principal = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "make a short video script",
            "mode": "dispatch",
            "workflow_id": "short-video-dispatch",
            "allow_workflow_adjustment": True,
            "conversation_id": "conv-short-video",
            "reference_conversation_id": "conv-previous",
            "agent_ids": ["director", "copywriter", "editor"],
        },
    )

    assert response.status_code == 202
    assert response.json()["conversation_id"] == "conv-short-video"
    assert response.json()["reference_conversation_id"] == "conv-previous"
    assert service.submitted == [
        (
            principal.tenant_id,
            principal.user_id,
            "make a short video script",
            TaskMode.DISPATCH,
            ("director", "copywriter", "editor"),
            "short-video-dispatch",
            True,
            "conv-short-video",
            "conv-previous",
            (),
            False,
        )
    ]


def test_attachment_upload_stores_image_without_exposing_server_path(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)

    response = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": "screen.png",
            "Content-Type": "image/png",
        },
        content=b"\x89PNG\r\n\x1a\nimage-bytes",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "screen.png"
    assert body["kind"] == "image"
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(b"\x89PNG\r\n\x1a\nimage-bytes")
    assert body["sha256"]
    assert "path" not in body
    assert list(tmp_path.rglob("*.bin"))

def test_attachment_upload_decodes_percent_encoded_filename_header(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)
    filename = "截图 方案.png"

    response = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": quote(filename, safe=""),
            "X-Agent-Hub-Filename-Encoding": "percent",
            "Content-Type": "image/png",
        },
        content=b"image-bytes",
    )

    assert response.status_code == 200
    assert response.json()["filename"] == filename
    assert response.json()["kind"] == "image"


def test_attachment_upload_extracts_common_archive_manifest_safely(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for name, content in {
            "src/main.py": b"print('hello')\n",
            "README.md": b"# demo\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))

    response = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": "source-package.tar.gz",
            "Content-Type": "application/gzip",
        },
        content=archive_buffer.getvalue(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "source-package.tar.gz"
    assert body["kind"] == "archive"
    assert body["content_type"] == "application/gzip"
    assert "path" not in body
    manifest_path = next(tmp_path.rglob(f"{body['id']}.manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["archive"]["extracted"] is True
    assert [item["path"] for item in manifest["files"]] == ["src/main.py", "README.md"]
    assert (manifest_path.parent / body["id"] / "src" / "main.py").is_file()


def test_attachment_management_lists_uploaded_metadata(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)

    uploaded = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": "notes.txt",
            "Content-Type": "text/plain",
        },
        content=b"hello",
    )
    listed = client.get("/api/v1/runs/attachments", headers=bearer())

    assert uploaded.status_code == 200
    assert listed.status_code == 200
    assert listed.json()["items"] == [uploaded.json()]


def test_attachment_management_deletes_data_metadata_manifest_and_extract_dir(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("src/main.py", "print('hello')\n")

    uploaded = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": "source.zip",
            "Content-Type": "application/zip",
        },
        content=archive_buffer.getvalue(),
    )
    attachment_id = uploaded.json()["id"]
    tenant_dir = next(tmp_path.iterdir())
    assert (tenant_dir / f"{attachment_id}.bin").is_file()
    assert (tenant_dir / f"{attachment_id}.json").is_file()
    assert (tenant_dir / f"{attachment_id}.manifest.json").is_file()
    assert (tenant_dir / attachment_id / "src" / "main.py").is_file()

    deleted = client.delete(f"/api/v1/runs/attachments/{attachment_id}", headers=bearer())
    listed = client.get("/api/v1/runs/attachments", headers=bearer())

    assert deleted.status_code == 200
    assert deleted.json() == {"id": attachment_id, "deleted": True}
    assert listed.json()["items"] == []
    assert not (tenant_dir / f"{attachment_id}.bin").exists()
    assert not (tenant_dir / f"{attachment_id}.json").exists()
    assert not (tenant_dir / f"{attachment_id}.manifest.json").exists()
    assert not (tenant_dir / attachment_id).exists()


def test_attachment_management_bulk_deletes_uploaded_attachments(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)
    first = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": "first.txt",
            "Content-Type": "text/plain",
        },
        content=b"first",
    ).json()
    second = client.post(
        "/api/v1/runs/attachments/upload",
        headers={
            **bearer(),
            "X-Agent-Hub-Filename": "second.txt",
            "Content-Type": "text/plain",
        },
        content=b"second",
    ).json()

    deleted = client.post(
        "/api/v1/runs/attachments/bulk-delete",
        headers=bearer(),
        json={"ids": [first["id"], second["id"], "att_deadbeefdeadbeefdeadbeefdeadbeef"]},
    )
    listed = client.get("/api/v1/runs/attachments", headers=bearer())

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == [first["id"], second["id"]]
    assert deleted.json()["failed"] == [
        {
            "id": "att_deadbeefdeadbeefdeadbeefdeadbeef",
            "code": "attachment_not_found",
            "message": "attachment was not found",
        }
    ]
    assert listed.json()["items"] == []


def test_attachment_management_bulk_delete_accepts_large_selection(tmp_path: Path) -> None:
    client, _, _ = _client(attachment_store_dir=tmp_path)
    ids = [f"att_{index:032x}" for index in range(201)]

    response = client.post(
        "/api/v1/runs/attachments/bulk-delete",
        headers=bearer(),
        json={"ids": ids},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == []
    assert [item["id"] for item in response.json()["failed"]] == ids
    assert {item["code"] for item in response.json()["failed"]} == {"attachment_not_found"}


def test_submission_forwards_attachment_ids() -> None:
    client, service, principal = _client()

    response = client.post(
        "/api/v1/runs",
        headers=bearer(),
        json={
            "message": "review this code package",
            "mode": "dispatch",
            "attachment_ids": ["att_0123456789abcdef0123456789abcdef"],
        },
    )

    assert response.status_code == 202
    assert service.submitted == [
        (
            principal.tenant_id,
            principal.user_id,
            "review this code package",
            TaskMode.DISPATCH,
            (),
            None,
            False,
            None,
            None,
            ("att_0123456789abcdef0123456789abcdef",),
            False,
        )
    ]


def test_choose_mode_enqueues_waiting_run_safely() -> None:
    client, _, _ = _client()
    run_id = uuid4()

    response = client.post(
        f"/api/v1/runs/{run_id}/choose-mode",
        headers=bearer(),
        json={
            "mode": "dispatch",
            "decision_token": "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            "version": 1,
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["mode"] == "dispatch"


def test_approve_temporary_agent_queues_confirmed_run_safely() -> None:
    client, _, _ = _client()
    run_id = uuid4()

    response = client.post(
        f"/api/v1/runs/{run_id}/approve-temporary-agent",
        headers=bearer(),
        json={
            "decision_token": "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            "version": 1,
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["mode"] == "dispatch"


def test_revise_temporary_agent_accepts_user_feedback_and_queues_run() -> None:
    client, _, _ = _client()
    run_id = uuid4()

    response = client.post(
        f"/api/v1/runs/{run_id}/revise-temporary-agent",
        headers=bearer(),
        json={
            "decision_token": "safe-decision-token-abcdefghijklmnopqrstuvwxyz1234",
            "version": 1,
            "feedback": "不要加工程师，先让产品经理重新拆任务。",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["mode"] == "dispatch"


def test_viewer_can_read_but_cannot_create_or_control_runs() -> None:
    client, _, _ = _client(Role.VIEWER)
    run_id = uuid4()

    create = client.post(
        "/api/v1/runs", headers=bearer(), json={"message": "safe task", "mode": "direct"}
    )
    read = client.get(f"/api/v1/runs/{run_id}", headers=bearer())
    cancel = client.post(f"/api/v1/runs/{run_id}/cancel", headers=bearer())

    assert create.status_code == 403
    assert read.status_code == 200
    assert cancel.status_code == 403


def test_run_events_and_details_never_expose_credentials_or_hidden_reasoning() -> None:
    client, _, _ = _client()
    run_id = uuid4()

    events = client.get(f"/api/v1/runs/{run_id}/events", headers=bearer())
    details = client.get(f"/api/v1/runs/{run_id}/details", headers=bearer())

    assert events.status_code == 200
    assert details.status_code == 200
    serialized = events.text + details.text + client.get("/openapi.json").text
    for forbidden in (
        "api_key",
        "secret_ref",
        "authorization",
        "hidden_reasoning",
        "chain_of_thought",
    ):
        assert forbidden not in serialized.lower()
