"""Replay/idempotency metadata must not waive runtime authorization."""
from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import default_capability_policy
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.runtime.defaults import RuntimeCapabilityGatewayProtocol, _capability_plan_item


class ReplaySafeOnly:
    def is_replay_safe(self, name: str) -> bool:
        return True


@pytest.mark.parametrize("name", (
    "workspace.write_text", "workspace.bundle", "workspace.prune",
    "project.generate_zip", "document.generate_docx", "presentation.generate_pptx",
    "project.preflight", "custom.replay_safe_writer",
))
def test_replay_safe_mutating_or_unknown_tool_has_runtime_approval_policy(name: str) -> None:
    hint = _capability_plan_item(
        name, capability_gateway=cast(RuntimeCapabilityGatewayProtocol, ReplaySafeOnly()),
    )
    assert hint == {"name": name, "replay_safe": True, "approval_policy": "runtime_policy"}


@pytest.mark.parametrize("name", (
    "read_context", "workspace_read", "workspace.read", "workspace.list",
    "calculator", "calculator_evaluate", "calculator.evaluate",
))
def test_read_and_calculation_names_are_not_authorization_evidence(name: str) -> None:
    hint = _capability_plan_item(
        name, capability_gateway=cast(RuntimeCapabilityGatewayProtocol, ReplaySafeOnly()),
    )
    assert hint == {"name": name, "replay_safe": True, "approval_policy": "runtime_policy"}


@pytest.mark.parametrize(("name", "capability", "operation", "resource"), (
    ("read_context", "context", "read", "context/current"),
    ("workspace_read", "file", "read", "workspace/current"),
    ("workspace.read", "file", "read", "workspace/current"),
    ("workspace.list", "file", "read", "workspace/current"),
    ("calculator", "calculator", "evaluate", "calculator"),
    ("calculator_evaluate", "calculator", "evaluate", "calculator"),
    ("calculator.evaluate", "calculator", "evaluate", "calculator"),
))
@pytest.mark.parametrize("effect", (PolicyEffect.ALLOW, PolicyEffect.REQUIRE_APPROVAL, PolicyEffect.DENY))
def test_custom_observation_policy_is_independent_of_replay_safety(
    name: str, capability: str, operation: str, resource: str, effect: PolicyEffect,
) -> None:
    uid = UUID("00000000-0000-4000-8000-000000000001")
    policy = default_capability_policy(uid, extra_rules=(
        CapabilityRule(uid, Role.OPERATOR, None, capability, operation, resource, effect),
    ))
    request = CapabilityRequest(
        tenant_id=uid, user_id=uid, run_id=uid, agent_id="synthetic",
        capability=capability, operation=operation, resource=resource,
        idempotency_key="synthetic-only",
    )

    class CustomPolicyGateway(ReplaySafeOnly):
        def approval_effect(self) -> PolicyEffect:
            return policy.evaluate(request, Role.OPERATOR).effect

        def execute(self) -> None:
            raise AssertionError("Hint generation must not execute a capability")

    gateway = CustomPolicyGateway()
    assert gateway.is_replay_safe(name) is True
    assert gateway.approval_effect() is effect
    before = policy.evaluate(request, Role.OPERATOR)
    hint = _capability_plan_item(
        name, capability_gateway=cast(RuntimeCapabilityGatewayProtocol, gateway),
    )
    assert hint == {"name": name, "replay_safe": True, "approval_policy": "runtime_policy"}
    assert policy.evaluate(request, Role.OPERATOR) == before


@pytest.mark.parametrize("name", ("workspace.write_text", "workspace.bundle"))
@pytest.mark.parametrize("effect", (PolicyEffect.ALLOW, PolicyEffect.REQUIRE_APPROVAL, PolicyEffect.DENY))
def test_known_policy_decisions_unchanged_by_hint_generation(name: str, effect: PolicyEffect) -> None:
    uid = UUID("00000000-0000-4000-8000-000000000001")
    operation = "write" if name == "workspace.write_text" else "create"
    resource = "workspace/current" if operation == "write" else "workspace/bundle"
    rules = (CapabilityRule(uid, Role.OPERATOR, None, "file", operation, resource, effect),)
    policy = default_capability_policy(uid, extra_rules=rules)
    request = CapabilityRequest(
        tenant_id=uid, user_id=uid, run_id=uid, agent_id="synthetic",
        capability="file", operation=operation, resource=resource, idempotency_key="synthetic-only",
    )
    before = policy.evaluate(request, Role.OPERATOR)
    assert before.effect is effect
    gateway = object.__new__(RuntimeCapabilityGateway)
    hint = _capability_plan_item(name, capability_gateway=gateway)
    assert hint["replay_safe"] is True and hint["approval_policy"] == "runtime_policy"
    assert policy.evaluate(request, Role.OPERATOR) == before


@pytest.mark.parametrize("name", ("workspace.write_text", "workspace.bundle", "read_context"))
def test_missing_or_non_replay_safe_gateway_stays_runtime_policy(name: str) -> None:
    class Unsafe:
        def is_replay_safe(self, name: str) -> bool:
            return False

    for gateway in (None, cast(RuntimeCapabilityGatewayProtocol, Unsafe())):
        assert _capability_plan_item(name, capability_gateway=gateway) == {
            "name": name, "replay_safe": False, "approval_policy": "runtime_policy",
        }


@pytest.mark.parametrize("answer", (1, "true"))
def test_truthy_replay_safe_value_is_not_an_approval_fact(answer: object) -> None:
    class Truthy:
        def is_replay_safe(self, name: str) -> object:
            return answer

    assert _capability_plan_item(
        "read_context", capability_gateway=cast(RuntimeCapabilityGatewayProtocol, Truthy()),
    ) == {"name": "read_context", "replay_safe": False, "approval_policy": "runtime_policy"}
