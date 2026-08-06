from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect


@dataclass(frozen=True, slots=True)
class CapabilityRule:
    tenant_id: UUID
    role: Role | None
    agent_id: str | None
    capability: str
    operation: str
    resource_prefix: str
    effect: PolicyEffect

    def __post_init__(self) -> None:
        normalized = normalize_resource(self.resource_prefix)
        if normalized is None:
            raise ValueError("resource prefix is invalid")
        object.__setattr__(self, "resource_prefix", normalized)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    normalized_resource: str
    reason: str


class CapabilityPolicy:
    def __init__(self, rules: tuple[CapabilityRule, ...]) -> None:
        self._rules = rules

    def evaluate(self, request: CapabilityRequest, role: Role) -> PolicyDecision:
        normalized_resource = normalize_resource(request.resource)
        if normalized_resource is None:
            return PolicyDecision(PolicyEffect.DENY, "", "resource denied")
        effects = [
            rule.effect
            for rule in self._rules
            if _matches(rule, request, role, normalized_resource)
        ]
        if PolicyEffect.DENY in effects:
            effect = PolicyEffect.DENY
        elif PolicyEffect.REQUIRE_APPROVAL in effects:
            effect = PolicyEffect.REQUIRE_APPROVAL
        elif PolicyEffect.ALLOW in effects:
            effect = PolicyEffect.ALLOW
        else:
            effect = PolicyEffect.DENY
        reason = {
            PolicyEffect.ALLOW: "capability allowed",
            PolicyEffect.REQUIRE_APPROVAL: "capability requires approval",
            PolicyEffect.DENY: "capability denied",
        }[effect]
        return PolicyDecision(effect, normalized_resource, reason)


def normalize_resource(resource: str) -> str | None:
    if resource.startswith(("/", "\\")) or "://" in resource:
        return None
    candidate = resource.replace("\\", "/")
    if len(candidate) >= 2 and candidate[1] == ":":
        return None
    parts: list[str] = []
    for raw_part in candidate.split("/"):
        if raw_part in {"", "."}:
            continue
        if raw_part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(raw_part)
    if not parts:
        return None
    normalized = "/".join(parts)
    if normalized.startswith("/") or "\x00" in normalized:
        return None
    return normalized


def _matches(
    rule: CapabilityRule,
    request: CapabilityRequest,
    role: Role,
    normalized_resource: str,
) -> bool:
    return (
        rule.tenant_id == request.tenant_id
        and (rule.role is None or rule.role is role)
        and (rule.agent_id is None or rule.agent_id == request.agent_id)
        and rule.capability == request.capability
        and rule.operation == request.operation
        and (
            normalized_resource == rule.resource_prefix
            or normalized_resource.startswith(f"{rule.resource_prefix}/")
        )
    )
