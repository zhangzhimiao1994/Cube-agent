from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterator, Mapping
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse
from agent_hub.routing.classifier import GatewayRouteClassifier, RouteClassificationError
from agent_hub.routing.service import ModeRouter, RoutingPolicy
from agent_hub.routing.types import (
    ConfirmationSubject,
    InMemoryDecisionTokenStore,
    RiskLevel,
    RouteAssessment,
    RouteDecision,
    RouteSource,
)


def assessment(
    mode: TaskMode = TaskMode.HYBRID,
    *,
    confidence: float = 0.9,
    cost: str = "0.10",
    risk: RiskLevel = RiskLevel.LOW,
    source: RouteSource = RouteSource.CLASSIFIER,
) -> RouteAssessment:
    return RouteAssessment(
        mode=mode,
        confidence=confidence,
        reason="bounded recommendation",
        roles=("researcher",),
        estimated_seconds=120,
        estimated_cost_usd=Decimal(cost),
        risk=risk,
        source=source,
        logical_model="router",
        deployment_id="router-a",
        provider_id="openai",
    )


class FakeClassifier:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []
        self.cancelled = False

    async def classify(self, task_text: str) -> object:
        self.calls.append(task_text)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class BlockingClassifier(FakeClassifier):
    def __init__(self) -> None:
        super().__init__(assessment())
        self.started = asyncio.Event()

    async def classify(self, task_text: str) -> RouteAssessment:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


def router(
    primary: FakeClassifier | None = None,
    verifier: FakeClassifier | None = None,
    *,
    policy: RoutingPolicy | None = None,
) -> ModeRouter:
    return ModeRouter(
        primary or FakeClassifier(assessment()),
        verifier
        or FakeClassifier(assessment(source=RouteSource.VERIFIER)),
        token_store=InMemoryDecisionTokenStore(),
        policy=policy,
    )


def subject(*, task_id: str = "task-1", user_id: str = "user-1") -> ConfirmationSubject:
    return ConfirmationSubject(
        tenant_id="tenant-1",
        user_id=user_id,
        task_id=task_id,
        generation="generation-1",
    )


@pytest.mark.parametrize("mode", ["direct", "dispatch", "discuss", "hybrid"])
async def test_exact_explicit_command_has_absolute_precedence(mode: str) -> None:
    classifier = FakeClassifier(RuntimeError("must not run"))
    result = await router(classifier).route(f"  /{mode} do the task")
    assert result.mode is TaskMode(mode)
    assert result.status == "ready"
    assert result.needs_user_choice is False
    assert classifier.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "/directly do it",
        "/dіrect do it",
        "/dir\u200bect do it",
        "> /direct do it",
        "```\n/direct do it\n```",
        "/direct --unsafe do it",
    ],
)
async def test_command_lookalikes_and_embedded_commands_are_not_explicit(text: str) -> None:
    classifier = FakeClassifier(assessment(TaskMode.HYBRID))
    result = await router(classifier).route(text)
    assert result.mode is not TaskMode.DIRECT


async def test_auto_restarts_normal_routing_on_bounded_body() -> None:
    primary = FakeClassifier(assessment(TaskMode.HYBRID))
    verifier = FakeClassifier(
        assessment(TaskMode.HYBRID, source=RouteSource.VERIFIER)
    )
    result = await router(primary, verifier).route("/auto ambiguous architecture request")
    assert result.mode is TaskMode.HYBRID
    assert primary.calls == ["ambiguous architecture request"]


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("What is a mutex?", TaskMode.DIRECT),
        ("Run the fixed workflow release-check", TaskMode.DISPATCH),
        ("Have multiple agents debate this proposal", TaskMode.DISCUSS),
    ],
)
async def test_unambiguous_rules_do_not_call_models(text: str, mode: TaskMode) -> None:
    primary = FakeClassifier(RuntimeError("must not run"))
    result = await router(primary).route(text)
    assert result.mode is mode
    assert primary.calls == []


async def test_conflicting_or_high_risk_rules_ask_user() -> None:
    conflict = await router().route(
        "Run the fixed workflow and have multiple agents debate it"
    )
    risky = await router().route("Delete the production database")
    assert conflict.status == "waiting_user_mode"
    assert risky.status == "waiting_user_mode"
    assert risky.requires_approval is True


async def test_classifier_agreement_at_threshold_is_ready() -> None:
    primary = FakeClassifier(assessment(confidence=0.85))
    verifier = FakeClassifier(assessment(confidence=0.85, source=RouteSource.VERIFIER))
    result = await router(primary, verifier).route("ambiguous architecture request")
    assert result.mode is TaskMode.HYBRID
    assert len(result.assessments) == 2


@pytest.mark.parametrize(
    "primary,verifier",
    [
        (assessment(TaskMode.DISPATCH), assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        (assessment(confidence=0.8499), assessment(source=RouteSource.VERIFIER)),
        (assessment(risk=RiskLevel.HIGH), assessment(source=RouteSource.VERIFIER)),
        (assessment(cost="0.60"), assessment(cost="0.60", source=RouteSource.VERIFIER)),
    ],
)
async def test_unsafe_or_inconsistent_assessments_ask_user(
    primary: RouteAssessment, verifier: RouteAssessment
) -> None:
    result = await router(FakeClassifier(primary), FakeClassifier(verifier)).route(
        "ambiguous architecture request"
    )
    assert result.status == "waiting_user_mode"
    assert result.mode is None


async def test_classifier_failure_and_timeout_are_redacted_and_cancel_sibling() -> None:
    blocker = BlockingClassifier()
    failing = FakeClassifier(RuntimeError("secret raw response"))
    result = await router(
        failing,
        blocker,
        policy=RoutingPolicy(classifier_timeout_seconds=0.05),
    ).route("ambiguous architecture request")
    assert result.status == "waiting_user_mode"
    assert blocker.cancelled is True
    assert "secret" not in repr(result)


async def test_caller_cancellation_propagates_and_cleans_both_children() -> None:
    first = BlockingClassifier()
    second = BlockingClassifier()
    task = asyncio.create_task(router(first, second).route("ambiguous request"))
    await asyncio.gather(first.started.wait(), second.started.wait())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.cancelled and second.cancelled


async def test_classifier_cancellation_propagates_and_cleans_sibling() -> None:
    blocker = BlockingClassifier()
    cancelled = FakeClassifier(asyncio.CancelledError("cancel-token"))
    with pytest.raises(asyncio.CancelledError, match="cancel-token"):
        await router(cancelled, blocker).route("ambiguous request")
    assert blocker.cancelled is True


async def test_classifier_source_is_locally_overwritten() -> None:
    primary = FakeClassifier(assessment(source=RouteSource.VERIFIER))
    verifier = FakeClassifier(assessment(source=RouteSource.CLASSIFIER))
    result = await router(primary, verifier).route("ambiguous request")
    assert result.status == "ready"
    assert tuple(item.source for item in result.assessments) == (
        RouteSource.CLASSIFIER,
        RouteSource.VERIFIER,
    )


async def test_router_revalidates_instances_and_rejects_mapping_contracts() -> None:
    invalid = assessment().model_copy(update={"confidence": float("nan")})
    rejected = await router(
        FakeClassifier(invalid),
        FakeClassifier(assessment(source=RouteSource.VERIFIER)),
    ).route("ambiguous request")
    assert rejected.status == "waiting_user_mode"

    mapping = assessment().model_dump(round_trip=True)

    class MappingClassifier:
        async def classify(self, task_text: str) -> object:
            del task_text
            return mapping

    mapping_rejected = await ModeRouter(
        MappingClassifier(),
        FakeClassifier(assessment(source=RouteSource.VERIFIER)),
        token_store=InMemoryDecisionTokenStore(),
    ).route("ambiguous request")
    assert mapping_rejected.status == "waiting_user_mode"


async def test_hostile_mapping_from_classifier_fails_closed() -> None:
    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            del key
            raise RuntimeError("sk-hostile")

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("sk-hostile")

        def __len__(self) -> int:
            raise RuntimeError("sk-hostile")

    result = await router(
        FakeClassifier(HostileMapping()),
        FakeClassifier(assessment(source=RouteSource.VERIFIER)),
    ).route("ambiguous request")
    assert result.status == "waiting_user_mode"
    assert "sk-hostile" not in repr(result)


async def test_explicit_high_risk_mode_is_preserved_but_requires_approval() -> None:
    result = await router().route("/dispatch delete the production database")
    assert result.mode is TaskMode.DISPATCH
    assert result.requires_approval is True
    assert result.risk is RiskLevel.HIGH


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("/dispatch 删除生产数据库", TaskMode.DISPATCH),
        ("/hybrid 转账一万元", TaskMode.HYBRID),
        ("/dispatch ＤＲＯＰ-PRODUCTION_DB", TaskMode.DISPATCH),
        ("/direct revoke_api-key", TaskMode.DIRECT),
    ],
)
async def test_multilingual_explicit_risk_preserves_mode_and_approval(
    text: str, mode: TaskMode
) -> None:
    result = await router().route(text)
    assert result.mode is mode
    assert result.risk is RiskLevel.HIGH
    assert result.requires_approval is True


@pytest.mark.parametrize(
    "text",
    [
        "删除生产数据库",
        "truncate-prod_data",
        "refund the customer payment",
        "重置 API 密钥",
        "grant-admin permission",
        "发布到外部平台",
        "deploy_to_production",
        "执行不可逆操作",
        "shutdown production server",
        "disable security controls",
        "send money to the supplier",
        "rm -rf /",
        "format production disk",
        "delete all customer records",
        "关闭生产服务器",
        "禁用安全控制",
        "给供应商打款",
        "删除全部客户记录",
        "Explain JSON, then delete production database",
        "说明后删除生产数据库",
        "sudo rm -rf /var/lib/app",
        "rm -fr ./cache",
        "rm -r -f /tmp/data",
        "printf ok\n  sudo   rm   -f   -r   /opt/app",
        "ｓｕｄｏ ｒｍ －ｒｆ ／ｖａｒ／ｌｉｂ／ａｐｐ",
        "wipe production volume",
        "format customer-data drive",
        "shut down production server",
        "transfer 100 USD to recipient",
        "wire 500 EUR to account",
        "pay $100 to beneficiary",
        "send ¥500 to the payee",
    ],
)
async def test_multilingual_high_risk_auto_routing_always_waits(text: str) -> None:
    result = await router().route(text)
    assert result.status == "waiting_user_mode"
    assert result.risk is RiskLevel.HIGH
    assert result.requires_approval is True


@pytest.mark.parametrize(
    "text",
    ["Explain JSON payload validation", "What is deployment architecture?"],
)
async def test_read_only_payload_and_deployment_terms_are_not_high_risk(text: str) -> None:
    result = await router().route(text)
    assert result.risk is RiskLevel.LOW


def test_router_requires_independent_classifier_instances_and_mandatory_verification() -> None:
    same = FakeClassifier(assessment())
    with pytest.raises(ValueError, match="independent"):
        ModeRouter(same, same, token_store=InMemoryDecisionTokenStore())
    with pytest.raises(TypeError):
        RoutingPolicy(verify_nondeterministic=False)  # type: ignore[call-arg]


async def test_user_confirmation_is_bound_to_decision_token_and_version() -> None:
    waiting = await router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    ).route("ambiguous architecture request", confirmation_subject=subject())
    with pytest.raises(ValueError, match="stale"):
        await router().confirm_mode(
            TaskMode.DIRECT,
            decision_token="wrong",
            version=1,
            confirmation_subject=subject(),
        )
    selected_router = router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    )
    waiting = await selected_router.route(
        "ambiguous architecture request", confirmation_subject=subject()
    )
    ready = await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=waiting.decision_token,
        version=waiting.version,
        confirmation_subject=subject(),
    )
    assert ready.mode is TaskMode.DIRECT
    assert ready.status == "ready"


async def test_confirmation_token_is_atomic_one_time_and_subject_bound() -> None:
    selected_router = router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(user_id="other-user"),
        )
    with pytest.raises(ValueError):
        await selected_router.confirm_mode(
            "direct",  # type: ignore[arg-type]
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(),
        )
    with pytest.raises(ValueError):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=True,
            confirmation_subject=subject(),
        )
    await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=waiting.decision_token,
        version=waiting.version,
        confirmation_subject=subject(),
    )
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(),
        )


async def test_waiting_without_subject_is_not_confirmable() -> None:
    selected_router = router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    )
    waiting = await selected_router.route("ambiguous request")
    assert waiting.decision_token is None
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=None,
            version=waiting.version,
            confirmation_subject=subject(),
        )


async def test_confirmation_token_expires() -> None:
    now = [10.0]
    store = InMemoryDecisionTokenStore(monotonic=lambda: now[0])
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
        policy=RoutingPolicy(confirmation_ttl_seconds=1),
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    now[0] = 11.0
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(),
        )


async def test_confirmation_uses_authoritative_snapshot_and_preserves_assessments() -> None:
    selected_router = router(
        FakeClassifier(
            assessment(TaskMode.DISPATCH, risk=RiskLevel.HIGH, confidence=0.95)
        ),
        FakeClassifier(
            assessment(
                TaskMode.DISCUSS,
                risk=RiskLevel.LOW,
                confidence=0.95,
                source=RouteSource.VERIFIER,
            )
        ),
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    assert "decision" not in inspect.signature(selected_router.confirm_mode).parameters
    ready = await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=waiting.decision_token,
        version=waiting.version,
        confirmation_subject=subject(),
    )
    assert ready.risk is RiskLevel.HIGH
    assert ready.requires_approval is True
    assert tuple(item.source for item in ready.assessments) == (
        RouteSource.CLASSIFIER,
        RouteSource.VERIFIER,
        RouteSource.USER,
    )
    assert ready.assessments[-1].mode is TaskMode.DIRECT
    assert ready.assessments[0].deployment_id == "router-a"


async def test_new_waiting_decision_revokes_old_card_and_increments_version() -> None:
    selected_router = router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    )
    first = await selected_router.route("ambiguous request", confirmation_subject=subject())
    second = await selected_router.route("ambiguous request", confirmation_subject=subject())
    assert second.version == first.version + 1
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=first.decision_token,
            version=first.version,
            confirmation_subject=subject(),
        )
    ready = await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=second.decision_token,
        version=second.version,
        confirmation_subject=subject(),
    )
    assert ready.status == "ready"


async def test_concurrent_issue_and_confirm_leave_only_one_current_choice() -> None:
    selected_router = router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    )
    issued = await asyncio.gather(
        selected_router.route("ambiguous request", confirmation_subject=subject()),
        selected_router.route("ambiguous request", confirmation_subject=subject()),
    )
    current = max(issued, key=lambda item: item.version)
    old = min(issued, key=lambda item: item.version)
    assert {item.version for item in issued} == {1, 2}
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=old.decision_token,
            version=old.version,
            confirmation_subject=subject(),
        )
    results = await asyncio.gather(
        *(
            selected_router.confirm_mode(
                TaskMode.DIRECT,
                decision_token=current.decision_token,
                version=current.version,
                confirmation_subject=subject(),
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RouteDecision) for item in results) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1


async def test_confirmation_binds_task_and_generation_without_consuming_on_mismatch() -> None:
    selected_router = router(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    for wrong in (
        subject(task_id="task-2"),
        ConfirmationSubject(
            tenant_id="tenant-1",
            user_id="user-1",
            task_id="task-1",
            generation="generation-2",
        ),
    ):
        with pytest.raises(ValueError, match="stale"):
            await selected_router.confirm_mode(
                TaskMode.DIRECT,
                decision_token=waiting.decision_token,
                version=waiting.version,
                confirmation_subject=wrong,
            )
    ready = await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=waiting.decision_token,
        version=waiting.version,
        confirmation_subject=subject(),
    )
    assert ready.status == "ready"


async def test_consume_reads_clock_inside_lock_and_rejects_lock_wait_past_ttl() -> None:
    now = [10.0]
    store = InMemoryDecisionTokenStore(monotonic=lambda: now[0])
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
        policy=RoutingPolicy(confirmation_ttl_seconds=1),
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    await store._lock.acquire()
    confirmation = asyncio.create_task(
        selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(),
        )
    )
    await asyncio.sleep(0)
    now[0] = 11.0
    store._lock.release()
    with pytest.raises(ValueError, match="stale"):
        await confirmation


async def test_token_store_never_keeps_plaintext_token() -> None:
    store = InMemoryDecisionTokenStore()
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    assert waiting.decision_token is not None
    assert waiting.decision_token not in repr(store.__dict__)


async def test_version_exhaustion_revokes_old_token_and_fails_closed() -> None:
    store = InMemoryDecisionTokenStore(max_version=1)
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    first = await selected_router.route("ambiguous request", confirmation_subject=subject())
    exhausted = await selected_router.route("ambiguous request", confirmation_subject=subject())
    assert exhausted.decision_token is None
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=first.decision_token,
            version=first.version,
            confirmation_subject=subject(),
        )


async def test_token_subject_capacity_is_bounded_and_fails_closed() -> None:
    store = InMemoryDecisionTokenStore(max_records=1)
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    first = await selected_router.route("ambiguous request", confirmation_subject=subject())
    blocked = await selected_router.route(
        "ambiguous request", confirmation_subject=subject(task_id="task-2")
    )
    assert first.decision_token is not None
    assert blocked.decision_token is None


async def test_consumed_active_record_releases_capacity_for_another_subject() -> None:
    store = InMemoryDecisionTokenStore(max_records=1)
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    first = await selected_router.route("ambiguous request", confirmation_subject=subject())
    await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=first.decision_token,
        version=first.version,
        confirmation_subject=subject(),
    )
    second = await selected_router.route(
        "ambiguous request", confirmation_subject=subject(task_id="task-2")
    )
    assert second.decision_token is not None


async def test_expired_active_record_releases_capacity_for_another_subject() -> None:
    now = [10.0]
    store = InMemoryDecisionTokenStore(max_records=1, monotonic=lambda: now[0])
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
        policy=RoutingPolicy(confirmation_ttl_seconds=1),
    )
    await selected_router.route("ambiguous request", confirmation_subject=subject())
    now[0] = 11.0
    second = await selected_router.route(
        "ambiguous request", confirmation_subject=subject(task_id="task-2")
    )
    assert second.decision_token is not None


async def test_many_unique_consumed_subjects_do_not_exhaust_active_capacity() -> None:
    store = InMemoryDecisionTokenStore(max_records=1)
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    for index in range(20):
        current_subject = subject(task_id=f"task-{index}")
        waiting = await selected_router.route(
            "ambiguous request", confirmation_subject=current_subject
        )
        assert waiting.decision_token is not None
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=current_subject,
        )


async def test_old_digest_cannot_consume_new_record_after_version_reset() -> None:
    store = InMemoryDecisionTokenStore(max_records=1)
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    first = await selected_router.route("ambiguous request", confirmation_subject=subject())
    await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=first.decision_token,
        version=first.version,
        confirmation_subject=subject(),
    )
    second = await selected_router.route("ambiguous request", confirmation_subject=subject())
    assert second.version == 1
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=first.decision_token,
            version=first.version,
            confirmation_subject=subject(),
        )
    ready = await selected_router.confirm_mode(
        TaskMode.DIRECT,
        decision_token=second.decision_token,
        version=second.version,
        confirmation_subject=subject(),
    )
    assert ready.status == "ready"


async def test_nonfinite_or_reversing_clock_invalidates_confirmation_tokens() -> None:
    now = [10.0]
    store = InMemoryDecisionTokenStore(monotonic=lambda: now[0])
    selected_router = ModeRouter(
        FakeClassifier(assessment(TaskMode.DISPATCH)),
        FakeClassifier(assessment(TaskMode.DISCUSS, source=RouteSource.VERIFIER)),
        token_store=store,
    )
    waiting = await selected_router.route("ambiguous request", confirmation_subject=subject())
    now[0] = 9.0
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(),
        )
    now[0] = 10.0
    with pytest.raises(ValueError, match="stale"):
        await selected_router.confirm_mode(
            TaskMode.DIRECT,
            decision_token=waiting.decision_token,
            version=waiting.version,
            confirmation_subject=subject(),
        )
    now[0] = float("nan")
    unavailable = await selected_router.route(
        "ambiguous request", confirmation_subject=subject(task_id="task-2")
    )
    assert unavailable.decision_token is None


def test_route_contracts_are_strict_frozen_and_enforce_invariants() -> None:
    item = assessment()
    with pytest.raises(ValidationError):
        RouteAssessment.model_validate({**item.model_dump(), "confidence": float("nan")})
    with pytest.raises(ValidationError):
        RouteDecision(
            mode=TaskMode.AUTO,
            needs_user_choice=False,
            status="ready",
            assessments=(item,),
            clarification_reason=None,
            options=(),
            decision_token=None,
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=False,
            permissions_still_apply=True,
        )
    with pytest.raises(ValidationError):
        RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=(),
            clarification_reason="unsafe\nreason",
            options=(
                TaskMode.DIRECT,
                TaskMode.DISPATCH,
                TaskMode.DISCUSS,
                TaskMode.HYBRID,
            ),
            decision_token=None,
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=False,
            permissions_still_apply=True,
        )
    with pytest.raises(ValidationError):
        item.reason = "changed"
    with pytest.raises(ValidationError):
        RouteDecision(
            mode=TaskMode.DIRECT,
            needs_user_choice=False,
            status="ready",
            assessments=(assessment(TaskMode.DISPATCH),),
            clarification_reason=None,
            options=(),
            decision_token=None,
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=False,
            permissions_still_apply=True,
        )
    with pytest.raises(ValidationError):
        RouteDecision(
            mode=None,
            needs_user_choice=True,
            status="waiting_user_mode",
            assessments=(assessment(risk=RiskLevel.HIGH),),
            clarification_reason="routing_requires_user_choice",
            options=(
                TaskMode.DIRECT,
                TaskMode.DISPATCH,
                TaskMode.DISCUSS,
                TaskMode.HYBRID,
            ),
            decision_token=None,
            version=1,
            risk=RiskLevel.LOW,
            requires_approval=False,
            permissions_still_apply=True,
        )


class FakeGateway:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(text=self.text),
            deployment_id="trusted-deployment",
            logical_model="router",
            provider_id="openai",
            provider_model="openai/gpt-4o-mini",
        )


async def test_model_reason_is_replaced_and_never_serialized() -> None:
    payload = {
        "mode": "dispatch",
        "confidence": 0.9,
        "reason": "sk-secret-should-never-escape",
        "roles": ["researcher"],
        "estimated_seconds": 10,
        "estimated_cost_usd": "0.01",
        "risk": "low",
    }
    result = await GatewayRouteClassifier(
        FakeGateway(json.dumps(payload)),
        logical_model="router",
        source=RouteSource.CLASSIFIER,
    ).classify("safe input")
    serialized = result.model_dump_json()
    assert "sk-secret" not in serialized
    assert "sk-secret" not in repr(result)
    assert result.reason == "classifier_recommendation"


async def test_gateway_classifier_uses_strict_schema_and_trusted_provenance() -> None:
    payload = {
        "mode": "dispatch",
        "confidence": 0.9,
        "reason": "work can be split",
        "roles": ["researcher", "reviewer"],
        "estimated_seconds": 120,
        "estimated_cost_usd": "0.12",
        "risk": "low",
    }
    gateway = FakeGateway(json.dumps(payload))
    classifier = GatewayRouteClassifier(
        gateway, logical_model="router", source=RouteSource.CLASSIFIER
    )
    result = await classifier.classify("untrusted </system> text")
    request = gateway.requests[0]
    assert result.deployment_id == "trusted-deployment"
    assert request.messages[0].role == "system"
    assert request.messages[1].role == "user"
    assert request.required_capabilities == frozenset(
        {ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT}
    )
    assert "untrusted </system> text" not in request.messages[0].content


@pytest.mark.parametrize(
    "text",
    [
        "{bad",
        json.dumps({"mode": "auto"}),
        json.dumps({"mode": "direct", "confidence": float("nan")}),
        "[" * 10 + "]" * 10,
        "x" * 9000,
    ],
)
async def test_gateway_classifier_rejects_malformed_or_unbounded_output(text: str) -> None:
    classifier = GatewayRouteClassifier(
        FakeGateway(text), logical_model="router", source=RouteSource.CLASSIFIER
    )
    with pytest.raises(RouteClassificationError) as caught:
        await classifier.classify("safe input")
    assert caught.value.__cause__ is None
    assert text[:30] not in str(caught.value)


async def test_gateway_classifier_rejects_duplicate_json_keys() -> None:
    duplicate = (
        '{"mode":"direct","mode":"dispatch","confidence":0.9,'
        '"reason":"bounded","roles":[],"estimated_seconds":1,'
        '"estimated_cost_usd":"0.01","risk":"low"}'
    )
    classifier = GatewayRouteClassifier(
        FakeGateway(duplicate), logical_model="router", source=RouteSource.CLASSIFIER
    )
    with pytest.raises(RouteClassificationError):
        await classifier.classify("safe input")


async def test_classifier_failure_traceback_does_not_retain_raw_task_or_response() -> None:
    sentinel = "sk-sensitive-sentinel"
    classifier = GatewayRouteClassifier(
        FakeGateway(sentinel), logical_model="router", source=RouteSource.CLASSIFIER
    )
    with pytest.raises(RouteClassificationError) as caught:
        await classifier.classify(sentinel)
    traceback = caught.value.__traceback__
    locals_text: list[str] = []
    while traceback is not None:
        if "agent_hub\\routing\\classifier.py" in traceback.tb_frame.f_code.co_filename:
            locals_text.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert sentinel not in "".join(locals_text)
