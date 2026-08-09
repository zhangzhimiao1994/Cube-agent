from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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
        ]
    ]
    enqueue_count: int = 0

    async def submit(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...] = (),
        workflow_id: str | None = None,
        allow_workflow_adjustment: bool = False,
        conversation_id: str | None = None,
        reference_conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SubmittedRun:
        del idempotency_key
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
            )
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
    ) -> SubmittedRun:
        del actor_id, decision_token, version
        return SubmittedRun(
            id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.QUEUED,
            mode=mode,
            decision_token=None,
            version=2,
            clarification_reason=None,
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
) -> tuple[TestClient, StubRunService, AuthenticatedPrincipal]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), role)
    service = StubRunService([])
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        run_service=service,
    )
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
        )
    ]
    assert service.enqueue_count == 0


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
