from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import (
    ApprovalService,
    InMemoryApprovalStore,
)
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.harness.tool_gateway import HarnessToolGateway
from tests.unit.harness.test_tool_gateway import (
    RUN_ID,
    TENANT_ID,
    USER_ID,
    FakeMcpToolBackend,
    FakePluginToolBackend,
    FakePolicyGateway,
    FakeRuntimeCapabilityGateway,
    builtin_request,
)
from tests.unit.harness.tool_authorize_fixture import AskRepository, StaticPolicyGateway

TOOLS = ["workspace.write_text", "project.verify", "workspace.bundle"]


@pytest.mark.parametrize(("name", "parts"), [
    ("workspace.write_text", ("file", "write", "workspace/current")),
    ("project.verify", ("command", "run", "workspace/current/project-verification")),
    ("workspace.bundle", ("file", "create", "workspace/bundle")),
])
async def test_authorize_returns_only_authority_without_executing_or_routing_external_backend(
    name: str, parts: tuple[str, str, str],
) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = StaticPolicyGateway()
    mcp, plugin = FakeMcpToolBackend(), FakePluginToolBackend()
    gateway = HarnessToolGateway(backend, policy_gateway=policy,
                                mcp_backend=mcp, plugin_backend=plugin)
    request = builtin_request(name, arguments={}, sandbox="workspace_write")

    result = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)

    assert result.status == "succeeded" and result.payload == {"authorized": True}
    assert result.call_id == request.call_id and result.tool_name == name
    assert result.failure_reason is None and result.truncated is False
    assert [call[0] for call in backend.calls] == ["available"]
    assert not mcp.calls and not plugin.calls
    assert len(policy.static_requests) == 1 and not policy.requests
    actual, role = policy.static_requests[0]
    assert (actual.capability, actual.operation, actual.resource) == parts
    assert (actual.tenant_id, actual.run_id, actual.user_id) == (TENANT_ID, RUN_ID, USER_ID)
    assert actual.agent_id == request.actor and actual.arguments == {}
    assert actual.idempotency_key == request.idempotency_key and role is Role.OPERATOR


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("status", [CapabilityStatus.WAITING_APPROVAL, CapabilityStatus.DENIED])
async def test_authorize_preserves_pending_or_denied_without_backend_execution(
    name: str, status: CapabilityStatus,
) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = StaticPolicyGateway(status, approval_id="approval_1")
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    request = builtin_request(name, arguments={}, sandbox="workspace_write")
    discovered = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert discovered.status == "succeeded" and discovered.payload == {"authorized": True}
    assert len(policy.static_requests) == 1 and not policy.requests
    assert [call[0] for call in backend.calls] == ["available"]
    result = await gateway.invoke(
        TENANT_ID, replace(request, idempotency_key="actual-"+name),
        user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed"
    assert result.failure_reason == (
        "capability requires approval" if status is CapabilityStatus.WAITING_APPROVAL
        else "capability denied"
    )
    assert result.payload == (
        {"approval_id": "approval_1"} if status is CapabilityStatus.WAITING_APPROVAL else {}
    )
    assert [call[0] for call in backend.calls] == ["available"] and len(policy.requests) == 1
    actual, role = policy.requests[0]
    assert actual.idempotency_key == "actual-"+name and role is Role.OPERATOR
    assert (actual.tenant_id, actual.run_id, actual.user_id) == (TENANT_ID, RUN_ID, USER_ID)


@pytest.mark.parametrize("identity", [{}, {"user_id": USER_ID}, {"role": Role.OPERATOR},
                                     {"user_id": str(USER_ID), "role": Role.OPERATOR}])
async def test_authorize_requires_actor_identity_even_for_optional_identity_gateway(
    identity: dict[str, Any],
) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    result = await gateway.authorize(
        TENANT_ID, builtin_request("project.verify", sandbox="workspace_write"), **identity,
    )
    assert result.status == "failed" and result.failure_reason == "capability identity unavailable"
    assert not backend.calls and not policy.requests


@pytest.mark.parametrize("name", ["calculator", "project.generate_zip", "workspace.prune",
                                  "workspace.read", "unknown.tool"])
async def test_authorize_rejects_every_tool_outside_three_static_builtins(name: str) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    result = await gateway.authorize(
        TENANT_ID, builtin_request(name, arguments={}, sandbox="workspace_write"),
        user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed" and result.failure_reason == "tool permission mapping unavailable"
    assert not backend.calls and not policy.requests


@pytest.mark.parametrize("name", ["workspace.write_text", "workspace.bundle"])
async def test_authorize_reuses_workspace_write_sandbox_guard(name: str) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    result = await gateway.authorize(
        TENANT_ID, builtin_request(name, sandbox="read_only"),
        user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed"
    assert result.failure_reason == "workspace write requires workspace_write sandbox"
    assert not backend.calls and not policy.requests


@pytest.mark.parametrize("available", [False, None, 1])
async def test_authorize_requires_confirmed_backend_availability(available: Any) -> None:
    backend = FakeRuntimeCapabilityGateway(available=available)
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    result = await gateway.authorize(
        TENANT_ID, builtin_request("project.verify", sandbox="workspace_write"),
        user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed" and result.failure_reason == "tool unavailable"
    assert [call[0] for call in backend.calls] == ["available"]


async def test_authorize_does_not_satisfy_later_invoke_policy_check() -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = StaticPolicyGateway()
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    request = builtin_request("workspace.bundle", arguments={}, sandbox="workspace_write")
    admitted = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert admitted.payload == {"authorized": True}
    policy.status = CapabilityStatus.DENIED
    result = await gateway.invoke(TENANT_ID, replace(request, idempotency_key="actual-bundle"),
                                  user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == "failed" and result.failure_reason == "capability denied"
    assert len(policy.static_requests) == 1 and len(policy.requests) == 1
    assert policy.requests[0][0].idempotency_key == "actual-bundle"
    policy.status, policy.approval_id = CapabilityStatus.WAITING_APPROVAL, "later_exact_approval"
    later = await gateway.invoke(TENANT_ID, replace(request, idempotency_key="actual-bundle-2"),
                                 user_id=USER_ID, role=Role.OPERATOR)
    assert later.status == "failed" and later.failure_reason == "capability requires approval"
    assert later.payload == {"approval_id": "later_exact_approval"}
    assert len(policy.static_requests) == 1 and len(policy.requests) == 2
    assert policy.requests[1][0].idempotency_key == "actual-bundle-2"
    assert [call[0] for call in backend.calls] == ["available"]


async def test_authorize_never_grants_authority_without_policy_gateway() -> None:
    backend = FakeRuntimeCapabilityGateway()
    gateway = HarnessToolGateway(backend)
    result = await gateway.authorize(
        TENANT_ID, builtin_request("project.verify", sandbox="workspace_write"),
        user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed" and result.failure_reason == "capability identity unavailable"
    assert not backend.calls


async def test_authorize_preserves_explicit_approval_requirement_without_policy() -> None:
    backend = FakeRuntimeCapabilityGateway()
    gateway = HarnessToolGateway(backend)
    request = replace(builtin_request("project.verify", sandbox="workspace_write"),
                      approval_required=True)
    result = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == "failed" and result.failure_reason == "approval required"
    assert not backend.calls


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("failure", ["availability", "policy"])
async def test_authorize_redacts_discovery_and_policy_exceptions(name: str, failure: str) -> None:
    class Backend(FakeRuntimeCapabilityGateway):
        def is_available(self, tenant_id: UUID, name: str) -> bool:
            if failure == "availability":
                raise RuntimeError("password=PRIVATE_SECRET")
            return super().is_available(tenant_id, name)

    class Policy(StaticPolicyGateway):
        async def authorize_static(self, request: CapabilityRequest, *, role: Role) -> CapabilityResult:
            if failure == "policy":
                raise RuntimeError("password=PRIVATE_SECRET")
            return await super().authorize_static(request, role=role)

    backend, policy = Backend(), Policy(CapabilityStatus.ALLOWED)
    result = await HarnessToolGateway(backend, policy_gateway=policy).authorize(
        TENANT_ID, builtin_request(name, sandbox="workspace_write"),
        user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed" and result.payload == {}
    assert result.failure_reason == (
        "tool unavailable" if failure == "availability" else "capability authorization unavailable"
    )
    assert "PRIVATE_SECRET" not in repr(result) and not backend.calls


@pytest.mark.parametrize("name", TOOLS)
async def test_authorize_uses_real_global_ask_policy_and_keeps_actor_and_role_checks(name: str) -> None:
    async def require_approval(tenant_id: UUID) -> bool:
        return True

    async def mode(tenant_id: UUID) -> str:
        return "ask"

    repository = AskRepository()
    store = InMemoryApprovalStore()
    policy = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(store), repository,
        require_approval_for_tools=require_approval, tool_approval_mode=mode,
    )
    backend = FakeRuntimeCapabilityGateway()
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    request = builtin_request(name, sandbox="workspace_write")
    authorized = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert authorized.status == "succeeded" and authorized.payload == {"authorized": True}
    assert not repository.waits and not repository.scopes and await store.list_pending() == ()
    assert [call[0] for call in backend.calls] == ["available"]
    for actor, role in [(uuid4(), Role.OPERATOR), (USER_ID, Role.VIEWER)]:
        denied = await gateway.authorize(TENANT_ID, request, user_id=actor, role=role)
        assert denied.status == "failed" and denied.failure_reason == "capability denied"
        invoked = await gateway.invoke(TENANT_ID, request, user_id=actor, role=role)
        assert invoked.status == "failed" and invoked.failure_reason == "capability denied"
    assert not repository.waits and await store.list_pending() == ()
    waiting = await gateway.invoke(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert waiting.status == "failed" and waiting.failure_reason == "capability requires approval"
    assert waiting.payload == {"approval_id": repository.waits[0]["approval_id"]}
    scope = repository.waits[0]["approval_scope"]
    assert isinstance(scope, str) and f"user={USER_ID}:run={RUN_ID}:" in scope
    assert repository.waits[0]["actor_id"] == USER_ID and not repository.scopes
    assert len(await store.list_pending()) == 1
    # Static discovery while pending neither consumes approval nor grants scope.
    again = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert again.status == "succeeded" and again.payload == {"authorized": True}
    assert len(repository.waits) == 1
    assert [call[0] for call in backend.calls] == ["available", "available"]


@pytest.mark.parametrize("name", TOOLS)
async def test_static_deny_precedes_discovery_and_never_invokes_policy(name: str) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = StaticPolicyGateway(static_status=CapabilityStatus.DENIED)
    result = await HarnessToolGateway(backend, policy_gateway=policy).authorize(
        TENANT_ID, builtin_request(name, sandbox="workspace_write"), user_id=USER_ID, role=Role.OPERATOR,
    )
    assert result.status == "failed" and result.failure_reason == "capability denied" and result.payload == {}
    assert len(policy.static_requests) == 1 and not policy.requests and not backend.calls


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("status", [CapabilityStatus.DENIED, CapabilityStatus.WAITING_APPROVAL])
async def test_legacy_policy_discovery_defers_consent_but_invoke_still_checks_exact_decision(
    name: str, status: CapabilityStatus,
) -> None:
    backend = FakeRuntimeCapabilityGateway()
    policy = FakePolicyGateway(status, approval_id="legacy_exact_approval")
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    request = builtin_request(name, sandbox="workspace_write")
    admission = await gateway.authorize(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert admission.status == "succeeded" and admission.payload == {"authorized": True}
    assert not policy.requests and [call[0] for call in backend.calls] == ["available"]
    result = await gateway.invoke(TENANT_ID, request, user_id=USER_ID, role=Role.OPERATOR)
    assert result.status == "failed"
    assert result.failure_reason == ("capability denied" if status is CapabilityStatus.DENIED else "capability requires approval")
    assert result.payload == ({} if status is CapabilityStatus.DENIED else {"approval_id": "legacy_exact_approval"})
    assert len(policy.requests) == 1 and policy.requests[0][0].idempotency_key == request.idempotency_key
    assert [call[0] for call in backend.calls] == ["available"]
