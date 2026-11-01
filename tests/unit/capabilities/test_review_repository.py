"""Private review storage contracts; no database or provider calls."""

import hashlib
import json
from collections.abc import Coroutine, Generator, Sequence
from decimal import Decimal
from typing import Unpack
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select, event
from sqlalchemy.engine import Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.util import await_only

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities import review_repository
from agent_hub.capabilities.review_contracts import (
    ApprovalReviewConfig,
    FrozenReviewAction,
    ModelReviewVerdict,
    freeze_review_action,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.base import Base
from agent_hub.models.run_admission_repository import DeploymentIdentity, _WorkerLease
from agent_hub.runs.authorization import _AuthenticatedInput, _capture_authenticated_input
from tests.unit.capabilities.review_repository_api_typed_fixture import (
    ActionArguments,
    freeze_untrusted_usage,
    mapping,
    observation,
    present,
    reclassify,
    replaced_evidence,
    text,
    untrusted_reservation,
    untrusted_usage,
)


def evidence() -> _AuthenticatedInput:
    return _capture_authenticated_input(
        AuthenticatedPrincipal(tenant_id=UUID(int=1), user_id=UUID(int=2), role=Role.OPERATOR),
        "private user text",
        {"private": "scope"},
    )


def config() -> ApprovalReviewConfig:
    return ApprovalReviewConfig(
        engine="rules_then_model", reviewer_logical_model="reviewer", max_cost_usd_per_run="1.00"
    )


def action(**changes: Unpack[ActionArguments]) -> FrozenReviewAction:
    m = review_repository
    e = evidence()
    payload: ActionArguments = {
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


def test_authorization_hash_exact_schema_and_each_security_field() -> None:
    m = review_repository
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
        assert m.authorization_binding_sha256(replaced_evidence(e, key, value)) != digest
    for key in ["message_sha256", "scope_sha256", "_seal"]:
        with pytest.raises(ValueError, match="^invalid review binding$"):
            m.authorization_binding_sha256(replaced_evidence(e, key, object()))


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
def test_action_revalidation_rejects_forged_or_unbounded_snapshot(change: str) -> None:
    m = review_repository
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


def test_action_roundtrip_and_config_hash_revalidation() -> None:
    m = review_repository
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
def test_revalidates_manually_forged_verdict(changes: dict[str, object]) -> None:
    m = review_repository
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
def test_breaker_errors_never_reset_explicit_denial_streak(
    history: list[str], blocked: bool
) -> None:
    assert review_repository._breaker_open(history, config()) is blocked


def test_breaker_window_counts_only_conclusive_reviews() -> None:
    c = ApprovalReviewConfig(
        denial_breaker_consecutive=3, denial_breaker_denials=4, denial_breaker_window=6
    )
    assert review_repository._breaker_open(["deny", "deny", "allow", "deny", "deny", "allow"], c)
    assert review_repository._breaker_open(
        ["deny", "deny", "allow", "deny", "deny", "allow"] + ["timeout"] * 100, c
    )
    assert not review_repository._breaker_open(
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
def test_reservation_revalidates_private_identity_and_holds(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "request_sha256": "e" * 64,
        "deployment": DeploymentIdentity("synthetic", "review", "model"),
        "token_hold": 20,
        "cost_hold": Decimal("0.1"),
        "lease": _WorkerLease("worker", uuid4()),
    }
    values.update(changes)
    with pytest.raises(ValueError, match="^invalid review binding$"):
        untrusted_reservation(values)


def test_private_binding_repr_never_exports_action_or_lease() -> None:
    m = review_repository
    lease = _WorkerLease("worker", uuid4())
    r = m._ReviewReservation(
        "e" * 64, DeploymentIdentity("synthetic", "review", "model"), 20, Decimal("0.1"), lease
    )
    binding = m._CurrentReviewBinding(action(), config(), lease)
    text = repr(r) + repr(binding)
    assert str(lease.token) not in text
    for value in ["private-content", "private user text", "private-source", "private-effect"]:
        assert value not in text


def test_begin_requires_active_caller_transaction_and_never_opens_one() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    m = review_repository
    repo = m.ReviewRepository(async_sessionmaker())
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
    assert isinstance(operation, Coroutine)
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
def test_accounting_mapping_never_refunds_unproven_unknown(
    outcome: review_repository.ReviewOutcome,
) -> None:
    m = review_repository
    # Valid usage alone cannot prove a successful unknown model response.
    assert m._accounting_outcome(outcome) == (
        "not_issued"
        if outcome == "cancelled"
        else "completed"
        if outcome in ("allow", "deny", "needs_human")
        else "unknown"
    )


def test_private_begin_arguments_are_isolated_before_first_database_await() -> None:
    m = review_repository
    original = config()
    reservation = m._ReviewReservation(
        "e" * 64,
        DeploymentIdentity("synthetic", "review", "model"),
        20,
        Decimal("0.1"),
        _WorkerLease("worker", uuid4()),
    )
    c, r = m._freeze_begin_arguments(original, reservation)
    object.__setattr__(original, "denial_breaker_consecutive", 999)
    object.__setattr__(reservation, "request_sha256", "f" * 64)
    object.__setattr__(reservation.deployment, "model_id", "changed")
    object.__setattr__(reservation.lease, "token", uuid4())
    assert c.denial_breaker_consecutive == 3
    assert r.request_sha256 == "e" * 64
    assert r.deployment.model_id == "model"
    assert r.lease.token != reservation.lease.token


@pytest.mark.parametrize(
    "initial, expected_status, charged, closed",
    [
        ((2, 3, 5, Decimal("0.01")), "allow", 5, False),
        ((2, None, None, None), "unknown", 30, False),
        ((20, 20, 40, Decimal("0.01")), "unknown", 30, True),
        ((2, 3, 5, Decimal("NaN")), "unknown", 30, False),
    ],
)
def test_finish_usage_mutation_between_review_and_budget_receipts_is_inert(
    initial: tuple[int, int | None, int | None, Decimal | None],
    expected_status: str,
    charged: int,
    closed: bool,
) -> None:
    """DB boundary double; BOTH production repositories perform real settlement."""
    from agent_hub.db.models import (
        RunAdmissionAttemptRow,
        RunAdmissionPoolRow,
        RunReviewEffectRow,
        RunReviewIntentRow,
        RunRow,
    )
    from agent_hub.models.run_admission_repository import UsageObservation

    m = review_repository
    a = action()
    p, req = a.to_payload(), a.request()
    review, physical = uuid4(), uuid4()
    attempt_id = m._attempt_id(review)
    lease = _WorkerLease("worker", uuid4())
    deployment = DeploymentIdentity("synthetic", "review", "model")
    usage = UsageObservation(*initial)
    verdict = ModelReviewVerdict(
        1, "allow", "low", "authorized_action", "Private reasoning.", a.action_digest
    )
    row = RunReviewIntentRow(
        review_id=review,
        tenant_id=req.tenant_id,
        run_id=req.run_id,
        authorization_turn_id=UUID(text(p["authorization_turn_id"])),
        attempt_id=attempt_id,
        action_digest=a.action_digest,
        effect_digest=a.effect_digest,
        authorization_sha256=p["authorization_sha256"],
        config_sha256=p["config_sha256"],
        policy_sha256=p["policy_sha256"],
        source_sha256=hashlib.sha256(
            json.dumps(p["source_binding"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        request_sha256="e" * 64,
        private_action=a._payload_bytes.decode(),
        private_config=config().to_payload(),
        status="issued",
        physical_identity=physical,
        consumed=False,
    )
    attempt = RunAdmissionAttemptRow(
        attempt_id=attempt_id,
        tenant_id=req.tenant_id,
        run_id=req.run_id,
        purpose="review",
        state="issued",
        request_sha256="e" * 64,
        deployment={"provider_id": "synthetic", "deployment_id": "review", "model_id": "model"},
        worker_id=lease.worker_id,
        worker_lease_token=lease.token,
        token_hold=30,
        review_cost_hold=Decimal("0.2"),
        physical_transport_id=physical,
    )
    pool = RunAdmissionPoolRow(
        run_id=req.run_id,
        tenant_id=req.tenant_id,
        token_charged=30,
        review_token_charged=30,
        review_count_charged=1,
        review_cost_charged=Decimal("0.2"),
        closed=False,
    )
    effect = RunReviewEffectRow(
        tenant_id=req.tenant_id,
        run_id=req.run_id,
        review_id=review,
        authorization_turn_id=row.authorization_turn_id,
        effect_digest=a.effect_digest,
    )

    class Pause:
        def __await__(self) -> Generator[str, None, None]:
            yield "budget-settlement-await"

    class SessionBoundary(AsyncSession):
        def __init__(self) -> None:
            super().__init__()
            self.run_reads = 0
            self.events: list[Base] = []
            event.listen(self.sync_session, "do_orm_execute", self.execute_boundary, retval=True)

        def in_transaction(self) -> bool:
            return True

        def execute_boundary(self, state: ORMExecuteState) -> Result[tuple[Base]]:
            statement = state.statement
            assert isinstance(statement, Select)
            entity = statement.column_descriptions[0]["entity"]
            if entity is RunRow:
                self.run_reads += 1
                if self.run_reads == 2:
                    await_only(Pause())
                value: Base = RunRow(id=req.run_id, tenant_id=req.tenant_id)
            else:
                value = {
                    RunAdmissionPoolRow: pool,
                    RunReviewEffectRow: effect,
                    RunReviewIntentRow: row,
                    RunAdmissionAttemptRow: attempt,
                }[entity]
            return IteratorResult(SimpleResultMetaData(["value"]), iter([(value,)]))

        def add(self, instance: object, _warn: bool = True) -> None:
            assert isinstance(instance, Base)
            self.events.append(instance)

        async def flush(self, objects: Sequence[object] | None = None) -> None:
            pass

    session = SessionBoundary()
    repo = m.ReviewRepository(async_sessionmaker())
    operation = repo._finish_in_session(
        session,
        tenant_id=req.tenant_id,
        run_id=req.run_id,
        review_id=review,
        attempt_id=attempt_id,
        request_sha256="e" * 64,
        physical_identity=physical,
        lease=lease,
        actual_model_identity=deployment,
        outcome="allow",
        usage=usage,
        verdict=verdict,
    )
    assert isinstance(operation, Coroutine)
    try:
        assert operation.send(None) == "budget-settlement-await"
        object.__setattr__(usage, "prompt_tokens", 2)
        object.__setattr__(usage, "completion_tokens", 4)
        object.__setattr__(usage, "total_tokens", 6)
        object.__setattr__(usage, "cost_usd", Decimal("0.01"))
        with pytest.raises(StopIteration) as completed:
            operation.send(None)
        assert completed.value.value.status == expected_status
        review_receipt = json.loads(present(row.private_receipt))
        assert mapping(present(attempt.receipt))["usage"] == review_receipt["evidence"]["usage"]
        assert pool.token_charged == charged
        assert pool.closed is closed
        assert mapping(mapping(present(attempt.receipt))["usage"])["total_tokens"] == initial[2]
        repo._terminal_proof(row, attempt)
    finally:
        operation.close()


@pytest.mark.parametrize(
    "usage_values",
    [
        (True, 3, 4, Decimal("0.01")),
        ("bad", None, None, Decimal("NaN")),
        (2, 3, None, None),
        (2, 3, 5, Decimal("Infinity")),
        ([], {}, object(), None),
    ],
)
def test_finish_freezes_malformed_usage_without_zero_or_lost_observation(
    usage_values: tuple[object, object, object, object],
) -> None:
    from agent_hub.models.run_admission_repository import _observation

    m = review_repository
    usage = untrusted_usage(usage_values)
    expected = _observation(usage)
    frozen, _, _, _ = m._freeze_finish_arguments(
        usage,
        _WorkerLease("worker", uuid4()),
        DeploymentIdentity("synthetic", "review", "model"),
        None,
    )
    object.__setattr__(usage, "prompt_tokens", 0)
    object.__setattr__(usage, "completion_tokens", 0)
    object.__setattr__(usage, "total_tokens", 0)
    object.__setattr__(usage, "cost_usd", Decimal(0))
    assert _observation(frozen) == expected
    assert frozen is not usage


def test_finish_freezes_model_lease_and_verdict_without_retaining_aliases() -> None:
    from agent_hub.models.run_admission_repository import UsageObservation

    m = review_repository
    lease = _WorkerLease("worker", uuid4())
    token = lease.token
    identity = DeploymentIdentity("synthetic", "review", "model")
    verdict = ModelReviewVerdict(
        1, "allow", "low", "authorized_action", "Private reasoning.", "a" * 64
    )
    u, owned_lease, owned_model, owned_verdict = m._freeze_finish_arguments(
        UsageObservation(2, 3, 5, Decimal("0.01")), lease, identity, verdict
    )
    object.__setattr__(lease, "token", uuid4())
    object.__setattr__(identity, "model_id", "changed")
    object.__setattr__(verdict, "risk", "high")
    assert owned_lease.token == token
    assert owned_model.model_id == "model"
    assert owned_verdict is not None
    assert u is not None
    assert owned_verdict.risk == "low"
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens) == (2, 3, 5)


def test_finish_freezes_opaque_invalid_type_without_retaining_caller_object() -> None:
    from agent_hub.models.run_admission_repository import _observation

    m = review_repository

    class BadUsage:
        pass

    class OtherUsage:
        pass

    field = BadUsage()
    original = untrusted_usage((field, None, None, None))
    frozen, _, _, _ = m._freeze_finish_arguments(
        original,
        _WorkerLease("worker", uuid4()),
        DeploymentIdentity("synthetic", "review", "model"),
        None,
    )
    reclassify(field, OtherUsage)
    assert present(_observation(frozen))["prompt_tokens"] == {"invalid_type": "BadUsage"}
    invalid = BadUsage()
    opaque, _, _, _ = freeze_untrusted_usage(
        invalid,
        _WorkerLease("worker", uuid4()),
        DeploymentIdentity("synthetic", "review", "model"),
    )
    reclassify(invalid, OtherUsage)
    assert observation(opaque) == {"invalid_type": "BadUsage"}
