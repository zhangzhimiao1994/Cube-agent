"""Ordinary reconnect reads expose only an owned, unaccepted repair decision."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.registry import RuntimeRegistry
from tests.api.test_runs_api import StubAuthService, bearer

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OWNER = UUID("22222222-2222-4222-8222-222222222222")
RUN = UUID("33333333-3333-4333-8333-333333333333")
TOKEN = "synthetic-repair-decision-abcdefghijklmnopqrstuvwxyz"


def repair_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: RunStatus = RunStatus.FAILED,
    routing_changes: dict[str, object] | None = None,
    actor: UUID = OWNER,
) -> TestClient:
    routing: dict[str, object] = {
        "approval_kind": "self_repair",
        "decision_token": TOKEN,
        "repair_proposal": {
            "kind": "self_repair", "attempt": 1, "max_attempts": 1,
            "source_run_id": str(RUN), "source_event_sequence": 174,
            "requires_approval": True,
            "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
            "authorization": "synthetic-private-field-must-not-escape",
        },
    }
    routing.update(routing_changes or {})
    row = RunRow(
        id=RUN, tenant_id=TENANT, actor_id=OWNER, actor_role=Role.OPERATOR.value,
        request="recover the owned workspace", mode=TaskMode.DISPATCH.value,
        status=status.value, version=6, created_at=datetime.now(UTC),
        routing_decision=routing,
    )
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.scalar.return_value = row
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], MagicMock(return_value=session)))
    monkeypatch.setattr(repository, "completed_step_ids", AsyncMock(return_value=("architect_step",)))
    monkeypatch.setattr(repository, "artifact_ids", AsyncMock(return_value=()))
    monkeypatch.setattr(repository, "usage_cost", AsyncMock(return_value=Decimal(0)))
    service = RunService(
        repository, runtime_registry=cast(RuntimeRegistry, object()), router=None,
        task_queue=cast(TaskQueue, object()),
    )
    principal = AuthenticatedPrincipal(actor, TENANT, Role.OPERATOR)
    return TestClient(create_app(
        auth_service=StubAuthService(principal), rate_limiter=object(),
        config_service=object(), run_service=service,
    ))


@pytest.mark.parametrize("suffix", ["", "/details"])
@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.WAITING_APPROVAL])
def test_owned_pending_repair_survives_ordinary_reconnect(
    monkeypatch: pytest.MonkeyPatch, suffix: str, status: RunStatus,
) -> None:
    client = repair_client(monkeypatch, status=status)
    response = client.get(f"/api/v1/runs/{RUN}{suffix}", headers=bearer())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status.value
    assert body["version"] == 6
    assert body["decision_token"] == TOKEN
    assert body["approval_kind"] == "self_repair"
    assert body["repair_proposal"]["kind"] == "self_repair"
    assert body["repair_proposal"]["source_event_sequence"] == 174
    assert "synthetic-private-field-must-not-escape" not in response.text


@pytest.mark.parametrize("status,changes", [
    (RunStatus.FAILED, {"source": "self_repair"}),
    (RunStatus.FAILED, {"self_repair_accepted": True}),
    (RunStatus.WAITING_APPROVAL, {"self_repair_accepted": True}),
    (RunStatus.CANCELLED, {}),
    (RunStatus.COMPLETED, {}),
    (RunStatus.RUNNING, {}),
    (RunStatus.FAILED, {"repair_proposal": None}),
    (RunStatus.FAILED, {"decision_token": None}),
    pytest.param(RunStatus.FAILED, {"decision_token": " "}, id="blank_token"),
    pytest.param(RunStatus.FAILED, {"repair_proposal": {
        "kind": "self_repair", "source_run_id": str(UUID(int=99)), "requires_approval": True,
    }}, id="wrong_source_run"),
    pytest.param(RunStatus.FAILED, {"repair_proposal": {
        "kind": "self_repair", "source_run_id": str(RUN), "requires_approval": False,
    }}, id="not_an_approval"),
])
def test_nonactionable_repair_does_not_expose_decision(
    monkeypatch: pytest.MonkeyPatch, status: RunStatus, changes: dict[str, object],
) -> None:
    client = repair_client(monkeypatch, status=status, routing_changes=changes)
    response = client.get(f"/api/v1/runs/{RUN}/details", headers=bearer())
    assert response.status_code == 200
    body = response.json()
    assert body["decision_token"] is None
    assert body["approval_kind"] is None
    assert body.get("repair_proposal") is None
    assert TOKEN not in response.text


@pytest.mark.parametrize("suffix", ["", "/details"])
def test_other_actor_cannot_read_repair_decision(
    monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    client = repair_client(monkeypatch, actor=UUID("44444444-4444-4444-8444-444444444444"))
    response = client.get(f"/api/v1/runs/{RUN}{suffix}", headers=bearer())
    assert response.status_code == 404
    assert TOKEN not in response.text
    assert "repair_proposal" not in response.text
