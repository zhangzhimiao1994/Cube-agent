from __future__ import annotations

from math import nan
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import default_capability_policy
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RUN_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def request(
    capability_operation: str,
    *,
    resource: str,
    tenant_id: UUID = TENANT_A,
    agent_id: str = "researcher",
    arguments: dict[str, object] | None = None,
) -> CapabilityRequest:
    capability, operation = capability_operation.split(".", 1)
    return CapabilityRequest(
        tenant_id=tenant_id,
        user_id=USER_ID,
        agent_id=agent_id,
        capability=capability,
        operation=operation,
        resource=resource,
        arguments={} if arguments is None else arguments,
        idempotency_key=f"{capability_operation}:{resource}",
        run_id=RUN_ID,
    )


@pytest.fixture
def policy() -> CapabilityPolicy:
    return CapabilityPolicy(
        (
            CapabilityRule(
                tenant_id=TENANT_A,
                role=Role.OPERATOR,
                agent_id="researcher",
                capability="file",
                operation="read",
                resource_prefix="workspace/docs",
                effect=PolicyEffect.ALLOW,
            ),
            CapabilityRule(
                tenant_id=TENANT_A,
                role=Role.OPERATOR,
                agent_id="researcher",
                capability="message",
                operation="send",
                resource_prefix="external",
                effect=PolicyEffect.REQUIRE_APPROVAL,
            ),
            CapabilityRule(
                tenant_id=TENANT_A,
                role=None,
                agent_id=None,
                capability="shell",
                operation="exec",
                resource_prefix="host",
                effect=PolicyEffect.DENY,
            ),
        )
    )


def test_read_only_workspace_file_is_safe(policy: CapabilityPolicy) -> None:
    decision = policy.evaluate(request("file.read", resource="workspace/docs/a.md"), Role.OPERATOR)

    assert decision.effect is PolicyEffect.ALLOW
    assert decision.normalized_resource == "workspace/docs/a.md"


def test_host_shell_is_always_denied(policy: CapabilityPolicy) -> None:
    decision = policy.evaluate(request("shell.exec", resource="host"), Role.SUPER_ADMIN)

    assert decision.effect is PolicyEffect.DENY


def test_restricted_external_message_requires_approval(policy: CapabilityPolicy) -> None:
    decision = policy.evaluate(request("message.send", resource="external/team"), Role.OPERATOR)

    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL


def test_deny_wins_and_approval_wins_over_allow() -> None:
    scoped_request = request("message.send", resource="external/team")
    policy = CapabilityPolicy(
        (
            CapabilityRule(
                tenant_id=TENANT_A,
                role=Role.OPERATOR,
                agent_id="researcher",
                capability="message",
                operation="send",
                resource_prefix="external",
                effect=PolicyEffect.ALLOW,
            ),
            CapabilityRule(
                tenant_id=TENANT_A,
                role=Role.OPERATOR,
                agent_id="researcher",
                capability="message",
                operation="send",
                resource_prefix="external/team",
                effect=PolicyEffect.REQUIRE_APPROVAL,
            ),
            CapabilityRule(
                tenant_id=TENANT_A,
                role=Role.OPERATOR,
                agent_id="researcher",
                capability="message",
                operation="send",
                resource_prefix="external/team/private",
                effect=PolicyEffect.DENY,
            ),
        )
    )

    assert policy.evaluate(scoped_request, Role.OPERATOR).effect is PolicyEffect.REQUIRE_APPROVAL
    assert (
        policy.evaluate(request("message.send", resource="external/team/private"), Role.OPERATOR)
        .effect
        is PolicyEffect.DENY
    )


def test_absence_of_matching_rule_denies_by_default(policy: CapabilityPolicy) -> None:
    decision = policy.evaluate(request("file.write", resource="workspace/docs/a.md"), Role.OPERATOR)

    assert decision.effect is PolicyEffect.DENY


def test_path_normalization_prevents_traversal_and_prefix_tricks(
    policy: CapabilityPolicy,
) -> None:
    traversal = policy.evaluate(
        request("file.read", resource="workspace/docs/../../secrets.env"), Role.OPERATOR
    )
    sibling_prefix = policy.evaluate(
        request("file.read", resource="workspace/docs_evil/a.md"), Role.OPERATOR
    )
    backslash = policy.evaluate(
        request("file.read", resource=r"workspace\docs\a.md"), Role.OPERATOR
    )

    assert traversal.effect is PolicyEffect.DENY
    assert sibling_prefix.effect is PolicyEffect.DENY
    assert backslash.effect is PolicyEffect.ALLOW
    assert backslash.normalized_resource == "workspace/docs/a.md"


@pytest.mark.parametrize(
    "resource",
    ["/workspace/docs/a.md", r"\workspace\docs\a.md", r"C:\workspace\docs\a.md"],
)
def test_absolute_resources_are_denied(policy: CapabilityPolicy, resource: str) -> None:
    decision = policy.evaluate(request("file.read", resource=resource), Role.OPERATOR)

    assert decision.effect is PolicyEffect.DENY


@pytest.mark.parametrize("arguments", [{"bad": object()}, {"bad": nan}])
def test_arguments_must_be_json_compatible(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="JSON|arguments"):
        request("file.read", resource="workspace/docs/a.md", arguments=arguments)


def test_tenant_and_rbac_role_are_isolated(policy: CapabilityPolicy) -> None:
    other_tenant = policy.evaluate(
        request("file.read", resource="workspace/docs/a.md", tenant_id=TENANT_B),
        Role.OPERATOR,
    )
    wrong_role = policy.evaluate(request("file.read", resource="workspace/docs/a.md"), Role.VIEWER)

    assert other_tenant.effect is PolicyEffect.DENY
    assert wrong_role.effect is PolicyEffect.DENY


def test_default_policy_requires_approval_for_generated_files_when_configured() -> None:
    policy = default_capability_policy(TENANT_A, require_approval_for_tools=True)

    decision = policy.evaluate(
        request("file.create", resource="generated/project.generate_zip"),
        Role.OPERATOR,
    )

    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL


def test_default_policy_keeps_low_risk_tools_allowed_when_approval_is_required() -> None:
    policy = default_capability_policy(TENANT_A, require_approval_for_tools=True)

    calculator = policy.evaluate(
        request("calculator.evaluate", resource="calculator"),
        Role.OPERATOR,
    )
    context = policy.evaluate(request("context.read", resource="context"), Role.OPERATOR)

    assert calculator.effect is PolicyEffect.ALLOW
    assert context.effect is PolicyEffect.ALLOW
