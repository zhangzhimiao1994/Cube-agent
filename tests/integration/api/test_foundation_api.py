from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.app import create_app
from agent_hub.auth.models import Role
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.config.service import ConfigService
from agent_hub.db.models import TenantRow


class AllowAllLimiter:
    async def check(self, endpoint: str, client_ip: str) -> object:
        del endpoint, client_ip

        class Decision:
            allowed = True
            retry_after = 0

        return Decision()


def document(prompt: str) -> dict[str, object]:
    return {
        "models": {
            "main": {
                "deployments": [
                    {
                        "provider": "openai",
                        "model": "gpt-test",
                        "secret_ref": "OPENAI_API_KEY",
                        "quota_scope_id": "primary",
                    }
                ]
            }
        },
        "agents": [
            {
                "id": "assistant",
                "role": "assistant",
                "prompt": prompt,
                "model": "main",
            }
        ],
    }


@pytest.mark.integration
async def test_real_foundation_api_workflow_and_tenant_authorization(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug="api-workflow", name="API Workflow"))

    tokens = AccessTokenService(b"x" * 32)
    auth = AuthService(
        auth_session_factory,
        tenant_id,
        PasswordService(),
        tokens,
    )
    configs = ConfigService(auth_session_factory)
    bootstrap_code = await auth.issue_bootstrap_code()
    app = create_app(
        auth_service=auth,
        config_service=configs,
        rate_limiter=AllowAllLimiter(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        setup = await client.post(
            "/api/v1/setup",
            json={
                "code": bootstrap_code,
                "username": "owner",
                "password": "correct horse battery staple",
            },
        )
        assert setup.status_code == 201
        token = setup.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        reused = await client.post(
            "/api/v1/setup",
            json={
                "code": bootstrap_code,
                "username": "other",
                "password": "correct horse battery staple",
            },
        )
        assert reused.status_code == 401
        assert bootstrap_code not in reused.text

        login = await client.post(
            "/api/v1/auth/login",
            json={
                "tenant_id": str(tenant_id),
                "username": "owner",
                "password": "correct horse battery staple",
            },
        )
        invalid_login = await client.post(
            "/api/v1/auth/login",
            json={
                "tenant_id": str(tenant_id),
                "username": "owner",
                "password": "this password is wrong",
            },
        )
        assert login.status_code == 200
        assert invalid_login.status_code == 401
        assert invalid_login.headers["www-authenticate"] == "Bearer"

        first = await client.post(
            "/api/v1/config/drafts", headers=headers, json=document("First.")
        )
        published_first = await client.post(
            f"/api/v1/config/drafts/{first.json()['id']}/publish", headers=headers
        )
        second = await client.post(
            "/api/v1/config/drafts", headers=headers, json=document("Second.")
        )
        published_second = await client.post(
            f"/api/v1/config/drafts/{second.json()['id']}/publish", headers=headers
        )
        current = await client.get("/api/v1/config/current", headers=headers)
        history = await client.get(
            "/api/v1/config/history?limit=1&offset=1", headers=headers
        )
        diff = await client.get(
            "/api/v1/config/diff?from_version=1&to_version=2", headers=headers
        )
        rollback = await client.post(
            "/api/v1/config/history/1/rollback", headers=headers
        )

        assert published_first.json()["status"] == "published"
        assert published_second.json()["version"] == 2
        assert current.json()["version"] == 2
        assert [item["version"] for item in history.json()["items"]] == [2]
        assert diff.json()["changed"][0]["path"] == "/agents"
        assert rollback.json()["version"] == 3

        operator_token = tokens.encode(uuid4(), tenant_id, Role.OPERATOR)
        operator_headers = {"Authorization": f"Bearer {operator_token}"}
        assert (
            await client.get("/api/v1/config/current", headers=operator_headers)
        ).status_code == 200
        assert (
            await client.post(
                "/api/v1/config/drafts",
                headers=operator_headers,
                json=document("Denied."),
            )
        ).status_code == 403

        real_draft = await client.post(
            "/api/v1/config/drafts", headers=headers, json=document("Publish denied.")
        )
        operator_publish = await client.post(
            f"/api/v1/config/drafts/{real_draft.json()['id']}/publish",
            headers=operator_headers,
        )
        assert operator_publish.status_code == 403
        admin_publish = await client.post(
            f"/api/v1/config/drafts/{real_draft.json()['id']}/publish",
            headers=headers,
        )
        assert admin_publish.status_code == 200

        other_tenant_token = tokens.encode(uuid4(), uuid4(), Role.ADMIN)
        other_headers = {"Authorization": f"Bearer {other_tenant_token}"}
        idor = await client.post(
            f"/api/v1/config/drafts/{first.json()['id']}/publish",
            headers=other_headers,
        )
        assert idor.status_code == 404
