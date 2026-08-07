from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.runs.service import RunSummary
from tests.api.test_runs_api import StubRunService


class TenantAuth:
    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return AuthenticatedPrincipal(
            UUID("11111111-1111-4111-8111-111111111111"),
            UUID("00000000-0000-4000-8000-000000000001"),
            Role.OPERATOR,
        )


class TenantScopedRunService(StubRunService):
    async def get(self, tenant_id: UUID, run_id: UUID) -> RunSummary:
        assert tenant_id == UUID("00000000-0000-4000-8000-000000000001")
        return await super().get(tenant_id, run_id)


def test_run_detail_uses_authenticated_tenant_scope() -> None:
    service = TenantScopedRunService([])
    app = create_app(auth_service=TenantAuth(), rate_limiter=object(), run_service=service)
    response = TestClient(app).get(
        "/api/v1/runs/22222222-2222-4222-8222-222222222222",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 200

