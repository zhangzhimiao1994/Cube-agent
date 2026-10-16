from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound
from agent_hub.runs.service import SubmittedRun
from tests.api.test_runs_api import StubRunService, _client, bearer


def test_retry_recovery_passes_exact_owner_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, principal = _client()
    run_id = uuid4()
    calls: list[int] = []

    async def retry(
        self: StubRunService, *, tenant_id: UUID, actor_id: UUID, run_id: UUID, version: int,
    ) -> SubmittedRun:
        assert tenant_id == principal.tenant_id and actor_id == principal.user_id
        calls.append(version)
        return SubmittedRun(
            id=run_id, tenant_id=tenant_id, status=RunStatus.QUEUED, mode=TaskMode.DISPATCH,
            decision_token=None, version=version + 1, clarification_reason=None,
        )

    monkeypatch.setattr(StubRunService, "retry_recovery", retry, raising=False)
    response = client.post(f"/api/v1/runs/{run_id}/retry-recovery", headers=bearer(), json={"version": 9})
    assert response.status_code == 202
    assert response.json()["id"] == str(run_id)
    assert response.json()["version"] == 10 and response.json()["status"] == "queued"
    assert calls == [9]


@pytest.mark.parametrize("error,code", [(RunConflict, 409), (RunNotFound, 404)])
def test_retry_recovery_maps_bound_refusals(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception], code: int,
) -> None:
    client, _, _ = _client()

    async def retry(self: StubRunService, **kwargs: object) -> SubmittedRun:
        raise error("recovery retry refused")

    monkeypatch.setattr(StubRunService, "retry_recovery", retry, raising=False)
    response = client.post(f"/api/v1/runs/{uuid4()}/retry-recovery", headers=bearer(), json={"version": 9})
    assert response.status_code == code


@pytest.mark.parametrize("body", [{}, {"version": 0}, {"version": True}, {"version": "9"},
                                  {"version": 9, "decision_token": "not accepted"}])
def test_retry_recovery_requires_strict_version_only(body: dict[str, object]) -> None:
    client, _, _ = _client()
    response = client.post(f"/api/v1/runs/{uuid4()}/retry-recovery", headers=bearer(), json=body)
    assert response.status_code == 422


def test_retry_recovery_requires_resume_permission() -> None:
    client, _, _ = _client(Role.VIEWER)
    response = client.post(f"/api/v1/runs/{uuid4()}/retry-recovery", headers=bearer(), json={"version": 9})
    assert response.status_code == 403
