from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest

from agent_hub.auth.feishu_oauth import (
    FeishuOAuthProfile,
    FeishuOAuthService,
    InMemoryOAuthStateStore,
    InMemoryUserAdminService,
    LastSuperAdminError,
    ManagedUser,
    OAuthBindingError,
    OAuthStateError,
)
from agent_hub.auth.models import AuthenticatedPrincipal, Role

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OWNER_ID = UUID("11111111-1111-4111-8111-111111111111")
ADMIN_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(slots=True)
class FakeOAuthClient:
    tenant_key: str = "tenant_key_1"
    open_id: str = "ou_owner"

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> FeishuOAuthProfile:
        del code, redirect_uri, code_verifier
        return FeishuOAuthProfile(open_id=self.open_id, tenant_key=self.tenant_key)


def oauth_service(client: FakeOAuthClient | None = None) -> FeishuOAuthService:
    return FeishuOAuthService(
        store=InMemoryOAuthStateStore(),
        client=client or FakeOAuthClient(),
        allowed_tenant_keys=frozenset({"tenant_key_1"}),
    )


def owner() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=OWNER_ID,
        tenant_id=TENANT_ID,
        role=Role.SUPER_ADMIN,
    )


async def test_oauth_rejects_replayed_state() -> None:
    service = oauth_service()
    state = await service.issue_state(
        tenant_key="tenant_key_1",
        redirect_uri="https://console.example.com/oauth/callback",
        code_challenge="challenge",
    )

    await service.consume_state(
        state,
        code="first",
        redirect_uri="https://console.example.com/oauth/callback",
        code_verifier="verifier",
    )
    with pytest.raises(OAuthStateError):
        await service.consume_state(
            state,
            code="second",
            redirect_uri="https://console.example.com/oauth/callback",
            code_verifier="verifier",
        )


async def test_oauth_rejects_wrong_redirect_and_tenant() -> None:
    service = oauth_service()
    state = await service.issue_state(
        tenant_key="tenant_key_1",
        redirect_uri="https://console.example.com/oauth/callback",
        code_challenge="challenge",
    )

    with pytest.raises(OAuthStateError, match="state"):
        await service.consume_state(
            state,
            code="code",
            redirect_uri="https://evil.example.com/oauth/callback",
            code_verifier="verifier",
        )

    service = oauth_service(FakeOAuthClient(tenant_key="other_tenant"))
    state = await service.issue_state(
        tenant_key="tenant_key_1",
        redirect_uri="https://console.example.com/oauth/callback",
        code_challenge="challenge",
    )
    with pytest.raises(OAuthStateError, match="tenant"):
        await service.consume_state(
            state,
            code="code",
            redirect_uri="https://console.example.com/oauth/callback",
            code_verifier="verifier",
        )


async def test_cannot_demote_last_super_admin() -> None:
    service = InMemoryUserAdminService(
        (
            ManagedUser(id=OWNER_ID, username="owner", role=Role.SUPER_ADMIN),
        )
    )

    with pytest.raises(LastSuperAdminError):
        await service.change_role(owner(), OWNER_ID, Role.ADMIN)


async def test_admin_can_bind_unique_feishu_open_id() -> None:
    service = InMemoryUserAdminService(
        (
            ManagedUser(id=OWNER_ID, username="owner", role=Role.SUPER_ADMIN),
            ManagedUser(id=ADMIN_ID, username="admin", role=Role.ADMIN),
        )
    )

    bound = await service.bind_feishu_open_id(
        owner(),
        ADMIN_ID,
        FeishuOAuthProfile(open_id="ou_admin", tenant_key="tenant_key_1"),
    )

    assert bound.feishu_open_id == "ou_admin"
    with pytest.raises(OAuthBindingError):
        await service.bind_feishu_open_id(
            owner(),
            OWNER_ID,
            FeishuOAuthProfile(open_id="ou_admin", tenant_key="tenant_key_1"),
        )
