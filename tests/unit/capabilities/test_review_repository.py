"""Private review storage contracts; no database or provider calls."""

import hashlib
import importlib
import json
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_contracts import (
    ApprovalReviewConfig,
    FrozenReviewAction,
    ModelReviewVerdict,
    freeze_review_action,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.models.run_admission_repository import DeploymentIdentity, _WorkerLease
from agent_hub.runs.authorization import _capture_authenticated_input


def module():
    return importlib.import_module("agent_hub.capabilities.review_repository")


def evidence():
    return _capture_authenticated_input(
        AuthenticatedPrincipal(tenant_id=UUID(int=1), user_id=UUID(int=2), role=Role.OPERATOR),
        "private user text",
        {"private": "scope"},
    )


def config():
    return ApprovalReviewConfig(
        engine="rules_then_model", reviewer_logical_model="reviewer", max_cost_usd_per_run="1.00"
    )


def action(**changes):
    m = module()
    e = evidence()
    payload = {
        "role": e.actor_role,
        "authorization_turn_id": e.turn_id,
        "authorization_sha256": m.authorization_binding_sha256(e),
        "config_sha256": m.review_config_sha256(config()),
        "policy_sha256": "c" * 64,
        "source_binding": {"private": "source"},
        "normalized_effect": {"target": "private-effect"},
    }
    payload.update(changes)
    req = CapabilityRequest(
        tenant_id=e.tenant_id,
        user_id=e.actor_id,
        run_id=UUID(int=3),
        agent_id="a",
        capability="workspace",
        operation="write",
        resource="private-path",
        arguments={"data": "private-content"},
        idempotency_key="key",
    )
    return freeze_review_action(req, **payload)


def test_authorization_hash_exact_schema_and_each_security_field():
    m = module()
    e = evidence()
    expected = {
        "schema_version": 1,
        "tenant_id": str(e.tenant_id),
        "user_id": str(e.actor_id),
        "actor_role": "operator",
        "turn_id": str(e.turn_id),
        "message_sha256": e.message_sha256,
        "scope_sha256": e.scope_sha256,
    }
    raw = json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    digest = m.authorization_binding_sha256(e)
    assert digest == hashlib.sha256(raw).hexdigest()
    for key, value in [
        ("tenant_id", uuid4()),
        ("actor_id", uuid4()),
        ("actor_role", Role.ADMIN),
        ("turn_id", uuid4()),
    ]:
        assert m.authorization_binding_sha256(replace(e, **{key: value})) != digest
    for key in ["message_sha256", "scope_sha256", "_seal"]:
        with pytest.raises(ValueError, match="^invalid review binding$"):
            m.authorization_binding_sha256(replace(e, **{key: object()}))


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "effect",
        "schema",
        "extra",
        "role",
        "request",
        "source",
        "oversize",
        "nul",
        "noncanonical",
    ],
)
def test_action_revalidation_rejects_forged_or_unbounded_snapshot(change):
    m = module()
    original = action()
    payload = json.loads(original._payload_bytes)
    digest, effect = original.action_digest, original.effect_digest
    if change == "digest":
        digest = "0" * 64
    elif change == "effect":
        effect = "0" * 64
    else:
        if change == "schema":
            payload["schema_version"] = True
        if change == "extra":
            payload["extra"] = "private-value"
        if change == "role":
            payload["role"] = "root"
        if change == "request":
            payload["request"]["agent_id"] = "INVALID"
        if change == "source":
            payload["source_binding"] = []
        if change == "oversize":
            payload["source_binding"] = {"x": "x" * 65536}
        if change == "nul":
            payload["source_binding"] = {"x": "\x00"}
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if change == "noncanonical":
            raw += b" "
        digest = hashlib.sha256(raw).hexdigest()
        original = FrozenReviewAction(digest, effect, raw)
    forged = FrozenReviewAction(digest, effect, original._payload_bytes)
    with pytest.raises(ValueError, match="^invalid review binding$") as caught:
        m._validated_action(forged)
    assert "private-value" not in str(caught.value)


def test_action_roundtrip_and_config_hash_revalidation():
    m = module()
    frozen = action()
    assert m._validated_action(frozen) == frozen
    assert len(m.review_config_sha256(config())) == 64
    forged = config()
    object.__setattr__(forged, "timeout_seconds", True)
    with pytest.raises(ValueError, match="^invalid review binding$"):
        m.review_config_sha256(forged)


@pytest.mark.parametrize(
    "changes",
    [
        {"action_digest": "b" * 64},
        {"risk": "high"},
        {"risk": "unknown"},
        {"schema_version": True},
        {"rationale": "x" * 241},
        {"rationale": "token=private-value"},
        {"reason_code": "private-value"},
    ],
)
def test_revalidates_manually_forged_verdict(changes):
    m = module()
    a = action()
    v = ModelReviewVerdict(
        1, "allow", "low", "authorized_action", "Private reasoning.", a.action_digest
    )
    for key, value in changes.items():
        object.__setattr__(v, key, value)
    with pytest.raises(ValueError, match="^invalid review binding$"):
        m._validated_verdict(v, a.action_digest)


@pytest.mark.parametrize(
    "history,blocked",
    [
        ([], False),
        (["deny"], False),
        (["deny", "timeout", "unknown", "invalid_response", "deny", "unavailable", "deny"], True),
        (["deny", "deny", "allow", "deny"], False),
        (["deny", "needs_human", "deny", "cancelled", "deny"], True),
    ],
)
def test_breaker_errors_never_reset_explicit_denial_streak(history, blocked):
    assert module()._breaker_open(history, config()) is blocked


def test_breaker_window_counts_only_conclusive_reviews():
    c = ApprovalReviewConfig(
        denial_breaker_consecutive=3, denial_breaker_denials=4, denial_breaker_window=6
    )
    assert module()._breaker_open(["deny", "deny", "allow", "deny", "deny", "allow"], c)
    assert module()._breaker_open(
        ["deny", "deny", "allow", "deny", "deny", "allow"] + ["timeout"] * 100, c
    )
    assert not module()._breaker_open(
        ["deny", "deny", "allow", "deny", "deny", "allow", "allow"], c
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"request_sha256": "private-value"},
        {"deployment": object()},
        {"token_hold": True},
        {"token_hold": 0},
        {"cost_hold": Decimal(-1)},
        {"lease": object()},
    ],
)
def test_reservation_revalidates_private_identity_and_holds(changes):
    values = {
        "request_sha256": "e" * 64,
        "deployment": DeploymentIdentity("synthetic", "review", "model"),
        "token_hold": 20,
        "cost_hold": Decimal("0.1"),
        "lease": _WorkerLease("worker", uuid4()),
    }
    values.update(changes)
    with pytest.raises(ValueError, match="^invalid review binding$"):
        module()._ReviewReservation(**values)


def test_private_binding_repr_never_exports_action_or_lease():
    m = module()
    lease = _WorkerLease("worker", uuid4())
    r = m._ReviewReservation(
        "e" * 64, DeploymentIdentity("synthetic", "review", "model"), 20, Decimal("0.1"), lease
    )
    binding = m._CurrentReviewBinding(action(), config(), lease)
    text = repr(r) + repr(binding)
    assert str(lease.token) not in text
    for value in ["private-content", "private user text", "private-source", "private-effect"]:
        assert value not in text


def test_begin_requires_active_caller_transaction_and_never_opens_one():
    from sqlalchemy.ext.asyncio import AsyncSession

    m = module()
    repo = m.ReviewRepository(None)
    session = AsyncSession()
    operation = repo._begin_intent_in_session(
        session,
        snapshot=action(),
        config=config(),
        reservation=m._ReviewReservation(
            "e" * 64,
            DeploymentIdentity("synthetic", "review", "model"),
            20,
            Decimal("0.1"),
            _WorkerLease("worker", uuid4()),
        ),
        review_id=uuid4(),
    )
    try:
        with pytest.raises(m.ReviewConflict, match="^review state conflict$"):
            operation.send(None)
        assert not session.in_transaction()
    finally:
        operation.close()


@pytest.mark.parametrize(
    "outcome",
    [
        "unknown",
        "timeout",
        "unavailable",
        "invalid_response",
        "cancelled",
        "allow",
        "deny",
        "needs_human",
    ],
)
def test_accounting_mapping_never_refunds_unproven_unknown(outcome):
    m = module()
    # Valid usage alone cannot prove a successful unknown model response.
    assert m._accounting_outcome(outcome) == (
        "not_issued"
        if outcome == "cancelled"
        else "completed"
        if outcome in ("allow", "deny", "needs_human")
        else "unknown"
    )
