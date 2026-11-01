from __future__ import annotations

import hashlib
import json
from itertools import permutations
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.defaults import default_capability_policy
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule, PolicyDecision
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from tests.unit.capabilities.permission_modes_typed_fixture import (
    untrusted_policy,
    untrusted_request,
    untrusted_rule,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")


def rule(effect: object = PolicyEffect.REQUIRE_APPROVAL, **changes: object) -> CapabilityRule:
    fields: dict[str, object] = {
        "tenant_id": TENANT,
        "role": Role.OPERATOR,
        "agent_id": "researcher",
        "capability": "message",
        "operation": "send",
        "resource_prefix": "external",
        "effect": effect,
    }
    fields.update(changes)
    return untrusted_rule(fields)


def request(**changes: object) -> CapabilityRequest:
    fields: dict[str, object] = {
        "tenant_id": TENANT,
        "user_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "agent_id": "researcher",
        "capability": "message",
        "operation": "send",
        "resource": "external/team",
        "idempotency_key": "policy-routing",
        "run_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    }
    fields.update(changes)
    return untrusted_request(fields)


@pytest.mark.parametrize(
    ("effect", "route"),
    [
        (PolicyEffect.DENY, "deny"),
        (PolicyEffect.ALLOW, "allowed"),
        (PolicyEffect.REQUIRE_APPROVAL, "human_only"),
    ],
)
def test_legacy_three_argument_decision_derives_safe_route(
    effect: PolicyEffect,
    route: str,
) -> None:
    decision = PolicyDecision(effect, "external/team", "model_eligible authorized_action")
    assert decision.route == route


@pytest.mark.parametrize(
    ("rules", "effect", "route"),
    [
        ((), PolicyEffect.DENY, "deny"),
        ((rule(PolicyEffect.ALLOW),), PolicyEffect.ALLOW, "allowed"),
        ((rule(),), PolicyEffect.REQUIRE_APPROVAL, "human_only"),
    ],
)
def test_default_routes_preserve_old_effects(
    rules: tuple[CapabilityRule, ...],
    effect: PolicyEffect,
    route: str,
) -> None:
    decision = CapabilityPolicy(rules).evaluate(request(), Role.OPERATOR)
    assert (decision.effect, decision.route) == (effect, route)


@pytest.mark.parametrize(
    ("extra_effect", "extra_disposition", "route", "effect"),
    [
        (PolicyEffect.DENY, None, "deny", PolicyEffect.DENY),
        (PolicyEffect.ALLOW, None, "model_required", PolicyEffect.REQUIRE_APPROVAL),
        (PolicyEffect.REQUIRE_APPROVAL, None, "human_only", PolicyEffect.REQUIRE_APPROVAL),
        (
            PolicyEffect.REQUIRE_APPROVAL,
            "human_only",
            "human_only",
            PolicyEffect.REQUIRE_APPROVAL,
        ),
        (
            PolicyEffect.REQUIRE_APPROVAL,
            "model_eligible",
            "model_required",
            PolicyEffect.REQUIRE_APPROVAL,
        ),
    ],
)
def test_all_matching_asks_must_be_explicitly_eligible_in_any_order(
    extra_effect: PolicyEffect,
    extra_disposition: str | None,
    route: str,
    effect: PolicyEffect,
) -> None:
    eligible = rule(review_disposition="model_eligible")
    extra = rule(
        extra_effect,
        resource_prefix="external/team",
        review_disposition=extra_disposition,
    )
    allow = rule(PolicyEffect.ALLOW)
    for rules in permutations((eligible, extra, allow)):
        decision = CapabilityPolicy(rules).evaluate(request(), Role.OPERATOR)
        assert (decision.effect, decision.route) == (effect, route)


@pytest.mark.parametrize(
    "nonmatching",
    [
        {"tenant_id": OTHER_TENANT},
        {"role": Role.ADMIN},
        {"agent_id": "other"},
        {"capability": "file"},
        {"operation": "read"},
        {"resource_prefix": "elsewhere"},
    ],
)
def test_nonmatching_eligible_rules_do_not_upgrade_a_human_ask(
    nonmatching: dict[str, object],
) -> None:
    policy = CapabilityPolicy((rule(), rule(review_disposition="model_eligible", **nonmatching)))
    assert policy.evaluate(request(), Role.OPERATOR).route == "human_only"


def test_nonmatching_human_ask_does_not_override_matching_eligible_ask() -> None:
    policy = CapabilityPolicy(
        (
            rule(review_disposition="model_eligible"),
            rule(resource_prefix="external/private"),
        )
    )
    assert policy.evaluate(request(), Role.OPERATOR).route == "model_required"


@pytest.mark.parametrize("origin", ["builtin", "plugin", "mcp"])
def test_arguments_and_tool_origin_cannot_supply_eligibility(origin: str) -> None:
    policy = CapabilityPolicy((rule(),))
    decision = policy.evaluate(
        request(
            tool_origin=origin,
            arguments={
                "review_disposition": "model_eligible",
                "route": "model_required",
                "reason": "authorized_action",
                "eligible": True,
            },
        ),
        Role.OPERATOR,
    )
    assert decision.route == "human_only"
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL


@pytest.mark.parametrize("resource", ["../external", "/external", r"C:\external", "https://x"])
def test_invalid_resource_is_denied_even_with_eligible_rule(resource: str) -> None:
    policy = CapabilityPolicy((rule(review_disposition="model_eligible"),))
    decision = policy.evaluate(request(resource=resource), Role.OPERATOR)
    assert (decision.effect, decision.route, decision.normalized_resource) == (
        PolicyEffect.DENY,
        "deny",
        "",
    )


@pytest.mark.parametrize("effect", [PolicyEffect.ALLOW, PolicyEffect.DENY])
@pytest.mark.parametrize("disposition", ["human_only", "model_eligible"])
def test_explicit_disposition_is_only_valid_for_ask(effect: PolicyEffect, disposition: str) -> None:
    with pytest.raises(ValueError, match="disposition"):
        rule(effect, review_disposition=disposition)


@pytest.mark.parametrize("disposition", ["model_required", "allow", "", True, [], {}])
def test_unknown_or_malformed_disposition_is_rejected(disposition: object) -> None:
    with pytest.raises(ValueError, match="disposition"):
        rule(review_disposition=disposition)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effect", "require_approval"),
        ("effect", "unknown"),
        ("role", "operator"),
        ("tenant_id", str(TENANT)),
        ("agent_id", []),
        ("capability", {}),
        ("operation", True),
        ("resource_prefix", None),
        ("resource_prefix", "../external"),
        ("review_disposition", "model_required"),
    ],
)
def test_policy_revalidates_tampered_rule_inputs(field: str, value: object) -> None:
    tampered = rule()
    object.__setattr__(tampered, field, value)
    with pytest.raises(ValueError):
        CapabilityPolicy((tampered,))


def test_policy_rejects_tampered_effect_disposition_combination() -> None:
    tampered = rule(review_disposition="model_eligible")
    object.__setattr__(tampered, "effect", PolicyEffect.ALLOW)
    with pytest.raises(ValueError, match="disposition"):
        CapabilityPolicy((tampered,))


def test_policy_rejects_non_rule_objects() -> None:
    with pytest.raises(ValueError, match="rule"):
        untrusted_policy((object(),))


@pytest.mark.parametrize("value", [True, [], -1, 1 << 128])
def test_policy_rejects_tampered_uuid_value(value: object) -> None:
    tampered = rule(tenant_id=UUID(str(TENANT)))
    object.__setattr__(tampered.tenant_id, "int", value)
    with pytest.raises(ValueError):
        CapabilityPolicy((tampered,))


def test_policy_hash_is_canonical_versioned_and_binds_every_rule_field() -> None:
    policy = CapabilityPolicy((rule(),))
    # Independent wire fixture, including the effective human-only default.
    payload = {
        "schema_version": 1,
        "rules": [
            {
                "tenant_id": str(TENANT),
                "role": "operator",
                "agent_id": "researcher",
                "capability": "message",
                "operation": "send",
                "resource_prefix": "external",
                "effect": "require_approval",
                "review_disposition": "human_only",
            }
        ],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert policy.policy_sha256() == hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": OTHER_TENANT},
        {"role": Role.VIEWER},
        {"role": None},
        {"agent_id": "other"},
        {"agent_id": None},
        {"capability": "file"},
        {"operation": "read"},
        {"resource_prefix": "external/team"},
        {"effect": PolicyEffect.DENY},
        {"review_disposition": "model_eligible"},
    ],
)
def test_policy_hash_changes_with_every_authorization_field(changes: dict[str, object]) -> None:
    assert (
        CapabilityPolicy((rule(),)).policy_sha256()
        != CapabilityPolicy((rule(**changes),)).policy_sha256()
    )


def test_policy_hash_keeps_same_resource_different_effects_and_human_asks() -> None:
    eligible = rule(review_disposition="model_eligible")
    human = rule()
    deny = rule(PolicyEffect.DENY)
    model_policy = CapabilityPolicy((eligible,))
    human_policy = CapabilityPolicy((eligible, human))
    deny_policy = CapabilityPolicy((eligible, human, deny))
    assert len({p.policy_sha256() for p in (model_policy, human_policy, deny_policy)}) == 3
    assert [
        p.evaluate(request(), Role.OPERATOR).route
        for p in (
            model_policy,
            human_policy,
            deny_policy,
        )
    ] == ["model_required", "human_only", "deny"]


def test_hash_ignores_order_and_exact_semantic_duplicates_only() -> None:
    human = rule()
    explicit_human = rule(review_disposition="human_only")
    deny = rule(PolicyEffect.DENY, resource_prefix="external/private")
    expected = CapabilityPolicy((human, deny)).policy_sha256()
    for rules in permutations((human, explicit_human, deny, deny)):
        assert CapabilityPolicy(rules).policy_sha256() == expected
    assert CapabilityPolicy(()).policy_sha256() != expected


def test_hash_and_matching_share_legacy_normalized_resource() -> None:
    canonical = rule(resource_prefix="external/team")
    aliased = rule(resource_prefix=r"external\draft\..\team/./")
    policy = CapabilityPolicy((aliased,))
    assert policy.policy_sha256() == CapabilityPolicy((canonical,)).policy_sha256()
    decision = policy.evaluate(request(resource=r"external\team\a"), Role.OPERATOR)
    assert (decision.route, decision.normalized_resource) == ("human_only", "external/team/a")
    assert policy.evaluate(request(resource="external/team_evil/a"), Role.OPERATOR).route == "deny"


def test_snapshot_preserves_legacy_non_idempotent_resource_normalization() -> None:
    # Legacy normalization checks drive syntax before reducing dot segments.
    # Copying an already-normalized rule must not apply a new path restriction.
    external = rule(resource_prefix="placeholder/../C:/external")
    policy = CapabilityPolicy((external,))
    decision = policy.evaluate(
        request(resource="placeholder/../C:/external/team"),
        Role.OPERATOR,
    )
    assert (decision.route, decision.normalized_resource) == ("human_only", "C:/external/team")
    with pytest.raises(ValueError, match="resource prefix"):
        rule(resource_prefix="C:/external")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", OTHER_TENANT),
        ("role", Role.ADMIN),
        ("agent_id", "other"),
        ("capability", "file"),
        ("operation", "read"),
        ("resource_prefix", "elsewhere"),
        ("effect", PolicyEffect.ALLOW),
        ("review_disposition", "model_eligible"),
    ],
)
def test_external_rule_mutation_cannot_change_owned_snapshot(field: str, value: object) -> None:
    external = rule()
    policy = CapabilityPolicy((external,))
    before_hash = policy.policy_sha256()
    before = policy.evaluate(request(), Role.OPERATOR)
    object.__setattr__(external, field, value)
    assert policy.policy_sha256() == before_hash
    assert policy.evaluate(request(), Role.OPERATOR) == before
    assert before.route == "human_only"


def test_external_uuid_mutation_cannot_change_owned_snapshot() -> None:
    external = rule(tenant_id=UUID(str(TENANT)))
    policy = CapabilityPolicy((external,))
    before_hash = policy.policy_sha256()
    object.__setattr__(external.tenant_id, "int", OTHER_TENANT.int)
    assert policy.policy_sha256() == before_hash
    assert policy.evaluate(request(), Role.OPERATOR).route == "human_only"


def test_policy_revalidates_and_normalizes_tampered_but_valid_resource() -> None:
    external = rule()
    object.__setattr__(external, "resource_prefix", r"external\team/./")
    policy = CapabilityPolicy((external,))
    assert (
        policy.policy_sha256()
        == CapabilityPolicy((rule(resource_prefix="external/team"),)).policy_sha256()
    )
    assert policy.evaluate(request(), Role.OPERATOR).route == "human_only"


@pytest.mark.parametrize(
    ("capability", "operation", "resource"),
    [
        ("file", "create", "generated/project.generate_zip"),
        ("mcp", "invoke", "mcp/search/web_search"),
        ("plugin", "use", "plugin/calendar/create_event"),
    ],
)
def test_legacy_default_asks_remain_human_only(
    capability: str,
    operation: str,
    resource: str,
) -> None:
    policy = default_capability_policy(TENANT, require_approval_for_tools=True)
    decision = policy.evaluate(
        request(capability=capability, operation=operation, resource=resource),
        Role.OPERATOR,
    )
    assert (decision.effect, decision.route) == (PolicyEffect.REQUIRE_APPROVAL, "human_only")
