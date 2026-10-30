from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect

ReviewDisposition = Literal["human_only", "model_eligible"]
PolicyRoute = Literal["deny", "allowed", "human_only", "model_required"]
POLICY_HASH_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CapabilityRule:
    """Server-built policy; review eligibility is never supplied by tool arguments."""

    tenant_id: UUID
    role: Role | None
    agent_id: str | None
    capability: str
    operation: str
    resource_prefix: str
    effect: PolicyEffect
    review_disposition: ReviewDisposition | None = None

    def __post_init__(self) -> None:
        if type(self.tenant_id) is not UUID or type(self.tenant_id.int) is not int:
            raise ValueError("rule tenant_id is invalid")
        if self.role is not None and type(self.role) is not Role:
            raise ValueError("rule role is invalid")
        if self.agent_id is not None and type(self.agent_id) is not str:
            raise ValueError("rule agent_id is invalid")
        if any(
            type(value) is not str
            for value in (
                self.capability,
                self.operation,
                self.resource_prefix,
            )
        ):
            raise ValueError("rule capability, operation or resource prefix is invalid")
        disposition = _normalize_review_disposition(self.effect, self.review_disposition)
        normalized = normalize_resource(self.resource_prefix)
        if normalized is None:
            raise ValueError("resource prefix is invalid")
        object.__setattr__(self, "tenant_id", UUID(int=self.tenant_id.int))
        object.__setattr__(self, "resource_prefix", normalized)
        object.__setattr__(self, "review_disposition", disposition)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    normalized_resource: str
    reason: str
    review_disposition: ReviewDisposition | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "review_disposition",
            _normalize_review_disposition(
                self.effect,
                self.review_disposition,
            ),
        )

    @property
    def route(self) -> PolicyRoute:
        """Describe the next gate only; model_required is still REQUIRE_APPROVAL."""
        if self.effect is PolicyEffect.DENY:
            return "deny"
        if self.effect is PolicyEffect.ALLOW:
            return "allowed"
        if self.review_disposition == "model_eligible":
            return "model_required"
        return "human_only"


class CapabilityPolicy:
    def __init__(self, rules: tuple[CapabilityRule, ...]) -> None:
        # Reconstruct even frozen dataclasses: callers can bypass frozen with
        # object.__setattr__. Matching and hashing must share our validated copy.
        snapshots: list[CapabilityRule] = []
        for rule in rules:
            if type(rule) is not CapabilityRule:
                raise ValueError("policy rule is invalid")
            prefix = rule.resource_prefix
            # Legacy reduction can expose drive-like text after its initial
            # drive check. Revalidate canonical outputs through a relative dot
            # prefix so copying them preserves their existing matching meaning.
            if type(prefix) is str and normalize_resource(f"./{prefix}") == prefix:
                prefix = f"./{prefix}"
            snapshots.append(
                CapabilityRule(
                    tenant_id=rule.tenant_id,
                    role=rule.role,
                    agent_id=rule.agent_id,
                    capability=rule.capability,
                    operation=rule.operation,
                    resource_prefix=prefix,
                    effect=rule.effect,
                    review_disposition=rule.review_disposition,
                )
            )
        self._rules = tuple(snapshots)

    def policy_sha256(self) -> str:
        """Hash all effective rules, independent of order and identical duplicates."""
        rows: dict[str, dict[str, object]] = {}
        for rule in self._rules:
            row: dict[str, object] = {
                "tenant_id": str(rule.tenant_id),
                "role": rule.role.value if rule.role is not None else None,
                "agent_id": rule.agent_id,
                "capability": rule.capability,
                "operation": rule.operation,
                "resource_prefix": rule.resource_prefix,
                "effect": rule.effect.value,
                "review_disposition": rule.review_disposition,
            }
            rows[_canonical_json(row)] = row
        payload = {
            "schema_version": POLICY_HASH_SCHEMA_VERSION,
            "rules": [rows[key] for key in sorted(rows)],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def evaluate(self, request: CapabilityRequest, role: Role) -> PolicyDecision:
        normalized_resource = normalize_resource(request.resource)
        if normalized_resource is None:
            return PolicyDecision(PolicyEffect.DENY, "", "resource denied")
        matching = [
            rule for rule in self._rules if _matches(rule, request, role, normalized_resource)
        ]
        effects = [rule.effect for rule in matching]
        disposition: ReviewDisposition | None = None
        if PolicyEffect.DENY in effects:
            effect = PolicyEffect.DENY
        elif PolicyEffect.REQUIRE_APPROVAL in effects:
            effect = PolicyEffect.REQUIRE_APPROVAL
            disposition = (
                "model_eligible"
                if all(
                    rule.review_disposition == "model_eligible"
                    for rule in matching
                    if rule.effect is PolicyEffect.REQUIRE_APPROVAL
                )
                else "human_only"
            )
        elif PolicyEffect.ALLOW in effects:
            effect = PolicyEffect.ALLOW
        else:
            effect = PolicyEffect.DENY
        reason = {
            PolicyEffect.ALLOW: "capability allowed",
            PolicyEffect.REQUIRE_APPROVAL: "capability requires approval",
            PolicyEffect.DENY: "capability denied",
        }[effect]
        return PolicyDecision(effect, normalized_resource, reason, disposition)


def _normalize_review_disposition(
    effect: PolicyEffect,
    disposition: ReviewDisposition | None,
) -> ReviewDisposition | None:
    if type(effect) is not PolicyEffect:
        raise ValueError("policy effect is invalid")
    if disposition is not None:
        if type(disposition) is not str or disposition not in {"human_only", "model_eligible"}:
            raise ValueError("review disposition is invalid")
        if effect is not PolicyEffect.REQUIRE_APPROVAL:
            raise ValueError("review disposition requires REQUIRE_APPROVAL")
    if effect is PolicyEffect.REQUIRE_APPROVAL:
        return "human_only" if disposition is None else disposition
    return None


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


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
